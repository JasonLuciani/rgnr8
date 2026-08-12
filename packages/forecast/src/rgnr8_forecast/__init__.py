"""RGNR8 direct-method 13-week cash forecast engine.

Deterministic, explainable, versioned. Begins with verified cash and projects
dated inflows/outflows; every projected number traces to its source.
"""

from __future__ import annotations

from .dates import Frequency, Recurrence, WeekBucket, add_months, week_buckets
from .enums import (
    Category,
    Confidence,
    Direction,
    InvoiceStatus,
    PublicationStatus,
    Scenario,
)
from .flow import CashFlow
from .models import (
    Bill,
    CashPosition,
    CustomerHistory,
    DebtInstrument,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    OneTimeItem,
    PaymentObservation,
    PayrollSchedule,
    PipelineOpportunity,
    Provenance,
    RecurringItem,
    ScenarioAssumptions,
    base_assumptions,
    downside_assumptions,
    upside_assumptions,
)
from .money import CurrencyMismatch, Money, money_sum
from .predict import TimingPrediction, predict_days_late
from .projection import Breach, CashTrough, Projection, WeekLine, compute_projection
from .reproducibility import ForecastVersion, fingerprint
from .resolve import ResolvedFlows, resolve
from .result import ForecastResult, run_forecast
from .scenarios import ScenarioComparison, run_all_scenarios
from .io import CONTRACT_VERSION, dumps, from_dto, loads, to_dto
from .backtest import (
    BacktestCase,
    BacktestReport,
    BreachScore,
    HorizonStat,
    WeekObservation,
    case_from_forecast,
    render_backtest_text,
    run_backtest,
)

__version__ = "0.1.0"

__all__ = [
    "Money",
    "money_sum",
    "CurrencyMismatch",
    "Frequency",
    "Recurrence",
    "WeekBucket",
    "week_buckets",
    "add_months",
    "Category",
    "Confidence",
    "Direction",
    "InvoiceStatus",
    "PublicationStatus",
    "Scenario",
    "CashFlow",
    "Provenance",
    "CashPosition",
    "Invoice",
    "PaymentObservation",
    "CustomerHistory",
    "Bill",
    "RecurringItem",
    "PayrollSchedule",
    "DebtInstrument",
    "OneTimeItem",
    "PipelineOpportunity",
    "ForecastInputs",
    "ForecastConfig",
    "ScenarioAssumptions",
    "base_assumptions",
    "downside_assumptions",
    "upside_assumptions",
    "TimingPrediction",
    "predict_days_late",
    "Projection",
    "WeekLine",
    "CashTrough",
    "Breach",
    "compute_projection",
    "ForecastVersion",
    "fingerprint",
    "resolve",
    "ResolvedFlows",
    "ForecastResult",
    "run_forecast",
    "ScenarioComparison",
    "run_all_scenarios",
    "to_dto",
    "from_dto",
    "dumps",
    "loads",
    "CONTRACT_VERSION",
    "BacktestCase",
    "BacktestReport",
    "BreachScore",
    "HorizonStat",
    "WeekObservation",
    "case_from_forecast",
    "render_backtest_text",
    "run_backtest",
    "THEME_CSS",
    "RG_TOKENS_CSS",
    "RG_BASE_CSS",
    "brand_bar",
    "wordmark_svg",
    "mark_svg",
    "status_color",
    "STATUS_COLOR",
]

from .brand import (  # noqa: E402
    RG_BASE_CSS,
    RG_TOKENS_CSS,
    STATUS_COLOR,
    THEME_CSS,
    brand_bar,
    mark_svg,
    status_color,
    wordmark_svg,
)
