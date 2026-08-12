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
    IntervalJob,
    Job,
    JobResult,
    JobRunStore,
)
from .status import ConnectorHealth, CloseProgress, TenantOpsStatus
from .store import FleetStore, FleetTenantRecord, InMemoryFleetStore, SqlFleetStore
from .deploy import bootstrap_python_schemas
from .config import ConfigError, Settings
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
    "ConnectorHealth",
    "CloseProgress",
    "TenantOpsStatus",
    "FleetStore",
    "FleetTenantRecord",
    "InMemoryFleetStore",
    "SqlFleetStore",
    "bootstrap_python_schemas",
    "Settings",
    "ConfigError",
    "build_authenticator",
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
