"""Cross-language contract: the Python COA category slugs must match the TS
`ledger-kernel` BusinessCategory enum values exactly (same one-to-one mapping the
`OnboardingRegistry` promises). If someone adds a template on one side, this
fails until the other side catches up."""

from __future__ import annotations

import re
from pathlib import Path

from rgnr8_ops import BUSINESS_CATEGORIES, SOURCE_SYSTEMS

_TS = Path(__file__).resolve().parents[2] / "ledger-kernel" / "src" / "coaTemplates.ts"


def _ts_enum_values() -> set[str]:
    text = _TS.read_text()
    body = text.split("export enum BusinessCategory", 1)[1].split("}", 1)[0]
    # lines like:  SERVICE_GENERAL = "SERVICE_GENERAL",
    return set(re.findall(r'=\s*"([A-Z_]+)"', body))


def test_python_slugs_match_ts_business_category_enum() -> None:
    py = {slug for slug, _ in BUSINESS_CATEGORIES}
    ts = _ts_enum_values()
    assert py == ts, f"drift: py-only={py - ts}, ts-only={ts - py}"


def test_source_systems_match_ts_cutover_type() -> None:
    text = (_TS.parent / "cutover.ts").read_text()
    body = text.split("export type SourceSystem", 1)[1].split(";", 1)[0]
    ts = set(re.findall(r'"([a-z]+)"', body))
    assert SOURCE_SYSTEMS == ts, f"drift: py={SOURCE_SYSTEMS}, ts={ts}"


def test_go_live_contract_fields_match_ts_dto() -> None:
    """The go-live/1 DTO the Python control plane emits must use the exact field
    names the TS `GoLiveDto` declares, so `goLiveFromDto` can parse it."""
    from rgnr8_ops import GO_LIVE_CONTRACT, build_go_live_request

    req = build_go_live_request(
        "acme", "quickbooks", "2026-08-31",
        [{"code": "1000", "name": "Checking", "balance_minor": 100, "subtype": "BANK"}],
        coa_category="RETAIL",
    )
    assert req["contract"] == GO_LIVE_CONTRACT

    ts = (_TS.parent / "goLive.ts").read_text()
    # the GoLiveDto interface body
    dto_body = ts.split("export interface GoLiveDto", 1)[1].split("}", 1)[0]
    ts_fields = set(re.findall(r"readonly (\w+)\??:", dto_body))
    # the contract constant value must match too
    ts_contract = re.search(r'GO_LIVE_CONTRACT\s*=\s*"([^"]+)"', ts).group(1)
    assert ts_contract == GO_LIVE_CONTRACT

    py_fields = set(req.keys())
    # every Python field is a declared TS DTO field (TS may allow more, e.g. provenance)
    assert py_fields <= ts_fields, f"python emits fields TS won't parse: {py_fields - ts_fields}"
    # the source_accounts item fields also line up
    sa_body = dto_body.split("source_accounts", 1)[1]
    for f in req["source_accounts"][0].keys():
        assert f in sa_body, f"source-account field {f} not in TS DTO"


def test_qbo_subtype_mapping_targets_valid_ts_subtypes() -> None:
    """Every subtype the QBO/Xero mapper emits must be a real ledger-kernel
    AccountSubtype enum value, so a mapped source account is placeable in TS."""
    from rgnr8_ops import qbo_trial_balance_to_source_accounts, subtype_for_account_type
    from rgnr8_ops.onboarding import _ACCOUNT_TYPE_TO_SUBTYPE

    types_ts = (_TS.parent / "types.ts").read_text()
    body = types_ts.split("export enum AccountSubtype", 1)[1].split("}", 1)[0]
    ts_subtypes = set(re.findall(r'=\s*"([A-Z_]+)"', body))

    emitted = set(_ACCOUNT_TYPE_TO_SUBTYPE.values())
    assert emitted <= ts_subtypes, f"mapper emits non-TS subtypes: {emitted - ts_subtypes}"

    # a concrete map round-trips through the normalizer
    rows = [{"code": "1000", "name": "Checking", "account_type": "Bank",
             "debit_minor": 100, "credit_minor": 0}]
    sa = qbo_trial_balance_to_source_accounts(rows)
    assert sa[0]["subtype"] in ts_subtypes
    assert subtype_for_account_type("Accounts Payable") == "ACCOUNTS_PAYABLE"
