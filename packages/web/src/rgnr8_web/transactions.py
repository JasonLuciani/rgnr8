"""The bank transactions register — the accountant/bookkeeper's daily surface.

A QuickBooks-style feed view: the raw bank/card activity RGNR8 ingests
(`@rgnr8/ingestion` canonical transactions, or the statement-import path, or a
QBO overlay), each row categorized to an account and matched against the books.
It leads with a **For review** queue (uncategorized or unmatched lines the
bookkeeper needs to action) and then the full register. Categorize/match actions
are gated on `CATEGORIZE_TXNS`; viewing is `VIEW_TRANSACTIONS` (everyone).

Money is signed: positive = money **in** (a deposit/received), negative = money
**out** (spent). This module only renders + summarizes — the matching engine is
`@rgnr8/reconciliation`; here `status` is the already-computed outcome.
"""

from __future__ import annotations

from .csp import script_open

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape

from rgnr8_forecast import Money
from rgnr8_forecast.brand import format_money as _money  # the one shared money formatter

# The chart of accounts a bookkeeper categorizes into (demo default; production
# pulls the tenant's real chart). Kept short + familiar.
DEFAULT_CATEGORIES = (
    "Uncategorized", "Sales income", "Payroll", "Rent", "Software & SaaS",
    "Contractors", "Bank fees", "Owner draw", "Transfer", "Taxes",
)

# review = needs a human (uncategorized OR no book match); matched = tied to the
# books; unmatched = on the feed but not yet in the books (a missing entry).
_STATUS_LABEL = {"matched": "Matched", "review": "For review", "unmatched": "Unmatched"}
_STATUS_COLOR = {"matched": "var(--rg-pos)", "review": "var(--rg-watch)", "unmatched": "var(--rg-risk)"}


@dataclass(frozen=True, slots=True)
class BankTransaction:
    id: str
    date: str  # ISO YYYY-MM-DD
    description: str
    amount: Money  # signed: + in, - out
    category: str = "Uncategorized"
    status: str = "review"  # matched | review | unmatched
    counterparty: str = ""
    account: str = "Checking"  # which bank/card account the line is on

    @property
    def needs_review(self) -> bool:
        return self.status != "matched" or self.category == "Uncategorized"


@dataclass(frozen=True, slots=True)
class RegisterSummary:
    total: int
    matched: int
    review: int
    unmatched: int
    inflow: Money
    outflow: Money


def summarize(txns: Sequence[BankTransaction], currency: str = "USD") -> RegisterSummary:
    matched = sum(1 for t in txns if t.status == "matched" and t.category != "Uncategorized")
    unmatched = sum(1 for t in txns if t.status == "unmatched")
    review = sum(1 for t in txns if t.needs_review and t.status != "unmatched")
    ccy: str = txns[0].amount.currency if txns else currency
    inflow = Money(0, ccy)
    outflow = Money(0, ccy)
    for t in txns:
        if t.amount.minor_units >= 0:
            inflow = inflow + t.amount
        else:
            outflow = outflow + t.amount
    return RegisterSummary(len(txns), matched, review, unmatched, inflow, outflow)


def _cat_select(t: BankTransaction, can_categorize: bool, categories: Sequence[str]) -> str:
    if not can_categorize:
        return escape(t.category)
    opts = "".join(
        f'<option value="{escape(c)}"{" selected" if c == t.category else ""}>{escape(c)}</option>'
        for c in categories
    )
    return f"<select data-txn='{escape(t.id)}' onchange='categorize(this)'>{opts}</select>"


def _suggestion_html(t: BankTransaction, suggestion: "tuple[str, float] | None") -> str:
    """The inline auto-categorize hint for an uncategorized For-review row: the
    suggested category, a confidence chip, and a one-click Apply that reuses the
    categorize POST (sets the category, which the accept path then matches)."""
    if suggestion is None:
        return ""
    category, confidence = suggestion
    pct = round(max(0.0, min(1.0, confidence)) * 100)
    return (
        "<div class='sugg' style='margin-top:7px;display:flex;gap:8px;align-items:center;flex-wrap:wrap'>"
        "<span class='muted' style='font-size:12px'>Suggested</span>"
        f"<strong style='font-size:13px'>{escape(category)}</strong>"
        f"<span class='chip' style='padding:2px 9px;font-size:11px'>{pct}% confident</span>"
        "<button class='btn sage' style='padding:4px 9px' "
        f"onclick=\"applySuggestion(this,'{escape(t.id)}','{escape(category)}')\">Apply suggestion</button>"
        "</div>"
    )


