"""Delivery: turn a briefing into a channel-ready envelope and (optionally) send it.

Per the blueprint: the weekly briefing is the retention loop; push is reserved
for critical cash risk or material worsening; every notification deep-links to a
single resolvable task. This module builds the envelope (subject, priority,
channels, bodies, deep links) and defines a minimal Deliverer seam — real email/
push providers implement the same protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from .models import StatusLevel, WeeklyBriefing
from .render import render_html, render_text
from .variance import VarianceReport, render_variance_text


class Channel(str, Enum):
    EMAIL = "EMAIL"
    PUSH = "PUSH"


class Priority(str, Enum):
    NORMAL = "NORMAL"
    URGENT = "URGENT"


@dataclass(frozen=True, slots=True)
class Schedule:
    """When the recurring briefing fires (data only — a scheduler consumes it)."""

    weekday: int = 0  # 0 = Monday
    hour: int = 8
    minute: int = 0
    timezone: str = "America/Denver"


def default_schedule() -> Schedule:
    return Schedule()


@dataclass(frozen=True, slots=True)
class DeepLink:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class DeliveryEnvelope:
    recipient: str
    subject: str
    priority: Priority
    channels: tuple[Channel, ...]
    text_body: str
    html_body: str
    deep_links: tuple[DeepLink, ...]
    schedule: Schedule = field(default_factory=default_schedule)


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    recipient: str
    subject: str
    channels: tuple[Channel, ...]
    priority: Priority
    delivered_at: str
    status: str = "SENT"


def subject_for(b: WeeklyBriefing) -> str:
    if b.status is StatusLevel.AT_RISK:
        wk = next((w.index for w in b.week_glance if w.status is StatusLevel.AT_RISK), None)
        where = f" in week {wk}" if wk is not None else ""
        return f"Action needed: cash dips below your floor{where}"
    if b.status is StatusLevel.WATCH:
        return "Heads up: your cash cushion is getting thin"
    return "Your cash looks steady this week"


def choose_priority(b: WeeklyBriefing, variance: VarianceReport | None) -> Priority:
    if b.status is StatusLevel.AT_RISK:
        return Priority.URGENT
    # Material worsening vs. the last published forecast also warrants a push.
    if variance is not None and not variance.within_tolerance and variance.mean_abs_pct_bps is not None:
        worse = any(w.delta.is_negative for w in variance.weeks)
        if worse:
            return Priority.URGENT
    return Priority.NORMAL


def build_envelope(
    briefing: WeeklyBriefing,
    recipient: str,
    tenant_id: str,
    base_url: str = "https://app.rgnr8.com",
    variance: VarianceReport | None = None,
    schedule: Schedule | None = None,
) -> DeliveryEnvelope:
    priority = choose_priority(briefing, variance)
    channels = (Channel.EMAIL, Channel.PUSH) if priority is Priority.URGENT else (Channel.EMAIL,)

    text = render_text(briefing)
    if variance is not None:
        text = text + "\n\n" + render_variance_text(variance)

    links = [
        DeepLink("View full briefing", f"{base_url}/t/{tenant_id}/briefing/{briefing.version_id}"),
    ]
    if briefing.primary_action:
        links.append(DeepLink("Take the recommended action", f"{base_url}/t/{tenant_id}/actions/next"))

    return DeliveryEnvelope(
        recipient=recipient,
        subject=subject_for(briefing),
        priority=priority,
        channels=channels,
        text_body=text,
        html_body=render_html(briefing),
        deep_links=tuple(links),
        schedule=schedule or default_schedule(),
    )


@runtime_checkable
class Deliverer(Protocol):
    def send(self, envelope: DeliveryEnvelope, at: str) -> DeliveryReceipt: ...


class RecordingDeliverer:
    """Test/dev deliverer: records envelopes instead of sending. Real providers
    (email, push) implement the same `send` signature."""

    def __init__(self) -> None:
        self.sent: list[DeliveryEnvelope] = []

    def send(self, envelope: DeliveryEnvelope, at: str) -> DeliveryReceipt:
        self.sent.append(envelope)
        return DeliveryReceipt(
            recipient=envelope.recipient,
            subject=envelope.subject,
            channels=envelope.channels,
            priority=envelope.priority,
            delivered_at=at,
        )
