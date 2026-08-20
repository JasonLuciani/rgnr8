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

import hashlib
import hmac
import http.client
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

# Matches a tenant-scoped path "/t/<tenant>/..." so the client can present the
# per-tenant auth token the ledger service expects.
_TENANT_IN_PATH = re.compile(r"^/t/([^/]+)(?:/|$)")


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


class PooledHttpTransport:
    """Production transport that reuses keep-alive connections.

    ``UrllibTransport`` opens (and tears down) a fresh TCP — and, over TLS, a
    fresh handshake — on every call, which is the single biggest avoidable cost
    on a chatty screen that makes a dozen ledger reads to render. This keeps one
    persistent HTTP/1.1 keep-alive connection **per thread** (WSGI servers are
    multi-threaded, and ``http.client`` connections are not thread-safe, so a
    thread-local is both the correct and the fast answer) and reuses it. On any
    connection-level error it transparently drops the socket and retries once on
    a fresh one, so a server that closed an idle keep-alive never surfaces as an
    error to the caller.
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        self._scheme = parsed.scheme or "http"
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or (443 if self._scheme == "https" else 80)
        self._prefix = parsed.path.rstrip("/")
        self._timeout = timeout
        self._local = threading.local()

    def _connection(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._scheme == "https":
                conn = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
            else:
                conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
            self._local.conn = conn
        return conn

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    def request(
        self, method: str, path: str, body: str, headers: Mapping[str, str]
    ) -> LedgerResponse:
        url = f"{self._prefix}{path}"
        payload = body.encode("utf-8") if body else None
        # One transparent retry: a keep-alive the server has since closed raises
        # on the reused socket, and the fix is simply a fresh connection.
        for attempt in (1, 2):
            conn = self._connection()
            try:
                conn.request(method, url, body=payload, headers=dict(headers))
                resp = conn.getresponse()
                raw = resp.read().decode("utf-8")
                return LedgerResponse(resp.status, _parse(raw))
            except (http.client.HTTPException, ConnectionError, OSError) as exc:
                self._drop()
                if attempt == 2:
                    return LedgerResponse(503, {"error": f"ledger service unreachable: {exc}"})
        return LedgerResponse(503, {"error": "ledger service unreachable"})  # unreachable


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

    def _headers(self, path: str) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._token:
            m = _TENANT_IN_PATH.match(path)
            if m is not None:
                # Present the per-tenant token, not the raw shared secret: it is
                # the HMAC of this tenant id under the secret, so a credential for
                # one tenant can't act on another (matches the ledger service).
                tenant = m.group(1)
                derived = hmac.new(
                    self._token.encode("utf-8"), tenant.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                h["authorization"] = f"Bearer {derived}"
            else:
                # Non-tenant paths (/health, /ready) are public on the service;
                # send the raw secret only for symmetry.
                h["authorization"] = f"Bearer {self._token}"
        return h

    def _call(self, method: str, path: str, payload: object = None) -> LedgerResponse:
        body = json.dumps(payload) if payload is not None else ""
        return self._t.request(method, path, body, self._headers(path))

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
        is_1099: bool = False, tax_id: str = "",
    ) -> LedgerResponse:
        payload: dict[str, object] = {"id": party_id, "name": name}
        if email:
            payload["email"] = email
        if terms_days is not None:
            payload["terms_days"] = terms_days
        if is_1099:
            payload["is_1099"] = True
        if tax_id:
            payload["tax_id"] = tax_id
        return self._call("POST", f"/t/{tenant}/{kind}", payload)

    def ten99(self, tenant: str, year: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/1099/{urllib.parse.quote(year)}")

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

    # --- recurring transactions ----------------------------------------------

    def recurring(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/recurring")

    def save_recurring(self, tenant: str, template: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/recurring", dict(template))

    def delete_recurring(self, tenant: str, template_id: str) -> LedgerResponse:
        return self._call(
            "DELETE", f"/t/{tenant}/recurring/{urllib.parse.quote(template_id)}"
        )

    def recurring_due(self, tenant: str, as_of: str) -> LedgerResponse:
        return self._call(
            "GET", f"/t/{tenant}/recurring/due?as_of={urllib.parse.quote(as_of)}"
        )

    def run_recurring(
        self, tenant: str, as_of: str, *, template_id: str = ""
    ) -> LedgerResponse:
        payload: dict[str, object] = {"as_of": as_of}
        if template_id:
            payload["id"] = template_id
        return self._call("POST", f"/t/{tenant}/recurring/run", payload)

    # --- attachments ---------------------------------------------------------

    def attachments(
        self, tenant: str, *, subject_kind: str = "", subject_id: str = ""
    ) -> LedgerResponse:
        parts = []
        if subject_kind:
            parts.append(f"subject_kind={urllib.parse.quote(subject_kind)}")
        if subject_id:
            parts.append(f"subject_id={urllib.parse.quote(subject_id)}")
        qs = f"?{'&'.join(parts)}" if parts else ""
        return self._call("GET", f"/t/{tenant}/attachments{qs}")

    def attachment_counts(self, tenant: str, subject_kind: str) -> LedgerResponse:
        return self._call(
            "GET", f"/t/{tenant}/attachments/counts"
                   f"?subject_kind={urllib.parse.quote(subject_kind)}"
        )

    def save_attachment(
        self, tenant: str, subject_kind: str, subject_id: str,
        filename: str, content_type: str, content: bytes, *, note: str = "",
    ) -> LedgerResponse:
        import base64

        return self._call("POST", f"/t/{tenant}/attachments", {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "filename": filename,
            "content_type": content_type,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "note": note,
        })

    def attachment_content(self, tenant: str, attachment_id: str) -> LedgerResponse:
        return self._call(
            "GET", f"/t/{tenant}/attachments/{urllib.parse.quote(attachment_id)}/content"
        )

    def delete_attachment(self, tenant: str, attachment_id: str) -> LedgerResponse:
        return self._call(
            "DELETE", f"/t/{tenant}/attachments/{urllib.parse.quote(attachment_id)}"
        )

    # --- reporting dimensions (classes, locations) ---------------------------

    def dimensions(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/dimensions")

    def save_dimension(self, tenant: str, dimension: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/dimensions", dict(dimension))

    def delete_dimension(self, tenant: str, key: str) -> LedgerResponse:
        return self._call("DELETE", f"/t/{tenant}/dimensions/{urllib.parse.quote(key)}")

    def dimension_report(
        self, tenant: str, key: str, *, frm: str = "", to: str = ""
    ) -> LedgerResponse:
        parts = []
        if frm:
            parts.append(f"from={urllib.parse.quote(frm)}")
        if to:
            parts.append(f"to={urllib.parse.quote(to)}")
        qs = f"?{'&'.join(parts)}" if parts else ""
        return self._call(
            "GET", f"/t/{tenant}/dimensions/{urllib.parse.quote(key)}/report{qs}"
        )

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

    def reconcile_import(
        self, tenant: str, code: str, statement_text: str,
        statement_date: str, statement_balance_minor: str,
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/accounts/{code}/reconcile/import",
            {
                "statement_text": statement_text,
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

    # --- the project layer ---------------------------------------------------
    #
    # Jobs, estimates, orders, work orders, purchasing, billing, stock and the
    # pipeline. Every one of these is a thin pass-through: the rules live in the
    # service, and a second copy of them here is a second thing to get wrong.

    def cost_codes(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/cost-codes")

    def save_cost_code(self, tenant: str, code: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/cost-codes", dict(code))

    def seed_cost_codes(self, tenant: str) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/cost-codes/seed", {})

    def jobs(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/jobs")

    def job(self, tenant: str, job_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}")

    def save_job(self, tenant: str, job: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/jobs", dict(job))

    def save_job_budget(
        self, tenant: str, job_id: str, lines: list[dict[str, object]]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/budget", {"lines": lines}
        )

    def job_cost(self, tenant: str, job_id: str, *, through: str = "") -> LedgerResponse:
        q = f"?through={urllib.parse.quote(through)}" if through else ""
        return self._call("GET", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/cost{q}")

    def job_work_orders(self, tenant: str, job_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/work-orders")

    def job_billing(self, tenant: str, job_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/billing")

    def save_schedule(
        self, tenant: str, job_id: str, lines: list[dict[str, object]]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/schedule", {"lines": lines}
        )

    def save_milestones(
        self, tenant: str, job_id: str, milestones: list[dict[str, object]]
    ) -> LedgerResponse:
        return self._call(
            "POST",
            f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/milestones",
            {"milestones": milestones},
        )

    def bill_job(
        self, tenant: str, job_id: str, method: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/bill/{method}", dict(request)
        )

    def release_retainage(
        self, tenant: str, job_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST",
            f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/retainage/release",
            dict(request),
        )

    def take_deposit(
        self, tenant: str, job_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/deposits", dict(request)
        )

    def apply_deposit(
        self, tenant: str, job_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/jobs/{urllib.parse.quote(job_id)}/deposits/apply", dict(request)
        )

    def estimates(self, tenant: str, *, everything: bool = False) -> LedgerResponse:
        q = "?all=1" if everything else ""
        return self._call("GET", f"/t/{tenant}/estimates{q}")

    def estimate(self, tenant: str, estimate_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/estimates/{urllib.parse.quote(estimate_id)}")

    def save_estimate(self, tenant: str, estimate: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/estimates", dict(estimate))

    def revise_estimate(
        self, tenant: str, estimate_id: str, estimate: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/estimates/{urllib.parse.quote(estimate_id)}/revise",
            dict(estimate),
        )

    def set_estimate_status(self, tenant: str, estimate_id: str, status: str) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/estimates/{urllib.parse.quote(estimate_id)}/status",
            {"status": status},
        )

    def accept_estimate(
        self, tenant: str, estimate_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/estimates/{urllib.parse.quote(estimate_id)}/accept",
            dict(request),
        )

    def invoice_estimate(
        self, tenant: str, estimate_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/estimates/{urllib.parse.quote(estimate_id)}/invoice",
            dict(request),
        )

    def sales_orders(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/sales-orders")

    def sales_order(self, tenant: str, order_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/sales-orders/{urllib.parse.quote(order_id)}")

    def save_sales_order(self, tenant: str, order: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/sales-orders", dict(order))

    def invoice_sales_order(
        self, tenant: str, order_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/sales-orders/{urllib.parse.quote(order_id)}/invoice",
            dict(request),
        )

    def set_sales_order_status(self, tenant: str, order_id: str, status: str) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/sales-orders/{urllib.parse.quote(order_id)}/status",
            {"status": status},
        )

    def backlog(self, tenant: str, *, job_id: str = "") -> LedgerResponse:
        q = f"?job_id={urllib.parse.quote(job_id)}" if job_id else ""
        return self._call("GET", f"/t/{tenant}/sales-orders/backlog{q}")

    def work_orders(self, tenant: str, *, job_id: str = "") -> LedgerResponse:
        q = f"?job_id={urllib.parse.quote(job_id)}" if job_id else ""
        return self._call("GET", f"/t/{tenant}/work-orders{q}")

    def work_order(self, tenant: str, work_order_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/work-orders/{urllib.parse.quote(work_order_id)}")

    def save_work_order(self, tenant: str, order: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/work-orders", dict(order))

    def complete_work_order(self, tenant: str, work_order_id: str, date: str) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/work-orders/{urllib.parse.quote(work_order_id)}/complete",
            {"date": date},
        )

    def add_work_entry(
        self, tenant: str, work_order_id: str, entry: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/work-orders/{urllib.parse.quote(work_order_id)}/entries",
            dict(entry),
        )

    def purchase_orders(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/purchase-orders")

    def purchase_order(self, tenant: str, order_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/purchase-orders/{urllib.parse.quote(order_id)}")

    def save_purchase_order(self, tenant: str, order: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/purchase-orders", dict(order))

    def receive_purchase_order(
        self, tenant: str, order_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/purchase-orders/{urllib.parse.quote(order_id)}/receipts",
            dict(request),
        )

    def bill_purchase_order(
        self, tenant: str, order_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/purchase-orders/{urllib.parse.quote(order_id)}/bill",
            dict(request),
        )

    def committed_cost(self, tenant: str, *, job_id: str = "") -> LedgerResponse:
        q = f"?job_id={urllib.parse.quote(job_id)}" if job_id else ""
        return self._call("GET", f"/t/{tenant}/purchase-orders/committed{q}")

    def wip(self, tenant: str, *, through: str = "") -> LedgerResponse:
        q = f"?through={urllib.parse.quote(through)}" if through else ""
        return self._call("GET", f"/t/{tenant}/wip{q}")

    def post_wip(self, tenant: str, request: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/wip/post", dict(request))

    def inventory(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/inventory")

    def inventory_items(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/inventory/items")

    def inventory_item(self, tenant: str, sku: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/inventory/items/{urllib.parse.quote(sku)}")

    def save_item(self, tenant: str, item: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/inventory/items", dict(item))

    def receive_stock(self, tenant: str, request: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/inventory/receipts", dict(request))

    def issue_stock(self, tenant: str, request: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/inventory/issues", dict(request))

    def count_stock(self, tenant: str, request: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/inventory/counts", dict(request))

    def leads(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/leads")

    def save_lead(self, tenant: str, lead: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/leads", dict(lead))

    def convert_lead(
        self, tenant: str, lead_id: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/leads/{urllib.parse.quote(lead_id)}/convert", dict(request)
        )

    def opportunities(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/opportunities")

    def save_opportunity(self, tenant: str, opportunity: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/opportunities", dict(opportunity))

    def opportunity_estimate(
        self, tenant: str, opportunity_id: str, estimate: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/opportunities/{urllib.parse.quote(opportunity_id)}/estimate",
            dict(estimate),
        )

    def close_opportunity(
        self, tenant: str, opportunity_id: str, outcome: str, request: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/opportunities/{urllib.parse.quote(opportunity_id)}/{outcome}",
            dict(request),
        )

    def pipeline(self, tenant: str, *, owner: str = "") -> LedgerResponse:
        q = f"?owner={urllib.parse.quote(owner)}" if owner else ""
        return self._call("GET", f"/t/{tenant}/pipeline{q}")

    def crm_events(self, tenant: str, *, since: int = 0) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/events?since={since}")

    def settings(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/settings")

    def save_settings(self, tenant: str, patch: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/settings", dict(patch))

    def entity_groups(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/consolidation/groups")

    def entity_group(self, tenant: str, group_id: str) -> LedgerResponse:
        return self._call(
            "GET", f"/t/{tenant}/consolidation/groups/{urllib.parse.quote(group_id)}"
        )

    def save_entity_group(self, tenant: str, group: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/consolidation/groups", dict(group))

    def save_elimination(
        self, tenant: str, group_id: str, elimination: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST",
            f"/t/{tenant}/consolidation/groups/{urllib.parse.quote(group_id)}/eliminations",
            dict(elimination),
        )

    def consolidation_report(
        self, tenant: str, group_id: str, *, through: str = "", allow_mismatch: bool = False
    ) -> LedgerResponse:
        params = []
        if through:
            params.append(f"through={urllib.parse.quote(through)}")
        if allow_mismatch:
            params.append("allow_mismatch=1")
        q = ("?" + "&".join(params)) if params else ""
        return self._call(
            "GET",
            f"/t/{tenant}/consolidation/groups/{urllib.parse.quote(group_id)}/report{q}",
        )

    def consolidated_statements(
        self, tenant: str, group_id: str, frm: str, to: str
    ) -> LedgerResponse:
        return self._call(
            "GET",
            f"/t/{tenant}/consolidation/groups/{urllib.parse.quote(group_id)}/statements"
            f"{_qs(frm, to)}",
        )

    def reverse_entry(
        self, tenant: str, entry_id: str, *, date: str = "", memo: str = ""
    ) -> LedgerResponse:
        payload: dict[str, object] = {}
        if date:
            payload["date"] = date
        if memo:
            payload["memo"] = memo
        return self._call(
            "POST", f"/t/{tenant}/entries/{urllib.parse.quote(entry_id)}/reverse", payload
        )

    def lock_period(self, tenant: str, period: str) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/periods/{period}/lock", {})

    # --- debt ----------------------------------------------------------------

    def debt_dashboard(self, tenant: str, *, as_of: str = "") -> LedgerResponse:
        q = f"?as_of={urllib.parse.quote(as_of)}" if as_of else ""
        return self._call("GET", f"/t/{tenant}/debt{q}")

    def loans(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/debt/loans")

    def loan(self, tenant: str, loan_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/debt/loans/{urllib.parse.quote(loan_id)}")

    def save_loan(self, tenant: str, loan: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/debt/loans", dict(loan))

    def loan_payment(self, tenant: str, payment: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/debt/payments", dict(payment))

    def loan_draw(self, tenant: str, draw: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/debt/draws", dict(draw))

    def loan_payoff(
        self, tenant: str, loan_id: str, *, extra_per_period_minor: str = "0"
    ) -> LedgerResponse:
        q = f"?extra_per_period_minor={urllib.parse.quote(extra_per_period_minor)}"
        return self._call(
            "GET", f"/t/{tenant}/debt/loans/{urllib.parse.quote(loan_id)}/payoff{q}"
        )

    # --- fixed assets --------------------------------------------------------

    def asset_register(self, tenant: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/assets")

    def asset(self, tenant: str, asset_id: str) -> LedgerResponse:
        return self._call("GET", f"/t/{tenant}/assets/{urllib.parse.quote(asset_id)}")

    def save_asset(self, tenant: str, asset: Mapping[str, object]) -> LedgerResponse:
        return self._call("POST", f"/t/{tenant}/assets", dict(asset))

    def depreciate_asset(
        self, tenant: str, asset_id: str, through_date: str
    ) -> LedgerResponse:
        return self._call(
            "POST",
            f"/t/{tenant}/assets/{urllib.parse.quote(asset_id)}/depreciate",
            {"through_date": through_date},
        )

    def asset_usage(
        self, tenant: str, asset_id: str, usage: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/assets/{urllib.parse.quote(asset_id)}/usage", dict(usage)
        )

    def dispose_asset(
        self, tenant: str, asset_id: str, disposal: Mapping[str, object]
    ) -> LedgerResponse:
        return self._call(
            "POST", f"/t/{tenant}/assets/{urllib.parse.quote(asset_id)}/dispose", dict(disposal)
        )

    # --- KPIs / ratios -------------------------------------------------------

    def ratios(self, tenant: str, *, as_of: str = "") -> LedgerResponse:
        q = f"?as_of={urllib.parse.quote(as_of)}" if as_of else ""
        return self._call("GET", f"/t/{tenant}/ratios{q}")

    def get(
        self, tenant: str, path: str, params: Mapping[str, str] | None = None
    ) -> LedgerResponse:
        """Generic tenant-scoped read — the seam Ask RGNR8's tools call. `path`
        starts with '/', e.g. '/ratios' or '/jobs/harper/cost'."""
        qs = ""
        if params:
            pairs = [
                f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}"
                for k, v in params.items() if str(v) != ""
            ]
            qs = ("?" + "&".join(pairs)) if pairs else ""
        return self._call("GET", f"/t/{tenant}{path}{qs}")


def _qs(frm: str, to: str) -> str:
    parts = []
    if frm:
        parts.append(f"from={frm}")
    if to:
        parts.append(f"to={to}")
    return f"?{'&'.join(parts)}" if parts else ""
