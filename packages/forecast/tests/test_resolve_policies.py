from datetime import date

from factory import AS_OF, usd
from rgnr8_forecast import (
    Bill,
    CashPosition,
    Category,
    CustomerHistory,
    DebtInstrument,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Frequency,
    Invoice,
    InvoiceStatus,
    OneTimeItem,
    PayrollSchedule,
    PipelineOpportunity,
    Recurrence,
    RecurringItem,
    Scenario,
    base_assumptions,
    downside_assumptions,
    upside_assumptions,
)
from rgnr8_forecast.resolve import resolve


def _opening() -> CashPosition:
    return CashPosition(as_of=AS_OF, available=usd("50000.00"))


def _cats(flows, category):  # type: ignore[no-untyped-def]
    return [f for f in flows if f.category is category]


def test_ar_predicted_timing_uses_due_plus_days_late() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        invoices=(Invoice("INV-1", "beta", date(2026, 8, 1), date(2026, 8, 31), usd("12000.00")),),
        customer_histories=(CustomerHistory("beta", override_days_late=10),),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    receipts = _cats(r.flows, Category.CUSTOMER_RECEIPT)
    assert len(receipts) == 1
    assert receipts[0].on_date == date(2026, 9, 10)  # due + 10
    assert receipts[0].amount == usd("12000.00")


def test_overdue_ar_is_clamped_to_today() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        invoices=(Invoice("INV-OLD", "beta", date(2026, 6, 1), date(2026, 7, 1), usd("5000.00")),),
        customer_histories=(CustomerHistory("beta", override_days_late=5),),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    receipts = _cats(r.flows, Category.CUSTOMER_RECEIPT)
    assert receipts[0].on_date == AS_OF
    assert "clamped" in receipts[0].basis


def test_partial_invoice_uses_open_amount() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        invoices=(
            Invoice("INV-P", "beta", date(2026, 8, 1), date(2026, 8, 15), usd("2500.00"),
                    status=InvoiceStatus.PARTIAL),
        ),
        customer_histories=(CustomerHistory("beta", override_days_late=0),),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    assert _cats(r.flows, Category.CUSTOMER_RECEIPT)[0].amount == usd("2500.00")


def test_disputed_invoice_is_excluded() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        invoices=(
            Invoice("INV-D", "beta", date(2026, 8, 1), date(2026, 8, 15), usd("9000.00"),
                    status=InvoiceStatus.DISPUTED),
        ),
        customer_histories=(CustomerHistory("beta", override_days_late=0),),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    assert _cats(r.flows, Category.CUSTOMER_RECEIPT) == []
    assert any("disputed" in n for n in r.notes)


def test_internal_transfers_are_excluded() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        recurring=(
            RecurringItem("Sweep to savings", Category.TRANSFER, Direction.OUTFLOW, usd("1000.00"),
                          Recurrence(Frequency.WEEKLY, anchor=date(2026, 8, 7))),
        ),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    assert _cats(r.flows, Category.TRANSFER) == []
    assert any("transfer" in n for n in r.notes)


def test_payroll_splits_net_and_tax_with_lag() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        payroll=(
            PayrollSchedule("Team", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("16000.00"), usd("4200.00"), tax_remit_lag_days=3),
        ),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    net = _cats(r.flows, Category.PAYROLL_NET)
    tax = _cats(r.flows, Category.PAYROLL_TAX)
    assert net[0].on_date == date(2026, 8, 7)
    assert net[0].amount == usd("16000.00")
    assert tax[0].on_date == date(2026, 8, 10)  # payday + 3
    assert tax[0].amount == usd("4200.00")
    assert all(f.direction is Direction.OUTFLOW for f in net + tax)


def test_debt_is_single_outflow_with_split_metadata() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        debt=(
            DebtInstrument("SBA", Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 15)),
                           usd("2200.00"), usd("1500.00"), usd("700.00")),
        ),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    debt = _cats(r.flows, Category.DEBT_SERVICE)
    assert debt[0].amount == usd("2200.00")  # single outflow, not 1500+700 double counted
    assert "principal" in debt[0].basis and "interest" in debt[0].basis


def test_bad_debt_writedown_only_in_downside() -> None:
    inv = Invoice("INV-AGED", "beta", date(2026, 3, 1), date(2026, 4, 1), usd("10000.00"))
    inputs = ForecastInputs(
        opening=_opening(),
        invoices=(inv,),
        customer_histories=(CustomerHistory("beta", override_days_late=2),),
    )
    base = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    down = resolve(inputs, ForecastConfig(), Scenario.DOWNSIDE, downside_assumptions())
    assert _cats(base.flows, Category.CUSTOMER_RECEIPT)[0].amount == usd("10000.00")
    # 25% written down -> 7500 receipt, plus a 1.5% refund reserve outflow
    assert _cats(down.flows, Category.CUSTOMER_RECEIPT)[0].amount == usd("7500.00")
    assert _cats(down.flows, Category.REFUND_CHARGEBACK)[0].amount == usd("112.50")


def test_pipeline_only_in_upside_and_probability_weighted() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        pipeline=(PipelineOpportunity("Big deal", "gamma", date(2026, 9, 15), usd("40000.00"), 5000),),
    )
    base = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    up = resolve(inputs, ForecastConfig(), Scenario.UPSIDE, upside_assumptions())
    assert _cats(base.flows, Category.PIPELINE_RECEIPT) == []
    assert _cats(up.flows, Category.PIPELINE_RECEIPT)[0].amount == usd("20000.00")  # 50%


def test_out_of_horizon_items_are_dropped_with_note() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        one_time=(
            OneTimeItem("Late capex", Category.CAPITAL_EXPENDITURE, Direction.OUTFLOW,
                        usd("5000.00"), date(2027, 1, 1)),
        ),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    assert _cats(r.flows, Category.CAPITAL_EXPENDITURE) == []
    assert any("outside the horizon" in n for n in r.notes)


def test_flows_have_stable_sequence_and_provenance_origin() -> None:
    inputs = ForecastInputs(
        opening=_opening(),
        bills=(Bill("B1", "vendor", date(2026, 8, 20), usd("3000.00")),),
    )
    r = resolve(inputs, ForecastConfig(), Scenario.BASE, base_assumptions())
    seqs = [f.seq for f in r.flows]
    assert seqs == sorted(seqs)
    assert r.flows[0].origin_id == "B1"
