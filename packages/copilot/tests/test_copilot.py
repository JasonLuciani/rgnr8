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
    Conversation,
    FakeLedgerReader,
    FakeLLM,
    HttpError,
    HttpLLM,
    LLMTurn,
    Msg,
    SYSTEM_PROMPT,
    ToolCall,
    UrllibHttpClient,
    anthropic_llm,
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


JOB_COST: dict[str, object] = {
    "job_id": "harper", "name": "Harper kitchen",
    "billed_minor": "11800000", "cost_minor": "9240000", "margin_minor": "2560000",
    "margin_pct": "21.70",
}

INVENTORY: dict[str, object] = {
    "contract": "inventory-valuation/1",
    "totals": {"items_value_minor": "9040000", "ledger_balance_minor": "9040000",
               "difference_minor": "0", "ties_out": True},
    "reorder": [{"sku": "PLY-34", "quantity_milli": "18000", "reorder_point_milli": "20000"}],
}

CONSOLIDATED: dict[str, object] = {
    "group": {"id": "harper-holdings"}, "balanced": True,
    "consolidated": {"revenue_minor": "16900000", "net_income_minor": "2480000"},
    "eliminations": [{"account_code": "1900", "signed_minor": "-41000000"}],
}


def _reader() -> FakeLedgerReader:
    return (FakeLedgerReader()
            .on("/ratios", RATIOS)
            .on("/trial-balance", TRIAL_BALANCE)
            .on("/debt", DEBT)
            .on("/aging", {"side": "ar", "buckets": []})
            .on("/jobs/harper/cost", JOB_COST)
            .on("/wip", {"rows": [], "totals": {"cost_to_date_minor": "9240000"}})
            .on("/inventory", INVENTORY)
            .on("/consolidation/groups/harper-holdings/report", CONSOLIDATED))


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


# --- source-traceable working record (A-3) -----------------------------------

def test_answer_carries_a_per_figure_working_record_tied_to_sources() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "ratios", {}),)),
        LLMTurn(final_text="Net income was $1,700.00 on revenue of $5,000.00."),
    ])
    ans = orch.answer(_ctx(), "how did we do?")
    assert ans.verified is True
    # Every dollar figure in the answer appears in the working record, in order,
    # each tied to the report that produced it.
    displays = [f.display for f in ans.working_record]
    assert displays == ["$1,700.00", "$5,000.00"]
    for fig in ans.working_record:
        assert fig.sources, f"{fig.display} has no source"
        assert fig.sources[0].source_type == "report"
        assert fig.sources[0].ref == "/ratios"


def test_working_record_routes_a_register_figure_to_its_entry_for_correction() -> None:
    reader = _reader().on(
        "/accounts/1000/register",
        {"code": "1000", "entries": [{"id": "je-1", "amount_minor": "20000000"}],
         "balance_minor": "20000000"},
    )
    ctx = AskContext(tenant="acme", permissions=frozenset({"ledger:read"}), ledger=reader)
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "account_register", {"code": "1000"}),)),
        LLMTurn(final_text="Account 1000 holds $200,000.00."),
    ])
    ans = orch.answer(ctx, "show the entries behind account 1000")
    assert ans.verified is True
    fig = next(f for f in ans.working_record if f.display == "$200,000.00")
    assert fig.correct_kind == "register"
    assert fig.correct_href == "/accounts/1000/register"


def test_working_record_marks_a_user_stated_figure_as_stated() -> None:
    orch, _llm = _orch([
        LLMTurn(final_text="A $8,000.00 hire is affordable."),
    ])
    ans = orch.answer(_ctx(), "can I afford an $8,000 hire?")
    fig = next(f for f in ans.working_record if f.display == "$8,000.00")
    assert fig.stated is True and fig.sources == ()


def test_working_record_provenance_survives_into_a_follow_up_turn() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "ratios", {}),)),
        LLMTurn(final_text="Net income was $1,700.00."),
        LLMTurn(final_text="Yes — still $1,700.00 this period."),  # follow-up, no new tool
    ])
    ans1, convo = orch.converse(_ctx(), Conversation.empty(), "what was net income?")
    ans2, _convo2 = orch.converse(_ctx(), convo, "are you sure?")
    assert ans2.verified is True
    fig = next(f for f in ans2.working_record if f.display == "$1,700.00")
    assert fig.sources and fig.sources[0].ref == "/ratios"  # carried source, no re-run


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


