"""Decide where each 9J line goes in the orders workbook.

Works on plain row data (lists of cell values, dates as spreadsheet serial numbers), so the same
logic runs against Google Sheets or a local .xlsx export.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .nine_j import NineJ, ScheduleLine

EPOCH = date(1899, 12, 30)  # Google Sheets / Excel day 0

BORDER_COLS = 10  # A..J: the order tabs' bordered columns

NEW = "new"
DUPLICATE = "already on sheet"
UNROUTED = "choose a tab"

# The order new rows are written to the sheet. key -> label shown in the window.
SORTS = {
    "date": "Delivery date, then part",
    "part": "Part number, then date",
    "9j": "Order on the 9J",
}
DEFAULT_SORT = "date"


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


def is_unshipped(row: Sequence, lay: "TabLayout") -> bool:
    """An order row with no Shipped Qty, Actual Ship Date or P/S (a formula counts as filled).
    Tabs without a Shipped Qty column never report unshipped rows, so nothing is deleted there."""
    if lay.shipped is None:
        return False
    cols = [c for c in (lay.shipped, lay.actual_date, lay.ps) if c is not None]
    return all(_blank(_cell(row, c)) for c in cols)


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
    actual_date: Optional[int] = None
    ps: Optional[int] = None
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
            elif t.startswith("actual"):
                lay.actual_date = i
            elif t in ("p/s", "ps"):
                lay.ps = i
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
    # {(part_key, serial)} of SHIPPED rows only. Unshipped rows get replaced by the 9J, so they
    # never count as "already on sheet".
    existing: set = field(default_factory=set)
    # Orders with no Shipped Qty, Actual Ship Date or P/S: the rows replace mode may delete.
    unshipped_rows: List[int] = field(default_factory=list)
    row_parts: Dict[int, str] = field(default_factory=dict)  # data row -> part_key
    parts: set = field(default_factory=set)
    part_labels: Dict[str, str] = field(default_factory=dict)  # part_key -> text as on the sheet
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
            st.part_labels.setdefault(k, part_text(p))
            st.row_parts[r + 1] = k
            st.latest_color[k] = colors.get(r + 1)  # later rows overwrite earlier ones
            s = serial_of(_cell(row, layout.date))
            if s is not None and is_unshipped(row, layout):
                st.unshipped_rows.append(r + 1)
            elif s is not None:
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
    seq: int = 0  # position of the line on the 9J


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
    # Replace mode. Deletes run first, so `rows`, `insert_at` and `last_data_row` are numbered
    # AFTER the deletions; `delete_rows`, `expected_deleted` and `expected_x_rows` are numbered
    # as the sheet is now (before anything is written).
    delete_rows: List[int] = field(default_factory=list)  # unshipped rows to remove, ascending
    expected_deleted: Dict[int, str] = field(default_factory=dict)  # row -> part_key, re-checked
    last_data_row: int = 0  # last order row that stays (header row if none)

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
class RemovedRow:
    """An unshipped sheet row that the 9J replaces (shown in the preview)."""
    tab: str
    row: int  # 1-based, as the sheet is now
    part: str
    delivery_date: Optional[date]
    req_qty: object


@dataclass
class Plan:
    lines: List[PlannedLine]
    writes: Dict[str, TabWrite]
    removed: List[RemovedRow] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        return sum(1 for pl in self.lines if pl.status == NEW)

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def has_changes(self) -> bool:
        return bool(self.writes)

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


def known_parts(tabs: Optional[Dict[str, "TabState"]], overrides: Dict[str, str]) -> Dict[str, List[Tuple[str, str]]]:
    """What the app knows about where parts go: {tab: [(part, source)]}, parts sorted.
    source is "sheet" (the part already has rows on that tab) or "remembered" (you chose the
    tab for it earlier and it has no rows there yet)."""
    out: Dict[str, Dict[str, Tuple[str, str]]] = {name: {} for name in (tabs or {})}
    for name, st in (tabs or {}).items():
        for k in st.parts:
            out[name][k] = (st.part_labels.get(k, k), "sheet")
    for part, tab in overrides.items():
        out.setdefault(tab, {}).setdefault(part_key(part), (part_text(part), "remembered"))
    return {tab: sorted(d.values(), key=lambda t: t[0]) for tab, d in out.items()}


def formula_cells(lay: TabLayout, row: int, part: str) -> Dict[int, object]:
    """The helper cells every order row carries, written with the row's own number:
    Ship to Customer (=WORKDAY(date,-2)), Balance/Net (=Req-Shipped) and, on tabs that have one,
    the extra part-number column. Columns a tab doesn't have are skipped."""
    cells: Dict[int, object] = {}
    if lay.ship_to is not None:
        cells[lay.ship_to] = f"=WORKDAY({col_letter(lay.date)}{row},-2)"
    if lay.net is not None and lay.req is not None and lay.shipped is not None:
        cells[lay.net] = f"={col_letter(lay.req)}{row}-{col_letter(lay.shipped)}{row}"
    if lay.part_copy is not None:
        cells[lay.part_copy] = part
    return cells


