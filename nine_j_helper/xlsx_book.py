"""Local .xlsx backend (an export of the Google Sheet). Used for tests and dry runs; the
original file is never modified — `apply` always writes to a different output path."""

from __future__ import annotations

import os
from typing import Dict, List

import openpyxl

from .planner import Plan


def read_rows(path: str) -> Dict[str, List[List]]:
    wb = openpyxl.load_workbook(path, data_only=False)
    return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def apply(plan: Plan, src: str, dst: str) -> None:
    if os.path.abspath(src) == os.path.abspath(dst):
        raise ValueError("Refusing to overwrite the source workbook; choose a different output path.")
    wb = openpyxl.load_workbook(src)
    for tw in plan.writes.values():
        ws = wb[tw.tab]
        if tw.insert_count:
            ws.insert_rows(tw.insert_at, tw.insert_count)
        for r, cells in tw.rows.items():
            for col, val in cells.items():
                ws.cell(r, col + 1).value = val
    wb.save(dst)
