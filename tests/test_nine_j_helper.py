import os
from datetime import date

import pytest

from nine_j_helper import google_book, planner
from nine_j_helper.config import spreadsheet_id_from
from nine_j_helper.nine_j import NineJParseError, parse_text

# Real sample files are kept out of git; point NINEJ_SAMPLES at the folder holding them.
ROOT = os.environ.get("NINEJ_SAMPLES") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_PDF = os.path.join(ROOT, "Example 9J", "9J_504206_R M KERNER COMPANY 106096.pdf")
SAMPLE_XLSX = os.path.join(ROOT, "Copy of HAUX - RMKCo.xlsx")

SAMPLE_TEXT = """Supplier Schedule
Vendor ID: 504206 Buyer: Ryan Beck
Vendor Name: R M KERNER COMPANY Release Date: 11/06/26
Attention: CHRIS KERNER Release ID: 20261106-001
Last Receipt Date Receipt Qty Hendrickson Item # Item Description
10/20/26 28.0 010545-1 UPPER KINGPIN PLATE
P.O. # Supplier Item # Item Revision
106096 010545-1 C
Interval Deliver Date Part Reference F P ir l m an / Req Qty Cum Req Qty Net Req Qty
Daily 10/28/26 010545-1 F* 28.0 528.0 28.0
11/04/26 010545-1 F* 28.0 556.0 28.0
Monthly01/04/27 010545-1 P 1,084.0 1,640.0 84.0
Last Receipt Date Receipt Qty Hendrickson Item # Item Description
NEWPART-9 BRAND NEW THING
P.O.# Supplier Item # Item Revision
106096 NEWPART-9 A
Weekly 12/01/26 NP9-REF ADDED 11/1 F 10.0 10.0 10.0
"""


def _d(y, m, d):
    return planner.to_serial(date(y, m, d))


def fake_rows():
    return {
        "Keys": [["Pns that equal", None, "Ship to"]],
        "Machined Only": [
            ["PO 106096", None, "firm orders have BOLD due dates"],
            ["Part Number", "Location", "Deliver Date", "Req Qty", "Ship to Customer", "Shipped Qty",
             "Balance Due", "Actual Ship Date", "P/S", "Comments", "part number"],
            ["010545-1", "HAUX", _d(2026, 10, 14), 28, 1, 28, 0, None, None, None, "010545-1"],
            ["010545-1", "HAUX", _d(2026, 10, 28), 28, 1, None, 28, None, None, None, "010545-1"],
            ["x"], ["x"],
        ],
        "S-Beams": [
            ["PO 107564"], [], [],
            ["Part number", "Location", "Delivery Date", "Req Qty", "Ship to Customer", "Shipped Qty",
             "Net Req Qty", "Actual Ship Date", "P/S", "Comments"],
            ["020823", "C", _d(2026, 10, 1), 50, 1, 50, 0],
        ],
    }


# ------------------------------------------------------------------ parser

def test_parse_text_header_and_lines():
    doc = parse_text(SAMPLE_TEXT)
    assert doc.vendor_id == "504206"
    assert doc.vendor_name == "R M KERNER COMPANY"
    assert doc.buyer == "Ryan Beck"
    assert doc.release_date == date(2026, 11, 6)
    assert doc.release_id == "20261106-001"
    assert doc.pos == ["106096"]
    assert len(doc.items) == 2
    lines = doc.lines
    assert len(lines) == 4
    assert [ln.interval for ln in lines] == ["Daily", "Daily", "Monthly", "Weekly"]
    assert lines[2].req_qty == 1084.0 and lines[2].firm_plan == "P"
    assert lines[0].is_firm and lines[0].is_new
    assert lines[3].reference == "ADDED 11/1" and lines[3].firm_plan == "F"
    assert doc.items[1].last_receipt_date is None
    assert all(ln.po == "106096" for ln in lines)


def test_parse_text_rejects_non_9j():
    with pytest.raises(NineJParseError):
        parse_text("Hello\nThis is an invoice\n")