def _sort_key(sort: str):
    if sort == "part":
        return lambda pl: (part_text(pl.line.sheet_part), pl.line.delivery_date, pl.seq)
    if sort == "9j":
        return lambda pl: pl.seq
    return lambda pl: (pl.line.delivery_date, part_text(pl.line.sheet_part), pl.seq)


def build_plan(doc: NineJ, tabs: Dict[str, TabState], overrides: Optional[Dict[str, str]] = None,
               sort: str = DEFAULT_SORT) -> Plan:
    """Replace mode. For every part on the 9J, that part's unshipped rows (no Shipped Qty, Actual
    Ship Date or P/S) are deleted from its tab and all of its 9J lines are written after the last
    remaining order. Shipped rows are never touched, and a 9J line that matches a shipped row's
    part + date is skipped. Written per new row: Part Number (9J Item #), Delivery Date, Req Qty
    (9J Net Req Qty) and the formula cells from `formula_cells`. `sort` (a key of SORTS) sets the
    order the new rows are written in."""
    overrides = overrides or {}
    planned: List[PlannedLine] = []
    seen = set()
    for seq, ln in enumerate(doc.lines):
        tab, cands, note = _route(ln, tabs, overrides)
        if tab is None:
            planned.append(PlannedLine(ln, UNROUTED, candidates=cands, note=note, seq=seq))
            continue
        key = (part_key(ln.sheet_part), to_serial(ln.delivery_date))
        if key in tabs[tab].existing or (tab, key) in seen:
            planned.append(PlannedLine(ln, DUPLICATE, tab=tab, note=note, seq=seq))
            continue
        seen.add((tab, key))
        planned.append(PlannedLine(ln, NEW, tab=tab, note=note, seq=seq))

    writes: Dict[str, TabWrite] = {}
    removed: List[RemovedRow] = []
    for name, st in tabs.items():
        lay = st.layout
        new = [pl for pl in planned if pl.status == NEW and pl.tab == name]
        on_9j = {part_key(pl.line.sheet_part) for pl in planned if pl.tab == name}
        delete = [r for r in st.unshipped_rows if st.row_parts[r] in on_9j]  # ascending
        if not new and not delete:
            continue
        new.sort(key=_sort_key(sort))
        tw = TabWrite(tab=name, date_col=lay.date, part_col=lay.part, delete_rows=delete,
                      expected_deleted={r: st.row_parts[r] for r in delete})
        for r in delete:
            row = st.rows[r - 1]
            removed.append(RemovedRow(name, r, part_text(_cell(row, lay.part)),
                                      from_serial(_cell(row, lay.date)), _cell(row, lay.req)))

        def after_delete(r: int) -> int:  # sheet row r (not deleted) -> its row once deletes ran
            return r - bisect_left(delete, r)

        gone = set(delete)
        kept = [r for r in st.data_rows if r not in gone]
        last_data = after_delete(kept[-1]) if kept else lay.header_row
        tw.last_data_row = last_data
        x_slots = [after_delete(r) for r in st.x_rows[: len(new)]]
        tw.expected_x_rows = list(st.x_rows[: len(new)])
        slots = list(x_slots)
        overflow = len(new) - len(slots)
        if overflow > 0:
            after = after_delete(st.x_rows[-1]) if st.x_rows else last_data
            tw.insert_at = after + 1
            tw.insert_count = overflow
            slots += list(range(after + 1, after + 1 + overflow))
        for pl, r in zip(new, slots):
            pl.row = r
            part = part_text(pl.line.sheet_part)
            # Dates stay `date` objects; each backend writes them in its own way.
            cells: Dict[int, object] = {lay.part: part, lay.date: pl.line.delivery_date}
            if lay.req is not None:
                cells[lay.req] = pl.line.net_qty
            cells.update(formula_cells(lay, r, part))
            tw.rows[r] = cells
            k = part_key(pl.line.sheet_part)
            if k in st.latest_color:
                tw.text_colors[r] = st.latest_color[k]
        writes[name] = tw
    return Plan(lines=planned, writes=writes, removed=removed)


