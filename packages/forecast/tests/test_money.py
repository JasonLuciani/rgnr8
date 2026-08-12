import pytest
from rgnr8_forecast import Money, money_sum
from rgnr8_forecast.money import CurrencyMismatch


def test_exact_addition_no_float_error() -> None:
    assert (Money.from_decimal("0.10") + Money.from_decimal("0.20")).to_decimal_string() == "0.30"


def test_from_decimal_parsing() -> None:
    assert Money.from_decimal("1234.56").minor_units == 123456
    assert Money.from_decimal("1000").minor_units == 100000
    assert Money.from_decimal("-42.00").minor_units == -4200
    assert Money.from_decimal("0.05").minor_units == 5


def test_from_decimal_rejects_excess_precision() -> None:
    with pytest.raises(ValueError):
        Money.from_decimal("1.005")


def test_from_decimal_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        Money.from_decimal("abc")
    with pytest.raises(ValueError):
        Money.from_decimal("1.2.3")


def test_to_decimal_string_formatting() -> None:
    assert Money(5).to_decimal_string() == "0.05"
    assert Money(-5).to_decimal_string() == "-0.05"
    assert Money(100000).to_decimal_string() == "1000.00"
    assert Money(0).to_decimal_string() == "0.00"


def test_currency_mismatch_raises() -> None:
    with pytest.raises(CurrencyMismatch):
        Money(100, "USD") + Money(100, "EUR")


def test_scale_by_banker_rounding() -> None:
    # 1000.00 * 5% = 50.00 exactly
    assert Money.from_decimal("1000.00").scale_by(5, 100).to_decimal_string() == "50.00"
    # 1.00 * 1/3 -> 0.33 (0.3333 rounds to 33 cents)
    assert Money.from_decimal("1.00").scale_by(1, 3).minor_units == 33
    # round half to even: 0.125 -> at 2dp of 12.5 cents -> 12 (even)
    assert Money(25).scale_by(1, 2).minor_units == 12  # 12.5 -> 12 (even)
    assert Money(75).scale_by(1, 2).minor_units == 38  # 37.5 -> 38 (even)


def test_scale_by_rounds_negatives_symmetrically() -> None:
    # Regression: negatives must round to nearest with half-to-even, the same as
    # positives — never away from zero (the pre-launch review off-by-2 bug).
    assert Money(-1).scale_by(1, 2).minor_units == 0    # -0.5 -> 0 (even)
    assert Money(-5).scale_by(1, 2).minor_units == -2   # -2.5 -> -2 (even)
    assert Money(-3).scale_by(1, 2).minor_units == -2   # -1.5 -> -2 (even)
    assert Money(-3).scale_by(1, 4).minor_units == -1   # -0.75 -> -1
    assert Money(-1).scale_by(1, 4).minor_units == 0    # -0.25 -> 0
    # mirror image of the positive cases
    assert Money(-25).scale_by(1, 2).minor_units == -12  # -12.5 -> -12 (even)
    # negative denominator handled too
    assert Money(5).scale_by(1, -2).minor_units == -2


def test_predicates_and_sum() -> None:
    assert Money(0).is_zero
    assert Money(5).is_positive
    assert Money(-5).is_negative
    assert money_sum([Money(100), Money(250), Money(1)]).minor_units == 351
    assert money_sum([]).minor_units == 0


def test_ordering_within_currency() -> None:
    assert Money(100) < Money(200)
    assert max(Money(1), Money(9), Money(3)).minor_units == 9
