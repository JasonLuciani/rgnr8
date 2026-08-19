"""Trust-monitor behaviour — grading, notes, alerts, and fleet ordering.

Every figure is exact integer-minor-unit ``Money`` in one currency (USD) and the
anchor date is injected, so each assertion is deterministic; nothing reads a clock.
"""

from __future__ import annotations

from datetime import date

from rgnr8_forecast import Money
from rgnr8_recon_monitor import (
    Divergence,
    Severity,
    check,
    divergence_alert,
    trust_report,
)

AS_OF = date(2026, 8, 13)

# A generous grading band shared across cases: within $1.00 is in sync, within
# $10.00 is a minor drift, beyond that is major.
MINOR_TOL = Money.from_decimal("1.00")
MAJOR_TOL = Money.from_decimal("10.00")


def _check(
    tenant: str,
    ledger: str,
    bank: str,
    forecast: str,
) -> Divergence:
    return check(
        tenant,
        AS_OF,
        Money.from_decimal(ledger),
        Money.from_decimal(bank),
        Money.from_decimal(forecast),
        minor_tolerance=MINOR_TOL,
        major_tolerance=MAJOR_TOL,
    )


# --------------------------------------------------------------------------- #
# check: grading the three figures
# --------------------------------------------------------------------------- #


def test_three_equal_figures_are_in_sync_with_no_alert() -> None:
    div = _check("t_sync", "5000.00", "5000.00", "5000.00")
    assert div.severity is Severity.IN_SYNC
    assert div.ledger_vs_bank == Money.zero("USD")
    assert div.bank_vs_forecast == Money.zero("USD")
    assert div.worst_gap == Money.zero("USD")
    assert "within tolerance" in div.note
    # in sync -> nothing to forward to the alert path
    assert divergence_alert(div) is None


def test_gap_just_over_minor_tolerance_is_minor_with_signed_amount_and_pair() -> None:
    # ledger is $1.50 above bank -> 150 minor units, past the $1.00 minor band.
    div = _check("t_minor", "5001.50", "5000.00", "5000.00")
    assert div.severity is Severity.MINOR
    # signed diff is exact: ledger - bank = +150 minor units.
    assert div.ledger_vs_bank == Money(150, "USD")
    assert div.worst_gap == Money(150, "USD")
    # the note names the diverging pair and the direction.
    assert "ledger exceeds bank" in div.note
    assert "minor divergence" in div.note


def test_negative_gap_keeps_sign_and_names_the_bank_side() -> None:
    # bank is above the forecast opening -> bank_vs_forecast positive; ledger below
    # bank -> ledger_vs_bank negative. Worst pair here is bank vs forecast.
    div = _check("t_neg", "5000.00", "5000.00", "4998.00")
    assert div.ledger_vs_bank == Money(0, "USD")
    assert div.bank_vs_forecast == Money(200, "USD")
    assert div.worst_gap == Money(200, "USD")
    assert div.severity is Severity.MINOR
    assert "bank exceeds forecast opening" in div.note


def test_gap_over_major_tolerance_is_major_with_alert_payload() -> None:
    # ledger is $50.00 below bank -> way past the $10.00 major band.
    div = _check("t_major", "4950.00", "5000.00", "5000.00")
    assert div.severity is Severity.MAJOR
    assert div.ledger_vs_bank == Money(-5000, "USD")
    assert div.worst_gap == Money(5000, "USD")

    alert = divergence_alert(div)
    assert alert is not None
    assert alert["severity"] == "major"
    assert alert["tenant"] == "t_major"
    assert alert["as_of"] == "2026-08-13"
    assert alert["message"] == div.note
    amounts = alert["amounts"]
    assert isinstance(amounts, dict)
    assert amounts["worst_gap"] == {
        "minor_units": 5000,
        "currency": "USD",
        "display": "USD 50.00",
    }
    assert amounts["ledger_vs_bank"]["minor_units"] == -5000


def test_worst_gap_picks_the_larger_diverging_pair() -> None:
    # ledger-vs-bank is $2.00; bank-vs-forecast is $7.00 -> forecast pair wins.
    div = _check("t_worst", "5002.00", "5000.00", "4993.00")
    assert div.ledger_vs_bank == Money(200, "USD")
    assert div.bank_vs_forecast == Money(700, "USD")
    assert div.worst_gap == Money(700, "USD")
    assert "bank exceeds forecast opening" in div.note


def test_boundary_at_minor_tolerance_stays_in_sync() -> None:
    # exactly $1.00 gap == minor tolerance -> still in sync (<=).
    div = _check("t_edge", "5001.00", "5000.00", "5000.00")
    assert div.worst_gap == Money(100, "USD")
    assert div.severity is Severity.IN_SYNC


def test_check_is_deterministic() -> None:
    a = _check("t", "4950.00", "5000.00", "5000.00")
    b = _check("t", "4950.00", "5000.00", "5000.00")
    assert a == b


# --------------------------------------------------------------------------- #
# trust_report: worst-first ordering + counts
# --------------------------------------------------------------------------- #


def test_trust_report_sorts_worst_first_and_counts_by_severity() -> None:
    in_sync = _check("t_ok", "5000.00", "5000.00", "5000.00")
    minor = _check("t_minor", "5001.50", "5000.00", "5000.00")
    major_small = _check("t_maj_a", "4980.00", "5000.00", "5000.00")  # $20 gap
    major_big = _check("t_maj_b", "4900.00", "5000.00", "5000.00")  # $100 gap

    report = trust_report([in_sync, minor, major_small, major_big])

    # MAJOR (largest gap first) -> MINOR -> IN_SYNC
    assert [row.tenant_id for row in report.rows] == [
        "t_maj_b",
        "t_maj_a",
        "t_minor",
        "t_ok",
    ]
    assert report.counts == {
        Severity.MAJOR: 2,
        Severity.MINOR: 1,
        Severity.IN_SYNC: 1,
    }
    assert report.as_of == AS_OF


def test_trust_report_to_dict_is_json_safe() -> None:
    import json

    report = trust_report(
        [
            _check("t_maj", "4900.00", "5000.00", "5000.00"),
            _check("t_ok", "5000.00", "5000.00", "5000.00"),
        ]
    )
    payload = report.to_dict()
    # round-trips through json without custom encoders
    encoded = json.dumps(payload)
    assert json.loads(encoded) == payload
    assert payload["as_of"] == "2026-08-13"
    assert payload["counts"] == {"major": 1, "minor": 0, "in_sync": 1}
    assert isinstance(payload["rows"], list)
    assert payload["rows"][0]["tenant_id"] == "t_maj"


def test_trust_report_empty_has_zero_counts_and_no_anchor() -> None:
    report = trust_report([])
    assert report.rows == ()
    assert report.as_of is None
    assert report.counts == {
        Severity.MAJOR: 0,
        Severity.MINOR: 0,
        Severity.IN_SYNC: 0,
    }
    assert report.to_dict()["as_of"] is None
