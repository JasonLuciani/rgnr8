"""Shared fixtures: a fixed clock, an anchor date, and a fully-populated
``DataContext`` built from real engine inputs (so the report sections compose the
same shapes production does)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Mapping

import pytest
from rgnr8_billing import Account, AccountStatus, Tier, UsageSummary
from rgnr8_forecast import (
    CashPosition,
    Category,
    Confidence,
    Direction,
    ForecastConfig,
    ForecastInputs,
    ForecastResult,
    Invoice,
    Money,
    OneTimeItem,
    run_forecast,
)
from rgnr8_reports import DataContext, ReconFigures, Transaction
from rgnr8_scenario import Scenario as ScenarioAdjustments
from rgnr8_scenario import one_time_expense

AS_OF = date(2026, 8, 1)
FIXED_NOW = datetime(2026, 8, 13, 9, 0, 0)


def _usd(s: str) -> Money:
    return Money.from_decimal(s)


@pytest.fixture
def as_of() -> date:
    return AS_OF


@pytest.fixture
def clock() -> Callable[[], datetime]:
    return lambda: FIXED_NOW


@pytest.fixture
def forecast_inputs() -> ForecastInputs:
    opening = CashPosition(
        as_of=AS_OF, available=_usd("50000.00"), restricted=Money.zero(), verified=True
    )
    invoices = (
        Invoice("INV-1", "C1", date(2026, 6, 1), date(2026, 6, 15), _usd("10000.00")),
        Invoice("INV-2", "C2", date(2026, 7, 20), date(2026, 8, 10), _usd("5000.00")),
        Invoice("INV-3", "C1", date(2026, 5, 1), date(2026, 5, 20), _usd("8000.00")),
    )
    one_time = (
        OneTimeItem("Rent", Category.RENT, Direction.OUTFLOW, _usd("4000.00"),
                    AS_OF + timedelta(days=7), Confidence.RECORDED),
        OneTimeItem("Payroll", Category.PAYROLL_NET, Direction.OUTFLOW, _usd("15000.00"),
                    AS_OF + timedelta(days=14), Confidence.RECORDED),
        OneTimeItem("Vendor bill", Category.VENDOR_PAYMENT, Direction.OUTFLOW, _usd("12000.00"),
                    AS_OF + timedelta(days=21), Confidence.RECORDED),
    )
    return ForecastInputs(opening=opening, currency="USD", invoices=invoices, one_time=one_time)


@pytest.fixture
def forecast_config() -> ForecastConfig:
    return ForecastConfig(currency="USD", minimum_cash=_usd("10000.00"))


@pytest.fixture
def forecast(forecast_inputs: ForecastInputs, forecast_config: ForecastConfig) -> ForecastResult:
    return run_forecast(forecast_inputs, forecast_config)


@pytest.fixture
def financial_statements() -> Mapping[str, object]:
    """A sample of the cross-language ``financial-statements/1`` contract."""
    return {
        "contract": "financial-statements/1",
        "period": "2026-07",
        "currency": "USD",
        "income_statement": {
            "revenue": [
                {"label": "Product", "amount_minor": 8_000_00},
                {"label": "Services", "amount_minor": 2_000_00},
            ],
            "expenses": [
                {"label": "Payroll", "amount_minor": 5_000_00},
                {"label": "Rent", "amount_minor": 1_200_00},
                {"label": "Software", "amount_minor": 300_00},
            ],
            "total_revenue": 10_000_00,
            "total_expenses": 6_500_00,
            "net_income": 3_500_00,
        },
        "balance_sheet": {
            "assets": [
                {"label": "Cash", "amount_minor": 50_000_00},
                {"label": "Accounts receivable", "amount_minor": 23_000_00},
            ],
            "liabilities": [{"label": "Accounts payable", "amount_minor": 15_000_00}],
            "equity": [{"label": "Retained earnings", "amount_minor": 58_000_00}],
            "total_assets": 73_000_00,
            "total_liabilities": 15_000_00,
            "total_equity": 58_000_00,
            "balanced": True,
        },
        "cash_flow": {
            "operating": [
                {"label": "Net income", "amount_minor": 3_500_00},
                {"label": "Change in AR", "amount_minor": -300_00},
            ],
            "investing": [{"label": "Equipment", "amount_minor": -500_00}],
            "financing": [{"label": "Owner draw", "amount_minor": -200_00}],
            "net_change": 2_500_00,
            "ending_cash": 50_000_00,
        },
    }


@pytest.fixture
def transactions() -> tuple[Transaction, ...]:
    return (
        Transaction(AS_OF, "Client payment", "Sales", 5_000_00),
        Transaction(AS_OF, "Rent", "Rent", -4_000_00),
        Transaction(AS_OF, "Payroll run", "Payroll", -12_000_00),
        Transaction(AS_OF, "Consulting", "Sales", 3_000_00),
    )


@pytest.fixture
def budget() -> Mapping[str, Money]:
    return {"Rent": _usd("3500.00"), "Payroll": _usd("13000.00")}


@pytest.fixture
def usage() -> UsageSummary:
    return UsageSummary(
        account_id="acct1", period="2026-07",
        analyst_minutes=200, briefings_sent=8, api_calls=1500, active_tenants=3,
    )


@pytest.fixture
def account() -> Account:
    return Account(
        id="acct1", name="Acme Co", billing_email="owner@acme.test",
        tier=Tier.ASSISTED, status=AccountStatus.ACTIVE, tenant_ids=("t1",),
    )


@pytest.fixture
def recon() -> ReconFigures:
    return ReconFigures(
        tenant_id="t1",
        ledger_cash=_usd("50000.00"),
        bank_cash=_usd("49500.00"),
        forecast_opening=_usd("50000.00"),
        minor_tolerance=_usd("100.00"),
        major_tolerance=_usd("1000.00"),
    )


@pytest.fixture
def scenario() -> ScenarioAdjustments:
    return one_time_expense("Equipment purchase", _usd("20000.00"), AS_OF + timedelta(days=30))


@pytest.fixture
def full_context(
    forecast: ForecastResult,
    forecast_inputs: ForecastInputs,
    forecast_config: ForecastConfig,
    financial_statements: Mapping[str, object],
    transactions: tuple[Transaction, ...],
    budget: Mapping[str, Money],
    usage: UsageSummary,
    account: Account,
    recon: ReconFigures,
    scenario: ScenarioAdjustments,
) -> DataContext:
    return DataContext(
        period="Aug 2026",
        as_of=AS_OF,
        currency="USD",
        forecast=forecast,
        invoices=forecast_inputs.invoices,
        avg_daily_sales=_usd("1000.00"),
        transactions=transactions,
        usage=usage,
        account=account,
        recon=recon,
        financial_statements=financial_statements,
        budget=budget,
        forecast_inputs=forecast_inputs,
        forecast_config=forecast_config,
        scenario=scenario,
    )
