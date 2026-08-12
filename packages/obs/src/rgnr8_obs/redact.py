"""Secret redaction — one shared rule so logs and error reports can't diverge.

Observability payloads are the classic place secrets leak: someone logs a request
context or attaches auth headers to an error report and the token lands in a log
aggregator forever. Rather than trusting every call site to remember, both the
logger and the error reporter funnel their user-supplied fields through
`redact_fields` here, so the redaction policy lives in exactly one place.

The rule is deliberately blunt: a *case-insensitive substring* match of the key
against a set of secret-ish tokens. Blunt beats clever — false positives (a
redacted "monkey" because it contains "key") are harmless, a missed token is not.
"""

from __future__ import annotations

from typing import Mapping

REDACTED = "***"

# Case-insensitive substrings; any key containing one of these is redacted.
DEFAULT_SECRET_PATTERNS: frozenset[str] = frozenset(
    {
        "secret",
        "token",
        "password",
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "key",
    }
)


def is_secret_key(key: str, patterns: frozenset[str] = DEFAULT_SECRET_PATTERNS) -> bool:
    """True if `key` looks like it names a secret (case-insensitive contains)."""
    lowered = key.lower()
    return any(pattern in lowered for pattern in patterns)


def redact_fields(
    fields: Mapping[str, object],
    patterns: frozenset[str] = DEFAULT_SECRET_PATTERNS,
) -> dict[str, object]:
    """Return a copy of `fields` with any secret-named value replaced by `***`."""
    return {
        key: (REDACTED if is_secret_key(key, patterns) else value)
        for key, value in fields.items()
    }