@pytest.mark.skipif(not os.path.exists(SAMPLE_PDF), reason="sample 9J not present")
def test_parse_sample_pdf():
    from nine_j_helper.nine_j import parse_pdf
    doc = parse_pdf(SAMPLE_PDF)
    assert doc.release_id == "20261106-001"
    assert doc.pos == ["106096"]
    assert [it.hendrickson_item for it in doc.items] == ["010545-1", "010545-2", "009956"]
    assert len(doc.lines) == 12
    assert sum(1 for ln in doc.lines if ln.is_firm) == 4


# ------------------------------------------------------------------ planner

def test_layout_detection_skips_non_order_tabs():
    tabs = planner.load_tabs(fake_rows())
    assert set(tabs) == {"Machined Only", "S-Beams"}
    mo = tabs["Machined Only"]
    assert mo.layout.header_row == 2
    assert (mo.layout.part, mo.layout.location, mo.layout.date, mo.layout.req) == (0, 1, 2, 3)
    assert (mo.layout.ship_to, mo.layout.shipped, mo.layout.net, mo.layout.part_copy) == (4, 5, 6, 10)
    assert (mo.layout.actual_date, mo.layout.ps) == (7, 8)
    assert mo.x_rows == [5, 6]
    assert mo.unshipped_rows == [4]  # row 3 shipped 28; row 4 has no F/H/I
    assert mo.existing == {("10545-1", _d(2026, 10, 14))}  # shipped rows only
    assert tabs["S-Beams"].layout.part_copy is None


def test_plan_replaces_unshipped_row_and_writes_formulas():
    doc = parse_text(SAMPLE_TEXT)
    plan = planner.build_plan(doc, planner.load_tabs(fake_rows()))
    by = {(pl.line.sheet_part, pl.line.delivery_date): pl for pl in plan.lines}
    # Row 4 (10/28) is unshipped, so it is replaced rather than "already on sheet".
    assert by[("010545-1", date(2026, 10, 28))].status == planner.NEW
    assert [(r.tab, r.row, r.part) for r in plan.removed] == [("Machined Only", 4, "010545-1")]
    # Brand-new part: PO is ignored, so the user must choose a tab
    assert [pl.line.sheet_part for pl in plan.unrouted] == ["NEWPART-9"]
    assert set(plan.unrouted[0].candidates) == {"Machined Only", "S-Beams"}
    # Row 4 is deleted first, so the old x rows 5-6 become 4-5 and the third line overflows to 6.
    assert [by[("010545-1", d)].row for d in (date(2026, 10, 28), date(2026, 11, 4), date(2027, 1, 4))] == [4, 5, 6]
    tw = plan.writes["Machined Only"]
    assert tw.delete_rows == [4] and tw.expected_deleted == {4: "10545-1"}
    assert tw.expected_x_rows == [5, 6] and tw.last_data_row == 3
    assert (tw.insert_at, tw.insert_count) == (6, 1)
    assert tw.rows[4] == {0: "010545-1", 2: date(2026, 10, 28), 3: 28.0,
                          4: "=WORKDAY(C4,-2)", 6: "=D4-F4", 10: "010545-1"}
    assert tw.rows[6][3] == 84.0  # Net Req Qty, not 1,084
    assert tw.rows[6][4] == "=WORKDAY(C6,-2)"  # formulas use the row's own number
    assert tw.rows[6][6] == "=D6-F6"


def test_formulas_skip_columns_a_tab_does_not_have():
    tabs = planner.load_tabs(fake_rows())
    sb = tabs["S-Beams"].layout  # no part-number copy column
    assert planner.formula_cells(sb, 9, "020823") == {4: "=WORKDAY(C9,-2)", 6: "=D9-F9"}
    assert planner.formula_cells(tabs["Machined Only"].layout, 9, "X1")[10] == "X1"


