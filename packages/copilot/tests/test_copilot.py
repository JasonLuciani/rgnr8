"""Ask RGNR8 — orchestrator, tools, and the numeric-integrity backstop.

Everything runs against FakeLLM (scripted turns) and FakeLedgerReader (canned
bodies): no network, fully deterministic. The load-bearing tests are the backstop
ones — a fabricated figure must never ship.
"""

from __future__ import annotations

from typing import Sequence

from rgnr8_copilot import (
    AskContext,
    AskOrchestrator,
    FakeLedgerReader,
    FakeLLM,
    HttpLLM,
    LLMTurn,
    Msg,
    ToolCall,
    collect_minor_figures,
    default_registry,
    extract_money_minor,
)

# --- fixtures ----------------------------------------------------------------

RATIOS: dict[str, object] = {
    "contract": "financial-ratios/1", "as_of": "2026-08-19",
    "inputs": {"revenue_minor": "500000", "net_income_minor": "170000",
               "total_assets_minor": "900000", "total_liabilities_minor": "300000"},
    "liquidity": {"current_ratio": "5.90", "health": "healthy — comfortably covers short-term"},
    "leverage": {"dscr": "0.19", "health": "at risk — earnings do not cover debt service"},
    "profitability": {"gross_margin_pct": "60.00", "net_margin_pct": "34.00", "health": "strong"},
}

TRIAL_BALANCE: dict[str, object] = {
    "rows": [
        {"code": "1000", "name": "Business Checking", "debit_minor": "20000000", "credit_minor": "0"},
        {"code": "1010", "name": "Savings", "debit_minor": "500000", "credit_minor": "0"},
        {"code": "4000", "name": "Sales", "debit_minor": "0", "credit_minor": "500000"},
    ],
}

DEBT: dict[str, object] = {
    "contract": "debt-dashboard/1",
    "totals": {"total_principal_minor": "2000000", "monthly_debt_service_minor": "86066",
               "weighted_avg_rate_micro": "90000", "loan_count": 2},
    "loans": [{"id": "truck", "lender": "First Bank", "current_principal_minor": "1000000"}],
}


def _reader() -> FakeLedgerReader:
    return (FakeLedgerReader()
            .on("/ratios", RATIOS)
            .on("/trial-balance", TRIAL_BALANCE)
            .on("/debt", DEBT)
            .on("/aging", {"side": "ar", "buckets": []}))


def _ctx(perms: Sequence[str] = ("ledger:read", "reports:read")) -> AskContext:
    return AskContext(tenant="acme", permissions=frozenset(perms), ledger=_reader(),
                      hints={"cash_accounts": ["1000", "1010"]})


def _orch(turns: list[LLMTurn]) -> tuple[AskOrchestrator, FakeLLM]:
    llm = FakeLLM(turns=list(turns))
    return AskOrchestrator(llm, default_registry()), llm


# --- helpers -----------------------------------------------------------------

def test_money_extraction() -> None:
    assert extract_money_minor("net income of $1,700.00 on $5,000") == {170000, 500000}
    assert extract_money_minor("margin was 60% over 12 months in 2026") == set()  # no $ tokens


def test_collect_minor_figures_by_convention() -> None:
    figs = collect_minor_figures(RATIOS)
    assert 500000 in figs and 170000 in figs and 900000 in figs


# --- happy path --------------------------------------------------------------

def test_ratios_question_answers_with_a_verified_cited_figure() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "ratios", {}),)),
        LLMTurn(final_text="Net income was $1,700.00 on revenue of $5,000.00 — a 34% net margin."),
    ])
    ans = orch.answer(_ctx(), "how did we do and what's my margin?")
    assert ans.verified is True and ans.refused is False
    assert "$1,700.00" in ans.text
    assert any(c.source_type == "report" for c in ans.citations)
    assert [s.tool for s in ans.trace] == ["ratios"]


def test_cash_position_sums_bank_accounts() -> None:
    # 1000 ($200,000.00) + 1010 ($5,000.00) = $205,000.00 → 20500000 minor
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text="You have $205,000.00 in cash across two accounts."),
    ])
    ans = orch.answer(_ctx(), "how much cash do I have?")
    assert ans.verified is True
    assert "$205,000.00" in ans.text


