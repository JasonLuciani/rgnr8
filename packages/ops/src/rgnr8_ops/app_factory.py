"""Compose the production web application from `Settings`.

`build_web_app` wires the framework-free `WebApp` with the authenticator the
config selects (static token / HS256 session / real-IdP JWKS), the tenant store
(Postgres when a DSN is set, else in-memory for dev), and the published-package
reader — then, once loaded onto a `Fleet`, registers every persisted tenant so
the routes resolve. `create_application` turns that into a WSGI callable for
gunicorn/uwsgi, and `readiness` adds a real DB round-trip to the `/ready` signal.
"""

from __future__ import annotations

import secrets
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

from rgnr8_obs import (
    ErrorReporter,
    InMemoryErrorReporter,
    MetricsRegistry,
    StreamLogSink,
    StructuredLogger,
)
from rgnr8_briefing import HttpEmailTransport, UrllibHttpClient
from rgnr8_runtime.subscriptions import SqlSubscriptionStore
from rgnr8_copilot import anthropic_llm
from rgnr8_qbo import (
    ConnectionStore,
    InMemoryConnectionStore,
    InMemoryNonceStore,
    NonceStore,
    QboConnectService,
    QboEnvironment,
    QboOAuthConfig,
    SqlConnectionStore,
    SqlNonceStore,
    cipher_from_env,
)
from rgnr8_qbo import UrllibHttpClient as QboHttpClient
from rgnr8_web import (
    AuditSink,
    AuthService,
    FinancialPackageReader,
    HttpJwksProvider,
    InMemoryAuditLog,
    InMemoryCredentialStore,
    InMemoryInvitationStore,
    InMemoryTenantStore,
    InMemoryUserDirectory,
    InvitationService,
    InvitationStore,
    JwksAuthenticator,
    JwtAuthenticator,
    LedgerClient,
    RateLimiter,
    make_rate_limit_key,
    SqlAuditLog,
    SqlCredentialStore,
    SqlInvitationStore,
    SqlTenantStore,
    SqlUserDirectory,
    StaticTokenAuthenticator,
    UrllibJwksSource,
    UrllibTransport,
    UserDirectory,
    WebApp,
    wsgi_app,
)

from datetime import datetime, timezone

from .account_mail import account_emailer
from .config import ConfigError, Settings
from .ddl_lock import ddl_bootstrap_lock
from .fleet import Fleet
from .onboarding import OnboardingRegistry, SqlOnboardingRegistry
from .operator_app import OperatorApp, operator_wsgi
from .provisioning import Provisioning, build_provisioning
from .store import SqlFleetStore


def build_authenticator(settings: Settings, *, clock: Callable[[], int] | None = None) -> Any:
    """Pick the authenticator the config asks for. `clock` is injectable so the
    JWKS/HS256 verifiers stay deterministic under test."""
    if settings.auth_mode == "static":
        return StaticTokenAuthenticator({})  # tokens registered per-tenant by the fleet
    if settings.auth_mode == "hs256":
        assert settings.jwt_secret is not None  # enforced by Settings.from_env
        return JwtAuthenticator(settings.jwt_secret, tenant_claim=settings.tenant_claim, clock=clock)
    if settings.auth_mode == "jwks":
        assert settings.jwks_url is not None
        provider = HttpJwksProvider(settings.jwks_url, UrllibJwksSource(), clock=clock)
        return JwksAuthenticator(
            provider,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            tenant_claim=settings.tenant_claim,
            clock=clock,
        )
    raise ConfigError(f"unknown auth_mode {settings.auth_mode!r}")


