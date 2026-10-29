"""A thin QuickBooks Online Accounting API client — read the data RGNR8 needs.

Once a tenant is connected (:mod:`rgnr8_qbo.service`), this pulls the figures that
drive the cash picture straight from QuickBooks: bank balances (cash on hand),
open invoices (money coming in), and open bills (money going out), plus the
company name. It speaks the Accounting **query** endpoint
(``GET /v3/company/{realmId}/query?query=<SQL>``) over the injected
:class:`~rgnr8_qbo.oauth.HttpClient`, so it's deterministic and testable with a
fake — the same seam the OAuth flow uses.

Money in QBO is decimal dollars; this client returns amounts as **strings**
(exactly as QBO sends them) so the caller converts with its own money type — this
package stays dependency-free.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Mapping

from .oauth import JSON_CONTENT_TYPE, HttpClient, QboOAuthError

# The Accounting API minor version we pin (stable field shapes).
MINOR_VERSION = "65"


def _to_minor(v: object) -> int:
    """A QBO decimal-dollar string ('1,201.00', '', '-50', '$3.50') → integer
    cents. Empty/garbage → 0. Half-cent inputs are truncated at two places (QBO
    reports are already 2dp), so no rounding surprises."""
    s = str(v if v is not None else "").strip().replace(",", "").replace("$", "")
    if not s:
        return 0
    neg = s.startswith("-")
    s = s.lstrip("+-")
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    try:
        cents = int(whole or "0") * 100 + int(frac or "0")
    except ValueError:
        return 0
    return -cents if neg else cents


class QboApiError(QboOAuthError):
    """A QuickBooks Accounting API request failed.

    `status` and `body` are Intuit's answer as received, for code that needs
    to tell an expired connection from a rate limit; the message is for logs.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class QboAccount:
    id: str
    name: str
    account_type: str
    current_balance: str  # decimal dollars, as QBO sends it


@dataclass(frozen=True, slots=True)
class QboInvoice:
    id: str
    doc_number: str
    customer: str
    balance: str
    txn_date: str
    due_date: str


@dataclass(frozen=True, slots=True)
class QboBill:
    id: str
    vendor: str
    balance: str
    txn_date: str
    due_date: str


@dataclass(frozen=True, slots=True)
class QboBankTxn:
    """One bank/card movement as QuickBooks records it.

    ``amount`` is a **signed** decimal-dollar string: negative for money out
    (a Purchase), positive for money in (a Deposit). QuickBooks stores both as
    positive totals on different entities, so the sign is applied here — once,
    at the boundary — rather than left for every caller to get right.
    """

    id: str
    kind: str          # "Purchase" | "Deposit"
    txn_date: str
    amount: str        # signed decimal dollars
    description: str
    counterparty: str
    account: str       # the QBO bank account the movement is on


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")


def fiscal_month(name: str) -> int:
    """QuickBooks' FiscalYearStartMonth ("July") as a month number; January when
    it is missing or not a month, which is what QuickBooks assumes too."""
    text = name.strip().lower()
    for i, month in enumerate(_MONTHS, start=1):
        if text and (text == month or (len(text) >= 3 and month.startswith(text))):
            return i
    return 1


@dataclass(frozen=True, slots=True)
class QboCompany:
    name: str
    country: str = ""
    # The month the company's fiscal year starts (1 = January). QuickBooks
    # names it ("July"); a company that never set one is on the calendar year.
    fiscal_year_start_month: int = 1


