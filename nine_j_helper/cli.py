"""Command-line dry run against a local .xlsx export (no Google access needed).

    py -m nine_j_helper.cli path\\to\\9J.pdf "Copy of HAUX - RMKCo.xlsx" [--out result.xlsx]
"""

from __future__ import annotations

import argparse

from . import xlsx_book
from .nine_j import parse_pdf
from .planner import build_plan, load_tabs


def main(argv=None):
    ap = argparse.ArgumentParser(description="Preview what a 9J would add to the sheet.")
    ap.add_argument("pdf")
    ap.add_argument("xlsx", help="Excel export of the Google Sheet (read only)")
    ap.add_argument("--out", help="write the result to this NEW .xlsx file")
    args = ap.parse_args(argv)

    doc = parse_pdf(args.pdf)
    print(f"Release {doc.release_id}  date {doc.release_date}  PO {', '.join(doc.pos)}  "
          f"{len(doc.lines)} lines")
    plan = build_plan(doc, load_tabs(xlsx_book.read_rows(args.xlsx)))
    for pl in plan.lines:
        ln = pl.line
        where = f"{pl.tab} row {pl.row}" if pl.row else (pl.tab or ", ".join(pl.candidates))
        print(f"  {ln.sheet_part:<12} {ln.delivery_date:%m/%d/%Y} {ln.net_qty:>8g} {ln.firm_plan:<3} "
              f"{pl.status:<17} {where}  {pl.note}")
    if args.out:
        xlsx_book.apply(plan, args.xlsx, args.out)
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
