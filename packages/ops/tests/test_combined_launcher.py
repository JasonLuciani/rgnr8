"""deploy/combined_launcher.py — ledger + web in one container.

The launcher's whole job is failure handling, so most of this drives real child
processes (tiny Python stand-ins for the ledger and gunicorn) and asserts on the
exit code and on what is still running afterwards.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"


def _launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("combined_launcher", DEPLOY / "combined_launcher.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["combined_launcher"] = mod
    spec.loader.exec_module(mod)
    return mod


L = _launcher()

BASE_ENV = {
    "RGNR8_LEDGER_TOKEN": "tok",
    "RGNR8_DATABASE_URL": "postgresql://u@db/rgnr8",
    "RGNR8_PORT": "10000",
}


# --- plan(): pure configuration --------------------------------------------


def test_plan_points_web_at_the_local_ledger_and_shares_token_and_db() -> None:
    p = L.plan({**BASE_ENV, "RGNR8_LEDGER_URL": "rgnr8-ledger-abc:10000"})
    assert p.web_env["RGNR8_LEDGER_URL"] == "http://127.0.0.1:8181"
    assert p.web_env["RGNR8_LEDGER_TOKEN"] == p.ledger_env["RGNR8_LEDGER_TOKEN"] == "tok"
    assert p.ledger_env["DATABASE_URL"] == "postgresql://u@db/rgnr8"
    assert p.ledger_env["RGNR8_LEDGER_HOST"] == "127.0.0.1"
    assert p.ledger_env["RGNR8_LEDGER_PORT"] == "8181"
    assert p.health_url == "http://127.0.0.1:8181/health"
    assert any("ignoring RGNR8_LEDGER_URL" in n for n in p.notes)


def test_plan_gunicorn_command_matches_the_render_service() -> None:
    p = L.plan(BASE_ENV, ["--max-requests", "5000"])
    assert p.web_cmd == [
        "gunicorn", "combined_entry:application", "--workers", "2",
        "--bind", "0.0.0.0:10000", "--access-logfile", "-", "--max-requests", "5000",
    ]


def test_plan_explicit_database_url_wins_and_port_falls_back_to_PORT() -> None:
    env = {**BASE_ENV, "DATABASE_URL": "postgresql://other/db", "RGNR8_LEDGER_INTERNAL_PORT": "9000"}
    del env["RGNR8_PORT"]
    env["PORT"] = "8080"
    p = L.plan(env)
    assert p.ledger_env["DATABASE_URL"] == "postgresql://other/db"
    assert p.web_env["RGNR8_LEDGER_URL"] == "http://127.0.0.1:9000"
    assert "0.0.0.0:8080" in p.web_cmd
    assert p.notes == []


def test_plan_never_lets_the_ledger_run_without_auth() -> None:
    p = L.plan({**BASE_ENV, "RGNR8_LEDGER_ALLOW_NO_AUTH": "1"})
    assert "RGNR8_LEDGER_ALLOW_NO_AUTH" not in p.ledger_env


@pytest.mark.parametrize(
    "env",
    [
        {k: v for k, v in BASE_ENV.items() if k != "RGNR8_LEDGER_TOKEN"},
        {k: v for k, v in BASE_ENV.items() if k != "RGNR8_DATABASE_URL"},
        {**BASE_ENV, "RGNR8_LEDGER_INTERNAL_PORT": "nope"},
        {**BASE_ENV, "RGNR8_LEDGER_INTERNAL_PORT": "10000"},
        {**BASE_ENV, "RGNR8_LEDGER_BOOT_TIMEOUT": "0"},
    ],
)
def test_plan_refuses_configs_that_cannot_work(env: dict[str, str]) -> None:
    with pytest.raises(L.LaunchError):
        L.plan(env)


def test_main_reports_bad_config_with_exit_code_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert L.main([], env={}) == 2
    assert "RGNR8_LEDGER_TOKEN is required" in capsys.readouterr().err


# --- run(): real child processes -------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _script(tmp_path: Path, name: str, body: str) -> list[str]:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def _fake_ledger(tmp_path: Path, port: int, *, pidfile: Path) -> list[str]:
    return _script(tmp_path, "ledger", f"""
        import http.server, os
        open({str(pidfile)!r}, "w").write(str(os.getpid()))
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/health" else 404)
                self.end_headers()
            def log_message(self, *a):
                pass
        http.server.HTTPServer(("127.0.0.1", {port}), H).serve_forever()
    """)


def _plan(ledger_cmd: list[str], web_cmd: list[str], port: int, **kw: float) -> object:
    return L.Plan(
        ledger_cmd=ledger_cmd, ledger_env=dict(os.environ),
        web_cmd=web_cmd, web_env=dict(os.environ),
        health_url=f"http://127.0.0.1:{port}/health",
        boot_timeout=kw.get("boot_timeout", 20.0), grace=kw.get("grace", 5.0),
    )


def _alive(pidfile: Path) -> bool:
    try:
        os.kill(int(pidfile.read_text()), 0)
    except (OSError, ValueError):
        return False
    return True


@pytest.fixture(autouse=True)
def _restore_signal_handlers() -> object:
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def test_ledger_that_crashes_on_boot_fails_loudly_and_web_never_starts(tmp_path: Path) -> None:
    marker = tmp_path / "web-started"
    ledger = _script(tmp_path, "ledger", "import sys; sys.exit(3)")
    web = _script(tmp_path, "web", f"open({str(marker)!r}, 'w').write('x')")
    assert L.run(_plan(ledger, web, _free_port())) == 1
    assert not marker.exists()


def test_ledger_that_never_gets_healthy_times_out(tmp_path: Path) -> None:
    pidfile = tmp_path / "ledger.pid"
    ledger = _script(tmp_path, "ledger", f"""
        import os, time
        open({str(pidfile)!r}, "w").write(str(os.getpid()))
        time.sleep(60)
    """)
    web = _script(tmp_path, "web", "pass")
    started = time.monotonic()
    assert L.run(_plan(ledger, web, _free_port(), boot_timeout=1.5)) == 1
    assert time.monotonic() - started < 15
    assert not _alive(pidfile)  # the half-started ledger was cleaned up


def test_web_dying_takes_the_container_down_and_stops_the_ledger(tmp_path: Path) -> None:
    port = _free_port()
    pidfile = tmp_path / "ledger.pid"
    web = _script(tmp_path, "web", "import time, sys; time.sleep(0.5); sys.exit(4)")
    assert L.run(_plan(_fake_ledger(tmp_path, port, pidfile=pidfile), web, port)) == 4
    assert not _alive(pidfile)


def test_ledger_dying_later_takes_the_container_down(tmp_path: Path) -> None:
    port = _free_port()
    pidfile = tmp_path / "ledger.pid"
    web_pid = tmp_path / "web.pid"
    web = _script(tmp_path, "web", f"""
        import os, signal, time
        open({str(web_pid)!r}, "w").write(str(os.getpid()))
        # Kill the ledger once we are up: a crash after a healthy boot.
        os.kill(int(open({str(pidfile)!r}).read()), signal.SIGKILL)
        time.sleep(60)
    """)
    code = L.run(_plan(_fake_ledger(tmp_path, port, pidfile=pidfile), web, port))
    assert code != 0
    assert not _alive(web_pid)


def test_sigterm_stops_both_and_exits_cleanly(tmp_path: Path) -> None:
    port = _free_port()
    pidfile = tmp_path / "ledger.pid"
    web_pid = tmp_path / "web.pid"
    web = _script(tmp_path, "web", f"""
        import os, time
        open({str(web_pid)!r}, "w").write(str(os.getpid()))
        time.sleep(60)
    """)

    def _term_when_web_is_up() -> None:
        for _ in range(200):
            if web_pid.exists():
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.05)

    threading.Thread(target=_term_when_web_is_up, daemon=True).start()
    assert L.run(_plan(_fake_ledger(tmp_path, port, pidfile=pidfile), web, port)) == 0
    assert not _alive(pidfile)
    assert not _alive(web_pid)


def test_combined_dockerfile_runs_the_launcher_and_ships_the_ledger() -> None:
    text = (DEPLOY / "Dockerfile.combined").read_text(encoding="utf-8")
    assert 'CMD ["python", "combined_launcher.py"]' in text
    # Render's Docker Command replaces an ENTRYPOINT, so none is declared.
    assert not any(line.startswith("ENTRYPOINT") for line in text.splitlines())
    assert "/app/ledger/packages" in text and "/app/ledger/node_modules" in text
    assert L.LEDGER_JS.startswith("/app/ledger/")


def test_combined_launcher_is_not_mistaken_for_a_wsgi_entrypoint() -> None:
    # test_entrypoint_posture discovers modules exposing `application`; the
    # launcher must not (it never opens the database itself).
    assert not hasattr(L, "application")


def test_health_probe_against_a_real_http_server(tmp_path: Path) -> None:
    port = _free_port()
    pidfile = tmp_path / "ledger.pid"
    proc = subprocess.Popen(_fake_ledger(tmp_path, port, pidfile=pidfile))
    try:
        assert L.wait_for_ledger(proc, f"http://127.0.0.1:{port}/health", 10) is None
        assert L._healthy(f"http://127.0.0.1:{port}/missing") is False  # 404
    finally:
        proc.terminate()
        proc.wait()
    assert L._healthy(f"http://127.0.0.1:{port}/health") is False  # nothing listening
