"""Decide where each 9J line goes in the orders workbook.

Works on plain row data (lists of cell values, dates as spreadsheet serial numbers), so the same
logic runs against Google Sheets or a local .xlsx export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .nine_j import NineJ, ScheduleLine

EPOCH = date(1899, 12, 30)  # Google Sheets / Excel day 0

BORDER_COLS = 10  # A..J: the order tabs' bordered columns

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
    # Part Number text color of each part's most recent row (None = default/black).
    latest_color: Dict[str, object] = field(default_factory=dict)

    @classmethod
    def build(cls, layout: TabLayout, rows: List[List],
              colors: Optional[Dict[int, object]] = None) -> "TabState":
        colors = colors or {}
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
            st.latest_color[k] = colors.get(r + 1)  # later rows overwrite earlier ones
            s = serial_of(_cell(row, layout.date))
            if s is not None:
                st.existing.add((k, s))
        return st

    @property
    def last_data_row(self) -> Optional[int]:
        return self.data_rows[-1] if self.data_rows else None


def load_tabs(sheet_rows: Dict[str, List[List]],
              part_colors: Optional[Dict[str, Dict[int, object]]] = None) -> Dict[str, TabState]:
    """`part_colors` (optional): {tab: {sheet_row: Part Number text color}}; colors are opaque
    values handed back unchanged to the same backend when writing."""
    part_colors = part_colors or {}
    tabs = {}
    for name, rows in sheet_rows.items():
        lay = detect_layout(name, rows)
        if lay:
            tabs[name] = TabState.build(lay, rows, part_colors.get(name))
    return tabs


def order_tab_names(sheet_rows: Dict[str, List[List]]) -> List[str]:
    return [n for n, rows in sheet_rows.items() if detect_layout(n, rows)]


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
    date_col: int = 0
    insert_at: Optional[int] = None  # insert `insert_count` rows starting at this 1-based row
    insert_count: int = 0
    rows: Dict[int, Dict[int, object]] = field(default_factory=dict)  # row -> {col: value}
    expected_x_rows: List[int] = field(default_factory=list)
    part_col: int = 0
    # row -> Part Number text color to set (None = default/black). Rows of brand-new parts
    # are absent, so their text color is left alone.
    text_colors: Dict[int, object] = field(default_factory=dict)
    border_cols: int = BORDER_COLS  # new rows get thin borders on columns A..(A+border_cols-1)

    def row_blocks(self) -> List[Tuple[int, int]]:
        """Consecutive runs of new rows as (first_row, last_row), 1-based inclusive."""
        blocks: List[Tuple[int, int]] = []
        for r in sorted(self.rows):
            if blocks and r == blocks[-1][1] + 1:
                blocks[-1] = (blocks[-1][0], r)
            else:
                blocks.append((r, r))
        return blocks


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
    k = part_key(line.sheet_part)
    ov = overrides.get(part_text(line.sheet_part))
    if ov in tabs:
        return ov, [], "your earlier choice"
    having = [n for n, t in tabs.items() if k in t.parts]
    if len(having) == 1:
        return having[0], [], ""
    if len(having) > 1:
        return None, having, "part is on several tabs"
    return None, list(tabs), "new part"


def build_plan(doc: NineJ, tabs: Dict[str, TabState], overrides: Optional[Dict[str, str]] = None) -> Plan:
    """Only Part Number (9J Item #), Delivery Date and Req Qty (9J Net Req Qty) are written."""
    overrides = overrides or {}
    planned: List[PlannedLine] = []
    seen = set()
    for ln in doc.lines:
        tab, cands, note = _route(ln, tabs, overrides)
        if tab is None:
            planned.append(PlannedLine(ln, UNROUTED, candidates=cands, note=note))
            continue
        key = (part_key(ln.sheet_part), to_serial(ln.delivery_date))
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
        new.sort(key=lambda pl: (pl.line.delivery_date, part_text(pl.line.sheet_part)))
        lay = st.layout
        tw = TabWrite(tab=name, date_col=lay.date, part_col=lay.part)
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
            # Dates stay `date` objects; each backend writes them in its own way.
            cells: Dict[int, object] = {lay.part: part_text(pl.line.sheet_part),
                                        lay.date: pl.line.delivery_date}
            if lay.req is not None:
                cells[lay.req] = pl.line.net_qty
            tw.rows[r] = cells
            k = part_key(pl.line.sheet_part)
            if k in st.latest_color:
                tw.text_colors[r] = st.latest_color[k]
        writes[name] = tw
    return Plan(lines=planned, writes=writes)
