"""The authenticated OPERATOR HTTP surface — RGNR8 staff only.

Where `rgnr8_web.WebApp` is the *client* surface (a business's own owners and
members, tenant-scoped), `OperatorApp` is *our* surface: the RGNR8-staff control
plane that runs the beta fleet. It reuses the framework-free
`rgnr8_web.Request`/`Response` shape (a pure `handle(Request) -> Response`, no
sockets), verifies the same HS256 session JWTs through the same `verify_jwt`
path, and gates every route on a **platform role** held in the shared
`UserDirectory`:

* **support** — read-only: view the fleet console + the audit log.
* **operator** — everything support can, plus onboard/provision a new business.

Onboarding runs through `PlatformAdmin` (provision the billing account if it's
new, then attach the business entitlement-checked and seat its owner), so the
operator surface and the platform admin API stay one code path. Every mutating
action is audited. `operator_wsgi` wraps it as a WSGI callable, mirroring
`rgnr8_web.wsgi_app`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime

from rgnr8_forecast import Money
from rgnr8_billing import BillingError, BillingService, EntitlementError, Tier
from rgnr8_web import (
    AuditSink,
    AuthService,
    JwtError,
    Request,
    Response,
    Role,
    UserDirectory,
    sign_jwt,
    verify_jwt,
)

from .console import render_operator_console, render_operator_login
from .fleet import BetaTenant, Fleet
from .platform import PlatformAdmin, PlatformError
from .onboarding import (
    OnboardingError,
    OnboardingRegistry,
    build_go_live_request,
    category_catalog,
    is_valid_category,
)
from .report import build_ops_report


def _redirect(location: str, extra: "tuple[tuple[str, str], ...]" = ()) -> Response:
    return Response(302, "", "text/html; charset=utf-8", (("Location", location), *extra))


def _cookie(headers: "dict[str, str]", name: str) -> str | None:
    for part in headers.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v or None
    return None


def _json(status: int, payload: object) -> Response:
    return Response(status, json.dumps(payload), "application/json")


def _html(status: int, body: str) -> Response:
    return Response(status, body, "text/html; charset=utf-8")


class OperatorApp:
    """A framework-free, JWT-authenticated operator surface for RGNR8 staff.

    Auth: a bearer HS256 JWT is verified through the same `verify_jwt` the web
    app uses; its `sub` must resolve to a **platform role** in the directory
    (operator/support) or the request is rejected. `support` is read-only;
    `operator` may onboard/provision. `clock` returns the current
    (timezone-aware) `datetime` — the fleet report reads it directly and JWT
    expiry is checked against its epoch, so the whole surface stays
    deterministic under test.
    """

    def __init__(
        self,
        fleet: Fleet,
        billing: BillingService,
        users: UserDirectory,
        audit: AuditSink,
        jwt_secret: str,
        *,
        clock: Callable[[], datetime],
        auth_service: AuthService | None = None,
    ) -> None:
        self._fleet = fleet
        self._billing = billing
        self._dir = users
        self._audit = audit
        self._secret = jwt_secret
        self._clock = clock
        # When set, staff can sign into the console in a browser (email+password
        # → session cookie). Without it, the console is bearer-JWT only (as before).
        self._auth_service = auth_service
        # One shared admin path with the platform API: provision + entitlement-
        # checked onboarding + owner seating + audit, all in PlatformAdmin.
        self._admin = PlatformAdmin(
            billing, fleet, users, audit, clock=lambda: self._epoch()
        )
        # Onboarding metadata: the chosen COA template + cutover/go-live status.
        self._onboarding = OnboardingRegistry()

    # --- clock ---------------------------------------------------------------
    def _epoch(self) -> int:
        return int(self._clock().timestamp())

    # --- auth ----------------------------------------------------------------
    def _staff(self, req: Request) -> "tuple[str, Role] | Response":
        """Resolve the authenticated RGNR8-staff principal, or a 401/403.

        Verifies the bearer JWT, reads `sub`, and requires that subject to hold a
        platform role in the directory. Returns `(subject, platform_role)` on
        success, else the error `Response` to return."""
        auth = req.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        if token is None:
            # browser session: the login cookie the console's sign-in set
            token = _cookie(req.headers, "rgnr8_operator")
        if not token:
            return _json(401, {"error": "missing bearer token"})
        try:
            claims = verify_jwt(token, self._secret, now=self._epoch())
        except JwtError:
            return _json(401, {"error": "invalid or expired token"})
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            return _json(401, {"error": "token carries no subject"})
        role = self._dir.platform_role(sub)
        if role is None or not role.is_platform:
            return _json(403, {"error": "not RGNR8 staff"})
        return (sub, role)

    def _login_post(self, req: Request) -> Response:
        """Verify a staff credential and, only for a platform role, set a session
        cookie. Non-committal on failure (never reveals why). Requires an
        AuthService (the credential store) to be wired."""
        if self._auth_service is None:
            return _html(400, render_operator_login(error="Browser login is not configured."))
        data = self._form_or_json(req.body)
        email = str(data.get("email", "")).strip()
        password = str(data.get("password", ""))
        subject = self._auth_service.login(email, password) if "@" in email else None
        if subject is None:
            return _html(401, render_operator_login(error="Invalid email or password."))
        role = self._dir.platform_role(subject)
        if role is None or not role.is_platform:
            # authenticated, but not RGNR8 staff — no console access
            return _html(403, render_operator_login(error="Your account isn't RGNR8 staff."))
        token = sign_jwt({"sub": subject, "role": role.value, "exp": self._epoch() + 8 * 3600},
                         self._secret)
        cookie = f"rgnr8_operator={token}; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax"
        self._audit.record(subject, "operator.login", self._epoch(), detail=role.value)
        return _redirect("/operator", (("Set-Cookie", cookie),))

    @staticmethod
    def _form_or_json(body: str) -> dict[str, object]:
        if not body:
            return {}
        stripped = body.lstrip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        # form-encoded (a browser <form> POST)
        from urllib.parse import parse_qsl
        return {k: v for k, v in parse_qsl(body)}

    # --- routing -------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        route = req.route

        # health is unauthenticated (load balancers / deploy checks)
        if route == "/health":
            return _json(200, {"status": "ok"})

        # browser sign-in (unauthenticated): only when a credential service is wired
        if route == "/operator/login" and req.method == "GET":
            if self._auth_service is None:
                return _json(404, {"error": "browser login is not configured"})
            return _html(200, render_operator_login())
        if route == "/operator/login" and req.method == "POST":
            return self._login_post(req)
        if route == "/operator/logout":
            return _redirect("/operator/login",
                             (("Set-Cookie", "rgnr8_operator=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"),))

        principal = self._staff(req)
        if isinstance(principal, Response):
            return principal
        subject, role = principal

        if route == "/operator" and req.method == "GET":
            return self._console(subject)

        if route == "/operator/onboard" and req.method == "POST":
            # provisioning is operator-only; support is read-only.
            if role is not Role.OPERATOR:
                return _json(403, {"error": "onboarding requires the operator role"})
            return self._onboard(req, subject)

        if route == "/operator/audit" and req.method == "GET":
            return self._audit_json(req)

        # The catalog of chart-of-accounts templates (by business category) an
        # operator can pick from at onboarding.
        if route == "/operator/coa-templates" and req.method == "GET":
            return _json(200, {"templates": category_catalog()})

        # View-as: mint a short-lived token to see a client's account exactly as
        # one of its roles does. Any platform role may launch it (it's audited +
        # time-boxed + globally toggleable); support included, for debugging.
        if route == "/operator/view-as" and req.method == "POST":
            return self._view_as(req, subject)

        # The global on/off switch for view-as is an operator-only security
        # setting ("turn it off once we're live").
        if route == "/operator/view-as/toggle" and req.method == "POST":
            if role is not Role.OPERATOR:
                return _json(403, {"error": "toggling view-as requires the operator role"})
            return self._view_as_toggle(req, subject)

        # --- parameterized management routes ---
        parts = [p for p in route.split("/") if p]

        # /operator/tenant/<id>/users — per-tenant user & role management
        if len(parts) == 4 and parts[0] == "operator" and parts[1] == "tenant" and parts[3] == "users":
            tenant_id = parts[2]
            if req.method == "GET":
                return self._tenant_users(tenant_id)
            if req.method == "POST":
                if role is not Role.OPERATOR:
                    return _json(403, {"error": "managing users requires the operator role"})
                return self._tenant_users_set(req, tenant_id, subject)

        # /operator/account/<id>/plan — change a client's billing plan
        if (len(parts) == 4 and parts[0] == "operator" and parts[1] == "account"
                and parts[3] == "plan" and req.method == "POST"):
            if role is not Role.OPERATOR:
                return _json(403, {"error": "changing a plan requires the operator role"})
            return self._account_plan(req, parts[2], subject)

        # /operator/tenant/<id>/cutover — mark / read RGNR8 go-live (system of record)
        if len(parts) == 4 and parts[0] == "operator" and parts[1] == "tenant" and parts[3] == "cutover":
            tenant_id = parts[2]
            if req.method == "GET":
                return self._cutover_status(tenant_id)
            if req.method == "POST":
                if role is not Role.OPERATOR:
                    return _json(403, {"error": "cutover requires the operator role"})
                return self._cutover_mark(req, tenant_id, subject)

        # /operator/tenant/<id>/go-live — build the go-live/1 request (seed COA +
        # opening balances) for the TS core, and mark the tenant live.
        if (len(parts) == 4 and parts[0] == "operator" and parts[1] == "tenant"
                and parts[3] == "go-live" and req.method == "POST"):
            if role is not Role.OPERATOR:
                return _json(403, {"error": "go-live requires the operator role"})
            return self._go_live(req, parts[2], subject)

        return _json(404, {"error": "not found"})

    # --- handlers ------------------------------------------------------------
    def _console(self, operator: str) -> Response:
        report = build_ops_report(self._fleet, self._clock())
        live = frozenset(t for t in self._fleet.tenants if self._onboarding.is_live(t))
        return _html(200, render_operator_console(
            report, operator=operator, coa_templates=category_catalog(), live_tenants=live,
        ))

    def _onboard(self, req: Request, operator: str) -> Response:
        try:
            data = json.loads(req.body) if req.body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(data, dict):
            return _json(400, {"error": "body must be a JSON object"})

        try:
            account_id = str(data["account_id"])
            tenant_id = str(data["tenant_id"])
            name = str(data["name"])
            recipient = str(data["recipient"])
            owner_email = str(data["owner_email"])
            dto = data["dto"]
        except KeyError as exc:
            return _json(400, {"error": f"missing required field {exc.args[0]!r}"})
        if not isinstance(dto, (dict, str)):
            return _json(400, {"error": "dto must be a forecast-inputs/1 object or JSON string"})

        # Optional chart-of-accounts template choice (validated before any side
        # effects). Recorded on success so the TS core seeds the right chart.
        coa_category = str(data.get("coa_category", "")).strip()
        if coa_category and not is_valid_category(coa_category):
            return _json(400, {"error": f"unknown COA category {coa_category!r}"})

        try:
            minimum_cash = Money.from_decimal(str(data.get("minimum_cash")))
        except ValueError:
            return _json(400, {"error": "minimum_cash is not a valid amount"})

        try:
            # Provision the billing account first if it doesn't exist yet, then
            # attach the business (entitlement-checked in billing) and seat its
            # owner — the same path PlatformAdmin exposes to the platform API.
            if self._billing.get_account(account_id) is None:
                tier = self._resolve_tier(data.get("tier"))
                self._admin.provision_account(
                    account_id, name, owner_email, tier, operator=operator
                )
            bt = self._admin.onboard_business(
                account_id, tenant_id, name, recipient, dto, minimum_cash,
                owner_email, operator=operator,
            )
        except EntitlementError as exc:
            # the plan forbids another business — a clean, retryable 402, never a 500
            return _json(402, {"error": str(exc)})
        except PlatformError as exc:
            return _json(403, {"error": str(exc)})
        except BillingError as exc:
            return _json(409, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            # a malformed DTO / bad amount surfaces as a 400, not a 500
            return _json(400, {"error": f"could not onboard: {exc}"})

        if coa_category:
            self._onboarding.set_coa_category(tenant_id, coa_category)
        summary = self._summary(bt, account_id, owner_email)
        summary["coa_category"] = coa_category or None
        return _json(201, summary)

    def _view_as(self, req: Request, subject: str) -> Response:
        """Launch a view-as session: mint a token to see a tenant as a client role.
        Body: {"tenant_id": "...", "role": "owner|controller|bookkeeper|accountant|viewer",
        "ttl_seconds"?: int}. A missing/empty role means a plain support session."""
        try:
            data = json.loads(req.body) if req.body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(data, dict):
            return _json(400, {"error": "body must be a JSON object"})
        tenant_id = str(data.get("tenant_id", "")).strip()
        if not tenant_id:
            return _json(400, {"error": "tenant_id is required"})
        role_raw = data.get("role")
        view_as: Role | None = None
        if role_raw not in (None, ""):
            try:
                view_as = Role(str(role_raw))
            except ValueError:
                return _json(400, {"error": f"unknown role {role_raw!r}"})
        ttl = data.get("ttl_seconds", 900)
        try:
            ttl_seconds = int(ttl)
        except (TypeError, ValueError):
            return _json(400, {"error": "ttl_seconds must be an integer"})

        try:
            token = self._admin.view_as(subject, tenant_id, view_as, ttl_seconds=ttl_seconds)
        except PlatformError as exc:
            return _json(403, {"error": str(exc)})
        return _json(200, {
            "tenant_id": tenant_id,
            "view_as": view_as.value if view_as is not None else None,
            "expires_in": ttl_seconds,
            "token": token,
        })

    def _cutover_status(self, tenant_id: str) -> Response:
        if tenant_id not in self._fleet.tenants:
            return _json(404, {"error": f"unknown tenant {tenant_id}"})
        rec = self._onboarding.cutover(tenant_id)
        return _json(200, {
            "tenant_id": tenant_id,
            "live": rec is not None,
            "coa_category": self._onboarding.coa_category(tenant_id),
            "cutover": None if rec is None else {
                "source_system": rec.source_system,
                "cutover_date": rec.cutover_date,
                "marked_by": rec.marked_by,
                "marked_at": rec.marked_at,
                "opening_entry_id": rec.opening_entry_id or None,
            },
        })

    def _cutover_mark(self, req: Request, tenant_id: str, operator: str) -> Response:
        """Mark a client live on RGNR8 (system of record). Body:
        {"source_system": "quickbooks|xero|other", "cutover_date": "YYYY-MM-DD",
        "opening_entry_id"?: "..."}. The opening-balance journal itself is posted
        by the TS core; this records the go-live for the console + audit trail."""
        if tenant_id not in self._fleet.tenants:
            return _json(404, {"error": f"unknown tenant {tenant_id}"})
        try:
            data = json.loads(req.body) if req.body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(data, dict):
            return _json(400, {"error": "body must be a JSON object"})
        source_system = str(data.get("source_system", "")).strip()
        cutover_date = str(data.get("cutover_date", "")).strip()
        if not cutover_date:
            return _json(400, {"error": "cutover_date is required"})
        try:
            rec = self._onboarding.mark_cutover(
                tenant_id, source_system, cutover_date,
                marked_by=operator, marked_at=self._epoch(),
                opening_entry_id=str(data.get("opening_entry_id", "")),
            )
        except OnboardingError as exc:
            return _json(400, {"error": str(exc)})
        self._audit.record(operator, "tenant.cutover", self._epoch(), tenant_id=tenant_id,
                           detail=f"{rec.source_system}@{rec.cutover_date}")
        return _json(200, {"tenant_id": tenant_id, "live": True,
                           "source_system": rec.source_system, "cutover_date": rec.cutover_date})

    def _go_live(self, req: Request, tenant_id: str, operator: str) -> Response:
        """Prepare a client's go-live: build the go-live/1 request (COA template +
        the source's as-of trial balance → opening balances) that the TS core
        executes, record it, and mark the tenant live. Body:
        {"source_system": "...", "cutover_date": "YYYY-MM-DD",
         "source_accounts": [{"code","name","balance_minor","subtype"|"type"}],
         "currency"?, "opening_balance_equity_code"?}. The COA template is taken
        from what was chosen at onboarding (override with "coa_category")."""
        if tenant_id not in self._fleet.tenants:
            return _json(404, {"error": f"unknown tenant {tenant_id}"})
        try:
            data = json.loads(req.body) if req.body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(data, dict):
            return _json(400, {"error": "body must be a JSON object"})
        cutover_date = str(data.get("cutover_date", "")).strip()
        source_system = str(data.get("source_system", "")).strip()
        if not cutover_date:
            return _json(400, {"error": "cutover_date is required"})
        raw_accounts = data.get("source_accounts")
        if not isinstance(raw_accounts, list):
            return _json(400, {"error": "source_accounts must be a list (the source trial balance)"})
        category = data.get("coa_category")
        coa_category = str(category) if category else self._onboarding.coa_category(tenant_id)

        try:
            request = build_go_live_request(
                tenant_id, source_system, cutover_date,
                [a for a in raw_accounts if isinstance(a, dict)],
                opening_balance_equity_code=str(data.get("opening_balance_equity_code", "3010")),
                currency=str(data.get("currency", "USD")),
                coa_category=coa_category,
            )
        except OnboardingError as exc:
            return _json(400, {"error": str(exc)})

        accounts = request["source_accounts"]
        n_accounts = len(accounts) if isinstance(accounts, list) else 0
        self._onboarding.set_go_live_request(tenant_id, request)
        self._onboarding.mark_cutover(tenant_id, source_system, cutover_date,
                                      marked_by=operator, marked_at=self._epoch())
        self._audit.record(operator, "tenant.go_live", self._epoch(), tenant_id=tenant_id,
                           detail=f"{source_system}@{cutover_date} accounts={n_accounts}")
        return _json(200, {"tenant_id": tenant_id, "live": True, "go_live_request": request})

    def _tenant_users(self, tenant_id: str) -> Response:
        if tenant_id not in self._fleet.tenants:
            return _json(404, {"error": f"unknown tenant {tenant_id}"})
        return _json(200, {"tenant_id": tenant_id, "members": self._admin.list_members(tenant_id),
                           "assignable_roles": [r.value for r in Role if not r.is_platform]})

    def _tenant_users_set(self, req: Request, tenant_id: str, operator: str) -> Response:
        """Add/change/remove a client member. Body: {"email": "...", "name"?: "...",
        "role": "owner|controller|bookkeeper|accountant|viewer"|null}."""
        if tenant_id not in self._fleet.tenants:
            return _json(404, {"error": f"unknown tenant {tenant_id}"})
        try:
            data = json.loads(req.body) if req.body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(data, dict) or not str(data.get("email", "")).strip():
            return _json(400, {"error": "email is required"})
        email = str(data["email"]).strip()
        name = str(data.get("name", ""))
        role_raw = data.get("role")
        role: Role | None = None
        if role_raw not in (None, ""):
            try:
                role = Role(str(role_raw))
            except ValueError:
                return _json(400, {"error": f"unknown role {role_raw!r}"})
        try:
            self._admin.set_member_role(tenant_id, email, role, operator=operator, name=name)
        except PlatformError as exc:
            return _json(403, {"error": str(exc)})
        return _json(200, {"tenant_id": tenant_id, "members": self._admin.list_members(tenant_id)})

    def _account_plan(self, req: Request, account_id: str, operator: str) -> Response:
        """Change a client's billing plan. Body: {"tier": "self_serve|assisted|co_delivery"}."""
        if self._billing.get_account(account_id) is None:
            return _json(404, {"error": f"unknown account {account_id}"})
        try:
            data = json.loads(req.body) if req.body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(data, dict) or "tier" not in data:
            return _json(400, {"error": "tier is required"})
        try:
            tier = Tier(str(data["tier"]))
        except ValueError:
            return _json(400, {"error": f"unknown tier {data['tier']!r}"})
        try:
            acct = self._admin.set_plan(account_id, tier, operator=operator)
        except BillingError as exc:
            return _json(409, {"error": str(exc)})
        return _json(200, {"account_id": account_id, "tier": acct.tier.value})

    def _view_as_toggle(self, req: Request, subject: str) -> Response:
        """Enable/disable developer view-as globally. Body: {"enabled": bool}."""
        try:
            data = json.loads(req.body) if req.body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(data, dict) or "enabled" not in data:
            return _json(400, {"error": "body must be {\"enabled\": true|false}"})
        enabled = bool(data["enabled"])
        self._admin.set_view_as_enabled(enabled, operator=subject)
        return _json(200, {"view_as_enabled": self._admin.view_as_enabled})

    def _resolve_tier(self, raw: object) -> Tier:
        if raw is None:
            return Tier.SELF_SERVE
        try:
            return Tier(str(raw))
        except ValueError:
            raise ValueError(f"unknown tier {raw!r}")

    @staticmethod
    def _summary(bt: BetaTenant, account_id: str, owner_email: str) -> dict[str, object]:
        return {
            "tenant_id": bt.tenant_id,
            "account_id": account_id,
            "name": bt.name,
            "recipient": bt.recipient,
            "owner_email": owner_email,
            "minimum_cash": bt.config.minimum_cash.to_decimal_string(),
            "cash_today": bt.inputs.opening.available.to_decimal_string(),
        }

    def _audit_json(self, req: Request) -> Response:
        q = req.query
        tenant = q.get("tenant") or None
        account = q.get("account") or None
        events = self._audit.events(tenant_id=tenant, account_id=account)
        return _json(200, {
            "tenant": tenant,
            "account": account,
            "events": [
                {
                    "seq": e.seq,
                    "actor": e.actor,
                    "action": e.action,
                    "at": e.at,
                    "tenant_id": e.tenant_id,
                    "account_id": e.account_id,
                    "target": e.target,
                    "detail": e.detail,
                }
                for e in events
            ],
        })


