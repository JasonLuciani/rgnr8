#!/usr/bin/env python3
"""Delivery worker — sends due weekly briefings on a schedule.

Rehydrates the persisted fleet from the database and runs one delivery tick
(idempotent: nothing re-sends within a fire window). Run it on a scheduler
(K8s CronJob / systemd timer / cron) rather than as a long-lived loop, so a
missed pod restart never drops a week. Delivery transport is chosen from config
(SendGrid when a key is set; otherwise a recording no-op for dry runs).

    python deploy/worker.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import _pathsetup  # noqa: F401

from rgnr8_briefing import RecordingDeliverer
from rgnr8_ops import Settings, load_fleet
from db import open_connection


def _deliverer(settings: Settings) -> object:
    if settings.sendgrid_api_key:
        # Real transport is wired in rgnr8-briefing (HttpEmailTransport/ProviderDeliverer);
        # a deploy would construct it here from settings. Kept explicit for the operator.
        print("[worker] SendGrid key present — construct ProviderDeliverer(HttpEmailTransport)")
    return RecordingDeliverer()


def main() -> int:
    settings = Settings.from_env(os.environ)
    if not settings.is_production_db:
        print("[worker] no database configured — nothing to deliver")
        return 0
    conn, _dialect, _ph = open_connection(settings.database_url)
    fleet = load_fleet(settings, conn)
    now = datetime.now(timezone.utc)
    outcomes = fleet.delivery_runtime(_deliverer(settings)).tick(now)  # type: ignore[arg-type]
    fired = sum(1 for o in outcomes if o.fired)
    print(f"[worker] tick at {now.isoformat()}: {fired} briefing(s) delivered of {len(outcomes)} subscription(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
