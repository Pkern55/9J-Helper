"""Google Sheets backend."""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List

import gspread

from . import config
from .planner import Plan, col_letter, load_tabs

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
    # No formatting is copied; new rows keep whatever formatting the x rows already have.
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
    return count
