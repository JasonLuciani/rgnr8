"""Split one commingled bank account into two (or more) sets of books.

The problem this solves: a business owner runs personal and business spending
through one checking account (or one account serves several clients/entities). The
books can't be trusted until those flows are separated. This engine takes a single
imported statement and routes every transaction to a *book* by rule, producing one
balanced set of books per destination — auditable line by line.

The pieces mirror the categorization stack so the same mental model (and the same
`Rule` predicates) apply:

* **`BookRule`** wraps a `Rule` and names the `book` a matching transaction lands
  in. `BookRouter` is an ordered list of them with a `default_book` fallback —
  first match wins, exactly like `RuleSet`, so precedence is explicit.
* **`split_books`** routes a batch, optionally attaching a category to each entry
  from a `Categorizer` (so a routed entry carries *both* its book and its
  category), and returns a `SplitResult`.
* Each `BookSet` turns its routed transactions into a balanced double-entry
  journal: every transaction posts to that book's own cash account and an
  offsetting income/expense account, so a book's debits always equal its credits.
  Splitting one account into N books therefore yields N balanced ledgers.
* The split is fully auditable: `SplitResult.audit_trail()` reports, per original
  transaction, which book it landed in and which rule put it there.

Stdlib-only, frozen, deterministic — no wall clock, no randomness. Reads
transactions through the structural `TxnLike`/categorize contracts, so this adds
no cross-package dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .rules import Rule
from .suggester import Categorizer
from .txn import TxnLike

# The account, within a book, that the commingled bank flows land on. Each book
# gets its own so the two sets of books never share a cash line.
CASH_ACCOUNT = "Cash"
# Where a transaction with no category lands — an explicit suspense line a human
# still has to clear, never a silent plug.
SUSPENSE_ACCOUNT = "Uncategorized"


@dataclass(frozen=True)
class BookRule:
    """Route a transaction to `book` when its `rule` matches. `rule` reuses the
    ordinary categorization `Rule` predicates (description regex, counterparty,
    amount sign), so routing and categorizing speak the same language."""

    book: str
    rule: Rule

    @property
    def name(self) -> str:
        return self.rule.name

    def matches(self, txn: TxnLike) -> bool:
        return self.rule.matches(txn)


@dataclass(frozen=True)
class BookRouter:
    """An ordered set of `BookRule`s plus a `default_book`. `route` returns the
    first matching rule's book (and the rule that decided it), or the default with
    no rule when nothing matches — deterministic, earlier rules win."""

    rules: tuple[BookRule, ...]
    default_book: str

    def __init__(self, rules: Iterable[BookRule], default_book: str) -> None:
        object.__setattr__(self, "rules", tuple(rules))
        object.__setattr__(self, "default_book", default_book)

    def route(self, txn: TxnLike) -> tuple[str, Optional[str]]:
        for br in self.rules:
            if br.matches(txn):
                return br.book, br.name
        return self.default_book, None

    @property
    def books(self) -> tuple[str, ...]:
        """Every book this router can produce, default first, in stable order."""
        seen: list[str] = [self.default_book]
        for br in self.rules:
            if br.book not in seen:
                seen.append(br.book)
        return tuple(seen)


@dataclass(frozen=True)
class RoutedEntry:
    """One transaction after routing: which book it landed in, which rule put it
    there (None = the default), and the category assigned to it."""

    txn_id: str
    description: str
    counterparty: str
    amount_minor: int
    currency: str
    book: str
    routed_by: Optional[str]
    category: str
    category_source: str


@dataclass(frozen=True)
class Posting:
    """One leg of a double-entry line. Exactly one of debit/credit is non-zero."""

    account: str
    debit_minor: int
    credit_minor: int


def _postings_for(entry: RoutedEntry) -> tuple[Posting, Posting]:
    """A balanced two-leg posting for one routed transaction. An inflow debits the
    book's cash and credits the category; an outflow does the reverse. The two legs
    are equal and opposite, so any set of them sums to zero."""
    amt = entry.amount_minor
    cat = entry.category or SUSPENSE_ACCOUNT
    if amt >= 0:  # money in
        return (
            Posting(CASH_ACCOUNT, debit_minor=amt, credit_minor=0),
            Posting(cat, debit_minor=0, credit_minor=amt),
        )
    mag = -amt  # money out
    return (
        Posting(cat, debit_minor=mag, credit_minor=0),
        Posting(CASH_ACCOUNT, debit_minor=0, credit_minor=mag),
    )


@dataclass(frozen=True)
class BookSet:
    """One destination book: the transactions routed to it, and the balanced
    journal they produce."""

    book: str
    entries: tuple[RoutedEntry, ...]

    def journal(self) -> list[Posting]:
        """The double-entry postings for this book, two per routed transaction."""
        out: list[Posting] = []
        for e in self.entries:
            out.extend(_postings_for(e))
        return out

    @property
    def debits_minor(self) -> int:
        return sum(p.debit_minor for p in self.journal())

    @property
    def credits_minor(self) -> int:
        return sum(p.credit_minor for p in self.journal())

    @property
    def balanced(self) -> bool:
        """A book balances when its debits equal its credits — true by
        construction here, asserted so a future change can't break it silently."""
        return self.debits_minor == self.credits_minor

    @property
    def net_cash_minor(self) -> int:
        """Net movement across this book's cash account (signed)."""
        return sum(e.amount_minor for e in self.entries)


