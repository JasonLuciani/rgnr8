"""Turning a read receipt into a bill the books can accept.

The extractor says what the document *is*; this says what to *do* with it — a
bill draft the web app posts through the ordinary AP path. A low-confidence read,
or one missing a total or a date, comes back flagged `needs_review` rather than
posted silently: a captured receipt is a claim, not an entry, exactly like a bank
line waiting in the inbox.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ExtractedReceipt


@dataclass(frozen=True)
class BillDraft:
    vendor: str
    date: str
    currency: str
    total_minor: int
    tax_minor: int
    expense_account_code: str
    needs_review: bool
    review_reason: str
    confidence: int

    def to_dict(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "date": self.date,
            "currency": self.currency,
            "total_minor": str(self.total_minor),
            "tax_minor": str(self.tax_minor),
            "expense_account_code": self.expense_account_code,
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
            "confidence": self.confidence,
        }


def to_bill_draft(
    receipt: ExtractedReceipt,
    *,
    default_expense_code: str = "6400",
    confidence_threshold: int = 700,
) -> BillDraft:
    """Map an extraction to a bill draft, deciding whether a human must see it
    first. The expense account is a default the reviewer can change; a real
    deployment can pass a learned category from `rgnr8-categorize` instead."""

    reasons: list[str] = []
    if receipt.confidence < confidence_threshold:
        reasons.append("low OCR confidence")
    if receipt.total_minor <= 0:
        reasons.append("no total found")
    if not receipt.date:
        reasons.append("no date found")
    if not receipt.vendor:
        reasons.append("no vendor found")

    return BillDraft(
        vendor=receipt.vendor,
        date=receipt.date,
        currency=receipt.currency,
        total_minor=receipt.total_minor,
        tax_minor=receipt.tax_minor,
        expense_account_code=default_expense_code,
        needs_review=bool(reasons),
        review_reason="; ".join(reasons),
        confidence=receipt.confidence,
    )


__all__ = ["BillDraft", "to_bill_draft"]
