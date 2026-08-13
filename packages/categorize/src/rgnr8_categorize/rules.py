"""Deterministic categorization rules — the hand-authored layer.

A `Rule` maps a transaction to a category when *every* predicate it declares
matches: an optional case-insensitive description regex (`re.search`, so it
matches anywhere in the string), an optional exact counterparty, and an optional
`amount_sign` constraint (`"in"` for an inflow, `"out"` for an outflow). Omitted
predicates are wildcards — a rule with only a `counterparty` matches on
counterparty alone. Because a rule must satisfy all of its predicates, a rule can
be as tight ("counterparty GUSTO *and* an outflow") or as loose as needed.

A `RuleSet` is an ordered list of rules; `first_match` returns the first rule that
matches, so precedence is explicit and deterministic — earlier rules win. Rules
are the authoritative layer: the categorizer treats a rule hit as certain
(confidence 1.0), overriding any learned signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from .txn import TxnLike

AmountSign = Literal["in", "out"]


@dataclass(frozen=True)
class Rule:
    """One category rule. All declared predicates must match for a hit.

    `description_regex` is matched case-insensitively with `re.search` (anywhere
    in the description). `counterparty`, when set, must equal the transaction's
    counterparty exactly. `amount_sign` constrains the flow direction: `"in"`
    requires `amount_minor > 0`, `"out"` requires `amount_minor < 0`. A `None`
    predicate is a wildcard.
    """

    name: str
    category: str
    description_regex: Optional[str] = None
    counterparty: Optional[str] = None
    amount_sign: Optional[AmountSign] = None

    def matches(self, txn: TxnLike) -> bool:
        """True iff every predicate this rule declares matches `txn`."""
        if self.description_regex is not None:
            if re.search(self.description_regex, txn.description, re.IGNORECASE) is None:
                return False
        if self.counterparty is not None:
            if txn.counterparty != self.counterparty:
                return False
        if self.amount_sign is not None:
            if self.amount_sign == "in" and txn.amount_minor <= 0:
                return False
            if self.amount_sign == "out" and txn.amount_minor >= 0:
                return False
        return True


@dataclass(frozen=True)
class RuleSet:
    """An ordered collection of rules; earlier rules take precedence."""

    rules: tuple[Rule, ...]

    def __init__(self, rules: Iterable[Rule]) -> None:
        # Accept any iterable of rules and freeze it to a tuple so the set is
        # itself immutable and its ordering — hence precedence — is stable.
        object.__setattr__(self, "rules", tuple(rules))

    def first_match(self, txn: TxnLike) -> Optional[Rule]:
        """Return the first rule that matches `txn`, or `None` if none do."""
        for rule in self.rules:
            if rule.matches(txn):
                return rule
        return None
