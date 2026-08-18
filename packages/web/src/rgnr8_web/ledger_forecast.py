"""Build the cash forecast from the books, keeping facts and assumptions apart.

RGNR8 has held two pictures of the same business: a **forecast** built from
inputs somebody typed, and **books** built from postings. They could disagree,
and when they did there was no way to tell which was wrong.

This joins them on the honest seam. Some things about next month are *facts the
books already know*:

* what is in the bank right now,
* which invoices are outstanding and when they fall due,
* which bills are owed and when,
* what payroll costs and what payroll liability is due.

Everything else is a *claim about the future* that no ledger can supply: the work
you expect to win, the spending you plan, the rent you intend to keep paying.
Those stay the owner's assumptions.

So the forecast is assembled from both, and every projected flow carries a
provenance saying which half it came from — `source_system="ledger"` for facts,
`source_system="assumption"` for the rest. The UI can then answer the question
that matters when a forecast looks wrong: *is this a number we know, or a number
we guessed?*

Nothing here invents a fact. If the ledger is unreachable, the facts are absent
and say so, rather than being quietly replaced by the assumptions they were
meant to correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from rgnr8_forecast import (
    Bill,
    CashPosition,
    ForecastInputs,
    Invoice,
    Money,
    Provenance,
)

from .ledger_client import LedgerClient

LEDGER = "ledger"
ASSUMPTION = "assumption"


def _minor(v: object) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def _money(minor: object, currency: str = "USD") -> Money:
    return Money(_minor(minor), currency)


def _date(text: object, fallback: date) -> date:
    try:
        return date.fromisoformat(str(text))
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class LedgerFacts:
    """What the books know, as opposed to what anybody expects."""

    cash: Money
    cash_accounts: int
    invoices: tuple[Invoice, ...] = ()
    bills: tuple[Bill, ...] = ()
    payroll_liability: Money = field(default_factory=lambda: Money(0))
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        return (
            f"{len(self.invoices)} open invoices and {len(self.bills)} open bills "
            f"from the books, cash {self.cash.to_decimal_string()}"
        )


def _bank_cash(ledger: LedgerClient, tenant: str, as_of: date) -> tuple[Money, int, str]:
    """Cash on hand: the trial-balance total of the bank and cash accounts.

    This is a fact in the strongest sense available — it is what the business's
    own posted, reconciled books say it holds, not what a bank feed last
    reported.
    """
    accounts = ledger.accounts(tenant)
    if not accounts.ok:
        return Money(0), 0, f"could not read the chart of accounts: {accounts.error()}"
    bank_codes = set()
    raw = accounts.body.get("accounts")
    for a in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(a, dict):
            continue
        subtype = str(a.get("subtype") or "")
        if subtype in {"BANK", "CASH", "UNDEPOSITED_FUNDS"}:
            bank_codes.add(str(a.get("code")))

    tb = ledger.trial_balance(tenant, to=as_of.isoformat())
    if not tb.ok:
        return Money(0), 0, f"could not read the trial balance: {tb.error()}"
    total = 0
    counted = 0
    rows = tb.body.get("rows")
    for r in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(r, dict) or str(r.get("code")) not in bank_codes:
            continue
        # An asset is debit-positive.
        total += _minor(r.get("debit_minor")) - _minor(r.get("credit_minor"))
        counted += 1
    return Money(total), counted, ""


def _open_documents(
    ledger: LedgerClient, tenant: str, kind: str, as_of: date
) -> tuple[list[dict[str, object]], str]:
    res = ledger.documents(tenant, kind)
    if not res.ok:
        return [], f"could not read {kind}: {res.error()}"
    docs = res.body.get("documents")
    out = []
    for d in docs if isinstance(docs, (list, tuple)) else []:
        if isinstance(d, dict) and _minor(d.get("open_minor")) > 0:
            out.append(d)
    return out, ""


def read_ledger_facts(
    ledger: LedgerClient, tenant: str, as_of: date, currency: str = "USD"
) -> LedgerFacts:
    """Read everything about the near future that the books already settle."""
    problems: list[str] = []

    cash, accounts, problem = _bank_cash(ledger, tenant, as_of)
    if problem:
        problems.append(problem)

    invoices: list[Invoice] = []
    raw_invoices, problem = _open_documents(ledger, tenant, "invoices", as_of)
    if problem:
        problems.append(problem)
    for d in raw_invoices:
        doc_id = str(d.get("id"))
        invoices.append(Invoice(
            id=doc_id,
            customer_id=str(d.get("party_id")),
            issue_date=_date(d.get("date"), as_of),
            due_date=_date(d.get("due_date"), as_of),
            open_amount=_money(d.get("open_minor"), currency),
            provenance=Provenance(LEDGER, "invoice", doc_id),
        ))

    bills: list[Bill] = []
    raw_bills, problem = _open_documents(ledger, tenant, "bills", as_of)
    if problem:
        problems.append(problem)
    for d in raw_bills:
        doc_id = str(d.get("id"))
        bills.append(Bill(
            id=doc_id,
            vendor_id=str(d.get("party_id")),
            due_date=_date(d.get("due_date"), as_of),
            amount=_money(d.get("open_minor"), currency),
            provenance=Provenance(LEDGER, "bill", doc_id),
        ))

    # Payroll liabilities are money already owed on work already done — as much
    # a fact as an unpaid bill, and one businesses routinely forget is coming.
    liability = Money(0)
    liabilities = ledger.payroll_liabilities(tenant)
    if liabilities.ok:
        liability = _money(liabilities.body.get("owed_minor"), currency)
    else:
        problems.append(f"could not read payroll liabilities: {liabilities.error()}")

    return LedgerFacts(
        cash=cash,
        cash_accounts=accounts,
        invoices=tuple(sorted(invoices, key=lambda i: (i.due_date, i.id))),
        bills=tuple(sorted(bills, key=lambda b: (b.due_date, b.id))),
        payroll_liability=liability,
        problems=tuple(problems),
    )


def merge_forecast_inputs(
    assumptions: ForecastInputs, facts: LedgerFacts, as_of: date
) -> ForecastInputs:
    """Facts from the books, assumptions from the owner, in one set of inputs.

    Receivables and payables are **replaced**, not merged: an invoice exists in
    the books or it doesn't, and a typed-in duplicate of a real invoice would
    double-count the same money. Everything forward-looking — pipeline,
    recurring plans, planned one-offs, payroll schedules, debt — is left exactly
    as the owner set it, because no ledger can know it.

    When the books are unreachable, the owner's inputs are returned untouched
    rather than blanked. A forecast built on stale assumptions is wrong; one
    built on silently-zeroed facts is worse.
    """
    if not facts.ok and facts.cash_accounts == 0 and not facts.invoices and not facts.bills:
        return assumptions

    opening = CashPosition(
        as_of=as_of,
        available=facts.cash,
        restricted=assumptions.opening.restricted,
        verified=facts.ok,
    )

    # The payroll liability is a dated outflow the books already commit to.
    one_time = tuple(
        item for item in assumptions.one_time
        if not (item.provenance and item.provenance.source_type == "payroll-liability")
    )
    if facts.payroll_liability.minor_units > 0:
        from rgnr8_forecast import Category, Direction, OneTimeItem

        one_time = one_time + (OneTimeItem(
            label="Payroll taxes and withholdings due",
            category=Category.PAYROLL_TAX,
            direction=Direction.OUTFLOW,
            amount=facts.payroll_liability,
            on_date=as_of,
            provenance=Provenance(LEDGER, "payroll-liability", "payroll"),
        ),)

    return replace(
        assumptions,
        opening=opening,
        invoices=facts.invoices,
        bills=facts.bills,
        one_time=one_time,
    )


def forecast_from_ledger(
    ledger: LedgerClient,
    tenant: str,
    assumptions: ForecastInputs,
    as_of: date,
    currency: str = "USD",
) -> tuple[ForecastInputs, LedgerFacts]:
    """The hybrid: read the facts, keep the assumptions, return both."""
    facts = read_ledger_facts(ledger, tenant, as_of, currency)
    return merge_forecast_inputs(assumptions, facts, as_of), facts


def provenance_split(inputs: ForecastInputs) -> dict[str, int]:
    """How much of this forecast is known versus assumed — for the UI to say so."""
    known = 0
    assumed = 0
    for group in (inputs.invoices, inputs.bills, inputs.one_time,
                  inputs.recurring, inputs.pipeline):
        for item in group:
            prov = getattr(item, "provenance", None)
            if prov is not None and prov.source_system == LEDGER:
                known += 1
            else:
                assumed += 1
    return {"from_the_books": known, "assumed": assumed}
