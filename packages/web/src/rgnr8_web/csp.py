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


# Every interactive control in the app names its handler in a data attribute
# rather than an inline `onclick=`. The CSP above forbids inline *attribute*
# handlers (the `script-src-attr` directive falls back to `script-src`, which
# carries only a nonce), so every `onclick=`, `onchange=` and `onsubmit=` in the
# product was silently dead in a real browser: the ask-your-CFO chips, the
# scenario runner, seal & publish, the bank feed's categorize dropdown, and
# every control on the team page including "Send invite". Unit tests could not
# see it, because nothing in them enforces a CSP.
#
#   data-action="fn"   click   -> fn(...args)
#   data-change="fn"   change  -> fn(...args)
#   data-submit="fn"   submit  -> fn(...args); native submission prevented,
#                                 because these forms post by fetch
#   data-action="selectAll"    -> select the field's text (built in)
#   data-confirm="…"   submit  -> an ordinary POST form that must ask first.
#
# args are data-arg and data-arg2 when present; `data-self` puts the element
# first; with no arguments at all the handler gets the element. So
# `advance(this,'k')` becomes data-action="advance" data-self data-arg="k".
#
# `data-confirm` matters more than it looks. Three destructive forms — dispose
# of an asset, clear owner-held data, remove a webhook — guarded themselves with
# `onsubmit="return confirm(…)"`. The CSP blocked the guard and not the form,
# so each one submitted immediately, without ever asking.
#
# Handlers are looked up on `window`, so they are the page's own top-level
# functions, defined in its own nonced script. The name is authored in this
# repo, never taken from a request.
DISPATCH_JS = (
    "(function(){"
    "function call(el,name,ev){"
    "if(name==='selectAll'){if(el.select)el.select();return;}"
    "var fn=window[name];if(typeof fn!=='function')return;"
    "var d=el.dataset,a=[];if('arg' in d)a.push(d.arg);if('arg2' in d)a.push(d.arg2);"
    "if('self' in d||!a.length)a.unshift(el);"
    "return fn.apply(null,a);}"
    "document.addEventListener('click',function(ev){"
    "var el=ev.target.closest&&ev.target.closest('[data-action]');"
    "if(el){ev.preventDefault();call(el,el.dataset.action,ev);}});"
    "document.addEventListener('change',function(ev){"
    "var el=ev.target.closest&&ev.target.closest('[data-change]');"
    "if(el)call(el,el.dataset.change,ev);});"
    "document.addEventListener('submit',function(ev){"
    "var f=ev.target;"
    "if(f.dataset&&('confirm' in f.dataset)&&!window.confirm(f.dataset.confirm)){ev.preventDefault();return;}"
    "var el=f.closest&&f.closest('[data-submit]');"
    "if(el){ev.preventDefault();call(el,el.dataset.submit,ev);}});"
    "})();"
)


def dispatcher() -> str:
    """The delegated event dispatcher, as one nonced script tag."""
    return f"{script_open()}{DISPATCH_JS}</script>"


__all__ = ["new_nonce", "current_nonce", "script_open", "csp_value", "dispatcher", "DISPATCH_JS"]