# --- WSGI adapter ------------------------------------------------------------

WsgiEnviron = dict[str, object]
StartResponse = Callable[[str, list[tuple[str, str]]], object]

_STATUS_TEXT = {
    200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
    402: "Payment Required", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
    500: "Internal Server Error",
}

MAX_BODY_BYTES = 1_048_576  # 1 MiB — cap bodies before reading into memory


def _headers_from_environ(environ: WsgiEnviron) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").lower()] = str(value)
    if "CONTENT_TYPE" in environ:
        headers["content-type"] = str(environ["CONTENT_TYPE"])
    return headers


def _read_body(environ: WsgiEnviron) -> str:
    try:
        length = int(str(environ.get("CONTENT_LENGTH") or "0"))
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return ""
    stream = environ.get("wsgi.input")
    if stream is None or not hasattr(stream, "read"):
        return ""
    raw = stream.read(min(length, MAX_BODY_BYTES))
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def request_from_environ(environ: WsgiEnviron) -> Request:
    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    path = str(environ.get("PATH_INFO", "/")) or "/"
    query = str(environ.get("QUERY_STRING", ""))
    if query:
        path = f"{path}?{query}"
    return Request(method=method, path=path,
                   headers=_headers_from_environ(environ), body=_read_body(environ))


def operator_wsgi(app: OperatorApp) -> Callable[[WsgiEnviron, StartResponse], Iterable[bytes]]:
    """Wrap an `OperatorApp` as a WSGI callable (mirrors `rgnr8_web.wsgi_app`)."""

    def application(environ: WsgiEnviron, start_response: StartResponse) -> Iterable[bytes]:
        try:
            resp = app.handle(request_from_environ(environ))
        except Exception:
            # never leak a stack trace to a client
            resp = Response(500, '{"error":"internal server error"}')
        status_line = f"{resp.status} {_STATUS_TEXT.get(resp.status, 'OK')}"
        body = resp.body_bytes()
        headers = [*resp.headers.items(), ("Content-Length", str(len(body)))]
        start_response(status_line, headers)
        return [body]

    return application