def _row(
    t: BankTransaction,
    can_categorize: bool,
    categories: Sequence[str],
    suggestion: "tuple[str, float] | None" = None,
) -> str:
    spent = _money(t.amount) if t.amount.minor_units < 0 else ""
    recd = _money(t.amount) if t.amount.minor_units >= 0 else ""
    desc = escape(t.description) + (
        f"<br><span class='muted' style='font-size:12px'>{escape(t.counterparty)}</span>" if t.counterparty else ""
    )
    dot = _STATUS_COLOR.get(t.status, "var(--rg-muted)")
    action = ""
    if can_categorize and t.needs_review:
        action = ("<button class='btn sage' style='padding:5px 10px' "
                  f"onclick=\"accept(this,'{escape(t.id)}')\">Accept</button>")
    # Auto-categorize hint only for uncategorized lines the caller can action.
    sugg = _suggestion_html(t, suggestion) if (can_categorize and t.category == "Uncategorized") else ""
    return (
        f"<tr><td class='muted'>{escape(t.date)}</td><td>{desc}</td>"
        f"<td>{_cat_select(t, can_categorize, categories)}{sugg}</td>"
        f"<td class='num'>{spent}</td><td class='num'>{recd}</td>"
        f"<td><span class='dot' style='background:{dot}'></span>{_STATUS_LABEL.get(t.status, t.status)}</td>"
        f"<td style='text-align:right'>{action}</td></tr>"
    )


def render_transactions(
    tenant: str,
    account_name: str,
    txns: Sequence[BankTransaction],
    *,
    can_categorize: bool,
    categories: Sequence[str] = DEFAULT_CATEGORIES,
    suggestions: "Mapping[str, tuple[str, float]] | None" = None,
) -> str:
    """The register body (wrap in `render_shell`). Leads with the For-review queue
    then the full feed. `can_categorize` toggles the interactive controls.

    `suggestions` maps a transaction id to an auto-categorize `(category,
    confidence)` from `@rgnr8/categorize`; each uncategorized For-review row with a
    suggestion gets an inline hint + one-click Apply. Rows without a suggestion (a
    novel line the engine can't place) show nothing extra."""
    sugg = suggestions or {}
    s = summarize(txns)
    review_rows = "".join(
        _row(t, can_categorize, categories, sugg.get(t.id)) for t in txns if t.needs_review
    )
    all_rows = "".join(_row(t, can_categorize, categories) for t in txns)
    review_block = ""
    if review_rows:
        review_block = f"""<h2>For review <span class="muted" style="text-transform:none;letter-spacing:0">— {s.review + s.unmatched} to action</span></h2>
        <div class="table-scroll"><table><thead><tr><th>Date</th><th>Description</th><th>Category</th><th class="num">Spent</th><th class="num">Received</th><th>Status</th><th></th></tr></thead>
        <tbody>{review_rows}</tbody></table></div>"""
    banner_cls = "warn" if (s.review + s.unmatched) else "good"
    banner = (
        f"{s.matched} matched · {s.review} to categorize · {s.unmatched} unmatched"
        if (s.review + s.unmatched) else f"All {s.matched} transactions reconciled — books are current."
    )
    err_block = ('<div id="rgErr" class="banner warn" role="alert" style="display:none;margin:12px 0"></div>'
                 if can_categorize else "")
    js = f"""{script_open()}
      const T={tenant!r};
      function rgErr(m){{ var b=document.getElementById('rgErr'); if(b){{ b.textContent=m||''; b.style.display=m?'block':'none'; }} }}
      function rgBusy(el,on,label){{ if(!el)return; el.disabled=on; if(el.tagName==='BUTTON'){{ if(on){{ el.dataset.rgPrev=el.dataset.rgPrev||el.textContent; el.textContent=label||'Saving…'; }} else if(el.dataset.rgPrev!=null){{ el.textContent=el.dataset.rgPrev; }} }} }}
      function post(body){{ return fetch('/api/'+T+'/transactions', {{method:'POST', headers:{{'content-type':'application/json'}}, body:JSON.stringify(body), credentials:'same-origin'}}); }}
      async function run(ctl, label, body){{
        rgErr(''); rgBusy(ctl, true, label);
        try{{
          const r = await post(body);
          if(!r.ok){{ rgErr('Could not save this change (HTTP '+r.status+'). No changes were saved.'); rgBusy(ctl, false); return; }}
          location.reload();
        }} catch(e){{ rgErr('Network error — please try again.'); rgBusy(ctl, false); }}
      }}
      function categorize(sel){{ return run(sel, null, {{id: sel.dataset.txn, category: sel.value}}); }}
      function accept(btn, id){{ return run(btn, 'Accepting…', {{id, accept:true}}); }}
      function applySuggestion(btn, id, category){{ return run(btn, 'Applying…', {{id, category}}); }}
    </script>""" if can_categorize else ""
    return f"""<h1>Bank transactions</h1>
    <p class="sub">{escape(account_name)} · {escape(tenant)} · the feed RGNR8 ingests, categorized and matched to your books</p>
    <div class="banner {banner_cls}">{banner}</div>
    {err_block}
    {review_block}
    <h2>All transactions</h2>
    <div class="table-scroll"><table><thead><tr><th>Date</th><th>Description</th><th>Category</th><th class="num">Spent</th><th class="num">Received</th><th>Status</th><th></th></tr></thead>
    <tbody>{all_rows}</tbody></table></div>
    {js}"""
