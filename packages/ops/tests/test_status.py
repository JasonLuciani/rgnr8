"""Books-current + close-progress folded into the fleet dashboard.

Attaches connector-health and close-progress (mirrors of the TS
`@rgnr8/connectors` health report and `@rgnr8/close` calendar status) to each
client and checks the ops report + dashboard surface them alongside cash.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from factory import at_risk_tenant, steady_tenant
from rgnr8_ops import (
    CloseProgress,
    ConnectorHealth,
    Fleet,
    TenantOpsStatus,
    build_ops_report,
    render_ops_html,
)

SECRET = "status-secret"
NOW_EPOCH = 1_760_000_000
MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=MT)


def _fleet() -> Fleet:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())
    f.onboard(at_risk_tenant())
    return f


def test_books_current_and_closed_client() -> None:
    f = _fleet()
    f.set_status(
        "acme",
        TenantOpsStatus(
            connectors=ConnectorHealth(total=3, needs_attention=0, stale=0, last_sync="2026-09-02T06:00:00Z"),
            close=CloseProgress(period="2026-08", total=7, done=7, overdue=0, blocked=0),
        ),
    )
    report = build_ops_report(f, NOW)
    acme = next(r for r in report.rows if r.tenant_id == "acme")
    assert acme.books_current is True
    assert acme.connector_summary == "3 ok"
    assert acme.close_complete is True
    assert acme.close_summary == "2026-08 closed"
    assert report.closes_done == 1


def test_books_behind_client_flags_the_fleet() -> None:
    f = _fleet()
    f.set_status(
        "bright",
        TenantOpsStatus(
            connectors=ConnectorHealth(total=2, needs_attention=1, stale=1),
            close=CloseProgress(period="2026-08", total=7, done=4, overdue=2, blocked=1,
                                next_task="Reconcile bank", next_due="2026-09-03"),
        ),
    )
    report = build_ops_report(f, NOW)
    bright = next(r for r in report.rows if r.tenant_id == "bright")
    assert bright.books_current is False
    assert "reconnect" in bright.connector_summary and "stale" in bright.connector_summary
    assert bright.close_complete is False
    assert "4/7" in bright.close_summary and "2 overdue" in bright.close_summary
    assert report.books_not_current == 1


def test_missing_status_renders_as_dash() -> None:
    f = _fleet()  # no statuses attached
    report = build_ops_report(f, NOW)
    for r in report.rows:
        assert r.books_current is None
        assert r.connector_summary == "—"
        assert r.close_complete is None
    assert report.books_not_current == 0
    assert report.closes_done == 0


def test_dashboard_shows_books_and_close_columns() -> None:
    f = _fleet()
    f.set_status(
        "bright",
        TenantOpsStatus(
            connectors=ConnectorHealth(total=2, needs_attention=1, stale=0),
            close=CloseProgress(period="2026-08", total=7, done=4, overdue=1, blocked=0),
        ),
    )
    html = render_ops_html(build_ops_report(f, NOW))
    assert "<th>Books</th>" in html
    assert "<th>Close</th>" in html
    assert "1 reconnect" in html
    assert "books behind" in html  # banner flags it even when cash is fine
    assert "closed" in html or "2026-08 4/7" in html


def test_set_status_rejects_unknown_tenant() -> None:
    f = _fleet()
    try:
        f.set_status("nobody", TenantOpsStatus())
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown tenant")
