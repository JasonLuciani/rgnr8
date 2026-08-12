"""Enumerations shared across the forecast engine."""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    INFLOW = "INFLOW"
    OUTFLOW = "OUTFLOW"


class Confidence(str, Enum):
    """Confidence tier of a projected flow, in the blueprint's forecast hierarchy.

    RECORDED   — a known commitment with a date and amount (payroll, debt, a bill).
    PREDICTED  — a behavioral estimate (customer pays N days after due).
    PLANNED    — an explicit owner plan (a hire, a purchase, a distribution).
    SCENARIO   — exists only inside a stress/upside scenario (pipeline, reserves).
    """

    RECORDED = "RECORDED"
    PREDICTED = "PREDICTED"
    PLANNED = "PLANNED"
    SCENARIO = "SCENARIO"


# Relative strength for rolling confidence up to a bucket/forecast level.
CONFIDENCE_WEIGHT: dict[Confidence, int] = {
    Confidence.RECORDED: 100,
    Confidence.PREDICTED: 70,
    Confidence.PLANNED: 55,
    Confidence.SCENARIO: 30,
}


class Category(str, Enum):
    CUSTOMER_RECEIPT = "CUSTOMER_RECEIPT"
    PIPELINE_RECEIPT = "PIPELINE_RECEIPT"
    OTHER_INFLOW = "OTHER_INFLOW"
    VENDOR_PAYMENT = "VENDOR_PAYMENT"
    PAYROLL_NET = "PAYROLL_NET"
    PAYROLL_TAX = "PAYROLL_TAX"
    TAX_REMITTANCE = "TAX_REMITTANCE"
    DEBT_SERVICE = "DEBT_SERVICE"
    RENT = "RENT"
    OWNER_DISTRIBUTION = "OWNER_DISTRIBUTION"
    CAPITAL_EXPENDITURE = "CAPITAL_EXPENDITURE"
    REFUND_CHARGEBACK = "REFUND_CHARGEBACK"
    OTHER_OUTFLOW = "OTHER_OUTFLOW"
    TRANSFER = "TRANSFER"  # internal account movement — excluded from net cash


class Scenario(str, Enum):
    BASE = "BASE"
    DOWNSIDE = "DOWNSIDE"
    UPSIDE = "UPSIDE"


class PublicationStatus(str, Enum):
    PRELIMINARY = "PRELIMINARY"
    VERIFIED = "VERIFIED"
    PUBLISHED = "PUBLISHED"


class InvoiceStatus(str, Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    DISPUTED = "DISPUTED"