# --- segment tools (all five segments) ---------------------------------------

def test_contractor_job_profitability_ties_out() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "job_profitability", {"job_id": "harper"}),)),
        LLMTurn(final_text="Harper kitchen: billed $118,000.00, cost $92,400.00 — margin 21.7%."),
    ])
    ans = orch.answer(_ctx(perms=("jobs:read", "reports:read")), "did the Harper job make money?")
    assert ans.verified is True
    assert "$118,000.00" in ans.text and "$92,400.00" in ans.text
    assert ans.trace[0].tool == "job_profitability"


def test_ecommerce_inventory_tie_out() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "inventory_valuation", {}),)),
        LLMTurn(final_text="Inventory is worth $90,400.00 and it ties to the balance sheet."),
    ])
    ans = orch.answer(_ctx(), "what's my inventory worth and does it match the books?")
    assert ans.verified is True
    assert "$90,400.00" in ans.text


def test_multientity_consolidated_pnl() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "consolidated_pnl", {"group": "harper-holdings"}),)),
        LLMTurn(final_text="Consolidated revenue $169,000.00, net income $24,800.00 after eliminations."),
    ])
    ans = orch.answer(_ctx(), "what's our consolidated profit after eliminations?")
    assert ans.verified is True
    assert "$169,000.00" in ans.text and "$24,800.00" in ans.text


def test_segment_tools_are_scope_gated() -> None:
    reg = default_registry()
    # a viewer with only reports:read gets inventory/consolidation but not job tools
    names = {str(t["name"]) for t in reg.catalog(frozenset({"reports:read"}))}
    assert "inventory_valuation" in names and "consolidated_pnl" in names
    assert "job_profitability" not in names and "backlog" not in names  # need jobs:read


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


# --- multi-turn conversation -------------------------------------------------
# TRIAL_BALANCE cash codes 1000 ($200,000.00) + 1010 ($5,000.00) → $205,000.00.

_CASH = "$205,000.00"


def test_converse_threads_the_prior_transcript_into_the_next_turn() -> None:
    orch, llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text=f"You have {_CASH} on hand."),
        LLMTurn(final_text=f"Yes — {_CASH} is a comfortable cushion."),
    ])
    ans1, conv1 = orch.converse(_ctx(), Conversation.empty(), "how much cash do I have?")
    ans2, conv2 = orch.converse(_ctx(), conv1, "is that healthy?")

    assert ans1.verified and _CASH in ans1.text
    # the follow-up re-uses a figure verified last turn — no tool call this turn —
    # and is accepted because the carried state remembers it tied to the books.
    assert ans2.verified and not ans2.refused and _CASH in ans2.text

    # the prior clean transcript was seeded into the follow-up's planning context
    seeded = llm.seen[-1][1]
    pairs = [(m.role, m.content) for m in seeded]
    assert ("user", "how much cash do I have?") in pairs
    assert any(m.role == "assistant" and _CASH in m.content for m in seeded)
    assert (seeded[-1].role, seeded[-1].content) == ("user", "is that healthy?")

    # the carried transcript holds both clean turns and no tool traffic
    assert [m.role for m in conv2.messages] == ["user", "assistant", "user", "assistant"]
    assert all(m.role != "tool" for m in conv2.messages)


def test_a_new_unverified_figure_is_refused_even_inside_a_thread() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text=f"You have {_CASH} on hand."),
        LLMTurn(final_text="Next year you'll have $999,999.00."),
        LLMTurn(final_text="Definitely $999,999.00."),
    ])
    _ans1, conv1 = orch.converse(_ctx(), Conversation.empty(), "how much cash?")
    ans2, conv2 = orch.converse(_ctx(), conv1, "what will it be next year?")

    assert ans2.refused and not ans2.verified
    assert "$999,999.00" not in ans2.text
    # the refusal is still recorded in the thread, and the fabricated figure never
    # enters the carried set of book-tied figures.
    assert conv2.messages[-1].role == "assistant"
    assert conv2.verified_minor == conv1.verified_minor


