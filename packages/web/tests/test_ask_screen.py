"""Ask RGNR8 — the conversational finance screen.

The screen is thin on purpose: it hands the question to the copilot and renders
what comes back. What these tests pin down is that the wiring is honest — the
copilot reads the *real* ledger through the same tenant-scoped client every other
screen uses (so the transport actually sees the read), the numeric-integrity
backstop is in force end-to-end (an unverified figure is refused, not shipped),
the answer carries its citations and the "computed from your books" footer, and
the surface degrades to "not configured" rather than 404 when no LLM is bound.

The LLM is a `FakeLLM` replaying scripted turns — zero network, fully
deterministic — so a test drives exactly the tool-use path it means to.
"""

from datetime import date
from typing import Any
from urllib.parse import urlencode

from rgnr8_copilot import FakeLLM, LLMTurn, ToolCall
from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    LedgerClient,
    LedgerResponse,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "ask-secret"
NOW = 1_760_000_000

# A trial balance with $50,000.00 sitting in cash account 1000. The cash_position
# tool sums the cash codes → total_cash_minor "5000000", which is what the answer
# is allowed to quote.
TRIAL_BALANCE = {
    "contract": "trial-balance/1",
    "rows": [
        {"code": "1000", "name": "Operating cash", "debit_minor": "5000000", "credit_minor": "0"},
        {"code": "4000", "name": "Revenue", "debit_minor": "0", "credit_minor": "5000000"},
    ],
}

RATIOS = {
    "contract": "financial-ratios/1", "as_of": "2026-08-31", "currency": "USD",
    "liquidity": {"current_ratio": "5.90", "health": "healthy"},
    "leverage": {"dscr": "0.19", "health": "at risk"},
    "profitability": {"net_margin_pct": "34.00", "health": "strong"},
}

ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/trial-balance": (200, TRIAL_BALANCE),
    "GET /t/acme/ratios": (200, RATIOS),
}


class FakeTransport:
    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, body))
        status, payload = self.routes.get(
            f"{method} {path.split('?')[0]}", (404, {"error": "not found"})
        )
        return LedgerResponse(status, payload)


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))
    )


def _app(llm: Any) -> tuple[WebApp, FakeTransport]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-view", "acme", Role.VIEWER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, ask_llm=llm)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    transport = FakeTransport(dict(ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport


def _req(app: WebApp, path: str, sub: str = "u-owner",
         method: str = "GET", form: dict[str, str] | None = None) -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    body = ""
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
        body = urlencode(form or {})
    return app.handle(Request(method, path, headers, body))


# --- unconfigured ------------------------------------------------------------

def test_ask_is_unavailable_without_an_llm() -> None:
    app, _t = _app(None)
    r = _req(app, "/t/acme/ask")
    assert r.status == 200
    assert "isn't configured" in r.body
    assert "computed by the ledger" in r.body


def test_ask_is_unavailable_without_a_ledger() -> None:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, ask_llm=FakeLLM())
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    r = _req(app, "/t/acme/ask")
    assert r.status == 200
    assert "No ledger service is configured" in r.body


# --- the empty form ----------------------------------------------------------

def test_ask_renders_the_prompt_and_chips() -> None:
    app, _t = _app(FakeLLM())
    r = _req(app, "/t/acme/ask")
    assert r.status == 200
    assert "Ask RGNR8" in r.body
    assert "cash and runway" in r.body           # a suggested chip
    assert 'method="post"' in r.body             # the ask form


def test_asking_with_no_text_prompts_for_a_question() -> None:
    app, _t = _app(FakeLLM())
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "   "})
    assert r.status == 200
    assert "Ask a question first" in r.body


# --- the happy path: a verified answer read from the books -------------------

def _cash_llm() -> FakeLLM:
    """Model plans one tool call (cash_position), then writes an answer that quotes
    only the figure the tool returned."""
    return FakeLLM(turns=[
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text="You have $50,000.00 in operating cash."),
    ])