def test_unshipped_needs_all_of_shipped_ship_date_and_ps_blank():
    lay = planner.load_tabs(fake_rows())["Machined Only"].layout
    row = lambda **kw: ["P", "HAUX", 46000, 28, "f", kw.get("f"), "g", kw.get("h"), kw.get("i")]
    assert planner.is_unshipped(row(), lay)
    assert planner.is_unshipped(row(f="  ", h="", i=None), lay)
    assert not planner.is_unshipped(row(f=28), lay)
    assert not planner.is_unshipped(row(f="=38+66+58"), lay)  # a formula counts as filled
    assert not planner.is_unshipped(row(h=46010), lay)
    assert not planner.is_unshipped(row(i=34465), lay)
    assert not planner.is_unshipped(row(f=0), lay)  # 0 shipped is still filled in
    no_shipped = planner.TabLayout(name="t", header_row=1, part=0, date=2)
    assert not planner.is_unshipped(row(), no_shipped)  # can't tell, so never delete


def _rows_for_replace():
    return {"Machined Only": [
        ["Part Number", "Location", "Deliver Date", "Req Qty", "Ship to Customer", "Shipped Qty",
         "Balance Due", "Actual Ship Date", "P/S", "Comments", "part number"],
        ["010545-1", "HAUX", _d(2026, 9, 9), 28, 1, None, 28, None, None, None, "010545-1"],  # dropped date
        ["010545-1", "HAUX", _d(2026, 10, 14), 28, 1, 28, 0, _d(2026, 10, 12), 1, None, "010545-1"],  # shipped
        ["010545-2", "HAUX", _d(2026, 10, 14), 28, 1, None, 28, None, None, None, "010545-2"],  # not on 9J
        ["010545-1", "HAUX", _d(2026, 10, 28), 28, 1, None, 28, None, None, None, "010545-1"],
        ["x"], ["x"],
    ]}


def test_only_parts_on_the_9j_lose_unshipped_rows_and_dropped_dates_go_too():
    tabs = planner.load_tabs(_rows_for_replace())
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), tabs)  # 9J has 010545-1 (+ an unrouted part)
    tw = plan.writes["Machined Only"]
    # row 2 (9/9, date no longer on the 9J) and row 5 (10/28) go; the other part's unshipped row stays
    assert tw.delete_rows == [2, 5]
    assert [r.row for r in plan.removed] == [2, 5]
    # Rows 3, 4 stay and become 2, 3; x rows 6-7 become 4-5; last order is row 3.
    assert tw.last_data_row == 3
    assert min(tw.rows) == 4


def test_9j_line_matching_a_shipped_row_is_skipped():
    text = SAMPLE_TEXT.replace("Daily 10/28/26", "Daily 10/14/26")
    plan = planner.build_plan(parse_text(text), planner.load_tabs(_rows_for_replace()))
    st = {(pl.line.delivery_date, pl.status) for pl in plan.lines if pl.line.sheet_part == "010545-1"}
    assert (date(2026, 10, 14), planner.DUPLICATE) in st
    # the unshipped 10/14 row of the OTHER part is irrelevant; shipped 010545-1 row 3 is untouched
    assert 3 not in plan.writes["Machined Only"].delete_rows


def test_deleting_a_middle_row_shifts_the_slots_up():
    rows = {"Machined Only": [
        ["Part Number", "Location", "Deliver Date", "Req Qty", "Ship to Customer", "Shipped Qty",
         "Balance Due", "Actual Ship Date", "P/S", "Comments", "part number"],
        ["010545-1", "HAUX", _d(2026, 9, 9), 28, 1, None, 28, None, None, None, "010545-1"],
        ["010545-2", "HAUX", _d(2026, 9, 9), 28, 1, 28, 0, _d(2026, 9, 8), 1, None, "010545-2"],
        ["x"], ["x"], ["x"], ["x"],
    ]}
    text = SAMPLE_TEXT.replace("NEWPART-9", "010545-2")
    tw = planner.build_plan(parse_text(text), planner.load_tabs(rows)).writes["Machined Only"]
    assert tw.delete_rows == [2]
    assert tw.expected_x_rows == [4, 5, 6, 7]
    assert sorted(tw.rows) == [3, 4, 5, 6] and tw.insert_count == 0  # x rows move up one
    assert tw.last_data_row == 2  # the shipped 010545-2 row, now row 2


