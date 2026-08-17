"""The operator's fleet — onboard beta clients and hold them consistently.

Running beta clients means provisioning each one the *same way* across the web
surface, the delivery runtime, and auth — and being able to mint a session token
for any of them. `Fleet` is that single provisioning point: `onboard(...)` a
client once and it's registered as a web tenant, a delivery `Subscription`, and a
runtime `TenantSource` entry, all from the one `forecast-inputs/1`-shaped inputs
object. This is the repeatable path the ad-hoc prototype did by hand.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from rgnr8_forecast import ForecastConfig, ForecastInputs, Money
from rgnr8_forecast.io import from_dto
from rgnr8_briefing import Schedule, Subscription

if TYPE_CHECKING:
    from .store import FleetStore
from rgnr8_web import FinancialPackageReader, JwtAuthenticator, WebApp, sign_jwt
from rgnr8_runtime import (
    DeliveryRuntime,
    InMemorySubscriptionStore,
    InMemoryTenantSource,
    RuntimeTenant,
    SubscriptionManager,
)
from rgnr8_runtime.subscriptions import SubscriptionStore
from rgnr8_briefing import Deliverer
from rgnr8_reports import (
    InMemoryReportScheduleStore,
    ReportSchedule,
    ReportScheduleStore,
    ReportSink,
)

from .status import TenantOpsStatus
from .recon import TenantRecon, make_recon

if TYPE_CHECKING:
    from rgnr8_reports import SavedReportStore

    from .report_job import ReportJob


def default_schedule() -> Schedule:
    return Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver")


@dataclass(frozen=True, slots=True)
class BetaTenant:
    tenant_id: str
    name: str
    recipient: str
    inputs: ForecastInputs
    config: ForecastConfig
    schedule: Schedule = field(default_factory=default_schedule)


class Fleet:
    """Holds the onboarded beta tenants and builds the app/runtime around them."""

    def __init__(
        self,
        *,
        jwt_secret: str,
        clock: Callable[[], int] | None = None,
        store: "FleetStore | None" = None,
        subscriptions: "SubscriptionStore | None" = None,
        report_schedules: "ReportScheduleStore | None" = None,
    ) -> None:
        self._secret = jwt_secret
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._store = store
        self.tenants: dict[str, BetaTenant] = {}
        # A durable subscription store preserves each briefing's delivery cursor
        # (`last_sent`) across restarts — otherwise the runtime re-onboards with
        # last_sent=None and re-sends the week's briefing to everyone on every
        # restart (pre-launch review). onboard() never overwrites an existing
        # subscription, so a rehydrated cursor survives.
        self.subscriptions: SubscriptionStore = (
            subscriptions if subscriptions is not None else InMemorySubscriptionStore())
        # Standing scheduled reports (which report, format, cadence, recipient) with
        # their own delivery cursors — the same durability story as subscriptions,
        # so the report scheduler never re-sends a cadence after a restart.
        self.report_schedules: ReportScheduleStore = (
            report_schedules if report_schedules is not None else InMemoryReportScheduleStore())
        self.tenant_source = InMemoryTenantSource()
        self.statuses: dict[str, TenantOpsStatus] = {}
        # Operator-attached trust figures (ledger/bank/forecast) per tenant, fed to
        # the recon monitor by build_ops_report. Live, not persisted — like a status.
        self.recon: dict[str, TenantRecon] = {}

    @classmethod
    def load(
        cls,
        store: "FleetStore",
        *,
        jwt_secret: str,
        clock: Callable[[], int] | None = None,
        subscriptions: "SubscriptionStore | None" = None,
        report_schedules: "ReportScheduleStore | None" = None,
    ) -> "Fleet":
        """Rehydrate a fleet from a `FleetStore` — the roster of onboarded
        clients, their forecast inputs + floor + schedule, and last-known
        operational status all survive a restart."""
        from .store import FleetStore as _FS  # noqa: F401 (runtime import breaks the cycle)

        fleet = cls(jwt_secret=jwt_secret, clock=clock, store=store,
                    subscriptions=subscriptions, report_schedules=report_schedules)
        for record in store.load():
            fleet.onboard(record.to_beta_tenant(), persist=False)
            if record.status_json is not None:
                fleet.statuses[record.tenant_id] = TenantOpsStatus.from_json(record.status_json)
        return fleet

    def set_status(self, tenant_id: str, status: TenantOpsStatus, *, persist: bool = True) -> None:
        """Attach connector-health + close-progress for a client (from the TS
        health/close reports) so the fleet dashboard shows books-current +
        close-state alongside cash. Unknown tenants are rejected."""
        if tenant_id not in self.tenants:
            raise KeyError(tenant_id)
        self.statuses[tenant_id] = status
        if persist and self._store is not None:
            self._store.save_status(tenant_id, status.to_json())

    def set_status_from_json(self, tenant_id: str, payload: dict[str, object] | str) -> None:
        """Attach operational status straight from the TS serializers' output
        (``ops-status/1``: ``{"connectors": …, "close": …}``) — the closed loop
        from the live connector-health + close-calendar reports to the dashboard,
        no hand-populated Python objects."""
        self.set_status(tenant_id, TenantOpsStatus.from_json(payload))

    def set_recon(
        self,
        tenant_id: str,
        ledger_cash: Money,
        bank_cash: Money,
        forecast_opening: Money,
        *,
        minor_tolerance: Money | None = None,
        major_tolerance: Money | None = None,
    ) -> None:
        """Attach the three cash figures (ledger / bank / forecast opening) for a
        client so the operator console can surface ledger-vs-bank-vs-forecast
        divergence (via `rgnr8_recon_monitor.check`) next to its cash. Mirrors
        `set_status`; unknown tenants are rejected. Live figures, not persisted."""
        if tenant_id not in self.tenants:
            raise KeyError(tenant_id)
        self.recon[tenant_id] = make_recon(
            ledger_cash, bank_cash, forecast_opening,
            minor_tolerance=minor_tolerance, major_tolerance=major_tolerance,
        )

    def onboard(self, bt: BetaTenant, *, persist: bool = True) -> None:
        """Provision one client everywhere: web tenant, delivery subscription,
        runtime tenant source (and the durable fleet store, if configured)."""
        self.tenants[bt.tenant_id] = bt
        self.tenant_source.add(RuntimeTenant(bt.tenant_id, bt.name, bt.inputs, bt.config))
        # Preserve an existing subscription's delivery cursor across rehydration.
        if not any(s.tenant_id == bt.tenant_id for s in self.subscriptions.list()):
            self.subscriptions.save(Subscription(bt.tenant_id, bt.recipient, bt.schedule, last_sent=None))
        if persist and self._store is not None:
            from .store import FleetTenantRecord

            self._store.save_tenant(FleetTenantRecord.of(bt))

    def onboard_from_dto(
        self,
        tenant_id: str,
        name: str,
        recipient: str,
        dto: dict[str, object] | str,
        minimum_cash: Money,
        *,
        schedule: Schedule | None = None,
    ) -> BetaTenant:
        """Onboard a client straight from a ``forecast-inputs/1`` DTO.

        This is the seam both onboarding stages land on: the **overlay** (RGNR8 on
        top of QBO — bank balances + open AR/AP mapped to the DTO) and the full
        **migration** (QBO GeneralLedger imported into the RGNR8 ledger, then
        reconciled into the DTO) both produce the same ``forecast-inputs/1``
        payload, so onboarding doesn't care which path a tenant arrived by. The
        DTO carries the cash facts; ``minimum_cash`` is the owner's floor
        (operator-set, not in the DTO)."""
        payload = json.loads(dto) if isinstance(dto, str) else dict(dto)
        inputs = from_dto(dict(payload))
        bt = BetaTenant(
            tenant_id=tenant_id,
            name=name,
            recipient=recipient,
            inputs=inputs,
            config=ForecastConfig(minimum_cash=minimum_cash),
            schedule=schedule if schedule is not None else default_schedule(),
        )
        self.onboard(bt)
        return bt

    def web_app(self, packages: FinancialPackageReader | None = None) -> WebApp:
        """A JWT-authenticated, RBAC-enforced web app serving every onboarded
        tenant. Each tenant's owner is seated in a directory so IdP tokens resolve
        to real permissions; no guessable static tokens are minted."""
        from rgnr8_web import InMemoryUserDirectory, Role, User

        directory = InMemoryUserDirectory()
        for bt in self.tenants.values():
            if bt.recipient and "@" in bt.recipient:
                directory.upsert_user(User(id=bt.recipient, email=bt.recipient))
                directory.set_membership(bt.recipient, bt.tenant_id, Role.OWNER)
        app = WebApp(
            packages=packages,
            authenticator=JwtAuthenticator(self._secret, clock=self._clock),
            users=directory,
            require_rbac=True,
        )
        for bt in self.tenants.values():
            app.add_tenant(bt.tenant_id, bt.name, bt.inputs, bt.config,
                           token=sign_jwt({"sub": bt.recipient, "tenant": bt.tenant_id,
                                           "exp": self._clock() + 10 * 365 * 24 * 3600}, self._secret))
        return app

    def mint_token(self, tenant_id: str, *, subject: str | None = None,
                   ttl_seconds: int = 3600, view_as: str | None = None) -> str:
        """A signed session JWT for a tenant (as a real IdP would hand out). The
        token always carries a `sub` (the acting principal) so downstream actions
        and the audit log attribute correctly — defaults to the tenant's owner.
        An optional `view_as` claim carries a client role RGNR8 staff want to see
        the tenant *as* (owner/bookkeeper/viewer/…); the web app only honors it
        for a real platform user."""
        if tenant_id not in self.tenants:
            raise KeyError(tenant_id)
        sub = subject if subject is not None else self.tenants[tenant_id].recipient
        claims: dict[str, object] = {"sub": sub, "tenant": tenant_id,
                                     "exp": self._clock() + ttl_seconds}
        if view_as is not None:
            claims["view_as"] = view_as
        return sign_jwt(claims, self._secret)

    def delivery_runtime(self, deliverer: Deliverer) -> DeliveryRuntime:
        """The delivery runtime over this fleet's subscriptions + tenants."""
        return DeliveryRuntime(self.subscriptions, self.tenant_source, deliverer)

    def subscriptions_manager(self) -> SubscriptionManager:
        """The add/list/pause/resume/remove surface over this fleet's
        subscriptions — extra briefing recipients, schedule changes, pausing a
        client's delivery without losing their cursor."""
        return SubscriptionManager(self.subscriptions)

    def schedule_report(
        self,
        tenant_id: str,
        report_id: str,
        *,
        recipient: str | None = None,
        schedule: Schedule | None = None,
        fmt: str = "pdf",
    ) -> ReportSchedule:
        """Add (or replace) a standing scheduled report for a client. ``report_id``
        is a baseline or the tenant's saved custom report; ``fmt`` is one of the
        report export formats (``pdf``/``xlsx``/``csv``/``html``/``json``). Defaults
        the recipient to the tenant's briefing recipient and the cadence to the
        tenant's briefing schedule, so "email me this report weekly" is one call.
        Unknown tenants are rejected."""
        if tenant_id not in self.tenants:
            raise KeyError(tenant_id)
        bt = self.tenants[tenant_id]
        sched = ReportSchedule(
            tenant_id=tenant_id,
            report_id=report_id,
            recipient=recipient if recipient is not None else bt.recipient,
            schedule=schedule if schedule is not None else bt.schedule,
            fmt=fmt,
        )
        self.report_schedules.save(sched)
        return sched

    def report_job(
        self,
        sink: ReportSink,
        *,
        saved: "SavedReportStore | None" = None,
        name: str = "reports",
    ) -> "ReportJob":
        """The scheduler `ReportJob` over this fleet's report schedules, ready to
        register on the `Dispatcher` alongside the briefing + alerts jobs. ``sink``
        is the report transport; ``saved`` lets scheduled custom reports resolve."""
        from .report_job import build_report_job  # runtime import breaks the cycle

        return build_report_job(self, self.report_schedules, sink, saved=saved, name=name)
