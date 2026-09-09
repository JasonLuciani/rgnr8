"""Account email — the invitation, verify-your-address and password-reset messages.

`WebApp` exposes a deliberately tiny seam, `emailer(email, purpose, token)`, so the
web package never learns what a mail provider is. This module is the other side of
it: it turns a (purpose, token) pair into a real message with a real clickable
link, and hands that to the same `EmailTransport` the briefing stack already uses.

Why it matters: a token that is never delivered is not a feature. Until this is
wired, an invitation has to be copied out of the team page by hand and a password
reset is unreachable for anyone who isn't reading the API response — which was the
audit's "notifications in no-op mode" finding, and the reason self-signup was
auto-verifying in production.

Two deliberate choices:

* **The link is absolute and built here.** The token alone is useless to a
  recipient; `base_url` is what makes it clickable, and it lives in config rather
  than being guessed from a request Host header (which an attacker controls).
* **A provider failure never breaks the request.** Sending is best-effort: if
  SendGrid is down, the invitation still exists and the owner can still copy the
  link from the team page. We log that a send failed — never the token itself,
  which would put a live credential in the log aggregator.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from html import escape
from urllib.parse import quote

from rgnr8_briefing import EmailTransport, TransportError

# purpose -> (path that consumes the token, subject, what the mail says)
_MESSAGES: dict[str, tuple[str, str, str, str]] = {
    "invitation": (
        "/signup",
        "You've been invited to RGNR8",
        "You've been invited to a business on RGNR8. Choose a password to finish "
        "setting up your account.",
        "Accept invitation",
    ),
    "verify_email": (
        "/verify",
        "Confirm your RGNR8 email address",
        "Confirm this address to finish setting up your RGNR8 account.",
        "Confirm my email",
    ),
    "password_reset": (
        "/password/reset",
        "Reset your RGNR8 password",
        "Someone asked to reset the password for this RGNR8 account. If that "
        "wasn't you, you can ignore this message — nothing has changed yet.",
        "Set a new password",
    ),
}

# How long each link lasts, mirrored from AuthService/InvitationService so the
# mail can say it out loud. Kept as text: it is copy, not a control.
_EXPIRY: dict[str, str] = {
    "invitation": "This link works once and expires in 7 days.",
    "verify_email": "This link works once and expires in 24 hours.",
    "password_reset": "This link works once and expires in 1 hour.",
}


def account_link(base_url: str, purpose: str, token: str) -> str:
    """The absolute URL that redeems `token`. Raises KeyError for an unknown purpose."""
    path, _subject, _body, _cta = _MESSAGES[purpose]
    return f"{base_url.rstrip('/')}{path}?token={quote(token, safe='')}"


def _text_body(intro: str, link: str, expiry: str) -> str:
    return f"{intro}\n\n{link}\n\n{expiry}\n\nIf you weren't expecting this, ignore it.\n"


def _html_body(intro: str, link: str, expiry: str, cta: str) -> str:
    # Inline styles only, no remote assets: mail clients strip <style> blocks and
    # block images by default, so anything else would arrive as unstyled soup.
    safe_link = escape(link, quote=True)
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:520px;margin:0 auto;padding:28px 24px;color:#1B1F1D">'
        '<div style="font:800 20px/1 sans-serif;letter-spacing:.18em;text-transform:uppercase;'
        'color:#123A2C;margin-bottom:24px">RGNR<span style="color:#7FA88B">8</span></div>'
        f'<p style="font-size:15px;line-height:1.6;margin:0 0 24px">{escape(intro)}</p>'
        f'<p style="margin:0 0 24px"><a href="{safe_link}" '
        'style="display:inline-block;background:#123A2C;color:#fff;text-decoration:none;'
        f'padding:12px 22px;border-radius:8px;font-weight:600">{escape(cta)}</a></p>'
        '<p style="font-size:13px;color:#6B7772;line-height:1.6;margin:0 0 8px">'
        f'{escape(expiry)}</p>'
        '<p style="font-size:13px;color:#6B7772;line-height:1.6;margin:0 0 24px">'
        "If the button doesn't work, paste this into your browser:<br>"
        f'<span style="word-break:break-all">{escape(link)}</span></p>'
        '<p style="font-size:12px;color:#9AA5A0;margin:0">If you weren\'t expecting '
        "this, you can ignore it.</p></div>"
    )


def account_emailer(
    transport: EmailTransport,
    base_url: str,
    *,
    on_error: Callable[[str], None] | None = None,
) -> Callable[[str, str, str], None]:
    """Build the `emailer` callable `WebApp` expects: `(email, purpose, token)`.

    Unknown purposes are ignored rather than raised — the seam is called from
    inside request handling, and a future purpose that nobody has written copy for
    yet should not turn a successful signup into a 500.
    """
    report = on_error if on_error is not None else _default_error_report

    def send(email: str, purpose: str, token: str) -> None:
        entry = _MESSAGES.get(purpose)
        if entry is None:
            report(f"account_mail: no template for purpose {purpose!r}; nothing sent")
            return
        _path, subject, intro, cta = entry
        expiry = _EXPIRY.get(purpose, "")
        link = account_link(base_url, purpose, token)
        try:
            transport.send_email(email, subject, _text_body(intro, link, expiry),
                                 _html_body(intro, link, expiry, cta))
        except TransportError as exc:
            # Best-effort by design (see module docstring). Never log `token` or
            # `link` — the link IS the credential.
            report(f"account_mail: {purpose} to {email} failed: {exc}")

    return send


def _default_error_report(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
