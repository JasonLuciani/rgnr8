"""The LLM provider seam.

The model's only job is to choose tools and, at the end, write prose. It never
computes money. `LLMProvider.plan()` takes the system prompt, the running message
list, and the permission-filtered tool catalog, and returns an `LLMTurn`: either a
batch of tool calls or a final text answer.

`FakeLLM` replays a scripted sequence of turns — so the orchestrator, tools, and
the numeric backstop are all testable with zero network and full determinism.
`HttpLLM` is a shape-correct adapter over an injected HTTP client (Anthropic /
OpenAI style tool-use); binding a real client + key turns it live.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Msg:
    role: str  # "user" | "assistant" | "tool"
    content: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, object]


@dataclass(frozen=True)
class LLMTurn:
    """Exactly one of `tool_calls` (non-empty) or `final_text` is set."""

    tool_calls: tuple[ToolCall, ...] = ()
    final_text: str | None = None

    @property
    def is_final(self) -> bool:
        return self.final_text is not None


class LLMProvider(Protocol):
    def plan(
        self, system: str, messages: Sequence[Msg], tools: Sequence[dict[str, object]]
    ) -> LLMTurn: ...


@dataclass
class FakeLLM:
    """Replays scripted turns in order. Records what it was asked to plan."""

    turns: list[LLMTurn] = field(default_factory=list)
    seen: list[tuple[str, tuple[Msg, ...], tuple[str, ...]]] = field(default_factory=list)

    def plan(
        self, system: str, messages: Sequence[Msg], tools: Sequence[dict[str, object]]
    ) -> LLMTurn:
        self.seen.append(
            (system, tuple(messages), tuple(str(t.get("name", "")) for t in tools))
        )
        if not self.turns:
            # Nothing scripted → behave like a model that gives up cleanly.
            return LLMTurn(final_text="I don't have enough to answer that.")
        return self.turns.pop(0)


class HttpClient(Protocol):
    def post_json(
        self, url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]: ...


class HttpLLM:
    """Shape-correct Anthropic-style tool-use adapter. Needs an API key and a real
    HTTP client to go live; the request/response mapping lives here and is the only
    thing between this and a live conversational layer."""

    def __init__(
        self, http: HttpClient, api_key: str, *, model: str = "claude-sonnet-4",
        base_url: str = "https://api.anthropic.com",
    ) -> None:
        self._http = http
        self._key = api_key
        self._model = model
        self._base = base_url.rstrip("/")

    def plan(
        self, system: str, messages: Sequence[Msg], tools: Sequence[dict[str, object]]
    ) -> LLMTurn:
        body: dict[str, object] = {
            "model": self._model,
            "system": system,
            "max_tokens": 1024,
            "tools": list(tools),
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        resp = self._http.post_json(
            f"{self._base}/v1/messages",
            body,
            {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
        )
        return _parse_anthropic(resp)


def _parse_anthropic(resp: dict[str, object]) -> LLMTurn:
    """Map an Anthropic Messages response into an LLMTurn. Tool-use blocks become
    tool calls; otherwise the text blocks are joined into a final answer."""
    content = resp.get("content")
    blocks = content if isinstance(content, list) else []
    calls: list[ToolCall] = []
    texts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            args = b.get("input")
            calls.append(
                ToolCall(
                    id=str(b.get("id", "")),
                    name=str(b.get("name", "")),
                    args=args if isinstance(args, dict) else {},
                )
            )
        elif b.get("type") == "text":
            texts.append(str(b.get("text", "")))
    if calls:
        return LLMTurn(tool_calls=tuple(calls))
    return LLMTurn(final_text="".join(texts))


__all__ = [
    "Msg",
    "ToolCall",
    "LLMTurn",
    "LLMProvider",
    "FakeLLM",
    "HttpLLM",
    "HttpClient",
]
