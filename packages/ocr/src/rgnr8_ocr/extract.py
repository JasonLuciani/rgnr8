"""Reading a receipt.

Two seams, one contract. `ReceiptExtractor` turns document *text* into an
`ExtractedReceipt`. `HeuristicExtractor` is the stdlib default: regexes for the
vendor, date, total, and tax that handle the text of a typed receipt, an emailed
invoice, or the output of any OCR engine — no network, fully testable.

`OcrClient` is the image seam: `image_to_text(bytes, content_type) -> str`. A
real vision provider (Google Vision, AWS Textract, Azure) plugs in here.
`OcrReceiptExtractor` composes an `OcrClient` with a text extractor so an image
becomes a coded draft: bytes → text → fields. It is shape-correct with no
network; binding a real client turns it live.
"""

from __future__ import annotations

import re
from typing import Protocol

from .model import ExtractedReceipt, LineItem

_AMOUNT = r"[-+]?\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_DATE_PATTERNS = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), ("m", "d", "y")),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b"), ("m", "d", "yy")),
)


def parse_amount_to_minor(text: str) -> int | None:
    """'$1,234.56' → 123456. Two decimal places assumed; a bare integer is dollars."""
    cleaned = text.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned:
        return None
    neg = cleaned.startswith("-")
    cleaned = cleaned.lstrip("+-")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", cleaned):
        return None
    if "." in cleaned:
        whole, frac = cleaned.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = cleaned, "00"
    minor = int(whole) * 100 + int(frac)
    return -minor if neg else minor


def _find_date(text: str) -> str:
    for pattern, order in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        parts = dict(zip(order, m.groups()))
        year = parts.get("y") or ("20" + parts["yy"] if "yy" in parts else "")
        if not year:
            continue
        return f"{int(year):04d}-{int(parts['m']):02d}-{int(parts['d']):02d}"
    return ""


def _labeled_amount(text: str, labels: tuple[str, ...]) -> int | None:
    """The amount on the last line whose label matches (receipts print the total last)."""
    found: int | None = None
    for line in text.splitlines():
        low = line.lower()
        if any(lbl in low for lbl in labels):
            matches = re.findall(_AMOUNT, line)
            if matches:
                minor = parse_amount_to_minor(matches[-1])
                if minor is not None:
                    found = minor
    return found


class ReceiptExtractor(Protocol):
    def extract(self, text: str) -> ExtractedReceipt: ...


class HeuristicExtractor:
    """A stdlib reader: vendor from the first non-empty line, date by pattern,
    total and tax by label. Confidence reflects how much it actually found, so a
    poor scan is flagged for review rather than posted as a guess."""

    def extract(self, text: str) -> ExtractedReceipt:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        vendor = lines[0] if lines else ""
        date = _find_date(text)
        total = _labeled_amount(text, ("total", "amount due", "balance due", "grand total"))
        tax = _labeled_amount(text, ("tax", "vat", "gst", "hst"))
        subtotal = _labeled_amount(text, ("subtotal", "sub total", "sub-total"))
        if subtotal is None and total is not None and tax is not None:
            subtotal = total - tax

        found = sum(x is not None and x != "" for x in (vendor, date, total))
        confidence = {0: 0, 1: 300, 2: 650, 3: 900}[found]
        if tax is not None:
            confidence = min(1000, confidence + 50)

        return ExtractedReceipt(
            vendor=vendor,
            date=date,
            subtotal_minor=subtotal or 0,
            tax_minor=tax or 0,
            total_minor=total or 0,
            confidence=confidence,
            raw_text=text,
        )


class OcrClient(Protocol):
    def image_to_text(self, image: bytes, content_type: str) -> str: ...


class OcrReceiptExtractor:
    """Image → text (via an OCR provider) → fields (via a text extractor).

    The `OcrClient` is the only thing standing between this and a live pipeline.
    Bind a Google Vision / Textract client and receipts photographed from a phone
    become drafted, coded bills.
    """

    def __init__(self, client: OcrClient, text_extractor: ReceiptExtractor | None = None) -> None:
        self._client = client
        self._text = text_extractor or HeuristicExtractor()

    def extract_image(self, image: bytes, content_type: str) -> ExtractedReceipt:
        text = self._client.image_to_text(image, content_type)
        return self._text.extract(text)


class StubOcrClient:
    """A test/dev OCR client that returns pre-set text for given image bytes —
    stands in for a real vision provider so the whole pipeline is exercisable."""

    def __init__(self, mapping: dict[bytes, str] | None = None) -> None:
        self._mapping = mapping or {}

    def register(self, image: bytes, text: str) -> None:
        self._mapping[image] = text

    def image_to_text(self, image: bytes, content_type: str) -> str:
        if image not in self._mapping:
            raise KeyError("no stub text registered for this image")
        return self._mapping[image]


__all__ = [
    "ReceiptExtractor",
    "HeuristicExtractor",
    "OcrClient",
    "OcrReceiptExtractor",
    "StubOcrClient",
    "LineItem",
    "parse_amount_to_minor",
]
