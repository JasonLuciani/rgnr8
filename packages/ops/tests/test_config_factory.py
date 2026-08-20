"""12-factor Settings + production app factory (auth modes, DB readiness)."""

import sqlite3

import pytest
from factory import steady_tenant
from rgnr8_forecast import Money
from rgnr8_ops import (
    ConfigError,
    Fleet,
    Settings,
    SqlFleetStore,
    bootstrap_python_schemas,
    build_web_app,
    create_application,
    readiness,
)
from rgnr8_web import Request


def test_settings_defaults_and_dev_warning() -> None:
    s = Settings.from_env({"RGNR8_JWT_SECRET": "x"})
    assert s.auth_mode == "hs256"
    assert s.is_production_db is False
    assert any("in-memory" in w for w in s.warnings)
    assert s.placeholder == "?"


def test_settings_postgres_placeholder_and_redaction() -> None:
    s = Settings.from_env({
        "RGNR8_JWT_SECRET": "sekret",
        "RGNR8_DATABASE_URL": "postgresql://u:p@host/db",
        "RGNR8_SENDGRID_API_KEY": "sg",
    })
    assert s.is_production_db and s.placeholder == "%s"
    red = s.redacted()
    assert red["jwt_secret"] == "set" and red["sendgrid"] == "set"
    assert red["database"] == "postgres"
    # the actual secret never appears in the redacted view
    assert "sekret" not in str(red)


def test_missing_required_settings_fail_fast() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"RGNR8_AUTH_MODE": "hs256"})  # no secret
    with pytest.raises(ConfigError):
        Settings.from_env({"RGNR8_AUTH_MODE": "jwks", "RGNR8_JWKS_URL": "https://x/jwks"})  # no iss/aud
    with pytest.raises(ConfigError):
        Settings.from_env({"RGNR8_AUTH_MODE": "bogus"})


def test_build_web_app_hs256_serves_a_health_check() -> None:
    s = Settings.from_env({"RGNR8_JWT_SECRET": "x"})
    app = build_web_app(s)
    assert app.handle(Request("GET", "/ready")).status == 200


def test_jwks_mode_builds_without_network() -> None:
    s = Settings.from_env({
        "RGNR8_AUTH_MODE": "jwks",
        "RGNR8_JWKS_URL": "https://issuer.example.com/.well-known/jwks.json",
        "RGNR8_JWT_ISSUER": "https://issuer.example.com/",
        "RGNR8_JWT_AUDIENCE": "rgnr8-api",
    })
    app = build_web_app(s)  # constructs the JWKS provider lazily — no fetch here
    # an unauth request is rejected (no token) without any network call
    assert app.handle(Request("GET", "/api/acme/today")).status == 401


# --- Phase 0 composition: login, ledger, Ask, QBO all wired from one factory ---

def test_session_secret_wires_a_working_browser_login() -> None:
    s = Settings.from_env({"RGNR8_JWT_SECRET": "x", "RGNR8_SESSION_SECRET": "sess"})
    app = build_web_app(s)
    assert app._auth_service is not None      # real password login, not email-only
    assert app._session_secret is not None
    assert app._sso_mode() is False           # browser self-issued login is active
    # POST /login is a real credential check now — not the "self-issued disabled" 400
    r = app.handle(Request("POST", "/login", {"content-type": "application/x-www-form-urlencoded"},
                           "email=nobody@acme.com&password=wrong"))
    assert "Self-issued login is disabled" not in r.body


def test_jwks_mode_has_no_self_issued_login() -> None:
    s = Settings.from_env({
        "RGNR8_AUTH_MODE": "jwks",
        "RGNR8_JWKS_URL": "https://i/jwks", "RGNR8_JWT_ISSUER": "https://i/",
        "RGNR8_JWT_AUDIENCE": "aud",
    })
    app = build_web_app(s)
    assert app._auth_service is None and app._session_secret is None  # log in via the IdP


