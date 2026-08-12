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
from collections.abc import Callable, Mapping
from typing import Any

from rgnr8_web import (
    FinancialPackageReader,
    HttpJwksProvider,
    InMemoryTenantStore,
    InMemoryUserDirectory,
    JwksAuthenticator,
    JwtAuthenticator,
    Role,
    SqlTenantStore,
    SqlUserDirectory,
    StaticTokenAuthenticator,
    UrllibJwksSource,
    User,
    UserDirectory,
    WebApp,
    wsgi_app,
)

from rgnr8_runtime.subscriptions import SqlSubscriptionStore

from .config import ConfigError, Settings
from .fleet import Fleet
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
) -> WebApp:
    """A `WebApp` wired from settings. Pass a DB-API connection for the durable
    tenant store; omit it (dev) for in-memory."""
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
    return WebApp(
        store=store,  # type: ignore[arg-type]
        packages=packages,
        authenticator=build_authenticator(settings, clock=clock),
        users=users,
        require_rbac=require_rbac,
    )


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


def create_application(
    env: Mapping[str, str] | None = None,
    *,
    conn: object | None = None,
    packages: FinancialPackageReader | None = None,
) -> Callable[..., object]:
    """The WSGI entrypoint factory: `Settings.from_env` → `WebApp` → `wsgi_app`.
    A deploy module does `application = create_application()` for gunicorn; in
    production it passes a live psycopg `conn`. Registers the persisted fleet's
    tenants when a connection is available so routes resolve after a restart."""
    settings = Settings.from_env(env)
    app = build_web_app(settings, conn=conn, packages=packages)
    if conn is not None:
        fleet = load_fleet(settings, conn)
        for bt in fleet.tenants.values():
            # No guessable static token: under a real authenticator the static map
            # is inert anyway (gated in `_principal`), but we also stop minting a
            # predictable `unused:<tenant>` credential. Seat the owner so IdP
            # tokens resolve to real RBAC.
            app.add_tenant(bt.tenant_id, bt.name, bt.inputs, bt.config,
                           token=secrets.token_urlsafe(32))
            _seat_owner(app, bt.recipient, bt.tenant_id)
    return wsgi_app(app)
