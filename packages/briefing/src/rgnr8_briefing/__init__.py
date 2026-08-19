"""RGNR8 weekly owner briefing — evidence-backed summary of the cash forecast.

Turns a ForecastResult into an owner-facing briefing where every number is
backed by evidence and reconciled by a validator, plus a constrained
ask-your-CFO answerer.
"""

from __future__ import annotations

from .answer import Answer, Question, answer, ask, route, suggested_questions
from .build import build_briefing
from .delivery import (
    Channel,
    DeepLink,
    Deliverer,
    DeliveryEnvelope,
    DeliveryReceipt,
    Priority,
    RecordingDeliverer,
    Schedule,
    build_envelope,
    choose_priority,
    default_schedule,
    subject_for,
)
from .http_providers import (
    FakeHttpClient,
    HttpClient,
    HttpEmailTransport,
    HttpPushTransport,
    HttpResponse,
    UrllibHttpClient,
)
from .models import (
    Driver,
    Evidence,
    Fact,
    StatusLevel,
    Violation,
    WeekGlance,
    WeeklyBriefing,
)
from .providers import (
    EmailTransport,
    FakeEmailTransport,
    FakePushTransport,
    ProviderDeliverer,
    PushTransport,
    SendResult,
    TransportError,
)
from .render import render_html, render_text
from .scheduler import (
    DeliveryOutcome,
    Subscription,
    is_due,
    most_recent_fire,
    next_fire,
    run_due,
)
from .today import render_today_html
from .validate import (
    check_driver,
    check_fact,
    resolve_field,
    validate_briefing,
    validate_facts,
)
from .variance import (
    VarianceReport,
    WeekActual,
    WeekVariance,
    compute_variance,
    render_variance_text,
)

__version__ = "0.1.0"

__all__ = [
    "build_briefing",
    "WeeklyBriefing",
    "Fact",
    "Driver",
    "Evidence",
    "WeekGlance",
    "StatusLevel",
    "Violation",
    "validate_briefing",
    "validate_facts",
    "check_fact",
    "check_driver",
    "resolve_field",
    "render_text",
    "render_html",
    "render_today_html",
    "Answer",
    "Question",
    "answer",
    "ask",
    "route",
    "suggested_questions",
    "WeekActual",
    "WeekVariance",
    "VarianceReport",
    "compute_variance",
    "render_variance_text",
    "Channel",
    "Priority",
    "Schedule",
    "DeepLink",
    "DeliveryEnvelope",
    "DeliveryReceipt",
    "Deliverer",
    "RecordingDeliverer",
    "build_envelope",
    "choose_priority",
    "subject_for",
    "default_schedule",
    "EmailTransport",
    "PushTransport",
    "ProviderDeliverer",
    "FakeEmailTransport",
    "FakePushTransport",
    "SendResult",
    "TransportError",
    "HttpClient",
    "HttpResponse",
    "HttpEmailTransport",
    "HttpPushTransport",
    "UrllibHttpClient",
    "FakeHttpClient",
    "Subscription",
    "DeliveryOutcome",
    "is_due",
    "most_recent_fire",
    "next_fire",
    "run_due",
]
