"""Google Sheets backend."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Dict, List

import gspread

from . import config
from .planner import Plan, TabWrite, col_letter, load_tabs, order_tab_names

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


BACKUP_PREFIX = "9J Helper backup - "  # hidden tabs holding the pre-update copy used by Undo


def read_rows(book: gspread.Spreadsheet) -> Dict[str, List[List]]:
    worksheets = [ws for ws in book.worksheets() if not ws.title.startswith(BACKUP_PREFIX)]
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


def _filter_extension(flt: Dict, insert_at, insert_count: int, last_row: int, deleted_rows=()):
    """setBasicFilter request that grows the tab's filter range down to `last_row` (1-based,
    numbered after the deletes), or None if it already reaches it. Rows deleted inside the filter
    range shrink it, and rows inserted inside it join it, automatically; rows below its last row
    (inserted or already there) do not, so they would not be sortable/filterable with the rest."""
    rng = flt.get("range", {})
    end = rng.get("endRowIndex")
    if end is None:
        return None
    end -= sum(1 for r in deleted_rows if r <= end)  # Sheets shrank the filter for these
    if insert_count and insert_at is not None and insert_at - 1 < end:
        end += insert_count  # Sheets already grew the filter for rows inserted inside it
    if last_row <= end:
        return None
    new = {k: v for k, v in flt.items() if k != "criteria"}  # filterSpecs supersedes criteria
    new["range"] = {**rng, "endRowIndex": last_row}
    return {"setBasicFilter": {"filter": new}}


WHITE_FILL = {"red": 1.0, "green": 1.0, "blue": 1.0}  # background of every added row
TEST_DELETE_FILL ={"red": 1.0, "green": 0.2, "blue": 0.2}  # rows that would be deleted
TEST_ADD_FILL = {"red": 1.0, "green": 0.75, "blue": 0.85}  # rows that are added


def _pre_delete_row(row: int, deleted) -> int:
    """Map a row number counted after the deletes back to its number on the sheet as it is now."""
    for d in sorted(deleted):
        if d <= row:
            row += 1
    return row


def _without_deletes(tw: TabWrite) -> TabWrite:
    """The same writes for a sheet where nothing is deleted: row numbers go back to the
    current-sheet numbering, and `delete_rows` is kept only for the caller to highlight."""
    m = lambda r: _pre_delete_row(r, tw.delete_rows)
    return replace(
        tw,
        rows={m(r): cells for r, cells in tw.rows.items()},
        text_colors={m(r): c for r, c in tw.text_colors.items()},
        insert_at=None if tw.insert_at is None else m(tw.insert_at),
        last_data_row=m(tw.last_data_row) if tw.delete_rows else tw.last_data_row,
    )


def apply(book: gspread.Spreadsheet, plan: Plan, test_mode: bool = False) -> int:
    """Write the plan. Re-reads the sheet first and refuses if the target rows changed.

    Test mode deletes nothing: the rows that would be deleted get a red fill, and the rows
    that are added get a pink fill."""
    if not plan.writes:
        return 0
    writes = plan.writes
    if test_mode:
        writes = {name: _without_deletes(tw) for name, tw in plan.writes.items()}
    fresh = load_tabs(read_rows(book))
    for tw in plan.writes.values():
        st = fresh.get(tw.tab)
        if (st is None or any(r not in st.x_rows for r in tw.expected_x_rows)
                or any(r not in st.unshipped_rows or st.row_parts[r] != k
                       for r, k in tw.expected_deleted.items())):
            raise SheetChangedError(
                f"The '{tw.tab}' tab changed since the 9J was checked. Please load the 9J again.")

    worksheets = book.worksheets()
    sheet_ids = {ws.title: ws.id for ws in worksheets}
    grid_rows = {ws.title: ws.row_count for ws in worksheets}
    requests = []
    # Replace mode: remove the stale unshipped rows first, bottom-most first so the row numbers
    # of the ones still to go don't move. Everything after this uses post-delete row numbers.
    for tw in writes.values():
        for r in ([] if test_mode else sorted(tw.delete_rows, reverse=True)):
            requests.append({"deleteDimension": {
                "range": {"sheetId": sheet_ids[tw.tab], "dimension": "ROWS",
                          "startIndex": r - 1, "endIndex": r}}})
    # Insert extra rows (only when the x rows run out). Each tab has at most one insert.
    for tw in writes.values():
        if tw.insert_count:
            start = tw.insert_at - 1
            grid = grid_rows.get(tw.tab, 0) - (0 if test_mode else len(tw.delete_rows))
            if start >= grid:
                # Past the end of the grid (data fills the whole tab): Sheets refuses an insert
                # there, so grow the grid at the bottom instead.
                requests.append({"appendDimension": {
                    "sheetId": sheet_ids[tw.tab], "dimension": "ROWS",
                    "length": start + tw.insert_count - grid}})
            else:
                requests.append({"insertDimension": {
                    "range": {"sheetId": sheet_ids[tw.tab], "dimension": "ROWS",
                              "startIndex": start, "endIndex": start + tw.insert_count},
                    "inheritFromBefore": False}})  # never copy the row above (it is often a green shipped row)
    # Pull the new rows (and any data rows already below it) into each tab's filter range.
    # Sent as its own request after the rows exist, so the filter sees the final layout.
    filter_requests = []
    filters = read_basic_filters(book)
    for tw in writes.values():
        flt = filters.get(sheet_ids[tw.tab])
        if not flt:
            continue
        last_row = max(max(tw.rows, default=0), tw.last_data_row)
        ext = _filter_extension(flt, tw.insert_at, tw.insert_count, last_row,
                                [] if test_mode else tw.delete_rows)
        if ext:
            ext["setBasicFilter"]["filter"]["range"]["sheetId"] = sheet_ids[tw.tab]
            filter_requests.append(ext)
    # Requests run in order, so these use the row numbers after the insert above.
    thin = {"style": "SOLID", "colorStyle": _BLACK}
    for tw in writes.values():
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
    def fill(sid, first, last, cols, color):
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": first - 1, "endRowIndex": last,
                      "startColumnIndex": 0, "endColumnIndex": cols},
            "cell": {"userEnteredFormat": {"backgroundColor": color}},
            "fields": "userEnteredFormat.backgroundColor"}}
    # Every added row is filled white (no stray colors from whatever was there before).
    for tw in writes.values():
        for first, last in tw.row_blocks():
            requests.append(fill(sheet_ids[tw.tab], first, last, tw.border_cols, WHITE_FILL))
    if test_mode:
        for name, tw in plan.writes.items():  # numbering: rows as the sheet is now
            for r in tw.delete_rows:
                requests.append(fill(sheet_ids[name], r, r, tw.border_cols, TEST_DELETE_FILL))
        for tw in writes.values():
            for first, last in tw.row_blocks():
                requests.append(fill(sheet_ids[tw.tab], first, last, tw.border_cols, TEST_ADD_FILL))
    if requests:
        book.batch_update({"requests": requests})

    data = []
    count = 0
    for tw in writes.values():
        for r, cells in tw.rows.items():
            lo, hi = min(cells), max(cells)
            # None = leave the cell alone (Sheets API skips nulls).
            values = [_cell_value(cells[c]) if c in cells else None for c in range(lo, hi + 1)]
            data.append({"range": f"{_quote(tw.tab)}!{col_letter(lo)}{r}:{col_letter(hi)}{r}",
                         "values": [values]})
            count += 1
    if data:
        book.values_batch_update({"valueInputOption": "USER_ENTERED", "data": data})
    if filter_requests:
        book.batch_update({"requests": filter_requests})
    return count


# ---------------------------------------------------------------- undo

@dataclass
class TabBackup:
    tab: str
    sheet_id: int
    backup_id: int
    rows: int
    cols: int
    filter: Dict = None  # the tab's basicFilter before the update, if it had one


@dataclass
class Backup:
    tabs: List[TabBackup]


def make_backup(book: gspread.Spreadsheet, tab_names: List[str]) -> Backup:
    """Copy each tab to a hidden 'backup' tab so the update can be undone. Any older backup
    tabs are deleted first (only the latest update can be undone)."""
    worksheets = book.worksheets()
    filters = read_basic_filters(book)
    used_ids = {ws.id for ws in worksheets}
    requests = [{"deleteSheet": {"sheetId": ws.id}}
                for ws in worksheets if ws.title.startswith(BACKUP_PREFIX)]
    tabs = []
    next_id = max(used_ids | {0}) + 1000
    for ws in worksheets:
        if ws.title not in tab_names:
            continue
        bid = next_id
        next_id += 1
        requests.append({"duplicateSheet": {
            "sourceSheetId": ws.id, "newSheetId": bid, "insertSheetIndex": len(worksheets),
            "newSheetName": (BACKUP_PREFIX + ws.title)[:100]}})
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": bid, "hidden": True}, "fields": "hidden"}})
        tabs.append(TabBackup(ws.title, ws.id, bid, ws.row_count, ws.col_count,
                              filters.get(ws.id)))
    if requests:
        book.batch_update({"requests": requests})
    return Backup(tabs)


def undo(book: gspread.Spreadsheet, backup: Backup) -> None:
    """Put each backed-up tab back exactly as it was (values, formulas, formatting, filter),
    then delete the backup tabs."""
    existing = {ws.id for ws in book.worksheets()}
    for tb in backup.tabs:
        if tb.backup_id not in existing or tb.sheet_id not in existing:
            raise RuntimeError(f"The backup of '{tb.tab}' is gone, so it can't be undone.")
    requests = []
    for tb in backup.tabs:
        sid = tb.sheet_id
        requests += [
            {"clearBasicFilter": {"sheetId": sid}},
            {"unmergeCells": {"range": {"sheetId": sid}}},
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {
                    "rowCount": tb.rows, "columnCount": tb.cols}},
                "fields": "gridProperties.rowCount,gridProperties.columnCount"}},
            {"updateCells": {"range": {"sheetId": sid},
                             "fields": "userEnteredValue,userEnteredFormat"}},
            {"copyPaste": {"source": {"sheetId": tb.backup_id}, "destination": {"sheetId": sid},
                           "pasteType": "PASTE_NORMAL", "pasteOrientation": "NORMAL"}},
        ]
        if tb.filter:
            requests.append({"setBasicFilter": {"filter": {
                **{k: v for k, v in tb.filter.items() if k != "criteria"},
                "range": {**tb.filter.get("range", {}), "sheetId": sid}}}})
        requests.append({"deleteSheet": {"sheetId": tb.backup_id}})
    if requests:
        book.batch_update({"requests": requests})
