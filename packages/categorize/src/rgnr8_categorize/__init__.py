"""RGNR8 auto-categorization — the register's "For review" pre-triage engine.

Manual transaction categorization is the bookkeeper's cost driver: RGNR8's price
bundles human-review load, so every transaction a machine can confidently
categorize is margin. This package turns each bank transaction into a
`Suggestion` — a category, a confidence in [0.0, 1.0], a human-readable reason,
and the source layer that produced it — so the bank-register "For review" queue
arrives pre-triaged: the high-confidence tail can auto-clear and only the
genuinely ambiguous transactions reach a person.

Two signals feed the suggestion, in strict precedence:

* **Rules** (`Rule`, `RuleSet`) — hand-authored matchers over description regex,
  counterparty, and amount sign. A rule hit is ground truth: it wins at
  confidence 1.0.
* **Learned history** (`LearnedModel`) — per-counterparty and per-normalized-
  description category frequencies mined from previously categorized
  transactions. When no rule matches, the most-likely category is suggested with
  its empirical confidence (`winning_count / total_count`), preferring the
  counterparty signal over the free-text description.

With neither signal the transaction is `"Uncategorized"` at 0.0 — the explicit
"a human must look" marker. `Categorizer.suggest` / `categorize_batch` apply the
whole stack. Everything is stdlib-only, frozen, and fully deterministic (no
randomness; frequency ties break by category name), so a test exercises the exact
path production does. Transactions are read through the structural `TxnLike`
Protocol, so this package takes no dependency on any other RGNR8 package.

Wiring into the web register (surfacing suggestions inline, capturing overrides as
new training signal) is a follow-up pass.
"""

from __future__ import annotations

from .learn import (
    LearnedModel,
    normalize_counterparty,
    normalize_description,
)
from .rules import AmountSign, Rule, RuleSet
from .split import (
    CASH_ACCOUNT,
    SUSPENSE_ACCOUNT,
    BookRouter,
    BookRule,
    BookSet,
    Posting,
    RoutedEntry,
    SplitResult,
    split_books,
)
from .suggester import Categorizer, Source, Suggestion
from .txn import (
    CategorizedTxn,
    CategorizedTxnLike,
    Txn,
    TxnLike,
)

__version__ = "0.1.0"

__all__ = [
    # txn shape
    "TxnLike",
    "CategorizedTxnLike",
    "Txn",
    "CategorizedTxn",
    # rules
    "Rule",
    "RuleSet",
    "AmountSign",
    # learned model
    "LearnedModel",
    "normalize_description",
    "normalize_counterparty",
    # suggester
    "Suggestion",
    "Source",
    "Categorizer",
    # split into sets of books
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
