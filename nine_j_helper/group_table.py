"""A scrollable table that draws one thick box around each group of rows (one per part number).

ttk.Treeview can't draw boxes around groups, and its row colors are unreliable on Windows, so
this draws everything on a Canvas. It only draws: what goes in it comes from
`planner.display_groups`.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from .planner import ADDED, NEEDS_TAB, REMOVED, SKIPPED, Group

HEADS = ("Sheet tab", "Location", "Part (Item #)", "Delivery date", "Req qty (Net)", "Status")
WIDTHS = (170, 80, 110, 100, 100, 330)  # the last column grows with the window

ROW_H = 26
GROUP_GAP = 14
BOX_W = 2
FONT = ("Segoe UI", 10)

# Preview: colored text. Summary: filled rows (green = added, red = removed).
TEXT = {ADDED: "#0a6b0a", REMOVED: "#b00020", SKIPPED: "#777777", NEEDS_TAB: "#b00020"}
FILL = {ADDED: "#c6efce", REMOVED: "#ffc7ce"}
FILLED_TEXT = "#1e1e1e"
LINE = "#e6e6e6"
BOX = "#4a5568"
HEAD_BG = "#e8ecf1"


def _fmt_qty(q) -> str:
    if isinstance(q, (int, float)) and not isinstance(q, bool):
        return f"{q:g}"
    return "" if q is None else str(q)


def row_values(row) -> tuple:
    d = f"{row.delivery_date:%m/%d/%Y}" if row.delivery_date else ""
    return (row.tab or "—", row.location or "—", row.part, d, _fmt_qty(row.qty), row.status)


class GroupTable(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.groups: List[Group] = []
        self.summary = False
        self.head = tk.Canvas(self, height=ROW_H + 2, highlightthickness=0, bg=HEAD_BG)
        self.body = tk.Canvas(self, highlightthickness=1, highlightbackground="#c8ced6", bg="white")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.body.yview)
        self.body.configure(yscrollcommand=self.scroll.set)
        self.head.grid(row=0, column=0, sticky="ew")
        self.body.grid(row=1, column=0, sticky="nsew")
        self.scroll.grid(row=0, column=1, rowspan=2, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.body.bind("<Configure>", lambda _e: self._redraw())
        self.body.bind("<Enter>", lambda _e: self.body.bind_all("<MouseWheel>", self._wheel))
        self.body.bind("<Leave>", lambda _e: self.body.unbind_all("<MouseWheel>"))

    def show(self, groups: List[Group], summary: bool = False) -> None:
        self.groups, self.summary = groups, summary
        self.body.yview_moveto(0)
        self._redraw()

    # ------------------------------------------------------------ drawing
    def _xs(self) -> List[int]:
        extra = max(0, self.body.winfo_width() - BOX_W * 2 - 8 - sum(WIDTHS))
        widths = list(WIDTHS[:-1]) + [WIDTHS[-1] + extra]
        xs = [BOX_W + 4]
        for w in widths:
            xs.append(xs[-1] + w)
        return xs

    def _redraw(self) -> None:
        xs = self._xs()
        self.head.delete("all")
        for i, h in enumerate(HEADS):
            self.head.create_text(xs[i] + 6, (ROW_H + 2) // 2, text=h, anchor="w",
                                  font=(FONT[0], FONT[1], "bold"))
        self.body.delete("all")
        y = GROUP_GAP // 2
        for g in self.groups:
            top = y
            for row in g.rows:
                self._draw_row(row, xs, y)
                y += ROW_H
            self.body.create_rectangle(xs[0] - 1, top, xs[-1], y, outline=BOX, width=BOX_W)
            y += GROUP_GAP
        self.body.configure(scrollregion=(0, 0, xs[-1] + 2, y))

    def _draw_row(self, row, xs, y) -> None:
        fill = FILL.get(row.kind) if self.summary else None
        if fill:
            self.body.create_rectangle(xs[0], y, xs[-1], y + ROW_H, fill=fill, outline="")
        self.body.create_line(xs[0], y + ROW_H, xs[-1], y + ROW_H, fill=LINE)
        color = FILLED_TEXT if fill else TEXT[row.kind]
        for i, val in enumerate(row_values(row)):
            self.body.create_text(xs[i] + 6, y + ROW_H // 2, text=val, anchor="w", font=FONT, fill=color)

    def _wheel(self, event) -> None:
        self.body.yview_scroll(-1 * (event.delta // 120), "units")
