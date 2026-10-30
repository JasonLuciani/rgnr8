"""The manual journal-entry form takes any number of lines, not just four.

Covers the POST handler (how lines are read from the form, the per-line name and
memo, balance checks), the rendered form (the rows and the Add line control),
and the spreadsheet paste parser. The parser is plain JavaScript in the page, so
its test runs it under Node and is skipped where Node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any
from urllib.parse import unquote_plus

import pytest
from test_books import _app, _req

from rgnr8_web.books_screens import _JE_LINES_JS


def _post(app: Any, body: str) -> Any:
    return _req(app, "/t/acme/books/entries", method="POST", body=body)


def _posted_lines(transport: Any) -> list[dict[str, Any]]:
    post = [c for c in transport.calls if c[0] == "POST"][0]
    return json.loads(post[2])["lines"]


# --- posting -----------------------------------------------------------------

def test_a_six_line_balanced_entry_posts_all_six_lines() -> None:
    app, transport, _a = _app()
    r = _post(app, "date=2026-08-15&memo=Month+end"
                   "&code1=1000&debit1=1,000.00&code2=1000&debit2=500.00&code3=1000&debit3=250.00"
                   "&code4=4000&credit4=1,000.00&code5=4000&credit5=500.00&code6=4000&credit6=250.00")
    assert r.status == 302
    assert "posted=" in str(r.headers.get("Location", ""))
    lines = _posted_lines(transport)
    assert len(lines) == 6
    assert [ln["side"] for ln in lines] == ["DEBIT"] * 3 + ["CREDIT"] * 3
    assert [ln["amount_minor"] for ln in lines] == [
        "100000", "50000", "25000", "100000", "50000", "25000"]


def test_an_entry_with_twelve_lines_posts_every_line_in_order() -> None:
    # Indexes past 4 and past 9 (where a suffix match would collide) are read.
    app, transport, _a = _app()
    fields = ["date=2026-08-15"]
    fields += [f"code{n}=1000&debit{n}=10.00" for n in range(1, 7)]
    fields += [f"code{n}=4000&credit{n}=10.00" for n in range(7, 13)]
    r = _post(app, "&".join(fields))
    assert r.status == 302
    lines = _posted_lines(transport)
    assert len(lines) == 12
    assert [ln["code"] for ln in lines] == ["1000"] * 6 + ["4000"] * 6


def test_an_unbalanced_entry_is_refused_before_it_reaches_the_ledger() -> None:
    app, transport, _a = _app()
    r = _post(app, "date=2026-08-15&code1=1000&debit1=1,000.00&code2=1000&debit2=1.00"
                   "&code3=4000&credit3=1,000.50")
    assert r.status == 302
    message = unquote_plus(str(r.headers.get("Location", "")))
    assert "must equal" in message and "$1,001.00" in message and "$1,000.50" in message
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_an_amount_on_a_row_with_no_account_is_refused() -> None:
    # Such a row would otherwise be dropped without a word, leaving an entry
    # that differs from what the owner sees on the page.
    app, transport, _a = _app()
    r = _post(app, "date=2026-08-15&code1=1000&debit1=10&code2=4000&credit2=10"
                   "&code3=&debit3=5.00")
    assert "choose an account" in unquote_plus(str(r.headers.get("Location", "")))
    assert not [c for c in transport.calls if c[0] == "POST"]


# --- per-line name and memo --------------------------------------------------

def test_a_line_name_and_memo_reach_the_payload_as_one_line_memo() -> None:
    app, transport, _a = _app()
    _post(app, "date=2026-08-15&code1=1000&debit1=75.00&name1=Acme+Corp&memo1=Consulting"
               "&code2=4000&credit2=75.00&name2=Acme+Corp")
    lines = _posted_lines(transport)
    assert lines[0] == {"code": "1000", "side": "DEBIT", "amount_minor": "7500",
                        "memo": "Acme Corp: Consulting"}
    # A name with no memo is still carried; a line with neither gets no memo key.
    assert lines[1] == {"code": "4000", "side": "CREDIT", "amount_minor": "7500",
                        "memo": "Acme Corp"}


def test_a_line_with_no_name_or_memo_sends_no_memo_key() -> None:
    app, transport, _a = _app()
    _post(app, "date=2026-08-15&code1=1000&debit1=10&code2=4000&credit2=10")
    assert all("memo" not in ln for ln in _posted_lines(transport))


def test_dimensions_stay_with_their_own_line_past_line_nine() -> None:
    # `dim_class2` must not also land on line 12 (both end in "2").
    app, transport, _a = _app()
    _post(app, "date=2026-08-15&code2=1000&debit2=10&dim_class2=Dine-in"
               "&code12=4000&credit12=10&dim_class12=Catering")
    lines = _posted_lines(transport)
    assert lines[0]["dimensions"] == {"class": "Dine-in"}
    assert lines[1]["dimensions"] == {"class": "Catering"}


# --- the rendered form -------------------------------------------------------

def test_the_form_renders_four_rows_and_the_add_line_control() -> None:
    app, _t, _a = _app()
    body = _req(app, "/t/acme/books").body
    assert 'name="code4"' in body and 'name="code5"' not in body
    assert 'data-action="jeAddLine"' in body
    assert 'data-action="jeRemoveLine"' in body
    assert 'name="name1"' in body and 'name="memo1"' in body
    assert 'name="paste"' in body and 'data-action="jeFillFromPaste"' in body
    assert "function jeParse(" in body


# --- the paste parser --------------------------------------------------------

def _parse(text: str) -> list[dict[str, str]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the paste parser is JavaScript")
    script = _JE_LINES_JS + (
        "\nprocess.stdout.write(JSON.stringify(jeParse(require('fs').readFileSync(0, 'utf8'))));")
    proc = subprocess.run([node, "-e", script], input=text.encode("utf-8"),
                          capture_output=True, check=True)
    return json.loads(proc.stdout)


def test_paste_parser_reads_tab_rows_with_blank_cells_and_thousands_separators() -> None:
    text = (
        "Account\tDebit\tCredit\tMemo\tName\r\n"
        "1000\t1,234.56\t\tRent\tAcme Corp\r\n"
        "\t\t\t\t\r\n"
        "6300\t\t75.00\t\t\r\n"
    )
    assert _parse(text) == [
        {"code": "1000", "debit": "1,234.56", "credit": "", "memo": "Rent", "name": "Acme Corp"},
        {"code": "6300", "debit": "", "credit": "75.00", "memo": "", "name": ""},
    ]


def test_paste_parser_strips_quotes_spreadsheets_wrap_around_a_cell() -> None:
    assert _parse('1000\t"2,000.00"\t\t"Deposit"\t\n') == [
        {"code": "1000", "debit": "2,000.00", "credit": "", "memo": "Deposit", "name": ""},
    ]


def test_paste_parser_returns_nothing_for_blank_input() -> None:
    assert _parse("\n  \n") == []
