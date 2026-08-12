"""Resolve inputs + scenario assumptions into a deterministic list of CashFlows.

This is where the blueprint's required treatment policies are applied:

- Transfers / internal account movements are excluded (no double counting).
- Pending vs posted: opening cash is posted; pending items arrive as OneTimeItems.
- Partial invoice payments: only ``open_amount`` is forecast.
- Payroll gross/net/liability timing: net pay on payday, taxes remitted later.
- Debt principal/interest: one cash outflow per payment; split kept as metadata.
- Tax reserves / annual & irregular expenses: recurring or one-time items.
- Refunds/chargebacks: a scenario reserve tied to projected receipts.
- Bad debt / disputed: disputed AR excluded; aged AR written down in stress.
- Restricted / minimum cash: handled by the engine's floor, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .dates import Recurrence
from .enums import Category, Confidence, Direction, InvoiceStatus, Scenario
from .flow import CashFlow
from .models import (
    Bill,
    DebtInstrument,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    OneTimeItem,
    PayrollSchedule,
    PipelineOpportunity,
    RecurringItem,
    ScenarioAssumptions,
)
from .money import Money
from .predict import predict_days_late


@dataclass(slots=True)
class ResolvedFlows:
    flows: list[CashFlow]
    notes: list[str] = field(default_factory=list)


def resolve(
    inputs: ForecastInputs,
    config: ForecastConfig,
    scenario: Scenario,
    assumptions: ScenarioAssumptions,
) -> ResolvedFlows:
    as_of = inputs.opening.as_of
    horizon_end = as_of + timedelta(days=config.horizon_weeks * 7 - 1)
    ccy = inputs.currency
    raw: list[CashFlow] = []
    notes: list[str] = []

    def in_window(d: date) -> bool:
        return as_of <= d <= horizon_end

    def clamp(d: date) -> date:
        return d if d >= as_of else as_of

    # --- Accounts receivable (predicted customer receipts) -------------------
    dropped_ar = 0
    excluded_disputed = 0
    for inv in inputs.invoices:
        if inv.status is InvoiceStatus.DISPUTED:
            excluded_disputed += 1
            continue

        if assumptions.ar_on_time:
            pay_date = clamp(inv.due_date)
            basis = "assumed paid on due date (optimistic)"
            conf = Confidence.PREDICTED
        else:
            pred = predict_days_late(inputs.history_for(inv.customer_id), config)
            pay_date = inv.due_date + timedelta(days=pred.days_late + assumptions.ar_extra_delay_days)
            extra = (
                f" +{assumptions.ar_extra_delay_days}d stress"
                if assumptions.ar_extra_delay_days
                else ""
            )
            clamped = pay_date < as_of
            pay_date = clamp(pay_date)
            basis = pred.basis + extra + (" (clamped to today; overdue)" if clamped else "")
            conf = Confidence.PREDICTED

        if not in_window(pay_date):
            dropped_ar += 1
            continue

        amount = inv.open_amount
        # Bad-debt write-down on aged receivables (stress scenarios).
        if assumptions.bad_debt_age_days is not None and assumptions.bad_debt_bps > 0:
            age = (as_of - inv.due_date).days
            if age > assumptions.bad_debt_age_days:
                kept = amount.scale_by(10000 - assumptions.bad_debt_bps, 10000)
                basis += (
                    f"; {assumptions.bad_debt_bps/100:.0f}% bad-debt write-down "
                    f"({age}d past due)"
                )
                amount = kept

        if amount.is_zero:
            continue

        raw.append(
            CashFlow(
                seq=0,
                on_date=pay_date,
                direction=Direction.INFLOW,
                amount=amount,
                category=Category.CUSTOMER_RECEIPT,
                confidence=conf,
                basis=basis,
                scenario=scenario,
                source=inv.provenance,
                origin_id=inv.id,
            )
        )

        # Refund/chargeback reserve tied to this receipt (scenario only).
        if assumptions.refund_reserve_bps > 0:
            reserve = amount.scale_by(assumptions.refund_reserve_bps, 10000)
            if reserve.is_positive:
                raw.append(
                    CashFlow(
                        seq=0,
                        on_date=pay_date,
                        direction=Direction.OUTFLOW,
                        amount=reserve,
                        category=Category.REFUND_CHARGEBACK,
                        confidence=Confidence.SCENARIO,
                        basis=f"{assumptions.refund_reserve_bps/100:.2f}% refund reserve on receipt {inv.id}",
                        scenario=scenario,
                        source=inv.provenance,
                        origin_id=inv.id,
                    )
                )

    if excluded_disputed:
        notes.append(f"{excluded_disputed} disputed invoice(s) excluded from receipts")
    if dropped_ar:
        notes.append(f"{dropped_ar} predicted receipt(s) fall beyond the {config.horizon_weeks}-week horizon")

    # --- Pipeline (upside only) ----------------------------------------------
    if assumptions.include_pipeline:
        for opp in inputs.pipeline:
            d = clamp(opp.expected_date)
            if not in_window(d):
                continue
            weighted = opp.amount.scale_by(opp.probability_bps, 10000)
            if weighted.is_zero:
                continue
            raw.append(
                CashFlow(
                    seq=0,
                    on_date=d,
                    direction=Direction.INFLOW,
                    amount=weighted,
                    category=Category.PIPELINE_RECEIPT,
                    confidence=Confidence.SCENARIO,
                    basis=f"pipeline {opp.label} at {opp.probability_bps/100:.0f}% probability",
                    scenario=scenario,
                    source=opp.provenance,
                    origin_id=opp.customer_id,
                )
            )

    # --- Accounts payable ----------------------------------------------------
    for bill in inputs.bills:
        d = clamp(bill.scheduled_date or bill.due_date)
        if not in_window(d):
            continue
        raw.append(
            CashFlow(
                seq=0,
                on_date=d,
                direction=Direction.OUTFLOW,
                amount=bill.amount,
                category=Category.VENDOR_PAYMENT,
                confidence=Confidence.RECORDED,
                basis="scheduled bill payment" if bill.scheduled_date else "bill due",
                scenario=scenario,
                source=bill.provenance,
                origin_id=bill.id,
            )
        )

    # --- Recurring items (rent, retainers, subscriptions) --------------------
    excluded_transfers = 0
    for item in inputs.recurring:
        if item.category is Category.TRANSFER:
            excluded_transfers += 1
            continue
        for d in item.recurrence.occurrences(as_of, horizon_end):
            raw.append(
                CashFlow(
                    seq=0,
                    on_date=d,
                    direction=item.direction,
                    amount=item.amount,
                    category=item.category,
                    confidence=Confidence.RECORDED,
                    basis=f"recurring: {item.label}",
                    scenario=scenario,
                    source=item.provenance,
                    origin_id=item.label,
                )
            )
    if excluded_transfers:
        notes.append(f"{excluded_transfers} internal transfer schedule(s) excluded from net cash")

    # --- Payroll (net pay + later tax remittance) ----------------------------
    for pr in inputs.payroll:
        for payday in pr.recurrence.occurrences(as_of, horizon_end):
            raw.append(
                CashFlow(
                    seq=0,
                    on_date=payday,
                    direction=Direction.OUTFLOW,
                    amount=pr.net_pay,
                    category=Category.PAYROLL_NET,
                    confidence=Confidence.RECORDED,
                    basis=f"payroll net: {pr.label}",
                    scenario=scenario,
                    source=pr.provenance,
                    origin_id=pr.label,
                )
            )
            if pr.payroll_taxes.is_positive:
                remit = payday + timedelta(days=pr.tax_remit_lag_days)
                if in_window(remit):
                    raw.append(
                        CashFlow(
                            seq=0,
                            on_date=remit,
                            direction=Direction.OUTFLOW,
                            amount=pr.payroll_taxes,
                            category=Category.PAYROLL_TAX,
                            confidence=Confidence.RECORDED,
                            basis=f"payroll taxes remitted (+{pr.tax_remit_lag_days}d): {pr.label}",
                            scenario=scenario,
                            source=pr.provenance,
                            origin_id=pr.label,
                        )
                    )

    # --- Debt service (single outflow; split kept as metadata) ---------------
    for d_inst in inputs.debt:
        for pay_date in d_inst.recurrence.occurrences(as_of, horizon_end):
            split = ""
            if d_inst.principal_portion and d_inst.interest_portion:
                split = (
                    f" (principal {d_inst.principal_portion.to_decimal_string()}, "
                    f"interest {d_inst.interest_portion.to_decimal_string()})"
                )
            raw.append(
                CashFlow(
                    seq=0,
                    on_date=pay_date,
                    direction=Direction.OUTFLOW,
                    amount=d_inst.payment,
                    category=Category.DEBT_SERVICE,
                    confidence=Confidence.RECORDED,
                    basis=f"debt service: {d_inst.label}{split}",
                    scenario=scenario,
                    source=d_inst.provenance,
                    origin_id=d_inst.label,
                )
            )

    # --- One-time items (plans, pending txns, tax payments) ------------------
    dropped_one_time = 0
    for ot in inputs.one_time:
        if ot.category is Category.TRANSFER:
            continue
        if not in_window(ot.on_date):
            dropped_one_time += 1
            continue
        raw.append(
            CashFlow(
                seq=0,
                on_date=ot.on_date,
                direction=ot.direction,
                amount=ot.amount,
                category=ot.category,
                confidence=ot.confidence,
                basis=f"one-time: {ot.label}",
                scenario=scenario,
                source=ot.provenance,
                origin_id=ot.label,
            )
        )
    if dropped_one_time:
        notes.append(f"{dropped_one_time} one-time item(s) fall outside the horizon")

    # Stable, deterministic ordering, then assign sequence numbers.
    raw.sort(
        key=lambda f: (
            f.on_date,
            f.direction.value,
            f.category.value,
            f.origin_id or "",
            f.amount.minor_units,
        )
    )
    flows = [
        CashFlow(
            seq=i + 1,
            on_date=f.on_date,
            direction=f.direction,
            amount=f.amount,
            category=f.category,
            confidence=f.confidence,
            basis=f.basis,
            scenario=f.scenario,
            source=f.source,
            origin_id=f.origin_id,
        )
        for i, f in enumerate(raw)
    ]
    _ = ccy  # currency consistency is enforced by Money arithmetic downstream
    return ResolvedFlows(flows=flows, notes=notes)


# Re-exported for engine/tests that build ad-hoc recurrences.
__all__ = ["resolve", "ResolvedFlows", "Recurrence", "Money"]
