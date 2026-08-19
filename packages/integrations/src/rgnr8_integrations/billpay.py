"""Bill pay — send an ACH disbursement for an approved bill and post the payment.

`DisbursementProvider` sends a payment for a bill and returns a `Disbursement`
with the provider's reference and status. `AchDisbursementProvider` is the
shape-correct adapter (needs credentials + a real client). `to_bill_payment` maps
a settled disbursement into the AP payment path, idempotent by the bill id, so a
payment posts once no matter how many status callbacks arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .http import HttpClient


@dataclass(frozen=True)
class Disbursement:
    external_id: str
    bill_ref: str
    amount_minor: int
    status: str  # e.g. "submitted", "settled", "returned"
    date: str


class DisbursementProvider(Protocol):
    def send_payment(
        self, bill_ref: str, amount_minor: int, funding_account: str, date: str
    ) -> Disbursement: ...


class AchDisbursementProvider:
    """Shape-correct ACH disbursement adapter. Needs credentials + a real client.

    NOTE: initiating a real transfer of funds is a permissioned, human-approved
    action; this adapter only builds the request. The web layer gates who may
    approve a disbursement — the provider never moves money on its own.
    """

    def __init__(
        self, http: HttpClient, api_key: str, *, base_url: str = "https://api.billpay.example"
    ) -> None:
        self._http = http
        self._key = api_key
        self._base = base_url.rstrip("/")

    def send_payment(
        self, bill_ref: str, amount_minor: int, funding_account: str, date: str
    ) -> Disbursement:
        body: dict[str, object] = {
            "reference": bill_ref,
            "amount": amount_minor,
            "funding_account": funding_account,
            "date": date,
        }
        resp = self._http.post_json(
            f"{self._base}/v1/disbursements", body, {"Authorization": f"Bearer {self._key}"}
        )
        return Disbursement(
            external_id=str(resp.get("id", "")),
            bill_ref=bill_ref,
            amount_minor=amount_minor,
            status=str(resp.get("status", "submitted")),
            date=date,
        )


class FakeDisbursementProvider:
    def __init__(self, status: str = "submitted") -> None:
        self._status = status
        self.sent: list[tuple[str, int, str, str]] = []
        self._seq = 0

    def send_payment(
        self, bill_ref: str, amount_minor: int, funding_account: str, date: str
    ) -> Disbursement:
        self._seq += 1
        self.sent.append((bill_ref, amount_minor, funding_account, date))
        return Disbursement(
            external_id=f"disb-{self._seq}",
            bill_ref=bill_ref,
            amount_minor=amount_minor,
            status=self._status,
            date=date,
        )


def to_bill_payment(disb: Disbursement, *, paid_from_code: str = "1000") -> dict[str, object]:
    """Map a settled disbursement to the AP payment path, idempotent by bill id."""
    return {
        "idempotency_key": f"billpay:{disb.bill_ref}",
        "bill_id": disb.bill_ref,
        "date": disb.date,
        "amount_minor": str(disb.amount_minor),
        "paid_from_code": paid_from_code,
        "reference": disb.external_id,
    }


__all__ = [
    "Disbursement",
    "DisbursementProvider",
    "AchDisbursementProvider",
    "FakeDisbursementProvider",
    "to_bill_payment",
]
