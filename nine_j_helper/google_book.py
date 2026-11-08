"""Google Sheets backend."""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List

import gspread

from . import config
from .planner import Plan, col_letter, load_tabs, order_tab_names

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetChangedError(Exception):
    pass


def connect() -> gspread.Client:
    secret = config.client_secret_path()
    if not secret:
        raise FileNotFoundError(
            "Google sign-in isn't set up: client_secret.json was not found. "
            "See the README section 'Google setup'.")
    # Opens the browser for sign-in the first time; afterwards uses the saved token.
    return gspread.oauth(scopes=SCOPES, credentials_filename=secret,
                         authorized_user_filename=config.token_path())


def open_book(client: gspread.Client, spreadsheet_id: str) -> gspread.Spreadsheet:
    return client.open_by_key(spreadsheet_id)


def read_rows(book: gspread.Spreadsheet) -> Dict[str, List[List]]:
    worksheets = book.worksheets()
    ranges = ["'" + ws.title.replace("'", "''") + "'" for ws in worksheets]
    resp = book.values_batch_get(ranges, params={
        "valueRenderOption": "UNFORMATTED_VALUE",
        "dateTimeRenderOption": "SERIAL_NUMBER",
    })
    out = {}
    for ws, vr in zip(worksheets, resp.get("valueRanges", [])):
        out[ws.title] = vr.get("values", [])
    return out


_BLACK = {"rgbColor": {}}


def _color_style(text_format: Dict):
    """Text color of a cell as a Sheets ColorStyle, or None for default/black."""
    style = text_format.get("foregroundColorStyle")
    if not style and text_format.get("foregroundColor"):
        style = {"rgbColor": text_format["foregroundColor"]}
    if not style or style == _BLACK:
        return None
    rgb = style.get("rgbColor")
    if rgb is not None and not any(rgb.get(c) for c in ("red", "green", "blue")):
        return None  # explicit black
    return style


def read_part_colors(book: gspread.Spreadsheet, tab_names: List[str]) -> Dict[str, Dict[int, object]]:
    """{tab: {sheet_row: text color style}} for column A of the given tabs (one API call)."""
    if not tab_names:
        return {}
    meta = book.fetch_sheet_metadata(params={
        "includeGridData": "true",
        "ranges": [f"{_quote(t)}!A:A" for t in tab_names],
        "fields": "sheets(properties.title,data(startRow,rowData(values(userEnteredFormat("
                  "textFormat(foregroundColor,foregroundColorStyle))))))",
    })
    out: Dict[str, Dict[int, object]] = {}
    for sheet in meta.get("sheets", []):
        colors = out.setdefault(sheet["properties"]["title"], {})
        for grid in sheet.get("data", []):
            start = grid.get("startRow", 0)
            for i, rd in enumerate(grid.get("rowData", [])):
                vals = rd.get("values") or [{}]
                tf = vals[0].get("userEnteredFormat", {}).get("textFormat", {})
                style = _color_style(tf)
                if style is not None:
                    colors[start + i + 1] = style
    return out


_NUMERIC_LOOKING = re.compile(r"^[+-]?[\d.,]+(?:[eE][+-]?\d+)?$|^\d{1,2}/\d{1,2}(/\d{2,4})?$")


def _cell_value(v):
    """Values are sent USER_ENTERED. Dates go as m/d/yyyy text, which Sheets turns into a real
    date (and gives it a date format even on an unformatted cell). Other text that Sheets would
    reinterpret is protected (e.g. part '009956' -> 9956)."""
    if isinstance(v, date):
        return f"{v.month}/{v.day}/{v.year}"
    if isinstance(v, str) and not v.startswith("=") and _NUMERIC_LOOKING.match(v):
        return "'" + v
    return v


def _quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def load_order_tabs(book: gspread.Spreadsheet):
    """Read every tab's values plus the order tabs' Part Number text colors."""
    rows = read_rows(book)
    return load_tabs(rows, read_part_colors(book, order_tab_names(rows)))


def read_basic_filters(book: gspread.Spreadsheet) -> Dict[int, Dict]:
    """{sheetId: basicFilter} for tabs that have a filter (the sortable 'table')."""
    meta = book.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId),basicFilter)"})
    return {s["properties"]["sheetId"]: s["basicFilter"]
            for s in meta.get("sheets", []) if s.get("basicFilter")}


