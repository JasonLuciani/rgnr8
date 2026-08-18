"""Consolidation — several sets of books, one set of statements.

An owner with three LLCs has three sets of books because they are three legal
entities. The bank manager wants one set of statements. This is the worksheet
that gets from one to the other: an entity per column, the combined total, the
eliminations, and the consolidated result — the form an accountant asks for,
because it is the only one where the eliminations are visible rather than
assumed.

When the entities disagree about what they owe each other, the page says so and
refuses to show a total. A consolidation with a plug in it is a lie with a
number at the bottom.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def render_consolidation_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Consolidation unavailable", '<div class="banner warn">The ledger service '
                 f"isn't reachable right now.</div>{extra}")


def render_groups(
    tenant: str,
    groups: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    rows = [g for g in _seq(groups.get("groups")) if isinstance(g, Mapping)]
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    if rows:
        cells = "".join(
            "<tr>"
            f'<td><a href="/t/{_esc(tenant)}/consolidation/{_esc(g.get("id"))}">'
            f'{_esc(g.get("name"))}</a></td>'
            f'<td class="muted">{_esc(", ".join(str(m.get("label")) for m in _seq(g.get("members")) if isinstance(m, Mapping)))}</td>'
            f'<td class="muted">{_esc(", ".join(str(c) for c in _seq(g.get("intercompany_codes"))))}</td>'
            "</tr>"
            for g in rows
        )
        body = (
            "<table><thead><tr><th>Group</th><th>Entities</th>"
            f"<th>Intercompany accounts</th></tr></thead><tbody>{cells}</tbody></table>"
        )
    else:
        body = (
            '<p class="muted">No groups. A group names the entities that get consolidated — '
            "and it is the only way this company can read another one's books, which is "
            "deliberate.</p>"
        )

    out = head + _card("Entity groups", body)
    if can_edit:
        out += _card("Define a group", (
            f'<form method="post" action="/t/{_esc(tenant)}/consolidation" class="grid">'
            '<label>Name<input name="name" required placeholder="Harper Holdings"></label>'
            '<label>Entities<input name="members" required '
            'placeholder="parent, sub, other"></label>'
            '<label>Intercompany accounts<input name="intercompany_codes" '
            'placeholder="1900, 2900"></label>'
            '<div style="grid-column:1/-1"><button type="submit">Create the group</button>'
            '<span class="muted" style="margin-left:8px">Entities are the tenant ids of the '
            "other companies. Nothing is consolidated that is not named here.</span>"
            "</div></form>"
        ))
    return out


def render_consolidation(
    tenant: str,
    report: Mapping[str, object],
    *,
    can_edit: bool,
    through: str = "",
    message: str = "",
    error: str = "",
) -> str:
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    group = report.get("group") if isinstance(report.get("group"), Mapping) else {}
    assert isinstance(group, Mapping)
    entities = [e for e in _seq(report.get("entities")) if isinstance(e, Mapping)]

    refused = report.get("refused")
    if refused:
        rows = [r for r in _seq(report.get("intercompany")) if isinstance(r, Mapping)]
        cells = ""
        for r in rows:
            by_entity = r.get("by_entity") if isinstance(r.get("by_entity"), Mapping) else {}
            assert isinstance(by_entity, Mapping)
            columns = "".join(
                _num(by_entity.get(str(e.get("tenant_id")))) for e in entities
            )
            cells += (
                f"<tr><td>{_esc(r.get('account_code'))}</td>{columns}"
                f"{_num(r.get('net_minor'))}</tr>"
            )
        headers = "".join(
            f"<th class='num'>{_esc(e.get('label'))}</th>" for e in entities
        )
        body = (
            _banner("warn", str(refused))
            + f'<p class="muted">Out by {money(report.get("mismatch_minor"))}.</p>'
            + "<table><thead><tr><th>Account</th>"
            f"{headers}<th class='num'>Net</th></tr></thead><tbody>{cells}</tbody></table>"
        )
        out = head + _card(f"{group.get('name')} — not consolidated", body)
        if can_edit:
            out += _anyway_form(tenant, group, through)
            out += _elimination_form(tenant, group)
        return out

    rows = [r for r in _seq(report.get("rows")) if isinstance(r, Mapping)]
    headers = "".join(f"<th class='num'>{_esc(e.get('label'))}</th>" for e in entities)
    cells = ""
    for r in rows:
        by_entity = r.get("by_entity") if isinstance(r.get("by_entity"), Mapping) else {}
        assert isinstance(by_entity, Mapping)
        columns = "".join(_num(by_entity.get(str(e.get("tenant_id")))) for e in entities)
        cells += (
            f"<tr><td>{_esc(r.get('account_code'))} "
            f'<span class="muted">{_esc(r.get("name"))}</span></td>{columns}'
            f"{_num(r.get('combined_minor'))}{_num(r.get('elimination_minor'))}"
            f"{_num(r.get('consolidated_minor'))}</tr>"
        )
    body = (
        "<table><thead><tr><th>Account</th>"
        f"{headers}<th class='num'>Combined</th><th class='num'>Eliminations</th>"
        f"<th class='num'>Consolidated</th></tr></thead><tbody>{cells}</tbody></table>"
        '<p class="muted">Amounts are debit-positive, so a liability or revenue reads '
        "negative. The eliminations column is the whole point of the worksheet: it is what "
        "the group owed itself.</p>"
    )
    out = head + _card(f"{group.get('name')}", body, (
        f'<a class="rg-btn" href="/t/{_esc(tenant)}/consolidation">All groups</a>'
    ))

    mismatch = _minor(report.get("mismatch_minor")) or 0
    if mismatch:
        out += _banner(
            "warn",
            f"{money(abs(mismatch))} of this is an intercompany difference nobody has "
            "explained. It is on its own line rather than hidden in an account, but it is "
            "still a difference.",
        )

    eliminations = [e for e in _seq(report.get("eliminations")) if isinstance(e, Mapping)]
    if eliminations:
        items = "".join(
            f"<tr><td>{_esc(e.get('account_code'))}</td>{_num(e.get('signed_minor'))}"
            f'<td class="muted">{_esc(e.get("reason"))}</td></tr>'
            for e in eliminations
        )
        out += _card("Eliminations", (
            "<table><thead><tr><th>Account</th><th class='num'>Amount</th>"
            f"<th>Why</th></tr></thead><tbody>{items}</tbody></table>"
        ))

    if can_edit:
        out += _elimination_form(tenant, group)
    return out


def _anyway_form(tenant: str, group: Mapping[str, object], through: str) -> str:
    return _card("Consolidate anyway", (
        f'<form method="get" action="/t/{_esc(tenant)}/consolidation/{_esc(group.get("id"))}">'
        f'<input type="hidden" name="through" value="{_esc(through)}">'
        '<input type="hidden" name="allow_mismatch" value="1">'
        '<button type="submit">Show it with the difference on its own line</button>'
        '<span class="muted" style="margin-left:8px">The statements will still refuse — a '
        "set of financials is not a worksheet you can annotate.</span></form>"
    ))


def _elimination_form(tenant: str, group: Mapping[str, object]) -> str:
    return _card("Add an elimination", (
        f'<form method="post" action="/t/{_esc(tenant)}/consolidation/'
        f'{_esc(group.get("id"))}/eliminations" class="grid">'
        '<label>Why<input name="description" required '
        'placeholder="Intercompany management fee"></label>'
        '<label>Account<input name="code1" required placeholder="4100"></label>'
        '<label>Debit<input name="debit1" placeholder="0.00"></label>'
        '<label>Account<input name="code2" required placeholder="6600"></label>'
        '<label>Credit<input name="credit2" placeholder="0.00"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Add it</button>'
        '<span class="muted" style="margin-left:8px">It has to balance. An elimination that '
        "doesn't moves value into or out of the group, which is not what eliminating "
        "means.</span></div></form>"
    ))


__all__ = [
    "render_consolidation",
    "render_consolidation_unavailable",
    "render_groups",
]
