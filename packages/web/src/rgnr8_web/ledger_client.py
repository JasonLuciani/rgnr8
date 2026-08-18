"""The web app's client for the RGNR8 ledger service.

The accounting core (double-entry GL, chart of accounts, statements) runs as its
own service — `@rgnr8/ledger-service`. This is how the owner-facing web app talks
to it: a thin, typed client over an injectable HTTP seam, so every screen and
route that shows a client's *books* is testable against a fake transport with no
socket in sight (the same seam pattern the QBO and connector code uses).

Money crosses this boundary as integer **minor-unit strings** and is never parsed
into a float — `Money.from_minor` keeps it exact on the Python side.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LedgerResponse:
    status: int
    body: dict[str, object]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error(self) -> str:
        raw = self.body.get("error")
        return str(raw) if raw else f"ledger service returned {self.status}"


class LedgerTransport(Protocol):
    """The one thing to implement for production: an HTTP round-trip."""

    def request(
        self, method: str, path: str, body: str, headers: Mapping[str, str]
    ) -> LedgerResponse: ...


class UrllibTransport:
    """Production transport — stdlib urllib, no new dependency."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def request(
        self, method: str, path: str, body: str, headers: Mapping[str, str]
    ) -> LedgerResponse:
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=body.encode("utf-8") if body else None,
            headers=dict(headers),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return LedgerResponse(resp.status, _parse(raw))
        except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON body
            raw = exc.read().decode("utf-8")
            return LedgerResponse(exc.code, _parse(raw))
        except urllib.error.URLError as exc:
            return LedgerResponse(503, {"error": f"ledger service unreachable: {exc.reason}"})


def _parse(raw: str) -> dict[str, object]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "ledger service returned invalid JSON"}
    return parsed if isinstance(parsed, dict) else {"error": "unexpected response shape"}


class LedgerUnavailable(Exception):
    """The ledger service could not be reached or refused the request."""


