"""Bank feed (Plaid) — pull cleared and pending transactions into the review inbox.

`BankFeedProvider` is the seam. `PlaidBankFeedProvider` builds a `/transactions/
sync` request over the shared HTTP client and parses the response; it is
shape-correct and needs a client_id/secret + a real client to go live.
`FakeBankFeedProvider` returns preset transactions for tests.

`to_inbox_items` maps provider transactions into the review inbox's shape, keyed
by the provider's own id so a re-sync lands each transaction once — a bank line
is a claim that waits for a human, never an auto-posted entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .http import HttpClient


@dataclass(frozen=True)
class BankTransaction:
    external_id: str
    date: str  # YYYY-MM-DD
    amount_minor: int  # positive = money in, negative = money out
    name: str
    pending: bool = False


class BankFeedProvider(Protocol):
    def fetch_transactions(
        self, access_token: str, cursor: str
    ) -> tuple[list[BankTransaction], str]: ...


class PlaidBankFeedProvider:
    """Shape-correct Plaid `/transactions/sync` adapter. Needs Plaid credentials
    and a real HTTP client bound to go live; the request/response mapping is here
    and tested against a recorded response."""

    def __init__(
        self, http: HttpClient, client_id: str, secret: str, *, base_url: str = "https://production.plaid.com"
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._secret = secret
        self._base = base_url.rstrip("/")

    def fetch_transactions(
        self, access_token: str, cursor: str
    ) -> tuple[list[BankTransaction], str]:
        body: dict[str, object] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
            "cursor": cursor,
        }
        resp = self._http.post_json(
            f"{self._base}/transactions/sync", body, {"Content-Type": "application/json"}
        )
        txns: list[BankTransaction] = []
        for raw in _as_list(resp.get("added")):
            # Plaid amounts are positive for money leaving the account; we invert
            # to the ledger convention (positive = money in).
            amt = _to_minor(raw.get("amount"))
            txns.append(
                BankTransaction(
                    external_id=str(raw.get("transaction_id", "")),
                    date=str(raw.get("date", "")),
                    amount_minor=-amt,
                    name=str(raw.get("name", "")),
                    pending=bool(raw.get("pending", False)),
                )
            )
        next_cursor = str(resp.get("next_cursor", cursor))
        return txns, next_cursor


class FakeBankFeedProvider:
    def __init__(self, transactions: list[BankTransaction], next_cursor: str = "END") -> None:
        self._transactions = transactions
        self._next = next_cursor

    def fetch_transactions(
        self, access_token: str, cursor: str
    ) -> tuple[list[BankTransaction], str]:
        return list(self._transactions), self._next


def to_inbox_items(txns: list[BankTransaction], *, account_code: str) -> list[dict[str, object]]:
    """Map to the review-inbox feed shape, idempotent by the provider's own id."""
    return [
        {
            "external_id": t.external_id,
            "date": t.date,
            "amount_minor": str(t.amount_minor),
            "description": t.name,
            "account_code": account_code,
            "pending": t.pending,
        }
        for t in txns
    ]


def _as_list(v: object) -> list[dict[str, object]]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _to_minor(v: object) -> int:
    """Plaid sends dollars as a number; convert to integer cents without float drift."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v * 100
    if isinstance(v, float):
        return round(v * 100)
    if isinstance(v, str):
        try:
            whole, _, frac = v.partition(".")
            frac = (frac + "00")[:2]
            return int(whole) * 100 + (int(frac) if frac else 0)
        except ValueError:
            return 0
    return 0


__all__ = [
    "BankTransaction",
    "BankFeedProvider",
    "PlaidBankFeedProvider",
    "FakeBankFeedProvider",
    "to_inbox_items",
]
