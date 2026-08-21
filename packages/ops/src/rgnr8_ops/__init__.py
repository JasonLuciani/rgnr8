"""RGNR8 ops — onboard beta tenants and watch the fleet from one dashboard."""

from __future__ import annotations

from .alerts_job import (
    AlertsJob,
    build_alert_dispatcher,
    build_alerts_job,
    default_alert_rules,
)
from .app_factory import (
    build_authenticator,
    build_observability,
    build_web_app,
    create_application,
    load_fleet,
    readiness,
)
from .config import ConfigError, Settings
from .console import render_operator_console
from .db_posture import rls_bypass_warnings
from .deploy import bootstrap_python_schemas
from .fleet import BetaTenant, Fleet, default_schedule
from .migrate import (
    PYTHON_MIGRATIONS,
    Migration,
    MigrationDriftError,
    MigrationResult,
    run_migrations,
    with_tenant,
)
from .onboarding import (
    BUSINESS_CATEGORIES,
    GO_LIVE_CONTRACT,
    SOURCE_SYSTEMS,
    CutoverRecord,
    OnboardingError,
    OnboardingRegistry,
    SqlOnboardingRegistry,
    build_go_live_request,
    category_catalog,
    is_valid_category,
    qbo_trial_balance_to_source_accounts,
    subtype_for_account_type,
)
from .operator_app import OperatorApp, operator_wsgi
from .platform import PlatformAdmin, PlatformError
from .provisioning import Provisioning, build_provisioning
from .recon import TenantRecon, make_recon
from .report import OpsReport, TenantOpsRow, build_ops_report, render_ops_html
from .report_job import (
    ReportJob,
    build_report_job,
    fleet_context_builder,
    make_spec_resolver,
)
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
from .status import CloseProgress, ConnectorHealth, TenantOpsStatus
from .store import FleetStore, FleetTenantRecord, InMemoryFleetStore, SqlFleetStore
from .worker import build_fleet_jobs, build_worker

__version__ = "0.1.0"

__all__ = [
    "rls_bypass_warnings",
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
    "build_fleet_jobs",
    "build_worker",
    "Provisioning",
    "OnboardingRegistry",
    "SqlOnboardingRegistry",
    "OnboardingError",
    "CutoverRecord",
    "build_go_live_request",
    "qbo_trial_balance_to_source_accounts",
    "subtype_for_account_type",
    "category_catalog",
    "is_valid_category",
    "BUSINESS_CATEGORIES",
    "SOURCE_SYSTEMS",
    "GO_LIVE_CONTRACT",
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
