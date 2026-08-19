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
    Money,
    OneTimeItem,
    PaymentObservation,
    PayrollSchedule,
    PipelineOpportunity,
    Recurrence,
    RecurringItem,
    Scenario,
    dumps,
    from_dto,
    loads,
    run_forecast,
    to_dto,
)


def _rich_inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("42000.00"), restricted=usd("5000.00")),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),),
        customer_histories=(
            CustomerHistory("acme", observations=(PaymentObservation(date(2026, 1, 1), date(2026, 1, 8)),)),
            CustomerHistory("beta", override_days_late=30),
        ),
        bills=(Bill("B1", "vendor", date(2026, 8, 25), usd("1800.00"), scheduled_date=date(2026, 8, 26)),),
        recurring=(
            RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        ),
        payroll=(
            PayrollSchedule("Team", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("16000.00"), usd("4200.00"), tax_remit_lag_days=3),
        ),
        debt=(
            DebtInstrument("Loan", Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 15)),
                           usd("2200.00"), usd("1500.00"), usd("700.00")),
        ),
        one_time=(
            OneTimeItem("Tax", Category.TAX_REMITTANCE, Direction.OUTFLOW, usd("12500.00"), date(2026, 9, 20)),
        ),
        pipeline=(
            PipelineOpportunity("Deal", "gamma", date(2026, 9, 15), usd("40000.00"), 5000),
        ),
    )


def test_round_trip_preserves_the_forecast_fingerprint() -> None:
    inputs = _rich_inputs()
    restored = loads(dumps(inputs))

    cfg = ForecastConfig()
    a = run_forecast(inputs, cfg, Scenario.BASE)
    b = run_forecast(restored, cfg, Scenario.BASE)
    assert a.version.input_fingerprint == b.version.input_fingerprint


def test_money_serializes_as_integer_minor_units() -> None:
    dto = to_dto(_rich_inputs())
    assert dto["opening"]["available"] == {"minor": 4200000, "currency": "USD"}
    assert dto["contract"] == "forecast-inputs/1"


def test_from_dto_tolerates_minimal_payload() -> None:
    dto = {
        "currency": "USD",
        "opening": {"as_of": "2026-08-03", "available": {"minor": 100000, "currency": "USD"}},
    }
    inputs = from_dto(dto)
    assert inputs.opening.available == usd("1000.00")
    assert inputs.opening.restricted == Money(0)
    assert inputs.invoices == ()
    # and it runs
    result = run_forecast(inputs)
    assert result.projection.opening_available == usd("1000.00")
