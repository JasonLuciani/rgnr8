"""12-factor configuration — one validated `Settings` from the environment.

Every deployment knob is read from environment variables into a frozen
`Settings`, validated up front so a misconfiguration fails at boot with a clear
message rather than at the first request. Secrets stay in the environment/secret
store; nothing is hard-coded. `from_env` takes an explicit mapping (default
`os.environ`) so it's deterministic and unit-testable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(Exception):
    """A missing or contradictory setting — raised at startup, never at runtime."""


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, slots=True)
class Settings:
    # --- web / auth ---
    auth_mode: str = "hs256"  # "hs256" | "jwks" | "static"
    jwt_secret: str | None = None
    jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    tenant_claim: str = "tenant"
    port: int = 8080

    # --- database ---
    database_url: str | None = None  # None → in-memory (dev only)

    # --- delivery (optional until you turn it on) ---
    sendgrid_api_key: str | None = None
    fcm_api_key: str | None = None
    delivery_from: str = "briefings@rgnr8.app"

    # --- connectors / QBO (optional until you connect data) ---
    plaid_client_id: str | None = None
    plaid_secret: str | None = None
    gusto_api_key: str | None = None
    qbo_client_id: str | None = None
    qbo_client_secret: str | None = None

    # non-secret derived flags
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_production_db(self) -> bool:
        return self.database_url is not None

    @property
    def placeholder(self) -> str:
        """DB-API parameter marker for the configured database."""
        url = (self.database_url or "").lower()
        return "%s" if url.startswith(("postgres://", "postgresql://")) else "?"

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> "Settings":
        e = env if env is not None else os.environ
        auth_mode = (e.get("RGNR8_AUTH_MODE") or "hs256").strip().lower()
        if auth_mode not in ("hs256", "jwks", "static"):
            raise ConfigError(f"RGNR8_AUTH_MODE must be hs256|jwks|static, got {auth_mode!r}")

        jwt_secret = e.get("RGNR8_JWT_SECRET")
        jwks_url = e.get("RGNR8_JWKS_URL")
        issuer = e.get("RGNR8_JWT_ISSUER")
        audience = e.get("RGNR8_JWT_AUDIENCE")

        if auth_mode == "hs256" and not jwt_secret:
            raise ConfigError("RGNR8_AUTH_MODE=hs256 requires RGNR8_JWT_SECRET")
        if auth_mode == "jwks":
            missing = [k for k, v in (("RGNR8_JWKS_URL", jwks_url),
                                      ("RGNR8_JWT_ISSUER", issuer),
                                      ("RGNR8_JWT_AUDIENCE", audience)) if not v]
            if missing:
                raise ConfigError(f"RGNR8_AUTH_MODE=jwks requires {', '.join(missing)}")

        warnings: list[str] = []
        database_url = e.get("RGNR8_DATABASE_URL")
        if not database_url:
            warnings.append("no RGNR8_DATABASE_URL — using in-memory storage (dev only, not durable)")

        try:
            port = int(e.get("RGNR8_PORT", "8080"))
        except ValueError as exc:
            raise ConfigError("RGNR8_PORT must be an integer") from exc

        return Settings(
            auth_mode=auth_mode,
            jwt_secret=jwt_secret,
            jwks_url=jwks_url,
            jwt_issuer=issuer,
            jwt_audience=audience,
            tenant_claim=e.get("RGNR8_TENANT_CLAIM", "tenant"),
            port=port,
            database_url=database_url,
            sendgrid_api_key=e.get("RGNR8_SENDGRID_API_KEY"),
            fcm_api_key=e.get("RGNR8_FCM_API_KEY"),
            delivery_from=e.get("RGNR8_DELIVERY_FROM", "briefings@rgnr8.app"),
            plaid_client_id=e.get("RGNR8_PLAID_CLIENT_ID"),
            plaid_secret=e.get("RGNR8_PLAID_SECRET"),
            gusto_api_key=e.get("RGNR8_GUSTO_API_KEY"),
            qbo_client_id=e.get("RGNR8_QBO_CLIENT_ID"),
            qbo_client_secret=e.get("RGNR8_QBO_CLIENT_SECRET"),
            warnings=tuple(warnings),
        )

    def redacted(self) -> dict[str, object]:
        """A safe-to-log view: secrets shown only as present/absent."""
        def has(v: object) -> str:
            return "set" if v else "unset"

        return {
            "auth_mode": self.auth_mode,
            "database": "postgres" if self.is_production_db else "in-memory",
            "jwt_secret": has(self.jwt_secret),
            "jwks_url": self.jwks_url or "unset",
            "issuer": self.jwt_issuer or "unset",
            "audience": self.jwt_audience or "unset",
            "sendgrid": has(self.sendgrid_api_key),
            "fcm": has(self.fcm_api_key),
            "plaid": has(self.plaid_client_id and self.plaid_secret),
            "gusto": has(self.gusto_api_key),
            "qbo": has(self.qbo_client_id and self.qbo_client_secret),
            "port": self.port,
            "warnings": list(self.warnings),
        }
