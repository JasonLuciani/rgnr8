import json

from rgnr8_web import Request
from factory import app_with_two_tenants

BRIGHT = {"authorization": "Bearer tok-bright"}


def _today(app):  # type: ignore[no-untyped-def]
    return json.loads(app.handle(Request("GET", "/api/bright/today", BRIGHT)).body)


def test_raising_the_floor_changes_the_status_and_recomputes() -> None:
    app = app_with_two_tenants()
    before = _today(app)
    assert before["floor"] == "10000.00"

    # Owner raises the minimum-cash floor well above the trough.
    r = app.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"minimum_cash": "90000.00"})))
    assert r.status == 200
    after = json.loads(r.body)
    assert after["floor"] == "90000.00"
    # the recomputed forecast reflects the new floor immediately
    assert _today(app)["floor"] == "90000.00"
    # raising the floor deepens the projected shortfall — proof the forecast recomputed
    assert float(after["breach"]["shortfall"]) > float(before["breach"]["shortfall"])


def test_payment_override_changes_the_forecast() -> None:
    app = app_with_two_tenants()
    before = _today(app)
    # Push the customer's payment timing far out — worsens the projection.
    r = app.handle(
        Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"payment_override": {"customer_id": "acme", "days_late": 120}})),
    )
    assert r.status == 200
    after = json.loads(r.body)
    assert after["trough"]["amount"] != before["trough"]["amount"]


def test_assumptions_validation() -> None:
    app = app_with_two_tenants()
    assert app.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"minimum_cash": "abc"}))).status == 400
    assert app.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({}))).status == 400


def test_decision_log_records_and_lists() -> None:
    app = app_with_two_tenants()
    # changing an assumption records a decision
    app.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"minimum_cash": "12000.00"})))
    # an explicit decision / resolved action
    r = app.handle(Request("POST", "/api/bright/decisions", BRIGHT, json.dumps({"label": "Called Northwind about INV-201", "kind": "action_resolved"})))
    assert r.status == 201
    listed = json.loads(app.handle(Request("GET", "/api/bright/decisions", BRIGHT)).body)
    assert listed["tenant"] == "bright"
    assert len(listed["decisions"]) == 2
    assert any(d.get("kind") == "action_resolved" for d in listed["decisions"])


def test_writes_are_tenant_isolated() -> None:
    app = app_with_two_tenants()
    # bright's token cannot write acme's assumptions
    r = app.handle(Request("POST", "/api/acme/assumptions", BRIGHT, json.dumps({"minimum_cash": "1.00"})))
    assert r.status == 403
