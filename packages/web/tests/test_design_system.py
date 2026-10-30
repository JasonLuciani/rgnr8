"""The design-system components (`data_table`, `money_bar`, `drawer`, `split_button`,
`status`, `form_shell`) and the two list screens migrated onto them: invoices and
bills.

The component tests are about what the markup does: links that carry state,
escaping, alignment, and that nothing needs an inline style or a script. The screen
tests run the real AR/AP renderer through the fake ledger transport in test_books.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from test_books import DEFAULT_ROUTES, _app, _req

from rgnr8_web.components import (
    Action,
    BarSegment,
    Cell,
    Column,
    FilterChip,
    Page,
    SortState,
    data_table,
    drawer,
    drawer_trigger,
    form_shell,
    money_bar,
    page_from_query,
    sort_from_query,
    split_button,
    status,
)


def _rows(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"name": Cell(text=n), "amount": Cell(text=a, sort=_num(a))} for n, a in pairs]


def _num(text: str) -> int:
    return int(re.sub(r"[^\d-]", "", text) or 0)


COLS = (
    Column("name", "Name", sortable=True),
    Column("amount", "Amount", money=True, sortable=True),
)


# --- data_table: escaping ----------------------------------------------------


def test_data_table_escapes_labels_and_text_but_not_caller_html() -> None:
    cols = (Column("n", "<b>Label</b>"),)
    html = data_table(cols, [{"n": Cell(text="<script>x</script>")},
                             {"n": Cell(html="<em>ok</em>")}])
    assert "<b>Label</b>" not in html and "&lt;b&gt;Label&lt;/b&gt;" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<em>ok</em>" in html, "html cells are markup the caller already escaped"


def test_data_table_escapes_the_filter_chip_label_and_url_encodes_query_values() -> None:
    html = data_table(COLS, [], filters=[FilterChip("other", "<i>Who</i>")],
                      query={"other": "x", "q": 'a"b&c'}, base="/t/acme/x")
    assert "<i>Who</i>" not in html and "&lt;i&gt;Who&lt;/i&gt;" in html
    assert "q=a%22b%26c" in html  # kept in the chip's remove link, encoded, not raw


# --- data_table: sort --------------------------------------------------------


def test_an_unsorted_sortable_header_offers_ascending_first() -> None:
    html = data_table(COLS, _rows(("A", "$1.00")), base="/t/acme/invoices", query={})
    assert 'aria-sort="none"' in html
    assert 'href="/t/acme/invoices?sort=amount&amp;dir=asc"' in html


def test_the_sort_link_toggles_direction_on_the_column_in_use() -> None:
    asc = data_table(COLS, _rows(("A", "$1.00")), sort=SortState("amount"),
                     base="/t/acme/invoices", query={"sort": "amount", "dir": "asc"})
    assert 'aria-sort="ascending"' in asc
    assert 'href="/t/acme/invoices?sort=amount&amp;dir=desc"' in asc
    desc = data_table(COLS, _rows(("A", "$1.00")), sort=SortState("amount", desc=True),
                      base="/t/acme/invoices", query={"sort": "amount", "dir": "desc"})
    assert 'aria-sort="descending"' in desc
    assert 'href="/t/acme/invoices?sort=amount&amp;dir=asc"' in desc


def test_sorting_keeps_the_rest_of_the_query_and_drops_the_page() -> None:
    html = data_table(COLS, _rows(("A", "$1.00")), base="/t/acme/invoices",
                      query={"status": "overdue", "page": "3"})
    assert 'href="/t/acme/invoices?status=overdue&amp;sort=amount&amp;dir=asc"' in html
    assert "page=" not in html


def test_sorting_orders_by_the_cell_sort_value_not_its_text() -> None:
    rows = _rows(("big", "$10.00"), ("small", "$9.00"))
    desc = data_table(COLS, rows, sort=SortState("amount", desc=True), base="/x")
    assert desc.index("big") < desc.index("small")
    asc = data_table(COLS, rows, sort=SortState("amount"), base="/x")
    assert asc.index("small") < asc.index("big"), "$9 sorts before $10 numerically"


def test_a_column_that_is_not_sortable_has_no_sort_link() -> None:
    html = data_table((Column("n", "Name"),), [{"n": "a"}], base="/x")
    assert "dt-sort" not in html and "aria-sort" not in html


def test_sort_from_query_only_accepts_a_sortable_column() -> None:
    assert sort_from_query({"sort": "amount", "dir": "desc"}, COLS) == SortState("amount", True)
    assert sort_from_query({"sort": "name"}, COLS) == SortState("name", False)
    assert sort_from_query({"sort": "nonsense"}, COLS) is None
    assert sort_from_query({"sort": "name"}, (Column("name", "Name"),)) is None


# --- data_table: paging ------------------------------------------------------


def test_paging_shows_one_page_and_links_to_the_neighbours() -> None:
    rows = [{"name": Cell(text=f"row{i}")} for i in range(1, 6)]
    cols = (Column("name", "Name"),)
    middle = data_table(cols, rows, page=Page(2, size=2), base="/x", query={"status": "open"})
    assert "row3" in middle and "row4" in middle and "row1" not in middle and "row5" not in middle
    assert "Showing 3–4 of 5" in middle and "Page 2 of 3" in middle
    assert 'href="/x?status=open&amp;page=3"' in middle, "Next keeps the filter"
    assert 'href="/x?status=open"' in middle, "Previous to page 1 drops the page param"


def test_the_first_page_has_no_previous_and_the_last_has_no_next() -> None:
    rows = [{"name": Cell(text=f"row{i}")} for i in range(1, 4)]
    cols = (Column("name", "Name"),)
    first = data_table(cols, rows, page=Page(1, size=2), base="/x")
    assert 'aria-disabled="true">‹ Previous' in first and "Next ›</a>" in first
    last = data_table(cols, rows, page=Page(2, size=2), base="/x")
    assert "row3" in last and 'aria-disabled="true">Next ›' in last
    assert "‹ Previous</a>" in last


def test_a_page_past_the_end_is_clamped_to_the_last() -> None:
    rows = [{"name": Cell(text=f"row{i}")} for i in range(1, 4)]
    html = data_table((Column("name", "Name"),), rows, page=Page(99, size=2), base="/x")
    assert "row3" in html and "Page 2 of 2" in html


def test_page_from_query_is_lenient() -> None:
    assert page_from_query({"page": "4"}, 10) == Page(4, 10)
    assert page_from_query({"page": "junk"}) == Page(1, 50)
    assert page_from_query({"page": "-3"}) == Page(1, 50)


# --- data_table: filter chips ------------------------------------------------


def test_removing_a_filter_chip_keeps_the_other_filters_and_the_sort() -> None:
    query = {"status": "overdue", "party": "acme", "sort": "amount", "dir": "desc"}
    html = data_table(COLS, _rows(("A", "$1.00")),
                      filters=[FilterChip("status", "Status: Overdue"),
                               FilterChip("party", "Party: Acme")],
                      base="/t/acme/invoices", query=query)
    remove_status = re.search(r'href="([^"]*)" aria-label="Remove filter Status', html)
    assert remove_status, html
    href = remove_status.group(1)
    assert "status=" not in href and "party=acme" in href and "sort=amount" in href
    clear = re.search(r'class="dt-clear" href="([^"]*)"', html)
    assert clear and "status=" not in clear.group(1) and "party=" not in clear.group(1)
    assert "sort=amount" in clear.group(1)


def test_no_filters_means_no_chip_bar() -> None:
    assert "dt-chips" not in data_table(COLS, _rows(("A", "$1.00")), base="/x")


# --- data_table: column gear and hidden columns ------------------------------


GEAR_COLS = (
    Column("name", "Name"),
    Column("amount", "Amount", money=True),
    Column("ref", "Reference", hidden=True),
)


def test_hidden_columns_stay_off_until_the_gear_turns_them_on() -> None:
    rows = [{"name": "A", "amount": "$1.00", "ref": "R-7"}]
    plain = data_table(GEAR_COLS, rows, base="/x", query={})
    plain_head = plain.split("<thead>")[1].split("</thead>")[0]
    assert "Reference" not in plain_head and "R-7" not in plain

    on = data_table(GEAR_COLS, rows, base="/x", query={"cols": "1", "c_ref": "1", "c_name": "1"})
    on_head = on.split("<thead>")[1].split("</thead>")[0]
    assert "Reference" in on_head and "Amount" not in on_head, "amount was not ticked"
    assert "R-7" in on


def test_the_gear_lists_every_column_and_is_a_plain_get_form() -> None:
    html = data_table(GEAR_COLS, [], base="/t/acme/invoices",
                      query={"sort": "amount", "dir": "desc", "page": "4", "cols": "1", "c_name": "1"})
    assert '<details class="dt-gear">' in html
    assert 'method="get"' in html and 'action="/t/acme/invoices"' in html
    assert 'name="c_name" value="1" checked' in html
    assert 'name="c_amount" value="1">' in html and 'name="c_ref" value="1">' in html
    assert '<input type="hidden" name="sort" value="amount">' in html, "sort survives the gear"
    assert 'name="page"' not in html, "changing columns resets paging"


def test_an_unlabelled_column_is_never_hidden_or_offered_by_the_gear() -> None:
    cols = (Column("name", "Name"), Column("actions", ""))
    rows = [{"name": "A", "actions": Cell(html="<a>go</a>")}]
    html = data_table(cols, rows, base="/x", query={"cols": "1", "c_name": "1"})
    assert "<a>go</a>" in html, "the row's actions survive a column change"
    assert 'name="c_actions"' not in html


def test_a_gear_submission_with_nothing_ticked_falls_back_to_the_defaults() -> None:
    shown = data_table(GEAR_COLS, [{"name": "A", "amount": "$1.00", "ref": "R-7"}],
                       base="/x", query={"cols": "1"})
    body = shown.split("<tbody>")[1]
    assert "$1.00" in body and "R-7" not in body


def test_gear_can_be_switched_off() -> None:
    assert "dt-gear" not in data_table(GEAR_COLS, [], base="/x", gear=False)


# --- data_table: money -------------------------------------------------------


def test_money_is_right_aligned_and_a_negative_takes_the_negative_class() -> None:
    cols = (Column("amt", "Amount", money=True),)
    html = data_table(cols, [{"amt": Cell(text="$5.00")}, {"amt": Cell(text="-$5.00")}], base="/x")
    assert '<th scope="col" class="num">Amount</th>' in html
    assert '<td class="num">$5.00</td>' in html
    assert '<td class="num neg">-$5.00</td>' in html


def test_a_numeric_column_aligns_right_without_being_money() -> None:
    cols = (Column("n", "Count", numeric=True),)
    html = data_table(cols, [{"n": "3"}], base="/x")
    assert '<td class="num">3</td>' in html and "neg" not in html


def test_a_totals_row_labels_itself_and_aligns_its_figures() -> None:
    html = data_table(COLS, _rows(("A", "$1.00")), totals={"amount": Cell(text="$1.00")}, base="/x")
    assert '<tfoot><tr class="dt-total">' in html
    assert "<td>Total</td>" in html and '<td class="num">$1.00</td>' in html


def test_an_empty_table_shows_the_empty_state_with_a_clear_action_when_filtered() -> None:
    html = data_table(COLS, [], filters=[FilterChip("status", "Status: Paid")],
                      empty="No invoices match this filter.", base="/t/acme/invoices",
                      query={"status": "paid"})
    assert 'class="empty"' in html and "No invoices match this filter." in html
    assert "Clear filters" in html and 'href="/t/acme/invoices"' in html


def test_an_empty_table_with_no_filter_has_no_clear_action() -> None:
    html = data_table(COLS, [], empty="No bills yet.", base="/x")
    assert "No bills yet." in html and "Clear filters" not in html


# --- money_bar ---------------------------------------------------------------

SEGS = (
    BarSegment("overdue", "Overdue", "$500.00", 2, "bad"),
    BarSegment("open", "Not yet due", "$1,200.00", 1, "info"),
    BarSegment("paid", "Paid", "$30.00", 1, "ok"),
)


def test_each_money_bar_segment_links_to_a_filter_on_the_same_page() -> None:
    html = money_bar(SEGS, base="/t/acme/invoices", query={"sort": "due"}, param="status",
                     unit=("invoice", "invoices"))
    assert 'href="/t/acme/invoices?sort=due&amp;status=overdue"' in html
    assert 'href="/t/acme/invoices?sort=due&amp;status=paid"' in html
    assert "2 invoices" in html and "1 invoice<" in html, "the unit agrees with the count"


def test_the_active_segment_is_marked_and_links_back_to_unfiltered() -> None:
    html = money_bar(SEGS, base="/t/acme/invoices", query={"status": "overdue", "page": "2"},
                     param="status", unit=("invoice", "invoices"))
    on = re.search(r'<a class="mb-seg tone-bad on" href="([^"]*)" aria-current="true">', html)
    assert on, html
    assert on.group(1) == "/t/acme/invoices", "clicking the active segment clears it"
    assert "Showing" in html, "the active state is in words, not colour alone"


def test_money_bar_segments_carry_their_tone_and_negative_amounts_take_neg() -> None:
    html = money_bar((BarSegment("x", "X", "-$4.00", 1, "nonsense"),), base="/x")
    assert "tone-neutral" in html and 'class="mb-amt neg"' in html


# --- drawer ------------------------------------------------------------------


def test_a_drawer_opens_from_its_trigger_with_no_script() -> None:
    html = drawer("inv-1", "Invoice INV-1", "<p>Lines</p>")
    assert 'class="drawer" id="inv-1"' in html
    assert 'aria-labelledby="inv-1-title"' in html
    assert "Invoice INV-1" in html and "<p>Lines</p>" in html
    trigger = drawer_trigger("inv-1", "Open")
    assert 'href="#inv-1"' in trigger and "Open" in trigger
    assert "<script" not in html and "onclick" not in html


def test_a_drawer_title_is_escaped_and_the_id_must_be_a_plain_token() -> None:
    assert "<img" not in drawer("d", "<img src=x>", "")
    with pytest.raises(ValueError):
        drawer('x"onmouseover=1', "t", "")
    with pytest.raises(ValueError):
        drawer_trigger("has space", "t")


# --- split_button ------------------------------------------------------------


def test_a_split_button_with_no_extra_actions_is_just_the_primary() -> None:
    html = split_button(Action("Save", kind=""))
    assert "split" not in html and "<details" not in html


def test_a_split_button_puts_related_actions_behind_a_disclosure() -> None:
    html = split_button(Action("Save", kind=""),
                        (Action("Save and new", kind="ghost"), Action("Void", kind="danger")))
    assert '<details class="split-more">' in html
    assert 'aria-label="More actions for Save"' in html
    assert html.count('class="split-item"') == 2 and "Save and new" in html and "Void" in html


# --- status ------------------------------------------------------------------


@pytest.mark.parametrize(("tone", "glyph"), [("ok", "✓"), ("warn", "◆"), ("bad", "▲"),
                                              ("info", "●"), ("neutral", "–")])
def test_every_status_tone_has_a_glyph_and_its_text(tone: str, glyph: str) -> None:
    html = status("Overdue", tone)
    assert f"stat tone-{tone}" in html
    assert glyph in html and "Overdue" in html


def test_an_unknown_status_tone_is_neutral_not_reassuring() -> None:
    html = status("Mystery", "green")
    assert "tone-neutral" in html and "tone-ok" not in html and "Mystery" in html


def test_status_escapes_its_label() -> None:
    assert "<b>" not in status("<b>Late</b>", "bad")


# --- form_shell -------------------------------------------------------------


def test_form_shell_puts_save_save_and_new_and_cancel_in_the_footer() -> None:
    html = form_shell("New invoice", "<label>Customer</label><input name='c'>",
                      primary=Action("Save", kind=""),
                      secondary=(Action("Save and new", kind="ghost"),
                                 Action("Cancel", href="/t/acme/invoices")))
    foot = html.split('class="form-foot"', 1)[1]
    assert "Save and new" in foot and "Cancel" in foot and ">Save</button>" in foot
    assert foot.index("Save and new") < foot.index("Cancel") < foot.index(">Save</button>")
    assert 'class="form-shell" method="post"' in html
    assert "New invoice" in html and "form-body" in html


def test_form_shell_escapes_its_title_and_places_the_aside() -> None:
    html = form_shell("<i>x</i>", "", primary=Action("Save", kind=""), aside="<p>Tip</p>")
    assert "&lt;i&gt;x&lt;/i&gt;" in html
    assert 'class="form-aside"><p>Tip</p>' in html


# --- none of it needs an inline style or a script ----------------------------


def test_the_components_emit_no_inline_styles_scripts_or_handlers() -> None:
    pieces = [
        data_table(GEAR_COLS, [{"name": "A", "amount": "-$1.00", "ref": "R"}],
                   sort=SortState("amount"), filters=[FilterChip("s", "S")],
                   page=Page(1, 1), totals={"amount": "-$1.00"}, base="/x",
                   query={"s": "1", "cols": "1", "c_ref": "1"}),
        money_bar(SEGS, base="/x"),
        drawer("d", "t", "b"),
        split_button(Action("Save", kind=""), (Action("Void"),)),
        status("ok", "ok"),
        form_shell("t", "b", primary=Action("Save", kind=""),
                   secondary=(Action("Cancel", href="/x"),), aside="a"),
    ]
    for html in pieces:
        assert 'style="' not in html
        assert "<script" not in html
        assert "onclick" not in html and "onchange" not in html


# --- the screens: invoices and bills -----------------------------------------

INVOICES = {
    "documents": [
        {"id": "INV-A", "party_id": "north", "date": "2024-01-01", "due_date": "2024-01-15",
         "total_minor": "50000", "open_minor": "50000", "status": "OPEN"},
        {"id": "INV-B", "party_id": "north", "date": "2030-01-01", "due_date": "2031-01-01",
         "total_minor": "120000", "open_minor": "120000", "status": "OPEN"},
        {"id": "INV-C", "party_id": "north", "date": "2024-02-01", "due_date": "2024-02-01",
         "total_minor": "30000", "open_minor": "0", "status": "PAID"},
        {"id": "INV-D", "party_id": "north", "date": "2030-02-01", "due_date": "2031-02-01",
         "total_minor": "90000", "open_minor": "90000", "status": "OPEN"},
    ],
    "open_total_minor": "260000",
    "overdue_total_minor": "50000",
}
PARTIES = {"parties": [{"id": "north", "name": "Northwind Ltd"}]}
BILLS = {
    "documents": [
        {"id": "BILL-1", "party_id": "north", "date": "2024-03-01", "due_date": "2024-03-31",
         "total_minor": "34999", "open_minor": "34999", "status": "OPEN"},
    ],
    "open_total_minor": "34999",
    "overdue_total_minor": "34999",
}


def _list_routes(docs: dict[str, Any] | None = None) -> dict[str, Any]:
    routes: dict[str, Any] = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/invoices"] = (200, docs if docs is not None else INVOICES)
    routes["GET /t/acme/bills"] = (200, BILLS)
    routes["GET /t/acme/customers"] = (200, PARTIES)
    routes["GET /t/acme/vendors"] = (200, PARTIES)
    return routes


def _table_ids(body: str) -> list[str]:
    tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return re.findall(r"<strong>([^<]+)</strong>", tbody)


def test_the_invoice_money_bar_filters_the_list_to_overdue() -> None:
    app, _t, _a = _app(_list_routes())
    page = _req(app, "/t/acme/invoices", method="GET").body
    assert _table_ids(page) == ["INV-A", "INV-B", "INV-C", "INV-D"]
    assert 'class="money-bar"' in page and 'href="/t/acme/invoices?status=overdue"' in page

    overdue = _req(app, "/t/acme/invoices?status=overdue").body
    assert _table_ids(overdue) == ["INV-A"]
    assert 'aria-current="true"' in overdue and "Showing" in overdue, "the active segment says so"


def test_the_invoice_money_bar_segments_sum_the_documents_they_hold() -> None:
    app, _t, _a = _app(_list_routes())
    page = _req(app, "/t/acme/invoices").body
    bar = page.split('class="money-bar"', 1)[1].split("</nav>", 1)[0]
    assert "$500.00" in bar and "1 invoice" in bar            # overdue: INV-A alone
    assert "$2,100.00" in bar and "2 invoices" in bar         # not yet due: INV-B + INV-D
    assert "$300.00" in bar                                   # paid: INV-C's total


def test_sorting_invoices_by_open_amount_in_both_directions() -> None:
    app, _t, _a = _app(_list_routes())
    desc = _req(app, "/t/acme/invoices?sort=open&dir=desc").body
    assert _table_ids(desc) == ["INV-B", "INV-D", "INV-A", "INV-C"]
    assert 'href="/t/acme/invoices?sort=open&amp;dir=asc"' in desc, "the header toggles"
    asc = _req(app, "/t/acme/invoices?sort=open&dir=asc").body
    assert _table_ids(asc) == ["INV-C", "INV-A", "INV-D", "INV-B"]


def test_sorting_invoices_by_total_uses_the_minor_units_not_the_text() -> None:
    app, _t, _a = _app(_list_routes())
    asc = _req(app, "/t/acme/invoices?sort=total&dir=asc").body
    assert _table_ids(asc) == ["INV-C", "INV-A", "INV-D", "INV-B"]


def test_an_empty_filter_shows_the_empty_state_not_a_blank_table() -> None:
    only_open = {"documents": [d for d in INVOICES["documents"] if d["id"] in ("INV-B", "INV-D")],
                 "open_total_minor": "210000", "overdue_total_minor": "0"}
    app, _t, _a = _app(_list_routes(only_open))
    page = _req(app, "/t/acme/invoices?status=paid").body
    assert _table_ids(page) == []
    assert "No invoices match this filter." in page and 'class="empty"' in page
    assert "Clear filters" in page


def test_a_list_with_no_invoices_still_says_so() -> None:
    app, _t, _a = _app(_list_routes({"documents": [], "open_total_minor": "0"}))
    page = _req(app, "/t/acme/invoices").body
    assert "No invoices yet." in page


def test_the_invoice_amounts_are_right_aligned_and_the_overdue_one_says_overdue() -> None:
    app, _t, _a = _app(_list_routes())
    page = _req(app, "/t/acme/invoices").body
    assert '<td class="num">$500.00</td>' in page
    assert "Overdue" in page and "Part paid" not in page
    assert "overdue $500.00" in page
    assert "$3,925.00" not in page  # not a fixture amount: no stale figure leaks in


def test_bills_list_uses_the_same_table_and_money_bar() -> None:
    app, _t, _a = _app(_list_routes())
    page = _req(app, "/t/acme/bills").body
    assert _table_ids(page) == ["BILL-1"]
    assert 'href="/t/acme/bills?status=overdue"' in page
    assert "1 bill" in page and "You owe" in page
    assert _table_ids(_req(app, "/t/acme/bills?status=paid").body) == []
    assert "No bills match this filter." in _req(app, "/t/acme/bills?status=paid").body


def test_the_money_bar_and_the_table_toolbar_carry_no_inline_styles() -> None:
    # The payment forms inside the rows keep their existing inline styles; the
    # components this slice added must not.
    app, _t, _a = _app(_list_routes())
    body = _req(app, "/t/acme/invoices?sort=open").body
    bar = body.split('<nav class="money-bar"', 1)[1].split("</nav>", 1)[0]
    toolbar = body.split('<div class="dt-bar">', 1)[1].split('<div class="table-scroll">', 1)[0]
    assert 'style="' not in bar and 'style="' not in toolbar
    head = body.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert 'style="' not in head
