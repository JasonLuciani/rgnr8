"""Receipt capture: read the text, decide whether a human must see it."""

from __future__ import annotations

from rgnr8_ocr import (
    HeuristicExtractor,
    OcrReceiptExtractor,
    StubOcrClient,
    parse_amount_to_minor,
    to_bill_draft,
)

RECEIPT = """HOME DEPOT #4512
1450 Industrial Blvd
Date: 03/14/2026

3/4in Plywood      2 @ 48.00     96.00
Wood screws        1 @ 12.50     12.50
Subtotal                        108.50
Tax                               8.68
TOTAL                           117.18

VISA ****1234
"""


def test_amount_parsing() -> None:
    assert parse_amount_to_minor("$1,234.56") == 123456
    assert parse_amount_to_minor("117.18") == 11718
    assert parse_amount_to_minor("48") == 4800
    assert parse_amount_to_minor("48.5") == 4850
    assert parse_amount_to_minor("nonsense") is None


def test_heuristic_reads_vendor_date_total_and_tax() -> None:
    r = HeuristicExtractor().extract(RECEIPT)
    assert r.vendor == "HOME DEPOT #4512"
    assert r.date == "2026-03-14"
    assert r.total_minor == 11718
    assert r.tax_minor == 868
    assert r.subtotal_minor == 10850
    assert r.is_confident()  # vendor + date + total + tax → high confidence


def test_ocr_pipeline_reads_an_image_through_the_seam() -> None:
    image = b"\x89PNG-fake-bytes"
    client = StubOcrClient()
    client.register(image, RECEIPT)
    extractor = OcrReceiptExtractor(client)
    r = extractor.extract_image(image, "image/png")
    assert r.vendor == "HOME DEPOT #4512"
    assert r.total_minor == 11718


def test_a_clean_read_becomes_a_postable_draft() -> None:
    r = HeuristicExtractor().extract(RECEIPT)
    draft = to_bill_draft(r)
    assert draft.needs_review is False
    assert draft.total_minor == 11718
    assert draft.tax_minor == 868
    d = draft.to_dict()
    assert d["total_minor"] == "11718"


def test_a_poor_read_is_flagged_for_review_not_posted() -> None:
    r = HeuristicExtractor().extract("blurry\nunreadable smudge")
    draft = to_bill_draft(r)
    assert draft.needs_review is True
    assert "no total found" in draft.review_reason
    assert "no date found" in draft.review_reason


def test_a_missing_total_always_needs_review_even_if_confident() -> None:
    # vendor + date present but no total → must not post silently
    text = "ACME SUPPLY\nDate: 2026-01-05\nThanks for your business"
    draft = to_bill_draft(HeuristicExtractor().extract(text))
    assert draft.needs_review is True
    assert "no total found" in draft.review_reason
