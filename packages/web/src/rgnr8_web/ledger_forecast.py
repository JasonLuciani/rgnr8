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

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date

from rgnr8_forecast import (
    Bill,
    CashPosition,
    ForecastInputs,
    Invoice,
    Money,
    Provenance,
    format_money,
)

from .ledger_client import LedgerClient, LedgerResponse
from .open_items import as_forecast_items, read_open_items
from .qbo_sync import quickbooks_bill

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
            f"from the books, cash {format_money(self.cash)}"
        )


def _ask(what: str, call: "Callable[[], LedgerResponse]") -> "tuple[LedgerResponse | None, str]":
    """One ledger call, with either kind of failure turned into one sentence.

    `LedgerClient._call` is where the never-raises contract is enforced, and it
    is the fix for the outage that took down every ledger-backed screen. This
    helper exists for a smaller reason: the four readers below each had their
    own two-line "was it ok, else describe the error" dance, and the descriptions
    had already drifted apart. One helper, one phrasing.

    It also still catches, which is not redundant — this module is handed a
    `LedgerClient` by type hint only, and a test double or a future wrapper is
    not obliged to honour that contract. A reader of the books should not be the
    thing that discovers otherwise.

    The message deliberately carries only the exception's type, never its text.
    A transport error's string can contain the request URL, and that URL carries
    the derived bearer token.
    """
    try:
        res = call()
    except Exception as exc:  # noqa: BLE001 — any transport failure, not a class of them
        return None, f"could not read {what}: the ledger service did not answer ({type(exc).__name__})"
    if not res.ok:
        return None, f"could not read {what}: {res.error()}"
    return res, ""


# The account RGNR8's own payroll engine posts withholdings and employer taxes
# to, and the one the ledger's liabilities read uses when no code is given
# (`DEFAULT_PAYROLL_CODES` in ledger-service/src/payroll.ts). If that default
# ever changes, this has to change with it.
_PAYROLL_LIABILITY_CODE = "2300"


def _bank_cash(
    ledger: LedgerClient, tenant: str, as_of: date
) -> tuple[Money, int, str, frozenset[str]]:
    """Cash on hand: the trial-balance total of the bank and cash accounts.

    This is a fact in the strongest sense available — it is what the business's
    own posted books say it holds, not what a bank feed last reported.

    The returned count is how many cash accounts the *chart* holds, and it is
    the caller's signal for whether the question was answerable at all: zero
    means "there was nowhere to read this from", never "the balance is zero".
    A chart that holds a bank account with no postings against it genuinely is
    at zero, and says so with a count of one.
    """
    accounts, problem = _ask("the chart of accounts", lambda: ledger.accounts(tenant))
    if accounts is None:
        return Money(0), 0, problem, frozenset()
    bank_codes = set()
    codes: set[str] = set()
    raw = accounts.body.get("accounts")
    for a in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(a, dict):
            continue
        codes.add(str(a.get("code")))
        subtype = str(a.get("subtype") or "")
        if subtype in {"BANK", "CASH", "UNDEPOSITED_FUNDS"}:
            bank_codes.add(str(a.get("code")))

    if not bank_codes:
        # The books answered, and the answer was "there is nowhere here to read
        # cash from" — a chart part-way through its first import, which is the
        # normal state of a QuickBooks company for the first hour of its life.
        # Returning Money(0) with no problem recorded made that indistinguishable
        # from a business that genuinely holds nothing, and the difference was
        # then printed in the largest type on the product's flagship screen.
        return Money(0), 0, (
            "could not read cash on hand: your chart of accounts has no bank or "
            "cash account yet"
        ), frozenset(codes)

    tb, problem = _ask("the trial balance", lambda: ledger.trial_balance(tenant, to=as_of.isoformat()))
    if tb is None:
        return Money(0), 0, problem, frozenset(codes)
    total = 0
    rows = tb.body.get("rows")
    for r in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(r, dict) or str(r.get("code")) not in bank_codes:
            continue
        # An asset is debit-positive.
        total += _minor(r.get("debit_minor")) - _minor(r.get("credit_minor"))
    return Money(total), len(bank_codes), "", frozenset(codes)


