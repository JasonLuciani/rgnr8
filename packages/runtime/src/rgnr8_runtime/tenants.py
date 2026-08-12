"""Where the runtime gets a tenant's forecast inputs + config.

The delivery runtime needs, for each due subscription, the tenant's
`ForecastInputs` and `ForecastConfig` so it can run the forecast and build the
briefing. That lookup is a seam (`TenantSource`) so production can load tenants
from the DB / the emitted ``forecast-inputs/1`` DTOs while tests use an in-memory
source. This mirrors the web app's tenant provisioning without depending on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from rgnr8_forecast import ForecastConfig, ForecastInputs, Money
from rgnr8_forecast.io import from_dto


@dataclass(frozen=True, slots=True)
class RuntimeTenant:
    tenant_id: str
    name: str
    inputs: ForecastInputs
    config: ForecastConfig


class TenantSource(Protocol):
    def resolve(self, tenant_id: str) -> RuntimeTenant | None:
        """Return the tenant's inputs + config, or None if unknown."""
        ...


class InMemoryTenantSource:
    def __init__(self) -> None:
        self._tenants: dict[str, RuntimeTenant] = {}

    def add(self, tenant: RuntimeTenant) -> None:
        self._tenants[tenant.tenant_id] = tenant

    def add_from_dto(
        self,
        tenant_id: str,
        name: str,
        dto: dict[str, object] | str,
        minimum_cash: Money,
    ) -> None:
        """Register a tenant from a ``forecast-inputs/1`` DTO (dict or JSON text)."""
        payload = json.loads(dto) if isinstance(dto, str) else dto
        inputs = from_dto(dict(payload))
        self.add(
            RuntimeTenant(
                tenant_id=tenant_id,
                name=name,
                inputs=inputs,
                config=ForecastConfig(minimum_cash=minimum_cash),
            )
        )

    def resolve(self, tenant_id: str) -> RuntimeTenant | None:
        return self._tenants.get(tenant_id)