def test_override_routes_new_part_and_overflow_inserts_rows():
    doc = parse_text(SAMPLE_TEXT)
    tabs = planner.load_tabs(fake_rows())
    plan = planner.build_plan(doc, tabs, {"NEWPART-9": "Machined Only"})
    assert not plan.unrouted
    newp = [pl for pl in plan.lines if pl.line.sheet_part == "NEWPART-9"][0]
    assert newp.line.part == "NP9-REF"  # the line's own Part column is ignored
    assert newp.tab == "Machined Only" and newp.row == 6
    tw = plan.writes["Machined Only"]
    assert {c: tw.rows[6][c] for c in (0, 2, 3)} == {0: "NEWPART-9", 2: date(2026, 12, 1), 3: 10.0}
    assert (tw.insert_at, tw.insert_count) == (6, 2)  # 4 lines; row 4 deleted, x rows now 4-5
    assert tw.expected_x_rows == [5, 6]
    plan = planner.build_plan(doc, tabs, {"NEWPART-9": "S-Beams"})
    newp = [pl for pl in plan.lines if pl.line.sheet_part == "NEWPART-9"][0]
    assert newp.tab == "S-Beams" and newp.row == 6  # no x rows -> inserted after last order


def test_text_color_comes_from_parts_latest_row():
    doc = parse_text(SAMPLE_TEXT)
    # rows 3 and 4 are both 010545-1; the later one (row 4) wins
    tabs = planner.load_tabs(fake_rows(), {"Machined Only": {3: "RED", 4: "BLUE"}})
    plan = planner.build_plan(doc, tabs, {"NEWPART-9": "Machined Only"})
    tw = plan.writes["Machined Only"]
    assert tw.text_colors == {4: "BLUE", 5: "BLUE", 7: "BLUE"}  # row 6 = brand-new part: left alone
    assert tw.part_col == 0 and tw.border_cols == 10


def test_text_color_default_when_latest_row_is_uncolored():
    tabs = planner.load_tabs(fake_rows(), {"Machined Only": {3: "RED"}})
    tw = planner.build_plan(parse_text(SAMPLE_TEXT), tabs).writes["Machined Only"]
    assert tw.text_colors == {4: None, 5: None, 6: None}  # explicit default, overriding any x-row color


def test_row_blocks_split_non_consecutive_rows():
    tw = planner.TabWrite(tab="t", rows={9: {}, 5: {}, 6: {}, 11: {}, 10: {}})
    assert tw.row_blocks() == [(5, 6), (9, 11)]


def test_part_key_tolerates_lost_leading_zeros():
    assert planner.part_key("009956") == planner.part_key(9956.0)


@pytest.mark.skipif(not (os.path.exists(SAMPLE_PDF) and os.path.exists(SAMPLE_XLSX)),
                    reason="sample files not present")
def test_sample_against_export():
    from nine_j_helper import xlsx_book
    from nine_j_helper.nine_j import parse_pdf
    plan = planner.build_plan(parse_pdf(SAMPLE_PDF), xlsx_book.load_order_tabs(SAMPLE_XLSX))
    assert {pl.tab for pl in plan.lines} == {"Machined Only"}
    tw0 = plan.writes["Machined Only"]
    colors = {tw0.rows[r][0]: tw0.text_colors[r] for r in tw0.rows}
    assert colors == {"010545-1": None, "010545-2": "FF0000FF", "009956": "FFFF00FF"}
    # Machined Only rows 33/34 (10/28, unshipped) are replaced by the 9J's lines, so nothing is a dup.
    assert not [pl for pl in plan.lines if pl.status == planner.DUPLICATE]
    assert [(r.row, r.part) for r in plan.removed] == [(33, "010545-1"), (34, "010545-2")]
    assert sorted(pl.row for pl in plan.lines if pl.status == planner.NEW) == list(range(33, 45))
    tw = plan.writes["Machined Only"]
    # Machined Only has no placeholder "x" rows (the filter range grows with the data), so after
    # rows 33-34 are removed all 12 lines are inserted below the last order (row 32).
    assert not xlsx_book.load_order_tabs(SAMPLE_XLSX)["Machined Only"].x_rows
    assert tw.delete_rows == [33, 34] and tw.last_data_row == 32
    assert (tw.insert_at, tw.insert_count) == (33, 12)
    first = tw.rows[33]
    assert (first[0], first[4], first[6], first[10]) == (
        "010545-1", "=WORKDAY(C33,-2)", "=D33-F33", "010545-1")
    jan = [c for c in tw.rows.values() if c[0] == "009956" and c[2] == date(2027, 1, 4)][0]
    assert jan[3] == 184.0  # Net Req Qty (the 9J's Req Qty is 193)


