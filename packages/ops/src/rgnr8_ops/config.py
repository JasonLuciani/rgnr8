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

    # --- browser login / sessions ---
    # A session secret enables self-issued session cookies so browser owners can
    # log in with a password (with a credential store wired). In jwks (SSO) mode
    # leave this unset and authenticate through the IdP.
    session_secret: str | None = None
    # Session cookies carry `Secure` (HTTPS-only) by default; set False only for
    # local dev over plain http. Trust X-Forwarded-For only behind a known proxy.
    secure_cookies: bool = True
    trust_forwarded_for: bool = False

    # --- database ---
    database_url: str | None = None  # None → in-memory (dev only)

    # --- ledger service (the accounting system of record) ---
    ledger_url: str | None = None    # RGNR8_LEDGER_URL; absent → books screens say "not configured"
    ledger_token: str | None = None  # RGNR8_LEDGER_TOKEN

    # --- secrets ---
    secret_key: str | None = None    # RGNR8_SECRET_KEY (Fernet key for at-rest encryption)

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
    qbo_redirect_uri: str | None = None
    qbo_environment: str = "sandbox"  # "sandbox" | "production"

    # --- Ask RGNR8 (conversational layer, optional) ---
    anthropic_api_key: str | None = None
    ask_model: str = "claude-sonnet-4"

    # non-secret derived flags
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_production_db(self) -> bool:
        return self.database_url is not None

    @property
    def qbo_enabled(self) -> bool:
        return bool(self.qbo_client_id and self.qbo_client_secret)

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

        session_secret = e.get("RGNR8_SESSION_SECRET")
        # Browser login needs a session secret unless we're on a real IdP (jwks).
        if auth_mode != "jwks" and database_url and not session_secret:
            warnings.append(
                "no RGNR8_SESSION_SECRET — browser login is disabled (API bearer tokens only)"
            )
        ledger_url = e.get("RGNR8_LEDGER_URL")
        if database_url and not ledger_url:
            warnings.append(
                "no RGNR8_LEDGER_URL — the books/ledger screens will show 'not configured'"
            )

        qbo_id = e.get("RGNR8_QBO_CLIENT_ID")
        qbo_secret = e.get("RGNR8_QBO_CLIENT_SECRET")
        secret_key = e.get("RGNR8_SECRET_KEY")
        # Fail closed: never write external OAuth credentials to the DB in cleartext.
        if qbo_id and qbo_secret and database_url and not secret_key:
            raise ConfigError(
                "QBO is enabled (RGNR8_QBO_CLIENT_ID/SECRET) with a database but no "
                "RGNR8_SECRET_KEY — refusing to store OAuth tokens in cleartext. Set "
                "RGNR8_SECRET_KEY to a Fernet key."
            )

        return Settings(
            auth_mode=auth_mode,
            jwt_secret=jwt_secret,
            jwks_url=jwks_url,
            jwt_issuer=issuer,
            jwt_audience=audience,
            tenant_claim=e.get("RGNR8_TENANT_CLAIM", "tenant"),
            port=port,
            session_secret=session_secret,
            secure_cookies=_bool(e.get("RGNR8_SECURE_COOKIES"), default=True),
            trust_forwarded_for=_bool(e.get("RGNR8_TRUST_FORWARDED_FOR"), default=False),
            database_url=database_url,
            ledger_url=ledger_url,
            ledger_token=e.get("RGNR8_LEDGER_TOKEN"),
            secret_key=secret_key,
            sendgrid_api_key=e.get("RGNR8_SENDGRID_API_KEY"),
            fcm_api_key=e.get("RGNR8_FCM_API_KEY"),
            delivery_from=e.get("RGNR8_DELIVERY_FROM", "briefings@rgnr8.app"),
            plaid_client_id=e.get("RGNR8_PLAID_CLIENT_ID"),
            plaid_secret=e.get("RGNR8_PLAID_SECRET"),
            gusto_api_key=e.get("RGNR8_GUSTO_API_KEY"),
            qbo_client_id=qbo_id,
            qbo_client_secret=qbo_secret,
            qbo_redirect_uri=e.get("RGNR8_QBO_REDIRECT_URI"),
            qbo_environment=(e.get("RGNR8_QBO_ENVIRONMENT") or "sandbox").strip().lower(),
            anthropic_api_key=e.get("RGNR8_ANTHROPIC_API_KEY") or e.get("ANTHROPIC_API_KEY"),
            ask_model=e.get("RGNR8_ASK_MODEL", "claude-sonnet-4"),
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
            "session_secret": has(self.session_secret),
            "secure_cookies": self.secure_cookies,
            "trust_forwarded_for": self.trust_forwarded_for,
            "ledger": self.ledger_url or "unset",
            "ledger_token": has(self.ledger_token),
            "secret_key": has(self.secret_key),
            "sendgrid": has(self.sendgrid_api_key),
            "fcm": has(self.fcm_api_key),
            "plaid": has(self.plaid_client_id and self.plaid_secret),
            "gusto": has(self.gusto_api_key),
            "qbo": has(self.qbo_client_id and self.qbo_client_secret),
            "qbo_env": self.qbo_environment,
            "ask": has(self.anthropic_api_key),
            "port": self.port,
            "warnings": list(self.warnings),
        }
