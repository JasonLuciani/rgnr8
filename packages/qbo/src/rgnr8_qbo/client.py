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

from .oauth import HttpClient, JSON_CONTENT_TYPE, QboOAuthError

# The Accounting API minor version we pin (stable field shapes).
MINOR_VERSION = "65"


class QboApiError(QboOAuthError):
    """A QuickBooks Accounting API request failed."""


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
class QboCompany:
    name: str
    country: str = ""


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
            raise QboApiError(f"query returned {resp.status}: {resp.body[:300]}")
        try:
            data = json.loads(resp.body)
        except json.JSONDecodeError as exc:
            raise QboApiError("query returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise QboApiError("query returned a non-object payload")
        qr = data.get("QueryResponse")
        return qr if isinstance(qr, dict) else {}

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
        qr = self.query("select * from Account where AccountType = 'Bank' and Active = true")
        out: list[QboAccount] = []
        for r in self._rows(qr, "Account"):
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
        qr = self.query("select * from Invoice where Balance > '0'")
        out: list[QboInvoice] = []
        for r in self._rows(qr, "Invoice"):
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
        qr = self.query("select * from Bill where Balance > '0'")
        out: list[QboBill] = []
        for r in self._rows(qr, "Bill"):
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
        return QboCompany(name=self._s(r, "CompanyName"), country=country)
