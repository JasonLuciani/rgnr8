"""The operator console: fleet-in-shell + onboarding panel, self-contained."""

from datetime import datetime
from zoneinfo import ZoneInfo

from rgnr8_ops import (
    ConnectorHealth,
    Fleet,
    TenantOpsStatus,
    build_ops_report,
    render_operator_console,
)
from factory import at_risk_tenant, steady_tenant

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


def test_console_empty_fleet_prompts_onboarding() -> None:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    html = render_operator_console(_report(f))
    assert "No clients onboarded yet" in html
    assert "operator@rgnr8.co" in html  # default operator identity
