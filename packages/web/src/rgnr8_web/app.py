"""A framework-free, multi-tenant HTTP application for the RGNR8 owner surface.

The core is a pure ``WebApp.handle(Request) -> Response`` function — no sockets,
so it is fully unit-testable. ``server.py`` wraps it in ``http.server`` for a
runnable process. Auth is a bearer token per tenant; every route is tenant-scoped
and a token may only reach its own tenant.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from rgnr8_forecast import (
    CustomerHistory,
    ForecastConfig,
    ForecastInputs,
    ForecastResult,
    Money,
    run_forecast,
)
from rgnr8_briefing import (
    WeeklyBriefing,
    ask,
    build_briefing,
    render_text,
    render_today_html,
    validate_briefing,
)

from .store import InMemoryTenantStore, TenantDef, TenantState, TenantStore
from .financial_package import (
    FinancialPackageReader,
    PackageIntegrityError,
    render_package_html,
)
from .auth import Authenticator, JwtError, StaticTokenAuthenticator, sign_jwt, verify_jwt
from .apikeys import ApiKeyService
from .audit import AuditSink
from .credentials import AuthError, AuthService, CredentialStore
from .openapi import build_openapi
from .rbac import AccessPolicy, Permission, Role, User, UserDirectory
from .shell import (
    render_app_home,
    render_audit_log,
    render_login_html,
    render_shell,
    render_users_admin,
)
from .transactions import BankTransaction, render_transactions
from .screens import (
    CloseBoard,
    default_close_board,
    render_briefing_body,
    render_cash_body,
    render_close_body,
    render_packages_body,
)


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def query(self) -> dict[str, str]:
        q = urlsplit(self.path).query
        return {k: v[0] for k, v in parse_qs(q).items()}

    @property
    def route(self) -> str:
        return urlsplit(self.path).path


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: str
    content_type: str = "application/json"
    # extra response headers (e.g. Set-Cookie, Location) — merged into `headers`
    extra_headers: tuple[tuple[str, str], ...] = ()

    @property
    def headers(self) -> dict[str, str]:
        h = {"Content-Type": self.content_type, "X-Content-Type-Options": "nosniff"}
        for k, v in self.extra_headers:
            h[k] = v
        return h


@dataclass(slots=True)
class _Tenant:
    tenant_id: str
    name: str
    inputs: ForecastInputs
    config: ForecastConfig
    # mutable owner state (the write path) — persisted through the TenantStore
    state: TenantState = field(default_factory=TenantState)
    _cache: ForecastResult | None = None
    _dirty: bool = False
    # the built briefing, cached alongside the ForecastResult it was derived from.
    # Keyed on the forecast's object identity so it invalidates automatically the
    # moment the forecast is recomputed (on `_dirty`, an assumption change, or an
    # erasure) — no separate flag to keep in sync.
    _briefing_cache: WeeklyBriefing | None = None
    _briefing_for: ForecastResult | None = None

    @property
    def min_cash_override(self) -> Money | None:
        return self.state.min_cash_override

    @property
    def payment_overrides(self) -> dict[str, int]:
        return self.state.payment_overrides

    @property
    def decisions(self) -> list[dict[str, object]]:
        return self.state.decisions


__version__ = "0.1.0"


def _cookie(headers: "dict[str, str]", name: str) -> str | None:
    raw = headers.get("cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v or None
    return None


def _html(status: int, body: str, extra: "tuple[tuple[str, str], ...]" = ()) -> Response:
    return Response(status, body, "text/html; charset=utf-8", extra)


def _redirect(location: str, extra: "tuple[tuple[str, str], ...]" = ()) -> Response:
    return Response(302, "", "text/html; charset=utf-8", (("Location", location), *extra))


def _json(status: int, payload: object) -> Response:
    return Response(status, json.dumps(payload), "application/json")


class WebApp:
    def __init__(
        self,
        store: TenantStore | None = None,
        packages: FinancialPackageReader | None = None,
        authenticator: Authenticator | None = None,
        users: UserDirectory | None = None,
        session_secret: str | None = None,
        session_clock: "Callable[[], int] | None" = None,
        api_keys: ApiKeyService | None = None,
        require_rbac: bool = False,
        usage_recorder: "Callable[[str, str, int], None] | None" = None,
        credentials: CredentialStore | None = None,
        auth_service: AuthService | None = None,
        audit: AuditSink | None = None,
        emailer: "Callable[[str, str, str], None] | None" = None,
    ) -> None:
        self._tenants: dict[str, _Tenant] = {}
        self._tokens: dict[str, str] = {}  # bearer token -> tenant_id (default auth)
        # persistence for mutable owner state; in-memory unless a durable one is given
        self._store: TenantStore = store if store is not None else InMemoryTenantStore()
        # reader for published financial packages (sealed by the TS close stack)
        self._packages = packages
        # production auth (e.g. JwtAuthenticator); None → the static token map below
        self._auth = authenticator
        # RBAC: with a user directory, routes are permission-gated by the caller's
        # role in the tenant; without one, access is tenant-scoped only (back-compat).
        self._users = users
        self._policy = AccessPolicy(users) if users is not None else None
        # Fail closed: a deployment that declares it needs RBAC must actually have
        # a user directory. Without this guard the production composition silently
        # ran with policy=None → every principal owner-equivalent (pre-launch review).
        self._require_rbac = require_rbac
        if require_rbac and users is None:
            raise ValueError("require_rbac=True but no user directory was provided")
        # optional metering hook: (tenant, kind, quantity) — wired to billing in prod
        self._usage_recorder = usage_recorder
        # dev/self-issued browser sessions: HS256 secret used to sign+verify the
        # session cookie set by POST /login. In IdP mode leave this None and log
        # in through the provider (the cookie then carries the IdP token).
        self._session_secret = session_secret
        self._session_clock = session_clock if session_clock is not None else (lambda: int(time.time()))
        # partner API keys — programmatic access resolving to a (tenant, subject)
        # principal that then goes through the same RBAC as a browser request.
        self._api_keys = api_keys
        # bank register feed per tenant (from ingestion / statement import / QBO)
        self._txns: dict[str, list[BankTransaction]] = {}
        self._accounts: dict[str, str] = {}  # tenant -> bank account label
        # month-end close board per tenant (mirrors the @rgnr8/close calendar)
        self._close: dict[str, CloseBoard] = {}
        # real end-user password auth (public track). With a credential store or an
        # AuthService, POST /login authenticates a password + verified email and
        # /signup, /verify and the reset endpoints come alive. None → today's
        # dev/static/SSO login is unchanged (back-compat).
        self._auth_service = auth_service
        if self._auth_service is None and credentials is not None:
            self._auth_service = AuthService(credentials=credentials, audit=audit)
        # audit sink for auth events + the data-export bundle (None → no log)
        self._audit = audit
        # emailer seam: (email, purpose, token). In prod the verify/reset token is
        # emailed here; in dev (no emailer) it is returned in the response body.
        self._emailer = emailer

    def add_tenant(
        self,
        tenant_id: str,
        name: str,
        inputs: ForecastInputs,
        config: ForecastConfig,
        token: str,
    ) -> None:
        # hydrate any previously-saved owner state (overrides + decisions)
        saved = self._store.load(tenant_id)
        tenant = _Tenant(tenant_id, name, inputs, config, state=saved)
        # existing overrides mean the first forecast must recompute with them
        if saved.min_cash_override is not None or saved.payment_overrides:
            tenant._dirty = True
        self._tenants[tenant_id] = tenant
        self._tokens[token] = tenant_id

    def add_transactions(
        self, tenant_id: str, txns: "list[BankTransaction]", *, account_name: str = "Checking"
    ) -> None:
        """Attach a tenant's bank register feed (from ingestion / statement import
        / a QBO overlay). Shown on the Transactions screen."""
        self._txns[tenant_id] = list(txns)
        self._accounts[tenant_id] = account_name

    def add_close(self, tenant_id: str, board: CloseBoard) -> None:
        """Attach a tenant's month-end close board (mirrors the `@rgnr8/close`
        calendar). Shown on the Close screen; advanced/sealed through the API."""
        self._close[tenant_id] = board

    def add_tenant_def(self, td: TenantDef) -> None:
        """Register a tenant from a definition whose inputs came from a
        ``forecast-inputs/1`` DTO (the TS side's emitted contract)."""
        self.add_tenant(td.tenant_id, td.name, td.inputs, td.config, td.token)

    @classmethod
    def from_defs(
        cls, defs: "list[TenantDef]", store: TenantStore | None = None
    ) -> "WebApp":
        app = cls(store=store)
        for td in defs:
            app.add_tenant_def(td)
        return app

    # --- effective inputs/config with owner overrides -----------------------
    def _effective_config(self, t: _Tenant) -> ForecastConfig:
        if t.min_cash_override is None:
            return t.config
        return dataclasses.replace(t.config, minimum_cash=t.min_cash_override)

    def _effective_inputs(self, t: _Tenant) -> ForecastInputs:
        if not t.payment_overrides:
            return t.inputs
        histories = {h.customer_id: h for h in t.inputs.customer_histories}
        for customer_id, days in t.payment_overrides.items():
            histories[customer_id] = CustomerHistory(customer_id=customer_id, override_days_late=days)
        return dataclasses.replace(t.inputs, customer_histories=tuple(histories.values()))

    # --- forecast (cached per tenant; invalidated on override) --------------
    def _forecast(self, t: _Tenant) -> ForecastResult:
        if t._cache is None or t._dirty:
            t._cache = run_forecast(self._effective_inputs(t), self._effective_config(t))
            t._dirty = False
        return t._cache

    def _briefing(self, t: _Tenant) -> WeeklyBriefing:
        """The built briefing for the tenant's current forecast, cached so repeated
        GETs don't rebuild it. Recomputed only when the underlying forecast changes
        (the cache is tied to the ForecastResult's identity, which the `_dirty`
        invalidation replaces)."""
        fc = self._forecast(t)
        if t._briefing_cache is None or t._briefing_for is not fc:
            t._briefing_cache = build_briefing(fc)
            t._briefing_for = fc
        return t._briefing_cache

    def _record_usage(self, tenant_id: str, kind: str, quantity: int) -> None:
        """Emit a metered-usage event via the injected recorder (wired to billing
        in production; no-op in dev). Never lets a metering failure break the
        request."""
        if self._usage_recorder is None:
            return
        try:
            self._usage_recorder(tenant_id, kind, quantity)
        except Exception:
            pass

    # --- auth ----------------------------------------------------------------
    def _authed_tenant(self, req: Request) -> str | None:
        p = self._principal(req)
        return p[0] if p is not None else None

    def _principal(self, req: Request) -> tuple[str, str] | None:
        """(tenant, subject). Order: Authorization bearer → session cookie →
        static token map. A configured authenticator verifies bearer/cookie
        tokens; the dev `session_secret` verifies self-issued session cookies."""
        # 0) partner API key (Authorization: Bearer rgk_... or X-API-Key)
        if self._api_keys is not None:
            presented = req.headers.get("x-api-key", "")
            if not presented:
                auth0 = req.headers.get("authorization", "")
                if auth0.lower().startswith("bearer "):
                    presented = auth0[7:].strip()
            if presented.startswith("rgk_"):
                resolved = self._api_keys.verify(presented)
                if resolved is not None:
                    return resolved
        # 1) Authorization header via the configured authenticator
        if self._auth is not None and req.headers.get("authorization"):
            pf = getattr(self._auth, "principal_for", None)
            if callable(pf):
                result = pf(req.headers)
                if result is not None:
                    return (result[0], result[1])
            else:
                t = self._auth.tenant_for(req.headers)
                if t is not None:
                    return (t, t)
        # 2) session cookie (browser navigation)
        cookie = _cookie(req.headers, "rgnr8_session")
        if cookie is not None:
            if self._auth is not None:
                pf = getattr(self._auth, "principal_for", None)
                synthetic = {"authorization": f"Bearer {cookie}"}
                if callable(pf):
                    result = pf(synthetic)
                    if result is not None:
                        return (result[0], result[1])
                else:
                    t = self._auth.tenant_for(synthetic)
                    if t is not None:
                        return (t, t)
            elif self._session_secret is not None:
                try:
                    claims = verify_jwt(cookie, self._session_secret, now=self._session_clock())
                except JwtError:
                    claims = {}
                tenant = claims.get("tenant")
                subject = claims.get("sub")
                if isinstance(tenant, str):
                    return (tenant, subject if isinstance(subject, str) else tenant)
        # 3) static bearer-token map — dev only, and ONLY when no real
        # authenticator is configured. Otherwise these per-tenant provisioning
        # tokens are a parallel, non-expiring, guessable credential path that
        # bypasses the IdP (the `unused:<tenant>` auth bypass in the pre-launch
        # review). The dev StaticTokenAuthenticator IS this path; a cryptographic
        # authenticator (JWT/JWKS) makes the static map inert.
        if self._auth is None or isinstance(self._auth, StaticTokenAuthenticator):
            auth = req.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                t = self._tokens.get(auth[7:].strip())
                if t is not None:
                    return (t, t)
        return None

    # --- routing -------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        route = req.route

        if route == "/health":
            return _json(200, {"status": "ok"})

        if route == "/ready":
            return _json(200, self._readiness())

        if route == "/openapi.json":
            return _json(200, build_openapi())

        # --- UI: login / logout (no auth required) ---
        if route == "/login" and req.method == "GET":
            return _html(200, render_login_html(sso=self._sso_mode()))
        if route == "/login" and req.method == "POST":
            return self._login_post(req)
        # --- public end-user auth (self-service signup / verify / reset) ---
        if route == "/signup" and req.method == "POST":
            return self._signup_post(req)
        if route == "/verify" and req.method == "POST":
            return self._verify_post(req)
        if route == "/password/reset-request" and req.method == "POST":
            return self._password_reset_request_post(req)
        if route == "/password/reset" and req.method == "POST":
            return self._password_reset_post(req)
        if route == "/logout":
            return _redirect("/login", (("Set-Cookie", "rgnr8_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"),))

        principal = self._principal(req)
        if principal is None:
            return _json(401, {"error": "missing or invalid bearer token"})
        token_tenant, subject = principal

        parts = [p for p in route.split("/") if p]
        P = Permission

        # /app  -> the role-aware home (owner-first landing)
        if route == "/app" or route == "/":
            return self._app_home(subject, token_tenant)

        # /t/<tenant>/team  -> users & roles admin (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "team":
            return self._require(subject, token_tenant, parts[1], P.MANAGE_USERS,
                                 lambda t: self._team_page(subject, t))

        # /t/<tenant>/transactions  -> the bank register (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "transactions":
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._transactions_page(subject, t))

        # /t/<tenant>/briefing  -> this week's briefing (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "briefing":
            return self._require(subject, token_tenant, parts[1], P.VIEW_BRIEFING,
                                 lambda t: self._briefing_page(subject, t))

        # /t/<tenant>/close  -> the month-end close board (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "close":
            return self._require(subject, token_tenant, parts[1], P.MANAGE_CLOSE,
                                 lambda t: self._close_page(subject, t))

        # /t/<tenant>/packages  -> sealed financial-package records (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "packages":
            return self._require(subject, token_tenant, parts[1], P.VIEW_PACKAGE,
                                 lambda t: self._packages_page(subject, t))

        # /t/<tenant>/audit  -> the who-did-what audit log (shell page, owner-gated)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "audit":
            return self._require(subject, token_tenant, parts[1], P.MANAGE_USERS,
                                 lambda t: self._audit_page(subject, t))

        # /t/<tenant>  -> Cash outlook (shell page)
        if len(parts) == 2 and parts[0] == "t":
            return self._require(subject, token_tenant, parts[1], P.VIEW_CASH,
                                 lambda t: self._cash_page(subject, t))

        # /t/<tenant>/packages/<period>  -> published package HTML
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "packages":
            period = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_PACKAGE,
                                 lambda t: self._package_html(t, period))

        # /api/<tenant>/<resource>
        if len(parts) == 3 and parts[0] == "api":
            tenant, resource = parts[1], parts[2]
            if resource == "me" and req.method == "GET":
                return self._me(subject, token_tenant, tenant)
            if resource == "users" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.MANAGE_USERS, self._users_list)
            if resource == "users" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.MANAGE_USERS,
                                     lambda t: self._users_set(t, req.body))
            if resource == "today" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.VIEW_CASH, self._today_json)
            if resource == "briefing.txt" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.VIEW_BRIEFING, self._briefing_text)
            if resource == "ask" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.ASK_CFO, lambda t: self._ask(t, req.body))
            if resource == "assumptions" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.EDIT_ASSUMPTIONS,
                                     lambda t: self._set_assumptions(t, req.body))
            if resource == "decisions" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.VIEW_CASH, self._decisions)
            if resource == "decisions" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.RECORD_DECISION,
                                     lambda t: self._add_decision(t, req.body))
            if resource == "packages" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.VIEW_PACKAGE, self._packages_list)
            if resource == "transactions" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.VIEW_TRANSACTIONS,
                                     self._transactions_json)
            if resource == "transactions" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.CATEGORIZE_TXNS,
                                     lambda t: self._categorize(t, req.body))
            if resource == "close" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.MANAGE_CLOSE,
                                     self._close_json)
            if resource == "close" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.MANAGE_CLOSE,
                                     lambda t: self._close_advance(t, req.body))
            # owner-gated GDPR/CCPA data-portability export (MANAGE_USERS is
            # owner-only among tenant roles).
            if resource == "export" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.MANAGE_USERS, self._export)
            # owner-gated GDPR/CCPA right-to-delete — the export's twin. POST or
            # DELETE both erase; idempotent.
            if resource == "erase" and req.method in ("POST", "DELETE"):
                return self._require(subject, token_tenant, tenant, P.MANAGE_USERS,
                                     lambda t: self._erase(subject, t))
            # owner-gated audit-log viewer (JSON), most-recent-first, optional ?actor=
            if resource == "audit" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.MANAGE_USERS,
                                     lambda t: self._audit_json(t, req.query.get("actor")))

        # /api/<tenant>/close/publish  -> seal the period (PUBLISH_CLOSE)
        if (len(parts) == 4 and parts[0] == "api" and parts[2] == "close"
                and parts[3] == "publish" and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.PUBLISH_CLOSE, self._close_publish)

        # /api/<tenant>/packages/<period>  -> published package JSON (verified)
        if len(parts) == 4 and parts[0] == "api" and parts[2] == "packages" and req.method == "GET":
            period = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_PACKAGE,
                                 lambda t: self._package_json(t, period))

        return _json(404, {"error": "not found"})

    def _readiness(self) -> dict[str, object]:
        """A readiness snapshot for load balancers / deploy checks: how many
        tenants are provisioned, whether package serving + auth are wired, and
        the auth mode. Deterministic and side-effect-free."""
        auth_mode = "none"
        if self._auth is not None:
            auth_mode = type(self._auth).__name__
        elif self._tokens:
            auth_mode = "static-token"
        return {
            "status": "ready",
            "tenants": len(self._tenants),
            "packages": self._packages is not None,
            "auth": auth_mode,
            "rbac": self._policy is not None,
            "version": __version__,
        }

    def _require(
        self,
        subject: str,
        token_tenant: str,
        wanted: str,
        permission: Permission,
        fn: Callable[[_Tenant], Response],
    ) -> Response:
        # Authorize for the tenant BEFORE checking existence, so a caller can't use
        # the unknown-vs-forbidden distinction to enumerate tenant ids.
        if token_tenant != wanted:
            return _json(403, {"error": "not authorized for this tenant"})
        if wanted not in self._tenants:
            return _json(404, {"error": f"unknown tenant {wanted}"})
        # Fail closed: if this deployment requires RBAC but no policy resolved,
        # deny rather than fall through to full access.
        if self._require_rbac and self._policy is None:
            return _json(403, {"error": "authorization not configured"})
        # RBAC: with a user directory, the caller needs the route's permission in
        # this tenant. Without one, access stays tenant-scoped (back-compat/dev).
        if self._policy is not None and not self._policy.can(subject, wanted, permission):
            return _json(403, {"error": "insufficient role", "need": permission.value})
        return fn(self._tenants[wanted])

    # --- RBAC handlers -------------------------------------------------------
    def _me(self, subject: str, token_tenant: str, tenant: str) -> Response:
        """Who am I here: identity, role in this business, and my permissions."""
        if tenant not in self._tenants:
            return _json(404, {"error": f"unknown tenant {tenant}"})
        if token_tenant != tenant:
            return _json(403, {"error": "token not authorized for this tenant"})
        if self._policy is None or self._users is None:
            # no RBAC configured — tenant-scoped access, full owner-equivalent view
            return _json(200, {"subject": subject, "tenant": tenant, "role": None,
                               "permissions": [], "rbac": False})
        perms = sorted(p.value for p in self._policy.permissions(subject, tenant))
        role = self._policy.role_in(subject, tenant)
        user = self._users.get_user(subject)
        if not perms:
            return _json(403, {"error": "no access to this business"})
        return _json(200, {
            "subject": subject,
            "email": user.email if user is not None else None,
            "name": user.name if user is not None else None,
            "tenant": tenant,
            "role": role.value if role is not None else None,
            "permissions": perms,
            "rbac": True,
        })

    def _users_list(self, t: _Tenant) -> Response:
        if self._users is None:
            return _json(501, {"error": "user management is not configured"})
        rows = []
        for m in self._users.members(t.tenant_id):
            u = self._users.get_user(m.user_id)
            rows.append({
                "user_id": m.user_id,
                "email": u.email if u is not None else None,
                "name": u.name if u is not None else None,
                "role": m.role.value,
            })
        return _json(200, {"tenant": t.tenant_id, "members": rows})

    def _users_set(self, t: _Tenant, body: str) -> Response:
        """Add/change a member's role, or remove them (role: null). Body:
        {"email": "...", "name"?: "...", "role": "controller"|null}."""
        if self._users is None:
            return _json(501, {"error": "user management is not configured"})
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        email = data.get("email")
        if not isinstance(email, str) or "@" not in email:
            return _json(400, {"error": "a valid email is required"})
        role_raw = data.get("role")
        user = self._users.find_by_email(email)
        if user is None:
            user = User(id=email, email=email, name=str(data.get("name", "")))
            self._users.upsert_user(user)
        if role_raw is None:
            self._users.remove_membership(user.id, t.tenant_id)
            return _json(200, {"user_id": user.id, "email": user.email, "role": None})
        try:
            role = Role(str(role_raw))
        except ValueError:
            return _json(400, {"error": f"unknown role {role_raw!r}",
                               "roles": [r.value for r in Role if not r.is_platform]})
        if role.is_platform:
            return _json(400, {"error": "platform roles are not assignable per-business"})
        self._users.set_membership(user.id, t.tenant_id, role)
        return _json(200, {"user_id": user.id, "email": user.email, "role": role.value})

    # --- UI (server-rendered app shell) -------------------------------------
    def _sso_mode(self) -> bool:
        """True when login goes to a real IdP (an authenticator is configured
        and there's no dev session secret to self-issue tokens)."""
        return self._auth is not None and self._session_secret is None

    def _perms_role(self, subject: str, tenant: str) -> tuple[frozenset[Permission], Role | None]:
        if self._policy is not None:
            return self._policy.permissions(subject, tenant), self._policy.role_in(subject, tenant)
        # no RBAC directory → tenant-scoped access = full owner-equivalent view
        return frozenset(Permission), None

    def _latest_period(self, tenant: str) -> str | None:
        if self._packages is None:
            return None
        periods = self._packages.list_periods(tenant)
        return sorted(periods)[-1] if periods else None

    @staticmethod
    def _money_str(m: Money) -> str:
        neg = m.minor_units < 0
        whole = m.to_decimal_string().lstrip("-")
        intpart, _, frac = whole.partition(".")
        return f"{'-' if neg else ''}${int(intpart):,}.{frac or '00'}"

    def _form_or_json(self, body: str) -> dict[str, object]:
        if not body:
            return {}
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        return {k: v[0] for k, v in parse_qs(body).items()}

    def _login_post(self, req: Request) -> Response:
        if self._session_secret is None:
            return _html(400, render_login_html(sso=True, error="Self-issued login is disabled — use SSO."))
        data = self._form_or_json(req.body)
        email = str(data.get("email", "")).strip()
        if "@" not in email:
            return _html(400, render_login_html(error="Enter a valid work email."))
        role_raw = str(data.get("role", "owner"))
        # Real credential login (public track): verify the password + verified email
        # via the AuthService. On any failure, respond without distinguishing why.
        if self._auth_service is not None:
            authed = self._auth_service.login(email, str(data.get("password", "")))
            if authed is None:
                return self._login_failure(req)
        tenant = self._resolve_login_tenant(email, data.get("tenant"))
        if tenant is None:
            return _html(400, render_login_html(error="No business is provisioned to sign into yet."))
        subject = email
        if self._users is not None:
            user = self._users.find_by_email(email) or User(id=email, email=email)
            self._users.upsert_user(user)
            subject = user.id
            # NEVER take the effective role from the client (the `role` field is
            # cosmetic). A brand-new user with no membership gets the least-
            # privilege default; elevation happens only via an invitation or an
            # admin. (Trusting the client role was a self-service priv-esc.)
            if self._users.membership(subject, tenant) is None:
                self._users.set_membership(subject, tenant, Role.VIEWER)
        _ = role_raw  # retained for the dev UI only; not authorization-bearing
        token = sign_jwt(
            {"sub": subject, "tenant": tenant, "exp": self._session_clock() + 8 * 3600},
            self._session_secret,
        )
        cookie = f"rgnr8_session={token}; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax"
        return _redirect("/app", (("Set-Cookie", cookie),))

    @staticmethod
    def _wants_json(req: Request) -> bool:
        ct = req.headers.get("content-type", "").lower()
        if "application/json" in ct:
            return True
        return req.body.lstrip().startswith("{")

    def _login_failure(self, req: Request) -> Response:
        """A single, non-committal login rejection — never reveals whether the email
        is unknown, the password wrong, or the account unverified."""
        if self._wants_json(req):
            return _json(401, {"error": "invalid email or password"})
        return _html(401, render_login_html(error="Invalid email or password."))

    # --- public end-user auth (self-service) --------------------------------
    def _signup_post(self, req: Request) -> Response:
        """Create an unverified credential and issue an email-verification token.
        Body (form or JSON): {email, password}. In dev the token is returned in the
        body; with an `emailer` seam it is sent and withheld from the response."""
        if self._auth_service is None:
            return _json(501, {"error": "signup is not configured"})
        data = self._form_or_json(req.body)
        email = str(data.get("email", "")).strip()
        password = str(data.get("password", ""))
        try:
            cred, token = self._auth_service.signup(email, password)
        except AuthError as exc:
            return _json(400, {"error": str(exc)})
        body: dict[str, object] = {"user_id": cred.user_id, "email": cred.email,
                                   "verified": cred.verified}
        if self._emailer is not None:
            self._emailer(cred.email, "verify_email", token)
        else:
            body["verify_token"] = token
        return _json(201, body)

    def _verify_post(self, req: Request) -> Response:
        """Consume an email-verification token (single-use). Body: {token}."""
        if self._auth_service is None:
            return _json(501, {"error": "signup is not configured"})
        data = self._form_or_json(req.body)
        if not self._auth_service.verify_email(str(data.get("token", ""))):
            return _json(400, {"error": "invalid or expired verification token"})
        return _json(200, {"verified": True})

    def _password_reset_request_post(self, req: Request) -> Response:
        """Request a password-reset token. Always answers 202 so it can't be used to
        probe which emails are registered; a token is minted only if the email is
        known (returned in dev, or emailed via the seam)."""
        if self._auth_service is None:
            return _json(501, {"error": "signup is not configured"})
        data = self._form_or_json(req.body)
        email = str(data.get("email", "")).strip()
        token = self._auth_service.request_password_reset(email)
        body: dict[str, object] = {"status": "ok"}
        if token is not None:
            if self._emailer is not None:
                self._emailer(email, "password_reset", token)
            else:
                body["reset_token"] = token
        return _json(202, body)

    def _password_reset_post(self, req: Request) -> Response:
        """Set a new password from a reset token (single-use + expiring). Body:
        {token, password}."""
        if self._auth_service is None:
            return _json(501, {"error": "signup is not configured"})
        data = self._form_or_json(req.body)
        token = str(data.get("token", ""))
        new_password = str(data.get("password", "") or data.get("new_password", ""))
        if not self._auth_service.reset_password(token, new_password):
            return _json(400, {"error": "invalid or expired token, or password too weak"})
        return _json(200, {"reset": True})

    def _resolve_login_tenant(self, email: str, requested: object) -> str | None:
        if isinstance(requested, str) and requested in self._tenants:
            return requested
        if self._users is not None:
            u = self._users.find_by_email(email)
            if u is not None:
                for m in [mm for mm in self._all_memberships_of(u.id)]:
                    if m in self._tenants:
                        return m
        if len(self._tenants) == 1:
            return next(iter(self._tenants))
        return None

    def _all_memberships_of(self, user_id: str) -> list[str]:
        if self._users is None:
            return []
        return [t for t in self._tenants if self._users.membership(user_id, t) is not None]

    def _app_home(self, subject: str, tenant: str) -> Response:
        if tenant not in self._tenants:
            return _redirect("/login")
        perms, role = self._perms_role(subject, tenant)
        if not perms:
            return _html(403, render_login_html(error="You don't have access to this business."))
        t = self._tenants[tenant]
        fc = self._forecast(t)
        b = self._briefing(t)
        body = render_app_home(
            tenant=tenant, display_name=t.name, role=role, permissions=perms,
            cash_today=self._money_str(fc.projection.opening_available),
            status=b.status.value, headline=b.headline, latest_period=self._latest_period(tenant),
        )
        return _html(200, render_shell(tenant=tenant, display_name=t.name, role=role,
                                       permissions=perms, active="home", body_html=body, subject=subject))

    def _team_page(self, subject: str, t: _Tenant) -> Response:
        perms, role = self._perms_role(subject, t.tenant_id)
        members = []
        if self._users is not None:
            members = [(m, self._users.get_user(m.user_id)) for m in self._users.members(t.tenant_id)]
        body = render_users_admin(t.tenant_id, members)
        return _html(200, render_shell(tenant=t.tenant_id, display_name=t.name, role=role,
                                       permissions=perms, active="team", body_html=body, subject=subject))

    def _transactions_page(self, subject: str, t: _Tenant) -> Response:
        perms, role = self._perms_role(subject, t.tenant_id)
        can_cat = self._policy is None or Permission.CATEGORIZE_TXNS in perms
        txns = self._txns.get(t.tenant_id, [])
        body = render_transactions(
            t.tenant_id, self._accounts.get(t.tenant_id, "Checking"), txns, can_categorize=can_cat
        )
        return _html(200, render_shell(tenant=t.tenant_id, display_name=t.name, role=role,
                                       permissions=perms, active="transactions", body_html=body, subject=subject))

    def _categorize(self, t: _Tenant, body: str) -> Response:
        """Categorize a line, or accept a for-review line into the books. Body:
        {"id": "...", "category"?: "...", "accept"?: true}."""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        txn_id = data.get("id")
        if not isinstance(txn_id, str):
            return _json(400, {"error": "id is required"})
        rows = self._txns.get(t.tenant_id, [])
        for i, tx in enumerate(rows):
            if tx.id != txn_id:
                continue
            category = tx.category
            status = tx.status
            if isinstance(data.get("category"), str):
                category = data["category"]
            if data.get("accept") is True or (category != "Uncategorized" and status == "review"):
                status = "matched"
            rows[i] = dataclasses.replace(tx, category=category, status=status)
            return _json(200, {"id": txn_id, "category": category, "status": status})
        return _json(404, {"error": f"unknown transaction {txn_id}"})

    # --- in-shell owner screens (cash / briefing / close / packages) --------
    def _shell(self, subject: str, t: _Tenant, active: str, body: str) -> Response:
        perms, role = self._perms_role(subject, t.tenant_id)
        return _html(200, render_shell(tenant=t.tenant_id, display_name=t.name, role=role,
                                       permissions=perms, active=active, body_html=body, subject=subject))

    def _cash_page(self, subject: str, t: _Tenant) -> Response:
        return self._shell(subject, t, "cash", render_cash_body(self._forecast(t), t.name))

    def _briefing_page(self, subject: str, t: _Tenant) -> Response:
        return self._shell(subject, t, "briefing", render_briefing_body(self._forecast(t), t.name))

    def _packages_page(self, subject: str, t: _Tenant) -> Response:
        periods = self._packages.list_periods(t.tenant_id) if self._packages is not None else []
        return self._shell(subject, t, "packages",
                           render_packages_body(t.tenant_id, periods, self._packages is not None))

    def _close_board(self, t: _Tenant) -> CloseBoard:
        board = self._close.get(t.tenant_id)
        if board is None:
            board = default_close_board(t.inputs.opening.as_of.strftime("%Y-%m"))
            self._close[t.tenant_id] = board
        return board

    def _close_page(self, subject: str, t: _Tenant) -> Response:
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_manage = self._policy is None or Permission.MANAGE_CLOSE in perms
        can_publish = self._policy is None or Permission.PUBLISH_CLOSE in perms
        body = render_close_body(self._close_board(t), t.tenant_id,
                                 can_manage=can_manage, can_publish=can_publish)
        return self._shell(subject, t, "close", body)

    def _transactions_json(self, t: _Tenant) -> Response:
        """The bank register as JSON: summary + the for-review queue (the shape the
        MCP `review_transactions` tool and partner integrations consume)."""
        from .transactions import summarize
        rows = self._txns.get(t.tenant_id, [])
        s = summarize(rows)
        return _json(200, {
            "tenant": t.tenant_id,
            "account": self._accounts.get(t.tenant_id, "Checking"),
            "summary": {"total": s.total, "matched": s.matched, "review": s.review,
                        "unmatched": s.unmatched, "inflow": s.inflow.to_decimal_string(),
                        "outflow": s.outflow.to_decimal_string()},
            "for_review": [
                {"id": r.id, "date": r.date, "description": r.description,
                 "amount": r.amount.to_decimal_string(), "category": r.category,
                 "status": r.status, "counterparty": r.counterparty}
                for r in rows if r.needs_review
            ],
        })

    def _close_json(self, t: _Tenant) -> Response:
        b = self._close_board(t)
        return _json(200, {
            "tenant": t.tenant_id, "period": b.period, "sealed": b.sealed,
            "done": b.done, "total": b.total, "overdue": b.overdue,
            "blocked": b.blocked, "complete": b.complete,
            "tasks": [{"key": tk.key, "label": tk.label, "status": tk.status,
                       "owner": tk.owner, "due": tk.due} for tk in b.tasks],
        })

    def _close_advance(self, t: _Tenant, body: str) -> Response:
        """Set a close task's status. Body: {"id": "...", "status": "done"|"open"|...}."""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        key = data.get("id")
        status = data.get("status", "done")
        if not isinstance(key, str) or not isinstance(status, str):
            return _json(400, {"error": "id and status are required"})
        if status not in ("done", "open", "overdue", "blocked"):
            return _json(400, {"error": f"unknown status {status!r}"})
        board = self._close_board(t)
        if board.sealed:
            return _json(409, {"error": "period is sealed"})
        if not any(task.key == key for task in board.tasks):
            return _json(404, {"error": f"unknown close task {key}"})
        board = board.with_task_status(key, status)
        self._close[t.tenant_id] = board
        return _json(200, {"id": key, "status": status, "done": board.done, "total": board.total,
                           "complete": board.complete})

    def _close_publish(self, t: _Tenant) -> Response:
        """Seal the period once every task is done (the publish gate)."""
        board = self._close_board(t)
        if board.sealed:
            return _json(200, {"period": board.period, "sealed": True})
        if not board.complete:
            return _json(409, {"error": "every close task must be done before sealing",
                               "done": board.done, "total": board.total})
        board = dataclasses.replace(board, sealed=True)
        self._close[t.tenant_id] = board
        t.state.decisions.append({"id": len(t.state.decisions) + 1, "kind": "close",
                                  "label": f"Sealed {board.period}"})
        self._store.save(t.tenant_id, t.state)
        return _json(200, {"period": board.period, "sealed": True})

    # --- handlers ------------------------------------------------------------
    def _today_html(self, t: _Tenant) -> Response:
        html = render_today_html(self._forecast(t), t.name)
        return Response(200, html, "text/html; charset=utf-8")

    def _today_json(self, t: _Tenant) -> Response:
        fc = self._forecast(t)
        b = self._briefing(t)
        if validate_briefing(b, fc):
            return _json(500, {"error": "briefing failed number validation"})
        p = fc.projection
        return _json(
            200,
            {
                "tenant": t.tenant_id,
                "as_of": p.as_of.isoformat(),
                "status": b.status.value,
                "headline": b.headline,
                "action": b.primary_action,
                "cash_today": p.opening_available.to_decimal_string(),
                "floor": p.effective_floor.to_decimal_string(),
                "trough": {
                    "amount": p.trough.balance.to_decimal_string(),
                    "date": p.trough.on_date.isoformat(),
                },
                "breach": {
                    "breached": p.breach.breached,
                    "week": p.breach.weeks_until,
                    "shortfall": p.breach.worst_shortfall.to_decimal_string(),
                },
                "confidence": fc.overall_confidence,
                "currency": p.currency,
                "weeks": [
                    {"index": w.index, "start": w.start.isoformat(), "closing": w.closing.to_decimal_string()}
                    for w in p.weeks
                ],
                "version": fc.version.version_id,
            },
        )

    def _briefing_text(self, t: _Tenant) -> Response:
        return Response(200, render_text(self._briefing(t)), "text/plain; charset=utf-8")

    def _ask(self, t: _Tenant, body: str) -> Response:
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        text = str(payload.get("text", "")).strip()
        if not text:
            return _json(400, {"error": "provide a 'text' field"})
        a = ask(self._forecast(t), text)
        self._record_usage(t.tenant_id, "api_call", 1)  # meter the CFO surface
        return _json(
            200,
            {
                "supported": a.supported,
                "answer": a.answer_text,
                "question": a.question_text,
                "suggestions": list(a.suggestions),
            },
        )

    # --- write path (the actionable surface) --------------------------------
    def _set_assumptions(self, t: _Tenant, body: str) -> Response:
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})

        changed: list[str] = []
        if "minimum_cash" in payload:
            try:
                t.state.set_min_cash(Money.from_decimal(str(payload["minimum_cash"])))
                changed.append("minimum_cash")
            except ValueError:
                return _json(400, {"error": "minimum_cash is not a valid amount"})
        if "payment_override" in payload:
            po = payload["payment_override"]
            try:
                customer_id = str(po["customer_id"])
                days = int(po["days_late"])
            except (KeyError, TypeError, ValueError):
                return _json(400, {"error": "payment_override needs customer_id and integer days_late"})
            t.state.payment_overrides[customer_id] = days
            changed.append(f"payment_override:{customer_id}")

        if not changed:
            return _json(400, {"error": "nothing to change"})

        t._dirty = True  # invalidate the cached forecast
        # record the change as a decision, persist, then return the recomputed summary
        t.state.decisions.append({"id": len(t.state.decisions) + 1, "kind": "assumption", "changed": changed})
        self._store.save(t.tenant_id, t.state)
        return self._today_json(t)

    def _add_decision(self, t: _Tenant, body: str) -> Response:
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        label = str(payload.get("label", "")).strip()
        if not label:
            return _json(400, {"error": "provide a 'label'"})
        entry: dict[str, object] = {
            "id": len(t.state.decisions) + 1,
            "kind": str(payload.get("kind", "decision")),
            "label": label,
            "note": str(payload.get("note", "")),
        }
        t.state.decisions.append(entry)
        self._store.save(t.tenant_id, t.state)
        return _json(201, entry)

    def _decisions(self, t: _Tenant) -> Response:
        return _json(200, {"tenant": t.tenant_id, "decisions": t.decisions})

    # --- published financial packages (sealed by the TS close stack) ---------
    def _packages_list(self, t: _Tenant) -> Response:
        if self._packages is None:
            return _json(404, {"error": "financial packages are not configured"})
        periods = self._packages.list_periods(t.tenant_id)
        return _json(200, {"tenant": t.tenant_id, "periods": periods})

    def _package_json(self, t: _Tenant, period: str) -> Response:
        if self._packages is None:
            return _json(404, {"error": "financial packages are not configured"})
        try:
            pkg = self._packages.get(t.tenant_id, period)
        except PackageIntegrityError:
            return _json(409, {"error": "stored package failed integrity verification"})
        if pkg is None:
            return _json(404, {"error": f"no published package for {period}"})
        return _json(200, pkg)

    # --- data portability (GDPR/CCPA export) --------------------------------
    def _export(self, t: _Tenant) -> Response:
        """The tenant's full data bundle as JSON — for a data-portability request.
        Owner-gated. Summarizes forecast inputs and includes decisions, the bank
        register, the close board, and (if a sink is wired) this tenant's audit
        events."""
        fc = self._forecast(t)
        p = fc.projection
        inp = t.inputs
        txns = self._txns.get(t.tenant_id, [])
        board = self._close.get(t.tenant_id)
        audit_events = self._audit.events(tenant_id=t.tenant_id) if self._audit is not None else []
        bundle: dict[str, object] = {
            "tenant": t.tenant_id,
            "name": t.name,
            "generated_at": self._session_clock(),
            "forecast_inputs": {
                "as_of": inp.opening.as_of.isoformat(),
                "opening_available": inp.opening.available.to_decimal_string(),
                "minimum_cash": self._effective_config(t).minimum_cash.to_decimal_string(),
                "invoices": len(inp.invoices),
                "payroll_schedules": len(inp.payroll),
                "recurring_items": len(inp.recurring),
                "customer_histories": len(inp.customer_histories),
                "payment_overrides": dict(t.payment_overrides),
            },
            "cash_today": p.opening_available.to_decimal_string(),
            "decisions": list(t.decisions),
            "transactions": [
                {"id": r.id, "date": r.date, "description": r.description,
                 "amount": r.amount.to_decimal_string(), "category": r.category,
                 "status": r.status, "counterparty": r.counterparty}
                for r in txns
            ],
            "close_board": None if board is None else {
                "period": board.period, "sealed": board.sealed,
                "tasks": [{"key": tk.key, "label": tk.label, "status": tk.status,
                           "owner": tk.owner, "due": tk.due} for tk in board.tasks],
            },
            "audit_events": [
                {"seq": e.seq, "actor": e.actor, "action": e.action, "at": e.at,
                 "target": e.target, "detail": e.detail}
                for e in audit_events
            ],
        }
        return _json(200, bundle)

    # --- right-to-delete (GDPR/CCPA erasure — the export's twin) --------------
    def _erase(self, subject: str, t: _Tenant) -> Response:
        """Purge the tenant's owner-facing data the web app holds: the bank
        register feed, the month-end close board, the decisions + assumption
        overrides in TenantState, and the cached forecast/briefing. Idempotent —
        a second call clears nothing and reports zeros. Writes a `data.erased`
        audit event. Returns a JSON summary of what was cleared."""
        txns = self._txns.get(t.tenant_id, [])
        had_close = t.tenant_id in self._close
        decisions_n = len(t.state.decisions)
        overrides_n = len(t.state.payment_overrides)
        had_min_cash = t.state.min_cash_override_dto is not None

        # bank register feed
        self._txns[t.tenant_id] = []
        self._accounts.pop(t.tenant_id, None)
        # month-end close board
        self._close.pop(t.tenant_id, None)
        # mutable owner state (decisions + assumption overrides), then persist
        t.state.decisions.clear()
        t.state.payment_overrides.clear()
        t.state.min_cash_override_dto = None
        self._store.save(t.tenant_id, t.state)
        # cached forecast + briefing — recompute clean on next read
        t._cache = None
        t._briefing_cache = None
        t._briefing_for = None
        t._dirty = True

        summary = {
            "transactions": len(txns),
            "decisions": decisions_n,
            "payment_overrides": overrides_n,
            "min_cash_override": had_min_cash,
            "close_board": had_close,
            "forecast_cache_cleared": True,
        }
        if self._audit is not None:
            self._audit.record(subject, "data.erased", self._session_clock(),
                               tenant_id=t.tenant_id, target=t.tenant_id,
                               detail=json.dumps(summary, sort_keys=True))
        return _json(200, {"tenant": t.tenant_id, "erased": summary})

    # --- audit-log viewer (who-did-what) ------------------------------------
    def _audit_json(self, t: _Tenant, actor: str | None) -> Response:
        if self._audit is None:
            return _json(501, {"error": "audit log is not configured"})
        events = self._audit.events(tenant_id=t.tenant_id, actor=actor)
        events = sorted(events, key=lambda e: e.seq, reverse=True)
        return _json(200, {
            "tenant": t.tenant_id,
            "actor": actor,
            "events": [
                {"seq": e.seq, "actor": e.actor, "action": e.action, "at": e.at,
                 "target": e.target, "detail": e.detail}
                for e in events
            ],
        })

    def _audit_page(self, subject: str, t: _Tenant) -> Response:
        perms, role = self._perms_role(subject, t.tenant_id)
        if self._audit is None:
            body = render_audit_log(t.tenant_id, [], configured=False)
        else:
            events = sorted(self._audit.events(tenant_id=t.tenant_id),
                            key=lambda e: e.seq, reverse=True)
            body = render_audit_log(t.tenant_id, events, configured=True)
        return _html(200, render_shell(tenant=t.tenant_id, display_name=t.name, role=role,
                                       permissions=perms, active="audit", body_html=body, subject=subject))

    def _package_html(self, t: _Tenant, period: str) -> Response:
        if self._packages is None:
            return Response(404, "financial packages are not configured", "text/plain; charset=utf-8")
        try:
            pkg = self._packages.get(t.tenant_id, period)
        except PackageIntegrityError:
            return Response(409, "stored package failed integrity verification", "text/plain; charset=utf-8")
        if pkg is None:
            return Response(404, f"no published package for {period}", "text/plain; charset=utf-8")
        return Response(200, render_package_html(pkg), "text/html; charset=utf-8")
