"""RGNR8 ops — onboard beta tenants and watch the fleet from one dashboard."""

from __future__ import annotations

from .fleet import BetaTenant, Fleet, default_schedule
from .report import OpsReport, TenantOpsRow, build_ops_report, render_ops_html
from .console import render_operator_console
from .platform import PlatformAdmin, PlatformError
from .operator_app import OperatorApp, operator_wsgi
from .scheduler import (
    BriefingDispatchJob,
    Dispatcher,
    InMemoryJobRunStore,
    InMemoryLeaseStore,
    IntervalJob,
    Job,
    JobResult,
    JobRunStore,
    LeasedDispatcher,
    LeaseStore,
    SqlLeaseStore,
)
from .status import ConnectorHealth, CloseProgress, TenantOpsStatus
from .recon import TenantRecon, make_recon
from .alerts_job import (
    AlertsJob,
    build_alert_dispatcher,
    build_alerts_job,
    default_alert_rules,
)
from .report_job import (
    ReportJob,
    build_report_job,
    fleet_context_builder,
    make_spec_resolver,
)
from .store import FleetStore, FleetTenantRecord, InMemoryFleetStore, SqlFleetStore
from .deploy import bootstrap_python_schemas
from .config import ConfigError, Settings
from .provisioning import Provisioning, build_provisioning
from .migrate import (
    Migration,
    MigrationDriftError,
    MigrationResult,
    PYTHON_MIGRATIONS,
    run_migrations,
    with_tenant,
)
from .app_factory import (
    build_authenticator,
    build_observability,
    build_web_app,
    create_application,
    load_fleet,
    readiness,
)

__version__ = "0.1.0"

__all__ = [
    "Fleet",
    "BetaTenant",
    "default_schedule",
    "OpsReport",
    "TenantOpsRow",
    "build_ops_report",
    "render_ops_html",
    "render_operator_console",
    "PlatformAdmin",
    "PlatformError",
    "OperatorApp",
    "operator_wsgi",
    "Dispatcher",
    "Job",
    "JobResult",
    "JobRunStore",
    "InMemoryJobRunStore",
    "IntervalJob",
    "BriefingDispatchJob",
    "LeaseStore",
    "InMemoryLeaseStore",
    "SqlLeaseStore",
    "LeasedDispatcher",
    "ConnectorHealth",
    "CloseProgress",
    "TenantOpsStatus",
    "TenantRecon",
    "make_recon",
    "AlertsJob",
    "build_alert_dispatcher",
    "build_alerts_job",
    "default_alert_rules",
    "ReportJob",
    "build_report_job",
    "fleet_context_builder",
    "make_spec_resolver",
    "Provisioning",
    "build_provisioning",
    "FleetStore",
    "FleetTenantRecord",
    "InMemoryFleetStore",
    "SqlFleetStore",
    "bootstrap_python_schemas",
    "Settings",
    "ConfigError",
    "build_authenticator",
    "build_observability",
    "build_web_app",
    "create_application",
    "load_fleet",
    "readiness",
    "Migration",
    "MigrationDriftError",
    "MigrationResult",
    "PYTHON_MIGRATIONS",
    "run_migrations",
    "with_tenant",
]
