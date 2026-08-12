#!/usr/bin/env python3
"""Onboarding demo — the Python operator half of the two-stage onboarding proof.

Runs `onboard.ts` (which emits the Stage-1 overlay and Stage-2 migration
`forecast-inputs/1` DTOs), then onboards **both** clients into one `Fleet` the
same way — `Fleet.onboard_from_dto` doesn't care whether a tenant arrived via
the overlay or the full migration, because both stages emit the same contract.
Then it runs each client's forecast, mints a session token, exercises the web
route, and renders the operator fleet dashboard so you can see both onboarded
clients side by side. Writes:
  out/onboard_fleet.html   the operator dashboard over both onboarded clients
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

for pkg in ("forecast", "briefing", "web", "runtime", "ops"):
    sys.path.insert(0, str(HERE.parent / pkg / "src"))

from rgnr8_forecast import Money  # noqa: E402
from rgnr8_web import Request, verify_jwt  # noqa: E402
from rgnr8_ops import (  # noqa: E402
    CloseProgress,
    ConnectorHealth,
    Fleet,
    TenantOpsStatus,
    build_ops_report,
    render_ops_html,
)

SECRET = "onboard-signing-key"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=ZoneInfo("America/Denver"))
NOW_EPOCH = int(NOW.timestamp())


def main() -> int:
    print("→ running onboard.ts to emit both stages' forecast-inputs/1 DTOs")
    subprocess.run(
        ["node", "--import", "tsx", "onboard.ts"], cwd=HERE, check=True,
    )

    overlay_dto = json.loads((OUT / "overlay_inputs.json").read_text())
    migration_dto = json.loads((OUT / "migration_inputs.json").read_text())

    fleet = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    # Stage 1 — overlay tenant. QBO stays the system of record.
    fleet.onboard_from_dto(
        "northwind", "Northwind LLC (overlay)", "owner@northwind.com",
        overlay_dto, Money.from_decimal("20000.00"),
    )
    # Stage 2 — migrated tenant. RGNR8 is now the ledger.
    fleet.onboard_from_dto(
        "bright", "Bright Agency (migrated)", "owner@bright.com",
        migration_dto, Money.from_decimal("15000.00"),
    )
    print(f"✓ onboarded {len(fleet.tenants)} client(s) from forecast-inputs/1 DTOs")

    # Operational status the operator would serialize from the TS connector-health
    # + close-calendar reports. Overlay client: QBO feeds are fresh (books current),
    # no RGNR8 close yet. Migrated client: books current + close in progress.
    fleet.set_status(
        "northwind",
        TenantOpsStatus(
            connectors=ConnectorHealth(total=2, needs_attention=0, stale=0, last_sync="2026-09-02T06:00:00Z"),
        ),
    )
    fleet.set_status(
        "bright",
        TenantOpsStatus(
            connectors=ConnectorHealth(total=3, needs_attention=0, stale=0, last_sync="2026-09-02T06:00:00Z"),
            close=CloseProgress(period="2026-08", total=7, done=5, overdue=1, blocked=0,
                                next_task="Reconcile bank", next_due="2026-09-03"),
        ),
    )

    # both clients answer the owner web route under their own token
    app = fleet.web_app()
    for tid in fleet.tenants:
        token = fleet.mint_token(tid)
        assert verify_jwt(token, SECRET, now=NOW_EPOCH)["tenant"] == tid
        r = app.handle(Request("GET", f"/api/{tid}/today", {"authorization": f"Bearer {token}"}))
        assert r.status == 200, f"{tid} today route → {r.status}"
        print(f"  ✓ {tid}: session token authorizes /api/{tid}/today")

    # operator fleet dashboard over both onboarded clients
    report = build_ops_report(fleet, NOW)
    (OUT / "onboard_fleet.html").write_text(render_ops_html(report))
    print(f"  wrote out/onboard_fleet.html ({report.total} clients, {report.at_risk} at risk)")
    for row in report.rows:
        print(f"    {row.tenant_id:10s} {row.status:8s} cash {row.cash_today}  trough {row.trough} @ {row.trough_date}")

    print("Onboarding proof complete — overlay + migration both provisioned identically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