# ------------------------------------------------------------------ google backend

def test_cell_value_protects_numeric_text():
    assert google_book._cell_value("009956") == "'009956"
    assert google_book._cell_value("010545-1") == "010545-1"
    assert google_book._cell_value("=D5-F5") == "=D5-F5"
    assert google_book._cell_value(46330) == 46330
    assert google_book._cell_value(date(2026, 11, 4)) == "11/4/2026"


class FakeWS:
    def __init__(self, title, id_):
        self.title, self.id = title, id_


class FakeBook:
    def __init__(self, rows):
        self.rows = rows
        self.batch = None
        self.batches = []
        self.values = None
        self.meta = {"sheets": []}

    def worksheets(self):
        return [FakeWS(t, i) for i, t in enumerate(self.rows)]

    def values_batch_get(self, ranges, params=None):
        return {"valueRanges": [{"values": self.rows[t]} for t in self.rows]}

    def batch_update(self, body):
        self.batch = body
        self.batches.append(body)

    def values_batch_update(self, body):
        self.values = body

    def fetch_sheet_metadata(self, params=None):
        self.meta_params = params
        return self.meta


BLUE = {"rgbColor": {"blue": 1}}
THIN = {"style": "SOLID", "colorStyle": {"rgbColor": {}}}


def _border_req(first, last, sheet_id=1):
    return {"updateBorders": {
        "range": {"sheetId": sheet_id, "startRowIndex": first - 1, "endRowIndex": last,
                  "startColumnIndex": 0, "endColumnIndex": 10},
        "top": THIN, "bottom": THIN, "left": THIN, "right": THIN,
        "innerHorizontal": THIN, "innerVertical": THIN}}


def _color_req(row, style, sheet_id=1):
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": row - 1, "endRowIndex": row,
                  "startColumnIndex": 0, "endColumnIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"foregroundColorStyle": style}}},
        "fields": "userEnteredFormat.textFormat.foregroundColorStyle"}}


def test_google_apply_builds_requests():
    rows = fake_rows()
    book = FakeBook(rows)
    tabs = planner.load_tabs(rows, {"Machined Only": {4: BLUE}})
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), tabs, {"NEWPART-9": "Machined Only"})
    # The tab's filter covers rows 2-6 (0-based end 6). Row 4 is deleted (-> end 5), then rows
    # 6-7 are inserted just below it, so they are outside the filter until it is extended.
    book.meta = {"sheets": [{"properties": {"sheetId": 1}, "basicFilter": {
        "range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 6, "endColumnIndex": 10},
        "criteria": {"0": {}},
        "filterSpecs": [{"columnIndex": 0, "filterCriteria": {}}]}}]}
    n = google_book.apply(book, plan)
    assert n == 4
    # Last request: after the rows are written, the filter grows to include the inserted rows.
    assert book.batches[-1]["requests"] == [
        {"setBasicFilter": {"filter": {
            "range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 7, "endColumnIndex": 10},
            "filterSpecs": [{"columnIndex": 0, "filterCriteria": {}}]}}}]
    assert book.batches[0]["requests"] == [
        {"deleteDimension": {  # the stale unshipped row 4 goes first
            "range": {"sheetId": 1, "dimension": "ROWS", "startIndex": 3, "endIndex": 4}}},
        {"insertDimension": {  # then 2 rows after the (shifted) x rows 4-5
            "range": {"sheetId": 1, "dimension": "ROWS", "startIndex": 5, "endIndex": 7},
            "inheritFromBefore": False}},
        _color_req(4, BLUE),  # 010545-1 keeps its blue; row 6 (new part) untouched
        _color_req(5, BLUE),
        _color_req(7, BLUE),
        _border_req(4, 7),    # x rows 4-5 + inserted rows 6-7 form one block
    ]
    assert book.values["valueInputOption"] == "USER_ENTERED"
    first = book.values["data"][0]
    assert first["range"] == "'Machined Only'!A4:K4"
    assert first["values"][0] == ["010545-1", None, "10/28/2026", 28.0, "=WORKDAY(C4,-2)", None,
                                  "=D4-F4", None, None, None, "010545-1"]  # B (Location) untouched


