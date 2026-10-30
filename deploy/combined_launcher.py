"""Run the ledger and the web app in ONE container (deploy/Dockerfile.combined).

rgnr8-web and rgnr8-ledger normally run as two Render services. This launcher
is the opt-in alternative: it starts the Node ledger bound to 127.0.0.1, waits
until its /health answers, then starts gunicorn pointed at it. One container,
one bill.

Contract:

  * The ledger is reachable only from inside the container
    (RGNR8_LEDGER_HOST=127.0.0.1, port RGNR8_LEDGER_INTERNAL_PORT, default 8181).
    The web app is always pointed at it: RGNR8_LEDGER_URL is overwritten with
    http://127.0.0.1:<port>, so a leftover value from the two-service setup
    cannot send the books screens to the old ledger.
  * Both use the same RGNR8_LEDGER_TOKEN (required). The ledger gets
    DATABASE_URL, defaulting to RGNR8_DATABASE_URL when unset.
  * Fail loudly: if the ledger exits or is not healthy within
    RGNR8_LEDGER_BOOT_TIMEOUT seconds (default 90), the launcher exits non-zero
    before gunicorn ever binds, so the Render deploy fails its health check and
    the previous instance keeps serving.
  * If either process dies later, the other is stopped and the launcher exits
    non-zero, so Render restarts the whole container rather than leaving a web
    app with no ledger behind it.
  * SIGTERM/SIGINT are forwarded (gunicorn first, so in-flight requests finish
    against a live ledger, then the ledger). Children that ignore them are
    killed after RGNR8_SHUTDOWN_GRACE seconds (default 25; Render waits 30).

Gunicorn runs `combined_entry:application` with RGNR8_WORKERS workers
(default 2) on 0.0.0.0:$RGNR8_PORT (else $PORT, else 10000). Any extra
command-line arguments are appended to the gunicorn command.

Anything here is stdlib-only on purpose: it runs before the app is importable.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import FrameType

LEDGER_JS = "/app/ledger/packages/ledger-service/dist/bin.js"


def _log(msg: str) -> None:
    sys.stderr.write(f"[combined] {msg}\n")
    sys.stderr.flush()


class LaunchError(RuntimeError):
    """Configuration that cannot work; reported before anything starts."""


@dataclass(frozen=True)
class Plan:
    ledger_cmd: list[str]
    ledger_env: dict[str, str]
    web_cmd: list[str]
    web_env: dict[str, str]
    health_url: str
    boot_timeout: float
    grace: float
    notes: list[str] = field(default_factory=list)


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise LaunchError(f"{key}={raw!r} is not a number") from exc
    if value <= 0:
        raise LaunchError(f"{key} must be positive")
    return value


def plan(env: Mapping[str, str], extra_args: Sequence[str] = ()) -> Plan:
    """Work out both commands and environments. Pure, so it is unit-tested."""
    notes: list[str] = []
    token = env.get("RGNR8_LEDGER_TOKEN", "")
    if not token:
        raise LaunchError(
            "RGNR8_LEDGER_TOKEN is required: the web app presents it and the "
            "ledger refuses to serve without it"
        )
    database_url = env.get("DATABASE_URL") or env.get("RGNR8_DATABASE_URL", "")
    if not database_url:
        raise LaunchError(
            "no database: set DATABASE_URL or RGNR8_DATABASE_URL (the ledger "
            "would otherwise run in-memory and lose every posting on restart)"
        )
    port_raw = env.get("RGNR8_LEDGER_INTERNAL_PORT", "8181").strip() or "8181"
    if not port_raw.isdigit() or not 0 < int(port_raw) < 65536:
        raise LaunchError(f"RGNR8_LEDGER_INTERNAL_PORT={port_raw!r} is not a port")
    ledger_port = int(port_raw)
    web_port = (env.get("RGNR8_PORT") or env.get("PORT") or "10000").strip()
    if web_port == str(ledger_port):
        raise LaunchError(f"web port and ledger port are both {web_port}")

    local_url = f"http://127.0.0.1:{ledger_port}"
    previous = env.get("RGNR8_LEDGER_URL", "")
    if previous and previous.rstrip("/") != local_url:
        notes.append(f"ignoring RGNR8_LEDGER_URL={previous!r}; using {local_url}")

    ledger_env = dict(env)
    ledger_env.update({
        "DATABASE_URL": database_url,
        "RGNR8_LEDGER_HOST": "127.0.0.1",
        "RGNR8_LEDGER_PORT": str(ledger_port),
    })
    ledger_env.setdefault("NODE_ENV", "production")  # as deploy/Dockerfile.ledger
    # The ledger must never come up unauthenticated in this mode.
    ledger_env.pop("RGNR8_LEDGER_ALLOW_NO_AUTH", None)

    web_env = dict(env)
    web_env["RGNR8_LEDGER_URL"] = local_url
    web_env["RGNR8_PORT"] = web_port

    workers = (env.get("RGNR8_WORKERS") or "2").strip()
    web_cmd = [
        "gunicorn", "combined_entry:application",
        "--workers", workers,
        "--bind", f"0.0.0.0:{web_port}",
        "--access-logfile", "-",
        *extra_args,
    ]
    ledger_js = env.get("RGNR8_LEDGER_JS", LEDGER_JS)
    return Plan(
        ledger_cmd=["node", ledger_js],
        ledger_env=ledger_env,
        web_cmd=web_cmd,
        web_env=web_env,
        health_url=f"{local_url}/health",
        boot_timeout=_float(env, "RGNR8_LEDGER_BOOT_TIMEOUT", 90.0),
        grace=_float(env, "RGNR8_SHUTDOWN_GRACE", 25.0),
        notes=notes,
    )


def _healthy(url: str) -> bool:
    """GET url (http://host:port/path) and report whether it answered 200.

    A raw socket rather than urllib on purpose: urllib pulls in ssl/http/email
    and costs this always-resident supervisor several MB for one loopback probe.
    """
    try:
        hostport, _, path = url.removeprefix("http://").partition("/")
        host, _, port = hostport.partition(":")
        with socket.create_connection((host, int(port or 80)), timeout=2) as conn:
            conn.sendall(f"GET /{path} HTTP/1.0\r\nHost: {hostport}\r\n\r\n".encode())
            status_line = conn.recv(64).split(b"\r\n", 1)[0]
    except (OSError, ValueError):
        return False
    parts = status_line.split()
    return len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1] == b"200"


def wait_for_ledger(
    proc: subprocess.Popen[bytes],
    url: str,
    timeout: float,
    *,
    probe: Callable[[str], bool] = _healthy,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stopping: Callable[[], bool] = lambda: False,
) -> str | None:
    """None once the ledger answers /health; otherwise why it never did."""
    deadline = clock() + timeout
    while True:
        code = proc.poll()
        if code is not None:
            return f"ledger exited with code {code} before becoming healthy"
        if stopping():
            return "shutdown requested while the ledger was starting"
        if probe(url):
            return None
        if clock() >= deadline:
            return f"ledger not healthy at {url} after {timeout:g}s"
        sleep(0.5)


def _stop(procs: Sequence[tuple[str, subprocess.Popen[bytes]]], grace: float) -> None:
    """Terminate in order, each with the remaining grace; kill stragglers."""
    deadline = time.monotonic() + grace
    for name, proc in procs:
        if proc.poll() is None:
            _log(f"stopping {name} (pid {proc.pid})")
            proc.terminate()
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _log(f"{name} ignored SIGTERM; killing")
                proc.kill()
                proc.wait()


def run(p: Plan) -> int:
    for note in p.notes:
        _log(note)
    stop_requested: list[int] = []

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        stop_requested.append(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)

    _log(f"starting ledger: {' '.join(p.ledger_cmd)} on {p.health_url.rsplit('/', 1)[0]}")
    ledger = subprocess.Popen(p.ledger_cmd, env=p.ledger_env)
    problem = wait_for_ledger(
        ledger, p.health_url, p.boot_timeout, stopping=lambda: bool(stop_requested),
    )
    if problem is not None:
        _stop([("ledger", ledger)], p.grace)
        if stop_requested:
            _log(problem)
            return 0
        _log(f"FATAL: {problem}; not starting the web app")
        return 1
    _log("ledger healthy; starting web: " + " ".join(p.web_cmd))
    web = subprocess.Popen(p.web_cmd, env=p.web_env)
    # Gunicorn first so in-flight requests finish against a live ledger.
    order = [("web", web), ("ledger", ledger)]

    while True:
        if stop_requested:
            _log(f"received signal {stop_requested[0]}; shutting down")
            _stop(order, p.grace)
            return 0
        for name, proc in order:
            code = proc.poll()
            if code is not None:
                _log(f"FATAL: {name} exited with code {code}; stopping the rest")
                _stop([o for o in order if o[1] is not proc], p.grace)
                return code if code > 0 else 1
        time.sleep(0.5)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        p = plan(os.environ if env is None else env, args)
    except LaunchError as exc:
        _log(f"FATAL: {exc}")
        return 2
    return run(p)


if __name__ == "__main__":
    sys.exit(main())