def _filter_extension(flt: Dict, insert_at, insert_count: int, last_row: int):
    """setBasicFilter request that grows the tab's filter range down to `last_row` (1-based),
    or None if it already reaches it. Rows inserted inside the filter range join it
    automatically; rows below its last row (inserted or already there) do not, so they
    would not be sortable/filterable with the rest."""
    rng = flt.get("range", {})
    end = rng.get("endRowIndex")
    if end is None:
        return None
    if insert_count and insert_at is not None and insert_at - 1 < end:
        end += insert_count  # Sheets already grew the filter for rows inserted inside it
    if last_row <= end:
        return None
    new = {k: v for k, v in flt.items() if k != "criteria"}  # filterSpecs supersedes criteria
    new["range"] = {**rng, "endRowIndex": last_row}
    return {"setBasicFilter": {"filter": new}}


def apply(book: gspread.Spreadsheet, plan: Plan) -> int:
    """Write the plan. Re-reads the sheet first and refuses if the target rows changed."""
    if not plan.writes:
        return 0
    fresh = load_tabs(read_rows(book))
    for tw in plan.writes.values():
        st = fresh.get(tw.tab)
        if st is None or any(r not in st.x_rows for r in tw.expected_x_rows):
            raise SheetChangedError(
                f"The '{tw.tab}' tab changed since the 9J was checked. Please load the 9J again.")

    sheet_ids = {ws.title: ws.id for ws in book.worksheets()}
    requests = []
    # Insert extra rows (only when the x rows run out). Bottom-most first per tab is fine:
    # each tab has at most one insert.
    for tw in plan.writes.values():
        if tw.insert_count:
            requests.append({"insertDimension": {
                "range": {"sheetId": sheet_ids[tw.tab], "dimension": "ROWS",
                          "startIndex": tw.insert_at - 1,
                          "endIndex": tw.insert_at - 1 + tw.insert_count},
                "inheritFromBefore": True}})
    # Pull the new rows (and any data rows already below it) into each tab's filter range.
    # Sent as its own request after the rows exist, so the filter sees the final layout.
    filter_requests = []
    filters = read_basic_filters(book)
    for tw in plan.writes.values():
        flt = filters.get(sheet_ids[tw.tab])
        if not flt:
            continue
        last_row = max(max(tw.rows), fresh[tw.tab].last_data_row or 0)
        ext = _filter_extension(flt, tw.insert_at, tw.insert_count, last_row)
        if ext:
            ext["setBasicFilter"]["filter"]["range"]["sheetId"] = sheet_ids[tw.tab]
            filter_requests.append(ext)
    # Requests run in order, so these use the row numbers after the insert above.
    thin = {"style": "SOLID", "colorStyle": _BLACK}
    for tw in plan.writes.values():
        sid = sheet_ids[tw.tab]
        # Part Number text color = that part's latest color on the tab (only the color changes).
        for r, color in sorted(tw.text_colors.items()):
            requests.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": r - 1, "endRowIndex": r,
                          "startColumnIndex": tw.part_col, "endColumnIndex": tw.part_col + 1},
                "cell": {"userEnteredFormat": {"textFormat": {"foregroundColorStyle": color or _BLACK}}},
                "fields": "userEnteredFormat.textFormat.foregroundColorStyle"}})
        # Thin black "all borders" on A..J, like the existing order rows.
        for first, last in tw.row_blocks():
            requests.append({"updateBorders": {
                "range": {"sheetId": sid, "startRowIndex": first - 1, "endRowIndex": last,
                          "startColumnIndex": 0, "endColumnIndex": tw.border_cols},
                "top": thin, "bottom": thin, "left": thin, "right": thin,
                "innerHorizontal": thin, "innerVertical": thin}})
    if requests:
        book.batch_update({"requests": requests})

    data = []
    count = 0
    for tw in plan.writes.values():
        for r, cells in tw.rows.items():
            lo, hi = min(cells), max(cells)
            # None = leave the cell alone (Sheets API skips nulls).
            values = [_cell_value(cells[c]) if c in cells else None for c in range(lo, hi + 1)]
            data.append({"range": f"{_quote(tw.tab)}!{col_letter(lo)}{r}:{col_letter(hi)}{r}",
                         "values": [values]})
            count += 1
    book.values_batch_update({"valueInputOption": "USER_ENTERED", "data": data})
    if filter_requests:
        book.batch_update({"requests": filter_requests})
    return count