# ---------------------------------------------------------------- what the window shows

ADDED, REMOVED, SKIPPED, NEEDS_TAB = "added", "removed", "skipped", "needs tab"
_KIND_ORDER = {REMOVED: 0, ADDED: 1, SKIPPED: 2, NEEDS_TAB: 3}


@dataclass
class DisplayRow:
    kind: str  # ADDED / REMOVED / SKIPPED / NEEDS_TAB
    tab: str
    part: str
    delivery_date: Optional[date]
    qty: object
    status: str


@dataclass
class Group:
    part: str  # "" for the final group of lines that still need a tab
    rows: List[DisplayRow]


def display_groups(plan: Plan, summary: bool = False) -> List[Group]:
    """Everything the window lists: one boxed group per part, the groups in the order the parts
    first appear on the 9J, and by date inside each group. A removed row sorts just above the
    line that replaces it. Lines that still need a tab form a last group. `summary` words the
    status as done."""
    by_part: Dict[str, List[DisplayRow]] = {}
    first_seq: Dict[str, int] = {}  # part -> position of its first line on the 9J
    unrouted: List[DisplayRow] = []
    for rr in plan.removed:
        txt = f"REMOVED (was row {rr.row})" if summary else f"REMOVE row {rr.row} (not shipped; replaced by the 9J)"
        by_part.setdefault(part_key(rr.part), []).append(
            DisplayRow(REMOVED, rr.tab, rr.part, rr.delivery_date, rr.req_qty, txt))
    for pl in plan.lines:
        ln = pl.line
        part = part_key(ln.sheet_part)
        first_seq[part] = min(first_seq.get(part, pl.seq), pl.seq)
        if pl.status == NEW:
            txt = f"ADDED at row {pl.row}" if summary else f"new → row {pl.row}"
            by_part.setdefault(part, []).append(
                DisplayRow(ADDED, pl.tab, ln.sheet_part, ln.delivery_date, ln.net_qty, txt))
        elif pl.status == DUPLICATE:
            by_part.setdefault(part, []).append(
                DisplayRow(SKIPPED, pl.tab, ln.sheet_part, ln.delivery_date, ln.net_qty,
                           "skipped (already shipped on sheet)"))
        else:
            unrouted.append(DisplayRow(NEEDS_TAB, "", ln.sheet_part, ln.delivery_date, ln.net_qty,
                                       UNROUTED + " (skipped)"))

    def key(r: DisplayRow):
        return (r.delivery_date or date.min, _KIND_ORDER[r.kind])

    # A removed row always belongs to a part that is on the 9J, so every group has a position.
    ordered = sorted(by_part.items(), key=lambda kv: (first_seq.get(kv[0], 10**9), kv[0]))
    groups = [Group(rows[0].part and part_text(rows[0].part), sorted(rows, key=key)) for _, rows in ordered]
    if unrouted:
        groups.append(Group("", sorted(unrouted, key=lambda r: (part_text(r.part), key(r)))))
    return groups
