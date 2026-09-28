"""Every figure an owner reads is formatted the same way.

The prelaunch review filed this as design-system drift, and it was visible on
the product's main screen right up to this change: the status line read
"below your USD 10000.00 floor" while the tile directly beside it read
"$10,000.00". Two formats for the same number, eighteen inches apart.

Reading the source for `f"{ccy} {x.to_decimal_string()}"` was how the drift was
found, but it is not what this test does — a grep only catches the spelling
that happened to be used last time. These tests read the strings the product
actually produces and assert the shape of what's in them, so a new sentence
built a new wrong way fails just the same.

`format_money` is the one formatter: `$128,400.00` for USD, `EUR 1,250.00` for
anything else, grouped, with the sign in front.
"""

from __future__ import annotations

import re

from factory import breach_forecast, healthy_forecast

from rgnr8_briefing import Question, answer, build_briefing, render_text
from rgnr8_briefing.render import render_html

# Any currency code at all. The fixtures are USD, and `format_money` renders
# USD as "$128,400.00" with no code anywhere — so in these strings a code is
# always a figure someone assembled by hand.
#
# The first version of this pattern was `\b(USD|EUR|GBP)\b\s*-?[\d(]`, which
# required a digit right after the code. It let through the worst case of all:
# an alert whose f-string had `{ccy} ` on one physical line and the amount on
# the next, which after the sweep rendered "below your USD $10,000.00 floor" —
# the code AND the dollar sign. Requiring the absence of the code entirely is
# the assertion that cannot be sneaked past.
_RAW_CODE = re.compile(r"\b(?:USD|EUR|GBP)\b")

# An unformatted decimal: four or more digits before the point with no grouping
# separator. "10000.00" fails, "10,000.00" passes, "2026-08-03" is a date and
# has no point, "97/100" has no point either.
_UNGROUPED = re.compile(r"(?<![\d,.])\d{4,}\.\d{2}\b")


def _owner_facing_strings(fc) -> dict[str, str]:
    """Everything a business owner can read that might contain a figure."""
    b = build_briefing(fc)
    out: dict[str, str] = {
        "forecast.headline": fc.headline,
        "forecast.recommended_action": fc.recommended_action or "",
        "briefing.headline": b.headline,
        "briefing.status_reason": b.status_reason,
        "briefing.primary_action": b.primary_action or "",
        "render_text": render_text(b),
        "render_html": render_html(b),
    }
    for i, note in enumerate(fc.data_quality):
        out[f"forecast.data_quality[{i}]"] = note
    for i, note in enumerate(b.data_quality):
        out[f"briefing.data_quality[{i}]"] = note
    for f in b.facts:
        out[f"fact[{f.key}].label"] = f.label
        out[f"fact[{f.key}].text"] = f.text or ""
        out[f"fact[{f.key}].evidence"] = f.evidence.note or ""
    for d in b.drivers:
        out[f"driver[{d.category.value}].label"] = d.label
    for q in Question:
        a = answer(fc, q)
        out[f"answer[{q.value}]"] = a.answer_text
        for f in a.facts:
            out[f"answer[{q.value}].fact[{f.key}]"] = f.text or ""
    # `basis` is the one-line explanation attached to every projected flow; it
    # surfaces wherever a figure is drilled into.
    for flow in fc.flows:
        out[f"flow[{flow.seq}].basis"] = flow.basis
    return out


def _offences(pattern: re.Pattern[str], fc) -> dict[str, str]:
    return {k: v for k, v in _owner_facing_strings(fc).items() if pattern.search(v)}


def test_no_owner_facing_string_shows_a_raw_currency_code() -> None:
    for name, fc in (("breach", breach_forecast()), ("healthy", healthy_forecast())):
        bad = _offences(_RAW_CODE, fc)
        assert not bad, (
            f"{name}: these strings put a currency code next to a bare number "
            f"instead of going through format_money: {bad}"
        )


def test_no_owner_facing_string_shows_an_ungrouped_thousand() -> None:
    # The other half of the same defect: "$10000.00" is as wrong as
    # "USD 10000.00", and a grep for the currency code would not have caught it.
    for name, fc in (("breach", breach_forecast()), ("healthy", healthy_forecast())):
        bad = _offences(_UNGROUPED, fc)
        assert not bad, f"{name}: these figures are not grouped: {bad}"


def test_the_headline_actually_carries_a_formatted_figure() -> None:
    # Guard against the tests above passing because the figures disappeared.
    fc = breach_forecast()
    assert re.search(r"\$\d{1,3}(,\d{3})*\.\d{2}", fc.headline), fc.headline
    b = build_briefing(fc)
    assert re.search(r"\$\d{1,3}(,\d{3})*\.\d{2}", b.headline), b.headline


def test_the_recommended_action_is_formatted_too() -> None:
    fc = breach_forecast()
    action = fc.recommended_action or ""
    assert action, "a breached forecast should recommend something"
    assert re.search(r"\$\d{1,3}(,\d{3})*\.\d{2}", action), action
    assert not _RAW_CODE.search(action), action


def test_a_healthy_forecast_reads_the_same_way() -> None:
    # The no-breach branch is a separate sentence and was separately wrong.
    fc = healthy_forecast()
    assert re.search(r"\$\d{1,3}(,\d{3})*\.\d{2}", fc.headline), fc.headline
    assert not _RAW_CODE.search(fc.headline), fc.headline
