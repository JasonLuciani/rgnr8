"""A framework-free, multi-tenant HTTP application for the RGNR8 owner surface.

The core is a pure ``WebApp.handle(Request) -> Response`` function — no sockets,
so it is fully unit-testable. ``server.py`` wraps it in ``http.server`` for a
runnable process. Auth is a bearer token per tenant; every route is tenant-scoped
and a token may only reach its own tenant.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import escape
from urllib.parse import parse_qs, urlsplit

from rgnr8_forecast import (
    CustomerHistory,
    ForecastConfig,
    ForecastInputs,
    ForecastResult,
    Money,
    run_forecast,
)
from rgnr8_forecast.brand import format_money
from rgnr8_scenario import (
    DelayCustomerPayment,
    OneTimeFlow,
    Scenario,
    ScenarioDiff,
    SetMinimumCash,
    customer_pays_late,
    hire_employee,
    one_time_expense,
    run_scenario,
    take_loan,
)
from rgnr8_scenario.adjustments import Adjustment
from rgnr8_ar import ARReport, ChaseItem, CollectionNudge, ar_report, chase_list, draft_nudge
from rgnr8_categorize import (
    CategorizedTxn,
    Categorizer,
    LearnedModel,
    RuleSet,
    Txn,
)
from rgnr8_briefing import (
    WeeklyBriefing,
    ask,
    build_briefing,
    render_text,
    render_today_html,
    validate_briefing,
)
from rgnr8_billing import Account, UsageSummary
from rgnr8_reports import (
    BASELINE_REPORTS,
    DataContext,
    InMemorySavedReportStore,
    ReportSpec,
    ReportSpecError,
    SavedReportStore,
    Transaction as ReportTransaction,
    build_report,
    render as render_report,
    render_csv,
    render_html as render_report_html,
    render_pdf as render_report_pdf,
    render_xlsx as render_report_xlsx,
    to_json,
)
from rgnr8_qbo import QboConnectService, QboStatus
from .qbo_sync import QboSyncSummary, build_inputs_from_qbo
from .qbo_ledger import LedgerSyncSummary, sync_qbo_to_ledger
from .ledger_forecast import LedgerFacts, forecast_from_ledger, provenance_split
from .multipart import MultipartError, parse_multipart
from .attachment_screens import render_attachments, render_attachments_unavailable
from .ten99_screens import render_ten99, render_ten99_unavailable
from .recurring_screens import (
    render_recurring, render_recurring_unavailable, render_run_result,
)
from .job_screens import render_job, render_jobs, render_jobs_unavailable
from .estimate_screens import (
    render_estimate, render_estimates, render_estimates_unavailable,
    render_pipeline, render_pipeline_unavailable,
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
from .owner_reports import render_owner_report
from .ledger_client import LedgerClient
from .arap_screens import render_aging as render_arap_aging, render_documents
from .reconcile_screens import (
    render_import_result, render_pick_account, render_reconcile,
)
from .reporting_screens import render_budget, render_general_ledger
from .dimension_screens import (
    render_dimension_report,
    render_dimensions,
    render_dimensions_unavailable,
)
from .payroll_screens import (
    render_payroll_home,
    render_payroll_run,
    render_payroll_unavailable,
)
from .inbox_screens import (
    render_actioned,
    render_feed_unavailable,
    render_inbox,
    render_rules,
)
from .books_screens import (
    render_books_home,
    render_books_statements,
    render_chart_of_accounts,
    render_register,
    unavailable as render_books_unavailable,
)
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
    render_ar_body,
    render_briefing_body,
    render_cash_body,
    render_close_body,
    render_packages_body,
    render_reports_list,
    render_scenario_body,
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
    # Text responses carry a ``str`` body; binary exports (PDF, XLSX) carry
    # ``bytes``. The serialization layer (WSGI + stdlib server) calls
    # :meth:`body_bytes`, so both paths write the correct bytes with an accurate
    # Content-Length.
    body: str | bytes
    content_type: str = "application/json"
    # extra response headers (e.g. Set-Cookie, Location) — merged into `headers`
    extra_headers: tuple[tuple[str, str], ...] = ()

    @property
    def headers(self) -> dict[str, str]:
        h = {"Content-Type": self.content_type, "X-Content-Type-Options": "nosniff"}
        for k, v in self.extra_headers:
            h[k] = v
        return h

    def body_bytes(self) -> bytes:
        """The response body as bytes — UTF-8-encoded when it is text, passed
        through unchanged when it is already binary (a PDF/XLSX export)."""
        if isinstance(self.body, bytes):
            return self.body
        return self.body.encode("utf-8")


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


def _json_seq(v: object) -> "list[object]":
    return list(v) if isinstance(v, (list, tuple)) else []


def _minor_decimal(v: object) -> str:
    """Integer minor units to a decimal string, exactly — never through a float."""
    try:
        n = int(str(v))
    except (TypeError, ValueError):
        return "0.00"
    sign = "-" if n < 0 else ""
    whole, frac = divmod(abs(n), 100)
    return f"{sign}{whole}.{frac:02d}"


def _provenance_note(facts: "LedgerFacts", split: "dict[str, int]") -> str:
    """Say which half of the forecast is known and which is assumed.

    A forecast that mixes "what the bank holds" with "what we hope to win" and
    presents both in the same typeface invites the reader to trust the wrong
    half. This is one line, above the chart, saying where the numbers came from.
    """
    from html import escape

    if facts.problems:
        detail = escape("; ".join(facts.problems[:2]))
        return (
            '<div class="banner warn">Working from your saved assumptions — the books '
            f"couldn't be read just now ({detail}). Cash and open items may be out of "
            "date.</div>"
        )
    known = split.get("from_the_books", 0)
    assumed = split.get("assumed", 0)
    return (
        '<div class="banner good">'
        f"Cash on hand and {known} open item(s) come straight from your books"
        f"{f'; {assumed} forward item(s) are your assumptions' if assumed else ''}. "
        "</div>"
    )


def _percent_to_ppm(raw: str) -> int:
    """"8.25" -> 82500. Exact: a tax rate never passes through a float."""
    text = raw.strip().rstrip("%").strip()
    if not text:
        return 0
    negative = text.startswith("-")
    if negative:
        raise ValueError("a tax rate cannot be negative")
    if not all(c.isdigit() or c == "." for c in text) or text.count(".") > 1:
        raise ValueError(f"not a valid tax rate: {raw!r}")
    whole, _, frac = text.partition(".")
    if len(frac) > 4:
        raise ValueError("a tax rate finer than four decimal places is a typo")
    return int(whole or "0") * 10_000 + int((frac or "0").ljust(4, "0"))


def _quantity_to_milli(raw: str) -> int:
    """"2.5" -> 2500. Quantities carry three decimals and never touch a float:
    half a day of labour has to cost exactly half a day."""
    text = raw.strip().replace(",", "")
    if not text:
        return 1000
    if text.startswith("-"):
        raise ValueError("a quantity cannot be negative")
    if not all(c.isdigit() or c == "." for c in text) or text.count(".") > 1:
        raise ValueError(f"not a valid quantity: {raw!r}")
    whole, _, frac = text.partition(".")
    if len(frac) > 3:
        raise ValueError("a quantity finer than three decimal places is a typo")
    return int(whole or "0") * 1000 + int((frac or "0").ljust(3, "0"))


def _suggested_code(suggestion: object) -> str:
    if isinstance(suggestion, dict):
        return str(suggestion.get("account_code", ""))
    return ""


def _qs_escape(text: str) -> str:
    """Percent-encode a short message for a redirect query string."""
    from urllib.parse import quote
    return quote(text[:200], safe="")


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


# Per-request "view as this role" for RGNR8 staff (see rgnr8_ops.PlatformAdmin.
# view_as). A ContextVar keeps it request-scoped and thread/async-safe without
# threading an extra argument through every authorization call site. `handle`
# resets it on each request; only a real platform user's token can set it.
_VIEW_AS: contextvars.ContextVar[Role | None] = contextvars.ContextVar("rgnr8_view_as", default=None)


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
        saved_reports: SavedReportStore | None = None,
        report_clock: "Callable[[], datetime] | None" = None,
        qbo: QboConnectService | None = None,
    ) -> None:
        # QuickBooks Online connect service (OAuth acquisition + token store).
        # When absent, the connect surface reports "not configured" rather than 404.
        self._qbo = qbo
        # last successful QBO sync summary, per tenant (for the connect page).
        self._qbo_last_sync: dict[str, QboSyncSummary] = {}
        self._qbo_last_ledger_sync: dict[str, LedgerSyncSummary] = {}
        # What the books last told the forecast, so a screen can say where a
        # number came from rather than presenting facts and guesses alike.
        self._ledger_facts: dict[str, LedgerFacts] = {}
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
        # optional hand-authored categorization rules per tenant (@rgnr8/categorize).
        # The learned model is mined from the tenant's already-categorized register
        # at request time, so suggestions track the bookkeeper's own history.
        self._rulesets: dict[str, RuleSet] = {}
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
        # reporting (rgnr8-reports): per-tenant saved custom report definitions,
        # plus the optional data sources a report composes that the app doesn't
        # already hold (financial-statements/1 dict, a budget, a usage summary).
        # `report_clock` stamps `generated_at` deterministically (never a wall clock).
        self._saved_reports: SavedReportStore = (
            saved_reports if saved_reports is not None else InMemorySavedReportStore()
        )
        self._report_clock: Callable[[], datetime] = (
            report_clock if report_clock is not None else (lambda: datetime.now(timezone.utc))
        )
        self._financial_statements: dict[str, Mapping[str, object]] = {}
        # Owner-facing ledger reports, keyed (tenant, kind) — the JSON contracts
        # the TS core emits (aging/1, budget-vs-actual/1, retained-earnings/1).
        self._owner_reports: dict[tuple[str, str], Mapping[str, object]] = {}
        # The ledger service client. When absent, the books screens say so plainly
        # rather than pretending the client has no transactions.
        self._ledger: LedgerClient | None = None
        self._budgets: dict[str, Mapping[str, Money]] = {}
        self._usage: dict[str, UsageSummary] = {}
        self._billing_accounts: dict[str, Account] = {}

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

    def add_categorizer(self, tenant_id: str, ruleset: RuleSet | None = None) -> None:
        """Turn on auto-categorization for a tenant's register. `ruleset` is the
        optional hand-authored rule layer (@rgnr8/categorize); the learned layer is
        built from the tenant's already-categorized transactions on each render, so
        the "For review" queue arrives pre-triaged. With no ruleset, suggestions come
        from learned history alone."""
        self._rulesets[tenant_id] = ruleset if ruleset is not None else RuleSet(())

    @staticmethod
    def _to_txn(tx: BankTransaction) -> Txn:
        """Project a `BankTransaction` onto the categorize `Txn` shape."""
        return Txn(
            id=tx.id,
            description=tx.description,
            counterparty=tx.counterparty,
            amount_minor=tx.amount.minor_units,
            currency=tx.amount.currency,
        )

    def _categorizer_for(self, t: _Tenant) -> Categorizer | None:
        """Build a `Categorizer` for a tenant: its ruleset (if any) over a
        `LearnedModel` mined from its already-categorized register. Returns None
        when there's nothing to learn from and no ruleset — nothing to suggest."""
        ruleset = self._rulesets.get(t.tenant_id)
        rows = self._txns.get(t.tenant_id, [])
        history = [
            CategorizedTxn(
                id=tx.id,
                description=tx.description,
                counterparty=tx.counterparty,
                amount_minor=tx.amount.minor_units,
                category=tx.category,
                currency=tx.amount.currency,
            )
            for tx in rows
            if tx.category != "Uncategorized"
        ]
        if ruleset is None and not history:
            return None
        model = LearnedModel.from_history(history)
        return Categorizer(ruleset=ruleset if ruleset is not None else RuleSet(()), model=model)

    def _suggestions_for(self, t: _Tenant) -> dict[str, tuple[str, float]]:
        """The auto-categorize suggestions for a tenant's for-review, uncategorized
        lines: id -> (category, confidence). Only real suggestions (a rule or a
        learned match) are included; a novel line the engine can't place is omitted
        so the register shows nothing rather than a spurious 0%-confidence hint."""
        cat = self._categorizer_for(t)
        if cat is None:
            return {}
        out: dict[str, tuple[str, float]] = {}
        for tx in self._txns.get(t.tenant_id, []):
            if tx.category != "Uncategorized" or not tx.needs_review:
                continue
            s = cat.suggest(self._to_txn(tx))
            if s.source != "none" and s.confidence > 0.0:
                out[tx.id] = (s.category, s.confidence)
        return out

    def add_close(self, tenant_id: str, board: CloseBoard) -> None:
        """Attach a tenant's month-end close board (mirrors the `@rgnr8/close`
        calendar). Shown on the Close screen; advanced/sealed through the API."""
        self._close[tenant_id] = board

    def add_financial_statements(self, tenant_id: str, statements: Mapping[str, object]) -> None:
        """Attach a tenant's ``financial-statements/1`` contract (the JSON the TS
        ``@rgnr8/financial-statements`` package emits). Feeds the P&L / balance-sheet
        / cash-flow sections of the reporting surface; absent → those degrade."""
        self._financial_statements[tenant_id] = statements

    # Owner-report ids (URL) -> renderer kind.
    _OWNER_REPORT_IDS: "dict[str, str]" = {
        "receivables-aging": "aging_ar",
        "payables-aging": "aging_ap",
        "budget": "budget",
        "retained-earnings": "retained_earnings",
    }

    def set_ledger(self, client: LedgerClient) -> None:
        """Wire the ledger service so owners can see and post to their books."""
        self._ledger = client

    def add_owner_report(self, tenant_id: str, kind: str, data: Mapping[str, object]) -> None:
        """Attach an owner-facing ledger report contract (aging/budget/retained
        earnings) the TS core emitted, surfaced under /t/<tenant>/reports/<id>."""
        if kind not in {"aging_ar", "aging_ap", "budget", "retained_earnings"}:
            raise ValueError(f"unknown owner report kind {kind!r}")
        self._owner_reports[(tenant_id, kind)] = data

    def add_budget(self, tenant_id: str, budget: Mapping[str, Money]) -> None:
        """Attach a tenant's per-category budget (category → budgeted amount). Feeds
        the Budget vs Actual report; absent → that report degrades gracefully."""
        self._budgets[tenant_id] = dict(budget)

    def add_usage(
        self, tenant_id: str, usage: UsageSummary, account: Account | None = None
    ) -> None:
        """Attach a tenant's metered-usage summary (and optionally its billing
        account, for the invoice preview). Feeds the Usage & Billing report."""
        self._usage[tenant_id] = usage
        if account is not None:
            self._billing_accounts[tenant_id] = account

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
        """The inputs the forecast actually runs on.

        With a ledger configured this is a HYBRID: cash, receivables, payables
        and payroll liabilities are read from the books (facts), while pipeline,
        recurring plans and planned one-offs stay the owner's assumptions. Every
        item carries a provenance saying which half it came from, so a surprising
        forecast can be traced to a number we know or a number we guessed.

        Without a ledger, the owner's inputs stand alone, exactly as before."""
        inputs = t.inputs
        if self._ledger is not None:
            as_of = t.inputs.opening.as_of
            merged, facts = forecast_from_ledger(
                self._ledger, t.tenant_id, inputs, as_of, currency=t.config.currency,
            )
            self._ledger_facts[t.tenant_id] = facts
            inputs = merged
        if not t.payment_overrides:
            return inputs
        histories = {h.customer_id: h for h in inputs.customer_histories}
        for customer_id, days in t.payment_overrides.items():
            histories[customer_id] = CustomerHistory(customer_id=customer_id, override_days_late=days)
        return dataclasses.replace(inputs, customer_histories=tuple(histories.values()))

    # --- forecast (cached per tenant; invalidated on override) --------------
    def _forecast(self, t: _Tenant) -> ForecastResult:
        # With a ledger behind it the forecast depends on the books, which change
        # whenever anyone posts. Caching it would show a stale cash position
        # moments after a transaction was accepted, so it is recomputed.
        if self._ledger is not None:
            t._cache = run_forecast(self._effective_inputs(t), self._effective_config(t))
            t._dirty = False
            return t._cache
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

    def _resolve_view_as(self, req: Request, subject: str) -> Role | None:
        """The client role RGNR8 staff want to view this tenant *as*, from the
        token's `view_as` claim. Honored ONLY when `subject` actually holds a
        platform role — a normal user's token can never grant it — and only for a
        real (non-platform) client role. Cheap-guarded: the (common) non-staff
        case returns before any extra token verification."""
        if self._users is None:
            return None
        if self._users.platform_role(subject) is None:
            return None  # not RGNR8 staff → never view-as, no matter the claim
        raw = self._view_as_claim(req)
        if raw is None:
            return None
        try:
            role = Role(raw)
        except ValueError:
            return None
        return None if role.is_platform else role

    def _view_as_claim(self, req: Request) -> str | None:
        """Extract the raw `view_as` claim across the same token paths as
        `_principal` (authenticator bearer/cookie, then dev session_secret)."""
        if self._auth is not None:
            vaf = getattr(self._auth, "view_as_for", None)
            if callable(vaf):
                got = vaf(req.headers)
                if isinstance(got, str):
                    return got
                cookie = _cookie(req.headers, "rgnr8_session")
                if cookie is not None:
                    got = vaf({"authorization": f"Bearer {cookie}"})
                    if isinstance(got, str):
                        return got
        if self._session_secret is not None:
            cookie = _cookie(req.headers, "rgnr8_session")
            token = cookie
            if token is None:
                auth = req.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    token = auth[7:].strip()
            if token is not None:
                try:
                    claims = verify_jwt(token, self._session_secret, now=self._session_clock())
                except JwtError:
                    return None
                v = claims.get("view_as")
                return v if isinstance(v, str) else None
        return None

    # --- routing -------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        route = req.route
        _VIEW_AS.set(None)  # reset per request; only a platform token re-sets it

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

        # --- QBO OAuth callback (public: Intuit redirects the owner's browser
        # here; the signed `state` is the CSRF boundary and carries the tenant) ---
        if route == "/oauth/qbo/callback" and req.method == "GET":
            return self._qbo_callback(req)

        principal = self._principal(req)
        if principal is None:
            return _json(401, {"error": "missing or invalid bearer token"})
        token_tenant, subject = principal
        _VIEW_AS.set(self._resolve_view_as(req, subject))

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

        # /t/<tenant>/scenarios  -> what-if planning (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "scenarios":
            return self._require(subject, token_tenant, parts[1], P.VIEW_CASH,
                                 lambda t: self._scenarios_page(subject, t))

        # /t/<tenant>/receivables  -> AR / collections (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "receivables":
            return self._require(subject, token_tenant, parts[1], P.VIEW_CASH,
                                 lambda t: self._receivables_page(subject, t))

        # /t/<tenant>/reports  -> baseline + saved reports index (shell page)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "reports":
            return self._require(subject, token_tenant, parts[1], P.VIEW_CASH,
                                 lambda t: self._reports_page(subject, t))

        # /t/<tenant>/packages  -> sealed financial-package records (shell page)
        # /t/<tenant>/connect -> the connections page (QBO status + connect button)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "connect":
            sync_flag = req.query.get("sync", "")
            return self._require(subject, token_tenant, parts[1], P.MANAGE_CONNECTORS,
                                 lambda t: self._connect_page(subject, t, sync_flag))

        # /t/<tenant>/connect/qbo -> start the QBO OAuth flow (redirect to Intuit)
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "connect" and parts[3] == "qbo":
            return self._require(subject, token_tenant, parts[1], P.MANAGE_CONNECTORS,
                                 self._qbo_begin)

        # /t/<tenant>/connect/qbo/disconnect -> revoke + drop the connection
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "connect"
                and parts[3] == "qbo" and parts[4] == "disconnect" and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.MANAGE_CONNECTORS,
                                 lambda t: self._qbo_disconnect(subject, t))

        # /t/<tenant>/connect/qbo/sync -> pull the connected company's data
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "connect"
                and parts[3] == "qbo" and parts[4] == "sync" and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.MANAGE_CONNECTORS,
                                 lambda t: self._qbo_sync(subject, t))

        if len(parts) == 3 and parts[0] == "t" and parts[2] == "packages":
            return self._require(subject, token_tenant, parts[1], P.VIEW_PACKAGE,
                                 lambda t: self._packages_page(subject, t))

        # /t/<tenant>/audit  -> the who-did-what audit log (shell page, owner-gated)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "audit":
            return self._require(subject, token_tenant, parts[1], P.MANAGE_USERS,
                                 lambda t: self._audit_page(subject, t))

        # --- estimates ---------------------------------------------------------
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "estimates":
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._estimate_save(subject, t, req.body))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._estimates_page(subject, t, req.query))
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "estimates" and req.method == "GET":
            estimate_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._estimate_page(subject, t, estimate_id,
                                                               req.query))
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "estimates"
                and req.method == "POST"):
            estimate_id, action = parts[3], parts[4]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._estimate_action(
                                     subject, t, estimate_id, action, req.body))

        # --- the pipeline ------------------------------------------------------
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "pipeline":
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._pipeline_page(subject, t, req.query))
        if (len(parts) == 3 and parts[0] == "t" and parts[2] == "leads"
                and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._lead_save(subject, t, req.body))
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "leads"
                and parts[4] == "convert" and req.method == "POST"):
            lead_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._lead_convert(subject, t, lead_id, req.body))
        if (len(parts) == 3 and parts[0] == "t" and parts[2] == "opportunities"
                and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._opportunity_save(subject, t, req.body))
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "opportunities"
                and parts[4] in ("win", "lose") and req.method == "POST"):
            opportunity_id, outcome = parts[3], parts[4]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._opportunity_close(
                                     subject, t, opportunity_id, outcome, req.body))

        # --- jobs: the project layer -----------------------------------------
        # /t/<tenant>/jobs -> every job, and the form to start one
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "jobs":
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._job_save(subject, t, req.body))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._jobs_page(subject, t, req.query))
        # /t/<tenant>/jobs/<id> -> the project hub
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "jobs" and req.method == "GET":
            job_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._job_page(subject, t, job_id, req.query))
        # /t/<tenant>/jobs/<id>/budget
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "jobs"
                and parts[4] == "budget" and req.method == "POST"):
            job_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._job_budget_save(subject, t, job_id, req.body))
        # /t/<tenant>/jobs/<id>/bill/<method>
        if (len(parts) == 6 and parts[0] == "t" and parts[2] == "jobs"
                and parts[4] == "bill" and req.method == "POST"):
            job_id, method = parts[3], parts[5]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._job_bill(subject, t, job_id, method, req.body))
        # /t/<tenant>/jobs/<id>/deposits
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "jobs"
                and parts[4] == "deposits" and req.method == "POST"):
            job_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._job_deposit(subject, t, job_id, req.body))

        # --- invoicing (AR) and bills (AP) ---
        if len(parts) >= 3 and parts[0] == "t" and parts[2] in ("invoices", "bills"):
            kind = parts[2]
            if len(parts) == 3 and req.method == "GET":
                return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                     lambda t: self._arap_page(subject, t, kind, req.query))
            if len(parts) == 3 and req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._arap_create(t, kind, req.body))
            if len(parts) == 5 and parts[4] == "payments" and req.method == "POST":
                doc_id = parts[3]
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._arap_pay(subject, t, kind, doc_id, req.body))
            # A credit reduces what is owed; a refund sends money back.
            if (len(parts) == 5 and parts[4] in ("credits", "refunds")
                    and req.method == "POST"):
                doc_id, action = parts[3], parts[4]
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._arap_adjust(subject, t, kind, doc_id,
                                                                 action, req.body))
        # /t/<tenant>/customers|vendors  -> add a party (form POST)
        if (len(parts) == 3 and parts[0] == "t" and parts[2] in ("customers", "vendors")
                and req.method == "POST"):
            kind = parts[2]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._arap_party(t, kind, req.body))
        # /t/<tenant>/receivables|payables/aging  -> the aging report
        if (len(parts) == 4 and parts[0] == "t" and parts[3] == "aging"
                and parts[2] in ("receivables", "payables")):
            side = "ar" if parts[2] == "receivables" else "ap"
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._arap_aging(subject, t, side, req.query))

        # --- attachments (the receipt behind the number) ---
        # /t/<tenant>/files/<id>/download -> the file itself
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "files"
                and parts[4] == "download" and req.method == "GET"):
            attachment_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._attachment_download(t, attachment_id))
        # /t/<tenant>/files/<kind>/<id> -> what evidences this, and an upload form
        if len(parts) == 5 and parts[0] == "t" and parts[2] == "files":
            kind, subject_id = parts[3], parts[4]
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._attachment_upload(
                                         subject, t, kind, subject_id, req))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._attachments_page(
                                     subject, t, kind, subject_id, req.query))
        # /t/<tenant>/files/<kind>/<id>/<attachment>/delete
        if (len(parts) == 7 and parts[0] == "t" and parts[2] == "files"
                and parts[6] == "delete" and req.method == "POST"):
            kind, subject_id, att = parts[3], parts[4], parts[5]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._attachment_delete(
                                     subject, t, kind, subject_id, att))

        # --- payroll ---
        # /t/<tenant>/payroll -> runs, liabilities, and the run form
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "payroll":
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._payroll_create(subject, t, req.body))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._payroll_page(subject, t, req.query))
        # /t/<tenant>/payroll/employees -> add someone to the payroll
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "payroll"
                and parts[3] == "employees" and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._payroll_employee(subject, t, req.body))
        # /t/<tenant>/payroll/remit -> record the tax deposit
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "payroll"
                and parts[3] == "remit" and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._payroll_remit(subject, t, req.body))
        # /t/<tenant>/payroll/<run> -> one run in detail
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "payroll"
                and req.method == "GET"):
            run_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._payroll_run_page(subject, t, run_id))
        # /t/<tenant>/payroll/<run>/post|void
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "payroll"
                and req.method == "POST" and parts[4] in ("post", "void")):
            run_id, action = parts[3], parts[4]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._payroll_run_action(subject, t, run_id, action))

        # --- the bank feed review inbox ---
        # /t/<tenant>/inbox -> the review queue (or an actioned list via ?status=)
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "inbox":
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._inbox_page(subject, t, req.query))
        # /t/<tenant>/inbox/rules -> the categorization rules
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "inbox"
                and parts[3] == "rules"):
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.CATEGORIZE_TXNS,
                                     lambda t: self._inbox_save_rule(subject, t, req.body))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._inbox_rules_page(subject, t, req.query))
        # /t/<tenant>/inbox/bulk-accept
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "inbox"
                and parts[3] == "bulk-accept" and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.CATEGORIZE_TXNS,
                                 lambda t: self._inbox_bulk_accept(subject, t, req.body))
        # /t/<tenant>/inbox/rules/<id>/delete
        if (len(parts) == 6 and parts[0] == "t" and parts[2] == "inbox"
                and parts[3] == "rules" and parts[5] == "delete" and req.method == "POST"):
            rule_id = parts[4]
            return self._require(subject, token_tenant, parts[1], P.CATEGORIZE_TXNS,
                                 lambda t: self._inbox_delete_rule(subject, t, rule_id))
        # /t/<tenant>/inbox/<txn>/accept|match|exclude|undo
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "inbox"
                and req.method == "POST"
                and parts[4] in ("accept", "match", "exclude", "undo")):
            txn_id, action = parts[3], parts[4]
            return self._require(subject, token_tenant, parts[1], P.CATEGORIZE_TXNS,
                                 lambda t: self._inbox_action(subject, t, txn_id, action,
                                                              req.body))

        # --- the books (general ledger) ---
        # /t/<tenant>/books -> trial balance + record a transaction
        if len(parts) == 3 and parts[0] == "t" and parts[2] == "books":
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._books_page(subject, t, req.query))
        # /t/<tenant>/books/accounts -> chart of accounts
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "books" and parts[3] == "accounts":
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._books_accounts_page(subject, t))
        # /t/<tenant>/books/statements -> P&L + balance sheet from posted books
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "books" and parts[3] == "statements":
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._books_statements_page(subject, t, req.query))
        # /t/<tenant>/books/entries -> post a journal entry (form POST)
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "entries" and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._books_post_entry(subject, t, req.body))
        # /t/<tenant>/books/accounts/<code> -> one account's register
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "accounts"):
            code = parts[4]
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._books_register_page(subject, t, code))

        # /t/<tenant>/books/1099 -> contractor payments for the year
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "1099" and req.method == "GET"):
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._ten99_page(subject, t, req.query))
        # /t/<tenant>/books/recurring -> what's due, what's memorized
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "recurring"):
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._recurring_save(subject, t, req.body))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._recurring_page(subject, t, req.query))
        # /t/<tenant>/books/recurring/run -> post what's due
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "recurring" and parts[4] == "run"
                and req.method == "POST"):
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._recurring_run(subject, t, req.body))
        # /t/<tenant>/books/recurring/<id>/delete
        if (len(parts) == 6 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "recurring" and parts[5] == "delete"
                and req.method == "POST"):
            template_id = parts[4]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._recurring_delete(subject, t, template_id))
        # /t/<tenant>/books/dimensions -> classes and locations
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "dimensions"):
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._dimension_save(subject, t, req.body))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._dimensions_page(subject, t, req.query))
        # /t/<tenant>/books/dimensions/<key>/delete
        if (len(parts) == 6 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "dimensions" and parts[5] == "delete"
                and req.method == "POST"):
            key = parts[4]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._dimension_delete(subject, t, key))
        # /t/<tenant>/books/dimensions/<key> -> the per-value report
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "dimensions" and req.method == "GET"):
            key = parts[4]
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._dimension_report(subject, t, key, req.query))
        # /t/<tenant>/books/gl -> general ledger detail across every account
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "gl"):
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._gl_page(subject, t, req.query))
        # /t/<tenant>/books/budget -> budget vs actual, and setting the budget
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "budget"):
            if req.method == "POST":
                return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                     lambda t: self._budget_save(subject, t, req.body))
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._budget_page(subject, t, req.query))
        # /t/<tenant>/books/reconcile -> pick a bank account to reconcile
        if (len(parts) == 4 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "reconcile"):
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._reconcile_pick(subject, t))
        # /t/<tenant>/books/reconcile/<code> -> the reconciliation worksheet
        if (len(parts) == 5 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "reconcile"):
            code = parts[4]
            return self._require(subject, token_tenant, parts[1], P.VIEW_TRANSACTIONS,
                                 lambda t: self._reconcile_page(subject, t, code, req.query))
        # /t/<tenant>/books/reconcile/<code>/import -> upload the bank's export
        if (len(parts) == 6 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "reconcile" and parts[5] == "import"
                and req.method == "POST"):
            code = parts[4]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._reconcile_import(subject, t, code, req))
        # /t/<tenant>/books/reconcile/<code>/toggle|finish -> tick a line, or lock it in
        if (len(parts) == 6 and parts[0] == "t" and parts[2] == "books"
                and parts[3] == "reconcile" and req.method == "POST"
                and parts[5] in ("toggle", "finish")):
            code, action = parts[4], parts[5]
            return self._require(subject, token_tenant, parts[1], P.POST_JOURNAL,
                                 lambda t: self._reconcile_action(subject, t, code, action,
                                                                  req.body))

        # /t/<tenant>  -> Cash outlook (shell page)
        if len(parts) == 2 and parts[0] == "t":
            return self._require(subject, token_tenant, parts[1], P.VIEW_CASH,
                                 lambda t: self._cash_page(subject, t))

        # /t/<tenant>/packages/<period>  -> published package HTML
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "packages":
            period = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_PACKAGE,
                                 lambda t: self._package_html(t, period))

        # /t/<tenant>/reports/<report_id>  -> render a baseline or saved report in-shell
        if len(parts) == 4 and parts[0] == "t" and parts[2] == "reports":
            report_id = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_CASH,
                                 lambda t: self._report_page(subject, t, report_id))

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
            if resource == "scenario" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.VIEW_CASH,
                                     lambda t: self._scenario(t, req.body))
            if resource == "receivables" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.VIEW_CASH,
                                     self._receivables_json)
            if resource == "close" and req.method == "GET":
                return self._require(subject, token_tenant, tenant, P.MANAGE_CLOSE,
                                     self._close_json)
            if resource == "close" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.MANAGE_CLOSE,
                                     lambda t: self._close_advance(t, req.body))
            # build + save a custom report definition (gated on RECORD_DECISION —
            # everyone but a read-only viewer can author a report).
            if resource == "reports" and req.method == "POST":
                return self._require(subject, token_tenant, tenant, P.RECORD_DECISION,
                                     lambda t: self._reports_save(t, req.body))
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

        # /api/<tenant>/reports/<report_id>.json|.csv  -> export a rendered report
        if len(parts) == 4 and parts[0] == "api" and parts[2] == "reports" and req.method == "GET":
            filename = parts[3]
            return self._require(subject, token_tenant, parts[1], P.VIEW_CASH,
                                 lambda t: self._report_export(t, filename))

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
        # When RGNR8 staff are viewing-as a client role, authorization is
        # evaluated as THAT role (so the experience matches what the role sees).
        if self._policy is not None and not self._policy.can(
            subject, wanted, permission, view_as=_VIEW_AS.get()
        ):
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
        view_as = _VIEW_AS.get()
        perms = sorted(p.value for p in self._policy.permissions(subject, tenant, view_as=view_as))
        role = self._policy.role_in(subject, tenant, view_as=view_as)
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
            # surfaced so the UI can show a "You are viewing as <role>" banner
            "viewing_as": view_as.value if view_as is not None else None,
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
            va = _VIEW_AS.get()
            return (
                self._policy.permissions(subject, tenant, view_as=va),
                self._policy.role_in(subject, tenant, view_as=va),
            )
        # no RBAC directory → tenant-scoped access = full owner-equivalent view
        return frozenset(Permission), None



    # --- invoicing (AR) and bills (AP) ---------------------------------------

    def _today(self, t: _Tenant) -> str:
        """The business's working date. Derived from its forecast as-of date so
        the whole app agrees on 'today' under test and in dev."""
        return t.inputs.opening.as_of.isoformat()

    def _arap_page(self, subject: str, t: _Tenant, kind: str, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, kind, render_books_unavailable(
                "No ledger service is configured for this deployment."))
        docs = self._ledger.documents(t.tenant_id, kind)
        if not docs.ok:
            return self._shell(subject, t, kind, render_books_unavailable(docs.error()))
        party_kind = "customers" if kind == "invoices" else "vendors"
        parties = self._ledger.parties(t.tenant_id, party_kind)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        counts = self._ledger.attachment_counts(
            t.tenant_id, "invoice" if kind == "invoices" else "bill",
        )
        counts = self._ledger.attachment_counts(
            t.tenant_id, "invoice" if kind == "invoices" else "bill",
        )
        raw_counts = counts.body.get("counts") if counts.ok else None
        body = render_documents(
            t.tenant_id, kind, docs.body, parties.body if parties.ok else {},
            today=self._today(t), can_post=can_post,
            attachment_counts=raw_counts if isinstance(raw_counts, dict) else None,
            message=query.get("ok", ""), error=query.get("err", ""),
        )
        return self._shell(subject, t, kind, body)

    def _arap_create(self, t: _Tenant, kind: str, body: str) -> Response:
        """Create an invoice or a bill from the owner form."""
        if self._ledger is None:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape('No ledger service configured')}")
        data = self._form_or_json(body)
        doc_id = str(data.get("id", "")).strip()
        party_id = str(data.get("party_id", "")).strip()
        date = str(data.get("date", "")).strip()
        if not doc_id or not party_id or not date:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err="
                             f"{_qs_escape('Number, party and date are all required')}")
        default_code = "4100" if kind == "invoices" else "6400"
        lines: list[dict[str, object]] = []
        try:
            for i in range(1, 4):
                raw = str(data.get(f"amount{i}", "")).strip()
                if not raw:
                    continue
                minor = self._amount_to_minor(raw)
                if minor is None or minor <= 0:
                    raise ValueError(f"line {i}: amount must be positive")
                lines.append({
                    "description": str(data.get(f"desc{i}", "")).strip(),
                    "quantity": 1,
                    "unit_amount_minor": str(minor),
                    "account_code": str(data.get(f"code{i}", "")).strip() or default_code,
                })
        except ValueError as exc:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape(str(exc))}")
        if not lines:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape('Add at least one line')}")

        # A tax rate is typed as a percentage and stored as parts per million,
        # so 8.25% is 82500 exactly — never 0.0825 through a float.
        try:
            tax_ppm = _percent_to_ppm(str(data.get("tax_rate", "")))
        except ValueError as exc:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape(str(exc))}")

        res = self._ledger.create_document(
            t.tenant_id, kind, doc_id, party_id, date, lines,
            memo=str(data.get("memo", "")).strip(),
            due_date=str(data.get("due_date", "")).strip(),
            tax_rate_ppm=tax_ppm,
        )
        if not res.ok:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape(res.error())}")
        noun = "Invoice" if kind == "invoices" else "Bill"
        return _redirect(f"/t/{t.tenant_id}/{kind}?ok={_qs_escape(f'{noun} {doc_id} created')}")

    def _arap_adjust(
        self, subject: str, t: _Tenant, kind: str, doc_id: str, action: str, body: str
    ) -> Response:
        """Credit an open document, or refund one that was already collected.

        The ledger owns every rule about which is allowed when — crediting a
        settled invoice and refunding an unpaid one are both refused there, with
        a message that says which one you actually wanted."""
        back = f"/t/{t.tenant_id}/{kind}"
        if self._ledger is None:
            return _redirect(f"{back}?err={_qs_escape('No ledger service configured')}")
        data = self._form_or_json(body)
        try:
            minor = self._amount_to_minor(str(data.get("amount", "")))
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        if minor is not None and minor <= 0:
            return _redirect(f"{back}?err={_qs_escape('Enter a positive amount')}")
        date = str(data.get("date", "")).strip() or self._today(t)
        memo = str(data.get("memo", "")).strip()

        if action == "credits":
            res = self._ledger.issue_credit(
                t.tenant_id, kind, doc_id, date,
                amount_minor=str(minor) if minor else "", memo=memo,
            )
        else:
            res = self._ledger.issue_refund(
                t.tenant_id, doc_id, date,
                amount_minor=str(minor) if minor else "", memo=memo,
            )
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        applied = _minor_decimal(res.body.get("applied_minor", "0"))
        if self._audit is not None:
            self._audit.record(subject, f"arap.{action[:-1]}", self._session_clock(),
                               tenant_id=t.tenant_id, target=doc_id, detail=applied)
        word = "Credited" if action == "credits" else "Refunded"
        return _redirect(f"{back}?ok={_qs_escape(f'{word} {applied} against {doc_id}')}")

    def _arap_pay(self, subject: str, t: _Tenant, kind: str, doc_id: str, body: str) -> Response:
        """Collect against an invoice, or pay a bill."""
        if self._ledger is None:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape('No ledger service configured')}")
        data = self._form_or_json(body)
        try:
            minor = self._amount_to_minor(str(data.get("amount", "")))
        except ValueError as exc:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape(str(exc))}")
        if minor is None or minor <= 0:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape('Enter an amount to pay')}")
        date = str(data.get("date", "")).strip() or self._today(t)
        res = self._ledger.record_payment(t.tenant_id, kind, doc_id, date, str(minor))
        if not res.ok:
            return _redirect(f"/t/{t.tenant_id}/{kind}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            action = "invoice.collected" if kind == "invoices" else "bill.paid"
            self._audit.record(subject, action, self._session_clock(),
                               tenant_id=t.tenant_id, target=doc_id)
        verb = "collected on" if kind == "invoices" else "paid on"
        return _redirect(f"/t/{t.tenant_id}/{kind}?ok={_qs_escape(f'Payment {verb} {doc_id}')}")

    def _arap_party(self, t: _Tenant, kind: str, body: str) -> Response:
        """Add a customer or vendor from the inline form."""
        if self._ledger is None:
            back = "invoices" if kind == "customers" else "bills"
            return _redirect(f"/t/{t.tenant_id}/{back}?err={_qs_escape('No ledger service configured')}")
        data = self._form_or_json(body)
        name = str(data.get("name", "")).strip()
        back = "invoices" if kind == "customers" else "bills"
        if not name:
            return _redirect(f"/t/{t.tenant_id}/{back}?err={_qs_escape('A name is required')}")
        party_id = str(data.get("id", "")).strip() or name.lower().replace(" ", "-")[:40]
        terms_raw = str(data.get("terms_days", "")).strip()
        terms = int(terms_raw) if terms_raw.isdigit() else None
        res = self._ledger.create_party(
            t.tenant_id, kind, party_id, name,
            email=str(data.get("email", "")).strip(), terms_days=terms,
            # Only vendors can be contractors; a customer flagged 1099 is a
            # form filled in wrong, not a fact worth storing.
            is_1099=kind == "vendors"
            and str(data.get("is_1099", "")).strip() in ("1", "true", "on"),
            tax_id=str(data.get("tax_id", "")).strip() if kind == "vendors" else "",
        )
        if not res.ok:
            return _redirect(f"/t/{t.tenant_id}/{back}?err={_qs_escape(res.error())}")
        return _redirect(f"/t/{t.tenant_id}/{back}?ok={_qs_escape(f'Added {name}')}")

    def _arap_aging(self, subject: str, t: _Tenant, side: str, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "invoices", render_books_unavailable())
        as_of = query.get("as_of", "") or self._today(t)
        res = self._ledger.aging(t.tenant_id, side, as_of=as_of)
        if not res.ok:
            return self._shell(subject, t, "invoices", render_books_unavailable(res.error()))
        party_kind = "customers" if side == "ar" else "vendors"
        parties = self._ledger.parties(t.tenant_id, party_kind)
        active = "invoices" if side == "ar" else "bills"
        return self._shell(subject, t, active,
                           render_arap_aging(t.tenant_id, side, res.body,
                                             parties.body if parties.ok else {}))

    # --- the books (general ledger, served by the ledger service) ------------

    @staticmethod
    def _amount_to_minor(raw: str) -> int | None:
        """Parse "1,234.56" into 123456 minor units — exactly, never via float.
        Returns None for blank input, and raises ValueError on a malformed one."""
        text = raw.strip().replace(",", "").replace("$", "")
        if not text:
            return None
        neg = text.startswith("-")
        if neg:
            text = text[1:]
        if not text or not all(c.isdigit() or c == "." for c in text) or text.count(".") > 1:
            raise ValueError(f"not a valid amount: {raw!r}")
        whole, _, frac = text.partition(".")
        if len(frac) > 2:
            raise ValueError(f"more than cents of precision: {raw!r}")
        minor = int(whole or "0") * 100 + int((frac or "0").ljust(2, "0"))
        return -minor if neg else minor

    def _books_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable(
                "No ledger service is configured for this deployment."))
        tb = self._ledger.trial_balance(t.tenant_id)
        if not tb.ok:
            return self._shell(subject, t, "books", render_books_unavailable(tb.error()))
        accounts = self._ledger.accounts(t.tenant_id)
        dims = self._ledger.dimensions(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        body = render_books_home(
            t.tenant_id, tb.body, accounts.body if accounts.ok else {},
            dimensions=dims.body if dims.ok else {},
            can_post=can_post,
            message=query.get("posted", ""),
            error=query.get("err", ""),
        )
        return self._shell(subject, t, "books", body)

    def _books_accounts_page(self, subject: str, t: _Tenant) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable())
        res = self._ledger.accounts(t.tenant_id)
        if not res.ok:
            return self._shell(subject, t, "books", render_books_unavailable(res.error()))
        return self._shell(subject, t, "books", render_chart_of_accounts(t.tenant_id, res.body))

    def _books_register_page(self, subject: str, t: _Tenant, code: str) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable())
        res = self._ledger.register(t.tenant_id, code)
        if not res.ok:
            return self._shell(subject, t, "books", render_books_unavailable(res.error()))
        return self._shell(subject, t, "books", render_register(t.tenant_id, res.body))

    # --- attachments -----------------------------------------------------------

    def _attachments_page(
        self, subject: str, t: _Tenant, kind: str, subject_id: str,
        query: "dict[str, str]",
    ) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_attachments_unavailable())
        res = self._ledger.attachments(
            t.tenant_id, subject_kind=kind, subject_id=subject_id,
        )
        if not res.ok:
            return self._shell(subject, t, "books",
                               render_attachments_unavailable(res.error()))
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "books", render_attachments(
            t.tenant_id, kind, subject_id, res.body, can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _attachment_upload(
        self, subject: str, t: _Tenant, kind: str, subject_id: str, req: Request
    ) -> Response:
        back = f"/t/{t.tenant_id}/files/{kind}/{subject_id}"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        try:
            fields, files = parse_multipart(req.body, req.headers.get("content-type", ""))
        except MultipartError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        if not files:
            return _redirect(f"{back}?err={_qs_escape('Choose a file to attach')}")

        note = fields.get("note", "").strip()
        saved = 0
        for f in files:
            res = self._ledger.save_attachment(
                t.tenant_id, kind, subject_id, f.filename, f.content_type, f.content,
                note=note,
            )
            if not res.ok:
                return _redirect(f"{back}?err={_qs_escape(res.error())}")
            saved += 1
        if self._audit is not None:
            self._audit.record(subject, "attachment.added", self._session_clock(),
                               tenant_id=t.tenant_id, target=f"{kind}:{subject_id}")
        noun = "file" if saved == 1 else "files"
        return _redirect(f"{back}?done={_qs_escape(f'Attached {saved} {noun}')}")

    def _attachment_download(self, t: _Tenant, attachment_id: str) -> Response:
        """Serve the file back with its own name and type.

        Content-Disposition is *attachment*, never inline: a receipt is an
        arbitrary uploaded file, and rendering one in the page's own origin is
        how an upload becomes a script that runs as the owner."""
        if self._ledger is None:
            return _json(503, {"error": "no ledger service configured"})
        res = self._ledger.attachment_content(t.tenant_id, attachment_id)
        if not res.ok:
            return _json(404, {"error": res.error()})
        import base64

        try:
            content = base64.b64decode(str(res.body.get("content_base64", "")))
        except (ValueError, TypeError):
            return _json(500, {"error": "the stored file could not be decoded"})
        filename = str(res.body.get("filename", "attachment")).replace('"', "")
        return Response(
            200, content,
            content_type=str(res.body.get("content_type", "application/octet-stream")),
            extra_headers=(
                ("Content-Disposition", f'attachment; filename="{filename}"'),
                ("X-Content-Type-Options", "nosniff"),
            ),
        )

    def _attachment_delete(
        self, subject: str, t: _Tenant, kind: str, subject_id: str, attachment_id: str
    ) -> Response:
        back = f"/t/{t.tenant_id}/files/{kind}/{subject_id}"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        res = self._ledger.delete_attachment(t.tenant_id, attachment_id)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "attachment.removed", self._session_clock(),
                               tenant_id=t.tenant_id, target=attachment_id)
        return _redirect(f"{back}?done={_qs_escape('Removed')}")

    # --- payroll ---------------------------------------------------------------

    def _payroll_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "payroll", render_payroll_unavailable(
                "No ledger service is configured for this deployment."))
        runs = self._ledger.payroll_runs(t.tenant_id)
        if not runs.ok:
            return self._shell(subject, t, "payroll", render_payroll_unavailable(runs.error()))
        liabilities = self._ledger.payroll_liabilities(t.tenant_id)
        employees = self._ledger.payroll_employees(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        body = render_payroll_home(
            t.tenant_id, runs.body,
            liabilities.body if liabilities.ok else {},
            employees.body if employees.ok else {},
            can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        )
        return self._shell(subject, t, "payroll", body)

    def _payroll_run_page(self, subject: str, t: _Tenant, run_id: str) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "payroll", render_payroll_unavailable())
        res = self._ledger.payroll_run(t.tenant_id, run_id)
        if not res.ok:
            return self._shell(subject, t, "payroll", render_payroll_unavailable(res.error()))
        employees = self._ledger.payroll_employees(t.tenant_id)
        run = res.body.get("run")
        return self._shell(subject, t, "payroll", render_payroll_run(
            t.tenant_id, run if isinstance(run, Mapping) else {},
            employees.body if employees.ok else {},
        ))

    def _payroll_create(self, subject: str, t: _Tenant, body: str) -> Response:
        """Draft a run from the owner form. Amounts are parsed exactly; an
        employee with no gross pay is simply not in this run."""
        back = f"/t/{t.tenant_id}/payroll"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        lines: list[dict[str, object]] = []
        try:
            for i in range(1, 41):
                employee = str(data.get(f"employee{i}", "")).strip()
                if not employee:
                    continue
                gross = self._amount_to_minor(str(data.get(f"gross{i}", "")))
                if not gross:
                    continue   # nothing to pay them this run
                lines.append({
                    "employee_id": employee,
                    "gross_minor": str(gross),
                    "employee_taxes_minor": str(
                        self._amount_to_minor(str(data.get(f"taxes{i}", ""))) or 0),
                    "deductions_minor": str(
                        self._amount_to_minor(str(data.get(f"deductions{i}", ""))) or 0),
                })
            employer = self._amount_to_minor(str(data.get("employer_taxes", ""))) or 0
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        if not lines:
            return _redirect(
                f"{back}?err={_qs_escape('Enter gross pay for at least one employee')}")

        run: dict[str, object] = {
            "id": str(data.get("id", "")).strip(),
            "date": str(data.get("date", "")).strip(),
            "memo": str(data.get("memo", "")).strip(),
            "employer_taxes_minor": str(employer),
            "bank_code": str(data.get("bank_code", "")).strip() or "1000",
            "lines": lines,
        }
        res = self._ledger.create_payroll_run(t.tenant_id, run)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        drafted = res.body.get("run")
        run_id = drafted.get("id") if isinstance(drafted, Mapping) else ""
        if self._audit is not None:
            self._audit.record(subject, "payroll.drafted", self._session_clock(),
                               tenant_id=t.tenant_id, target=str(run_id))
        return _redirect(
            f"{back}?done={_qs_escape('Draft created — review it, then post it')}")

    def _payroll_run_action(
        self, subject: str, t: _Tenant, run_id: str, action: str
    ) -> Response:
        back = f"/t/{t.tenant_id}/payroll"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        res = (self._ledger.post_payroll_run(t.tenant_id, run_id) if action == "post"
               else self._ledger.void_payroll_run(t.tenant_id, run_id))
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, f"payroll.{action}", self._session_clock(),
                               tenant_id=t.tenant_id, target=run_id)
        done = ("Payroll posted — gross wages, your taxes, and the liability"
                if action == "post"
                else "Voided — a reversing entry was posted; the original stays in the journal")
        return _redirect(f"{back}?done={_qs_escape(done)}")

    def _payroll_employee(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/payroll"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        res = self._ledger.save_employee(t.tenant_id, {
            "id": str(data.get("id", "")).strip(),
            "name": str(data.get("name", "")).strip(),
        })
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        return _redirect(f"{back}?done={_qs_escape('Employee added')}")

    def _payroll_remit(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/payroll"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        try:
            amount = self._amount_to_minor(str(data.get("amount", "")))
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        if not amount:
            return _redirect(f"{back}?err={_qs_escape('Enter how much you are depositing')}")
        res = self._ledger.payroll_remit(t.tenant_id, {
            "date": str(data.get("date", "")).strip(),
            "amount_minor": str(amount),
            "bank_code": str(data.get("bank_code", "")).strip() or "1000",
            "memo": str(data.get("memo", "")).strip(),
        })
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        remaining = res.body.get("remaining_minor", "0")
        if self._audit is not None:
            self._audit.record(subject, "payroll.remitted", self._session_clock(),
                               tenant_id=t.tenant_id, detail=str(amount))
        left = _minor_decimal(remaining)
        return _redirect(
            f"{back}?done={_qs_escape(f'Deposit recorded — {left} still owed')}")

    # --- the bank feed review inbox -------------------------------------------

    def _inbox_json(self, t: _Tenant) -> Response:
        """The real review queue as JSON, for the MCP tool and integrations."""
        assert self._ledger is not None
        res = self._ledger.feed_inbox(t.tenant_id)
        if not res.ok:
            return _json(502, {"error": res.error()})
        rows = [r for r in _json_seq(res.body.get("items")) if isinstance(r, dict)]
        return _json(200, {
            "tenant": t.tenant_id,
            "account": self._accounts.get(t.tenant_id, "Checking"),
            "summary": {
                "total": len(rows),
                "matched": res.body.get("matched", 0),
                "review": res.body.get("pending", 0),
                "unmatched": 0,
                "inflow": _minor_decimal(res.body.get("pending_in_minor")),
                "outflow": _minor_decimal(res.body.get("pending_out_minor")),
            },
            "for_review": [
                {
                    "id": r.get("id"), "date": r.get("date"),
                    "description": r.get("description"),
                    "amount": _minor_decimal(r.get("amount_minor")),
                    "suggested_account": _suggested_code(r.get("suggestion")),
                    "status": "review",
                    "counterparty": r.get("counterparty", ""),
                }
                for r in rows
            ],
        })

    def _inbox_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        """The review queue, or one of the actioned lists when ?status= is given."""
        if self._ledger is None:
            return self._shell(subject, t, "inbox", render_feed_unavailable(
                "No ledger service is configured for this deployment."))
        status = query.get("status", "").strip().upper()
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.CATEGORIZE_TXNS in perms

        res = self._ledger.feed_inbox(t.tenant_id, status=status)
        if not res.ok:
            return self._shell(subject, t, "inbox", render_feed_unavailable(res.error()))
        if status in ("POSTED", "MATCHED", "EXCLUDED"):
            return self._shell(subject, t, "inbox",
                               render_actioned(t.tenant_id, status, res.body, can_post=can_post))

        accounts = self._ledger.accounts(t.tenant_id)
        dims = self._ledger.dimensions(t.tenant_id)
        body = render_inbox(
            t.tenant_id, res.body, accounts.body if accounts.ok else {},
            dimensions=dims.body if dims.ok else {},
            can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        )
        return self._shell(subject, t, "inbox", body)

    def _inbox_action(
        self, subject: str, t: _Tenant, txn_id: str, action: str, body: str
    ) -> Response:
        """Accept, match, exclude or undo one bank line. The ledger service owns
        every rule about what is allowed; this only shapes the request."""
        back = f"/t/{t.tenant_id}/inbox"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)

        payload: dict[str, object] = {}
        if action == "accept":
            code = str(data.get("category_code", "")).strip()
            if not code:
                return _redirect(
                    f"{back}?err={_qs_escape('Choose an account before accepting')}")
            payload["category_code"] = code
            dims = self._dimensions_of(data)
            if dims:
                payload["dimensions"] = dims
        elif action == "match":
            raw = str(data.get("match", "")).strip()
            kind, _, doc_id = raw.partition(":")
            if not kind or not doc_id:
                return _redirect(f"{back}?err={_qs_escape('Choose what to match it to')}")
            payload = {"doc_kind": kind, "doc_id": doc_id}
        elif action == "exclude":
            payload["reason"] = str(data.get("reason", "")).strip()

        res = self._ledger.feed_action(t.tenant_id, txn_id, action, payload)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, f"feed.{action}", self._session_clock(),
                               tenant_id=t.tenant_id, target=txn_id)
        done = {
            "accept": "Posted to the books",
            "match": "Matched — the open item is settled",
            "exclude": "Excluded — nothing was posted",
            "undo": "Undone — a reversing entry was posted and the line is back in review",
        }.get(action, "Done")
        return _redirect(f"{back}?done={_qs_escape(done)}")

    def _inbox_bulk_accept(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/inbox"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        try:
            minimum = float(str(data.get("min_confidence", "")).strip())
        except ValueError:
            return _redirect(f"{back}?err={_qs_escape('Choose a confidence level')}")
        res = self._ledger.feed_bulk_accept(t.tenant_id, minimum)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        accepted = res.body.get("accepted", 0)
        skipped = res.body.get("skipped", 0)
        if self._audit is not None:
            self._audit.record(subject, "feed.bulk_accept", self._session_clock(),
                               tenant_id=t.tenant_id, detail=f"{accepted} accepted")
        return _redirect(
            f"{back}?done={_qs_escape(f'Posted {accepted}; left {skipped} for you to look at')}")

    def _inbox_rules_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "inbox", render_feed_unavailable())
        res = self._ledger.feed_rules(t.tenant_id)
        if not res.ok:
            return self._shell(subject, t, "inbox", render_feed_unavailable(res.error()))
        accounts = self._ledger.accounts(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.CATEGORIZE_TXNS in perms
        body = render_rules(
            t.tenant_id, res.body, accounts.body if accounts.ok else {},
            can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        )
        return self._shell(subject, t, "inbox", body)

    def _inbox_save_rule(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/inbox/rules"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        rule: dict[str, object] = {
            "id": str(data.get("id", "")).strip(),
            "account_code": str(data.get("account_code", "")).strip(),
            "description_contains": str(data.get("description_contains", "")).strip(),
            "counterparty_equals": str(data.get("counterparty_equals", "")).strip(),
            "sign": str(data.get("sign", "")).strip(),
            "auto_post": str(data.get("auto_post", "")).strip() in ("1", "true", "on"),
        }
        res = self._ledger.save_feed_rule(t.tenant_id, rule)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        would = res.body.get("would_match", 0)
        if self._audit is not None:
            self._audit.record(subject, "feed.rule_saved", self._session_clock(),
                               tenant_id=t.tenant_id, target=str(rule["id"]))
        return _redirect(
            f"{back}?done={_qs_escape(f'Rule saved — it matches {would} waiting line(s)')}")

    def _inbox_delete_rule(self, subject: str, t: _Tenant, rule_id: str) -> Response:
        back = f"/t/{t.tenant_id}/inbox/rules"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        res = self._ledger.delete_feed_rule(t.tenant_id, rule_id)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "feed.rule_deleted", self._session_clock(),
                               tenant_id=t.tenant_id, target=rule_id)
        return _redirect(f"{back}?done={_qs_escape('Rule removed')}")

    # --- 1099 contractors --------------------------------------------------------

    def _ten99_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "bills", render_ten99_unavailable())
        # Default to LAST year: 1099s are filed in January for the year that just
        # ended, so "this year" is almost never the one being asked about.
        today = self._today(t)
        year = query.get("year", "").strip() or str(int(today[:4]) - 1)
        res = self._ledger.ten99(t.tenant_id, year)
        if not res.ok:
            return self._shell(subject, t, "bills", render_ten99_unavailable(res.error()))
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "bills", render_ten99(
            t.tenant_id, year, res.body, can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    # --- recurring transactions --------------------------------------------------

    # --- jobs -----------------------------------------------------------------

    # --- estimates and the pipeline -------------------------------------------

    def _estimates_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "estimates", render_estimates_unavailable())
        estimates = self._ledger.estimates(t.tenant_id)
        if not estimates.ok:
            return self._shell(subject, t, "estimates",
                               render_estimates_unavailable(estimates.error()))
        customers = self._ledger.parties(t.tenant_id, "customers")
        cost_codes = self._ledger.cost_codes(t.tenant_id)
        accounts = self._ledger.accounts(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_edit = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "estimates", render_estimates(
            t.tenant_id, estimates.body,
            customers.body if customers.ok else {},
            cost_codes.body if cost_codes.ok else {},
            accounts.body if accounts.ok else {},
            can_edit=can_edit,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _estimate_page(
        self, subject: str, t: _Tenant, estimate_id: str, query: "dict[str, str]",
    ) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "estimates", render_estimates_unavailable())
        res = self._ledger.estimate(t.tenant_id, estimate_id)
        if not res.ok:
            return self._shell(subject, t, "estimates",
                               render_estimates_unavailable(res.error()))
        estimate = res.body.get("estimate")
        if not isinstance(estimate, dict):
            return self._shell(subject, t, "estimates",
                               render_estimates_unavailable("that estimate came back empty"))
        jobs = self._ledger.jobs(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_edit = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "estimates", render_estimate(
            t.tenant_id, estimate, jobs.body if jobs.ok else {},
            can_edit=can_edit,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _estimate_lines(self, data: "dict[str, object]") -> list[dict[str, object]]:
        """Read the builder's rows. A blank row is a blank row, not a zero line."""
        lines: list[dict[str, object]] = []
        for i in range(1, 6):
            cost = self._amount_to_minor(str(data.get(f"cost{i}", "")))
            price = self._amount_to_minor(str(data.get(f"price{i}", "")))
            description = str(data.get(f"desc{i}", "")).strip()
            if cost is None and price is None:
                continue
            quantity = _quantity_to_milli(str(data.get(f"qty{i}", "")))
            line: dict[str, object] = {
                "description": description,
                "quantity_milli": str(quantity),
                "unit_cost_minor": str(cost or 0),
            }
            code = str(data.get(f"code{i}", "")).strip()
            if code:
                line["cost_code"] = code
            account = str(data.get(f"account{i}", "")).strip()
            if account:
                line["account_code"] = account
            if price is not None:
                line["unit_price_minor"] = str(price)
            else:
                line["markup_ppm"] = _percent_to_ppm(str(data.get(f"markup{i}", "")))
            lines.append(line)
        return lines

    def _estimate_save(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/estimates"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        try:
            lines = self._estimate_lines(data)
            tax = _percent_to_ppm(str(data.get("tax_rate", "")))
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        if not lines:
            return _redirect(f"{back}?err={_qs_escape('Add at least one line')}")
        payload: dict[str, object] = {
            "customer_id": str(data.get("customer_id", "")).strip(),
            "date": str(data.get("date", "")).strip(),
            "expiry_date": str(data.get("expiry_date", "")).strip(),
            "memo": str(data.get("memo", "")).strip(),
            "tax_rate_ppm": tax,
            "lines": lines,
        }
        if str(data.get("id", "")).strip():
            payload["id"] = str(data.get("id", "")).strip()
        res = self._ledger.save_estimate(t.tenant_id, payload)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        estimate = res.body.get("estimate")
        estimate_id = str(estimate.get("id")) if isinstance(estimate, dict) else ""
        if self._audit is not None:
            self._audit.record(subject, "estimate.saved", self._session_clock(),
                               tenant_id=t.tenant_id, target=estimate_id)
        return _redirect(f"{back}/{estimate_id}?done={_qs_escape('Estimate saved')}")

    def _estimate_action(
        self, subject: str, t: _Tenant, estimate_id: str, action: str, body: str,
    ) -> Response:
        back = f"/t/{t.tenant_id}/estimates/{estimate_id}"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        if action == "status":
            res = self._ledger.set_estimate_status(
                t.tenant_id, estimate_id, str(data.get("status", "")).strip(),
            )
            note = "Updated"
        elif action == "accept":
            try:
                retainage = _percent_to_ppm(str(data.get("retainage", "")))
            except ValueError as exc:
                return _redirect(f"{back}?err={_qs_escape(str(exc))}")
            request: dict[str, object] = {
                "job_id": str(data.get("job_id", "")).strip(),
                "job_name": str(data.get("job_name", "")).strip(),
                "billing_method": str(data.get("billing_method", "PROGRESS")).strip(),
                "start_date": str(data.get("start_date", "")).strip(),
                "retainage_ppm": retainage,
            }
            res = self._ledger.accept_estimate(t.tenant_id, estimate_id, request)
            note = "Accepted — the job's budget came from the estimate"
        elif action == "revise":
            res = self._ledger.revise_estimate(t.tenant_id, estimate_id, {
                "memo": str(data.get("memo", "")).strip(),
            })
            note = "Revision started; the old one is superseded, not gone"
        elif action == "invoice":
            res = self._ledger.invoice_estimate(t.tenant_id, estimate_id, {
                "id": str(data.get("id", "")).strip(),
                "date": str(data.get("date", "")).strip() or self._today(t),
            })
            note = "Invoiced"
        else:
            return _redirect(f"{back}?err={_qs_escape('Unknown action')}")

        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, f"estimate.{action}", self._session_clock(),
                               tenant_id=t.tenant_id, target=estimate_id)
        if action == "revise":
            revised = res.body.get("estimate")
            if isinstance(revised, dict):
                return _redirect(
                    f"/t/{t.tenant_id}/estimates/{revised.get('id')}?done={_qs_escape(note)}"
                )
        return _redirect(f"{back}?done={_qs_escape(note)}")

    def _pipeline_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "pipeline", render_pipeline_unavailable())
        pipeline = self._ledger.pipeline(t.tenant_id)
        if not pipeline.ok:
            return self._shell(subject, t, "pipeline",
                               render_pipeline_unavailable(pipeline.error()))
        leads = self._ledger.leads(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_edit = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "pipeline", render_pipeline(
            t.tenant_id, pipeline.body, leads.body if leads.ok else {},
            can_edit=can_edit,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _lead_save(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/pipeline"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        res = self._ledger.save_lead(t.tenant_id, {
            "name": str(data.get("name", "")).strip(),
            "company": str(data.get("company", "")).strip(),
            "email": str(data.get("email", "")).strip(),
            "phone": str(data.get("phone", "")).strip(),
            "source": str(data.get("source", "")).strip(),
            "owner": str(data.get("owner", "")).strip(),
            "created_date": self._today(t),
        })
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "lead.saved", self._session_clock(),
                               tenant_id=t.tenant_id, target=str(data.get("name", "")))
        return _redirect(f"{back}?done={_qs_escape('Lead added')}")

    def _lead_convert(self, subject: str, t: _Tenant, lead_id: str, body: str) -> Response:
        back = f"/t/{t.tenant_id}/pipeline"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        request: dict[str, object] = {}
        if str(data.get("customer_id", "")).strip():
            request["customer_id"] = str(data.get("customer_id", "")).strip()
        res = self._ledger.convert_lead(t.tenant_id, lead_id, request)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "lead.converted", self._session_clock(),
                               tenant_id=t.tenant_id, target=lead_id)
        return _redirect(f"{back}?done={_qs_escape('They are a customer now')}")

    def _opportunity_save(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/pipeline"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        try:
            value = self._amount_to_minor(str(data.get("value", "")))
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        payload: dict[str, object] = {
            "name": str(data.get("name", "")).strip(),
            "stage": str(data.get("stage", "NEW")).strip(),
            "value_minor": str(value or 0),
            "expected_close_date": str(data.get("expected_close_date", "")).strip(),
            "owner": str(data.get("owner", "")).strip(),
        }
        for key in ("lead_id", "customer_id"):
            if str(data.get(key, "")).strip():
                payload[key] = str(data.get(key, "")).strip()
        res = self._ledger.save_opportunity(t.tenant_id, payload)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "opportunity.saved", self._session_clock(),
                               tenant_id=t.tenant_id, target=str(data.get("name", "")))
        return _redirect(f"{back}?done={_qs_escape('Opportunity opened')}")

    def _opportunity_close(
        self, subject: str, t: _Tenant, opportunity_id: str, outcome: str, body: str,
    ) -> Response:
        back = f"/t/{t.tenant_id}/pipeline"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        request: dict[str, object] = {}
        if outcome == "lose":
            request["reason"] = str(data.get("reason", "")).strip()
        if str(data.get("job_id", "")).strip():
            request["job_id"] = str(data.get("job_id", "")).strip()
        res = self._ledger.close_opportunity(t.tenant_id, opportunity_id, outcome, request)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, f"opportunity.{outcome}", self._session_clock(),
                               tenant_id=t.tenant_id, target=opportunity_id)
        note = "Won — nothing is booked until the job is billed" if outcome == "win" else "Closed"
        return _redirect(f"{back}?done={_qs_escape(note)}")

    def _jobs_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "jobs", render_jobs_unavailable())
        jobs = self._ledger.jobs(t.tenant_id)
        if not jobs.ok:
            return self._shell(subject, t, "jobs", render_jobs_unavailable(jobs.error()))
        customers = self._ledger.parties(t.tenant_id, "customers")
        accounts = self._ledger.accounts(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_edit = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "jobs", render_jobs(
            t.tenant_id, jobs.body,
            customers.body if customers.ok else {},
            accounts.body if accounts.ok else {},
            can_edit=can_edit,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _job_page(
        self, subject: str, t: _Tenant, job_id: str, query: "dict[str, str]",
    ) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "jobs", render_jobs_unavailable())
        detail = self._ledger.job(t.tenant_id, job_id)
        if not detail.ok:
            return self._shell(subject, t, "jobs", render_jobs_unavailable(detail.error()))
        cost = self._ledger.job_cost(t.tenant_id, job_id)
        work_orders = self._ledger.job_work_orders(t.tenant_id, job_id)
        billing = self._ledger.job_billing(t.tenant_id, job_id)
        cost_codes = self._ledger.cost_codes(t.tenant_id)
        # The WIP row is the only place earned-vs-billed lives; a job with no
        # schedule simply has none, and the page says less rather than guessing.
        wip = self._ledger.wip(t.tenant_id)
        wip_row = None
        if wip.ok:
            rows = wip.body.get("rows")
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("job_id") == job_id:
                    wip_row = row
                    break
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_edit = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "jobs", render_job(
            t.tenant_id, detail.body,
            cost.body if cost.ok else {},
            work_orders.body if work_orders.ok else {},
            billing.body if billing.ok else {},
            wip_row,
            cost_codes.body if cost_codes.ok else {},
            can_edit=can_edit,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _job_save(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/jobs"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        try:
            contract = self._amount_to_minor(str(data.get("contract", ""))) or 0
            retainage = _percent_to_ppm(str(data.get("retainage", "")))
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        payload: dict[str, object] = {
            "name": str(data.get("name", "")).strip(),
            "customer_id": str(data.get("customer_id", "")).strip(),
            "billing_method": str(data.get("billing_method", "TIME_AND_MATERIALS")).strip(),
            "contract_minor": str(contract),
            "retainage_ppm": retainage,
            "start_date": str(data.get("start_date", "")).strip(),
            "end_date": str(data.get("end_date", "")).strip(),
            "memo": str(data.get("memo", "")).strip(),
        }
        revenue = str(data.get("revenue_account_code", "")).strip()
        if revenue:
            payload["revenue_account_code"] = revenue
        if str(data.get("id", "")).strip():
            payload["id"] = str(data.get("id", "")).strip()
        if str(data.get("status", "")).strip():
            payload["status"] = str(data.get("status", "")).strip()
        res = self._ledger.save_job(t.tenant_id, payload)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        job = res.body.get("job")
        job_id = str(job.get("id")) if isinstance(job, dict) else ""
        if self._audit is not None:
            self._audit.record(subject, "job.saved", self._session_clock(),
                               tenant_id=t.tenant_id, target=job_id)
        return _redirect(f"{back}/{job_id}?done={_qs_escape('Job opened')}")

    def _job_budget_save(
        self, subject: str, t: _Tenant, job_id: str, body: str,
    ) -> Response:
        back = f"/t/{t.tenant_id}/jobs/{job_id}"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        lines: list[dict[str, object]] = []
        try:
            for key, raw in data.items():
                if not key.startswith("bid_"):
                    continue
                code = key[4:]
                bid = self._amount_to_minor(str(raw))
                revised_raw = str(data.get(f"revised_{code}", ""))
                revised = self._amount_to_minor(revised_raw)
                if bid is None and revised is None:
                    continue
                line: dict[str, object] = {
                    "cost_code": code, "budget_cost_minor": str(bid or 0),
                }
                if revised is not None:
                    line["revised_cost_minor"] = str(revised)
                lines.append(line)
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        res = self._ledger.save_job_budget(t.tenant_id, job_id, lines)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "job.budget", self._session_clock(),
                               tenant_id=t.tenant_id, target=job_id)
        return _redirect(f"{back}?done={_qs_escape('Budget saved')}")

    def _job_bill(
        self, subject: str, t: _Tenant, job_id: str, method: str, body: str,
    ) -> Response:
        back = f"/t/{t.tenant_id}/jobs/{job_id}"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        request: dict[str, object] = {
            "id": str(data.get("id", "")).strip(),
            "date": str(data.get("date", "")).strip(),
        }
        if method == "progress":
            lines: list[dict[str, object]] = []
            try:
                for key, raw in data.items():
                    if not key.startswith("pct"):
                        continue
                    text = str(raw).strip()
                    if not text:
                        continue
                    lines.append({
                        "line_no": int(key[3:]),
                        "percent_ppm": _percent_to_ppm(text),
                    })
            except ValueError as exc:
                return _redirect(f"{back}?err={_qs_escape(str(exc))}")
            request["lines"] = lines
        elif method == "milestone":
            request["milestone_id"] = str(data.get("milestone_id", "")).strip()
        elif method == "time-and-materials":
            if str(data.get("through", "")).strip():
                request["through"] = str(data.get("through", "")).strip()
            if str(data.get("summarize", "")).strip():
                request["summarize"] = True
        else:
            return _redirect(f"{back}?err={_qs_escape('Unknown billing method')}")

        res = self._ledger.bill_job(t.tenant_id, job_id, method, request)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "job.billed", self._session_clock(),
                               tenant_id=t.tenant_id, target=str(request.get("id", "")))
        retainage = str(res.body.get("retainage_minor", "0"))
        note = "Invoice raised"
        if retainage not in ("", "0"):
            note = f"Invoice raised; {retainage} minor units held as retainage"
        return _redirect(f"{back}?done={_qs_escape(note)}")

    def _job_deposit(self, subject: str, t: _Tenant, job_id: str, body: str) -> Response:
        back = f"/t/{t.tenant_id}/jobs/{job_id}"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        try:
            amount = self._amount_to_minor(str(data.get("amount", "")))
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        if not amount:
            return _redirect(f"{back}?err={_qs_escape('Enter the deposit amount')}")
        res = self._ledger.take_deposit(t.tenant_id, job_id, {
            "date": str(data.get("date", "")).strip() or self._today(t),
            "amount_minor": str(amount),
            "memo": str(data.get("memo", "")).strip(),
        })
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "job.deposit", self._session_clock(),
                               tenant_id=t.tenant_id, target=job_id)
        return _redirect(f"{back}?done={_qs_escape('Deposit recorded as a liability')}")

    def _recurring_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_recurring_unavailable())
        as_of = query.get("as_of", "").strip() or self._today(t)
        templates = self._ledger.recurring(t.tenant_id)
        if not templates.ok:
            return self._shell(subject, t, "books",
                               render_recurring_unavailable(templates.error()))
        due = self._ledger.recurring_due(t.tenant_id, as_of)
        accounts = self._ledger.accounts(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "books", render_recurring(
            t.tenant_id, templates.body, due.body if due.ok else {},
            accounts.body if accounts.ok else {}, as_of,
            can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _recurring_save(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/books/recurring"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        lines: list[dict[str, object]] = []
        try:
            for i in range(1, 5):
                code = str(data.get(f"code{i}", "")).strip()
                if not code:
                    continue
                debit = self._amount_to_minor(str(data.get(f"debit{i}", "")))
                credit = self._amount_to_minor(str(data.get(f"credit{i}", "")))
                if debit and credit:
                    raise ValueError(f"line {i}: enter a debit or a credit, not both")
                if debit:
                    lines.append({"account_code": code, "side": "DEBIT",
                                  "amount_minor": str(debit)})
                elif credit:
                    lines.append({"account_code": code, "side": "CREDIT",
                                  "amount_minor": str(credit)})
        except ValueError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")

        res = self._ledger.save_recurring(t.tenant_id, {
            "name": str(data.get("name", "")).strip(),
            "frequency": str(data.get("frequency", "MONTHLY")).strip(),
            "interval": str(data.get("interval", "1")).strip() or "1",
            "start_date": str(data.get("start_date", "")).strip(),
            "end_date": str(data.get("end_date", "")).strip(),
            "memo": str(data.get("memo", "")).strip(),
            "lines": lines,
        })
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "recurring.saved", self._session_clock(),
                               tenant_id=t.tenant_id, target=str(data.get("name", "")))
        return _redirect(f"{back}?done={_qs_escape('Memorized')}")

    def _recurring_run(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/books/recurring"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        as_of = str(data.get("as_of", "")).strip() or self._today(t)
        res = self._ledger.run_recurring(
            t.tenant_id, as_of, template_id=str(data.get("id", "")).strip(),
        )
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "recurring.run", self._session_clock(),
                               tenant_id=t.tenant_id,
                               detail=f"{res.body.get('posted', 0)} posted")
        return self._shell(subject, t, "books",
                           render_run_result(t.tenant_id, res.body, as_of))

    def _recurring_delete(self, subject: str, t: _Tenant, template_id: str) -> Response:
        back = f"/t/{t.tenant_id}/books/recurring"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        res = self._ledger.delete_recurring(t.tenant_id, template_id)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "recurring.removed", self._session_clock(),
                               tenant_id=t.tenant_id, target=template_id)
        return _redirect(f"{back}?done={_qs_escape('Removed')}")

    # --- classes and locations --------------------------------------------------

    def _dimensions_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_dimensions_unavailable())
        res = self._ledger.dimensions(t.tenant_id)
        if not res.ok:
            return self._shell(subject, t, "books",
                               render_dimensions_unavailable(res.error()))
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "books", render_dimensions(
            t.tenant_id, res.body, can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _dimension_save(self, subject: str, t: _Tenant, body: str) -> Response:
        back = f"/t/{t.tenant_id}/books/dimensions"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        res = self._ledger.save_dimension(t.tenant_id, {
            "key": str(data.get("key", "")).strip(),
            "label": str(data.get("label", "")).strip(),
            "values": str(data.get("values", "")),
            "required": str(data.get("required", "")).strip() in ("1", "true", "on"),
        })
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "dimension.saved", self._session_clock(),
                               tenant_id=t.tenant_id, target=str(data.get("key", "")))
        return _redirect(f"{back}?done={_qs_escape('Saved')}")

    def _dimension_delete(self, subject: str, t: _Tenant, key: str) -> Response:
        back = f"/t/{t.tenant_id}/books/dimensions"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        res = self._ledger.delete_dimension(t.tenant_id, key)
        if not res.ok:
            return _redirect(f"{back}?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "dimension.removed", self._session_clock(),
                               tenant_id=t.tenant_id, target=key)
        return _redirect(f"{back}?done={_qs_escape('Removed')}")

    def _dimension_report(
        self, subject: str, t: _Tenant, key: str, query: "dict[str, str]"
    ) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_dimensions_unavailable())
        frm = query.get("from", "").strip()
        to = query.get("to", "").strip()
        res = self._ledger.dimension_report(t.tenant_id, key, frm=frm, to=to)
        if not res.ok:
            return self._shell(subject, t, "books",
                               render_dimensions_unavailable(res.error()))
        return self._shell(subject, t, "books",
                           render_dimension_report(t.tenant_id, res.body, frm=frm, to=to))

    def _dimensions_of(self, data: "Mapping[str, object]", suffix: str = "") -> "dict[str, str]":
        """Collect dim_<key> fields off a form into a dimensions object."""
        out: dict[str, str] = {}
        for field, raw in data.items():
            if not field.startswith("dim_"):
                continue
            name = field[len("dim_"):]
            if suffix:
                if not name.endswith(suffix):
                    continue
                name = name[: -len(suffix)]
            value = str(raw).strip()
            if value:
                out[name] = value
        return out

    # --- general ledger and budgets --------------------------------------------

    def _gl_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable())
        frm = query.get("from", "").strip()
        to = query.get("to", "").strip()
        res = self._ledger.general_ledger(
            t.tenant_id, frm=frm, to=to, codes=query.get("codes", "").strip(),
        )
        if not res.ok:
            return self._shell(subject, t, "books", render_books_unavailable(res.error()))
        return self._shell(subject, t, "books",
                           render_general_ledger(t.tenant_id, res.body, frm=frm, to=to))

    def _budget_period(self, t: _Tenant, query: "dict[str, str]") -> str:
        period = query.get("period", "").strip()
        return period or self._today(t)[:7]

    def _budget_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable())
        period = self._budget_period(t, query)
        res = self._ledger.budget(t.tenant_id, period)
        if not res.ok:
            return self._shell(subject, t, "books", render_books_unavailable(res.error()))
        accounts = self._ledger.accounts(t.tenant_id)
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        return self._shell(subject, t, "books", render_budget(
            t.tenant_id, period, res.body, accounts.body if accounts.ok else {},
            can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        ))

    def _budget_save(self, subject: str, t: _Tenant, body: str) -> Response:
        """Save a period's budget. A blank line is left unbudgeted rather than
        recorded as a plan to earn or spend nothing — those are different claims."""
        data = self._form_or_json(body)
        period = str(data.get("period", "")).strip()
        back = f"/t/{t.tenant_id}/books/budget?period={_qs_escape(period)}"
        if self._ledger is None:
            return _redirect(f"{back}&err=No+ledger+service+configured")

        lines: list[dict[str, object]] = []
        try:
            for key, raw in data.items():
                if not key.startswith("amount_"):
                    continue
                text = str(raw).strip()
                if not text:
                    continue
                minor = self._amount_to_minor(text)
                if minor is None:
                    continue
                lines.append({
                    "account_code": key[len("amount_"):],
                    "amount_minor": str(minor),
                })
        except ValueError as exc:
            return _redirect(f"{back}&err={_qs_escape(str(exc))}")
        if not lines:
            return _redirect(f"{back}&err={_qs_escape('Enter at least one budget figure')}")

        res = self._ledger.save_budget(t.tenant_id, period, lines)
        if not res.ok:
            return _redirect(f"{back}&err={_qs_escape(res.error())}")
        saved = res.body.get("saved", 0)
        if self._audit is not None:
            self._audit.record(subject, "budget.saved", self._session_clock(),
                               tenant_id=t.tenant_id, detail=f"{period}: {saved} lines")
        return _redirect(f"{back}&done={_qs_escape(f'Budget saved — {saved} accounts')}")

    # --- bank reconciliation --------------------------------------------------

    def _reconcile_pick(self, subject: str, t: _Tenant) -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable())
        res = self._ledger.accounts(t.tenant_id)
        if not res.ok:
            return self._shell(subject, t, "books", render_books_unavailable(res.error()))
        return self._shell(subject, t, "books", render_pick_account(t.tenant_id, res.body))

    def _reconcile_page(
        self, subject: str, t: _Tenant, code: str, query: "dict[str, str]"
    ) -> Response:
        """The worksheet. The statement date and balance live in the query string,
        so a reconciliation session is a bookmarkable URL rather than server state."""
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable())
        date = query.get("statement_date", "").strip() or self._today(t)
        raw = query.get("statement_balance", "").strip()
        minor = query.get("statement_balance_minor", "").strip()
        if not minor:
            try:
                minor = str(self._amount_to_minor(raw) or 0)
            except ValueError as exc:
                minor = "0"
                query = dict(query, err=str(exc))
        res = self._ledger.reconcile_view(t.tenant_id, code, date, minor)
        if not res.ok:
            return self._shell(subject, t, "books", render_books_unavailable(res.error()))
        perms, _role = self._perms_role(subject, t.tenant_id)
        can_post = self._policy is None or Permission.POST_JOURNAL in perms
        body = render_reconcile(
            t.tenant_id, res.body, can_post=can_post,
            message=query.get("done", ""), error=query.get("err", ""),
        )
        return self._shell(subject, t, "books", body)

    def _reconcile_import(
        self, subject: str, t: _Tenant, code: str, req: Request
    ) -> Response:
        """Import the bank's own export and tick off what it matches.

        The file is decoded as text with replacement: a statement is UTF-8 or
        Latin-1 in practice, and a stray byte in a payee name should not stop a
        reconciliation. The amounts are parsed exactly by the service."""
        back = f"/t/{t.tenant_id}/books/reconcile/{code}"
        if self._ledger is None:
            return _redirect(f"{back}?err=No+ledger+service+configured")
        try:
            fields, files = parse_multipart(req.body, req.headers.get("content-type", ""))
        except MultipartError as exc:
            return _redirect(f"{back}?err={_qs_escape(str(exc))}")
        if not files:
            return _redirect(f"{back}?err={_qs_escape('Choose a statement file')}")

        date = fields.get("statement_date", "").strip()
        minor = fields.get("statement_balance_minor", "").strip() or "0"
        text = files[0].content.decode("utf-8", "replace")

        res = self._ledger.reconcile_import(t.tenant_id, code, text, date, minor)
        if not res.ok:
            return _redirect(
                f"{back}?statement_date={_qs_escape(date)}"
                f"&statement_balance_minor={_qs_escape(minor)}"
                f"&err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "bank.statement_imported", self._session_clock(),
                               tenant_id=t.tenant_id, target=code,
                               detail=f"{res.body.get('newly_cleared', 0)} matched")
        return self._shell(subject, t, "books",
                           render_import_result(t.tenant_id, code, res.body, date))

    def _reconcile_action(
        self, subject: str, t: _Tenant, code: str, action: str, body: str
    ) -> Response:
        data = self._form_or_json(body)
        date = str(data.get("statement_date", "")).strip()
        minor = str(data.get("statement_balance_minor", "")).strip() or "0"
        back = (f"/t/{t.tenant_id}/books/reconcile/{code}"
                f"?statement_date={_qs_escape(date)}&statement_balance_minor={_qs_escape(minor)}")
        if self._ledger is None:
            return _redirect(f"{back}&err=No+ledger+service+configured")

        if action == "toggle":
            res = self._ledger.reconcile_toggle(
                t.tenant_id, code, str(data.get("entry_id", "")).strip(),
                str(data.get("cleared", "")).strip() in ("1", "true", "on"), date, minor,
            )
            if not res.ok:
                return _redirect(f"{back}&err={_qs_escape(res.error())}")
            return _redirect(back)

        res = self._ledger.reconcile_finish(t.tenant_id, code, date, minor)
        if not res.ok:
            return _redirect(f"{back}&err={_qs_escape(res.error())}")
        count = res.body.get("reconciled_entries", 0)
        if self._audit is not None:
            self._audit.record(subject, "bank.reconciled", self._session_clock(),
                               tenant_id=t.tenant_id, detail=f"{code} through {date}")
        return _redirect(
            f"{back}&done={_qs_escape(f'Reconciled {count} transactions through {date}')}")

    def _books_statements_page(self, subject: str, t: _Tenant, query: "dict[str, str]") -> Response:
        if self._ledger is None:
            return self._shell(subject, t, "books", render_books_unavailable())
        frm = query.get("from", "")
        to = query.get("to", "")
        if not frm or not to:
            # default to the current month of the tenant's forecast as-of date
            as_of = t.inputs.opening.as_of.isoformat()
            frm = f"{as_of[:7]}-01"
            to = as_of
        res = self._ledger.statements(t.tenant_id, frm, to)
        if not res.ok:
            return self._shell(subject, t, "books", render_books_unavailable(res.error()))
        return self._shell(subject, t, "books",
                           render_books_statements(f"{frm} to {to}", res.body))

    def _books_post_entry(self, subject: str, t: _Tenant, body: str) -> Response:
        """Post a journal entry from the owner form. Amounts are parsed exactly;
        the ledger service is the one that enforces balance and period locks."""
        if self._ledger is None:
            return _redirect(f"/t/{t.tenant_id}/books?err=No+ledger+service+configured")
        data = self._form_or_json(body)
        date = str(data.get("date", "")).strip()
        memo = str(data.get("memo", "")).strip()
        lines: list[dict[str, object]] = []
        try:
            for i in range(1, 5):
                code = str(data.get(f"code{i}", "")).strip()
                if not code:
                    continue
                debit = self._amount_to_minor(str(data.get(f"debit{i}", "")))
                credit = self._amount_to_minor(str(data.get(f"credit{i}", "")))
                if debit and credit:
                    raise ValueError(f"line {i}: enter a debit or a credit, not both")
                dims = self._dimensions_of(data, suffix=str(i))
                line: dict[str, object] = {"code": code}
                if dims:
                    line["dimensions"] = dims
                if debit:
                    lines.append({**line, "side": "DEBIT", "amount_minor": str(debit)})
                elif credit:
                    lines.append({**line, "side": "CREDIT", "amount_minor": str(credit)})
        except ValueError as exc:
            return _redirect(f"/t/{t.tenant_id}/books?err={_qs_escape(str(exc))}")
        if len(lines) < 2:
            return _redirect(
                f"/t/{t.tenant_id}/books?err={_qs_escape('An entry needs at least two lines')}")

        res = self._ledger.post_entry(t.tenant_id, date, lines, memo=memo, source="owner")
        if not res.ok:
            return _redirect(f"/t/{t.tenant_id}/books?err={_qs_escape(res.error())}")
        if self._audit is not None:
            self._audit.record(subject, "journal.posted", self._session_clock(),
                               tenant_id=t.tenant_id, detail=f"{date} {memo}".strip())
        return _redirect(f"/t/{t.tenant_id}/books?posted={_qs_escape('Entry posted')}")

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
        """The bank register.

        With a ledger service configured this IS the review inbox — the real
        queue, backed by the tenant's durable feed, where accepting a line posts
        a journal entry. The in-memory register below is only the demo surface
        for a deployment with no ledger behind it, and it posts nothing."""
        if self._ledger is not None:
            return _redirect(f"/t/{t.tenant_id}/inbox")
        perms, role = self._perms_role(subject, t.tenant_id)
        can_cat = self._policy is None or Permission.CATEGORIZE_TXNS in perms
        txns = self._txns.get(t.tenant_id, [])
        # Only compute suggestions when the caller can act on them.
        suggestions = self._suggestions_for(t) if can_cat else None
        body = render_transactions(
            t.tenant_id, self._accounts.get(t.tenant_id, "Checking"), txns,
            can_categorize=can_cat, suggestions=suggestions,
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
        forecast = self._forecast(t)
        body = render_cash_body(forecast, t.name)
        facts = self._ledger_facts.get(t.tenant_id)
        if facts is not None:
            body = _provenance_note(facts, provenance_split(self._effective_inputs(t))) + body
        return self._shell(subject, t, "cash", body)

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

    # --- scenario planning (what-if) ----------------------------------------
    def _scenarios_page(self, subject: str, t: _Tenant) -> Response:
        return self._shell(subject, t, "scenarios", render_scenario_body(t.tenant_id, t.name))

    def _scenario_from_template(self, template: str, params: Mapping[str, object]) -> Scenario:
        """Build a library `Scenario` from a template key + its params."""
        def money(key: str) -> Money:
            return Money.from_decimal(str(params[key]))

        def day(key: str) -> date:
            return date.fromisoformat(str(params[key]))

        if template == "hire":
            return hire_employee(money("monthly_cost"), day("start"))
        if template == "customer_pays_late":
            return customer_pays_late(str(params["customer_id"]), int(str(params["days"])))
        if template == "take_loan":
            return take_loan(money("amount"), day("on"), money("monthly_repayment"),
                             day("first_repayment"))
        if template == "one_time_expense":
            return one_time_expense(str(params.get("label", "One-time expense")),
                                    money("amount"), day("on"))
        raise ValueError(f"unknown template {template!r}")

    def _parse_adjustment(self, a: Mapping[str, object]) -> Adjustment:
        """Parse one raw adjustment dict from a custom scenario spec."""
        kind = str(a.get("kind", ""))
        if kind == "delay_customer":
            return DelayCustomerPayment(str(a["customer_id"]), int(str(a["days"])))
        if kind == "one_time":
            return OneTimeFlow(str(a.get("label", "One-time")),
                               Money.from_decimal(str(a["amount"])),
                               date.fromisoformat(str(a["on"])), bool(a.get("inflow", False)))
        if kind == "set_minimum_cash":
            return SetMinimumCash(Money.from_decimal(str(a["amount"])))
        raise ValueError(f"unknown adjustment kind {kind!r}")

    def _build_scenario(self, payload: Mapping[str, object]) -> Scenario:
        """A scenario from a spec: a library `template` + `params`, or a raw list of
        `adjustments`."""
        template = payload.get("template")
        if isinstance(template, str):
            raw = payload.get("params", {})
            params = raw if isinstance(raw, dict) else {}
            return self._scenario_from_template(template, params)
        adjustments = payload.get("adjustments")
        if isinstance(adjustments, list):
            parsed = tuple(
                self._parse_adjustment(a) for a in adjustments if isinstance(a, dict)
            )
            if not parsed:
                raise ValueError("no valid adjustments in spec")
            return Scenario(name=str(payload.get("name", "Custom scenario")), adjustments=parsed)
        raise ValueError("provide a 'template' (+ params) or a list of 'adjustments'")

    @staticmethod
    def _diff_json(scenario: Scenario, diff: ScenarioDiff) -> dict[str, object]:
        return {
            "scenario": scenario.name,
            "trough_delta": diff.trough_delta.to_decimal_string(),
            "cushion_delta": diff.cushion_delta.to_decimal_string(),
            "breach_week_before": diff.breach_week_before,
            "breach_week_after": diff.breach_week_after,
            "weekly_closing_deltas": [d.to_decimal_string() for d in diff.weekly_closing_deltas],
        }

    def _scenario(self, t: _Tenant, body: str) -> Response:
        """Run a what-if scenario against the tenant's live inputs/config and return
        the signed `ScenarioDiff` as JSON. Nothing is persisted."""
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        if not isinstance(payload, dict):
            return _json(400, {"error": "expected a JSON object"})
        try:
            scenario = self._build_scenario(payload)
        except (KeyError, TypeError, ValueError) as exc:
            return _json(400, {"error": f"invalid scenario spec: {exc}"})
        _result, diff = run_scenario(
            self._effective_inputs(t), self._effective_config(t), scenario
        )
        return _json(200, self._diff_json(scenario, diff))

    # --- AR / collections ----------------------------------------------------
    def _ar_data(
        self, t: _Tenant
    ) -> "tuple[ARReport, list[ChaseItem], list[CollectionNudge]]":
        """Compute the AR surface for a tenant from its open invoices: the point-in-
        time report, the prioritized chase list, and a nudge draft per chase item."""
        inputs = self._effective_inputs(t)
        as_of = inputs.opening.as_of
        invoices = inputs.invoices
        report = ar_report(invoices, as_of)
        histories = {h.customer_id: h for h in inputs.customer_histories}
        chase = chase_list(invoices, as_of, histories=histories)
        nudges = [draft_nudge(item.invoice, as_of) for item in chase]
        return report, chase, nudges

    def _receivables_page(self, subject: str, t: _Tenant) -> Response:
        report, chase, nudges = self._ar_data(t)
        body = render_ar_body(t.tenant_id, t.name, report, chase, nudges)
        return self._shell(subject, t, "receivables", body)

    def _receivables_json(self, t: _Tenant) -> Response:
        report, chase, nudges = self._ar_data(t)
        aging = report.aging
        return _json(200, {
            "tenant": t.tenant_id,
            "as_of": report.as_of.isoformat(),
            "total_ar": report.total_ar.to_decimal_string(),
            "overdue_total": report.overdue_total.to_decimal_string(),
            "dso": report.dso,
            "aging": {
                "current": aging.current.to_decimal_string(),
                "d1_30": aging.d1_30.to_decimal_string(),
                "d31_60": aging.d31_60.to_decimal_string(),
                "d60_plus": aging.d60_plus.to_decimal_string(),
            },
            "chase_list": [
                {"invoice": c.invoice.id, "customer": c.invoice.customer_id,
                 "open_amount": c.invoice.open_amount.to_decimal_string(),
                 "days_overdue": c.days_overdue, "bucket": c.bucket.value,
                 "risk_score": c.risk_score, "priority": c.priority}
                for c in chase
            ],
            "nudges": [
                {"invoice": n.invoice.id, "tone": n.tone.value,
                 "subject": n.subject, "body": n.body}
                for n in nudges
            ],
        })

    # --- reporting (rgnr8-reports) ------------------------------------------
    def _report_context(self, t: _Tenant) -> DataContext:
        """Assemble a reports `DataContext` for a tenant from what the app already
        holds: the (override-aware) forecast, its open invoices + histories for AR,
        the bank register, plus any attached financial-statements dict / budget /
        usage summary. Missing sources make their sections degrade gracefully."""
        inputs = self._effective_inputs(t)
        fc = self._forecast(t)
        as_of = inputs.opening.as_of
        board = self._close.get(t.tenant_id)
        period = board.period if board is not None else as_of.strftime("%Y-%m")
        transactions = tuple(
            ReportTransaction(
                on_date=date.fromisoformat(r.date),
                description=r.description,
                category=r.category,
                amount_minor=r.amount.minor_units,
                currency=r.amount.currency,
            )
            for r in self._txns.get(t.tenant_id, [])
        )
        histories = {h.customer_id: h for h in inputs.customer_histories}
        return DataContext(
            period=period,
            as_of=as_of,
            currency=fc.projection.currency,
            forecast=fc,
            invoices=inputs.invoices,
            histories=histories,
            transactions=transactions,
            usage=self._usage.get(t.tenant_id),
            account=self._billing_accounts.get(t.tenant_id),
            financial_statements=self._financial_statements.get(t.tenant_id),
            budget=self._budgets.get(t.tenant_id),
            forecast_inputs=inputs,
            forecast_config=self._effective_config(t),
        )

    def _resolve_report_spec(self, t: _Tenant, report_id: str) -> ReportSpec | None:
        """A baseline report (from the library) or one of the tenant's saved custom
        reports, by id. None → no such report for this tenant."""
        if report_id in BASELINE_REPORTS:
            return BASELINE_REPORTS[report_id]
        return self._saved_reports.get(t.tenant_id, report_id)

    def _reports_page(self, subject: str, t: _Tenant) -> Response:
        """The reports index inside the shell: the baseline library + this tenant's
        saved custom reports, each linking to its render + JSON/CSV exports."""
        baselines = list(BASELINE_REPORTS.values())
        saved = self._saved_reports.list_for_tenant(t.tenant_id)
        return self._shell(subject, t, "reports",
                           render_reports_list(t.tenant_id, baselines, saved))

    def _report_page(self, subject: str, t: _Tenant, report_id: str) -> Response:
        """Render a baseline or saved report against the tenant's live data, wrapped
        in the app shell. Unknown id → 404."""
        # Owner-facing ledger reports (aging/budget/retained-earnings) come from
        # the TS core as JSON contracts; render them if this id is one and we hold it.
        kind = self._OWNER_REPORT_IDS.get(report_id)
        if kind is not None:
            data = self._owner_reports.get((t.tenant_id, kind))
            if data is None:
                return self._shell(subject, t, "reports",
                                   '<section class="card"><p class="muted">This report isn\'t available yet — '
                                   'it appears once the ledger has computed it.</p></section>')
            html = render_owner_report(kind, data)
            if html is not None:
                return self._shell(subject, t, "reports", html)

        spec = self._resolve_report_spec(t, report_id)
        if spec is None:
            return _json(404, {"error": f"unknown report {report_id}"})
        report = render_report(spec, self._report_context(t), clock=self._report_clock)
        return self._shell(subject, t, "reports", render_report_html(report))

    # Export suffix -> (content-type, whether the export is a file download).
    _REPORT_EXPORTS: "dict[str, str]" = {
        ".json": "application/json",
        ".csv": "text/csv; charset=utf-8",
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }

    def _report_export(self, t: _Tenant, filename: str) -> Response:
        """Export a rendered report as JSON, CSV, PDF, or XLSX. `filename` is the
        report id plus a format suffix (`.json`/`.csv`/`.pdf`/`.xlsx`). Unknown id
        or suffix → 404. PDF/XLSX are binary and are sent as file downloads."""
        suffix = ""
        for ext in self._REPORT_EXPORTS:
            if filename.endswith(ext):
                suffix = ext
                break
        if not suffix:
            return _json(404, {"error": "export must end in .json, .csv, .pdf, or .xlsx"})
        report_id = filename[: -len(suffix)]
        spec = self._resolve_report_spec(t, report_id)
        if spec is None:
            return _json(404, {"error": f"unknown report {report_id}"})
        report = render_report(spec, self._report_context(t), clock=self._report_clock)
        content_type = self._REPORT_EXPORTS[suffix]
        body: str | bytes
        if suffix == ".csv":
            body = render_csv(report)
        elif suffix == ".pdf":
            body = render_report_pdf(report)
        elif suffix == ".xlsx":
            body = render_report_xlsx(report)
        else:
            body = to_json(report)
        extra: tuple[tuple[str, str], ...] = ()
        if suffix in (".pdf", ".xlsx"):
            # Offer a sensible download filename for the binary exports.
            extra = (("Content-Disposition", f'attachment; filename="{report_id}{suffix}"'),)
        return Response(200, body, content_type, extra)

    def _reports_save(self, t: _Tenant, body: str) -> Response:
        """Build a `ReportSpec` from a JSON `{id, title, description, sections}` body
        and persist it to the tenant's saved-report store. A bad spec (an unknown
        section kind) is rejected with 400."""
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        try:
            spec = build_report(payload)
        except ReportSpecError as exc:
            return _json(400, {"error": f"invalid report spec: {exc}"})
        self._saved_reports.save(t.tenant_id, spec)
        return _json(201, {"id": spec.id, "title": spec.title,
                           "sections": [s.kind for s in spec.sections]})

    # --- QuickBooks Online connect (rgnr8-qbo) -------------------------------
    def _connect_page(self, subject: str, t: _Tenant, sync_flag: str = "") -> Response:
        """The connections page: QBO status + a Connect / Reconnect / Disconnect
        control, a Sync-now action, and the last sync's summary. Shows a "not
        configured" note when no `QboConnectService` is seated."""
        from .screens import render_connect_page
        configured = self._qbo is not None
        conn = self._qbo.status(t.tenant_id) if self._qbo is not None else None
        status = conn.status.value if conn is not None else None
        realm = conn.realm_id if conn is not None else None
        summary = self._qbo_last_sync.get(t.tenant_id)
        last_sync = None
        if summary is not None:
            last_sync = {
                "company": summary.company,
                "cash": format_money(summary.cash),
                "bank_accounts": str(summary.bank_accounts),
                "invoice_count": str(summary.invoice_count),
                "ar_total": format_money(summary.ar_total),
                "bill_count": str(summary.bill_count),
                "ap_total": format_money(summary.ap_total),
            }
        books = self._qbo_last_ledger_sync.get(t.tenant_id)
        ledger_sync = None
        if books is not None:
            ledger_sync = {
                "invoices": str(books.invoices),
                "bills": str(books.bills),
                "bank_new": str(books.bank_new),
                "pending_review": str(books.pending_review),
                "skipped": str(books.documents_skipped + books.bank_duplicates),
                "errors": "; ".join(books.errors[:3]),
            }
        return self._shell(subject, t, "connect",
                           render_connect_page(t.tenant_id, configured=configured,
                                               status=status, realm_id=realm,
                                               last_sync=last_sync, sync_flag=sync_flag,
                                               ledger_sync=ledger_sync))

    def _qbo_begin(self, t: _Tenant) -> Response:
        """Redirect the owner to Intuit's authorize screen (signed, tenant-bound
        state). 501 when QBO isn't configured on this deployment."""
        if self._qbo is None:
            return _json(501, {"error": "QuickBooks connect is not configured"})
        return _redirect(self._qbo.begin(t.tenant_id))

    def _qbo_disconnect(self, subject: str, t: _Tenant) -> Response:
        """Revoke at Intuit (best-effort) + drop the stored connection, then back
        to the connections page."""
        if self._qbo is None:
            return _json(501, {"error": "QuickBooks connect is not configured"})
        self._qbo.disconnect(t.tenant_id)
        if self._audit is not None:
            self._audit.record(subject, "qbo.disconnected", self._session_clock(),
                               tenant_id=t.tenant_id, target=t.tenant_id)
        return _redirect(f"/t/{t.tenant_id}/connect")

    def _qbo_sync(self, subject: str, t: _Tenant) -> Response:
        """Pull the connected QuickBooks company and rebuild this tenant's forecast
        inputs from it (cash, AR, AP), so the whole app reflects the live books.
        501 if QBO isn't configured; if the tenant isn't connected (or its refresh
        token lapsed) the connect page shows a reconnect prompt."""
        if self._qbo is None:
            return _json(501, {"error": "QuickBooks connect is not configured"})
        client = self._qbo.api_client(t.tenant_id)
        if client is None:
            # not connected / needs reconnect — bounce back to the connect page
            return _redirect(f"/t/{t.tenant_id}/connect")
        as_of = date.fromtimestamp(self._session_clock())
        try:
            inputs, summary = build_inputs_from_qbo(client, as_of=as_of, currency=t.config.currency)
        except Exception:
            # a live API failure shouldn't 500 the owner; surface a soft error
            return _redirect(f"/t/{t.tenant_id}/connect?sync=error")
        # replace the tenant's inputs with the synced ones; invalidate the cache
        # so the next forecast/briefing/report recomputes from the live books.
        t.inputs = inputs
        t._dirty = True
        self._qbo_last_sync[t.tenant_id] = summary
        if self._audit is not None:
            self._audit.record(subject, "qbo.synced", self._session_clock(),
                               tenant_id=t.tenant_id,
                               target=f"cash={summary.cash.to_decimal_string()} "
                                      f"ar={summary.invoice_count} ap={summary.bill_count}")

        # The forecast is a projection; the ledger is the record. With a ledger
        # configured, the same sync brings the actual bookkeeping across — open
        # items as real AR/AP documents, bank movements into the review queue
        # (never straight into the books, because QuickBooks doesn't know this
        # chart of accounts).
        if self._ledger is not None:
            ledger_summary = sync_qbo_to_ledger(client, self._ledger, t.tenant_id)
            self._qbo_last_ledger_sync[t.tenant_id] = ledger_summary
            if self._audit is not None:
                self._audit.record(subject, "qbo.ledger_synced", self._session_clock(),
                                   tenant_id=t.tenant_id, detail=ledger_summary.describe())
            if not ledger_summary.ok:
                return _redirect(f"/t/{t.tenant_id}/connect?sync=partial")
        return _redirect(f"/t/{t.tenant_id}/connect?sync=ok")

    def _qbo_callback(self, req: Request) -> Response:
        """Intuit's OAuth redirect target. Verifies the signed `state` (which
        carries the tenant), exchanges the `code`, persists the connection, and
        redirects to that tenant's connections page. All failures render a small
        error page rather than leaking details."""
        if self._qbo is None:
            return _html(503, "<p>QuickBooks connect is not configured.</p>")
        q = req.query
        error = q.get("error")
        if error:
            return _html(400, f"<p>QuickBooks authorization was declined ({escape(error)}).</p>")
        code = q.get("code", "")
        state = q.get("state", "")
        realm_id = q.get("realmId", "")
        if not code or not state:
            return _html(400, "<p>Missing authorization code or state.</p>")
        try:
            conn = self._qbo.complete(state, code, realm_id)
        except Exception:
            # never leak state/exchange internals to the browser
            return _html(400, "<p>Could not complete the QuickBooks connection. "
                              "Please start the connect again from your dashboard.</p>")
        if self._audit is not None:
            self._audit.record("", "qbo.connected", self._session_clock(),
                               tenant_id=conn.tenant_id, target=conn.realm_id)
        return _redirect(f"/t/{conn.tenant_id}/connect")

    def _transactions_json(self, t: _Tenant) -> Response:
        """The bank register as JSON: summary + the for-review queue (the shape the
        MCP `review_transactions` tool and partner integrations consume).

        Served from the ledger service's real inbox when one is configured."""
        if self._ledger is not None:
            return self._inbox_json(t)
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
