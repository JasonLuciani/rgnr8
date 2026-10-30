"""Turn a recording (:mod:`rgnr8_qbo.recorder`) into fixtures that are safe to commit.

    py -m rgnr8_qbo.scrub <in_dir> <fixtures_dir>

``<in_dir>`` is a recorder output directory (``raw/`` and ``manifest.json``).
``<fixtures_dir>`` is where the scrubbed copy goes, normally
``packages/qbo/tests/fixtures/recorded``. Nothing is written unless the leak check
passes. The owner must still read the fixtures before committing them.

Pseudonyms
    Every replacement is an HMAC-SHA256 of the original value under the key in
    ``RGNR8_SCRUB_KEY``. The same name therefore gets the same fake name in every
    entity and every reference, so links between documents survive. Without the
    key the mapping cannot be recomputed. If the variable is unset, a random key is
    generated and printed once; the next run then produces different pseudonyms.

Amounts
    One global factor, between 0.5 and 1.5, is derived from the same key and applied
    to every money field, rounded half-up to cents. Journal entries are re-balanced
    so debits equal credits, and the TotalAmt of a transaction is recomputed from its
    scaled lines (plus tax and unapplied amounts) so the document still sums. The
    factor is never written anywhere. Anyone who knows one true amount from the
    company can recover the factor, so keep ``RGNR8_SCRUB_KEY`` private.

Leak check
    Every value that was replaced as personal data is collected with its field path.
    After scrubbing, each one (at least ``LEAK_MIN_LENGTH`` characters) is searched
    for in every string the output kept. A hit raises :class:`LeakError` naming the
    field path (the value itself is never printed). That catches a name that was not
    replaced but still appears in a kept field such as an account name.
"""

from __future__ import annotations

import argparse
import copy
import hmac
import json
import os
import re
import secrets
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from .recorder import ENTITIES

KEY_ENV = "RGNR8_SCRUB_KEY"
LEAK_MIN_LENGTH = 4
CENT = Decimal("0.01")
FACTOR_LOW = Decimal("0.5")
FACTOR_HIGH = Decimal("1.5")

# --- what counts as money ----------------------------------------------------
# A value under one of these keys, anywhere in a document, is scaled by the factor.
# Quantities, rates and percentages are NOT money and are kept.
MONEY_KEYS = frozenset({
    "TotalAmt", "Balance", "BalanceWithJobs", "Amount", "UnitPrice", "PurchaseCost",
    "TotalTax", "TaxAmount", "TaxInclusiveAmount", "NetAmountTaxable", "UnappliedAmt",
    "RemainingCredit", "CurrentBalance", "CurrentBalanceWithSubAccounts",
    "HomeBalance", "HomeBalanceWithSubAccounts", "BillRate", "PayRate", "HourlyRate",
})
# Transactions whose TotalAmt is the sum of their lines (plus tax, plus unapplied).
TOTALLED = frozenset({
    "Invoice", "SalesReceipt", "CreditMemo", "RefundReceipt", "Estimate", "PurchaseOrder",
    "Bill", "VendorCredit", "Purchase", "Deposit", "BillPayment", "Payment",
})

# --- what counts as personal data -------------------------------------------
NAME_KEYS = frozenset({
    "DisplayName", "CompanyName", "LegalName", "GivenName", "FamilyName", "MiddleName",
    "PrintOnCheckName",
})
# Entities whose names are chart structure, so kept. Everything else is scrubbed.
NAME_KEEP_ENTITIES = frozenset({"Account", "Class", "Department", "Term", "PaymentMethod", "TaxCode", "TaxRate"})
# Reference types whose name is chart structure (kept), not a person or customer.
KEEP_REF_NAMES = frozenset({
    "ClassRef", "DepartmentRef", "TaxCodeRef", "TermRef", "PaymentMethodRef",
    "CurrencyRef", "TxnTaxCodeRef", "TaxRateRef",
})
MEMO_KEYS = frozenset({"PrivateNote", "Memo", "Description", "CustomerMemo", "Note", "Notes"})
ID_NUMBER_KEYS = frozenset({
    "TaxIdentifier", "SSN", "EIN", "AcctNum", "BankAccountNumber", "RoutingNumber",
    "AccountNumber", "ResaleNum", "TaxNumber",
})
EMAIL_PARENTS = frozenset({"PrimaryEmailAddr", "EmailAddr", "Email", "BillEmail"})
ADDRESS_LINE_KEYS = frozenset({"Line1", "Line2", "Line3", "Line4", "Line5"})
ADDRESS_KEPT_KEYS = frozenset({"Country", "CountrySubDivisionCode"})

