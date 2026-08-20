"""The durable webhook outbox: nothing is dropped, retries back off, dead letters
are kept and replayable, and delivery survives a process restart."""

from __future__ import annotations

import sqlite3

from rgnr8_web import (
    DEAD,
    DELIVERED,
    PENDING,
    DurableWebhookDispatcher,
    InMemoryWebhookEndpointStore,
    InMemoryWebhookOutbox,
    PlatformEvent,
    SqlWebhookOutbox,
    WebhookEndpoint,
    backoff_seconds,
    sign_webhook,
)
from rgnr8_web.webhooks_out import HttpResponse


class _FakeHttp:
    """Scriptable HTTP: a queue of responses/exceptions per URL, and a capture log."""

    def __init__(self) -> None:
        self.script: list[object] = []
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def post_json(self, url: str, body: str, headers: dict[str, str]) -> HttpResponse:
        self.calls.append((url, body, headers))
        outcome = self.script.pop(0) if self.script else HttpResponse(200, "")
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, HttpResponse)
        return outcome


def _clock(t: list[int]):
    return lambda: t[0]


def _resolve(_host: str) -> list[str]:
    return ["93.184.216.34"]  # a public IP; offline tests skip real DNS


def _ep(*, url: str = "https://hooks.example.com/x", **kw: object) -> WebhookEndpoint:
    return WebhookEndpoint(id="ep1", tenant_id="acme", url=url, secret="shh", **kw)  # type: ignore[arg-type]


def _event(eid: str = "e1") -> PlatformEvent:
    return PlatformEvent(eid, "close.sealed", "acme", 100, {"period": "2026-08"})


def test_enqueue_is_idempotent_and_fans_out_per_endpoint() -> None:
    eps = InMemoryWebhookEndpointStore()
    eps.save(_ep())
    box = InMemoryWebhookOutbox()
    t = [1000]
    disp = DurableWebhookDispatcher(box, eps, _FakeHttp(), resolver=_resolve, clock=_clock(t))
    assert disp.enqueue(_event()) == 1
    # re-enqueueing the same event queues nothing new (durable dedupe)
    assert disp.enqueue(_event()) == 0
    assert len(box.for_tenant("acme")) == 1


def test_a_successful_delivery_signs_and_marks_delivered() -> None:
    eps = InMemoryWebhookEndpointStore()
    eps.save(_ep())
    box = InMemoryWebhookOutbox()
    http = _FakeHttp()
    t = [1000]
    disp = DurableWebhookDispatcher(box, eps, http, clock=_clock(t), resolver=_resolve)
    disp.enqueue(_event())
    summary = disp.deliver_due()
    assert (summary.delivered, summary.retried, summary.dead) == (1, 0, 0)
    row = box.for_tenant("acme")[0]
    assert row.status == DELIVERED and row.attempts == 1
    # the body was signed with the endpoint secret
    _url, body, headers = http.calls[0]
    assert headers["X-RGNR8-Signature"] == sign_webhook(body, "shh")
    assert headers["X-RGNR8-Event-Id"] == "e1"


def test_failure_backs_off_then_eventually_dead_letters_never_dropping() -> None:
    eps = InMemoryWebhookEndpointStore()
    eps.save(_ep())
    box = InMemoryWebhookOutbox()
    http = _FakeHttp()
    t = [1000]
    disp = DurableWebhookDispatcher(box, eps, http, clock=_clock(t), resolver=_resolve, max_attempts=3)
    disp.enqueue(_event())

    # attempt 1: server 500 → reschedule with backoff, still pending, not dropped
    http.script = [HttpResponse(500, "")]
    s1 = disp.deliver_due()
    assert (s1.delivered, s1.retried, s1.dead) == (0, 1, 0)
    row = box.for_tenant("acme")[0]
    assert row.status == PENDING and row.attempts == 1
    assert row.next_attempt_at == 1000 + backoff_seconds(1)

    # nothing is due yet (backoff in the future): deliver_due does nothing
    assert disp.deliver_due().considered == 0

    # advance time; attempt 2 also fails
    t[0] = row.next_attempt_at
    http.script = [HttpResponse(503, "")]
    disp.deliver_due()
    row = box.for_tenant("acme")[0]
    assert row.status == PENDING and row.attempts == 2

    # advance; attempt 3 fails → exhausted → DEAD, kept for inspection/replay
    t[0] = row.next_attempt_at
    http.script = [ConnectionError("refused")]
    s3 = disp.deliver_due()
    assert s3.dead == 1
    dead = box.for_tenant("acme", status=DEAD)
    assert len(dead) == 1 and "refused" in dead[0].last_error


def test_a_dead_letter_can_be_replayed() -> None:
    eps = InMemoryWebhookEndpointStore()
    eps.save(_ep())
    box = InMemoryWebhookOutbox()
    http = _FakeHttp()
    t = [1000]
    disp = DurableWebhookDispatcher(box, eps, http, clock=_clock(t), resolver=_resolve, max_attempts=1)
    disp.enqueue(_event())
    http.script = [HttpResponse(500, "")]
    disp.deliver_due()
    dead = box.for_tenant("acme", status=DEAD)[0]

    # the endpoint is fixed; replay re-arms the letter and it delivers
    assert disp.replay(dead.id) is True
    http.script = [HttpResponse(200, "")]
    s = disp.deliver_due()
    assert s.delivered == 1
    assert box.get(dead.id).status == DELIVERED  # type: ignore[union-attr]


def test_an_ssrf_or_http_target_is_refused_without_a_request() -> None:
    eps = InMemoryWebhookEndpointStore()
    eps.save(_ep(url="http://169.254.169.254/latest/meta-data"))  # link-local, non-https
    box = InMemoryWebhookOutbox()
    http = _FakeHttp()
    disp = DurableWebhookDispatcher(box, eps, http, clock=_clock([1000]), resolver=_resolve, max_attempts=2)
    disp.enqueue(_event())
    disp.deliver_due()
    assert http.calls == [], "no request is made to a blocked target"
    row = box.for_tenant("acme")[0]
    assert row.status == PENDING and row.attempts == 1  # failed, will retry/dead, never sent


def test_delivery_survives_a_restart_via_the_sql_outbox() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        eps = InMemoryWebhookEndpointStore()
        eps.save(_ep())
        # instance #1 enqueues, then "crashes" (we drop the dispatcher)
        box1 = SqlWebhookOutbox(conn)
        box1.create_schema()
        DurableWebhookDispatcher(box1, eps, _FakeHttp(), resolver=_resolve, clock=_clock([1000])).enqueue(_event())

        # instance #2 boots against the same database and finds the pending work
        box2 = SqlWebhookOutbox(conn)
        http = _FakeHttp()
        disp2 = DurableWebhookDispatcher(box2, eps, http, clock=_clock([2000]), resolver=_resolve)
        s = disp2.deliver_due()
        assert s.delivered == 1, "the queued delivery outlived the restart"
        assert box2.get("ep1:e1").status == DELIVERED  # type: ignore[union-attr]
    finally:
        conn.close()
