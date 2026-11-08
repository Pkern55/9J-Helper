"""Local .xlsx backend (an export of the Google Sheet). Used for tests and dry runs; the
original file is never modified — `apply` always writes to a different output path."""

from __future__ import annotations

import os
from copy import copy
from typing import Dict, List

import openpyxl
from openpyxl.formula.translate import Translator
from openpyxl.styles import Border, Side

from .planner import Plan, load_tabs, order_tab_names

_THIN = Side(style="thin", color="FF000000")


def read_rows(path: str) -> Dict[str, List[List]]:
    wb = openpyxl.load_workbook(path, data_only=False)
    return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def _font_color(cell):
    """Text color as an ARGB string, or None for default/black."""
    col = cell.font.color if cell.font is not None else None
    if col is None or col.type != "rgb" or not isinstance(col.rgb, str):
        return None
    return None if col.rgb.upper() in ("FF000000", "00000000") else col.rgb.upper()


def read_part_colors(path: str, tab_names: List[str]) -> Dict[str, Dict[int, object]]:
    wb = openpyxl.load_workbook(path)
    out = {}
    for name in tab_names:
        ws = wb[name]
        out[name] = {r: c for r in range(1, ws.max_row + 1)
                     if (c := _font_color(ws.cell(r, 1))) is not None}
    return out


def load_order_tabs(path: str):
    rows = read_rows(path)
    return load_tabs(rows, read_part_colors(path, order_tab_names(rows)))


def _delete_row(ws, r: int) -> None:
    """Delete sheet row r. openpyxl moves the cells below up but leaves their formulas pointing
    at the old rows, so those are translated here. (Merged cells, filter range and conditional
    formats are not adjusted: this backend is only for dry runs.)"""
    ws.delete_rows(r)
    for row in ws.iter_rows(min_row=r, max_row=ws.max_row):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.value = Translator(cell.value, origin=f"{cell.column_letter}{cell.row + 1}"
                                        ).translate_formula(cell.coordinate)


def apply(plan: Plan, src: str, dst: str) -> None:
    if os.path.abspath(src) == os.path.abspath(dst):
        raise ValueError("Refusing to overwrite the source workbook; choose a different output path.")
    wb = openpyxl.load_workbook(src)
    border = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
    for tw in plan.writes.values():
        ws = wb[tw.tab]
        for r in sorted(tw.delete_rows, reverse=True):  # bottom-most first
            _delete_row(ws, r)
        if tw.insert_count:
            ws.insert_rows(tw.insert_at, tw.insert_count)
        for r, cells in tw.rows.items():
            for col, val in cells.items():
                ws.cell(r, col + 1).value = val
            for c in range(1, tw.border_cols + 1):
                ws.cell(r, c).border = border
            if r in tw.text_colors:
                cell = ws.cell(r, tw.part_col + 1)
                font = copy(cell.font)
                font.color = tw.text_colors[r] or "FF000000"
                cell.font = font
    wb.save(dst)