def test_ask_answers_cash_from_the_ledger_and_ties_out() -> None:
    app, transport = _app(_cash_llm())
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    assert r.status == 200
    # the answer is the model's prose, carrying the tool's figure
    assert "$50,000.00" in r.body
    assert "operating cash" in r.body
    # it actually read the real ledger through the tenant-scoped client
    assert ("GET", "/t/acme/trial-balance", "") in transport.calls
    # the trust footer + citation are shown
    assert "Computed from your books" in r.body
    assert "1 source read" in r.body
    assert "Cash on hand" in r.body               # the citation label
    assert "every figure ties out" in r.body


def test_ask_echoes_the_question_back() -> None:
    app, _t = _app(_cash_llm())
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    assert "You asked:" in r.body
    assert "how much cash do I have?" in r.body


def test_ask_runs_a_get_query_from_a_chip() -> None:
    app, transport = _app(_cash_llm())
    r = _req(app, "/t/acme/ask?q=how+much+cash+do+I+have")
    assert r.status == 200
    assert "$50,000.00" in r.body
    assert any(c[1].startswith("/t/acme/trial-balance") for c in transport.calls)


# --- the backstop: an unverified figure is refused, not shipped --------------

def test_ask_refuses_a_figure_that_does_not_tie_to_the_books() -> None:
    # The model answers with a dollar amount no tool ever returned. The backstop
    # asks once to re-answer; the model repeats the bad figure; the surface refuses.
    llm = FakeLLM(turns=[
        LLMTurn(final_text="Your cash is about $88,888.00."),
        LLMTurn(final_text="Definitely $88,888.00."),
    ])
    app, _t = _app(llm)
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash?"})
    assert r.status == 200
    assert "$88,888.00" not in r.body                  # the guess never reaches the user
    assert "Couldn't verify against your books" in r.body
    assert "every figure ties out" not in r.body


# --- RBAC: the tool catalog is scoped to the caller --------------------------

def test_the_model_only_sees_tools_the_caller_may_run() -> None:
    llm = FakeLLM(turns=[LLMTurn(final_text="Looks healthy.")])
    app, _t = _app(llm)
    _req(app, "/t/acme/ask", method="POST", form={"q": "am I healthy?"})
    # FakeLLM records the tool names it was offered on each plan() call.
    assert llm.seen, "the model was asked to plan at least once"
    offered = set(llm.seen[0][2])
    # a full-access owner is offered the cross-segment core (ledger + reports + jobs)
    assert {"cash_position", "ratios", "job_profitability"} <= offered


def test_a_viewer_can_ask_and_read_the_books() -> None:
    # Every viewing role has ASK_CFO and read access (QBO-like transparency), so a
    # viewer gets the same read answer — the copilot is read-only in v1.
    app, transport = _app(_cash_llm())
    r = _req(app, "/t/acme/ask", method="POST", sub="u-view", form={"q": "cash?"})
    assert r.status == 200
    assert "$50,000.00" in r.body
    assert ("GET", "/t/acme/trial-balance", "") in transport.calls


# --- multi-turn conversation -------------------------------------------------

def _thread_llm() -> FakeLLM:
    """Turn 1 reads cash; turn 2 answers a follow-up by re-using the figure that
    tied out last turn — no second ledger read."""
    return FakeLLM(turns=[
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text="You have $50,000.00 in operating cash."),
        LLMTurn(final_text="Yes — $50,000.00 is a comfortable cushion."),
    ])


def test_the_thread_accumulates_and_follow_ups_carry_context() -> None:
    app, transport = _app(_thread_llm())
    _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "is that healthy?"})
    assert r.status == 200
    # both exchanges are rendered in the thread
    assert "how much cash do I have?" in r.body
    assert "is that healthy?" in r.body
    assert "comfortable cushion" in r.body
    assert r.body.count("You asked:") == 2
    # the follow-up re-used the verified figure without a second ledger read
    reads = [c for c in transport.calls if c[1].startswith("/t/acme/trial-balance")]
    assert len(reads) == 1


