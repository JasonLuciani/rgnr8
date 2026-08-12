"""Constrained "ask-your-CFO" answerer.

Supports a fixed set of cash questions. AI (upstream) would interpret free text
and pick one of these; the answer itself is computed deterministically from the
forecast and every number carries evidence, so answers pass the same validator
as the briefing. Unsupported questions are declined with a clear limit and the
list of questions that CAN be answered (never an empty chat box).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rgnr8_forecast import Category, Direction, ForecastResult, Money

from .build import _largest, _outflow_drivers
from .models import Evidence, Fact


class Question(str, Enum):
    CASH_TODAY = "CASH_TODAY"
    WHEN_LOW = "WHEN_LOW"
    WILL_I_BREACH = "WILL_I_BREACH"
    BIGGEST_COST = "BIGGEST_COST"
    TOP_RECEIVABLE = "TOP_RECEIVABLE"
    MONEY_IN_OUT = "MONEY_IN_OUT"


SUGGESTED: list[tuple[Question, str]] = [
    (Question.CASH_TODAY, "How much cash do I have right now?"),
    (Question.WHEN_LOW, "When will my cash be at its lowest?"),
    (Question.WILL_I_BREACH, "Am I going to run short of cash?"),
    (Question.BIGGEST_COST, "What's my biggest cost over the next 13 weeks?"),
    (Question.TOP_RECEIVABLE, "Which upcoming customer payment matters most?"),
    (Question.MONEY_IN_OUT, "How much is coming in versus going out?"),
]

_KEYWORDS: list[tuple[Question, tuple[str, ...]]] = [
    (Question.WILL_I_BREACH, ("run short", "run out", "breach", "safe", "trouble", "at risk")),
    (Question.WHEN_LOW, ("lowest", "low point", "trough", "when will", "tightest")),
    (Question.CASH_TODAY, ("cash today", "right now", "how much cash", "balance")),
    (Question.BIGGEST_COST, ("biggest cost", "biggest expense", "largest cost", "spending")),
    (Question.TOP_RECEIVABLE, ("customer", "receivable", "who owes", "invoice", "payment coming")),
    (Question.MONEY_IN_OUT, ("coming in", "going out", "in versus out", "in vs out", "inflow", "outflow")),
]


@dataclass(frozen=True, slots=True)
class Answer:
    question_key: str
    question_text: str
    supported: bool
    answer_text: str
    facts: tuple[Fact, ...]
    confidence: int | None
    suggestions: tuple[str, ...] = ()


def suggested_questions() -> list[str]:
    return [text for _, text in SUGGESTED]


def route(text: str) -> Question | None:
    """Deterministic keyword routing (a stand-in for upstream NLU)."""
    low = text.lower()
    for q, kws in _KEYWORDS:
        if any(k in low for k in kws):
            return q
    return None


def answer(forecast: ForecastResult, question: Question) -> Answer:
    p = forecast.projection
    ccy = p.currency
    conf = forecast.overall_confidence

    if question is Question.CASH_TODAY:
        return Answer(
            question.value, _text(question), True,
            f"You have {ccy} {p.opening_available.to_decimal_string()} available today"
            + (f" ({ccy} {p.restricted.to_decimal_string()} of it restricted)." if p.restricted.is_positive else "."),
            (Fact("cash_today", "Cash available today", Evidence("field", field="opening_available"),
                  amount=p.opening_available),),
            conf,
        )

    if question is Question.WHEN_LOW:
        return Answer(
            question.value, _text(question), True,
            f"Your projected low point is {ccy} {p.trough.balance.to_decimal_string()} "
            f"around {p.trough.on_date.isoformat()}.",
            (Fact("low_point", "Projected low point", Evidence("field", field="trough_balance"),
                  amount=p.trough.balance, text=p.trough.on_date.isoformat()),),
            conf,
        )

    if question is Question.WILL_I_BREACH:
        if p.breach.breached and p.breach.first_date is not None:
            txt = (
                f"Yes — cash is projected to dip below your {ccy} {p.effective_floor.to_decimal_string()} "
                f"floor around {p.breach.first_date.isoformat()} (week {p.breach.weeks_until}), "
                f"short by up to {ccy} {p.breach.worst_shortfall.to_decimal_string()}."
            )
            facts = (
                Fact("shortfall", "Largest projected shortfall", Evidence("field", field="worst_shortfall"),
                     amount=p.breach.worst_shortfall),
            )
        else:
            cushion = p.trough.balance - p.effective_floor
            txt = (
                f"No — cash stays above your {ccy} {p.effective_floor.to_decimal_string()} floor. "
                f"The tightest it gets is {ccy} {cushion.to_decimal_string()} of cushion "
                f"around {p.trough.on_date.isoformat()}."
            )
            facts = (
                Fact("cushion", "Cushion at the low point", Evidence("field", field="cushion_at_trough"),
                     amount=cushion),
            )
        return Answer(question.value, _text(question), True, txt, facts, conf)

    if question is Question.BIGGEST_COST:
        drivers = _outflow_drivers(forecast.flows, p.total_outflows, ccy, top=1)
        if not drivers:
            return Answer(question.value, _text(question), True,
                          "There are no outflows in the forecast window.", (), conf)
        d = drivers[0]
        return Answer(
            question.value, _text(question), True,
            f"Your biggest cost is {d.label.lower()} at {ccy} {d.amount.to_decimal_string()} "
            f"over 13 weeks ({d.share_bps / 100:.0f}% of all outflows).",
            (Fact("biggest_cost", d.label, d.evidence, amount=d.amount),),
            conf,
        )

    if question is Question.TOP_RECEIVABLE:
        rec = _largest(
            tuple(f for f in forecast.flows if f.category is Category.CUSTOMER_RECEIPT),
            Direction.INFLOW,
        )
        if rec is None:
            return Answer(question.value, _text(question), True,
                          "No customer receipts are projected in the window.", (), conf)
        return Answer(
            question.value, _text(question), True,
            f"The receipt that matters most is {rec.origin_id} for {ccy} "
            f"{rec.amount.to_decimal_string()}, expected {rec.on_date.isoformat()}.",
            (Fact("top_receivable", "Largest upcoming receipt", Evidence("flows", flow_seqs=(rec.seq,)),
                  amount=rec.amount, text=rec.basis),),
            conf,
        )

    # MONEY_IN_OUT
    net = p.total_inflows - p.total_outflows
    return Answer(
        question.value, _text(question), True,
        f"Over the next 13 weeks: {ccy} {p.total_inflows.to_decimal_string()} in, "
        f"{ccy} {p.total_outflows.to_decimal_string()} out — a net of {ccy} {net.to_decimal_string()}.",
        (
            Fact("total_in", "Money in", Evidence("field", field="total_inflows"), amount=p.total_inflows),
            Fact("total_out", "Money out", Evidence("field", field="total_outflows"), amount=p.total_outflows),
        ),
        conf,
    )


def ask(forecast: ForecastResult, text: str) -> Answer:
    """Answer free text if it maps to a supported question; otherwise decline."""
    q = route(text)
    if q is not None:
        return answer(forecast, q)
    return Answer(
        question_key="UNSUPPORTED",
        question_text=text,
        supported=False,
        answer_text=(
            "That's outside what I can answer from the cash forecast right now. "
            "I can help with the questions below, or route this to your finance reviewer."
        ),
        facts=(),
        confidence=None,
        suggestions=tuple(suggested_questions()),
    )


def _text(q: Question) -> str:
    return dict(SUGGESTED)[q]
