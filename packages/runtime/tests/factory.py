from __future__ import annotations

from datetime import date

from rgnr8_forecast import (
    CashPosition,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    Money,
)
from rgnr8_runtime import InMemoryTenantSource, RuntimeTenant


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def inputs(available: str) -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=usd(available)),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),),
    )


def tenant_source() -> InMemoryTenantSource:
    src = InMemoryTenantSource()
    src.add(RuntimeTenant("bright", "Bright Agency", inputs("80000.00"), ForecastConfig(minimum_cash=usd("10000.00"))))
    src.add(RuntimeTenant("acme", "Acme Co", inputs("20000.00"), ForecastConfig(minimum_cash=usd("10000.00"))))
    return src
