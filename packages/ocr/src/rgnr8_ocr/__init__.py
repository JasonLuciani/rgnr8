"""RGNR8 receipt & document capture — photograph a receipt, get a coded draft.

A provider-agnostic OCR seam (`OcrClient`) plus a stdlib text reader
(`HeuristicExtractor`) that needs no network, and a mapping to a review-gated
bill draft. Wiring a real vision provider into the `OcrClient` turns the image
path live; the text path (typed receipts, emailed invoices) works today.
"""

from __future__ import annotations

from .draft import BillDraft, to_bill_draft
from .extract import (
    HeuristicExtractor,
    OcrClient,
    OcrReceiptExtractor,
    ReceiptExtractor,
    StubOcrClient,
    parse_amount_to_minor,
)
from .model import ExtractedReceipt, LineItem

__all__ = [
    "ExtractedReceipt",
    "LineItem",
    "ReceiptExtractor",
    "HeuristicExtractor",
    "OcrClient",
    "OcrReceiptExtractor",
    "StubOcrClient",
    "parse_amount_to_minor",
    "BillDraft",
    "to_bill_draft",
]
