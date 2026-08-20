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

from rgnr8_briefing import (
    HttpEmailTransport,
    ProviderDeliverer,
    RecordingDeliverer,
    UrllibHttpClient,
)
from rgnr8_ops import Settings, load_fleet
from db import open_connection


def _deliverer(settings: Settings) -> object:
    """The real SendGrid transport when a key is set, else a recording no-op for
    dry runs. Previously this always returned the recorder — briefings never sent."""
    if settings.sendgrid_api_key:
        email = HttpEmailTransport(
            UrllibHttpClient(),
            api_key=settings.sendgrid_api_key,
            from_email=settings.delivery_from,
        )
        print(f"[worker] email transport: SendGrid, from {settings.delivery_from}")
        return ProviderDeliverer(email)
    print("[worker] no RGNR8_SENDGRID_API_KEY — using recording no-op (dry run)")
    return RecordingDeliverer()


def main() -> int:
    settings = Settings.from_env(os.environ)
    if not settings.is_production_db:
        print("[worker] no database configured — nothing to deliver")
        return 0
    conn, _dialect, ph = open_connection(settings.database_url)
    fleet = load_fleet(settings, conn)
    now = datetime.now(timezone.utc)
    outcomes = fleet.delivery_runtime(_deliverer(settings)).tick(now)  # type: ignore[arg-type]
    fired = sum(1 for o in outcomes if o.fired)
    print(f"[worker] tick at {now.isoformat()}: {fired} briefing(s) delivered of {len(outcomes)} subscription(s)")

    # Drive the durable outbound-webhook outbox: deliver everything due, backing
    # off failures and dead-lettering the exhausted. Idempotent per delivery.
    from rgnr8_web import (
        DurableWebhookDispatcher,
        SqlWebhookEndpointStore,
        SqlWebhookOutbox,
        UrllibHttpClient,
    )

    from rgnr8_qbo import cipher_from_env
    endpoints = SqlWebhookEndpointStore(conn, placeholder=ph, cipher=cipher_from_env(os.environ))
    outbox = SqlWebhookOutbox(conn, placeholder=ph)
    dispatcher = DurableWebhookDispatcher(outbox, endpoints, UrllibHttpClient())
    summary = dispatcher.deliver_due(limit=500)
    print(f"[worker] webhooks: considered={summary.considered} delivered={summary.delivered} "
          f"retried={summary.retried} dead={summary.dead}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
