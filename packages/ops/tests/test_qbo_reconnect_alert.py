"""A QuickBooks connection that needs reconnecting raises one alert through the
scheduler's alerts job: delivered once, quiet while it persists, re-armed when the
owner reconnects, and delivered again if it lapses a second time."""

from dataclasses import replace
from datetime import datetime

from factory import steady_tenant
from rgnr8_alerts import InMemoryAlertSink
from rgnr8_ops import Fleet, build_alerts_job
from rgnr8_qbo import QboConnection, QboStatus

SECRET = "alerts-secret"
NOW_EPOCH = 1_760_000_000.0
T0 = datetime(2026, 9, 2, 8, 0)


def _conn(status: QboStatus) -> QboConnection:
    return QboConnection(
        tenant_id="acme", realm_id="R42", access_token="", refresh_token="",
        access_expires_at=T0, refresh_expires_at=T0, status=status,
    )


def test_needs_reconnect_alerts_once_and_rearms_after_reconnect() -> None:
    fleet = Fleet(jwt_secret=SECRET, clock=lambda: int(NOW_EPOCH))
    fleet.onboard(steady_tenant())
    sink = InMemoryAlertSink()
    current = [_conn(QboStatus.NEEDS_RECONNECT)]
    job = build_alerts_job(fleet, sink, clock=lambda: NOW_EPOCH, connections=lambda: current)

    def qbo_alerts() -> list[str]:
        return [a.key for a in sink.sent if a.key.startswith("qbo-reconnect:")]

    job.run(T0)
    assert qbo_alerts() == ["qbo-reconnect:acme"]
    job.run(T0)                                       # still needs reconnecting: quiet
    assert qbo_alerts() == ["qbo-reconnect:acme"]

    current[0] = _conn(QboStatus.CONNECTED)           # the owner reconnects: re-armed
    job.run(T0)
    assert qbo_alerts() == ["qbo-reconnect:acme"]

    current[0] = replace(_conn(QboStatus.NEEDS_RECONNECT))  # lapses again: fires again
    job.run(T0)
    assert qbo_alerts() == ["qbo-reconnect:acme", "qbo-reconnect:acme"]