def build_web_app(
    settings: Settings,
    *,
    conn: object | None = None,
    packages: FinancialPackageReader | None = None,
    clock: Callable[[], int] | None = None,
    usage_recorder: "Callable[[str, str, int], None] | None" = None,
) -> WebApp:
    """A `WebApp` wired from settings. Pass a DB-API connection for the durable
    tenant store; omit it (dev) for in-memory. ``usage_recorder`` (tenant, kind,
    quantity) meters into billing when the provisioning seam is wired."""
    if conn is not None:
        store: object = SqlTenantStore(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
    else:
        store = InMemoryTenantStore()
    # Wire a real user directory + RBAC whenever a real authenticator is in play
    # (hs256/jwks). Without this the composed app ran policy=None → every
    # principal owner-equivalent (pre-launch review). `static` mode is dev-only
    # and stays tenant-scoped.
    users: UserDirectory | None = None
    require_rbac = settings.auth_mode != "static"
    if require_rbac:
        users = (SqlUserDirectory(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
                 if conn is not None else InMemoryUserDirectory())

    # --- browser login ---------------------------------------------------
    # A session secret enables self-issued session cookies; a credential store
    # makes POST /login a real password check (not just an email). In jwks (SSO)
    # mode leave the session secret unset and authenticate through the IdP.
    session_secret = settings.session_secret
    credentials = None
    auth_service = None
    if session_secret:
        credentials = (
            SqlCredentialStore(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
            if conn is not None else InMemoryCredentialStore()
        )
        if isinstance(credentials, SqlCredentialStore):
            credentials.create_schema()
        auth_service = AuthService(credentials=credentials)

    # --- invitations -----------------------------------------------------
    # The gate on account creation. Wired whenever there is a directory to grant
    # membership in AND a signup path to gate; without it `WebApp` refuses every
    # un-invited signup, so a production composition that forgot this fails
    # closed (no accounts) rather than open (anyone can register).
    invitations = None
    if users is not None and auth_service is not None:
        inv_store: InvitationStore
        if conn is not None:
            sql_invites = SqlInvitationStore(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
            sql_invites.create_schema()
            inv_store = sql_invites
        else:
            inv_store = InMemoryInvitationStore()
        invitations = InvitationService(users, inv_store)

    # --- account email ---------------------------------------------------
    # Delivers the invitation / verify / password-reset links. Off unless BOTH a
    # provider key and a public base URL are configured (see
    # `Settings.account_mail_enabled`); off means the tokens still exist and the
    # owner copies invite links from the team page, which is how the beta runs
    # until SendGrid is wired.
    emailer = None
    if settings.account_mail_enabled:
        assert settings.sendgrid_api_key is not None and settings.public_base_url is not None
        emailer = account_emailer(
            HttpEmailTransport(UrllibHttpClient(), api_key=settings.sendgrid_api_key,
                               from_email=settings.delivery_from),
            settings.public_base_url,
        )

    # --- QuickBooks Online connect (optional) ----------------------------
    # Only wired when the Intuit app credentials are present. Tokens are
    # encrypted at rest via the Fernet key (config.from_env fails closed when a
    # DB is configured without one). State signing needs a secret; without any
    # available secret the connect surface stays "not configured".
    qbo = None
    qbo_state_secret = session_secret or settings.jwt_secret or settings.secret_key
    if settings.qbo_enabled and qbo_state_secret:
        assert settings.qbo_client_id is not None and settings.qbo_client_secret is not None
        # OAuth `state` replay defense. With a DB, use the durable cross-worker
        # SqlNonceStore so a replayed state is caught even when the callback lands
        # on a different web worker than the one that minted it; without a DB
        # (single-process dev) the in-process default is fine.
        nonce_store: NonceStore
        if conn is not None:
            # At-rest encryption for tokens; config.from_env fails closed when a DB
            # is configured without RGNR8_SECRET_KEY, so the key is present here.
            cipher = cipher_from_env({"RGNR8_SECRET_KEY": settings.secret_key or ""})
            conn_store: ConnectionStore = SqlConnectionStore(
                conn, placeholder=settings.placeholder, cipher=cipher)  # type: ignore[arg-type]
            sql_nonces = SqlNonceStore(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
            sql_nonces.create_schema()
            nonce_store = sql_nonces
        else:
            conn_store = InMemoryConnectionStore()
            nonce_store = InMemoryNonceStore()
        redirect = settings.qbo_redirect_uri or "http://localhost:8080/oauth/qbo/callback"
        qbo = QboConnectService(
            QboOAuthConfig(
                client_id=settings.qbo_client_id,
                client_secret=settings.qbo_client_secret,
                redirect_uri=redirect,
                environment=QboEnvironment(settings.qbo_environment),
            ),
            QboHttpClient(),
            conn_store,
            state_secret=qbo_state_secret,
            nonce_store=nonce_store,
        )

    # --- Ask RGNR8 (optional) --------------------------------------------
    ask_llm = anthropic_llm(settings.anthropic_api_key, model=settings.ask_model)

    app = WebApp(
        store=store,  # type: ignore[arg-type]
        packages=packages,
        authenticator=build_authenticator(settings, clock=clock),
        users=users,
        require_rbac=require_rbac,
        usage_recorder=usage_recorder,
        session_secret=session_secret,
        credentials=credentials,
        auth_service=auth_service,
        invitations=invitations,
        emailer=emailer,
        qbo=qbo,
        ask_llm=ask_llm,
        secure_cookies=settings.secure_cookies,
        public_base_url=settings.public_base_url,
    )

    # --- ledger (the accounting system of record) ------------------------
    # Without a ledger URL the books/ledger screens say "not configured" rather
    # than pretending. With one, every books screen and Ask RGNR8 read the real
    # general ledger through the same tenant-scoped client.
    if settings.ledger_url:
        app.set_ledger(LedgerClient(UrllibTransport(settings.ledger_url),
                                    token=settings.ledger_token or ""))
    return app


def _seat_owner(app: WebApp, email: str, tenant_id: str) -> None:
    """Ensure the tenant's declared owner can actually reach it.

    Seats them as OWNER in the directory (so an IdP token for that email
    resolves to real owner permissions rather than landing with no membership →
    403 everywhere) AND, since signup became invitation-only, issues their
    invitation when they have no account yet. Without that second half a brand
    new business would be unreachable: inviting requires MANAGE_USERS, which
    requires already being in the business, so its first owner would have nobody
    to let them in.

    `ensure_owner_access` is idempotent, so running this for every fleet tenant
    on every boot is a no-op once each owner has signed in."""
    if not email:
        return
    app.ensure_owner_access(email, tenant_id)


def load_fleet(
    settings: Settings,
    conn: object,
    *,
    jwt_secret: str | None = None,
    clock: Callable[[], int] | None = None,
) -> Fleet:
    """Rehydrate the persisted fleet from the database (roster + status)."""
    secret = jwt_secret if jwt_secret is not None else (settings.jwt_secret or "dev-secret")
    store = SqlFleetStore(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
    # Durable subscription store → delivery cursors (last_sent) survive restarts,
    # so a redeploy doesn't re-send the week's briefing to everyone.
    subs = SqlSubscriptionStore(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
    return Fleet.load(store, jwt_secret=secret, clock=clock, subscriptions=subs)


def readiness(settings: Settings, conn: object | None = None) -> dict[str, object]:
    """Deploy-check readiness: config summary + a real DB round-trip when a
    connection is supplied (`SELECT 1`). Never raises — reports `db: "error"`."""
    out: dict[str, object] = {"status": "ready", "config": settings.redacted()}
    if conn is not None:
        try:
            cur = conn.cursor()  # type: ignore[attr-defined]
            try:
                cur.execute("SELECT 1")
                cur.fetchall()
            finally:
                cur.close()
            out["db"] = "ok"
        except Exception as exc:  # pragma: no cover - defensive
            out["status"] = "degraded"
            out["db"] = f"error: {type(exc).__name__}"
    else:
        out["db"] = "in-memory"
    return out


def build_observability(
    settings: Settings,
    *,
    clock: Callable[[], float],
) -> tuple[RateLimiter, StructuredLogger, MetricsRegistry, ErrorReporter]:
    """The edge/observability stack the served WSGI app binds: a token-bucket
    rate limiter, a structured logger (JSON to stdout), a metrics registry, and an
    error reporter — all on one injected ``clock`` so request latency is a single
    deterministic measurement. In-memory/stdout sinks here; production swaps the
    sinks without changing this wiring."""
    logger = StructuredLogger(StreamLogSink(sys.stdout), clock=clock, service="rgnr8-web")
    metrics = MetricsRegistry(clock=clock)
    errors: ErrorReporter = InMemoryErrorReporter()
    rate_limiter = RateLimiter(
        capacity=60, refill_per_second=30, clock=clock,
        key_func=make_rate_limit_key(trust_forwarded_for=settings.trust_forwarded_for),
    )
    return rate_limiter, logger, metrics, errors


def create_application(
    env: Mapping[str, str] | None = None,
    *,
    conn: object | None = None,
    packages: FinancialPackageReader | None = None,
    provisioning: Provisioning | None = None,
) -> Callable[..., object]:
    """The WSGI entrypoint factory: `Settings.from_env` → `WebApp` → hardened
    `wsgi_app`. A deploy module does `application = create_application()` for
    gunicorn; in production it passes a live psycopg `conn`.

    The served app is bound to the full edge stack (rate limiting, structured
    logging, metrics, error capture) and the growth seams (analytics + the
    signup→checkout→provisioning + metering `Provisioning`), so a fresh deployment
    is login-ready end-to-end. Everything is optional/injected: called with no
    args this still builds the in-memory dev app. Registers the persisted fleet's
    tenants when a connection is available so routes resolve after a restart."""
    settings = Settings.from_env(env)
    prov = provisioning if provisioning is not None else build_provisioning()
    # Serialize the boot-time schema creation across concurrent gunicorn workers.
    # Each worker imports this module and builds its own app+connection, and the
    # stores' `CREATE TABLE IF NOT EXISTS` is NOT atomic in Postgres — two workers
    # can both pass the existence check and one then fails with a pg_type unique
    # violation, crashing that worker. A session-level advisory lock lets the first
    # worker create everything while the rest wait, then no-op. No-op off Postgres.
    with ddl_bootstrap_lock(conn, settings.placeholder):
        app = build_web_app(settings, conn=conn, packages=packages,
                            usage_recorder=prov.usage_recorder)
        if conn is not None:
            fleet = load_fleet(settings, conn)
            prov.bind_fleet(fleet)  # the on_provisioned → fleet-onboard/metering seam
            for bt in fleet.tenants.values():
                # No guessable static token: under a real authenticator the static
                # map is inert anyway (gated in `_principal`), but we also stop
                # minting a predictable `unused:<tenant>` credential. Seat the owner
                # so IdP tokens resolve to real RBAC.
                app.add_tenant(bt.tenant_id, bt.name, bt.inputs, bt.config,
                               token=secrets.token_urlsafe(32))
                _seat_owner(app, bt.recipient, bt.tenant_id)
    rate_limiter, logger, metrics, errors = build_observability(settings, clock=time.monotonic)
    return wsgi_app(app, rate_limiter=rate_limiter, logger=logger,
                    metrics=metrics, errors=errors)


def create_operator_application(
    env: Mapping[str, str] | None = None,
    *,
    conn: object | None = None,
    provisioning: Provisioning | None = None,
) -> Callable[..., object]:
    """The RGNR8-staff control-plane WSGI entrypoint (the operator console).

    This is the composition that was missing: `OperatorApp` is where go-live /
    onboarding state lives, and until now nothing constructed it in production, so
    its durable `SqlOnboardingRegistry` was dead code. With a live `conn` this
    wires the **durable** onboarding registry (chosen COA template + cutover /
    go-live status survive a restart) and a persisted fleet + audit log; without a
    connection it builds the in-memory dev console. A deploy module does
    `application = create_operator_application()` for gunicorn."""
    settings = Settings.from_env(env)
    prov = provisioning if provisioning is not None else build_provisioning()
    secret = settings.jwt_secret or "dev-secret"

    users: UserDirectory
    audit: AuditSink
    onboarding: OnboardingRegistry
    if conn is not None:
        users = SqlUserDirectory(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
        sql_audit = SqlAuditLog(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
        sql_onboarding = SqlOnboardingRegistry(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
        # Same cold-start race as create_application(): the operator console is its
        # own gunicorn entrypoint, so its workers contend on the identical DDL.
        with ddl_bootstrap_lock(conn, settings.placeholder):
            sql_audit.create_schema()
            sql_onboarding.create_schema()
        audit = sql_audit
        fleet = load_fleet(settings, conn, jwt_secret=secret)
        onboarding = sql_onboarding
    else:
        users = InMemoryUserDirectory()
        audit = InMemoryAuditLog()
        fleet = Fleet(jwt_secret=secret)
        onboarding = OnboardingRegistry()

    ledger = (
        LedgerClient(UrllibTransport(settings.ledger_url), token=settings.ledger_token or "")
        if settings.ledger_url else None
    )

    # Staff browser sign-in: a credential store makes GET/POST /operator/login a
    # real email+password check. The console mints its own session cookie signed
    # with the JWT secret (no separate session secret needed, unlike the owner web
    # app). In jwks (SSO) mode staff come in through the IdP with a bearer token,
    # so the password form stays off. Without this the console's login page
    # returned {"error": "browser login is not configured"}.
    auth_service = None
    if settings.auth_mode != "jwks":
        if conn is not None:
            creds = SqlCredentialStore(conn, placeholder=settings.placeholder)  # type: ignore[arg-type]
            with ddl_bootstrap_lock(conn, settings.placeholder):
                creds.create_schema()
            auth_service = AuthService(credentials=creds)
        else:
            auth_service = AuthService(credentials=InMemoryCredentialStore())

    app = OperatorApp(
        fleet, prov.billing, users, audit, secret,
        clock=lambda: datetime.now(timezone.utc),
        onboarding=onboarding,
        ledger=ledger,
        auth_service=auth_service,
        # Accept the owner web app's session cookie too, so one login at acctg
        # grants the console (single sign-on across the unified front door).
        session_secret=settings.session_secret,
    )
    return operator_wsgi(app)
