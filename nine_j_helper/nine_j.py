"""Parse a Hendrickson "9J" Supplier Schedule PDF into structured data."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

import pdfplumber

DATE = r"\d{1,2}/\d{1,2}/\d{2,4}"
NUM = r"-?[\d,]+(?:\.\d+)?"

# e.g. "Daily 10/28/26 010545-1 F* 28.0 528.0 28.0"
#      "Monthly01/04/27 010545-1 P 84.0 640.0 84.0"
#      "11/04/26 010545-1 SOME REF F 28.0 556.0 28.0"
SCHEDULE_LINE = re.compile(
    rf"^(?P<interval>[A-Za-z]+)?\s*(?P<date>{DATE})\s+(?P<part>\S+)\s+"
    rf"(?:(?P<ref>.*?)\s+)?(?P<fp>[FfP]\*?)\s+"
    rf"(?P<req>{NUM})\s+(?P<cum>{NUM})\s+(?P<net>{NUM})\s*$"
)
# Line after the "Last Receipt Date ... Item Description" heading.
ITEM_LINE = re.compile(
    rf"^(?:(?P<last_date>{DATE})\s+(?P<last_qty>{NUM})\s+)?(?P<item>\S+)\s*(?P<desc>.*)$"
)
# Line after the "P.O. # Supplier Item # Item Revision" heading.
PO_LINE = re.compile(r"^(?P<po>\S+)\s+(?P<supplier_item>\S+)(?:\s+(?P<rev>\S+))?\s*$")


class NineJParseError(Exception):
    pass


@dataclass
class ScheduleLine:
    part: str
    delivery_date: date
    firm_plan: str  # "F", "F*", "P" ...
    req_qty: float
    cum_qty: float
    net_qty: float
    interval: str = ""
    reference: str = ""
    po: str = ""
    item_description: str = ""
    item: str = ""  # Hendrickson Item # of the block this line belongs to

    @property
    def sheet_part(self) -> str:
        """Part number written to the sheet: the block's Hendrickson Item #."""
        return self.item or self.part

    @property
    def is_firm(self) -> bool:
        return self.firm_plan.upper().startswith("F")

    @property
    def is_new(self) -> bool:
        return self.firm_plan.endswith("*")


@dataclass
class Item:
    hendrickson_item: str
    description: str = ""
    po: str = ""
    supplier_item: str = ""
    revision: str = ""
    last_receipt_date: Optional[date] = None
    last_receipt_qty: Optional[float] = None
    lines: List[ScheduleLine] = field(default_factory=list)


@dataclass
class NineJ:
    vendor_id: str = ""
    vendor_name: str = ""
    buyer: str = ""
    release_date: Optional[date] = None
    release_id: str = ""
    items: List[Item] = field(default_factory=list)

    @property
    def lines(self) -> List[ScheduleLine]:
        return [ln for it in self.items for ln in it.lines]

    @property
    def pos(self) -> List[str]:
        seen = []
        for it in self.items:
            if it.po and it.po not in seen:
                seen.append(it.po)
        return seen


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _date(s: str) -> date:
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise NineJParseError(f"Unrecognized date: {s!r}")


def _header_field(text: str, label: str, stop_labels: List[str]) -> str:
    """Value after 'Label:' up to the next known label on the same line."""
    m = re.search(rf"{re.escape(label)}:\s*(.*)", text)
    if not m:
        return ""
    value = m.group(1)
    for stop in stop_labels:
        idx = value.find(stop + ":")
        if idx != -1:
            value = value[:idx]
    return value.strip()


_HEADER_LABELS = ["Vendor ID", "Vendor Name", "Attention", "Phone", "Fax", "Contact",
                  "Buyer", "Release Date", "Release ID"]


def parse_text(text: str) -> NineJ:
    doc = NineJ(
        vendor_id=_header_field(text, "Vendor ID", _HEADER_LABELS),
        vendor_name=_header_field(text, "Vendor Name", _HEADER_LABELS),
        buyer=_header_field(text, "Buyer", _HEADER_LABELS),
        release_id=_header_field(text, "Release ID", _HEADER_LABELS),
    )
    rd = _header_field(text, "Release Date", _HEADER_LABELS)
    if rd:
        try:
            doc.release_date = _date(rd.split()[0])
        except NineJParseError:
            pass

    lines = [ln.strip() for ln in text.splitlines()]
    item: Optional[Item] = None
    interval = ""
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("Last Receipt Date") and i + 1 < len(lines):
            m = ITEM_LINE.match(lines[i + 1])
            if m:
                item = Item(hendrickson_item=m.group("item"), description=m.group("desc").strip())
                if m.group("last_date"):
                    item.last_receipt_date = _date(m.group("last_date"))
                    item.last_receipt_qty = _num(m.group("last_qty"))
                doc.items.append(item)
                interval = ""
                i += 2
                continue
        if re.match(r"^P\.?\s?O\.?\s*#", ln) and item is not None and i + 1 < len(lines):
            m = PO_LINE.match(lines[i + 1])
            if m:
                item.po = m.group("po")
                item.supplier_item = m.group("supplier_item")
                item.revision = m.group("rev") or ""
                i += 2
                continue
        m = SCHEDULE_LINE.match(ln)
        if m:
            if m.group("interval"):
                interval = m.group("interval")
            if item is None:  # schedule line without a header; keep it anyway
                item = Item(hendrickson_item=m.group("part"))
                doc.items.append(item)
            item.lines.append(ScheduleLine(
                part=m.group("part"),
                delivery_date=_date(m.group("date")),
                firm_plan=m.group("fp"),
                req_qty=_num(m.group("req")),
                cum_qty=_num(m.group("cum")),
                net_qty=_num(m.group("net")),
                interval=interval,
                reference=(m.group("ref") or "").strip(),
                po=item.po,
                item_description=item.description,
                item=item.hendrickson_item,
            ))
        i += 1

    # PO is read after the item line but before schedule lines; backfill to be safe.
    for it in doc.items:
        for sl in it.lines:
            sl.po = sl.po or it.po
            sl.item_description = sl.item_description or it.description
            sl.item = sl.item or it.hendrickson_item

    if not doc.lines:
        raise NineJParseError("No schedule lines found. Is this a Hendrickson 9J supplier schedule?")
    return doc


def parse_pdf(path: str) -> NineJ:
    with pdfplumber.open(path) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    if not text.strip():
        raise NineJParseError("The PDF has no readable text (it may be a scanned image).")
    return parse_text(text)
