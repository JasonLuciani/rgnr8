"""What a captured receipt or bill becomes once it is read.

Everything is integer minor units and immutable. `confidence` is a 0–1000
per-mille score (no floats in the money path, and none needed here either) so a
low-confidence extraction can be routed to human review instead of posted blind.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity_milli: int  # thousandths, so 1.5 units is 1500
    unit_amount_minor: int
    amount_minor: int


@dataclass(frozen=True)
class ExtractedReceipt:
    """The structured read of one document. Fields are best-effort; a reader
    that could not find a field leaves it at its empty default and lowers the
    confidence, rather than inventing a value."""

    vendor: str = ""
    date: str = ""  # YYYY-MM-DD, empty if not found
    currency: str = "USD"
    subtotal_minor: int = 0
    tax_minor: int = 0
    total_minor: int = 0
    line_items: tuple[LineItem, ...] = field(default_factory=tuple)
    confidence: int = 0  # 0–1000 per-mille
    raw_text: str = ""

    def is_confident(self, threshold: int = 700) -> bool:
        return self.confidence >= threshold