def test_follow_up_planning_sees_the_prior_transcript() -> None:
    llm = _thread_llm()
    app, _t = _app(llm)
    _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    _req(app, "/t/acme/ask", method="POST", form={"q": "is that healthy?"})
    # the last plan() call carried the prior question + answer as context
    seeded = llm.seen[-1][1]
    pairs = [(m.role, m.content) for m in seeded]
    assert ("user", "how much cash do I have?") in pairs
    assert any(m.role == "assistant" and "$50,000.00" in m.content for m in seeded)
    assert (seeded[-1].role, seeded[-1].content) == ("user", "is that healthy?")


def test_threads_are_isolated_per_caller() -> None:
    app, _t = _app(FakeLLM(turns=[
        LLMTurn(tool_calls=(ToolCall("c1", "cash_position", {}),)),
        LLMTurn(final_text="You have $50,000.00 in operating cash."),
    ]))
    # the owner asks; the viewer's thread must not show the owner's exchange
    _req(app, "/t/acme/ask", method="POST", sub="u-owner", form={"q": "owner cash?"})
    r = _req(app, "/t/acme/ask", sub="u-view")
    assert "owner cash?" not in r.body
    assert "You asked:" not in r.body


def test_clearing_the_conversation_starts_fresh() -> None:
    app, _t = _app(_thread_llm())
    _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    cleared = _req(app, "/t/acme/ask/clear", method="POST")
    assert cleared.status in (302, 303)
    r = _req(app, "/t/acme/ask")
    assert "You asked:" not in r.body
    assert "how much cash do I have?" not in r.body
    # a cleared, non-empty thread still offers the prompt + chips
    assert "Ask RGNR8" in r.body


def test_a_started_thread_shows_a_clear_control() -> None:
    app, _t = _app(_cash_llm())
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    assert "Clear conversation" in r.body
    assert "/t/acme/ask/clear" in r.body


def test_the_answer_shows_a_trace_of_the_tools_it_ran() -> None:
    app, _t = _app(_cash_llm())
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    assert "How I worked this out" in r.body
    assert "cash_position" in r.body            # the tool the copilot actually ran
    assert "Cash on hand" in r.body             # its one-line summary


# --- source-traceable working record (A-3) -----------------------------------

def test_the_answer_shows_a_working_record_tying_each_figure_to_its_source() -> None:
    app, _t = _app(_cash_llm())
    r = _req(app, "/t/acme/ask", method="POST", form={"q": "how much cash do I have?"})
    # the working record surfaces every figure and where it came from
    assert "Working record" in r.body
    assert "$50,000.00" in r.body
    assert "Cash on hand" in r.body            # the source label, tied to the figure
    # a report total has no single correctable row, so it says so rather than
    # inventing a "correct this" link
    assert "from a report total" in r.body


def test_a_register_figure_offers_a_correct_this_link_to_the_account() -> None:
    # account_register carries a "register" citation → the working record routes the
    # figure to /t/acme/books/accounts/1000 where the entry can be corrected.
    routes = dict(ROUTES)
    routes["GET /t/acme/accounts/1000/register"] = (
        200,
        {"code": "1000", "entries": [{"id": "je-1", "amount_minor": "5000000"}],
         "balance_minor": "5000000"},
    )
    llm = FakeLLM(turns=[
        LLMTurn(tool_calls=(ToolCall("c1", "account_register", {"code": "1000"}),)),
        LLMTurn(final_text="Account 1000 holds $50,000.00."),
    ])
    app, transport = _app(llm)
    transport.routes = routes
    r = _req(app, "/t/acme/ask", method="POST",
             form={"q": "show the entries behind account 1000"})
    assert r.status == 200
    assert "Working record" in r.body
    assert "Correct this" in r.body
    assert "/t/acme/books/accounts/1000" in r.body