@dataclass(frozen=True)
class SplitResult:
    """The outcome of splitting one statement into books."""

    books: tuple[BookSet, ...]

    def book(self, book_id: str) -> Optional[BookSet]:
        for b in self.books:
            if b.book == book_id:
                return b
        return None

    @property
    def all_balanced(self) -> bool:
        return all(b.balanced for b in self.books)

    @property
    def entry_count(self) -> int:
        return sum(len(b.entries) for b in self.books)

    def audit_trail(self) -> list[dict[str, object]]:
        """Per original transaction: where it went and what put it there — the
        record that makes the split defensible."""
        rows: list[dict[str, object]] = []
        for b in self.books:
            for e in b.entries:
                rows.append({
                    "txn_id": e.txn_id,
                    "description": e.description,
                    "amount_minor": e.amount_minor,
                    "book": e.book,
                    "routed_by": e.routed_by or "(default)",
                    "category": e.category,
                    "category_source": e.category_source,
                })
        return rows


def split_books(
    txns: Iterable[TxnLike],
    router: BookRouter,
    categorizer: Optional[Categorizer] = None,
) -> SplitResult:
    """Route `txns` into balanced sets of books.

    Every transaction is routed to exactly one book by `router`; if a
    `categorizer` is given, each entry also carries its category suggestion (so a
    book's lines post to real income/expense accounts rather than a single
    suspense line). Books appear in the router's stable order; empty books are
    omitted. The result's per-book journals each balance."""
    buckets: dict[str, list[RoutedEntry]] = {b: [] for b in router.books}
    for txn in txns:
        book, routed_by = router.route(txn)
        if categorizer is not None:
            s = categorizer.suggest(txn)
            category, source = s.category, s.source
        else:
            category, source = SUSPENSE_ACCOUNT, "none"
        entry = RoutedEntry(
            txn_id=txn.id,
            description=txn.description,
            counterparty=txn.counterparty,
            amount_minor=txn.amount_minor,
            currency=txn.currency,
            book=book,
            routed_by=routed_by,
            category=category,
            category_source=source,
        )
        buckets.setdefault(book, []).append(entry)
    books = tuple(
        BookSet(book=b, entries=tuple(entries))
        for b, entries in buckets.items()
        if entries
    )
    return SplitResult(books=books)


__all__ = [
    "BookRule",
    "BookRouter",
    "RoutedEntry",
    "Posting",
    "BookSet",
    "SplitResult",
    "split_books",
    "CASH_ACCOUNT",
    "SUSPENSE_ACCOUNT",
]
