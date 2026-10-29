"""QuickBooks failures, said plainly.

A failed go-live or Refresh used to show the owner Intuit's raw response:
`query returned 401: {"Fault":{"Error":[{"Message":"message=AuthenticationFailed;
errorCode=003200;statusCode=401",...`. What the owner needs is the cause and what
to do about it; the raw text goes to the server log for whoever debugs it.
"""

from __future__ import annotations

import json
import logging

from rgnr8_qbo import QboApiError, QboOAuthError

log = logging.getLogger(__name__)


def _intuit_message(body: str) -> str:
    """The first human-readable message in an Intuit fault body, or ""."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return ""
    fault = data.get("Fault") if isinstance(data, dict) else None
    errors = fault.get("Error") if isinstance(fault, dict) else None
    first = errors[0] if isinstance(errors, list) and errors else None
    if not isinstance(first, dict):
        return ""
    message = str(first.get("Message") or "").strip()
    # Auth faults pack machine fields into the message ("message=...;errorCode=...").
    return "" if "=" in message or len(message) > 160 else message


def plain_qbo_error(exc: BaseException) -> str:
    """What went wrong talking to QuickBooks, in a sentence an owner can act on."""
    log.warning("QuickBooks request failed: %s", exc)
    if isinstance(exc, QboApiError) and exc.status is not None:
        status = exc.status
        if status == 401:
            return ("QuickBooks no longer accepts this connection (it has probably expired). "
                    "Reconnect QuickBooks, then try again")
        if status == 403:
            return ("QuickBooks refused access to this company. The person who connected it "
                    "may need to be a QuickBooks admin; reconnect as one, then try again")
        if status == 429:
            return "QuickBooks is limiting requests right now. Wait a minute, then try again"
        if status >= 500:
            return f"QuickBooks had a problem on its side (error {status}). Try again in a few minutes"
        said = _intuit_message(exc.body)
        return (f"QuickBooks turned the request down: {said}" if said
                else f"QuickBooks turned the request down (error {status})")
    if isinstance(exc, QboOAuthError):
        return "couldn't reach QuickBooks just now. Check the connection, then try again"
    return "something went wrong reading from QuickBooks. Try again in a moment"
