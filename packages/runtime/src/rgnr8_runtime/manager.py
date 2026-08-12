"""Subscription management — the operator/owner surface over the store.

`SubscriptionStore` is the persistence seam; this is the small vocabulary an
operator (or a settings screen) actually needs: add a recipient, list who's
subscribed, change the send schedule, pause/resume delivery, or remove someone.
Pausing keeps the row (and its delivery cursor) but stops it firing — the
scheduler's `run_due` skips inactive subscriptions. Every mutation persists, so
it survives a restart just like the rest of the fleet.
"""

from __future__ import annotations

from rgnr8_briefing import Schedule, Subscription

from .subscriptions import SubscriptionStore


class SubscriptionManager:
    def __init__(self, store: SubscriptionStore) -> None:
        self._store = store

    def _find(self, tenant_id: str, recipient: str) -> Subscription | None:
        for s in self._store.list():
            if s.tenant_id == tenant_id and s.recipient == recipient:
                return s
        return None

    def list(self, tenant_id: str | None = None) -> list[Subscription]:
        subs = self._store.list()
        if tenant_id is not None:
            subs = [s for s in subs if s.tenant_id == tenant_id]
        subs.sort(key=lambda s: (s.tenant_id, s.recipient))
        return subs

    def add(self, tenant_id: str, recipient: str, schedule: Schedule) -> Subscription:
        """Add (or re-activate) a recipient. Re-adding an existing one keeps its
        delivery cursor and just refreshes the schedule + marks it active."""
        existing = self._find(tenant_id, recipient)
        sub = Subscription(
            tenant_id=tenant_id,
            recipient=recipient,
            schedule=schedule,
            last_sent=existing.last_sent if existing is not None else None,
            active=True,
        )
        self._store.save(sub)
        return sub

    def set_schedule(self, tenant_id: str, recipient: str, schedule: Schedule) -> None:
        sub = self._find(tenant_id, recipient)
        if sub is None:
            raise KeyError((tenant_id, recipient))
        sub.schedule = schedule
        self._store.save(sub)

    def pause(self, tenant_id: str, recipient: str) -> None:
        sub = self._find(tenant_id, recipient)
        if sub is None:
            raise KeyError((tenant_id, recipient))
        sub.active = False
        self._store.save(sub)

    def resume(self, tenant_id: str, recipient: str) -> None:
        sub = self._find(tenant_id, recipient)
        if sub is None:
            raise KeyError((tenant_id, recipient))
        sub.active = True
        self._store.save(sub)

    def remove(self, tenant_id: str, recipient: str) -> None:
        self._store.remove(tenant_id, recipient)
