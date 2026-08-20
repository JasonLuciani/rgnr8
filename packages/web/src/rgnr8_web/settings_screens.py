"""Account settings — the options an administrator turns on or off.

Rendered from the ledger service's per-account settings, with each section
shown only when the viewer holds the permission that governs it. The retention
controls are separated deliberately: configuring how long data is kept, and
erasing a client's data, are more sensitive than choosing a costing method, and
the permission model reflects that.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


def render_settings_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Settings unavailable",
        '<div class="banner warn">The ledger service isn\'t reachable right now.</div>' + extra,
    )


def render_settings(
    tenant: str,
    settings: Mapping[str, object],
    *,
    can_manage_settings: bool,
    can_manage_retention: bool,
    can_erase: bool,
    done: str = "",
    error: str = "",
) -> str:
    costing = str(settings.get("inventory_costing_method", "MOVING_AVERAGE"))
    base_ccy = str(settings.get("base_currency", "USD"))
    multi = bool(settings.get("multi_currency_enabled", False))
    audit_days = settings.get("retention_audit_days", 0)
    soft_days = settings.get("retention_soft_delete_days", 0)

    banner = ""
    if done:
        banner += f'<div class="banner good">{_esc(done)}</div>'
    if error:
        banner += f'<div class="banner warn">{_esc(error)}</div>'

    disabled = "" if can_manage_settings else " disabled"
    base = f"/t/{_esc(tenant)}/settings"

    # --- account options (MANAGE_SETTINGS) ---
    options_form = (
        f'<form method="post" action="{base}" class="grid">'
        "<label>Inventory costing method"
        f'<select name="inventory_costing_method"{disabled}>'
        + _options([
            ("MOVING_AVERAGE", "Moving average"),
            ("FIFO", "FIFO — first in, first out (lots)"),
            ("LIFO", "LIFO — last in, first out (lots)"),
            ("SPECIFIC", "Specific identification (named lots)"),
        ], costing)
        + "</select></label>"
        "<label>Base currency"
        f'<input name="base_currency" value="{_esc(base_ccy)}" maxlength="3"{disabled}></label>'
        "<label>Multi-currency consolidation"
        f'<select name="multi_currency_enabled"{disabled}>'
        + _options([("false", "Off — one currency"), ("true", "On — translate group members")],
                   "true" if multi else "false")
        + "</select></label>"
        + ('<div style="grid-column:1/-1"><button type="submit">Save account options</button></div>'
           if can_manage_settings else
           '<p class="muted" style="grid-column:1/-1">You can view these, but changing them '
           'needs the Manage settings permission.</p>')
        + "</form>"
    )

    sections = [_card("Account options", options_form)]

    # --- data retention (MANAGE_DATA_RETENTION) ---
    if can_manage_retention:
        retention_form = (
            f'<form method="post" action="{base}" class="grid">'
            "<label>Keep audit-log entries for (days, 0 = forever)"
            f'<input name="retention_audit_days" type="number" min="0" value="{_esc(audit_days)}"></label>'
            "<label>Keep soft-deleted rows for (days, 0 = forever)"
            f'<input name="retention_soft_delete_days" type="number" min="0" value="{_esc(soft_days)}"></label>'
            '<div style="grid-column:1/-1"><button type="submit">Save retention policy</button></div>'
            "</form>"
        )
        sections.append(_card("Data retention", retention_form))

    # --- data retention sweep (MANAGE_DATA_RETENTION) ---
    if can_manage_retention:
        sweep = (
            '<p class="muted">Run the retention policy now — purge audit-log entries '
            "older than the horizon above. Entries within the horizon are kept.</p>"
            f'<form method="post" action="/t/{_esc(tenant)}/retention" style="display:inline">'
            '<button type="submit">Run retention sweep</button></form>'
        )
        sections.append(_card("Run retention now", sweep))

    # --- reset owner-held data (ERASE_DATA) ---
    if can_erase:
        erase = (
            '<p class="muted">Clear the owner-facing data this app holds — the bank '
            "register, close board, decisions and assumption overrides, and cached "
            "forecast. This is irreversible and is logged.</p>"
            '<p class="muted" style="font-size:12px">This does <strong>not</strong> delete '
            "your ledger (journal entries, vendors and their tax IDs, attachments), "
            "QuickBooks connection, users, API keys, billing, or the audit log — so it "
            "is not a full GDPR/CCPA erasure. Contact support for a complete deletion.</p>"
            f'<form method="post" action="/t/{_esc(tenant)}/erase" '
            'onsubmit="return confirm(\'Clear owner-held data? This cannot be undone.\')">'
            '<button type="submit" class="danger">Reset owner-held data</button></form>'
        )
        sections.append(_card("Reset owner-held data", erase))

    return banner + "".join(sections)