def test_ledger_url_wires_the_client() -> None:
    off = build_web_app(Settings.from_env({"RGNR8_JWT_SECRET": "x"}))
    assert off._ledger is None                # absent → "not configured", not pretend
    on = build_web_app(Settings.from_env({"RGNR8_JWT_SECRET": "x",
                                          "RGNR8_LEDGER_URL": "http://ledger:8181",
                                          "RGNR8_LEDGER_TOKEN": "svc"}))
    assert on._ledger is not None


def test_anthropic_key_wires_ask() -> None:
    off = build_web_app(Settings.from_env({"RGNR8_JWT_SECRET": "x"}))
    assert off._ask_svc is None
    on = build_web_app(Settings.from_env({"RGNR8_JWT_SECRET": "x",
                                          "RGNR8_ANTHROPIC_API_KEY": "sk-live"}))
    assert on._ask_svc is not None


def test_qbo_fails_closed_without_a_secret_key_on_a_database() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({
            "RGNR8_JWT_SECRET": "x", "RGNR8_DATABASE_URL": "postgresql://u@h/db",
            "RGNR8_QBO_CLIENT_ID": "id", "RGNR8_QBO_CLIENT_SECRET": "sec",
        })


def test_qbo_wired_when_configured_with_a_key() -> None:
    s = Settings.from_env({
        "RGNR8_JWT_SECRET": "x", "RGNR8_SESSION_SECRET": "sess",
        "RGNR8_QBO_CLIENT_ID": "id", "RGNR8_QBO_CLIENT_SECRET": "sec",
    })
    app = build_web_app(s)  # in-memory (no DB) → connection store needs no cipher
    assert app._qbo is not None


def test_readiness_does_a_real_db_round_trip() -> None:
    conn = sqlite3.connect(":memory:")
    s = Settings.from_env({"RGNR8_JWT_SECRET": "x", "RGNR8_DATABASE_URL": "sqlite://"})
    r = readiness(s, conn)
    assert r["status"] == "ready" and r["db"] == "ok"
    assert readiness(s, None)["db"] == "in-memory"


def test_create_application_registers_persisted_fleet_over_wsgi() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_python_schemas(conn)
    fleet = Fleet(jwt_secret="x", clock=lambda: 1, store=SqlFleetStore(conn))
    fleet.onboard(steady_tenant())

    app = create_application({"RGNR8_JWT_SECRET": "x"}, conn=conn)
    seen: dict[str, object] = {}

    def sr(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"] = status

    body = b"".join(app({"REQUEST_METHOD": "GET", "PATH_INFO": "/ready",
                          "QUERY_STRING": "", "CONTENT_LENGTH": "0"}, sr))  # type: ignore[operator,arg-type]
    assert str(seen["status"]).startswith("200")
    # the rehydrated tenant is registered → its count shows in /ready
    import json

    assert json.loads(body.decode())["tenants"] == 1
    _ = Money  # (imported for parity with other tests)


def test_create_application_builds_in_memory_dev_app_with_edge_hardening() -> None:
    # no conn → in-memory dev path still builds a served, hardened WSGI app
    app = create_application({"RGNR8_JWT_SECRET": "x"})
    status_line: list[str] = []
    headers: list[tuple[str, str]] = []

    def sr(status: str, hdrs: list[tuple[str, str]]) -> None:
        status_line.append(status)
        headers.extend(hdrs)

    body = b"".join(app({"REQUEST_METHOD": "GET", "PATH_INFO": "/ready",
                         "QUERY_STRING": "", "CONTENT_LENGTH": "0"}, sr))  # type: ignore[operator,arg-type]
    assert status_line and status_line[0].startswith("200")
    assert body  # a body was served
    # the observability + edge-hardening stack is bound (security headers merged)
    header_names = {k.lower() for k, _ in headers}
    assert "content-security-policy" in header_names
    assert "x-frame-options" in header_names