def test_the_carried_transcript_is_trimmed_to_max_turns() -> None:
    llm = FakeLLM(turns=[
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text=f"You have {_CASH}."),
        LLMTurn(final_text=f"Still {_CASH}."),
    ])
    orch = AskOrchestrator(llm, default_registry(), max_turns=1)
    _a1, conv1 = orch.converse(_ctx(), Conversation.empty(), "cash now?")
    ans2, conv2 = orch.converse(_ctx(), conv1, "cash still?")

    assert ans2.verified and _CASH in ans2.text
    # only the most recent turn is retained (2 messages = 1 user+assistant pair)…
    assert [m.content for m in conv2.messages] == ["cash still?", f"Still {_CASH}."]
    # …but a figure verified in the trimmed-away turn stays quotable.
    assert 20_500_000 in conv2.verified_minor


def test_answer_is_still_single_turn_and_stateless() -> None:
    orch, _llm = _orch([
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text=f"You have {_CASH}."),
    ])
    ans = orch.answer(_ctx(), "cash?")
    assert ans.verified and _CASH in ans.text


def test_the_system_prompt_is_grounded_with_date_and_business() -> None:
    orch, llm = _orch([LLMTurn(final_text="Sure.")])
    ctx = AskContext(tenant="acme", permissions=frozenset({"reports:read"}),
                     ledger=_reader(), hints={"today": "2026-08-31", "business": "Acme Co"})
    orch.answer(ctx, "how did we do last quarter?")
    system = llm.seen[0][0]
    assert "today is 2026-08-31" in system
    assert "Acme Co" in system
    assert "last quarter" in system  # the instruction to resolve periods to dates


def test_the_system_prompt_is_unchanged_without_hints() -> None:
    orch, llm = _orch([LLMTurn(final_text="Sure.")])
    ctx = AskContext(tenant="acme", permissions=frozenset({"reports:read"}),
                     ledger=_reader(), hints={})
    orch.answer(ctx, "hello?")
    assert llm.seen[0][0] == SYSTEM_PROMPT


# --- the concrete urllib client + env factory --------------------------------

import io
import json
import urllib.error
import urllib.request

import pytest


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_urllib_client_posts_json_and_parses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["data"] = req.data
        seen["key"] = req.get_header("X-api-key")
        return _FakeResp(b'{"content": [{"type": "text", "text": "hi"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = UrllibHttpClient().post_json(
        "https://api.anthropic.com/v1/messages", {"model": "m"}, {"x-api-key": "sk-live"},
    )
    assert out["content"] == [{"type": "text", "text": "hi"}]
    assert seen["method"] == "POST"
    assert seen["key"] == "sk-live"
    assert isinstance(seen["data"], (bytes, bytearray))
    assert json.loads(seen["data"]) == {"model": "m"}


def test_urllib_client_surfaces_an_http_error_with_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        raise urllib.error.HTTPError(
            "https://x", 429, "Too Many Requests", {}, io.BytesIO(b"rate limited"),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(HttpError) as caught:
        UrllibHttpClient().post_json("https://x", {}, {})
    assert caught.value.status == 429
    assert "rate limited" in str(caught.value)


def test_urllib_client_rejects_a_non_object_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    def arr(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        return _FakeResp(b"[1, 2, 3]")

    monkeypatch.setattr(urllib.request, "urlopen", arr)
    with pytest.raises(HttpError):
        UrllibHttpClient().post_json("https://x", {}, {})


def test_anthropic_factory_is_off_without_a_key() -> None:
    assert anthropic_llm(None) is None
    assert anthropic_llm("   ") is None


def test_anthropic_factory_builds_a_live_provider_with_a_key() -> None:
    llm = anthropic_llm("sk-live", model="claude-x")
    assert isinstance(llm, HttpLLM)


def test_factory_provider_plans_over_a_stubbed_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        return _FakeResp(b'{"content": [{"type": "text", "text": "healthy"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    llm = anthropic_llm("sk-live")
    assert llm is not None
    turn = llm.plan("sys", [Msg("user", "am I healthy?")], [])
    assert turn.is_final and turn.final_text == "healthy"
