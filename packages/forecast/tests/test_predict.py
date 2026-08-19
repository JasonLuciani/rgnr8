
from factory import history
from rgnr8_forecast import CustomerHistory, ForecastConfig, predict_days_late


def test_override_wins() -> None:
    h = CustomerHistory("beta", override_days_late=30)
    p = predict_days_late(h, ForecastConfig())
    assert p.days_late == 30
    assert "override" in p.basis
    assert p.numeric_confidence >= 85


def test_learns_median_from_history() -> None:
    h = history("acme", [8, 10, 7, 9, 12])
    p = predict_days_late(h, ForecastConfig())
    assert p.days_late == 9  # median of [7,8,9,10,12]
    assert p.sample_size == 5
    assert "median" in p.basis


def test_falls_back_to_default_when_history_thin() -> None:
    h = history("new", [5])  # below min_history_observations (3)
    cfg = ForecastConfig(default_payment_delay_days=7)
    p = predict_days_late(h, cfg)
    assert p.days_late == 7
    assert "default" in p.basis
    assert p.numeric_confidence < 50


def test_no_history_uses_default() -> None:
    p = predict_days_late(None, ForecastConfig(default_payment_delay_days=4))
    assert p.days_late == 4


def test_tighter_history_is_more_confident() -> None:
    tight = predict_days_late(history("a", [10, 10, 10, 10, 10]), ForecastConfig())
    loose = predict_days_late(history("b", [0, 5, 10, 20, 45]), ForecastConfig())
    assert tight.numeric_confidence > loose.numeric_confidence


def test_early_payer_negative_days() -> None:
    p = predict_days_late(history("early", [-3, -2, -4, -3]), ForecastConfig())
    assert p.days_late < 0