@dataclass(frozen=True, slots=True)
class QboApiClient:
    """Reads one connected company. ``api_base`` is the environment's host,
    ``realm_id`` the company id, ``access_token`` a *currently valid* bearer token
    (refresh via the service before building this)."""

    http: HttpClient
    api_base: str
    realm_id: str
    access_token: str
    minor_version: str = MINOR_VERSION

    def _headers(self) -> Mapping[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": JSON_CONTENT_TYPE,
        }

    def query(self, sql: str) -> dict[str, object]:
        """Run an Accounting API query; return the ``QueryResponse`` object."""
        q = urllib.parse.urlencode({"query": sql, "minorversion": self.minor_version})
        url = f"{self.api_base}/v3/company/{self.realm_id}/query?{q}"
        resp = self.http.get(url, self._headers())
        if resp.status < 200 or resp.status >= 300:
            raise QboApiError(f"query returned {resp.status}: {resp.body[:300]}",
                              status=resp.status, body=resp.body)
        try:
            data = json.loads(resp.body)
        except json.JSONDecodeError as exc:
            raise QboApiError("query returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise QboApiError("query returned a non-object payload")
        qr = data.get("QueryResponse")
        return qr if isinstance(qr, dict) else {}

    # QuickBooks answers a query with at most 100 rows unless asked for more,
    # and with at most 1,000 however it is asked. Every list read here used to
    # be a single bare query, so a company with more than 100 accounts lost the
    # rest without a word — the first real go-live dropped thirteen accounts
    # carrying $206,556.77 of revenue, $68,000 of salaries and two bank accounts,
    # and Opening Balance Equity quietly absorbed the difference.
    PAGE_SIZE = 1000
    MAX_PAGES = 200  # 200,000 rows: a guard against a server that never stops

    def query_all(self, sql: str, entity: str) -> list[dict[str, object]]:
        """Every row a query matches, read a page at a time."""
        out: list[dict[str, object]] = []
        start = 1
        for _ in range(self.MAX_PAGES):
            page = self._rows(
                self.query(f"{sql} STARTPOSITION {start} MAXRESULTS {self.PAGE_SIZE}"), entity)
            out.extend(page)
            if len(page) < self.PAGE_SIZE:
                return out
            start += self.PAGE_SIZE
        raise QboApiError(f"{entity}: more than {self.PAGE_SIZE * self.MAX_PAGES} rows; refusing "
                          "to guess where the list ends")

    @staticmethod
    def _rows(qr: Mapping[str, object], entity: str) -> list[dict[str, object]]:
        rows = qr.get(entity)
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    @staticmethod
    def _s(row: Mapping[str, object], key: str, default: str = "") -> str:
        v = row.get(key, default)
        if isinstance(v, (int, float)):
            return str(v)
        return v if isinstance(v, str) else default

    @staticmethod
    def _ref_name(row: Mapping[str, object], key: str) -> str:
        ref = row.get(key)
        if isinstance(ref, dict):
            name = ref.get("name")
            if isinstance(name, str):
                return name
            value = ref.get("value")
            if isinstance(value, str):
                return value
        return ""

    def bank_accounts(self) -> list[QboAccount]:
        """Active bank accounts + their current balances (cash on hand)."""
        out: list[QboAccount] = []
        for r in self.query_all(
                "select * from Account where AccountType = 'Bank' and Active = true", "Account"):
            out.append(
                QboAccount(
                    id=self._s(r, "Id"),
                    name=self._s(r, "Name"),
                    account_type=self._s(r, "AccountType"),
                    current_balance=self._s(r, "CurrentBalance", "0"),
                )
            )
        return out

    def open_invoices(self) -> list[QboInvoice]:
        """Invoices with an outstanding balance (accounts receivable)."""
        out: list[QboInvoice] = []
        for r in self.query_all("select * from Invoice where Balance > '0'", "Invoice"):
            out.append(
                QboInvoice(
                    id=self._s(r, "Id"),
                    doc_number=self._s(r, "DocNumber"),
                    customer=self._ref_name(r, "CustomerRef"),
                    balance=self._s(r, "Balance", "0"),
                    txn_date=self._s(r, "TxnDate"),
                    due_date=self._s(r, "DueDate"),
                )
            )
        return out

    def open_bills(self) -> list[QboBill]:
        """Bills with an outstanding balance (accounts payable)."""
        out: list[QboBill] = []
        for r in self.query_all("select * from Bill where Balance > '0'", "Bill"):
            out.append(
                QboBill(
                    id=self._s(r, "Id"),
                    vendor=self._ref_name(r, "VendorRef"),
                    balance=self._s(r, "Balance", "0"),
                    txn_date=self._s(r, "TxnDate"),
                    due_date=self._s(r, "DueDate"),
                )
            )
        return out

    def bank_transactions(self, *, since: str = "") -> list[QboBankTxn]:
        """Purchases (money out) and Deposits (money in), newest activity first.

        These are the raw movements a review inbox is built from — not summaries
        or balances. ``since`` narrows to transactions on or after an ISO date,
        which is what an incremental sync wants; QuickBooks has no cursor here,
        so re-syncing an overlapping window is normal and the inbox is
        idempotent by transaction id for exactly that reason.
        """
        where = f" where TxnDate >= '{since}'" if since else ""
        out: list[QboBankTxn] = []

        for r in self.query_all(f"select * from Purchase{where}", "Purchase"):
            total = self._s(r, "TotalAmt", "0")
            out.append(
                QboBankTxn(
                    id=f"qbo-purchase-{self._s(r, 'Id')}",
                    kind="Purchase",
                    txn_date=self._s(r, "TxnDate"),
                    # QuickBooks stores a purchase as a positive total; from the
                    # bank account's point of view it is money leaving.
                    amount=f"-{total}" if not total.startswith("-") else total,
                    description=self._s(r, "PrivateNote") or self._s(r, "DocNumber"),
                    counterparty=self._ref_name(r, "EntityRef"),
                    account=self._ref_name(r, "AccountRef"),
                )
            )

        for r in self.query_all(f"select * from Deposit{where}", "Deposit"):
            out.append(
                QboBankTxn(
                    id=f"qbo-deposit-{self._s(r, 'Id')}",
                    kind="Deposit",
                    txn_date=self._s(r, "TxnDate"),
                    amount=self._s(r, "TotalAmt", "0"),
                    description=self._s(r, "PrivateNote") or self._s(r, "DocNumber"),
                    counterparty=self._ref_name(r, "EntityRef"),
                    account=self._ref_name(r, "DepositToAccountRef"),
                )
            )

        return sorted(out, key=lambda t: (t.txn_date, t.id))

    def chart_of_accounts(self, *, include_inactive: bool = False) -> list[dict[str, str]]:
        """Every account: its QBO id, number (`code`), name, and QBO type.
        This is the chart brought across at go-live, and the join table that turns
        a TrialBalance report's account rows into coded, typed source accounts.
        Accounts with no number fall back to the QBO id as the code so a migration
        never drops an account for lacking a human number.

        `include_inactive` is for that join: QuickBooks lets an account be made
        inactive while it still carries history, and a trial balance row for
        one has to find its account like any other."""
        where = "where Active IN (true, false)" if include_inactive else "where Active = true"
        out: list[dict[str, str]] = []
        for r in self.query_all(f"select * from Account {where}", "Account"):
            acct_id = self._s(r, "Id")
            out.append({
                "id": acct_id,
                "code": self._s(r, "AcctNum") or acct_id,
                # The account number as QuickBooks holds it, "" when the account
                # has none and `code` is QuickBooks' internal id instead.
                "number": self._s(r, "AcctNum"),
                "name": self._s(r, "Name"),
                "account_type": self._s(r, "AccountType"),
            })
        return out

    def report(self, name: str, params: "Mapping[str, str] | None" = None) -> dict[str, object]:
        """Fetch a QuickBooks report (e.g. 'TrialBalance') from the Reports API and
        return its JSON object. Reports are a separate endpoint from `query`."""
        q = dict(params or {})
        q["minorversion"] = self.minor_version
        url = f"{self.api_base}/v3/company/{self.realm_id}/reports/{name}?{urllib.parse.urlencode(q)}"
        resp = self.http.get(url, self._headers())
        if resp.status < 200 or resp.status >= 300:
            raise QboApiError(f"report {name} returned {resp.status}: {resp.body[:300]}",
                              status=resp.status, body=resp.body)
        try:
            data = json.loads(resp.body)
        except json.JSONDecodeError as exc:
            raise QboApiError(f"report {name} returned invalid JSON") from exc
        return data if isinstance(data, dict) else {}

    @classmethod
    def _report_coldata(cls, container: object) -> list[list[dict[str, object]]]:
        """Flatten a QBO report's (possibly nested) Rows into a list of ColData
        arrays — one per data row. Section groupings nest under Rows/Row; account
        rows carry ColData. Summary rows use a Summary key and are ignored here."""
        rows = container.get("Row") if isinstance(container, dict) else None
        out: list[list[dict[str, object]]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            cols = row.get("ColData")
            if isinstance(cols, list):
                out.append([c for c in cols if isinstance(c, dict)])
            nested = row.get("Rows")
            if isinstance(nested, dict):
                out.extend(cls._report_coldata(nested))
        return out

    def trial_balance(self, *, as_of: str = "") -> list[dict[str, object]]:
        """The company's trial balance as go-live `source_accounts` input rows —
        {code, name, account_type, numbered, debit_minor, credit_minor} — ready for
        `qbo_trial_balance_to_source_accounts`. Debit/credit come from the
        TrialBalance report (already balanced by QuickBooks); the code and QBO
        account type come from the chart of accounts, joined by QBO account id.
        `as_of` is an ISO date (default: today in the company's books).

        Rows with no account id — section headers and totals — are skipped. A row
        that *has* an account id the chart doesn't know is an error, never a
        skip: that is exactly how accounts went missing before, silently, with
        the trial balance still appearing to balance. And the rows read are
        checked against the report's own TOTAL, so anything that goes missing
        for any other reason is caught here rather than plugged downstream."""
        by_id = {a["id"]: a for a in self.chart_of_accounts(include_inactive=True)}
        params = {"start_date": as_of, "end_date": as_of} if as_of else {"date_macro": "Today"}
        report = self.report("TrialBalance", params)
        out: list[dict[str, object]] = []
        unknown: list[str] = []
        for cols in self._report_coldata(report.get("Rows")):
            if len(cols) < 3:
                continue
            acct_id = str(cols[0].get("id", "")).strip()
            if not acct_id:
                continue  # a section header or a total: no account behind it
            acct = by_id.get(acct_id)
            if acct is None:
                unknown.append(f"{cols[0].get('value', '')} (id {acct_id})")
                continue
            out.append({
                "code": acct["code"],
                "name": acct["name"] or str(cols[0].get("value", "")),
                "account_type": acct["account_type"],
                # False when `code` is QuickBooks' internal id, not a number the
                # company gave the account: screens show such accounts by name.
                "numbered": bool(acct["number"]),
                "debit_minor": _to_minor(cols[1].get("value")),
                "credit_minor": _to_minor(cols[2].get("value")),
            })
        if unknown:
            raise QboApiError(
                "the trial balance has rows for accounts the chart of accounts did not "
                f"return: {', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''}")
        total = self._report_total(report.get("Rows"))
        if total is not None:
            got = (sum(int(str(r["debit_minor"])) for r in out),
                   sum(int(str(r["credit_minor"])) for r in out))
            if got != total:
                raise QboApiError(
                    f"read {got[0] / 100:,.2f} of debits and {got[1] / 100:,.2f} of credits, but "
                    f"QuickBooks' trial balance totals {total[0] / 100:,.2f} / "
                    f"{total[1] / 100:,.2f}; some rows did not come across")
        return out

    @classmethod
    def _report_total(cls, container: object) -> "tuple[int, int] | None":
        """The (debit, credit) of the report's TOTAL row, wherever it sits."""
        rows = container.get("Row") if isinstance(container, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            summary = row.get("Summary")
            cols = summary.get("ColData") if isinstance(summary, dict) else None
            if (isinstance(cols, list) and len(cols) >= 3 and isinstance(cols[0], dict)
                    and str(cols[0].get("value", "")).strip().upper() == "TOTAL"):
                return (_to_minor(cols[1].get("value") if isinstance(cols[1], dict) else None),
                        _to_minor(cols[2].get("value") if isinstance(cols[2], dict) else None))
            nested = cls._report_total(row.get("Rows"))
            if nested is not None:
                return nested
        return None

    # The Profit and Loss sections that are money going out.
    _SPEND_GROUPS = frozenset({"COGS", "Expenses", "OtherExpenses"})

    def expense_lines(self, *, start: str, end: str) -> list[tuple[str, int]]:
        """What the company spent from `start` to `end` (ISO dates), per account:
        (account name, minor units), from the ProfitAndLoss report's cost of
        goods, expense and other-expense sections. Accounts with nothing spent
        are left out; refunds that net an account below zero are kept as such."""
        report = self.report("ProfitAndLoss", {"start_date": start, "end_date": end,
                                                "accounting_method": "Accrual"})
        rows = report.get("Rows")
        top = rows.get("Row") if isinstance(rows, dict) else None
        out: list[tuple[str, int]] = []
        for section in top if isinstance(top, list) else []:
            if not isinstance(section, dict) or section.get("group") not in self._SPEND_GROUPS:
                continue
            for cols in self._report_coldata(section.get("Rows")):
                if len(cols) < 2:
                    continue
                minor = _to_minor(cols[-1].get("value"))
                if minor:
                    out.append((str(cols[0].get("value", "")).strip() or "Expense", minor))
        return out

    def company_info(self) -> QboCompany:
        """The connected company's name (and country)."""
        qr = self.query("select * from CompanyInfo")
        rows = self._rows(qr, "CompanyInfo")
        if not rows:
            return QboCompany(name="")
        r = rows[0]
        country = ""
        addr = r.get("CompanyAddr")
        if isinstance(addr, dict):
            country = self._s(addr, "Country")
        return QboCompany(name=self._s(r, "CompanyName"), country=country,
                          fiscal_year_start_month=fiscal_month(self._s(r, "FiscalYearStartMonth")))