def _open_documents(
    ledger: LedgerClient, tenant: str, kind: str, as_of: date
) -> tuple[list[dict[str, object]], str]:
    res, problem = _ask(kind, lambda: ledger.documents(tenant, kind))
    if res is None:
        return [], problem
    docs = res.body.get("documents")
    out = []
    for d in docs if isinstance(docs, (list, tuple)) else []:
        if isinstance(d, dict) and _minor(d.get("open_minor")) > 0:
            out.append(d)
    return out, ""


def in_parallel_run(ledger: LedgerClient, tenant: str) -> bool:
    """Whether QuickBooks is this tenant's book of record, per the ledger."""
    if not hasattr(ledger, "settings"):
        return False
    res = ledger.settings(tenant)
    raw = res.body.get("settings") if res.ok else None
    return isinstance(raw, dict) and raw.get("book_of_record") == "quickbooks"


def read_ledger_facts(
    ledger: LedgerClient, tenant: str, as_of: date, currency: str = "USD"
) -> LedgerFacts:
    """Read everything about the near future that the books already settle."""
    problems: list[str] = []

    cash, accounts, problem, chart_codes = _bank_cash(ledger, tenant, as_of)
    if problem:
        problems.append(problem)

    invoices: list[Invoice] = []
    raw_invoices, problem = _open_documents(ledger, tenant, "invoices", as_of)
    docs_read = not problem
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
    docs_read = docs_read and not problem
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

    # A QuickBooks mirror has A/R and A/P balances but no documents behind them,
    # so the books alone say "nothing is owed" while QuickBooks has invoices open.
    # Then the open items QuickBooks reported at the last Refresh stand in, as
    # facts from QuickBooks. Real documents always win: after cutover they exist.
    # Only while QuickBooks IS the book of record (after cutover a stale copy
    # must never come back once the real invoices are paid), and only when the
    # books were actually read (a failed read is not "nothing is owed").
    if docs_read and not raw_invoices and not raw_bills and in_parallel_run(ledger, tenant):
        snapshot = read_open_items(ledger, tenant, currency)
        if snapshot is not None and not snapshot.empty:
            qbo_invoices, qbo_bills = as_forecast_items(snapshot, as_of, currency)
            invoices, bills = list(qbo_invoices), list(qbo_bills)

    # Payroll liabilities are money already owed on work already done — as much
    # a fact as an unpaid bill, and one businesses routinely forget is coming.
    #
    # Only asked when the account RGNR8's payroll posts to exists. A company that
    # came across from QuickBooks has QuickBooks' own account codes and no 2300,
    # and the ledger answers "unknown account code 2300" — which was being
    # reported as "the books couldn't be read", in a warning banner, on every
    # page load, for every migrated company, forever. No such account means
    # RGNR8 payroll has never posted, so nothing is owed through it; that is an
    # answer, not a failure. (Payroll run in QuickBooks lands in QuickBooks'
    # own liability accounts and reaches the outlook through the bills and the
    # trial balance like any other liability.)
    liability = Money(0)
    if not chart_codes or _PAYROLL_LIABILITY_CODE in chart_codes:
        liabilities, problem = _ask("payroll liabilities", lambda: ledger.payroll_liabilities(tenant))
        if liabilities is None:
            problems.append(problem)
        else:
            liability = _money(liabilities.body.get("owed_minor"), currency)

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

    # Cash is taken from the books only when the books held somewhere to read it
    # from. This is deliberately *not* folded into the whole-record guard above:
    # that guard needs the read to have failed completely, and a company whose
    # invoices came through fine while its chart has no bank account yet sails
    # straight past it. The owner's own figure then has to stand on its own.
    #
    # "Could not work it out" and "it is zero" are different sentences, and this
    # figure is the largest thing on the screen. `verified` follows the same
    # rule, because a zero nobody checked must not present as a checked zero.
    cash_known = facts.cash_accounts > 0
    opening = CashPosition(
        as_of=as_of,
        available=facts.cash if cash_known else assumptions.opening.available,
        restricted=assumptions.opening.restricted,
        verified=facts.ok and cash_known,
    )

    # The payroll liability is a dated outflow the books already commit to.
    one_time = tuple(
        item for item in assumptions.one_time
        if not (item.provenance and item.provenance.source_type == "payroll-liability")
    )
    # A QuickBooks Refresh also records each open bill as a one-time payment in
    # the owner's inputs. When the bills come from the books (or the QuickBooks
    # copy behind them), those would count every bill twice.
    if facts.bills:
        one_time = tuple(i for i in one_time if not quickbooks_bill(i))
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
