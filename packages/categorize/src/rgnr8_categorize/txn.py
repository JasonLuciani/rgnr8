"""The minimal transaction shape this package reasons about.

Auto-categorization only needs a handful of fields off a bank transaction, so
rather than take a cross-package dependency (e.g. on `rgnr8-forecast` for a full
`Money`), this package defines its own tiny structural contract. `TxnLike` is a
`Protocol`: any object that carries an `id`, a free-text `description`, a
`counterparty`, an integer `amount_minor` (minor units — cents — signed so an
inflow is positive and an outflow negative), and an ISO `currency` code satisfies
it. Callers pass whatever transaction record they already have; nothing here
imports another RGNR8 package, so `rgnr8-categorize` has no cross-deps.

`Txn` is a frozen concrete implementation of that Protocol, handy for tests and
for callers that want a ready-made record. `CategorizedTxn` extends the shape with
the human-assigned `category` that the learned model trains on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class TxnLike(Protocol):
    """Structural shape of a bank transaction the categorizer can read.

    `amount_minor` is signed integer minor units: positive is money in (an
    inflow / credit), negative is money out (an outflow / debit).
    """

    @property
    def id(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def counterparty(self) -> str: ...

    @property
    def amount_minor(self) -> int: ...

    @property
    def currency(self) -> str: ...


@runtime_checkable
class CategorizedTxnLike(TxnLike, Protocol):
    """A `TxnLike` that also carries the human-assigned `category` used as
    training signal by the learned model."""

    @property
    def category(self) -> str: ...


@dataclass(frozen=True)
class Txn:
    """A concrete, immutable `TxnLike` — useful for callers and tests."""

    id: str
    description: str
    counterparty: str
    amount_minor: int
    currency: str = "USD"


@dataclass(frozen=True)
class CategorizedTxn:
    """A concrete, immutable `CategorizedTxnLike` — a prior transaction whose
    `category` a bookkeeper has already confirmed. This is the learned model's
    training record."""

    id: str
    description: str
    counterparty: str
    amount_minor: int
    category: str
    currency: str = "USD"
