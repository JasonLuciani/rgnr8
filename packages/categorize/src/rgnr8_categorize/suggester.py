"""The categorizer — combine rules and learned history into one suggestion.

`Categorizer` layers the two signals with a fixed precedence: a matching `Rule`
wins outright at confidence 1.0 (rules are hand-authored ground truth), tagged
`source="rule"`. With no rule hit, the `LearnedModel`'s empirical suggestion is
used as-is, tagged `source="learned"`, carrying its own history-derived
confidence. With neither, the transaction falls through to a `Suggestion` of
`"Uncategorized"` at confidence 0.0, `source="none"` — the explicit signal that a
human still has to look at it.

Every path returns a `Suggestion` (never `None`), so downstream — the bank
register's "For review" queue — can sort by confidence and auto-clear the
high-confidence tail while routing the rest to a bookkeeper. `categorize_batch`
maps the same logic across a list, order-preserving and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from .learn import LearnedModel
from .rules import RuleSet
from .txn import TxnLike

Source = Literal["rule", "learned", "none"]


@dataclass(frozen=True)
class Suggestion:
    """A category suggestion for one transaction.

    `confidence` is in [0.0, 1.0]; `reason` is a human-readable explanation for
    the register UI; `source` records which layer produced it.
    """

    category: str
    confidence: float
    reason: str
    source: Source


@dataclass(frozen=True)
class Categorizer:
    """Rules-over-learning categorizer.

    Construct with a `RuleSet` and a `LearnedModel`; `suggest` applies rule
    precedence then learned fallback then the Uncategorized default.
    """

    ruleset: RuleSet
    model: LearnedModel

    def suggest(self, txn: TxnLike) -> Suggestion:
        """Return the best `Suggestion` for `txn` (always a value, never None)."""
        rule = self.ruleset.first_match(txn)
        if rule is not None:
            return Suggestion(
                category=rule.category,
                confidence=1.0,
                reason=f"matched rule '{rule.name}'",
                source="rule",
            )

        learned = self.model.suggest(txn)
        if learned is not None:
            category, confidence, reason = learned
            return Suggestion(
                category=category,
                confidence=confidence,
                reason=reason,
                source="learned",
            )

        return Suggestion(
            category="Uncategorized",
            confidence=0.0,
            reason="no rule or history match",
            source="none",
        )

    def categorize_batch(self, txns: Iterable[TxnLike]) -> list[Suggestion]:
        """Categorize a batch, preserving input order."""
        return [self.suggest(txn) for txn in txns]