def test_filter_extension_grows_to_last_row():
    flt = {"range": {"startRowIndex": 1, "endRowIndex": 34, "endColumnIndex": 11},
           "criteria": {"0": {}}, "filterSpecs": [{"columnIndex": 0}], "sortSpecs": [{"x": 1}]}
    ext = google_book._filter_extension(flt, insert_at=None, insert_count=0, last_row=44)
    new = ext["setBasicFilter"]["filter"]
    assert new["range"]["endRowIndex"] == 44 and "criteria" not in new
    assert new["filterSpecs"] == flt["filterSpecs"] and new["sortSpecs"] == flt["sortSpecs"]
    assert flt["range"]["endRowIndex"] == 34  # input untouched
    # rows already within the filter: nothing to do
    assert google_book._filter_extension(flt, None, 0, last_row=30) is None
    # inserted inside the filter: Sheets grows it itself (34 -> 37), so 36 is already covered
    assert google_book._filter_extension(flt, insert_at=10, insert_count=3, last_row=36) is None
    assert google_book._filter_extension({}, None, 0, last_row=5) is None


def test_google_apply_sets_default_color_and_borders():
    rows = fake_rows()
    book = FakeBook(rows)
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), planner.load_tabs(rows))
    assert google_book.apply(book, plan) == 3
    black = {"rgbColor": {}}
    assert book.batch["requests"][2:] == [
        _color_req(4, black), _color_req(5, black), _color_req(6, black), _border_req(4, 6)]


def test_google_apply_deletes_bottom_up_without_inserting_when_x_rows_suffice():
    rows = {"Machined Only": _rows_for_replace()["Machined Only"]}
    rows["Machined Only"][6:6] = [["x"], ["x"]]  # plenty of placeholder rows
    book = FakeBook(rows)
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), planner.load_tabs(rows))
    assert google_book.apply(book, plan) == 3
    dels = [r["deleteDimension"]["range"] for r in book.batches[0]["requests"] if "deleteDimension" in r]
    assert [(d["startIndex"], d["endIndex"]) for d in dels] == [(4, 5), (1, 2)]  # row 5 then row 2
    assert not any("insertDimension" in r for r in book.batches[0]["requests"])


def test_google_apply_with_only_deletes_writes_no_values():
    hdr = fake_rows()["Machined Only"][1]
    shipped = lambda d: ["010545-1", "HAUX", _d(*d), 28, 1, 28, 0, _d(2026, 1, 1), 1, None, "010545-1"]
    rows = {"Machined Only": [
        hdr,
        ["010545-1", "HAUX", _d(2026, 9, 9), 28, 1, None, 28, None, None, None, "010545-1"],  # stale
        shipped((2026, 10, 28)), shipped((2026, 11, 4)), shipped((2027, 1, 4)),
    ]}
    book = FakeBook(rows)
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), planner.load_tabs(rows))
    assert plan.new_count == 0 and plan.removed_count == 1 and plan.has_changes
    assert google_book.apply(book, plan) == 0
    assert book.batches[0]["requests"][0]["deleteDimension"]["range"]["startIndex"] == 1
    assert book.values is None  # nothing to write


def test_google_apply_refuses_if_a_row_to_delete_was_shipped_meanwhile():
    rows = fake_rows()
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), planner.load_tabs(rows))
    rows["Machined Only"][3][5] = 28  # someone just filled in Shipped Qty on row 4
    with pytest.raises(google_book.SheetChangedError):
        google_book.apply(FakeBook(rows), plan)


