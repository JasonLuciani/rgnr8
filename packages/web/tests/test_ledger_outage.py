"""A ledger outage degrades the product; it does not take it down.

Before this, a ledger service that stopped answering did not produce a 500 — the
exception propagated out of `WebApp.handle` altogether, so every screen that
touches the books died, including the cash page the product exists to show.

The rule these tests hold is narrower than "don't crash", and it is the reason
the fix is not a blanket try/except at the top:

    the owner may be shown figures computed without their books,
    but never without being told that is what they are looking at.

Falling back silently to assumption-only numbers would be worse than an error
page. An error page is obviously broken; a confident wrong cash figure is not.
"""

from __future__ import annotations

import re

from factory import app_with_two_tenants

from rgnr8_web import LedgerClient, Request
from rgnr8_web.ledger_client import LedgerResponse

TOKEN = "super-secret-service-token"


class _Dead:
    """A ledger service that is not answering at all."""

    def request(self, method, path, body, headers):
        raise ConnectionError("ledger service unreachable")


class _Slow:
    def request(self, method, path, body, headers):
        raise TimeoutError("read timed out")


class _Leaky:
    """A transport whose exception text carries the URL — and so the token."""

    def request(self, method, path, body, headers):
        raise OSError(f"failed to reach https://ledger.internal{path} "
                      f"with {headers.get('authorization')}")


class _Garbage:
    def request(self, method, path, body, headers):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def _app(transport) -> object:
    app = app_with_two_tenants()
    app.set_ledger(LedgerClient(transport, token=TOKEN))
    return app


def _get(app, path: str):
    return app.handle(Request("GET", path, {"authorization": "Bearer tok-acme"}, ""))


# --- the client's contract --------------------------------------------------


def test_the_client_never_raises_whatever_the_transport_does() -> None:
    # ~150 call sites read `res = self._ledger.x(...)` then `if not res.ok`.
    # Every one of them is correct only if this holds.
    for transport in (_Dead(), _Slow(), _Leaky(), _Garbage()):
        res = LedgerClient(transport, token=TOKEN).accounts("acme")
        assert isinstance(res, LedgerResponse), transport
        assert not res.ok, transport
        assert res.status == 503, transport


def test_the_failure_message_does_not_carry_the_token_or_the_url() -> None:
    # A transport error's text routinely contains the request URL, and the URL
    # carries the derived bearer token. Only the exception's type goes in.
    res = LedgerClient(_Leaky(), token=TOKEN).accounts("acme")
    assert TOKEN not in res.error()
    assert "ledger.internal" not in res.error()
    assert "OSError" in res.error()


# --- the screens ------------------------------------------------------------

_OWNER_ROUTES = [
    "/t/acme",
    "/t/acme/briefing",
    "/t/acme/receivables",
    "/t/acme/scenarios",
    "/t/acme/reports",
    "/t/acme/close",
    "/t/acme/health",
    "/t/acme/team",
    "/api/acme/today",
]


def test_every_owner_screen_still_answers_during_an_outage() -> None:
    app = _app(_Dead())
    for route in _OWNER_ROUTES:
        res = _get(app, route)
        assert res.status == 200, f"{route} returned {res.status}"


def test_the_cash_page_still_shows_a_figure() -> None:
    body = _get(_app(_Dead()), "/t/acme").body
    assert "Cash today" in body
    assert re.search(r"\$\d{1,3}(,\d{3})*\.\d{2}", body)


def test_the_cash_page_says_the_figure_is_not_from_the_books() -> None:
    # The whole point. A number without this line is a lie by omission.
    body = _get(_app(_Dead()), "/t/acme").body
    assert "Working from your saved assumptions" in body
    assert "the books couldn&#x27;t be read just now" in body or \
           "the books couldn't be read just now" in body


def test_the_queue_leads_with_the_books_being_unreadable() -> None:
    # attention.py's rule 1: anything that makes the numbers untrustworthy
    # outranks anything merely due — including a floor breach this week.
    body = _get(_app(_Dead()), "/t/acme").body
    heads = [re.sub(r"<[^>]+>", "", m).strip()
             for m in re.findall(r'class="h">(.*?)</div>', body, re.S)]
    assert heads, "the queue should not be empty during an outage"
    assert heads[0] == "Your books could not be read in full"


def test_no_page_leaks_the_service_token() -> None:
    app = _app(_Leaky())
    for route in _OWNER_ROUTES:
        assert TOKEN not in _get(app, route).body, route


def test_the_forecast_falls_back_to_the_owners_assumptions_not_to_zero() -> None:
    """A forecast on stale assumptions is wrong; one on zeroed facts is worse.

    The factory's tenant opens at $20,000. With the books unreachable the page
    must still show that figure — the owner's own saved assumption — rather
    than the $0.00 that a blanked `LedgerFacts` would produce.
    """
    body = _get(_app(_Dead()), "/t/acme").body
    assert "$20,000.00" in body
    hero = body[body.index("Cash today"):body.index("Cash today") + 400]
    assert "$0.00" not in hero
