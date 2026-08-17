"""The operator console: fleet-in-shell + onboarding panel, self-contained."""

from datetime import datetime
from zoneinfo import ZoneInfo

from rgnr8_forecast import Money
from rgnr8_ops import (
    ConnectorHealth,
    Fleet,
    TenantOpsStatus,
    build_ops_report,
    render_operator_console,
)
from factory import at_risk_tenant, steady_tenant


def _usd(s: str) -> Money:
    return Money.from_decimal(s)

SECRET = "console-secret"
NOW_EPOCH = 1_760_000_000
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=ZoneInfo("America/Denver"))


def _report(fleet: Fleet):
    return build_ops_report(fleet, NOW)


def test_console_renders_fleet_and_onboarding() -> None:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())
    f.onboard(at_risk_tenant())
    f.set_status("bright", TenantOpsStatus(connectors=ConnectorHealth(total=3, needs_attention=1, stale=0)))
    html = render_operator_console(_report(f), operator="ops@rgnr8.co")

    assert "Operator console" in html and "Onboard a client" in html
    assert "ops@rgnr8.co" in html and "Operator" in html
    assert "Acme Co" in html and "Bright Agency" in html      # the fleet table
    assert "forecast-inputs/1" in html                        # the onboarding DTO field
    assert 'action="/operator/onboard"' in html
    # worst-first: the AT_RISK client sorts above the STABLE one
    assert html.index("Bright Agency") < html.index("Acme Co")
    # books-behind surfaces (1 connector needs reconnect)
    assert "reconnect" in html
    # self-contained
    assert "http://" not in html and "https://" not in html


def test_console_renders_trust_drift_with_amount_and_in_sync() -> None:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())     # acme
    f.onboard(at_risk_tenant())    # bright
    # bright: ledger and bank disagree by $1,000.00 → a MAJOR drift (past $100 tol)
    f.set_recon("bright", _usd("250000.00"), _usd("249000.00"), _usd("250000.00"))
    # acme: all three figures agree → in sync
    f.set_recon("acme", _usd("120000.00"), _usd("120000.00"), _usd("120000.00"))
    html = render_operator_console(_report(f))

    assert "Trust" in html                     # the trust column header
    assert "1000.00" in html and "drift" in html   # the drift amount surfaces
    assert "in sync" in html                   # the agreeing client reads in sync
    report = _report(f)
    assert report.recon_drift == 1


def test_console_omits_trust_when_no_recon_attached() -> None:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())
    report = _report(f)
    row = report.rows[0]
    assert row.recon_severity is None and row.recon_in_sync is None
    assert report.recon_drift == 0


def test_console_empty_fleet_prompts_onboarding() -> None:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    html = render_operator_console(_report(f))
    assert "No clients onboarded yet" in html
    assert "operator@rgnr8.co" in html  # default operator identity


def test_console_shows_coa_picker_and_live_badge() -> None:
    from rgnr8_ops import category_catalog
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())  # "acme"
    html = render_operator_console(
        _report(f), operator="ops@rgnr8.co",
        coa_templates=category_catalog(),
        live_tenants=frozenset({"acme"}),
    )
    # the COA-template picker appears in the onboard form
    assert 'name="coa_category"' in html
    assert "Contractor &amp; Trades" in html or "Contractor & Trades" in html
    # the system-of-record live badge appears for the cut-over tenant
    assert "LIVE" in html
    # still self-contained
    assert "http://" not in html and "https://" not in html
