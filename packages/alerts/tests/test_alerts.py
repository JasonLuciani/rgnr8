"""Tests for the RGNR8 proactive-alert engine."""

from __future__ import annotations

from factory import build_forecast
from rgnr8_alerts import (
    Alert,
    AlertDispatcher,
    AlertRule,
    BalanceBelow,
    CallableAlertSink,
    FloorBreachWithinWeeks,
    InMemoryAlertSink,
    InMemoryAlertStateStore,
    LargeOutflow,
    Severity,
    TroughWorsened,
    evaluate,
)
from rgnr8_forecast import ForecastResult, Money


def _breach_forecast() -> ForecastResult:
    """Floor $1000; cash breaches it in week 3 (trough $500, short by $500)."""
    return build_forecast(
        weeks=[(500_00, 300_00), (300_00, 200_00), (200_00, 5_00)],
        floor_minor=1000_00,
        breach_week=3,
        breach_shortfall_minor=995_00,
    )


def _healthy_forecast() -> ForecastResult:
    """Floor $1000; cash stays well above it; gentle week-over-week drift."""
    return build_forecast(
        weeks=[(5000_00, 4950_00), (4950_00, 4900_00), (4900_00, 4850_00)],
        floor_minor=1000_00,
    )


# --- floor breach: fire once, dedupe, re-arm --------------------------------- #


def test_floor_breach_week3_fires_at_risk_once() -> None:
    forecast = _breach_forecast()
    rules = [FloorBreachWithinWeeks(weeks=4)]
    alerts = evaluate(forecast, rules, clock=lambda: 1000.0)

    assert len(alerts) == 1
    (alert,) = alerts
    assert alert.severity is Severity.AT_RISK
    assert alert.at == 1000
    assert alert.evidence["weeks_until"] == 3


def test_floor_breach_outside_window_does_not_fire() -> None:
    forecast = _breach_forecast()  # breach in week 3
    alerts = evaluate(forecast, [FloorBreachWithinWeeks(weeks=2)])
    assert alerts == []


def test_dispatch_dedupes_then_rearms() -> None:
    store = InMemoryAlertStateStore()
    sink = InMemoryAlertSink()
    dispatcher = AlertDispatcher(store, sink, clock=lambda: 2000.0)
    rules = [FloorBreachWithinWeeks(weeks=4)]

    breach = _breach_forecast()
    healthy = _healthy_forecast()

    # Tick 1: breach forming -> delivered once.
    delivered = dispatcher.dispatch(evaluate(breach, rules))
    assert len(delivered) == 1
    assert len(sink.sent) == 1
    assert dispatcher.last_dispatch_at == 2000

    # Tick 2: unchanged -> deduped, nothing new delivered.
    delivered = dispatcher.dispatch(evaluate(breach, rules))
    assert delivered == []
    assert len(sink.sent) == 1

    # Tick 3: condition clears -> key re-armed (dropped from store).
    delivered = dispatcher.dispatch(evaluate(healthy, rules))
    assert delivered == []
    assert len(sink.sent) == 1
    assert store.active_keys() == frozenset()

    # Tick 4: condition recurs -> fires again.
    delivered = dispatcher.dispatch(evaluate(breach, rules))
    assert len(delivered) == 1
    assert len(sink.sent) == 2


# --- each rule fires on the right fixture and not otherwise ------------------- #


def test_balance_below_fires_only_when_trough_below_threshold() -> None:
    rule = BalanceBelow(amount=Money(1000_00))

    hit = evaluate(_breach_forecast(), [rule])
    assert len(hit) == 1
    assert hit[0].severity is Severity.WATCH
    assert hit[0].key == "balance_below_USD_100000"

    miss = evaluate(_healthy_forecast(), [rule])
    assert miss == []


def test_large_outflow_fires_on_big_week_and_reports_worst() -> None:
    rule = LargeOutflow(amount=Money(150_00))

    hit = evaluate(_breach_forecast(), [rule])
    assert len(hit) == 1
    # Worst single-week drop is week 1: $500 -> $300 = $200.
    assert hit[0].evidence["week_index"] == 1
    assert hit[0].evidence["drop_minor"] == 200_00

    miss = evaluate(_healthy_forecast(), [rule])
    assert miss == []


def test_trough_worsened_fires_only_below_baseline() -> None:
    rule = TroughWorsened(vs_amount=Money(1200_00))

    hit = evaluate(_breach_forecast(), [rule])
    assert len(hit) == 1
    assert hit[0].severity is Severity.INFO
    assert hit[0].evidence["worse_by_minor"] == 1200_00 - 5_00

    miss = evaluate(_healthy_forecast(), [rule])
    assert miss == []


# --- ordering ---------------------------------------------------------------- #


def test_alerts_sort_worst_first() -> None:
    forecast = _breach_forecast()
    rules: list[AlertRule] = [
        TroughWorsened(vs_amount=Money(1200_00)),  # INFO
        BalanceBelow(amount=Money(1000_00)),  # WATCH
        FloorBreachWithinWeeks(weeks=4),  # AT_RISK
        LargeOutflow(amount=Money(150_00)),  # WATCH
    ]
    alerts = evaluate(forecast, rules)

    severities = [a.severity for a in alerts]
    assert severities == [Severity.AT_RISK, Severity.WATCH, Severity.WATCH, Severity.INFO]
    assert alerts[0].key.startswith("floor_breach")
    assert alerts[-1].key.startswith("trough_worsened")
    # Descending, non-increasing severity throughout.
    assert all(
        int(a.severity) >= int(b.severity) for a, b in zip(alerts, alerts[1:])
    )


# --- sinks ------------------------------------------------------------------- #


def test_callable_sink_wires_to_a_function() -> None:
    received: list[Alert] = []
    store = InMemoryAlertStateStore()
    dispatcher = AlertDispatcher(
        store, CallableAlertSink(received.append), clock=lambda: 0.0
    )

    dispatcher.dispatch(evaluate(_breach_forecast(), [FloorBreachWithinWeeks(weeks=4)]))
    assert len(received) == 1
    assert received[0].severity is Severity.AT_RISK
