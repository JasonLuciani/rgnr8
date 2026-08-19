"""Payroll (Gusto) — pull processed pay runs into the payroll journal.

The ledger already posts a full payroll journal (wages, withholdings as a
liability, employer taxes); Gusto is where the run is actually processed and
filed — the same split Xero uses. `PayrollProvider` fetches processed runs;
`GustoPayrollProvider` is the shape-correct adapter (needs a token + real client);
`to_payroll_run` maps a run into the payroll-journal payload, idempotent by the
Gusto payroll id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .http import HttpClient


@dataclass(frozen=True)
class PayrollRun:
    external_id: str
    pay_date: str  # YYYY-MM-DD
    gross_minor: int
    employee_taxes_minor: int
    employer_taxes_minor: int
    net_minor: int


class PayrollProvider(Protocol):
    def fetch_payrolls(self, company_id: str, since: str) -> list[PayrollRun]: ...


class GustoPayrollProvider:
    """Shape-correct Gusto payrolls adapter. Needs an access token + real client."""

    def __init__(
        self, http: HttpClient, token: str, *, base_url: str = "https://api.gusto.com"
    ) -> None:
        self._http = http
        self._token = token
        self._base = base_url.rstrip("/")

    def fetch_payrolls(self, company_id: str, since: str) -> list[PayrollRun]:
        resp = self._http.get_json(
            f"{self._base}/v1/companies/{company_id}/payrolls?processed=true&start_date={since}",
            {"Authorization": f"Bearer {self._token}"},
        )
        runs: list[PayrollRun] = []
        for raw in _as_list(resp.get("payrolls")):
            totals = raw.get("totals", {})
            totals = totals if isinstance(totals, dict) else {}
            runs.append(
                PayrollRun(
                    external_id=str(raw.get("payroll_uuid", "")),
                    pay_date=str(raw.get("check_date", "")),
                    gross_minor=_to_minor(totals.get("gross_pay")),
                    employee_taxes_minor=_to_minor(totals.get("employee_taxes")),
                    employer_taxes_minor=_to_minor(totals.get("employer_taxes")),
                    net_minor=_to_minor(totals.get("net_pay")),
                )
            )
        return runs


class FakePayrollProvider:
    def __init__(self, runs: list[PayrollRun]) -> None:
        self._runs = runs

    def fetch_payrolls(self, company_id: str, since: str) -> list[PayrollRun]:
        return [r for r in self._runs if r.pay_date >= since]


def to_payroll_run(run: PayrollRun) -> dict[str, object]:
    """Map a processed run into the ledger's payroll-journal payload."""
    return {
        "idempotency_key": f"gusto:{run.external_id}",
        "pay_date": run.pay_date,
        "gross_minor": str(run.gross_minor),
        "employee_taxes_minor": str(run.employee_taxes_minor),
        "employer_taxes_minor": str(run.employer_taxes_minor),
        "net_minor": str(run.net_minor),
    }


def _as_list(v: object) -> list[dict[str, object]]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _to_minor(v: object) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return round(float(v) * 100)
    if isinstance(v, str):
        try:
            whole, _, frac = v.partition(".")
            frac = (frac + "00")[:2]
            return int(whole) * 100 + (int(frac) if frac else 0)
        except ValueError:
            return 0
    return 0


__all__ = [
    "PayrollRun",
    "PayrollProvider",
    "GustoPayrollProvider",
    "FakePayrollProvider",
    "to_payroll_run",
]