# String values kept as they are: identifiers, dates, statuses, types, chart and
# structural fields. A string under any other key is replaced (see ``defaulted``).
KEEP_KEYS = frozenset({
    "Id", "SyncToken", "TxnType", "TxnId", "type", "Type", "AccountType", "AccountSubType",
    "Classification", "Domain", "value", "PostingType", "DetailType", "DocNumber", "Active",
    "Taxable", "FiscalYearStartMonth", "ContentType", "Currency", "Level", "SubAccount",
    "Sparse", "Qty", "Rate", "RateValue", "TaxPercent", "Size",
})
KEEP_SUFFIXES = ("Date", "Time", "Status", "Id")

# --- report cells ------------------------------------------------------------
_NUMBER = re.compile(r"-?\$?[0-9][0-9,]*(\.[0-9]+)?")


class ScrubError(Exception):
    """The recording could not be scrubbed. The message is safe to print."""


class LeakError(ScrubError):
    """A value that was scrubbed as personal data still appears in the output."""


@dataclass
class ScrubStats:
    money_fields: Counter[str] = field(default_factory=Counter)
    pseudonymized: Counter[str] = field(default_factory=Counter)
    defaulted_keys: Counter[str] = field(default_factory=Counter)
    rebalanced_journal_entries: int = 0
    inconsistent_totals: int = 0
    # Every replacement value that was produced, so the leak check can skip them.
    generated: set[str] = field(default_factory=set)
    # (original value, field path) for every value replaced as personal data.
    personal: list[tuple[str, str]] = field(default_factory=list)


class Pseudonyms:
    """Deterministic replacements keyed by an HMAC of the original value."""

    def __init__(self, key: bytes) -> None:
        if not key:
            raise ScrubError(f"{KEY_ENV} is empty")
        self._key = key

    def _digest(self, label: str, value: str, size: int = 8) -> str:
        return hmac.new(self._key, f"{label}\x00{value}".encode("utf-8"), sha256).hexdigest()[:size]

    def factor(self) -> Decimal:
        unit = int(self._digest("amount-factor", "", 8), 16) / 0xFFFFFFFF
        span = FACTOR_HIGH - FACTOR_LOW
        return (FACTOR_LOW + span * Decimal(repr(unit))).quantize(Decimal("0.000001"))

    def name(self, value: str) -> str:
        # QuickBooks names are unique case-insensitively, so case does not change the pseudonym.
        return f"Name-{self._digest('name', value.strip().casefold())}"

    def memo(self, value: str) -> str:
        return f"memo {self._digest('memo', value)}"

    def email(self, value: str) -> str:
        return f"user-{self._digest('email', value.strip().casefold())}@example.com"

    def phone(self, value: str) -> str:
        return f"555-{int(self._digest('phone', value), 16) % 10000:04d}"

    def web(self, value: str) -> str:
        return f"https://example.com/{self._digest('web', value)}"

    def address_line(self, value: str) -> str:
        return f"{int(self._digest('line', value), 16) % 9000 + 100} Fixture Way"

    def city(self, value: str) -> str:
        return f"City-{self._digest('city', value)}"

    def postal(self, value: str) -> str:
        return f"{int(self._digest('postal', value), 16) % 100000:05d}"

    def file(self, value: str) -> str:
        return f"file-{self._digest('file', value)}"

    def identifier(self, value: str) -> str:
        return f"ID-{self._digest('id', value)}"

    def other(self, value: str) -> str:
        return f"x-{self._digest('other', value)}"


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        return Decimal(0)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        try:
            return Decimal(cleaned) if cleaned else Decimal(0)
        except InvalidOperation:
            return Decimal(0)
    return Decimal(0)


