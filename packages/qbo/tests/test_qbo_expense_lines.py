"""What a company spent, per account, from QuickBooks' Profit and Loss report:
the source of the cost baseline that lets the cash outlook see money leaving."""

from __future__ import annotations

import json
from typing import Any

from rgnr8_qbo import QboApiClient


class FakeHttp:
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        self.urls: list[str] = []

    def get(self, url: str, headers: Any) -> Any:
        self.urls.append(url)

        class R:
            status = 200
            body = json.dumps(self.report)
        return R()


def _section(group: str, rows: list[tuple[str, str]], nested: bool = False) -> dict[str, Any]:
    data = [{"ColData": [{"value": n, "id": str(i)}, {"value": v}], "type": "Data"}
            for i, (n, v) in enumerate(rows, start=1)]
    if nested:  # QuickBooks nests sub-accounts as a section inside the section
        data = [{"Header": {"ColData": [{"value": "Payroll"}, {"value": ""}]},
                 "Rows": {"Row": data}, "type": "Section"}]
    return {"Header": {"ColData": [{"value": group}, {"value": ""}]}, "Rows": {"Row": data},
            "Summary": {"ColData": [{"value": f"Total {group}"}, {"value": "999.00"}]},
            "type": "Section", "group": group}


REPORT = {"Rows": {"Row": [
    _section("Income", [("Consulting", "25,000.00")]),
    _section("COGS", [("Subcontractors", "3,000.00")]),
    _section("Expenses", [("Rent", "6,000.00"), ("Software", "450.50"), ("Meals", "0.00")]),
    _section("Expenses", [("Wages", "12,000.00")], nested=True),
    _section("OtherExpenses", [("Interest", "120.00")]),
    {"Summary": {"ColData": [{"value": "Net Income"}, {"value": "3,429.50"}]}, "group": "NetIncome"},
]}}


def test_expense_lines_reads_every_spend_section_and_nothing_else() -> None:
    http = FakeHttp(REPORT)
    client = QboApiClient(http=http, api_base="https://sandbox", realm_id="R1", access_token="tok")
    lines = client.expense_lines(start="2026-07-01", end="2026-09-30")
    assert lines == [("Subcontractors", 300000), ("Rent", 600000), ("Software", 45050),
                     ("Wages", 1200000), ("Interest", 12000)]
    # Income, the section totals and zero rows never count as spending.
    assert "/reports/ProfitAndLoss" in http.urls[0]
    assert "start_date=2026-07-01" in http.urls[0] and "end_date=2026-09-30" in http.urls[0]