class LedgerClient:
    """Typed calls onto the ledger service, all tenant-scoped."""

    def __init__(self, transport: LedgerTransport, *, token: str = "") -> None:
        self._t = transport
        self._token = token

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._token:
            h["authorization"] = f"Bearer {self._token}"
        return h

    def _call(self, method: str, path: str, payload: object = None) -> LedgerResponse:
        body = json.dumps(payload) if payload is not None else ""
        return self._t.request(method, path, body, self._headers())

    # --- reads ---------------------------------------------------------------

    def health(self) -> bool:
        return self._call("GET", "/health").ok

    def accounts(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/accounts")

    def trial_balance(self, tenant: str, *, to: str = "", frm: str = "") -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/trial-balance{_qs(frm, to)}")

    def statements(self, tenant: str, frm: str, to: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/statements{_qs(frm, to)}")

    def register(self, tenant: str, code: str, *, frm: str = "", to: str = "") -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/accounts/{code}/register{_qs(frm, to)}")

    def entries(self, tenant: str, *, frm: str = "", to: str = "") -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/entries{_qs(frm, to)}")

    # --- writes --------------------------------------------------------------

    def post_entry(
        self,
        tenant: str,
        date: str,
        lines: list[dict[str, object]],
        *,
        memo: str = "",
        idempotency_key: str = "",
        source: str = "manual",
    ) -> LedgerResponse:
        payload: dict[str, object] = {"date": date, "lines": lines, "source": source}
        if memo:
            payload["memo"] = memo
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._call("POST", f"/t/{tenant}/entries", payload)

    def ingest(
        self,
        tenant: str,
        transactions: list[dict[str, object]],
        *,
        source: str = "feed",
        accounts: Mapping[str, str] | None = None,
        rules: list[dict[str, object]] | None = None,
    ) -> LedgerResponse:
        """Push a batch of bank/card/QBO feed transactions into the books. The
        ledger de-duplicates by transaction id, so re-syncing an overlapping
        window is safe."""
        payload: dict[str, object] = {"source": source, "transactions": transactions}
        if accounts:
            payload["accounts"] = dict(accounts)
        if rules:
            payload["rules"] = rules
        return self._call("POST", f"/t/{tenant}/ingest", payload)

    # --- AR / AP -------------------------------------------------------------

    def parties(self, tenant: str, kind: str) -> LedgerResponse:
        """kind: "customers" or "vendors"."""
        return self._call("GET", f"/t/{tenant}/{kind}")

    def create_party(
        self, tenant: str, kind: str, party_id: str, name: str,
        *, email: str = "", terms_days: int | None = None,
    ) -> LedgerResponse:
        payload: dict[str, object] = {"id": party_id, "name": name}
        if email:
            payload["email"] = email
        if terms_days is not None:
            payload["terms_days"] = terms_days
        return self._call("POST", f"/t/{tenant}/{kind}", payload)

    def documents(self, tenant: str, kind: str) -> LedgerResponse:
        """kind: "invoices" or "bills"."""
        return self._call("GET", f"/t/{tenant}/{kind}")

    def document(self, tenant: str, kind: str, doc_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/{kind}/{doc_id}")

    def create_document(
        self, tenant: str, kind: str, doc_id: str, party_id: str, date: str,
        lines: list[dict[str, object]], *, memo: str = "", due_date: str = "",
        tax_rate_ppm: int = 0,
    ) -> LedgerResponse:
        payload: dict[str, object] = {
            "id": doc_id, "party_id": party_id, "date": date, "lines": lines,
        }
        if memo:
            payload["memo"] = memo
        if due_date:
            payload["due_date"] = due_date
        if tax_rate_ppm:
            payload["tax_rate_ppm"] = tax_rate_ppm
        return self._call("POST", f"/t/{tenant}/{kind}", payload)

    def issue_credit(
        self, tenant: str, kind: str, doc_id: str, date: str,
        *, amount_minor: str = "", memo: str = "",
    ) -> LedgerResponse:
        """Reduce what is owed on an open invoice or bill."""
        payload: dict[str, object] = {"date": date}
        if amount_minor:
            payload["amount_minor"] = amount_minor
        if memo:
            payload["memo"] = memo
        return self._call("POST", f"/t/{tenant}/{kind}/{doc_id}/credits", payload)

    def issue_refund(
        self, tenant: str, doc_id: str, date: str,
        *, amount_minor: str = "", memo: str = "", bank_code: str = "",
    ) -> LedgerResponse:
        """Send money back on an invoice that was already collected."""
        payload: dict[str, object] = {"date": date}
        if amount_minor:
            payload["amount_minor"] = amount_minor
        if memo:
            payload["memo"] = memo
        if bank_code:
            payload["bank_code"] = bank_code
        return self._call("POST", f"/t/{tenant}/invoices/{doc_id}/refunds", payload)

    def record_payment(
        self, tenant: str, kind: str, doc_id: str, date: str, amount_minor: str,
        *, memo: str = "",
    ) -> LedgerResponse:
        payload: dict[str, object] = {"date": date, "amount_minor": amount_minor}
        if memo:
            payload["memo"] = memo
        return self._call("POST", f"/t/{tenant}/{kind}/{doc_id}/payments", payload)

    # --- reporting -----------------------------------------------------------

    def general_ledger(
        self, tenant: str, *, frm: str = "", to: str = "", codes: str = ""
    ) -> LedgerResponse:
        parts = []
        if frm:
            parts.append(f"from={urllib.parse.quote(frm)}")
        if to:
            parts.append(f"to={urllib.parse.quote(to)}")
        if codes:
            parts.append(f"codes={urllib.parse.quote(codes)}")
        qs = f"?{'&'.join(parts)}" if parts else ""
        return self._call("GET", f"/t/{tenant}/gl{qs}")

    def budget(self, tenant: str, period: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/budget/{urllib.parse.quote(period)}")

    def save_budget(
        self, tenant: str, period: str, lines: list[dict[str, object]]
    ) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/budget", {"period": period, "lines": lines})

    # --- payroll -------------------------------------------------------------

    def payroll_employees(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/payroll/employees")

    def save_employee(self, tenant: str, employee: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/payroll/employees", dict(employee))

    def payroll_runs(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/payroll/runs")

    def payroll_run(self, tenant: str, run_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/payroll/runs/{urllib.parse.quote(run_id)}")

    def create_payroll_run(self, tenant: str, run: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/payroll/runs", dict(run))

    def post_payroll_run(self, tenant: str, run_id: str) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/payroll/runs/{urllib.parse.quote(run_id)}/post", {}
        )

    def void_payroll_run(self, tenant: str, run_id: str) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/payroll/runs/{urllib.parse.quote(run_id)}/void", {}
        )

    def payroll_liabilities(self, tenant: str, *, code: str = "") -> LedgerResponse:
        qs = f"?code={urllib.parse.quote(code)}" if code else ""
        return self._call("GET", f"/t/{tenant}/payroll/liabilities{qs}")

    def payroll_remit(self, tenant: str, payment: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/payroll/remit", dict(payment))

    # --- the bank feed review inbox ------------------------------------------

    def feed_inbox(
        self, tenant: str, *, account_code: str = "", status: str = ""
    ) -> LedgerResponse:
        parts = []
        if account_code:
            parts.append(f"account_code={urllib.parse.quote(account_code)}")
        if status:
            parts.append(f"status={urllib.parse.quote(status)}")
        qs = f"?{'&'.join(parts)}" if parts else ""
        return self._call("GET", f"/t/{tenant}/feed{qs}")

    def feed_deliver(
        self, tenant: str, account_code: str, transactions: list[dict[str, object]],
        *, source: str = "feed",
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/feed/{account_code}",
            {"source": source, "transactions": transactions},
        )

    def feed_action(
        self, tenant: str, txn_id: str, action: str, payload: Mapping[str, object] | None = None
    ) -> LedgerResponse:
        """action: accept | match | exclude | undo."""
        return self._call(
            "POST", f"/t/{tenant}/feed/txn/{urllib.parse.quote(txn_id)}/{action}",
            dict(payload or {}),
        )

    def feed_bulk_accept(
        self, tenant: str, min_confidence: float, *, account_code: str = ""
    ) -> LedgerResponse:
        payload: dict[str, object] = {"min_confidence": min_confidence}
        if account_code:
            payload["account_code"] = account_code
        return self._call("POST", f"/t/{tenant}/feed/bulk-accept", payload)

    def feed_rules(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/feed-rules")

    def save_feed_rule(self, tenant: str, rule: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/feed-rules", dict(rule))

    def delete_feed_rule(self, tenant: str, rule_id: str) -> LedgerResponse:
        return self._call("DELETE", f"/t/{tenant}/feed-rules/{urllib.parse.quote(rule_id)}")

    # --- bank reconciliation -------------------------------------------------

    def reconcile_view(
        self, tenant: str, code: str, statement_date: str, statement_balance_minor: str
    ) -> LedgerResponse:
        qs = (
            f"?statement_date={urllib.parse.quote(statement_date)}"
            f"&statement_balance_minor={urllib.parse.quote(statement_balance_minor)}"
        )
        return self._call("GET", f"/t/{tenant}/accounts/{code}/reconcile{qs}")

    def reconcile_toggle(
        self, tenant: str, code: str, entry_id: str, cleared: bool,
        statement_date: str, statement_balance_minor: str,
    ) -> LedgerResponse:
        return self._call(
            "POST",
            f"/t/{tenant}/accounts/{code}/reconcile/toggle",
            {
                "entry_id": entry_id,
                "cleared": cleared,
                "statement_date": statement_date,
                "statement_balance_minor": statement_balance_minor,
            },
        )

    def reconcile_finish(
        self, tenant: str, code: str, statement_date: str, statement_balance_minor: str
    ) -> LedgerResponse:
        return self._call(
            "POST",
            f"/t/{tenant}/accounts/{code}/reconcile/finish",
            {
                "statement_date": statement_date,
                "statement_balance_minor": statement_balance_minor,
            },
        )

    def aging(self, tenant: str, side: str, *, as_of: str = "") -> LedgerResponse:
        """side: "ar" or "ap"."""
        qs = f"?as_of={as_of}" if as_of else ""
        return self._call("GET", f"/t/{tenant}/aging/{side}{qs}")

    def create_account(
        self, tenant: str, code: str, name: str, *, subtype: str = "", type_: str = ""
    ) -> LedgerResponse:
        payload: dict[str, object] = {"code": code, "name": name}
        if subtype:
            payload["subtype"] = subtype
        if type_:
            payload["type"] = type_
        return self._call("POST", f"/t/{tenant}/accounts", payload)

    def seed_chart(self, tenant: str, category: str, *, currency: str = "USD") -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/accounts/seed", {"category": category, "currency": currency}
        )

    def go_live(self, tenant: str, request: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/go-live", dict(request))

    def lock_period(self, tenant: str, period: str) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/periods/{period}/lock", {})


def _qs(frm: str, to: str) -> str:
    parts = []
    if frm:
        parts.append(f"from={frm}")
    if to:
        parts.append(f"to={to}")
    return f"?{'&'.join(parts)}" if parts else ""
