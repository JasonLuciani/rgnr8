"""Auto-categorization — rules precedence, learned suggestions, confidence math.

Everything is deterministic: fixed histories, no randomness, so every confidence
and category assertion is exact.
"""

from __future__ import annotations

from rgnr8_categorize import (
    Categorizer,
    CategorizedTxn,
    LearnedModel,
    Rule,
    RuleSet,
    Txn,
    normalize_description,
)


def _model(history: list[CategorizedTxn]) -> LearnedModel:
    return LearnedModel.from_history(history)


def test_rule_forces_category_and_beats_conflicting_learned_signal() -> None:
    # History strongly says this vendor is "Software"...
    history = [
        CategorizedTxn("h1", "ACME SUBSCRIPTION", "ACME", -1000, "Software"),
        CategorizedTxn("h2", "ACME SUBSCRIPTION", "ACME", -1000, "Software"),
        CategorizedTxn("h3", "ACME SUBSCRIPTION", "ACME", -1000, "Software"),
    ]
    # ...but a rule says ACME is "Advertising". The rule must win at 1.0.
    ruleset = RuleSet([Rule("acme-ads", "Advertising", counterparty="ACME")])
    cat = Categorizer(ruleset, _model(history))

    s = cat.suggest(Txn("t1", "ACME SUBSCRIPTION", "ACME", -1000))
    assert s.category == "Advertising"
    assert s.confidence == 1.0
    assert s.source == "rule"


def test_recurring_vendor_suggested_via_normalized_key_high_confidence() -> None:
    history = [
        CategorizedTxn(f"h{i}", "GUSTO PAYROLL", "GUSTO", -50000, "Payroll")
        for i in range(8)
    ]
    cat = Categorizer(RuleSet([]), _model(history))

    # New txn with a trailing ref number — must share the normalized key.
    s = cat.suggest(Txn("t1", "GUSTO PAYROLL 55", "GUSTO", -50000))
    assert s.category == "Payroll"
    assert s.confidence == 1.0
    assert s.source == "learned"
    assert s.confidence >= 0.9  # "high confidence"


def test_novel_transaction_is_uncategorized() -> None:
    cat = Categorizer(RuleSet([]), _model([]))
    s = cat.suggest(Txn("t1", "TOTALLY NEW MERCHANT XYZ", "WHOEVER", -1234))
    assert s.category == "Uncategorized"
    assert s.confidence == 0.0
    assert s.source == "none"
    assert s.reason == "no rule or history match"


def test_counterparty_key_preferred_over_description_key() -> None:
    # Description "transfer" historically => Savings; but counterparty ACME
    # historically => Software. A txn with both should follow the counterparty.
    history = [
        CategorizedTxn("h1", "TRANSFER", "BANK", -100, "Savings"),
        CategorizedTxn("h2", "TRANSFER", "BANK", -100, "Savings"),
        CategorizedTxn("h3", "ACME MONTHLY", "ACME", -100, "Software"),
        CategorizedTxn("h4", "ACME MONTHLY", "ACME", -100, "Software"),
    ]
    cat = Categorizer(RuleSet([]), _model(history))

    # Same normalized description as the Savings rows, but counterparty ACME.
    s = cat.suggest(Txn("t1", "TRANSFER", "ACME", -100))
    assert s.category == "Software"  # counterparty key wins over description key
    assert s.source == "learned"


def test_regex_and_amount_sign_predicates_combine() -> None:
    # An out-only refund rule: matches PAYPAL debits, not PAYPAL credits.
    rule = Rule(
        "paypal-out",
        "Fees",
        description_regex=r"paypal",
        amount_sign="out",
    )
    ruleset = RuleSet([rule])
    cat = Categorizer(ruleset, _model([]))

    outflow = Txn("t1", "PAYPAL TRANSFER", "PAYPAL", -500)
    inflow = Txn("t2", "PAYPAL TRANSFER", "PAYPAL", 500)

    assert rule.matches(outflow) is True
    assert rule.matches(inflow) is False  # amount_sign gate blocks the inflow

    assert cat.suggest(outflow).category == "Fees"
    # Inflow doesn't match the out-only rule => no rule, no history => Uncategorized.
    s_in = cat.suggest(inflow)
    assert s_in.source == "none"
    assert s_in.category == "Uncategorized"


def test_confidence_math_on_mixed_history_key() -> None:
    # 6x Meals, 4x Travel on the same normalized description key => 0.6 Meals.
    history = (
        [CategorizedTxn(f"m{i}", "UBER EATS 111", "UBER", -1000, "Meals") for i in range(6)]
        + [CategorizedTxn(f"t{i}", "UBER EATS 222", "UBER", -1000, "Travel") for i in range(4)]
    )
    # Drop counterparty so the description key is what's consulted.
    history = [
        CategorizedTxn(h.id, h.description, "", h.amount_minor, h.category)
        for h in history
    ]
    cat = Categorizer(RuleSet([]), _model(history))

    s = cat.suggest(Txn("t1", "UBER EATS 999", "", -1000))
    assert s.category == "Meals"
    assert s.confidence == 0.6
    assert s.source == "learned"


def test_normalize_collapses_ref_numbers() -> None:
    assert normalize_description("SQ *COFFEE 1234") == normalize_description("SQ *COFFEE 9987")
    assert normalize_description("SQ *COFFEE 1234") == "sq coffee"


def test_first_match_precedence_and_batch() -> None:
    rules = RuleSet(
        [
            Rule("payroll", "Payroll", counterparty="GUSTO"),
            Rule("catch-all-out", "Misc Expense", amount_sign="out"),
        ]
    )
    cat = Categorizer(rules, _model([]))

    txns = [
        Txn("a", "GUSTO PAYROLL", "GUSTO", -50000),
        Txn("b", "COFFEE", "SQUARE", -400),
        Txn("c", "REFUND", "SQUARE", 400),
    ]
    out = cat.categorize_batch(txns)
    assert [s.category for s in out] == ["Payroll", "Misc Expense", "Uncategorized"]
    assert [s.source for s in out] == ["rule", "rule", "none"]


def test_ties_broken_deterministically_by_category_name() -> None:
    # 1x Zebra, 1x Apple on one key => tie; Apple wins (ascending name).
    history = [
        CategorizedTxn("h1", "SPLIT VENDOR", "SPLIT", -100, "Zebra"),
        CategorizedTxn("h2", "SPLIT VENDOR", "SPLIT", -100, "Apple"),
    ]
    cat = Categorizer(RuleSet([]), _model(history))
    s = cat.suggest(Txn("t1", "SPLIT VENDOR", "SPLIT", -100))
    assert s.category == "Apple"
    assert s.confidence == 0.5
