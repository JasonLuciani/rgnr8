"""(De)serialize ForecastInputs to/from a JSON-friendly contract.

This is the boundary between the TypeScript app services (ingestion,
reconciliation) that assemble a business's activity and the Python forecast
engine that consumes it. The same contract is emitted by
``@rgnr8/forecast-inputs`` on the TS side.

Money is serialized as ``{"minor": <int>, "currency": <str>}`` — integer minor
units, never a float. Dates are ISO ``YYYY-MM-DD``. Provenance is not carried in
the contract (it lives with the ledger); loaded records have ``provenance=None``.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from .dates import Frequency, Recurrence
from .enums import Category, Confidence, Direction, InvoiceStatus
from .models import (
    Bill,
    CashPosition,
    CustomerHistory,
    DebtInstrument,
    ForecastInputs,
    Invoice,
    OneTimeItem,
    PaymentObservation,
    PayrollSchedule,
    PipelineOpportunity,
    RecurringItem,
)
from .money import Money

CONTRACT_VERSION = "forecast-inputs/1"


# --- money -------------------------------------------------------------------
def money_to_dto(m: Money) -> dict[str, Any]:
    return {"minor": m.minor_units, "currency": m.currency}


def money_from_dto(d: dict[str, Any]) -> Money:
    return Money(int(d["minor"]), str(d.get("currency", "USD")))


# --- recurrence --------------------------------------------------------------
def recurrence_to_dto(r: Recurrence) -> dict[str, Any]:
    return {
        "frequency": r.frequency.value,
        "anchor": r.anchor.isoformat(),
        "interval": r.interval,
        "second_day": r.second_day,
        "end": r.end.isoformat() if r.end else None,
        "count": r.count,
    }


def recurrence_from_dto(d: dict[str, Any]) -> Recurrence:
    return Recurrence(
        frequency=Frequency(d["frequency"]),
        anchor=date.fromisoformat(d["anchor"]),
        interval=int(d.get("interval", 1)),
        second_day=d.get("second_day"),
        end=date.fromisoformat(d["end"]) if d.get("end") else None,
        count=d.get("count"),
    )


# --- top level ---------------------------------------------------------------
def to_dto(inputs: ForecastInputs) -> dict[str, Any]:
    o = inputs.opening
    return {
        "contract": CONTRACT_VERSION,
        "currency": inputs.currency,
        "opening": {
            "as_of": o.as_of.isoformat(),
            "available": money_to_dto(o.available),
            "restricted": money_to_dto(o.restricted),
            "verified": o.verified,
        },
        "invoices": [
            {
                "id": i.id,
                "customer_id": i.customer_id,
                "issue_date": i.issue_date.isoformat(),
                "due_date": i.due_date.isoformat(),
                "open_amount": money_to_dto(i.open_amount),
                "status": i.status.value,
            }
            for i in inputs.invoices
        ],
        "customer_histories": [
            {
                "customer_id": h.customer_id,
                "observations": [
                    {"due_date": o2.due_date.isoformat(), "paid_date": o2.paid_date.isoformat()}
                    for o2 in h.observations
                ],
                "override_days_late": h.override_days_late,
            }
            for h in inputs.customer_histories
        ],
        "bills": [
            {
                "id": b.id,
                "vendor_id": b.vendor_id,
                "due_date": b.due_date.isoformat(),
                "amount": money_to_dto(b.amount),
                "scheduled_date": b.scheduled_date.isoformat() if b.scheduled_date else None,
            }
            for b in inputs.bills
        ],
        "recurring": [
            {
                "label": r.label,
                "category": r.category.value,
                "direction": r.direction.value,
                "amount": money_to_dto(r.amount),
                "recurrence": recurrence_to_dto(r.recurrence),
            }
            for r in inputs.recurring
        ],
        "payroll": [
            {
                "label": p.label,
                "recurrence": recurrence_to_dto(p.recurrence),
                "net_pay": money_to_dto(p.net_pay),
                "payroll_taxes": money_to_dto(p.payroll_taxes),
                "tax_remit_lag_days": p.tax_remit_lag_days,
            }
            for p in inputs.payroll
        ],
        "debt": [
            {
                "label": d.label,
                "recurrence": recurrence_to_dto(d.recurrence),
                "payment": money_to_dto(d.payment),
                "principal_portion": money_to_dto(d.principal_portion) if d.principal_portion else None,
                "interest_portion": money_to_dto(d.interest_portion) if d.interest_portion else None,
            }
            for d in inputs.debt
        ],
        "one_time": [
            {
                "label": t.label,
                "category": t.category.value,
                "direction": t.direction.value,
                "amount": money_to_dto(t.amount),
                "on_date": t.on_date.isoformat(),
                "confidence": t.confidence.value,
            }
            for t in inputs.one_time
        ],
        "pipeline": [
            {
                "label": p.label,
                "customer_id": p.customer_id,
                "expected_date": p.expected_date.isoformat(),
                "amount": money_to_dto(p.amount),
                "probability_bps": p.probability_bps,
            }
            for p in inputs.pipeline
        ],
    }


def from_dto(d: dict[str, Any]) -> ForecastInputs:
    o = d["opening"]
    opening = CashPosition(
        as_of=date.fromisoformat(o["as_of"]),
        available=money_from_dto(o["available"]),
        restricted=money_from_dto(o["restricted"]) if o.get("restricted") else Money(0),
        # Fail CLOSED: a payload that doesn't explicitly assert reconciliation is
        # treated as unverified, never silently presented as "verified cash".
        verified=bool(o.get("verified", False)),
    )
    return ForecastInputs(
        opening=opening,
        currency=str(d.get("currency", "USD")),
        invoices=tuple(
            Invoice(
                id=i["id"],
                customer_id=i["customer_id"],
                issue_date=date.fromisoformat(i["issue_date"]),
                due_date=date.fromisoformat(i["due_date"]),
                open_amount=money_from_dto(i["open_amount"]),
                status=InvoiceStatus(i.get("status", "OPEN")),
            )
            for i in d.get("invoices", [])
        ),
        customer_histories=tuple(
            CustomerHistory(
                customer_id=h["customer_id"],
                observations=tuple(
                    PaymentObservation(
                        due_date=date.fromisoformat(ob["due_date"]),
                        paid_date=date.fromisoformat(ob["paid_date"]),
                    )
                    for ob in h.get("observations", [])
                ),
                override_days_late=h.get("override_days_late"),
            )
            for h in d.get("customer_histories", [])
        ),
        bills=tuple(
            Bill(
                id=b["id"],
                vendor_id=b["vendor_id"],
                due_date=date.fromisoformat(b["due_date"]),
                amount=money_from_dto(b["amount"]),
                scheduled_date=date.fromisoformat(b["scheduled_date"]) if b.get("scheduled_date") else None,
            )
            for b in d.get("bills", [])
        ),
        recurring=tuple(
            RecurringItem(
                label=r["label"],
                category=Category(r["category"]),
                direction=Direction(r["direction"]),
                amount=money_from_dto(r["amount"]),
                recurrence=recurrence_from_dto(r["recurrence"]),
            )
            for r in d.get("recurring", [])
        ),
        payroll=tuple(
            PayrollSchedule(
                label=p["label"],
                recurrence=recurrence_from_dto(p["recurrence"]),
                net_pay=money_from_dto(p["net_pay"]),
                payroll_taxes=money_from_dto(p["payroll_taxes"]) if p.get("payroll_taxes") else Money(0),
                tax_remit_lag_days=int(p.get("tax_remit_lag_days", 3)),
            )
            for p in d.get("payroll", [])
        ),
        debt=tuple(
            DebtInstrument(
                label=x["label"],
                recurrence=recurrence_from_dto(x["recurrence"]),
                payment=money_from_dto(x["payment"]),
                principal_portion=money_from_dto(x["principal_portion"]) if x.get("principal_portion") else None,
                interest_portion=money_from_dto(x["interest_portion"]) if x.get("interest_portion") else None,
            )
            for x in d.get("debt", [])
        ),
        one_time=tuple(
            OneTimeItem(
                label=t["label"],
                category=Category(t["category"]),
                direction=Direction(t["direction"]),
                amount=money_from_dto(t["amount"]),
                on_date=date.fromisoformat(t["on_date"]),
                confidence=Confidence(t.get("confidence", "PLANNED")),
            )
            for t in d.get("one_time", [])
        ),
        pipeline=tuple(
            PipelineOpportunity(
                label=p["label"],
                customer_id=p["customer_id"],
                expected_date=date.fromisoformat(p["expected_date"]),
                amount=money_from_dto(p["amount"]),
                probability_bps=int(p["probability_bps"]),
            )
            for p in d.get("pipeline", [])
        ),
    )


def dumps(inputs: ForecastInputs, *, indent: int | None = None) -> str:
    return json.dumps(to_dto(inputs), indent=indent, sort_keys=True)


def loads(text: str) -> ForecastInputs:
    return from_dto(json.loads(text))
