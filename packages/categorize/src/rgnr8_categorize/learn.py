"""Learned suggester — categorize from what the bookkeeper did before.

Rules cover the known vendors; the long tail is learned. `LearnedModel` is built
from a history of already-categorized transactions and, for each *key*, records how
often each category was chosen. Two kinds of key are tracked in parallel:

* a **counterparty** key (normalized counterparty string), and
* a **description** key (the description normalized down to its stable core).

The description normalizer lowercases and strips digits, punctuation and the
trailing reference numbers card networks tack on, so `"SQ *COFFEE 1234"` and
`"SQ *COFFEE 9987"` collapse to the same key `"sq coffee"` and reinforce one
signal instead of splintering into two.

`suggest` looks up both keys and prefers the counterparty key when it exists (a
named vendor is a stronger signal than free-text memo), falling back to the
description key. Confidence is the empirical share of the winning category for that
key — `winning_count / total_count` — so eight-of-eight reads as 1.0 and
six-of-ten as 0.6. Ties between equally frequent categories are broken by
category name (ascending) so the output is fully deterministic; there is no
randomness anywhere.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from .txn import CategorizedTxnLike, TxnLike

# One or more non-letter characters — used to strip digits, punctuation, the
# "SQ *" prefix noise and trailing reference numbers down to a whitespace gap.
_NON_ALPHA = re.compile(r"[^a-z]+")


def normalize_description(description: str) -> str:
    """Collapse a raw description to its stable categorization key.

    Lowercases, replaces every run of non-letter characters (digits,
    punctuation, `*`, trailing ref numbers) with a single space, and trims. So
    `"SQ *COFFEE 1234"` and `"SQ *COFFEE 9987"` both become `"sq coffee"`.
    """
    return _NON_ALPHA.sub(" ", description.lower()).strip()


def normalize_counterparty(counterparty: str) -> str:
    """Lowercase + trim + collapse internal whitespace of a counterparty, so
    `"Gusto"` and `"gusto "` share a key. Empty/blank counterparties yield `""`,
    which the model treats as 'no counterparty key'."""
    return " ".join(counterparty.lower().split())


def _pick(counts: Counter[str]) -> tuple[str, int, int]:
    """Return `(category, winning_count, total_count)` for a key's tally.

    The winner is the most frequent category; ties are broken by category name
    ascending, making the choice deterministic regardless of insertion order.
    """
    total = sum(counts.values())
    # Sort by descending count, then ascending category name for a stable tie-break.
    category, winning = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return category, winning, total


@dataclass(frozen=True)
class LearnedModel:
    """Frequency tables learned from categorized history.

    `by_counterparty` and `by_description` map a normalized key to a `Counter`
    of category → occurrences. Frozen; build with `from_history`.
    """

    by_counterparty: dict[str, Counter[str]]
    by_description: dict[str, Counter[str]]

    @classmethod
    def from_history(cls, history: Iterable[CategorizedTxnLike]) -> "LearnedModel":
        """Build a model from prior categorized transactions."""
        by_counterparty: dict[str, Counter[str]] = {}
        by_description: dict[str, Counter[str]] = {}
        for txn in history:
            cp_key = normalize_counterparty(txn.counterparty)
            if cp_key:
                by_counterparty.setdefault(cp_key, Counter())[txn.category] += 1
            desc_key = normalize_description(txn.description)
            if desc_key:
                by_description.setdefault(desc_key, Counter())[txn.category] += 1
        return cls(by_counterparty=by_counterparty, by_description=by_description)

    def suggest(self, txn: TxnLike) -> Optional[tuple[str, float, str]]:
        """Suggest `(category, confidence, reason)` for `txn`, or `None`.

        Prefers the counterparty key when history exists for it; otherwise uses
        the normalized description key. Confidence is the winning category's
        share of that key's total. Returns `None` when neither key is known.
        """
        cp_key = normalize_counterparty(txn.counterparty)
        if cp_key and cp_key in self.by_counterparty:
            category, winning, total = _pick(self.by_counterparty[cp_key])
            reason = (
                f"counterparty '{cp_key}' seen {total}x in history, "
                f"{winning} as {category}"
            )
            return category, winning / total, reason

        desc_key = normalize_description(txn.description)
        if desc_key and desc_key in self.by_description:
            category, winning, total = _pick(self.by_description[desc_key])
            reason = (
                f"description '{desc_key}' seen {total}x in history, "
                f"{winning} as {category}"
            )
            return category, winning / total, reason

        return None