def test_debt_question_uses_debt_summary() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "debt_summary", {}),)),
        LLMTurn(final_text="You owe $20,000.00 across 2 loans; monthly debt service is $860.66."),
    ])
    ans = orch.answer(_ctx(), "what do I owe on my loans?")
    assert ans.verified is True
    assert "$20,000.00" in ans.text and "$860.66" in ans.text


# --- the backstop (load-bearing) ---------------------------------------------

def test_backstop_refuses_a_fabricated_figure() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "ratios", {}),)),
        LLMTurn(final_text="Your net income was $999,999.00."),   # not in any tool result
        LLMTurn(final_text="Still $999,999.00, trust me."),        # still wrong after correction
    ])
    ans = orch.answer(_ctx(), "what was net income?")
    assert ans.verified is False and ans.refused is True
    assert "$999,999.00" not in ans.text  # the fabricated figure never ships


def test_backstop_retries_then_accepts_a_corrected_figure() -> None:
    orch, llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "ratios", {}),)),
        LLMTurn(final_text="Net income was $999,999.00."),         # rejected
        LLMTurn(final_text="Correction: net income was $1,700.00."),  # verified
    ])
    ans = orch.answer(_ctx(), "what was net income?")
    assert ans.verified is True
    assert "$1,700.00" in ans.text
    assert llm.turns == []  # all three scripted turns consumed


def test_user_stated_amounts_may_be_echoed() -> None:
    # The $8,000 comes from the user's own question — echoing it is allowed even
    # though no tool returned it.
    orch, _llm = _orch([
        LLMTurn(final_text="A $8,000.00 hire is affordable given your position."),
    ])
    ans = orch.answer(_ctx(), "can I afford an $8,000 hire?")
    assert ans.verified is True


# --- permissions & safety ----------------------------------------------------

def test_catalog_is_filtered_to_caller_scopes() -> None:
    reg = default_registry()
    names = {str(t["name"]) for t in reg.catalog(frozenset({"ledger:read"}))}
    assert "cash_position" in names and "aging" in names
    assert "ratios" not in names and "debt_summary" not in names  # need reports:read


def test_calling_a_tool_without_permission_is_refused_not_executed() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "ratios", {}),)),  # caller lacks reports:read
        LLMTurn(final_text="I can't see your ratios with your current access."),
    ])
    ans = orch.answer(_ctx(perms=("ledger:read",)), "am I healthy?")
    assert ans.verified is True
    failed = [s for s in ans.trace if s.tool == "ratios" and not s.ok]
    assert len(failed) == 1 and "not available" in failed[0].summary


def test_a_bad_tool_argument_is_surfaced_not_crashed() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "aging", {"side": "sideways"}),)),
        LLMTurn(final_text="I need to know if you mean receivables or payables."),
    ])
    ans = orch.answer(_ctx(), "show me aging")
    failed = [s for s in ans.trace if s.tool == "aging" and not s.ok]
    assert len(failed) == 1


def test_out_of_scope_question_declines_cleanly() -> None:
    orch, _llm = _orch([
        LLMTurn(final_text="I can only answer from your books — I can't predict the market."),
    ])
    ans = orch.answer(_ctx(), "will the stock market crash?")
    assert ans.verified is True and "your books" in ans.text


# --- HttpLLM adapter shape ---------------------------------------------------

class _FakeHttp:
    def __init__(self, resp: dict[str, object]) -> None:
        self._resp = resp
        self.calls: list[dict[str, object]] = []

    def post_json(self, url: str, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
        self.calls.append({"url": url, "body": body, "headers": headers})
        return self._resp


def test_httpllm_parses_a_tool_use_response() -> None:
    http = _FakeHttp({"content": [
        {"type": "tool_use", "id": "tu_1", "name": "ratios", "input": {"as_of": "2026-08-19"}},
    ]})
    llm = HttpLLM(http, "sk-test")
    turn = llm.plan("sys", [Msg("user", "am I healthy?")], [{"name": "ratios"}])
    assert not turn.is_final
    assert turn.tool_calls[0].name == "ratios"
    assert turn.tool_calls[0].args == {"as_of": "2026-08-19"}
    headers = http.calls[0]["headers"]
    assert isinstance(headers, dict) and headers["x-api-key"] == "sk-test"


def test_httpllm_parses_a_final_text_response() -> None:
    http = _FakeHttp({"content": [{"type": "text", "text": "You're healthy."}]})
    turn = HttpLLM(http, "sk-test").plan("sys", [Msg("user", "?")], [])
    assert turn.is_final and turn.final_text == "You're healthy."
