"""Per-request CSP nonce so inline scripts don't need `script-src 'unsafe-inline'`.

`'unsafe-inline'` lets ANY injected inline script run, which defeats much of a
Content-Security-Policy. Instead every request gets a fresh random nonce: the
policy allows `'nonce-<value>'`, and each first-party inline `<script>` we emit
carries that nonce. An attacker who injects markup can't guess the nonce, so
their script is blocked while ours runs.

Standalone (no internal imports) so both the middleware that builds the CSP header
and the screen renderers that open `<script>` tags can import it without a cycle.
The nonce lives in a contextvar set once per request in `WebApp.handle`.
"""

from __future__ import annotations

import contextvars
import secrets

_NONCE: contextvars.ContextVar[str] = contextvars.ContextVar("rgnr8_csp_nonce", default="")


def new_nonce() -> str:
    """Generate and install a fresh nonce for this request; returns it."""
    nonce = secrets.token_urlsafe(16)
    _NONCE.set(nonce)
    return nonce


def current_nonce() -> str:
    """The nonce for this request, or '' if none was set (e.g. a unit-level call)."""
    return _NONCE.get()


def script_open() -> str:
    """Open an inline script tag carrying this request's nonce (plain `<script>`
    when no nonce is set, e.g. outside a request)."""
    nonce = _NONCE.get()
    return f'<script nonce="{nonce}">' if nonce else "<script>"


def csp_value() -> str:
    """The Content-Security-Policy for this request. Scripts: self + this request's
    nonce (no `'unsafe-inline'`). Styles still allow inline (lower risk; the app
    emits many inline `style=` attributes)."""
    nonce = _NONCE.get()
    script_src = f"script-src 'self' 'nonce-{nonce}'" if nonce else "script-src 'self'"
    return f"default-src 'self'; style-src 'self' 'unsafe-inline'; {script_src}"


__all__ = ["new_nonce", "current_nonce", "script_open", "csp_value"]
