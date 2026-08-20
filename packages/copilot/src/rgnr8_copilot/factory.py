"""Build a live LLM provider from a key.

Env-reading stays at the composition edge (deploy/run_local); this just assembles
the pieces — a real urllib client behind the Anthropic-shaped `HttpLLM`. Returns
`None` for a blank key so a caller can pass `anthropic_llm(os.environ.get(...))`
straight into `WebApp(ask_llm=...)` and have the feature stay off until a key
exists (same pattern as the QBO connect wiring).
"""

from __future__ import annotations

from .http import UrllibHttpClient
from .llm import HttpLLM


def anthropic_llm(
    api_key: str | None,
    *,
    model: str = "claude-sonnet-4",
    timeout: float = 30.0,
    base_url: str = "https://api.anthropic.com",
) -> HttpLLM | None:
    key = (api_key or "").strip()
    if not key:
        return None
    return HttpLLM(UrllibHttpClient(timeout=timeout), key, model=model, base_url=base_url)


__all__ = ["anthropic_llm"]