def _is_money(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and _NUMBER.fullmatch(value.strip()) is not None


def _formatted_like(template: object, amount: Decimal) -> Any:
    """``amount`` in the same JSON shape as ``template`` (a number or a string)."""
    if amount == 0:
        amount = Decimal("0.00")
    if isinstance(template, str):
        return f"{amount:,.2f}" if "," in template else f"{amount:.2f}"
    return float(amount)


def _scaled(value: object, factor: Decimal) -> Any:
    amount = (_to_decimal(value) * factor).quantize(CENT, rounding=ROUND_HALF_UP)
    return _formatted_like(value, amount)


def _amount(value: object) -> Decimal:
    return _to_decimal(value) if _is_money(value) else Decimal(0)


def _line_amount(line: object) -> Decimal:
    return _amount(line.get("Amount")) if isinstance(line, dict) else Decimal(0)


def _document_total(row: Mapping[str, Any]) -> Decimal:
    """What a transaction's TotalAmt should be: its non-subtotal lines, plus its
    tax, plus any unapplied payment amount."""
    total = Decimal(0)
    lines = row.get("Line")
    for line in lines if isinstance(lines, list) else []:
        if isinstance(line, dict) and line.get("DetailType") != "SubTotalLineDetail":
            total += _line_amount(line)
    tax = row.get("TxnTaxDetail")
    if isinstance(tax, dict):
        total += _amount(tax.get("TotalTax"))
    total += _amount(row.get("UnappliedAmt"))
    return total


class _Scrubber:
    def __init__(self, pseudo: Pseudonyms, factor: Decimal, stats: ScrubStats) -> None:
        self._p = pseudo
        self._factor = factor
        self._stats = stats

    # --- walking ------------------------------------------------------------
    def document(self, node: Any, entity: str, path: str) -> Any:
        if isinstance(node, dict):
            return self._dict(node, entity, path, "")
        if isinstance(node, list):
            return self._list(node, entity, path, "")
        return node

    def _dict(self, node: Mapping[str, Any], entity: str, path: str, own_key: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key == "TempDownloadUri":
                self._stats.pseudonymized["dropped TempDownloadUri"] += 1
                continue
            if isinstance(value, dict):
                out[key] = self._dict(value, entity, child, key)
            elif isinstance(value, list):
                out[key] = self._list(value, entity, child, key)
            else:
                out[key] = self._leaf(key, value, entity, own_key, child)
        return out

    def _list(self, items: list[Any], entity: str, path: str, key: str) -> list[Any]:
        is_rows = key in ENTITIES
        out: list[Any] = []
        for index, item in enumerate(items):
            item_path = f"{path}[{index}]"
            if isinstance(item, dict):
                row_entity = key if is_rows else entity
                scrubbed = self._dict(item, row_entity, item_path, key)
                if is_rows:
                    self._fix_totals(key, item, scrubbed, item_path)
                out.append(scrubbed)
            elif isinstance(item, list):
                out.append(self._list(item, entity, item_path, key))
            else:
                out.append(self._leaf(key, item, entity, "", item_path))
        return out

    # --- leaves -------------------------------------------------------------
    def _leaf(self, key: str, value: object, entity: str, own_key: str, path: str) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if key in MONEY_KEYS and _is_money(value):
            self._stats.money_fields[key] += 1
            return _scaled(value, self._factor)
        if not isinstance(value, str):
            return value
        return self._string(key, value, entity, own_key, path)

    def _string(self, key: str, value: str, entity: str, own_key: str, path: str) -> str:
        p = self._p
        if value == "":
            return value
        if key in NAME_KEYS:
            return self._personal("name", value, p.name(value), path)
        if key == "Name":
            if entity in NAME_KEEP_ENTITIES or own_key == "CustomField":
                return value
            return self._personal("name", value, p.name(value), path)
        if key == "FullyQualifiedName":
            if entity in NAME_KEEP_ENTITIES:
                return value
            segments = [
                self._personal("name", seg.strip(), p.name(seg), path) if seg.strip() else seg
                for seg in value.split(":")
            ]
            return ":".join(segments)
        if key == "name":
            if own_key.endswith("AccountRef") or own_key in KEEP_REF_NAMES:
                return value
            if own_key == "ParentRef" and entity == "Account":
                return value
            return self._personal("name", value, p.name(value), path)
        if key in MEMO_KEYS:
            return self._personal("memo", value, p.memo(value), path)
        if key == "value" and own_key in MEMO_KEYS:  # CustomerMemo is {"value": text}
            return self._personal("memo", value, p.memo(value), path)
        if key in ID_NUMBER_KEYS:
            if key == "AcctNum" and entity == "Account":
                return value
            return self._personal("identifier", value, p.identifier(value), path)
        if key == "FileName":
            return self._personal("file", value, p.file(value), path)
        if key == "URI":
            return self._personal("web", value, p.web(value), path)
        if key == "FreeFormNumber":
            return self._personal("phone", value, p.phone(value), path)
        if key == "Address" and own_key in EMAIL_PARENTS:
            return self._personal("email", value, p.email(value), path)
        if own_key.endswith("Addr"):
            if key in ADDRESS_LINE_KEYS:
                return self._personal("address", value, p.address_line(value), path)
            if key == "City":
                return self._personal("address", value, p.city(value), path)
            if key == "PostalCode":
                return self._personal("address", value, p.postal(value), path)
            if key in ("Lat", "Long"):
                self._stats.pseudonymized["address"] += 1
                self._stats.generated.add("0")
                return "0"
            if key in ADDRESS_KEPT_KEYS:
                return value
        if key in KEEP_KEYS or key.endswith(KEEP_SUFFIXES):
            return value
        self._stats.defaulted_keys[key] += 1
        replacement = p.other(value)
        self._stats.generated.add(replacement)
        return replacement

    def _personal(self, category: str, original: str, replacement: str, path: str) -> str:
        # A replacement equal to the original means a pseudonym failed; refuse rather than leak.
        if replacement == original:
            raise LeakError(f"{path}: the {category} replacement equals the original; nothing was written")
        self._stats.personal.append((original.strip(), path))
        self._stats.generated.add(replacement)
        self._stats.pseudonymized[category] += 1
        return replacement

    # --- totals -------------------------------------------------------------
    def _fix_totals(self, entity: str, source: Mapping[str, Any], out: dict[str, Any], path: str) -> None:
        if entity == "JournalEntry":
            self._rebalance_journal(source, out, path)
            return
        if entity not in TOTALLED or "TotalAmt" not in source or not isinstance(out.get("Line"), list):
            return
        original_total = _amount(source.get("TotalAmt"))
        if _document_total(source) != original_total:
            self._stats.inconsistent_totals += 1
            return
        new_total = _document_total(out)
        out["TotalAmt"] = _formatted_like(source["TotalAmt"], new_total)
        if "Balance" in source and _amount(source["Balance"]) == original_total and _is_money(source["Balance"]):
            out["Balance"] = _formatted_like(source["Balance"], new_total)

    def _rebalance_journal(self, source: Mapping[str, Any], out: dict[str, Any], path: str) -> None:
        source_lines = source.get("Line")
        out_lines = out.get("Line")
        if not isinstance(source_lines, list) or not isinstance(out_lines, list):
            return

        def posting(line: object) -> object:
            detail = line.get("JournalEntryLineDetail") if isinstance(line, dict) else None
            return detail.get("PostingType") if isinstance(detail, dict) else None

        def sides(lines: list[Any]) -> tuple[Decimal, Decimal]:
            debit = credit = Decimal(0)
            for line in lines:
                if posting(line) == "Debit":
                    debit += _line_amount(line)
                elif posting(line) == "Credit":
                    credit += _line_amount(line)
            return debit, credit

        src_debit, src_credit = sides(source_lines)
        if src_debit != src_credit:
            self._stats.inconsistent_totals += 1  # the source did not balance; do not invent a fix
            return
        debit, credit = sides(out_lines)
        difference = debit - credit
        if difference == 0:
            return
        want = "Credit" if difference > 0 else "Debit"
        candidates = [ln for ln in out_lines if isinstance(ln, dict) and posting(ln) == want and _is_money(ln.get("Amount"))]
        if not candidates:
            self._stats.inconsistent_totals += 1
            return
        target = max(candidates, key=lambda ln: _amount(ln.get("Amount")))
        target["Amount"] = _formatted_like(target["Amount"], _amount(target["Amount"]) + abs(difference))
        self._stats.rebalanced_journal_entries += 1


# --- reports -----------------------------------------------------------------
def _cells_of(node: Any, rows: list[list[Any]], totals: list[list[Any]]) -> None:
    if isinstance(node, dict):
        cols = node.get("ColData")
        if isinstance(cols, list) and len(cols) >= 3 and isinstance(cols[0], dict) and "id" in cols[0]:
            rows.append(cols)
        summary = node.get("Summary")
        if isinstance(summary, dict):
            scols = summary.get("ColData")
            if (isinstance(scols, list) and len(scols) >= 3 and isinstance(scols[0], dict)
                    and str(scols[0].get("value", "")).strip().upper() == "TOTAL"):
                totals.append(scols)
        for value in node.values():
            _cells_of(value, rows, totals)
    elif isinstance(node, list):
        for value in node:
            _cells_of(value, rows, totals)


def _scrub_report(doc: Any, name: str, factor: Decimal, stats: ScrubStats) -> Any:
    """Report cells after the first column are money and are scaled. The first column
    (account and section names) is kept. For a trial balance the TOTAL row is set to
    the sum of the scaled rows, so the report still balances."""
    out = copy.deepcopy(doc)
    rows: list[list[Any]] = []
    totals: list[list[Any]] = []
    _cells_of(out, rows, totals)
    before = [(_amount(r[1].get("value")), _amount(r[2].get("value"))) for r in rows]
    source_sums = (sum((b[0] for b in before), Decimal(0)), sum((b[1] for b in before), Decimal(0)))
    source_totals = [(_amount(t[1].get("value")), _amount(t[2].get("value"))) for t in totals]

    def scale(node: Any) -> None:
        if isinstance(node, dict):
            cols = node.get("ColData")
            if isinstance(cols, list):
                for cell in cols[1:]:
                    if isinstance(cell, dict) and _is_money(cell.get("value")):
                        cell["value"] = _scaled(cell.get("value"), factor)
                        stats.money_fields["ColData"] += 1
            for value in node.values():
                scale(value)
        elif isinstance(node, list):
            for value in node:
                scale(value)

    scale(out)
    if name == "TrialBalance" and totals:
        new_sums = (sum((_amount(r[1].get("value")) for r in rows), Decimal(0)),
                    sum((_amount(r[2].get("value")) for r in rows), Decimal(0)))
        for total_cols, source_total in zip(totals, source_totals):
            if source_total == source_sums:
                total_cols[1]["value"] = _formatted_like(total_cols[1].get("value"), new_sums[0])
                total_cols[2]["value"] = _formatted_like(total_cols[2].get("value"), new_sums[1])
            else:
                stats.inconsistent_totals += 1
    return out


# --- the leak check ----------------------------------------------------------
def _strings(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _strings(value, out)
    elif isinstance(node, list):
        for value in node:
            _strings(value, out)
    elif isinstance(node, str):
        out.append(node)


def check_no_leaks(personal: Sequence[tuple[str, str]], kept_strings: Sequence[str]) -> None:
    """Raise :class:`LeakError`, naming the field path, if any personal value appears
    (whole word, case-insensitive) in ``kept_strings``."""
    haystack = "\n".join(kept_strings).casefold()
    checked: set[str] = set()
    for original, path in personal:
        folded = original.strip().casefold()
        if len(folded) < LEAK_MIN_LENGTH or folded in checked:
            continue
        checked.add(folded)
        if folded not in haystack:
            continue
        if re.search(r"(?<!\w)" + re.escape(folded) + r"(?!\w)", haystack):
            raise LeakError(f"an unscrubbed personal value from {path} still appears in the output; "
                            "nothing was written")


def _scrub_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    kept = {k: v for k, v in manifest.items() if "tenant" not in k.lower()}
    copied: dict[str, Any] = json.loads(json.dumps(kept))
    return copied


def scrub_recording(in_dir: Path, fixtures_dir: Path, *, key: bytes) -> ScrubStats:
    """Scrub ``in_dir`` into ``fixtures_dir``. Returns the statistics; raises
    :class:`ScrubError` (or :class:`LeakError`) before writing anything if it cannot."""
    raw = in_dir / "raw"
    manifest_path = in_dir / "manifest.json"
    if not raw.is_dir():
        raise ScrubError(f"no raw/ directory under {in_dir}")
    if not manifest_path.is_file():
        raise ScrubError(f"no manifest.json under {in_dir}")

    pseudo = Pseudonyms(key)
    factor = pseudo.factor()
    stats = ScrubStats()
    scrubber = _Scrubber(pseudo, factor, stats)

    outputs: dict[Path, Any] = {}
    for source in sorted(raw.rglob("*.json")):
        rel = source.relative_to(raw)
        doc = json.loads(source.read_text(encoding="utf-8"))
        if rel.parts[0] == "reports":
            report_name = rel.parts[1] if len(rel.parts) > 2 else ""
            outputs[rel] = _scrub_report(doc, report_name, factor, stats)
        else:
            entity = "" if rel.parts[0] == "CDC" else rel.parts[0]
            outputs[rel] = scrubber.document(doc, entity, rel.as_posix())

    manifest = _scrub_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))

    kept: list[str] = []
    for doc in outputs.values():
        _strings(doc, kept)
    check_no_leaks(stats.personal, [s for s in kept if s not in stats.generated])

    fixtures_dir.mkdir(parents=True, exist_ok=True)
    for rel, doc in outputs.items():
        target = fixtures_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    manifest["scrub"] = {
        "files": len(outputs),
        "personal_fields_replaced": dict(stats.pseudonymized),
        "money_fields_scaled": dict(stats.money_fields),
        "keys_replaced_by_default": dict(stats.defaulted_keys),
        "journal_entries_rebalanced": stats.rebalanced_journal_entries,
        "documents_not_recomputed": stats.inconsistent_totals,
    }
    (fixtures_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rgnr8_qbo.scrub",
        description="Scrub a QuickBooks recording into fixtures that are safe to review and commit.")
    parser.add_argument("in_dir", help="a recorder output directory (raw/ and manifest.json)")
    parser.add_argument("fixtures_dir", help="where the scrubbed fixtures go, e.g. packages/qbo/tests/fixtures/recorded")
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ if env is None else env
    configured = environ.get(KEY_ENV, "")
    if configured:
        key = configured.encode("utf-8")
    else:
        generated = secrets.token_hex(32)
        key = generated.encode("utf-8")
        print(f"scrub: {KEY_ENV} is not set; a random key was generated for this run and is printed once. "
              f"Set {KEY_ENV} to this value to reproduce the same pseudonyms and amounts:\n{generated}",
              file=sys.stderr)
    try:
        stats = scrub_recording(Path(args.in_dir), Path(args.fixtures_dir), key=key)
    except ScrubError as exc:
        print(f"scrub: {exc}", file=sys.stderr)
        return 1
    print(f"scrubbed into {args.fixtures_dir}: {sum(stats.pseudonymized.values())} personal values replaced, "
          f"{sum(stats.money_fields.values())} money fields scaled, "
          f"{stats.rebalanced_journal_entries} journal entries rebalanced, "
          f"{stats.inconsistent_totals} documents not recomputed. "
          "Read the fixtures before committing them.")
    if stats.defaulted_keys:
        print("keys replaced by default (check these are not identifying): "
              + ", ".join(f"{k} ({n})" for k, n in sorted(stats.defaulted_keys.items())), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
