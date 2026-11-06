"""Local .xlsx backend (an export of the Google Sheet). Used for tests and dry runs; the
original file is never modified — `apply` always writes to a different output path."""

from __future__ import annotations

import os
from copy import copy
from typing import Dict, List

import openpyxl

from .planner import Plan, from_serial


def read_rows(path: str) -> Dict[str, List[List]]:
    wb = openpyxl.load_workbook(path, data_only=False)
    out = {}
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
        out[ws.title] = rows
    return out


def apply(plan: Plan, src: str, dst: str) -> None:
    if os.path.abspath(src) == os.path.abspath(dst):
        raise ValueError("Refusing to overwrite the source workbook; choose a different output path.")
    wb = openpyxl.load_workbook(src)
    for tw in plan.writes.values():
        ws = wb[tw.tab]
        if tw.insert_count:
            ws.insert_rows(tw.insert_at, tw.insert_count)
        for r, cells in tw.rows.items():
            if tw.format_source_row:
                for c in range(1, ws.max_column + 1):
                    src_cell = ws.cell(tw.format_source_row, c)
                    dst_cell = ws.cell(r, c)
                    if src_cell.has_style:
                        dst_cell._style = copy(src_cell._style)
            for col, val in cells.items():
                cell = ws.cell(r, col + 1)
                if col == tw.date_col and isinstance(val, int):
                    val = from_serial(val)
                cell.value = val
    wb.save(dst)
