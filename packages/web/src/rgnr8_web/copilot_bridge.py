"""Bridge between the web app and the Ask RGNR8 copilot.

Three small pieces: a `LedgerReader` adapter over the tenant-scoped `LedgerClient`
(so the copilot's tools read the real ledger through the same RLS-enforced path
every screen uses); a mapping from the caller's RBAC permissions to the copilot's
tool scopes (so a viewer only gets tools they're allowed to run); and an
`AskService` that holds the orchestrator. The LLM provider is injected — bind a
real `HttpLLM` in production, a `FakeLLM` in tests; `None` turns the feature off.
"""

from __future__ import annotations

from collections.abc import Mapping

from rgnr8_copilot import (
    AskAnswer,
    AskContext,
    AskOrchestrator,
    Conversation,
    LedgerReadError,
    LLMProvider,
    default_registry,
)

from .ledger_client import LedgerClient
from .rbac import Permission


class LedgerReaderAdapter:
    """Implements the copilot's `LedgerReader` over a `LedgerClient`, pinned to one
    tenant. No path can widen the tenant — isolation is the client's job (and the
    database's, via RLS)."""

    def __init__(self, client: LedgerClient, tenant: str) -> None:
        self._client = client
        self._tenant = tenant

    def read(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        resp = self._client.get(self._tenant, path, dict(params))
        if not resp.ok:
            raise LedgerReadError(resp.error())
        return dict(resp.body)


def copilot_scopes(permissions: frozenset[Permission], *, rbac_on: bool) -> frozenset[str]:
    """Map RBAC permissions to the copilot's tool scopes. With RBAC off (no policy
    configured) the caller gets every read scope."""
    if not rbac_on:
        return frozenset({"ledger:read", "reports:read", "jobs:read"})
    scopes: set[str] = set()
    if Permission.VIEW_TRANSACTIONS in permissions:
        scopes.update({"ledger:read", "jobs:read"})
    if Permission.VIEW_CASH in permissions or Permission.VIEW_PACKAGE in permissions:
        scopes.add("reports:read")
    return frozenset(scopes)


class AskService:
    """Answers a question for a tenant. Stateless per call; the LLM provider and
    tool registry are fixed at construction."""

    def __init__(self, llm: LLMProvider) -> None:
        self._orch = AskOrchestrator(llm, default_registry())

    def answer(
        self,
        *,
        tenant: str,
        scopes: frozenset[str],
        reader: LedgerReaderAdapter,
        hints: dict[str, object],
        question: str,
    ) -> AskAnswer:
        ctx = AskContext(tenant=tenant, permissions=scopes, ledger=reader, hints=hints)
        return self._orch.answer(ctx, question)

    def converse(
        self,
        *,
        tenant: str,
        scopes: frozenset[str],
        reader: LedgerReaderAdapter,
        hints: dict[str, object],
        conversation: Conversation,
        question: str,
    ) -> tuple[AskAnswer, Conversation]:
        """Multi-turn: answer `question` in the context of `conversation`, returning
        the answer and the conversation to carry into the next turn."""
        ctx = AskContext(tenant=tenant, permissions=scopes, ledger=reader, hints=hints)
        return self._orch.converse(ctx, conversation, question)


__all__ = ["LedgerReaderAdapter", "copilot_scopes", "AskService"]
