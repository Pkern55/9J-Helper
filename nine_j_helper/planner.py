"""Decide where each 9J line goes in the orders workbook.

Works on plain row data (lists of cell values, dates as spreadsheet serial numbers), so the same
logic runs against Google Sheets or a local .xlsx export.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .nine_j import NineJ, ScheduleLine

EPOCH = date(1899, 12, 30)  # Google Sheets / Excel day 0

NEW = "new"
DUPLICATE = "already on sheet"
UNROUTED = "choose a tab"


# ---------------------------------------------------------------- helpers

def to_serial(d: date) -> int:
    return (d - EPOCH).days


def from_serial(v) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 < v < 100000:
        return date.fromordinal(EPOCH.toordinal() + int(v))
    return None


def serial_of(v) -> Optional[int]:
    d = from_serial(v)
    return to_serial(d) if d else None


def part_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip().upper()


def part_key(v) -> str:
    """Comparison key: case/space-insensitive and tolerant of lost leading zeros."""
    return part_text(v).lstrip("0")


def col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(row: Sequence, idx: Optional[int]):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ---------------------------------------------------------------- tab layout

@dataclass
class TabLayout:
    name: str
    header_row: int  # 1-based sheet row
    part: int  # 0-based column indexes
    date: int
    location: Optional[int] = None
    req: Optional[int] = None
    ship_to: Optional[int] = None
    shipped: Optional[int] = None
    net: Optional[int] = None
    part_copy: Optional[int] = None
    width: int = 0
    title_text: str = ""  # text above the header (e.g. "PO 106096 ...")


def detect_layout(name: str, rows: List[List]) -> Optional[TabLayout]:
    """Return the layout if this tab is an order tab (has a Part Number / Delivery Date header)."""
    for r_idx, row in enumerate(rows[:20]):
        labels = [str(c).strip().lower() if c is not None else "" for c in row]
        if not labels or labels[0] != "part number":
            continue
        date_col = next((i for i, t in enumerate(labels) if t.startswith("deliver")), None)
        if date_col is None:
            continue
        lay = TabLayout(name=name, header_row=r_idx + 1, part=0, date=date_col, width=len(labels))
        for i, t in enumerate(labels):
            if i == 0:
                continue
            if t == "location":
                lay.location = i
            elif t == "req qty":
                lay.req = i
            elif t.startswith("ship to"):
                lay.ship_to = i
            elif t.startswith("shipped"):
                lay.shipped = i
            elif t in ("net req qty", "balance due") and lay.net is None:
                lay.net = i
            elif t == "part number" and lay.part_copy is None:
                lay.part_copy = i
        lay.title_text = " ".join(str(c) for row_ in rows[:r_idx] for c in row_ if not _blank(c))
        return lay
    return None


@dataclass
class TabState:
    layout: TabLayout
    rows: List[List]
    data_rows: List[int] = field(default_factory=list)  # 1-based sheet rows with orders
    x_rows: List[int] = field(default_factory=list)  # 1-based placeholder rows
    existing: set = field(default_factory=set)  # {(part_key, serial)}
    parts: set = field(default_factory=set)

    @classmethod
    def build(cls, layout: TabLayout, rows: List[List]) -> "TabState":
        st = cls(layout=layout, rows=rows)
        for r in range(layout.header_row, len(rows)):  # rows after the header (0-based index r)
            row = rows[r]
            p = _cell(row, layout.part)
            if _blank(p):
                continue
            if part_text(p) == "X":
                st.x_rows.append(r + 1)
                continue
            st.data_rows.append(r + 1)
            k = part_key(p)
            st.parts.add(k)
            s = serial_of(_cell(row, layout.date))
            if s is not None:
                st.existing.add((k, s))
        return st

    def row(self, sheet_row: int) -> List:
        return self.rows[sheet_row - 1] if sheet_row - 1 < len(self.rows) else []

    def location_for(self, part: str):
        k = part_key(part)
        loc = None
        for r in self.data_rows:
            row = self.row(r)
            if part_key(_cell(row, self.layout.part)) == k and not _blank(_cell(row, self.layout.location)):
                loc = _cell(row, self.layout.location)
        if loc is None and self.layout.location is not None:
            vals = [_cell(self.row(r), self.layout.location) for r in self.data_rows]
            vals = [v for v in vals if not _blank(v)]
            if vals:
                loc = Counter(vals).most_common(1)[0][0]
        return loc

    @property
    def last_data_row(self) -> Optional[int]:
        return self.data_rows[-1] if self.data_rows else None


def load_tabs(sheet_rows: Dict[str, List[List]]) -> Dict[str, TabState]:
    tabs = {}
    for name, rows in sheet_rows.items():
        lay = detect_layout(name, rows)
        if lay:
            tabs[name] = TabState.build(lay, rows)
    return tabs


# ---------------------------------------------------------------- plan

@dataclass
class PlannedLine:
    line: ScheduleLine
    status: str
    tab: Optional[str] = None
    row: Optional[int] = None  # 1-based target row (new lines only)
    candidates: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class TabWrite:
    tab: str
    format_source_row: Optional[int]
    date_col: int = 0
    insert_at: Optional[int] = None  # insert `insert_count` rows starting at this 1-based row
    insert_count: int = 0
    rows: Dict[int, Dict[int, object]] = field(default_factory=dict)  # row -> {col: value}
    expected_x_rows: List[int] = field(default_factory=list)


@dataclass
class Plan:
    lines: List[PlannedLine]
    writes: Dict[str, TabWrite]

    @property
    def new_count(self) -> int:
        return sum(1 for pl in self.lines if pl.status == NEW)

    @property
    def unrouted(self) -> List[PlannedLine]:
        return [pl for pl in self.lines if pl.status == UNROUTED]


def _route(line: ScheduleLine, tabs: Dict[str, TabState], overrides: Dict[str, str]) -> Tuple[Optional[str], List[str], str]:
    k = part_key(line.part)
    ov = overrides.get(part_text(line.part))
    if ov in tabs:
        return ov, [], "your earlier choice"
    having = [n for n, t in tabs.items() if k in t.parts]
    if len(having) == 1:
        return having[0], [], ""
    po_tabs = []
    if line.po:
        pat = re.compile(rf"(?<!\d){re.escape(line.po)}(?!\d)")
        po_tabs = [n for n, t in tabs.items() if pat.search(t.layout.title_text)]
    if len(having) > 1:
        both = [n for n in having if n in po_tabs]
        if len(both) == 1:
            return both[0], [], "part is on several tabs; matched by PO"
        return None, having, "part is on several tabs"
    if len(po_tabs) == 1:
        return po_tabs[0], [], "new part; matched by PO"
    return None, (po_tabs or list(tabs)), "new part"


def build_plan(doc: NineJ, tabs: Dict[str, TabState], overrides: Optional[Dict[str, str]] = None) -> Plan:
    overrides = overrides or {}
    planned: List[PlannedLine] = []
    seen = set()
    for ln in doc.lines:
        tab, cands, note = _route(ln, tabs, overrides)
        if tab is None:
            planned.append(PlannedLine(ln, UNROUTED, candidates=cands, note=note))
            continue
        key = (part_key(ln.part), to_serial(ln.delivery_date))
        if key in tabs[tab].existing or (tab, key) in seen:
            planned.append(PlannedLine(ln, DUPLICATE, tab=tab, note=note))
            continue
        seen.add((tab, key))
        planned.append(PlannedLine(ln, NEW, tab=tab, note=note))

    writes: Dict[str, TabWrite] = {}
    for name, st in tabs.items():
        new = [pl for pl in planned if pl.status == NEW and pl.tab == name]
        if not new:
            continue
        new.sort(key=lambda pl: (pl.line.delivery_date, part_text(pl.line.part)))
        lay = st.layout
        tw = TabWrite(tab=name, format_source_row=st.last_data_row, date_col=lay.date)
        slots = list(st.x_rows[: len(new)])
        tw.expected_x_rows = list(slots)
        overflow = len(new) - len(slots)
        if overflow > 0:
            after = st.x_rows[-1] if st.x_rows else (st.last_data_row or lay.header_row)
            tw.insert_at = after + 1
            tw.insert_count = overflow
            slots += list(range(after + 1, after + 1 + overflow))
        for pl, r in zip(new, slots):
            pl.row = r
            cells: Dict[int, object] = {lay.part: part_text(pl.line.part),
                                        lay.date: to_serial(pl.line.delivery_date)}
            if lay.location is not None:
                cells[lay.location] = st.location_for(pl.line.part)
            if lay.req is not None:
                cells[lay.req] = pl.line.req_qty
            if lay.ship_to is not None:
                cells[lay.ship_to] = f"=WORKDAY({col_letter(lay.date)}{r},-2)"
            if lay.net is not None and lay.req is not None and lay.shipped is not None:
                cells[lay.net] = f"={col_letter(lay.req)}{r}-{col_letter(lay.shipped)}{r}"
            if lay.part_copy is not None:
                cells[lay.part_copy] = part_text(pl.line.part)
            tw.rows[r] = cells
        writes[name] = tw
    return Plan(lines=planned, writes=writes)
