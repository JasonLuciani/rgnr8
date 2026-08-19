"""Integrations — the outbound webhook endpoints an account administrator manages.

Shows the registered endpoints (URL, the events they subscribe to, active state),
a form to add one, and the recent delivery attempts from the durable outbox with
a replay button for anything that dead-lettered. Gated by MANAGE_INTEGRATIONS; the
secret is shown only once, when it is created.
"""

from __future__ import annotations

from collections.abc import Sequence

from .books_screens import _card, _esc
from .webhook_outbox import DEAD, OutboxDelivery
from .webhooks_out import EVENTS, WebhookEndpoint


def render_integrations_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Integrations unavailable",
        '<div class="banner warn">Outbound webhooks are not configured for this '
        "deployment.</div>" + extra,
    )


def _checkboxes(selected: Sequence[str]) -> str:
    return "".join(
        f'<label style="font-weight:400"><input type="checkbox" name="events" value="{_esc(ev)}"'
        f'{" checked" if ev in selected else ""}> {_esc(ev)}</label> '
        for ev in EVENTS
    )


def render_integrations(
    tenant: str,
    endpoints: Sequence[WebhookEndpoint],
    deliveries: Sequence[OutboxDelivery],
    *,
    new_secret: str = "",
    done: str = "",
    error: str = "",
) -> str:
    banner = ""
    if new_secret:
        banner += ('<div class="banner good">Endpoint created. Its signing secret is shown '
                   f"once — copy it now: <code>{_esc(new_secret)}</code></div>")
    if done:
        banner += f'<div class="banner good">{_esc(done)}</div>'
    if error:
        banner += f'<div class="banner warn">{_esc(error)}</div>'

    if endpoints:
        rows = "".join(
            "<tr>"
            f"<td>{_esc(e.id)}</td><td>{_esc(e.url)}</td>"
            f"<td>{_esc(', '.join(e.events))}</td>"
            f"<td>{'active' if e.active else 'paused'}</td>"
            f'<td><form method="post" action="/t/{_esc(tenant)}/integrations/{_esc(e.id)}/delete" '
            'onsubmit="return confirm(\'Remove this endpoint?\')" style="display:inline">'
            '<button type="submit">Remove</button></form></td>'
            "</tr>"
            for e in endpoints
        )
        table = ("<table><thead><tr><th>ID</th><th>URL</th><th>Events</th><th>State</th>"
                 f"<th></th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        table = '<p class="muted">No endpoints yet. Add one below.</p>'

    add_form = (
        f'<form method="post" action="/t/{_esc(tenant)}/integrations" class="grid">'
        '<label>Endpoint ID<input name="id" required placeholder="crm-sync"></label>'
        '<label>HTTPS URL<input name="url" required placeholder="https://example.com/hooks/rgnr8"></label>'
        f'<div style="grid-column:1/-1">Events: {_checkboxes(EVENTS)}</div>'
        '<div style="grid-column:1/-1"><button type="submit">Add endpoint</button> '
        '<span class="muted">A signing secret is generated and shown once.</span></div>'
        "</form>"
    )

    sections = [
        _card("Webhook endpoints", table + add_form),
    ]

    if deliveries:
        drows = "".join(
            "<tr>"
            f"<td>{_esc(d.event_type)}</td><td>{_esc(d.event_id)}</td>"
            f"<td>{_esc(d.endpoint_id)}</td><td>{_esc(d.status)}</td>"
            f"<td>{_esc(d.attempts)}</td><td>{_esc(d.last_error)}</td>"
            + (f'<td><form method="post" '
               f'action="/t/{_esc(tenant)}/integrations/deliveries/{_esc(d.id)}/replay" '
               'style="display:inline"><button type="submit">Replay</button></form></td>'
               if d.status == DEAD else "<td></td>")
            + "</tr>"
            for d in deliveries
        )
        dtable = ("<table><thead><tr><th>Event</th><th>Event ID</th><th>Endpoint</th>"
                  "<th>Status</th><th>Tries</th><th>Last error</th><th></th></tr></thead>"
                  f"<tbody>{drows}</tbody></table>")
        sections.append(_card("Recent deliveries", dtable))

    return banner + "".join(sections)
