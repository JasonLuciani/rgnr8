import dataclasses

from rgnr8_forecast import Money
from rgnr8_briefing import build_briefing, validate_briefing
from rgnr8_briefing.models import Evidence, Fact
from factory import breach_forecast, healthy_forecast


def test_a_freshly_built_briefing_has_no_violations() -> None:
    for fc in (healthy_forecast(), breach_forecast()):
        b = build_briefing(fc)
        assert validate_briefing(b, fc) == []


def test_tampered_fact_amount_is_caught() -> None:
    fc = healthy_forecast()
    b = build_briefing(fc)
    # inflate the "cash today" fact by a dollar
    tampered_facts = tuple(
        dataclasses.replace(f, amount=f.amount + Money(100)) if f.key == "cash_today" else f
        for f in b.facts
    )
    tampered = dataclasses.replace(b, facts=tampered_facts)
    violations = validate_briefing(tampered, fc)
    assert len(violations) == 1
    assert violations[0].where == "fact:cash_today"
    assert violations[0].expected == fc.projection.opening_available


def test_fact_referencing_unknown_flow_is_caught() -> None:
    fc = healthy_forecast()
    b = build_briefing(fc)
    bogus = Fact("bogus", "Bogus", Evidence("flows", flow_seqs=(9999,)), amount=Money(500))
    tampered = dataclasses.replace(b, facts=b.facts + (bogus,))
    violations = validate_briefing(tampered, fc)
    assert any(v.where == "fact:bogus" for v in violations)


def test_tampered_driver_total_is_caught() -> None:
    fc = breach_forecast()
    b = build_briefing(fc)
    bad_drivers = (dataclasses.replace(b.drivers[0], amount=b.drivers[0].amount + Money(1)),) + b.drivers[1:]
    tampered = dataclasses.replace(b, drivers=bad_drivers)
    violations = validate_briefing(tampered, fc)
    assert any(v.where.startswith("driver:") for v in violations)


def test_textual_fact_without_amount_is_not_flagged() -> None:
    fc = healthy_forecast()
    b = build_briefing(fc)
    note = Fact("note", "Note", Evidence("field", field="opening_available"), amount=None, text="hello")
    tampered = dataclasses.replace(b, facts=b.facts + (note,))
    assert validate_briefing(tampered, fc) == []
