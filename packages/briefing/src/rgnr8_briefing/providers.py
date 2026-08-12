"""Concrete delivery providers behind a transport seam.

`delivery.py` defines the `Deliverer` protocol and a `RecordingDeliverer` for
tests. This module implements *real* providers — email and push — but keeps the
actual wire call behind a small transport seam (`EmailTransport` /
`PushTransport`), exactly like the connectors package hides `fetch` behind
`HttpClient`. That means the routing, channel-selection, partial-failure, and
receipt logic is fully unit-testable with a scripted fake, and going live is
just supplying a transport that calls SendGrid / SES / APNs / FCM.

Per the blueprint: email is the default weekly channel; push is reserved for
urgent envelopes (critical cash risk or material worsening). A `ProviderDeliverer`
routes one envelope across whatever channels it carries and returns a single
receipt describing what actually went out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .delivery import Channel, DeliveryEnvelope, DeliveryReceipt


class TransportError(Exception):
    """Raised by a transport when a provider send fails (network/provider error)."""


@dataclass(frozen=True, slots=True)
class SendResult:
    """What a transport returns on success — the provider's message id + channel."""

    channel: Channel
    provider_message_id: str


@runtime_checkable
class EmailTransport(Protocol):
    def send_email(
        self, recipient: str, subject: str, text_body: str, html_body: str
    ) -> str:
        """Send one email; return the provider message id. Raise TransportError on failure."""
        ...


@runtime_checkable
class PushTransport(Protocol):
    def send_push(self, recipient: str, title: str, body: str) -> str:
        """Send one push; return the provider message id. Raise TransportError on failure."""
        ...


class ProviderDeliverer:
    """A `Deliverer` that routes an envelope across its channels via injected
    transports. Email is required; push is optional (an envelope may request PUSH
    but if no push transport is configured that channel is skipped, not failed).

    The returned receipt's `channels` are the ones that *succeeded*; if any
    requested channel fails the status is ``PARTIAL`` (some went out) or
    ``FAILED`` (none did), and the reasons are collected in ``detail``.
    """

    def __init__(
        self, email: EmailTransport, push: PushTransport | None = None
    ) -> None:
        self._email = email
        self._push = push

    def send(self, envelope: DeliveryEnvelope, at: str) -> DeliveryReceipt:
        succeeded: list[Channel] = []
        failures: list[str] = []
        message_ids: list[str] = []

        for channel in envelope.channels:
            if channel is Channel.EMAIL:
                try:
                    mid = self._email.send_email(
                        envelope.recipient,
                        envelope.subject,
                        envelope.text_body,
                        envelope.html_body,
                    )
                    succeeded.append(channel)
                    message_ids.append(f"EMAIL:{mid}")
                except TransportError as exc:
                    failures.append(f"EMAIL: {exc}")
            elif channel is Channel.PUSH:
                if self._push is None:
                    failures.append("PUSH: no push transport configured")
                    continue
                try:
                    mid = self._push.send_push(
                        envelope.recipient, envelope.subject, _push_body(envelope)
                    )
                    succeeded.append(channel)
                    message_ids.append(f"PUSH:{mid}")
                except TransportError as exc:
                    failures.append(f"PUSH: {exc}")

        if succeeded and not failures:
            status = "SENT"
        elif succeeded:
            status = "PARTIAL"
        else:
            status = "FAILED"

        detail = "; ".join(message_ids + failures)
        return DeliveryReceipt(
            recipient=envelope.recipient,
            subject=envelope.subject,
            channels=tuple(succeeded),
            priority=envelope.priority,
            delivered_at=at,
            status=status if not detail else f"{status} ({detail})",
        )


def _push_body(envelope: DeliveryEnvelope) -> str:
    """A short push body: the recommended action deep-link label if present,
    else the first line of the text body."""
    for link in envelope.deep_links:
        if "action" in link.url:
            return link.label
    first_line = envelope.text_body.strip().splitlines()
    return first_line[0] if first_line else envelope.subject


# --- fakes for tests -------------------------------------------------------


class FakeEmailTransport:
    """Records emails; can be told to fail to exercise the failure path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, str]] = []
        self._counter = 0

    def send_email(
        self, recipient: str, subject: str, text_body: str, html_body: str
    ) -> str:
        if self.fail:
            raise TransportError("simulated email failure")
        self._counter += 1
        self.sent.append(
            {"recipient": recipient, "subject": subject, "text": text_body, "html": html_body}
        )
        return f"email-{self._counter}"


class FakePushTransport:
    """Records pushes; can be told to fail to exercise the failure path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, str]] = []
        self._counter = 0

    def send_push(self, recipient: str, title: str, body: str) -> str:
        if self.fail:
            raise TransportError("simulated push failure")
        self._counter += 1
        self.sent.append({"recipient": recipient, "title": title, "body": body})
        return f"push-{self._counter}"
