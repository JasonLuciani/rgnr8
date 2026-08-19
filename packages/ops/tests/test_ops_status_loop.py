"""The closed cross-language ops-status loop.

Feeds `Fleet.set_status_from_json` the exact `ops-status/1` JSON the TS
serializers (`opsConnectorStatus` + `opsCloseStatus`) emit, and checks the
dashboard reflects it — no hand-built Python status objects.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from factory import steady_tenant
from rgnr8_ops import Fleet, TenantOpsStatus, build_ops_report

SECRET = "loop-secret"
NOW_EPOCH = 1_760_000_000
MT = ZoneInfo("America/Denver")

# Exactly what the TS opsConnectorStatus + opsCloseStatus serializers produce.
OPS_STATUS_JSON = {
    "connectors": {"total": 3, "needs_attention": 1, "stale": 0, "last_sync": "2026-09-02T06:00:00Z"},
    "close": {"period": "2026-08", "total": 7, "done": 5, "overdue": 1, "blocked": 0,
              "next_task": "Reconcile bank", "next_due": "2026-09-03"},
}


def test_from_json_assembles_both_fragments() -> None:
    st = TenantOpsStatus.from_json(OPS_STATUS_JSON)
    assert st.connectors is not None and st.connectors.needs_attention == 1
    assert st.connectors.books_current is False  # 1 needs attention
    assert st.close is not None and st.close.summary() == "2026-08 5/7 · 1 overdue"


def test_from_json_tolerates_missing_sides() -> None:
    only_conn = TenantOpsStatus.from_json({"connectors": {"total": 2, "needs_attention": 0, "stale": 0}})
    assert only_conn.connectors is not None and only_conn.close is None
    empty = TenantOpsStatus.from_json({})
    assert empty.connectors is None and empty.close is None


def test_set_status_from_json_drives_the_dashboard() -> None:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())  # tenant_id "acme"
    # accepts a dict or a JSON string (as it'd arrive over the wire)
    f.set_status_from_json("acme", json.dumps(OPS_STATUS_JSON))
    report = build_ops_report(f, datetime(2026, 9, 2, 12, 0, tzinfo=MT))
    row = next(r for r in report.rows if r.tenant_id == "acme")
    assert row.books_current is False
    assert "1 reconnect" in row.connector_summary
    assert row.close_complete is False
    assert "5/7" in row.close_summary
    assert report.books_not_current == 1
