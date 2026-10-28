"""Version skew between the web app and the ledger service.

#24's ledger deploy failed and Render kept the #23 ledger serving behind the new
web app. The old ledger answered settings writes with 200 and dropped the new
fields, and nothing on any screen said the two were mismatched. These tests pin
the check that now does.
"""

import json
import re
from pathlib import Path
from typing import Any

from factory import app_with_two_tenants

from rgnr8_web import LedgerClient, LedgerResponse, Request, WebApp
from rgnr8_web.deploy_check import EXPECTED_LEDGER_SCHEMA, LedgerBuild, ledger_skew

MIGRATIONS_TS = (
    Path(__file__).resolve().parents[2] / "ledger-service" / "src" / "migrations.ts"
)


def test_expected_schema_matches_the_ledgers_migrations() -> None:
    # A new ledger migration without raising this would leave the web app
    # trusting a ledger that lacks it; raising it without one would banner
    # every page. Either way, CI says so.
    versions = [int(v) for v in re.findall(r"\{ version: (\d+), name:", MIGRATIONS_TS.read_text())]
    assert versions, "no migrations found — has migrations.ts moved?"
    assert max(v for v in versions if v < 100) == EXPECTED_LEDGER_SCHEMA


def _build(status: int, body: dict[str, Any]) -> LedgerBuild:
    return LedgerBuild.from_ready(status, body)


def test_a_ledger_on_the_expected_schema_and_commit_is_quiet() -> None:
    ledger = _build(200, {"status": "ready", "version": "abc1234", "schema": EXPECTED_LEDGER_SCHEMA})
    assert ledger_skew("abc1234", ledger) is None


def test_a_ledger_that_predates_schema_reporting_is_skew() -> None:
    # Exactly production after #24: the old ledger says only {"status": "ready"}.
    msg = ledger_skew("5582216", _build(200, {"status": "ready"}))
    assert msg is not None
    assert "older build" in msg


def test_a_ledger_behind_on_schema_is_skew() -> None:
    msg = ledger_skew("dev", _build(200, {"status": "ready", "version": "dev",
                                          "schema": EXPECTED_LEDGER_SCHEMA - 1}))
    assert msg is not None and "older build" in msg


def test_different_commits_are_skew_but_dev_builds_are_not_compared() -> None:
    ledger = _build(200, {"status": "ready", "version": "1111111aaaa", "schema": EXPECTED_LEDGER_SCHEMA})
    msg = ledger_skew("2222222bbbb", ledger)
    assert msg is not None and "1111111" in msg and "2222222" in msg
    assert ledger_skew("dev", ledger) is None
    assert ledger_skew("", ledger) is None


def test_an_unreachable_ledger_is_not_called_skew() -> None:
    # Screens already say when the books can't be read; skew is a different claim.
    assert ledger_skew("abc", _build(503, {"error": "ledger service did not answer (URLError)"})) is None
    assert ledger_skew("abc", _build(404, {"error": "not found"})) is None


def test_a_ledger_with_its_database_down_still_names_its_build() -> None:
    ledger = _build(503, {"status": "unavailable", "version": "abc", "schema": EXPECTED_LEDGER_SCHEMA})
    assert ledger.reachable and ledger.version == "abc"


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _ReadyLedger:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls = 0

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        if path == "/ready":
            self.calls += 1
            return LedgerResponse(200, self.body)
        return LedgerResponse(404, {"error": "not found"})


def test_ready_reports_the_ledgers_build_and_the_skew() -> None:
    clock = _Clock()
    app = WebApp(build_version="newcommit", monotonic=clock)
    ledger = _ReadyLedger({"status": "ready"})
    app.set_ledger(LedgerClient(ledger))
    body = json.loads(app.handle(Request("GET", "/ready")).body)
    assert body["status"] == "ready"  # reported, never a failed health check
    assert body["build"] == "newcommit"
    assert body["ledger"] == {"reachable": True, "version": None, "schema": None}
    assert "older build" in body["ledger_skew"]

    # Cached for a minute, then re-read: the redeployed ledger clears it.
    ledger.body = {"status": "ready", "version": "newcommit", "schema": EXPECTED_LEDGER_SCHEMA}
    app.handle(Request("GET", "/ready"))
    assert ledger.calls == 1
    clock.t = 61.0
    body = json.loads(app.handle(Request("GET", "/ready")).body)
    assert ledger.calls == 2
    assert body["ledger_skew"] is None


def test_ready_without_a_ledger_reports_none() -> None:
    body = json.loads(WebApp().handle(Request("GET", "/ready")).body)
    assert body["ledger"] is None and body["ledger_skew"] is None


def _page(app: WebApp, path: str) -> str:
    return app.handle(Request("GET", path, {"authorization": "Bearer tok-acme"}, "")).body


def test_every_screen_carries_the_banner_while_the_ledger_is_behind() -> None:
    app = app_with_two_tenants()
    ledger = _ReadyLedger({"status": "ready"})
    app.set_ledger(LedgerClient(ledger))
    for path in ("/t/acme", "/t/acme/invoices"):
        page = _page(app, path)
        assert 'class="banner warn" role="alert"' in page, path
        assert "older build than this app" in page, path

    ledger.body = {"status": "ready", "version": "dev", "schema": EXPECTED_LEDGER_SCHEMA}
    app.set_ledger(LedgerClient(ledger))  # a new client drops the cached answer
    assert "older build than this app" not in _page(app, "/t/acme")
