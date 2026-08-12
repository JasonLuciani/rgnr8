"""The write path is durable: overrides + decisions survive a fresh WebApp
(simulating a process restart) when backed by a persistent TenantStore, and
tenants can be provisioned from the `forecast-inputs/1` DTO the TS side emits.
"""

from __future__ import annotations

import json
from datetime import date

from rgnr8_forecast import (
    CashPosition,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    Money,
)
from rgnr8_forecast.io import to_dto
from rgnr8_web import (
    InMemoryTenantStore,
    JsonFileTenantStore,
    Request,
    TenantDef,
    WebApp,
)

BRIGHT = {"authorization": "Bearer tok-bright"}


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _inputs(available: str) -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=usd(available)),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),),
    )


def _app(store: object) -> WebApp:
    app = WebApp(store=store)  # type: ignore[arg-type]
    app.add_tenant("bright", "Bright Agency", _inputs("80000.00"), ForecastConfig(minimum_cash=usd("10000.00")), token="tok-bright")
    return app


def _today(app: WebApp) -> dict[str, object]:
    return json.loads(app.handle(Request("GET", "/api/bright/today", BRIGHT)).body)


def test_overrides_survive_restart_with_file_store(tmp_path: object) -> None:
    path = f"{tmp_path}/tenants.json"  # type: ignore[str-bytes-safe]
    # process 1: owner raises the floor and records a decision
    app1 = _app(JsonFileTenantStore(path))
    r = app1.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"minimum_cash": "55000.00"})))
    assert r.status == 200
    app1.handle(Request("POST", "/api/bright/decisions", BRIGHT, json.dumps({"label": "Chase INV-1"})))

    # process 2: brand-new WebApp + fresh tenant registration, same file store
    app2 = _app(JsonFileTenantStore(path))
    assert _today(app2)["floor"] == "55000.00"  # override rehydrated and applied
    listed = json.loads(app2.handle(Request("GET", "/api/bright/decisions", BRIGHT)).body)
    labels = [d.get("label") for d in listed["decisions"]]
    assert "Chase INV-1" in labels
    # the assumption change was also logged
    assert any(d.get("kind") == "assumption" for d in listed["decisions"])


def test_file_store_is_valid_json_on_disk(tmp_path: object) -> None:
    path = f"{tmp_path}/tenants.json"  # type: ignore[str-bytes-safe]
    app = _app(JsonFileTenantStore(path))
    app.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"payment_override": {"customer_id": "acme", "days_late": 45}})))
    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["bright"]["payment_overrides"]["acme"] == 45


def test_in_memory_store_does_not_persist_across_apps() -> None:
    # two apps with independent in-memory stores don't share state
    app1 = _app(InMemoryTenantStore())
    app1.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"minimum_cash": "77000.00"})))
    app2 = _app(InMemoryTenantStore())
    assert _today(app2)["floor"] == "10000.00"  # the configured default, no override


def test_provision_tenant_from_dto() -> None:
    # the TS side emits a forecast-inputs/1 DTO; the web app can load a tenant from it
    dto = to_dto(_inputs("42000.00"))
    td = TenantDef.from_dto("bright", "Bright Agency", "tok-bright", dto, minimum_cash=usd("9000.00"))
    app = WebApp.from_defs([td])
    today = _today(app)
    assert today["tenant"] == "bright"
    assert today["cash_today"] == "42000.00"
    assert today["floor"] == "9000.00"


def test_provision_from_dto_as_json_text() -> None:
    # accepts the DTO as raw JSON text too (as it would arrive over the wire / from a file)
    dto_text = json.dumps(to_dto(_inputs("30000.00")))
    td = TenantDef.from_dto("bright", "Bright", "tok-bright", dto_text, minimum_cash=usd("5000.00"))
    app = WebApp.from_defs([td])
    assert _today(app)["cash_today"] == "30000.00"
