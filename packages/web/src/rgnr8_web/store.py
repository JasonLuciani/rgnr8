"""Persistence seam for the web write path.

The owner surface is actionable: an owner can change assumptions (minimum-cash
floor, a customer's payment timing) and record decisions. Those are *mutable
owner state*, distinct from the tenant *definition* (the forecast inputs, which
arrive from the ``forecast-inputs/1`` DTO the TS side emits, plus provisioning
like the display name, token, and configured floor).

This module separates the two and puts the mutable state behind a small
``TenantStore`` protocol — the same seam pattern used by the kernel's
``LedgerStore`` and the connectors' ``HttpClient``. ``InMemoryTenantStore`` is
the default; ``JsonFileTenantStore`` makes the write path durable across process
restarts with no database dependency; a production deployment swaps in a
Postgres-backed implementation of the same three methods.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rgnr8_forecast import ForecastConfig, ForecastInputs, Money
from rgnr8_forecast.io import from_dto, money_from_dto, money_to_dto


@dataclass(slots=True)
class TenantState:
    """The mutable, owner-authored state for one tenant — what the write path
    changes and what must survive a restart. The minimum-cash override is held
    as the contract's money DTO (``{"minor","currency"}``) so it round-trips
    through JSON without losing the currency."""

    min_cash_override_dto: dict[str, object] | None = None
    payment_overrides: dict[str, int] = field(default_factory=dict)
    decisions: list[dict[str, object]] = field(default_factory=list)

    @property
    def min_cash_override(self) -> Money | None:
        if self.min_cash_override_dto is None:
            return None
        return money_from_dto(self.min_cash_override_dto)

    def set_min_cash(self, amount: Money) -> None:
        self.min_cash_override_dto = money_to_dto(amount)

    def to_dict(self) -> dict[str, object]:
        return {
            "min_cash_override": self.min_cash_override_dto,
            "payment_overrides": dict(self.payment_overrides),
            "decisions": list(self.decisions),
        }

    @staticmethod
    def from_dict(d: dict[str, object]) -> TenantState:
        raw_min = d.get("min_cash_override")
        overrides_obj = d.get("payment_overrides")
        decisions_obj = d.get("decisions")
        payment_overrides: dict[str, int] = {}
        if isinstance(overrides_obj, dict):
            payment_overrides = {str(k): int(v) for k, v in overrides_obj.items()}
        decisions: list[dict[str, object]] = []
        if isinstance(decisions_obj, list):
            decisions = [dict(x) for x in decisions_obj if isinstance(x, dict)]
        return TenantState(
            min_cash_override_dto=dict(raw_min) if isinstance(raw_min, dict) else None,
            payment_overrides=payment_overrides,
            decisions=decisions,
        )


class TenantStore(Protocol):
    """Persistence for mutable owner state. Three methods; swap the backend
    without touching the web app."""

    def load(self, tenant_id: str) -> TenantState:
        """Return the tenant's saved state, or a fresh empty state if none."""
        ...

    def save(self, tenant_id: str, state: TenantState) -> None:
        """Durably persist the tenant's state."""
        ...


class InMemoryTenantStore:
    """Default store — state lives in a dict, lost when the process exits."""

    def __init__(self) -> None:
        self._states: dict[str, TenantState] = {}

    def load(self, tenant_id: str) -> TenantState:
        return self._states.get(tenant_id, TenantState())

    def save(self, tenant_id: str, state: TenantState) -> None:
        self._states[tenant_id] = state


class JsonFileTenantStore:
    """Durable store — one JSON file holds every tenant's state. Written
    atomically (temp file + replace) so a crash mid-write can't corrupt it.
    Proves the write path survives a restart without a database."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _read_all(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        text = self._path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = json.loads(text)
        return {str(k): dict(v) for k, v in data.items()}

    def load(self, tenant_id: str) -> TenantState:
        raw = self._read_all().get(tenant_id)
        return TenantState.from_dict(raw) if raw is not None else TenantState()

    def save(self, tenant_id: str, state: TenantState) -> None:
        all_states = self._read_all()
        all_states[tenant_id] = state.to_dict()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(all_states, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)


@dataclass(frozen=True, slots=True)
class TenantDef:
    """A tenant *definition* — the immutable provisioning + forecast inputs.
    Inputs come from a ``forecast-inputs/1`` DTO (dict or JSON text), exactly as
    the TS ``@rgnr8/forecast-inputs`` side emits it."""

    tenant_id: str
    name: str
    token: str
    inputs: ForecastInputs
    config: ForecastConfig

    @staticmethod
    def from_dto(
        tenant_id: str,
        name: str,
        token: str,
        dto: dict[str, object] | str,
        minimum_cash: Money,
    ) -> TenantDef:
        payload = json.loads(dto) if isinstance(dto, str) else dto
        inputs = from_dto(dict(payload))
        return TenantDef(
            tenant_id=tenant_id,
            name=name,
            token=token,
            inputs=inputs,
            config=ForecastConfig(minimum_cash=minimum_cash),
        )
