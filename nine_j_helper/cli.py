"""Command-line dry run against a local .xlsx export (no Google access needed).

    py -m nine_j_helper.cli path\\to\\9J.pdf "Copy of HAUX - RMKCo.xlsx" [--out result.xlsx]
"""

from __future__ import annotations

import argparse

from . import xlsx_book
from .nine_j import parse_pdf
from .planner import SORTS, build_plan


def main(argv=None):
    ap = argparse.ArgumentParser(description="Preview how a 9J would update the sheet.")
    ap.add_argument("pdf")
    ap.add_argument("xlsx", help="Excel export of the Google Sheet (read only)")
    ap.add_argument("--sort", choices=list(SORTS), default="date",
                    help="order new rows are added in: " + "; ".join(f"{k} = {v}" for k, v in SORTS.items()))
    ap.add_argument("--location", help="Location code, if the 9J's Ship To isn't a known one")
    ap.add_argument("--out", help="write the result to this NEW .xlsx file")
    args = ap.parse_args(argv)

    doc = parse_pdf(args.pdf)
    print(f"Release {doc.release_id}  date {doc.release_date}  PO {', '.join(doc.pos)}  "
          f"{len(doc.lines)} lines")
    location = args.location or doc.location_code
    if location is None:
        raise SystemExit(f"Detected Ship To: {doc.ship_to or '(none)'} - unknown location. "
                         "Re-run with --location CODE (the code used in the sheet's Location column).")
    print(f"Ship to: {doc.ship_to}  ->  Location {location}")
    plan = build_plan(doc, xlsx_book.load_order_tabs(args.xlsx), sort=args.sort, location=location)
    for pl in plan.lines:
        ln = pl.line
        where = f"{pl.tab} row {pl.row}" if pl.row else (pl.tab or ", ".join(pl.candidates))
        print(f"  {ln.sheet_part:<12} {ln.delivery_date:%m/%d/%Y} {ln.net_qty:>8g} {ln.firm_plan:<3} "
              f"{pl.status:<17} {where}  {pl.note}")
    for rr in plan.removed:
        d = f"{rr.delivery_date:%m/%d/%Y}" if rr.delivery_date else "-"
        print(f"  REMOVE {rr.part:<12} {d} {rr.req_qty!s:>8}  {rr.tab} row {rr.row} (unshipped)")
    print(f"{plan.new_count} line(s) to add, {plan.removed_count} unshipped row(s) to remove")
    if args.out:
        xlsx_book.apply(plan, args.xlsx, args.out)
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