def test_filter_extension_accounts_for_deleted_rows():
    flt = {"range": {"startRowIndex": 1, "endRowIndex": 34, "endColumnIndex": 11}}
    # rows 33-34 deleted inside the filter: it is 32 long now, so 44 needs an extension
    ext = google_book._filter_extension(flt, 33, 12, 44, deleted_rows=[33, 34])
    assert ext["setBasicFilter"]["filter"]["range"]["endRowIndex"] == 44
    # inserting inside the shrunken filter grows it by itself: 32 + 12 = 44, nothing to do
    assert google_book._filter_extension(flt, 20, 12, 44, deleted_rows=[33, 34]) is None


def test_read_part_colors_parses_grid_data():
    book = FakeBook(fake_rows())
    book.meta = {"sheets": [{"properties": {"title": "Machined Only"}, "data": [{
        "startRow": 1,  # first rowData entry is sheet row 2
        "rowData": [
            {"values": [{"userEnteredFormat": {"textFormat": {"foregroundColorStyle": BLUE}}}]},
            {},  # no values at all
            {"values": [{"userEnteredFormat": {"textFormat": {"foregroundColorStyle": {"rgbColor": {}}}}}]},
            {"values": [{"userEnteredFormat": {"textFormat": {"foregroundColor": {"red": 1}}}}]},
            {"values": [{"userEnteredFormat": {"textFormat": {"foregroundColorStyle": {"themeColor": "ACCENT1"}}}}]},
        ]}]}]}
    out = google_book.read_part_colors(book, ["Machined Only"])
    assert out == {"Machined Only": {2: BLUE, 5: {"rgbColor": {"red": 1}}, 6: {"themeColor": "ACCENT1"}}}
    assert book.meta_params["ranges"] == ["'Machined Only'!A:A"]
    assert book.meta_params["includeGridData"] == "true"


def test_load_order_tabs_reads_colors_only_for_order_tabs():
    book = FakeBook(fake_rows())
    book.meta = {"sheets": []}
    tabs = google_book.load_order_tabs(book)
    assert set(tabs) == {"Machined Only", "S-Beams"}
    assert book.meta_params["ranges"] == ["'Machined Only'!A:A", "'S-Beams'!A:A"]


def test_google_apply_refuses_if_sheet_changed():
    rows = fake_rows()
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), planner.load_tabs(rows))
    rows["Machined Only"][4] = ["999999", "HAUX"]  # someone typed into the x row
    with pytest.raises(google_book.SheetChangedError):
        google_book.apply(FakeBook(rows), plan)


def test_spreadsheet_id_from_url():
    url = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=0"
    assert spreadsheet_id_from(url) == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    assert spreadsheet_id_from("not a link") is None


# ------------------------------------------------------------------ xlsx backend

def test_xlsx_apply_deletes_rows_and_translates_formulas_below(tmp_path):
    import openpyxl
    from nine_j_helper import xlsx_book
    src, dst = str(tmp_path / "src.xlsx"), str(tmp_path / "out.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Machined Only"
    for r in fake_rows()["Machined Only"][:2]:
        ws.append(r)
    ws.append(["010545-1", "HAUX", date(2026, 9, 9), 28, "=WORKDAY(C3,-2)", None, "=D3-F3", None, None, None, "010545-1"])
    ws.append(["010545-2", "HAUX", date(2026, 9, 9), 28, "=WORKDAY(C4,-2)", 28, "=D4-F4", date(2026, 9, 8), 1, None, "010545-2"])
    ws.append(["x"])
    wb.save(src)
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), xlsx_book.load_order_tabs(src))
    assert plan.writes["Machined Only"].delete_rows == [3]
    xlsx_book.apply(plan, src, dst)
    out = openpyxl.load_workbook(dst)["Machined Only"]
    assert [out.cell(3, c).value for c in (1, 5, 7)] == ["010545-2", "=WORKDAY(C3,-2)", "=D3-F3"]  # moved up
    assert out.cell(4, 1).value == "010545-1" and out.cell(4, 5).value == "=WORKDAY(C4,-2)"  # new, in x row
    assert out.cell(4, 11).value == "010545-1" and out.cell(4, 4).value == 28.0
    assert openpyxl.load_workbook(src)["Machined Only"].cell(3, 1).value == "010545-1"  # source untouched
