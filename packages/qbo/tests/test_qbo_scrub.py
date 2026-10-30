"""The scrubber: deterministic pseudonyms, personal fields replaced, money scaled and
kept consistent, and the leak check. Synthetic recordings only; nothing here is real
company data."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rgnr8_qbo import scrub as scrub_module
from rgnr8_qbo.scrub import LeakError, Pseudonyms, ScrubError, check_no_leaks, main, scrub_recording

KEY = b"test-scrub-key-not-secret"
FACTOR = Pseudonyms(KEY).factor()


def write_recording(root: Path, docs: dict[str, Any], manifest: dict[str, Any] | None = None) -> Path:
    """Write ``docs`` (relative raw path -> JSON value) as a recorder output directory."""
    for rel, doc in docs.items():
        target = root / "raw" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest or {
        "recorded_at": "2026-01-15T12:00:00", "minorversion": "75", "since": "2026-01-01",
        "records": [{"name": "Customer", "kind": "entity", "pages": 1, "count": 1,
                     "requests": ["https://quickbooks.api.intuit.com/v3/company/{realm}/query?query=x"]}],
    }), encoding="utf-8")
    return root


def query(entity: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"QueryResponse": {entity: rows, "startPosition": 1}}


def scrubbed(tmp_path: Path, docs: dict[str, Any]) -> tuple[Path, Path]:
    src = write_recording(tmp_path / "rec", docs)
    dst = tmp_path / "fixtures"
    scrub_recording(src, dst, key=KEY)
    return src, dst


def load(dst: Path, rel: str) -> dict[str, Any]:
    return json.loads((dst / rel).read_text(encoding="utf-8"))


def every_output_text(dst: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in dst.rglob("*.json"))


CUSTOMER = {
    "Id": "58", "SyncToken": "2", "DisplayName": "Dana Whitfield", "GivenName": "Dana",
    "FamilyName": "Whitfield", "FullyQualifiedName": "Dana Whitfield:Kitchen remodel",
    "PrintOnCheckName": "Dana Whitfield", "PrimaryEmailAddr": {"Address": "dana@whitfield.test"},
    "PrimaryPhone": {"FreeFormNumber": "555-867-5309"},
    "BillAddr": {"Id": "12", "Line1": "12 Oak Street", "City": "Springfield",
                 "CountrySubDivisionCode": "IL", "PostalCode": "62701", "Country": "USA"},
    "WebAddr": {"URI": "https://whitfield.test/home"},
    "Notes": "Prefers calls after lunch", "Taxable": True, "Balance": 1200.5,
    "TaxIdentifier": "123-45-6789", "AcctNum": "ACME-77",
    "MetaData": {"CreateTime": "2024-03-01T10:00:00-08:00", "LastUpdatedTime": "2025-01-02T09:00:00-08:00"},
}

INVOICE = {
    "Id": "101", "SyncToken": "0", "DocNumber": "1001", "TxnDate": "2025-02-01", "DueDate": "2025-03-03",
    "CustomerRef": {"value": "58", "name": "Dana Whitfield"},
    "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
    "Line": [
        {"Id": "1", "LineNum": 1, "Description": "Kitchen design call", "Amount": 120.00,
         "DetailType": "SalesItemLineDetail",
         "SalesItemLineDetail": {"ItemRef": {"value": "7", "name": "Design services"}, "UnitPrice": 120.00, "Qty": 1}},
        {"Id": "2", "LineNum": 2, "Amount": 80.50, "DetailType": "SalesItemLineDetail",
         "SalesItemLineDetail": {"ItemRef": {"value": "8", "name": "Materials"}, "UnitPrice": 80.50, "Qty": 1}},
        {"Amount": 200.50, "DetailType": "SubTotalLineDetail", "SubTotalLineDetail": {}},
    ],
    "TotalAmt": 200.50, "Balance": 200.50,
    "CustomerMemo": {"value": "Thanks for the business, Dana"},
    "PrivateNote": "Chase Dana on Friday",
    "BillAddr": {"Line1": "12 Oak Street", "City": "Springfield", "PostalCode": "62701", "Country": "USA"},
}

ACCOUNTS = [
    {"Id": "35", "Name": "Consulting Income", "FullyQualifiedName": "Consulting Income",
     "AccountType": "Income", "AccountSubType": "ServiceFeeIncome", "AcctNum": "4000",
     "CurrentBalance": 5000.00, "Classification": "Revenue", "Active": True},
    {"Id": "36", "Name": "Checking", "AccountType": "Bank", "CurrentBalance": 1000.0, "Active": True},
]


def test_same_name_gets_the_same_pseudonym_everywhere(tmp_path: Path) -> None:
    attachable = {"Id": "9", "FileName": "contract.pdf", "Note": "Signed",
                  "AttachableRef": [{"EntityRef": {"type": "Customer", "value": "58", "name": "Dana Whitfield"}}]}
    _, dst = scrubbed(tmp_path, {
        "Customer/0001.json": query("Customer", [CUSTOMER]),
        "Invoice/0001.json": query("Invoice", [INVOICE]),
        "Attachable/0001.json": query("Attachable", [attachable]),
    })

    customer = load(dst, "Customer/0001.json")["QueryResponse"]["Customer"][0]
    invoice = load(dst, "Invoice/0001.json")["QueryResponse"]["Invoice"][0]
    attached = load(dst, "Attachable/0001.json")["QueryResponse"]["Attachable"][0]

    pseudonym = customer["DisplayName"]
    assert pseudonym != "Dana Whitfield"
    assert invoice["CustomerRef"]["name"] == pseudonym
    assert customer["FullyQualifiedName"].startswith(pseudonym + ":")
    assert customer["PrintOnCheckName"] == pseudonym
    assert attached["AttachableRef"][0]["EntityRef"]["name"] == pseudonym
    assert customer["Id"] == "58" and invoice["CustomerRef"]["value"] == "58"


def test_personal_fields_are_replaced_and_structure_is_kept(tmp_path: Path) -> None:
    _, dst = scrubbed(tmp_path, {
        "Customer/0001.json": query("Customer", [CUSTOMER]),
        "Invoice/0001.json": query("Invoice", [INVOICE]),
    })

    customer = load(dst, "Customer/0001.json")["QueryResponse"]["Customer"][0]
    assert customer["PrimaryEmailAddr"]["Address"].endswith("@example.com")
    assert customer["PrimaryPhone"]["FreeFormNumber"].startswith("555-")
    assert customer["PrimaryPhone"]["FreeFormNumber"] != "555-867-5309"
    addr = customer["BillAddr"]
    assert addr["Line1"] != "12 Oak Street" and addr["City"] != "Springfield"
    assert addr["PostalCode"] != "62701"
    assert addr["CountrySubDivisionCode"] == "IL" and addr["Country"] == "USA"
    assert customer["WebAddr"]["URI"].startswith("https://example.com/")
    assert customer["Notes"].startswith("memo ") and len(customer["Notes"]) == len("memo ") + 8
    assert customer["TaxIdentifier"] != "123-45-6789"
    assert customer["AcctNum"] != "ACME-77"
    assert customer["Taxable"] is True
    assert customer["MetaData"]["CreateTime"] == "2024-03-01T10:00:00-08:00"

    invoice = load(dst, "Invoice/0001.json")["QueryResponse"]["Invoice"][0]
    assert invoice["CustomerMemo"]["value"].startswith("memo ")
    assert invoice["PrivateNote"].startswith("memo ")
    assert invoice["Line"][0]["Description"].startswith("memo ")
    assert invoice["DocNumber"] == "1001" and invoice["TxnDate"] == "2025-02-01"

    text = every_output_text(dst)
    for original in ("Whitfield", "Dana", "Oak Street", "Springfield", "dana@", "867-5309",
                     "123-45-6789", "ACME-77", "Thanks for the business", "Chase Dana", "Kitchen design"):
        assert original not in text, original


def test_account_names_and_types_are_kept(tmp_path: Path) -> None:
    _, dst = scrubbed(tmp_path, {"Account/0001.json": query("Account", ACCOUNTS)})

    account = load(dst, "Account/0001.json")["QueryResponse"]["Account"][0]
    assert account["Name"] == "Consulting Income"
    assert account["FullyQualifiedName"] == "Consulting Income"
    assert account["AccountType"] == "Income" and account["AccountSubType"] == "ServiceFeeIncome"
    assert account["AcctNum"] == "4000"
    assert account["Classification"] == "Revenue"


def test_money_is_one_global_factor_rounded_to_cents(tmp_path: Path) -> None:
    assert Decimal("0.5") <= FACTOR <= Decimal("1.5")
    _, dst = scrubbed(tmp_path, {"Customer/0001.json": query("Customer", [CUSTOMER])})

    balance = load(dst, "Customer/0001.json")["QueryResponse"]["Customer"][0]["Balance"]
    assert balance == float((Decimal("1200.5") * FACTOR).quantize(Decimal("0.01")))


def test_invoice_total_equals_the_sum_of_its_scaled_lines(tmp_path: Path) -> None:
    _, dst = scrubbed(tmp_path, {"Invoice/0001.json": query("Invoice", [INVOICE])})

    invoice = load(dst, "Invoice/0001.json")["QueryResponse"]["Invoice"][0]
    lines = [Decimal(str(ln["Amount"])) for ln in invoice["Line"] if ln["DetailType"] != "SubTotalLineDetail"]
    assert Decimal(str(invoice["TotalAmt"])) == sum(lines)
    assert invoice["Balance"] == invoice["TotalAmt"]
    assert invoice["Line"][0]["SalesItemLineDetail"]["UnitPrice"] == invoice["Line"][0]["Amount"]


@pytest.mark.parametrize("debits, credit", [
    ((100.33, 50.10), 150.43),
    ((0.33, 0.33, 0.34), 1.00),
    ((999.99, 0.01), 1000.00),
    ((12.345, 0.005), 12.35),
    ((1.11, 2.22, 3.33, 4.44), 11.10),
])
def test_journal_entries_still_balance_after_scaling(tmp_path: Path, debits: tuple[float, ...], credit: float) -> None:
    lines = [{"Id": str(i), "Amount": amount, "DetailType": "JournalEntryLineDetail",
              "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "36", "name": "Checking"}}}
             for i, amount in enumerate(debits)]
    lines.append({"Id": "9", "Amount": credit, "DetailType": "JournalEntryLineDetail",
                  "JournalEntryLineDetail": {"PostingType": "Credit", "AccountRef": {"value": "35", "name": "Consulting Income"}}})
    _, dst = scrubbed(tmp_path, {"JournalEntry/0001.json": query("JournalEntry", [{"Id": "200", "Line": lines}])})

    out_lines = load(dst, "JournalEntry/0001.json")["QueryResponse"]["JournalEntry"][0]["Line"]
    debit = sum(Decimal(str(ln["Amount"])) for ln in out_lines if ln["JournalEntryLineDetail"]["PostingType"] == "Debit")
    credit_out = sum(Decimal(str(ln["Amount"])) for ln in out_lines if ln["JournalEntryLineDetail"]["PostingType"] == "Credit")
    assert debit == credit_out


def test_journal_entry_is_rebalanced_when_rounding_breaks_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # At a factor of 0.5, 0.03 -> 0.02 (half-up) and 0.06 -> 0.03, so the scaled debits (0.04)
    # no longer equal the scaled credit (0.03). The scrubber must put the difference back.
    monkeypatch.setattr(scrub_module.Pseudonyms, "factor", lambda self: Decimal("0.5"))
    lines = [
        {"Amount": 0.03, "DetailType": "JournalEntryLineDetail",
         "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "36", "name": "Checking"}}},
        {"Amount": 0.03, "DetailType": "JournalEntryLineDetail",
         "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "36", "name": "Checking"}}},
        {"Amount": 0.06, "DetailType": "JournalEntryLineDetail",
         "JournalEntryLineDetail": {"PostingType": "Credit", "AccountRef": {"value": "35", "name": "Consulting Income"}}},
    ]
    src = write_recording(tmp_path / "rec", {"JournalEntry/0001.json": query("JournalEntry", [{"Id": "200", "Line": lines}])})
    stats = scrub_recording(src, tmp_path / "fixtures", key=KEY)

    out_lines = load(tmp_path / "fixtures", "JournalEntry/0001.json")["QueryResponse"]["JournalEntry"][0]["Line"]
    debit = sum(Decimal(str(ln["Amount"])) for ln in out_lines if ln["JournalEntryLineDetail"]["PostingType"] == "Debit")
    credit = sum(Decimal(str(ln["Amount"])) for ln in out_lines if ln["JournalEntryLineDetail"]["PostingType"] == "Credit")
    assert debit == credit == Decimal("0.04")
    assert stats.rebalanced_journal_entries == 1
    assert stats.inconsistent_totals == 0


def test_trial_balance_total_matches_its_scaled_rows(tmp_path: Path) -> None:
    report = {"Header": {"ReportName": "TrialBalance", "Currency": "USD"}, "Rows": {"Row": [
        {"type": "Data", "ColData": [{"value": "Checking", "id": "36"}, {"value": "1,000.00"}, {"value": ""}]},
        {"type": "Data", "ColData": [{"value": "Consulting Income", "id": "35"}, {"value": ""}, {"value": "1,000.00"}]},
        {"Summary": {"ColData": [{"value": "TOTAL"}, {"value": "1,000.00"}, {"value": "1,000.00"}]}},
    ]}}
    _, dst = scrubbed(tmp_path, {"reports/TrialBalance/2026-01.json": report})

    rows = load(dst, "reports/TrialBalance/2026-01.json")["Rows"]["Row"]
    debit = Decimal(rows[0]["ColData"][1]["value"].replace(",", ""))
    credit = Decimal(rows[1]["ColData"][2]["value"].replace(",", ""))
    total = rows[2]["Summary"]["ColData"]
    assert rows[0]["ColData"][0]["value"] == "Checking"
    assert Decimal(total[1]["value"].replace(",", "")) == debit
    assert Decimal(total[2]["value"].replace(",", "")) == credit
    assert debit == (Decimal("1000.00") * FACTOR).quantize(Decimal("0.01"))


def test_temp_download_uris_are_dropped(tmp_path: Path) -> None:
    attachable = {"Id": "9", "FileName": "contract.pdf",
                  "TempDownloadUri": "https://attachments.example.test/secret-token-abc"}
    _, dst = scrubbed(tmp_path, {"Attachable/0001.json": query("Attachable", [attachable])})

    assert "TempDownloadUri" not in load(dst, "Attachable/0001.json")["QueryResponse"]["Attachable"][0]
    assert "secret-token-abc" not in every_output_text(dst)


def test_unknown_string_fields_are_replaced_by_default(tmp_path: Path) -> None:
    row = {"Id": "5", "Mystery": "Dana Whitfield", "Sku": "SKU-1", "Type": "Service"}
    _, dst = scrubbed(tmp_path, {"Item/0001.json": query("Item", [row])})

    out = load(dst, "Item/0001.json")["QueryResponse"]["Item"][0]
    assert out["Mystery"] != "Dana Whitfield" and out["Sku"] != "SKU-1"
    assert out["Type"] == "Service"
    assert "Dana Whitfield" not in every_output_text(dst)


def test_account_names_that_contain_a_customer_name_fail_loudly(tmp_path: Path) -> None:
    accounts = [{"Id": "1", "Name": "Dana Whitfield Retainer", "AccountType": "Income"}]
    src = write_recording(tmp_path / "rec", {
        "Customer/0001.json": query("Customer", [CUSTOMER]),
        "Account/0001.json": query("Account", accounts),
    })
    dst = tmp_path / "fixtures"

    with pytest.raises(LeakError) as caught:
        scrub_recording(src, dst, key=KEY)
    assert "Customer/0001.json" in str(caught.value)
    assert "DisplayName" in str(caught.value)
    assert "Dana" not in str(caught.value)
    assert not dst.exists() or not any(dst.rglob("*.json"))


def test_a_replacement_that_equals_the_original_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Pseudonyms, "name", lambda self, value: value)
    src = write_recording(tmp_path / "rec", {"Customer/0001.json": query("Customer", [CUSTOMER])})

    with pytest.raises(LeakError, match="Customer/0001.json"):
        scrub_recording(src, tmp_path / "fixtures", key=KEY)


def test_leak_check_names_the_path_of_a_planted_name() -> None:
    with pytest.raises(LeakError, match=r"Invoice/0007\.json\.QueryResponse\.Invoice\[3\]\.Notes"):
        check_no_leaks(
            [("Ada Planted", "Invoice/0007.json.QueryResponse.Invoice[3].Notes")],
            ["see the memo", "a line that still says Ada Planted in it"],
        )


def test_leak_check_passes_when_the_name_is_gone() -> None:
    check_no_leaks([("Ada Planted", "x")], ["Name-1a2b3c4d", "memo 99887766"])


def test_leak_check_ignores_values_too_short_to_check() -> None:
    check_no_leaks([("Al", "x")], ["Alphabet soup"])


def test_empty_key_is_refused(tmp_path: Path) -> None:
    src = write_recording(tmp_path / "rec", {"Customer/0001.json": query("Customer", [CUSTOMER])})
    with pytest.raises(ScrubError, match="empty"):
        scrub_recording(src, tmp_path / "fixtures", key=b"")


def test_manifest_is_copied_without_tenant_and_with_a_scrub_summary(tmp_path: Path) -> None:
    src = write_recording(tmp_path / "rec", {"Customer/0001.json": query("Customer", [CUSTOMER])},
                          {"recorded_at": "x", "tenant_id": "tenant-1", "minorversion": "75", "records": []})
    dst = tmp_path / "fixtures"
    scrub_recording(src, dst, key=KEY)

    manifest = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    assert "tenant_id" not in manifest
    assert manifest["minorversion"] == "75"
    assert manifest["scrub"]["files"] == 1
    assert manifest["scrub"]["personal_fields_replaced"]["name"] >= 1
    assert "factor" not in json.dumps(manifest)


def test_cli_scrubs_with_the_configured_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write_recording(tmp_path / "rec", {"Account/0001.json": query("Account", ACCOUNTS)})
    dst = tmp_path / "fixtures"

    code = main([str(src), str(dst)], env={"RGNR8_SCRUB_KEY": "cli-key"})

    assert code == 0
    assert (dst / "Account" / "0001.json").is_file()


def test_cli_generates_and_prints_a_key_when_none_is_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write_recording(tmp_path / "rec", {"Account/0001.json": query("Account", ACCOUNTS)})

    assert main([str(src), str(tmp_path / "fixtures")], env={}) == 0
    assert "random key" in capsys.readouterr().err


def test_cli_reports_a_leak_without_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    accounts = [{"Id": "1", "Name": "Dana Whitfield Retainer", "AccountType": "Income"}]
    src = write_recording(tmp_path / "rec", {
        "Customer/0001.json": query("Customer", [CUSTOMER]),
        "Account/0001.json": query("Account", accounts),
    })

    assert main([str(src), str(tmp_path / "fixtures")], env={"RGNR8_SCRUB_KEY": "k"}) == 1
    assert "Customer/0001.json" in capsys.readouterr().err
