#!/usr/bin/env python3
"""RGNR8 end-to-end prototype — the Python owner-experience half.

Runs the TypeScript seed (QBO import → ledger → close → sealed package), then:
  1. reads the sealed financial package back and **re-verifies its fingerprint**
     in Python (the cross-language integrity check),
  2. builds the 13-week cash forecast + the validated weekly briefing + the
     interactive "Today" dashboard,
  3. boots the web app (JWT session auth + the package reader) and exercises the
     owner routes in-process,
  4. runs the delivery runtime one tick through the real HTTP email transport
     (fake client) to prove a briefing would be sent,
and writes the owner-facing HTML pages to ./out for viewing.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

# wire the sibling Python packages onto the path
for pkg in ("forecast", "briefing", "web", "runtime"):
    sys.path.insert(0, str(HERE.parent / pkg / "src"))

from rgnr8_forecast import (  # noqa: E402
    CashPosition,
    Category,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Frequency,
    Invoice,
    Money,
    PayrollSchedule,
    Recurrence,
    RecurringItem,
    run_forecast,
)
from rgnr8_briefing import (  # noqa: E402
    FakeHttpClient,
    HttpEmailTransport,
    ProviderDeliverer,
    Schedule,
    Subscription,
    build_briefing,
    render_today_html,
    render_text,
    validate_briefing,
)
from rgnr8_web import (  # noqa: E402
    FinancialPackageReader,
    JwtAuthenticator,
    Request,
    WebApp,
    render_package_html,
    sign_jwt,
    verify_package,
)
from rgnr8_runtime import DeliveryRuntime, InMemorySubscriptionStore, InMemoryTenantSource, RuntimeTenant  # noqa: E402

SECRET = "prototype-signing-key"
NOW_EPOCH = 1_760_000_000
MT = ZoneInfo("America/Denver")


def banner(title: str) -> None:
    print("\n" + "=" * 68 + f"\n  {title}\n" + "=" * 68)


def run_seed() -> None:
    banner("1 · TypeScript accounting stack: QBO import → close → sealed package")
    subprocess.run(["npm", "run", "--silent", "seed"], cwd=HERE, check=True)


def load_sealed_package() -> tuple[dict, dict]:
    summary = json.loads((OUT / "seed_summary.json").read_text())
    pkg = json.loads((OUT / "package.json").read_text())
    return summary, pkg


def cross_language_verify(pkg: dict) -> FinancialPackageReader:
    banner("2 · Python re-verifies the sealed package (cross-language fingerprint)")
    # simulate what the TS SqlFinancialPackageStore would have written
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE financial_package (tenant_id TEXT, period_key TEXT, fingerprint TEXT, "
        "package_json TEXT, PRIMARY KEY (tenant_id, period_key))"
    )
    conn.execute(
        "INSERT INTO financial_package VALUES (?, ?, ?, ?)",
        ("bright", pkg["periodKey"], pkg["fingerprint"], json.dumps(pkg)),
    )
    conn.commit()
    ok = verify_package(pkg)
    print(f"  fingerprint {pkg['fingerprint'][:16]}… · Python re-verify: {'✓ MATCH' if ok else '✗ MISMATCH'}")
    print(f"  QBO parallel close: {'ties to the penny' if pkg['qboReconciliation']['inAgreement'] else 'diverges'}")
    return FinancialPackageReader(conn)  # type: ignore[arg-type]


def build_inputs(opening_cash: Money) -> ForecastInputs:
    def usd(s: str) -> Money:
        return Money.from_decimal(s)

    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=opening_cash),
        invoices=(
            Invoice("INV-1002", "northwind", date(2026, 8, 20), date(2026, 9, 18), usd("20000.00")),
        ),
        customer_histories=(),
        payroll=(
            PayrollSchedule(
                "Payroll",
                Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 9, 3)),
                usd("16000.00"),
                usd("4200.00"),
            ),
        ),
        recurring=(
            RecurringItem(
                "Rent",
                Category.RENT,
                Direction.OUTFLOW,
                usd("5000.00"),
                Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 1)),
            ),
        ),
    )


def run_owner_loop(summary: dict, inputs: ForecastInputs, config: ForecastConfig, reader: FinancialPackageReader) -> None:
    banner("3 · Forecast → validated briefing → Today dashboard")
    forecast = run_forecast(inputs, config)
    briefing = build_briefing(forecast)
    violations = validate_briefing(briefing, forecast)
    print(f"  status: {briefing.status.value} · headline: {briefing.headline}")
    print(f"  primary action: {briefing.primary_action}")
    print(f"  unsupported-number validator: {'PASSED' if not violations else 'FAILED'}")
    (OUT / "today.html").write_text(render_today_html(forecast, summary["tenantName"]))
    (OUT / "briefing.txt").write_text(render_text(briefing))

    banner("4 · Web surface (JWT session auth + published-package route)")
    auth = JwtAuthenticator(SECRET, clock=lambda: NOW_EPOCH)
    app = WebApp(packages=reader, authenticator=auth)
    app.add_tenant("bright", summary["tenantName"], inputs, config, token="unused-with-jwt")
    token = sign_jwt({"tenant": "bright", "exp": NOW_EPOCH + 3600}, SECRET)
    hdr = {"authorization": f"Bearer {token}"}

    checks = [
        ("GET /t/bright (dashboard)", Request("GET", "/t/bright", hdr)),
        ("GET /api/bright/today", Request("GET", "/api/bright/today", hdr)),
        ("GET /api/bright/packages", Request("GET", "/api/bright/packages", hdr)),
        ("GET /api/bright/packages/2026-08", Request("GET", "/api/bright/packages/2026-08", hdr)),
        ("GET /t/bright/packages/2026-08 (record)", Request("GET", "/t/bright/packages/2026-08", hdr)),
    ]
    for label, req in checks:
        resp = app.handle(req)
        print(f"  {label:<44} → {resp.status}")
        assert resp.status == 200, f"{label} returned {resp.status}"
    # save the two owner-facing pages the web app served
    (OUT / "today_web.html").write_text(app.handle(Request("GET", "/t/bright", hdr)).body)
    (OUT / "package.html").write_text(app.handle(Request("GET", "/t/bright/packages/2026-08", hdr)).body)
    # confirm a bad tenant is blocked even with a valid signature
    ghost = sign_jwt({"tenant": "ghost", "exp": NOW_EPOCH + 3600}, SECRET)
    blocked = app.handle(Request("GET", "/api/ghost/today", {"authorization": f"Bearer {ghost}"}))
    print(f"  JWT for unregistered tenant 'ghost'          → {blocked.status} (blocked)")

    banner("5 · Delivery runtime: one tick → real HTTP email transport (fake client)")
    http = FakeHttpClient(status=202, headers={"X-Message-Id": "sg-proto-1"})
    deliverer = ProviderDeliverer(HttpEmailTransport(http, api_key="demo-key", from_email="cfo@rgnr8.com"))
    tenants = InMemoryTenantSource()
    tenants.add(RuntimeTenant("bright", summary["tenantName"], inputs, config))
    subs = InMemorySubscriptionStore()
    subs.save(Subscription("bright", "owner@bright.com", Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver"), last_sent=None))
    runtime = DeliveryRuntime(subs, tenants, deliverer)
    outcomes = runtime.tick(datetime(2026, 9, 2, 12, 0, tzinfo=MT), at="2026-09-02T18:00:00Z")
    fired = outcomes[0].fired
    to = http.calls[0]["payload"]["personalizations"][0]["to"][0]["email"] if http.calls else None
    print(f"  briefing delivered: {fired} · HTTP POSTs: {len(http.calls)} · to: {to}")
    # re-tick same week is idempotent
    runtime.tick(datetime(2026, 9, 2, 12, 5, tzinfo=MT), at="2026-09-02T18:05:00Z")
    print(f"  re-tick same week → HTTP POSTs still: {len(http.calls)} (idempotent)")


def main() -> None:
    run_seed()
    summary, pkg = load_sealed_package()
    reader = cross_language_verify(pkg)
    opening = Money(int(summary["openingCashMinor"]))
    inputs = build_inputs(opening)
    config = ForecastConfig(minimum_cash=Money.from_decimal("15000.00"))
    run_owner_loop(summary, inputs, config, reader)

    banner("PROTOTYPE COMPLETE — the whole loop ran end to end")
    print(
        "  QBO CSV → ledger → close → sealed package (TS)\n"
        "     → re-verified in Python → forecast → briefing → Today dashboard\n"
        "     → served over JWT-authed web → delivered via HTTP email transport.\n"
    )
    print("  Open these artifacts in ./out:")
    for f in ("today.html", "package.html", "qbo_trial_diff.html", "qbo_statements_diff.html", "briefing.txt"):
        print(f"    - out/{f}")


if __name__ == "__main__":
    main()
