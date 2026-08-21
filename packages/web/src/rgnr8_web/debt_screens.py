"""Debt — the screen that answers "what do I owe, and when am I free?".

Renders the debt dashboard (total owed, blended rate, monthly service, next
payments, payoff dates) and a per-loan detail with the amortization schedule and
a payoff-planning what-if. Writes (adding a loan, recording a payment, drawing a
line) are gated by POST_JOURNAL, since each posts to the general ledger.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _banner, _card, _esc, _seq, money
from .provenance_labels import Provenance
from .provenance_labels import badge as prov_badge
from .provenance_labels import legend as prov_legend

# Loan balances post to the general ledger (POSTED) and reconcile against the
# lender's statement (RECONCILED).
_DEBT_PROV = (Provenance.POSTED, Provenance.RECONCILED)

_KINDS = [
    ("TERM", "Term loan"),
    ("SBA", "SBA loan"),
    ("MORTGAGE", "Mortgage"),
    ("EQUIPMENT", "Equipment financing"),
    ("AUTO", "Vehicle loan"),
    ("OWNER_NOTE", "Owner / seller note"),
    ("MCA", "Merchant cash advance"),
    ("INTERCOMPANY", "Intercompany loan"),
    ("LINE_OF_CREDIT", "Line of credit"),
    ("CREDIT_CARD", "Credit card"),
]
_FREQS = [
    ("MONTHLY", "Monthly"), ("BIWEEKLY", "Every 2 weeks"), ("WEEKLY", "Weekly"),
    ("QUARTERLY", "Quarterly"), ("ANNUAL", "Annual"),
]


def render_debt_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Debt unavailable", _banner("warn", "The ledger service isn't reachable right now.") + extra)


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


def _pct_from_micro(micro: object) -> str:
    try:
        return f"{int(str(micro)) / 10_000:.2f}%"
    except (ValueError, TypeError):
        return "—"


def _banners(done: str, error: str) -> str:
    out = ""
    if done:
        out += _banner("good", done)
    if error:
        out += _banner("warn", error)
    return out


def render_debt(
    tenant: str, dashboard: Mapping[str, object], *, can_write: bool, done: str = "", error: str = "",
) -> str:
    totals = dashboard.get("totals", {})
    totals = totals if isinstance(totals, Mapping) else {}
    loans = _seq(dashboard.get("loans"))

    # headline tiles
    tiles = (
        '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px">'
        + _tile("Total owed", money(totals.get("total_principal_minor")))
        + _tile("Avg. rate", _pct_from_micro(totals.get("weighted_avg_rate_micro")))
        + _tile("Monthly debt service", money(totals.get("monthly_debt_service_minor")))
        + _tile("Loans", str(totals.get("loan_count", 0)))
        + "</div>"
    )

    rows = []
    for ln in loans:
        if not isinstance(ln, Mapping):
            continue
        lid = _esc(ln.get("id"))
        maturity = ln.get("maturity_within_90_days")
        due = _esc(ln.get("next_payment_date") or "—")
        badge = ' <span class="pill warn">maturing</span>' if maturity else ""
        rows.append(
            f'<tr><td><a href="/t/{_esc(tenant)}/debt/{lid}">{_esc(ln.get("lender"))}</a>'
            f'<div class="muted" style="font-size:12px">{_esc(ln.get("kind"))}</div></td>'
            f'<td class="num">{money(ln.get("current_principal_minor"))}</td>'
            f'<td class="num">{_pct_from_micro(ln.get("annual_rate_micro"))}</td>'
            f'<td>{due}{badge}</td>'
            f'<td class="num">{money(ln.get("next_payment_minor"))}</td>'
            f'<td>{_esc(ln.get("payoff_date") or "—")}</td>'
            f'<td class="num">{money(ln.get("interest_paid_ytd_minor"))}</td></tr>'
        )
    table = (
        '<table class="tbl"><thead><tr><th>Lender</th><th class="num">Balance</th>'
        '<th class="num">Rate</th><th>Next payment</th><th class="num">Amount</th>'
        '<th>Payoff</th><th class="num">Interest YTD</th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="7" class="muted">No loans yet.</td></tr>')
        + "</tbody></table>"
    )

    sections = [_banners(done, error), prov_legend(_DEBT_PROV),
                _card("What you owe", tiles + table, actions=prov_badge(Provenance.POSTED))]

    if can_write:
        sections.append(_card("Add a loan", _add_loan_form(tenant)))
        sections.append(_card("Record a payment", _payment_form(tenant, loans)))
    else:
        sections.append(
            '<p class="muted">Recording loans and payments needs the Post journal permission.</p>'
        )
    return "".join(sections)


def _tile(label: str, value: str) -> str:
    return (
        '<div class="card" style="margin:0;padding:14px">'
        f'<div class="muted" style="font-size:12px;text-transform:uppercase;letter-spacing:.04em">{_esc(label)}</div>'
        f'<div style="font:600 22px/1.2 var(--rg-serif);margin-top:4px">{_esc(value)}</div></div>'
    )


def _add_loan_form(tenant: str) -> str:
    return (
        f'<form method="post" action="/t/{_esc(tenant)}/debt/loans" class="grid">'
        '<label>Loan id (short handle)<input name="id" required></label>'
        '<label>Lender<input name="lender" required></label>'
        f'<label>Kind<select name="kind">{_options(_KINDS)}</select></label>'
        '<label>Original amount ($)<input name="original_principal" type="text" placeholder="10000.00"></label>'
        '<label>Annual rate (%)<input name="annual_rate_pct" type="text" placeholder="6.5"></label>'
        '<label>Start date<input name="start_date" type="date" required></label>'
        '<label>Term (number of payments)<input name="term_periods" type="number" min="0" placeholder="60"></label>'
        f'<label>Frequency<select name="frequency">{_options(_FREQS)}</select></label>'
        '<label>Deposit proceeds to (account code)<input name="proceeds_to_code" placeholder="1000"></label>'
        '<label>Min DSCR covenant (e.g. 1.25, optional)<input name="min_dscr" placeholder=""></label>'
        '<div style="grid-column:1/-1"><button type="submit">Add loan</button></div>'
        "</form>"
    )


def _payment_form(tenant: str, loans: list[object]) -> str:
    opts = _options(
        [(str(ln.get("id")), f'{ln.get("lender")} ({money(ln.get("current_principal_minor"))})')
         for ln in loans if isinstance(ln, Mapping)]
    ) or '<option value="">— no loans —</option>'
    return (
        f'<form method="post" action="/t/{_esc(tenant)}/debt/payments" class="grid">'
        f'<label>Loan<select name="loan_id">{opts}</select></label>'
        '<label>Date<input name="date" type="date" required></label>'
        '<label>Payment amount ($)<input name="amount" type="text" placeholder="860.66" required></label>'
        '<label>Pay from (account code)<input name="paid_from_code" placeholder="1000"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Record payment</button></div>'
        "</form>"
    )


def render_loan_detail(
    tenant: str, data: Mapping[str, object], *, can_write: bool, done: str = "", error: str = "",
) -> str:
    loan = data.get("loan", {})
    loan = loan if isinstance(loan, Mapping) else {}
    schedule = _seq(data.get("schedule"))
    payments = _seq(data.get("payments"))
    payoff = data.get("payoff")
    lid = _esc(loan.get("id"))

    summary = (
        '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px">'
        + _tile("Balance", money(loan.get("current_principal_minor")))
        + _tile("Rate", _pct_from_micro(loan.get("annual_rate_micro")))
        + _tile("Original", money(loan.get("original_principal_minor")))
        + _tile("Payment", money(loan.get("payment_minor")))
        + "</div>"
    )

    srows = []
    for r in schedule[:600]:
        if not isinstance(r, Mapping):
            continue
        srows.append(
            f'<tr><td>{_esc(r.get("period"))}</td><td>{_esc(r.get("due_date"))}</td>'
            f'<td class="num">{money(r.get("payment_minor"))}</td>'
            f'<td class="num">{money(r.get("principal_minor"))}</td>'
            f'<td class="num">{money(r.get("interest_minor"))}</td>'
            f'<td class="num">{money(r.get("balance_minor"))}</td></tr>'
        )
    schedule_tbl = (
        '<table class="tbl"><thead><tr><th>#</th><th>Due</th><th class="num">Payment</th>'
        '<th class="num">Principal</th><th class="num">Interest</th><th class="num">Balance</th>'
        "</tr></thead><tbody>"
        + ("".join(srows) or '<tr><td colspan="6" class="muted">No schedule (revolving line).</td></tr>')
        + "</tbody></table>"
    )

    prows = []
    for p in payments:
        if not isinstance(p, Mapping):
            continue
        prows.append(
            f'<tr><td>{_esc(p.get("date"))}</td><td>{_esc(p.get("kind"))}</td>'
            f'<td class="num">{money(p.get("principalMinor") or p.get("principal_minor"))}</td>'
            f'<td class="num">{money(p.get("interestMinor") or p.get("interest_minor"))}</td></tr>'
        )
    payments_tbl = (
        '<table class="tbl"><thead><tr><th>Date</th><th>Type</th>'
        '<th class="num">Principal</th><th class="num">Interest</th></tr></thead><tbody>'
        + ("".join(prows) or '<tr><td colspan="4" class="muted">No payments yet.</td></tr>')
        + "</tbody></table>"
    )

    payoff_html = ""
    if isinstance(payoff, Mapping) and payoff.get("periods_saved") is not None:
        payoff_html = _banner(
            "good",
            f'Paying {money(payoff.get("extra_per_period_minor"))} extra per period: '
            f'{_esc(payoff.get("periods_saved"))} fewer payments and '
            f'{money(payoff.get("interest_saved_minor"))} less interest.',
        )
    payoff_form = (
        f'<form method="get" action="/t/{_esc(tenant)}/debt/{lid}" class="grid">'
        '<label>Extra per payment ($)<input name="extra" type="text" placeholder="200.00"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Show me the plan</button></div>'
        "</form>"
    )

    sections = [
        _banners(done, error),
        f'<p><a href="/t/{_esc(tenant)}/debt">← All debt</a></p>',
        _card(_esc(loan.get("lender")), summary),
        _card("Payoff planning", payoff_html + payoff_form),
        _card("Amortization schedule", schedule_tbl),
        _card("Payments", payments_tbl),
    ]
    return "".join(sections)
