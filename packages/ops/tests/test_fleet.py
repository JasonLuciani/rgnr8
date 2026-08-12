from datetime import datetime
from zoneinfo import ZoneInfo

from rgnr8_briefing import RecordingDeliverer
from rgnr8_web import Request, verify_jwt
from rgnr8_ops import Fleet, build_ops_report, render_ops_html
from factory import steady_tenant, at_risk_tenant

SECRET = "ops-secret"
NOW_EPOCH = 1_760_000_000
MT = ZoneInfo("America/Denver")


def _fleet() -> Fleet:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())
    f.onboard(at_risk_tenant())
    return f


def test_onboard_registers_tenant_everywhere() -> None:
    f = _fleet()
    assert set(f.tenants) == {"acme", "bright"}
    # subscription created per tenant
    subs = {s.tenant_id for s in f.subscriptions.list()}
    assert subs == {"acme", "bright"}
    # runtime tenant source resolves them
    assert f.tenant_source.resolve("acme") is not None
    assert f.tenant_source.resolve("bright") is not None


def test_minted_jwt_authorizes_the_web_app() -> None:
    f = _fleet()
    app = f.web_app()
    token = f.mint_token("acme")
    claims = verify_jwt(token, SECRET, now=NOW_EPOCH)
    assert claims["tenant"] == "acme"
    r = app.handle(Request("GET", "/api/acme/today", {"authorization": f"Bearer {token}"}))
    assert r.status == 200
    # acme's token cannot reach bright
    blocked = app.handle(Request("GET", "/api/bright/today", {"authorization": f"Bearer {token}"}))
    assert blocked.status == 403


def test_ops_report_ranks_at_risk_first_and_reports_status() -> None:
    f = _fleet()
    report = build_ops_report(f, datetime(2026, 9, 2, 12, 0, tzinfo=MT))
    assert report.total == 2
    # worst-first ordering: the at-risk client leads
    assert report.rows[0].tenant_id == "bright"
    assert report.rows[0].status == "AT_RISK"
    assert report.rows[0].breached is True
    assert report.at_risk == 1
    # steady client present and STABLE
    acme = next(r for r in report.rows if r.tenant_id == "acme")
    assert acme.status in ("STABLE", "WATCH")


def test_ops_report_tracks_delivery_state_across_a_runtime_tick() -> None:
    f = _fleet()
    now = datetime(2026, 9, 2, 12, 0, tzinfo=MT)  # a Wednesday; Mon 08:00 fire already passed
    before = build_ops_report(f, now)
    assert all(not r.delivered_this_period for r in before.rows)
    assert all(r.next_due for r in before.rows)

    # run one delivery tick → subscriptions advance
    f.delivery_runtime(RecordingDeliverer()).tick(now)
    after = build_ops_report(f, now)
    assert all(r.delivered_this_period for r in after.rows)
    assert after.delivered == 2


def test_render_ops_html_dashboard() -> None:
    f = _fleet()
    html = render_ops_html(build_ops_report(f, datetime(2026, 9, 2, 12, 0, tzinfo=MT)))
    assert "<!doctype html>" in html
    assert "beta fleet" in html
    assert "Bright Agency" in html and "Acme Co" in html
    assert "AT RISK" in html  # banner flags the at-risk client
