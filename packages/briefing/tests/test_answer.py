from factory import breach_forecast, healthy_forecast
from rgnr8_briefing import Question, answer, ask, route, suggested_questions
from rgnr8_briefing.validate import validate_facts


def test_every_supported_answer_is_backed_by_valid_facts() -> None:
    fc = breach_forecast()
    for q in Question:
        a = answer(fc, q)
        assert a.supported
        assert a.answer_text
        assert validate_facts(a.facts, fc) == []


def test_will_i_breach_reflects_state() -> None:
    assert "Yes" in answer(breach_forecast(), Question.WILL_I_BREACH).answer_text
    assert "No" in answer(healthy_forecast(), Question.WILL_I_BREACH).answer_text


def test_routing_maps_free_text() -> None:
    assert route("am I going to run short?") is Question.WILL_I_BREACH
    assert route("how much cash right now") is Question.CASH_TODAY
    assert route("what's my biggest expense") is Question.BIGGEST_COST
    assert route("tell me a joke") is None


def test_unsupported_question_declines_with_suggestions() -> None:
    a = ask(healthy_forecast(), "what's the weather in Denver")
    assert not a.supported
    assert a.suggestions
    assert set(a.suggestions) == set(suggested_questions())


def test_biggest_cost_answer_matches_top_driver() -> None:
    fc = breach_forecast()
    a = answer(fc, Question.BIGGEST_COST)
    # the fact amount must reconcile (validator) and mention payroll
    assert validate_facts(a.facts, fc) == []
    assert "payroll" in a.answer_text.lower()
