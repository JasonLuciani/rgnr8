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
from rgnr8_runtime.subscriptions import SqlSubscriptionStore
from rgnr8_copilot import anthropic_llm
from rgnr8_qbo import (
    ConnectionStore,
    InMemoryConnectionStore,
    QboConnectService,
    QboEnvironment,
    QboOAuthConfig,
    SqlConnectionStore,
    cipher_from_env,
)
from rgnr8_qbo import UrllibHttpClient as QboHttpClient
from rgnr8_web import (
    AuthService,
    FinancialPackageReader,
    HttpJwksProvider,
    InMemoryCredentialStore,
    InMemoryTenantStore,
    InMemoryUserDirectory,
    JwksAuthenticator,
    JwtAuthenticator,
    LedgerClient,
    RateLimiter,
    Role,
    SqlCredentialStore,
    SqlTenantStore,
    SqlUserDirectory,
    StaticTokenAuthenticator,
    UrllibJwksSource,
    UrllibTransport,
    User,
    UserDirectory,
    WebApp,
    wsgi_app,
)

from .config import ConfigError, Settings
from .fleet import Fleet
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

    # --- QuickBooks Online connect (optional) ----------------------------
    # Only wired when the Intuit app credentials are present. Tokens are
    # encrypted at rest via the Fernet key (config.from_env fails closed when a
    # DB is configured without one). State signing needs a secret; without any
    # available secret the connect surface stays "not configured".
    qbo = None
    qbo_state_secret = session_secret or settings.jwt_secret or settings.secret_key
    if settings.qbo_enabled and qbo_state_secret:
        assert settings.qbo_client_id is not None and settings.qbo_client_secret is not None
        if conn is not None:
            # At-rest encryption for tokens; config.from_env fails closed when a DB
            # is configured without RGNR8_SECRET_KEY, so the key is present here.
            cipher = cipher_from_env({"RGNR8_SECRET_KEY": settings.secret_key or ""})
            conn_store: ConnectionStore = SqlConnectionStore(
                conn, placeholder=settings.placeholder, cipher=cipher)  # type: ignore[arg-type]
        else:
            conn_store = InMemoryConnectionStore()
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
        qbo=qbo,
        ask_llm=ask_llm,
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
    """Ensure the tenant's owner exists + is seated as OWNER in the directory, so
    an IdP token for that email resolves to real owner permissions (rather than
    landing with no membership → 403 everywhere)."""
    directory = app._users  # the app's configured directory
    if directory is None or not email or "@" not in email:
        return
    user = directory.find_by_email(email) or User(id=email, email=email)
    directory.upsert_user(user)
    if directory.membership(user.id, tenant_id) is None:
        directory.set_membership(user.id, tenant_id, Role.OWNER)


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
    rate_limiter = RateLimiter(capacity=60, refill_per_second=30, clock=clock)
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
    app = build_web_app(settings, conn=conn, packages=packages,
                        usage_recorder=prov.usage_recorder)
    if conn is not None:
        fleet = load_fleet(settings, conn)
        prov.bind_fleet(fleet)  # the on_provisioned → fleet-onboard/metering seam
        for bt in fleet.tenants.values():
            # No guessable static token: under a real authenticator the static map
            # is inert anyway (gated in `_principal`), but we also stop minting a
            # predictable `unused:<tenant>` credential. Seat the owner so IdP
            # tokens resolve to real RBAC.
            app.add_tenant(bt.tenant_id, bt.name, bt.inputs, bt.config,
                           token=secrets.token_urlsafe(32))
            _seat_owner(app, bt.recipient, bt.tenant_id)
    rate_limiter, logger, metrics, errors = build_observability(settings, clock=time.monotonic)
    return wsgi_app(app, rate_limiter=rate_limiter, logger=logger,
                    metrics=metrics, errors=errors)
