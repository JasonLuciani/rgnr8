from datetime import date

from factory import AS_OF, usd
from rgnr8_forecast import (
    CashPosition,
    CustomerHistory,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    PublicationStatus,
    Scenario,
    run_forecast,
)
from rgnr8_forecast.reproducibility import fingerprint


def _inputs(amount: str = "10000.00") -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("40000.00")),
        invoices=(Invoice("A", "beta", date(2026, 8, 1), date(2026, 8, 20), usd(amount)),),
        customer_histories=(CustomerHistory("beta", override_days_late=5),),
    )


def test_same_inputs_reproduce_the_same_fingerprint() -> None:
    cfg = ForecastConfig()
    a = run_forecast(_inputs(), cfg, Scenario.BASE)
    b = run_forecast(_inputs(), cfg, Scenario.BASE)
    assert a.version.input_fingerprint == b.version.input_fingerprint
    assert a.version.version_id == b.version.version_id


def test_changing_an_input_changes_the_fingerprint() -> None:
    cfg = ForecastConfig()
    a = run_forecast(_inputs("10000.00"), cfg, Scenario.BASE)
    b = run_forecast(_inputs("10000.01"), cfg, Scenario.BASE)
    assert a.version.input_fingerprint != b.version.input_fingerprint


def test_scenario_is_part_of_identity() -> None:
    cfg = ForecastConfig()
    base = run_forecast(_inputs(), cfg, Scenario.BASE)
    down = run_forecast(_inputs(), cfg, Scenario.DOWNSIDE)
    assert base.version.input_fingerprint != down.version.input_fingerprint
    assert "base" in base.version.version_id and "downside" in down.version.version_id


def test_publication_lifecycle_is_immutable() -> None:
    result = run_forecast(_inputs(), ForecastConfig(), Scenario.BASE)
    assert result.status is PublicationStatus.PRELIMINARY

    verified = result.verified("controller@rgnr8")
    published = verified.published("controller@rgnr8", "2026-08-06T22:00:00Z")

    assert verified.status is PublicationStatus.VERIFIED
    assert published.status is PublicationStatus.PUBLISHED
    assert published.version.reviewer == "controller@rgnr8"
    assert published.version.published_at == "2026-08-06T22:00:00Z"
    # original result object is unchanged — new versions rather than mutation
    assert result.status is PublicationStatus.PRELIMINARY
    # the underlying forecast numbers are identical across status changes
    assert published.version.input_fingerprint == result.version.input_fingerprint


def test_fingerprint_is_pure_and_order_independent_for_dicts() -> None:
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
