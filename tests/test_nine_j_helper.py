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
    assert mo.x_rows == [5, 6]
    assert tabs["S-Beams"].layout.part_copy is None


def test_plan_uses_item_number_net_qty_and_only_three_columns():
    doc = parse_text(SAMPLE_TEXT)
    plan = planner.build_plan(doc, planner.load_tabs(fake_rows()))
    by = {(pl.line.sheet_part, pl.line.delivery_date): pl for pl in plan.lines}
    assert by[("010545-1", date(2026, 10, 28))].status == planner.DUPLICATE
    # Brand-new part: PO is ignored, so the user must choose a tab
    assert [pl.line.sheet_part for pl in plan.unrouted] == ["NEWPART-9"]
    assert set(plan.unrouted[0].candidates) == {"Machined Only", "S-Beams"}
    assert by[("010545-1", date(2026, 11, 4))].row == 5
    assert by[("010545-1", date(2027, 1, 4))].row == 6
    tw = plan.writes["Machined Only"]
    assert tw.insert_count == 0
    assert tw.rows[5] == {0: "010545-1", 2: date(2026, 11, 4), 3: 28.0}
    assert tw.rows[6] == {0: "010545-1", 2: date(2027, 1, 4), 3: 84.0}  # Net Req Qty, not 1,084


def test_override_routes_new_part_and_overflow_inserts_rows():
    doc = parse_text(SAMPLE_TEXT)
    tabs = planner.load_tabs(fake_rows())
    plan = planner.build_plan(doc, tabs, {"NEWPART-9": "Machined Only"})
    assert not plan.unrouted
    newp = [pl for pl in plan.lines if pl.line.sheet_part == "NEWPART-9"][0]
    assert newp.line.part == "NP9-REF"  # the line's own Part column is ignored
    assert newp.tab == "Machined Only" and newp.row == 6
    tw = plan.writes["Machined Only"]
    assert tw.rows[6] == {0: "NEWPART-9", 2: date(2026, 12, 1), 3: 10.0}
    assert (tw.insert_at, tw.insert_count) == (7, 1)  # 3 new lines, only 2 x rows
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
    assert tw.text_colors == {5: "BLUE", 7: "BLUE"}  # row 6 = brand-new part: left alone
    assert tw.part_col == 0 and tw.border_cols == 10


def test_text_color_default_when_latest_row_is_uncolored():
    tabs = planner.load_tabs(fake_rows(), {"Machined Only": {3: "RED"}})
    tw = planner.build_plan(parse_text(SAMPLE_TEXT), tabs).writes["Machined Only"]
    assert tw.text_colors == {5: None, 6: None}  # explicit default, overriding any x-row color


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
    dups = [(pl.line.sheet_part, pl.line.delivery_date) for pl in plan.lines if pl.status == planner.DUPLICATE]
    assert sorted(dups) == [("010545-1", date(2026, 10, 28)), ("010545-2", date(2026, 10, 28))]
    assert sorted(pl.row for pl in plan.lines if pl.status == planner.NEW) == list(range(35, 45))
    tw = plan.writes["Machined Only"]
    # Machined Only no longer has placeholder "x" rows (the filter range now grows with the data),
    # so all 10 new lines are inserted below the last data row (row 34) and join the filter.
    assert not xlsx_book.load_order_tabs(SAMPLE_XLSX)["Machined Only"].x_rows
    assert (tw.insert_at, tw.insert_count) == (35, 10)
    jan =[c for c in tw.rows.values() if c[0] == "009956" and c[2] == date(2027, 1, 4)][0]
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
    # The tab's filter covers rows 2-6 (0-based end 6): the inserted row 7 sits right below it.
    book.meta = {"sheets": [{"properties": {"sheetId": 1}, "basicFilter": {
        "range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 6, "endColumnIndex": 10},
        "criteria": {"0": {}},
        "filterSpecs": [{"columnIndex": 0, "filterCriteria": {}}]}}]}
    n = google_book.apply(book, plan)
    assert n == 3
    # Last request: after the rows are written, the filter grows to include the inserted row.
    assert book.batches[-1]["requests"] == [
        {"setBasicFilter": {"filter": {
            "range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 7, "endColumnIndex": 10},
            "filterSpecs": [{"columnIndex": 0, "filterCriteria": {}}]}}}]
    assert book.batches[0]["requests"] == [
        {"insertDimension": {
            "range": {"sheetId": 1, "dimension": "ROWS", "startIndex": 6, "endIndex": 7},
            "inheritFromBefore": True}},
        _color_req(5, BLUE),  # 010545-1 keeps its blue; row 6 (new part) untouched
        _color_req(7, BLUE),
        _border_req(5, 7),    # x rows 5-6 + inserted row 7 form one block
    ]
    assert book.values["valueInputOption"] == "USER_ENTERED"
    first = book.values["data"][0]
    assert first["range"] == "'Machined Only'!A5:D5"
    assert first["values"][0] == ["010545-1", None, "11/4/2026", 28.0]  # B (Location) untouched


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


def test_google_apply_without_insert_sets_default_color_and_borders():
    rows = fake_rows()
    book = FakeBook(rows)
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), planner.load_tabs(rows))
    assert google_book.apply(book, plan) == 2
    black = {"rgbColor": {}}
    assert book.batch["requests"] == [_color_req(5, black), _color_req(6, black), _border_req(5, 6)]


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
