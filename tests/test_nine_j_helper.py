import os
from datetime import date

import pytest

from nine_j_helper import google_book, planner
from nine_j_helper.config import spreadsheet_id_from
from nine_j_helper.nine_j import NineJParseError, parse_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
Weekly 12/01/26 NEWPART-9 ADDED 11/1 F 10.0 10.0 10.0
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


def test_plan_routes_dedupes_fills_x_rows_and_inserts_overflow():
    doc = parse_text(SAMPLE_TEXT)
    plan = planner.build_plan(doc, planner.load_tabs(fake_rows()))
    by = {(pl.line.part, pl.line.delivery_date): pl for pl in plan.lines}
    assert by[("010545-1", date(2026, 10, 28))].status == planner.DUPLICATE
    assert by[("010545-1", date(2026, 11, 4))].row == 5
    # Unknown part routed by PO in the tab title
    newp = by[("NEWPART-9", date(2026, 12, 1))]
    assert newp.tab == "Machined Only" and newp.status == planner.NEW
    assert newp.row == 6
    # Third new line overflows the two x rows -> inserted after them
    jan = by[("010545-1", date(2027, 1, 4))]
    assert jan.row == 7
    tw = plan.writes["Machined Only"]
    assert (tw.insert_at, tw.insert_count) == (7, 1)
    assert tw.expected_x_rows == [5, 6]
    assert tw.format_source_row == 4
    row5 = tw.rows[5]
    assert row5[0] == "010545-1" and row5[1] == "HAUX" and row5[2] == _d(2026, 11, 4)
    assert row5[3] == 28.0 and row5[4] == "=WORKDAY(C5,-2)" and row5[6] == "=D5-F5"
    assert row5[10] == "010545-1"
    assert 5 not in row5 and 7 not in row5  # Shipped Qty / Actual Ship Date left for dad


def test_unrouted_when_no_match_and_override_resolves():
    rows = fake_rows()
    rows["Machined Only"][0] = ["Machined parts"]  # no PO in title
    doc = parse_text(SAMPLE_TEXT)
    tabs = planner.load_tabs(rows)
    plan = planner.build_plan(doc, tabs)
    assert [pl.line.part for pl in plan.unrouted] == ["NEWPART-9"]
    plan = planner.build_plan(doc, tabs, {"NEWPART-9": "S-Beams"})
    assert not plan.unrouted
    pl = [p for p in plan.lines if p.line.part == "NEWPART-9"][0]
    assert pl.tab == "S-Beams" and pl.row == 6  # no x rows -> inserted after last order


def test_part_key_tolerates_lost_leading_zeros():
    assert planner.part_key("009956") == planner.part_key(9956.0)


@pytest.mark.skipif(not (os.path.exists(SAMPLE_PDF) and os.path.exists(SAMPLE_XLSX)),
                    reason="sample files not present")
def test_sample_against_export():
    from nine_j_helper import xlsx_book
    from nine_j_helper.nine_j import parse_pdf
    plan = planner.build_plan(parse_pdf(SAMPLE_PDF), planner.load_tabs(xlsx_book.read_rows(SAMPLE_XLSX)))
    assert {pl.tab for pl in plan.lines} == {"Machined Only"}
    dups = [(pl.line.part, pl.line.delivery_date) for pl in plan.lines if pl.status == planner.DUPLICATE]
    assert sorted(dups) == [("010545-1", date(2026, 10, 28)), ("010545-2", date(2026, 10, 28))]
    assert sorted(pl.row for pl in plan.lines if pl.status == planner.NEW) == list(range(35, 45))
    assert plan.writes["Machined Only"].insert_count == 0


# ------------------------------------------------------------------ google backend

def test_cell_value_protects_numeric_text():
    assert google_book._cell_value("009956") == "'009956"
    assert google_book._cell_value("010545-1") == "010545-1"
    assert google_book._cell_value("=D5-F5") == "=D5-F5"
    assert google_book._cell_value(46330) == 46330


class FakeWS:
    def __init__(self, title, id_):
        self.title, self.id = title, id_


class FakeBook:
    def __init__(self, rows):
        self.rows = rows
        self.batch = None
        self.values = None

    def worksheets(self):
        return [FakeWS(t, i) for i, t in enumerate(self.rows)]

    def values_batch_get(self, ranges, params=None):
        return {"valueRanges": [{"values": self.rows[t]} for t in self.rows]}

    def batch_update(self, body):
        self.batch = body

    def values_batch_update(self, body):
        self.values = body


def test_google_apply_builds_requests():
    rows = fake_rows()
    book = FakeBook(rows)
    plan = planner.build_plan(parse_text(SAMPLE_TEXT), planner.load_tabs(rows))
    n = google_book.apply(book, plan)
    assert n == 3
    reqs = book.batch["requests"]
    assert reqs[0]["insertDimension"]["range"] == {"sheetId": 1, "dimension": "ROWS", "startIndex": 6, "endIndex": 7}
    assert sum(1 for r in reqs if "copyPaste" in r) == 3
    assert book.values["valueInputOption"] == "USER_ENTERED"
    first = book.values["data"][0]
    assert first["range"] == "'Machined Only'!A5:K5"
    vals = first["values"][0]
    assert vals[5] is None and vals[7] is None  # untouched cells


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
