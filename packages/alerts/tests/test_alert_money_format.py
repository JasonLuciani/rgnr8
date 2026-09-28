"""Alert text is read by an owner, usually in email, so it is formatted too.

Same rule as `briefing/tests/test_money_reads_as_money.py`, applied at the other
end of the product: an alert that says "below your USD 1000.00 threshold" while
the screen it links to says "$1,000.00" undermines both.
"""

from __future__ import annotations

import re

from factory import build_forecast
from rgnr8_alerts import (
    BalanceBelow,
    FloorBreachWithinWeeks,
    LargeOutflow,
    TroughWorsened,
    evaluate,
)

from rgnr8_forecast import Money

# Any code at all — see the note in the briefing test. A pattern that required
# a digit after the code missed "USD $1,000.00", which is precisely what the
# floor-breach rule produced.
_RAW_CODE = re.compile(r"\b(?:USD|EUR|GBP)\b")
_UNGROUPED = re.compile(r"(?<![\d,.])\d{4,}\.\d{2}\b")
_MONEY = re.compile(r"-?\$\d{1,3}(,\d{3})*\.\d{2}")


def _breach():
    return build_forecast(
        weeks=[(500_00, 300_00), (300_00, 200_00), (200_00, 5_00)],
        floor_minor=1000_00,
        breach_week=3,
        breach_shortfall_minor=995_00,
    )


def _every_alert_message() -> list[str]:
    fc = _breach()
    rules = (
        FloorBreachWithinWeeks(weeks=13),
        BalanceBelow(amount=Money(2_000_00)),
        LargeOutflow(amount=Money(100_00)),
        TroughWorsened(vs_amount=Money(5_000_00)),
    )
    out: list[str] = []
    for rule in rules:
        for alert in evaluate(fc, (rule,)):
            out.append(alert.message)
            out.append(alert.title)
    return out


def test_alerts_never_show_a_raw_currency_code() -> None:
    bad = [m for m in _every_alert_message() if _RAW_CODE.search(m)]
    assert not bad, f"alert text bypassing format_money: {bad}"


def test_alerts_never_show_an_ungrouped_thousand() -> None:
    bad = [m for m in _every_alert_message() if _UNGROUPED.search(m)]
    assert not bad, f"alert figures not grouped: {bad}"


def test_the_alerts_still_contain_figures() -> None:
    # So the two tests above can't pass by the numbers going missing.
    messages = _every_alert_message()
    assert messages, "the fixture should trip at least one rule"
    assert any(_MONEY.search(m) for m in messages), messages
