"""Tkinter window: Browse for a 9J PDF, preview, and update the Google Sheet."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__, config, google_book
from .nine_j import NineJParseError, parse_pdf
from .planner import DUPLICATE, NEW, UNROUTED, build_plan

TITLE = f"9J Helper {__version__}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        self.geometry("900x560")
        self.minsize(700, 420)
        self.cfg = config.Config.load()
        self.client = None
        self.book = None
        self.tabs = None
        self.doc = None
        self.plan = None
        self.jobs: "queue.Queue" = queue.Queue()
        self._build()
        self.after(100, self._poll)
        self._refresh_sheet_label()

    # ------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Google Sheet:").pack(side="left")
        self.sheet_var = tk.StringVar()
        ttk.Label(top, textvariable=self.sheet_var, font=("Segoe UI", 9, "bold")).pack(side="left", padx=6)
        ttk.Button(top, text="Change sheet…", command=self.change_sheet).pack(side="right")

        row = ttk.Frame(self)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="9J PDF:").pack(side="left")
        self.file_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.file_var, state="readonly").pack(side="left", fill="x", expand=True, padx=6)
        self.browse_btn = ttk.Button(row, text="Browse…", command=self.browse)
        self.browse_btn.pack(side="right")

        self.info_var = tk.StringVar(value="Click Browse and pick the 9J PDF.")
        ttk.Label(self, textvariable=self.info_var, wraplength=860, justify="left").pack(fill="x", **pad)

        cols = ("tab", "part", "date", "qty", "fp", "status")
        heads = ("Sheet tab", "Part (Item #)", "Delivery date", "Req qty (Net)", "Firm/Plan", "Status")
        widths = (170, 110, 100, 70, 70, 260)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="none")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("new", foreground="#0a6b0a")
        self.tree.tag_configure("dup", foreground="#777777")
        self.tree.tag_configure("unrouted", foreground="#b00020")
        self.tree.tag_configure("removed", foreground="#b00020")

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        self.status_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")
        self.update_btn = ttk.Button(bottom, text="Update Sheet", command=self.update_sheet, state="disabled")
        self.update_btn.pack(side="right")

    def _refresh_sheet_label(self):
        if self.cfg.spreadsheet_id:
            self.sheet_var.set(self.cfg.spreadsheet_title or self.cfg.spreadsheet_id)
        else:
            self.sheet_var.set("(not chosen yet)")

    # ------------------------------------------------------------ background work
    def _run(self, label, fn, done):
        self.status_var.set(label)
        self.browse_btn.configure(state="disabled")
        self.update_btn.configure(state="disabled")

        def worker():
            try:
                self.jobs.put((done, fn(), None))
            except Exception as e:  # noqa: BLE001 - shown to the user
                traceback.print_exc()
                self.jobs.put((done, None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, result, err = self.jobs.get_nowait()
                self.browse_btn.configure(state="normal")
                self.status_var.set("")
                done(result, err)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ------------------------------------------------------------ actions
    def change_sheet(self) -> bool:
        url = simpledialog.askstring(
            "Google Sheet",
            "Open the Google Sheet in your browser, copy the address bar, and paste it here:",
            parent=self)
        if not url:
            return False
        sid = config.spreadsheet_id_from(url)
        if not sid:
            messagebox.showerror(TITLE, "That doesn't look like a Google Sheets link.")
            return False
        self.cfg.spreadsheet_id = sid
        self.cfg.spreadsheet_title = ""
        self.cfg.save()
        self.book = None
        self.tabs = None
        self._refresh_sheet_label()
        if self.doc:
            self._load_sheet_and_plan()
        return True

    def browse(self):
        path = filedialog.askopenfilename(
            title="Choose the 9J PDF", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            initialdir=self.cfg.last_folder or os.path.expanduser("~"))
        if not path:
            return
        self.cfg.last_folder = os.path.dirname(path)
        self.cfg.save()
        self.file_var.set(path)
        try:
            self.doc = parse_pdf(path)
        except NineJParseError as e:
            self.doc = None
            messagebox.showerror(TITLE, f"Couldn't read this 9J:\n\n{e}")
            return
        except Exception as e:  # noqa: BLE001
            self.doc = None
            messagebox.showerror(TITLE, f"Couldn't open the PDF:\n\n{e}")
            return
        d = self.doc
        self.info_var.set(
            f"Release {d.release_id}  ·  released {d.release_date:%m/%d/%Y}  ·  PO {', '.join(d.pos)}  ·  "
            f"{len(d.items)} parts, {len(d.lines)} lines" if d.release_date else
            f"Release {d.release_id}  ·  PO {', '.join(d.pos)}  ·  {len(d.lines)} lines")
        if not self.cfg.spreadsheet_id and not self.change_sheet():
            return
        self._load_sheet_and_plan()

    def _load_sheet_and_plan(self):
        sid = self.cfg.spreadsheet_id

        def work():
            if self.client is None:
                self.client = google_book.connect()
            book = google_book.open_book(self.client, sid)
            return book, google_book.load_order_tabs(book)

        def done(result, err):
            if err:
                messagebox.showerror(TITLE, f"Couldn't open the Google Sheet:\n\n{err}")
                return
            self.book, self.tabs = result
            if not self.tabs:
                messagebox.showerror(TITLE, "No order tabs (with a 'Part Number' / 'Delivery Date' "
                                            "header) were found in that sheet.")
                return
            if self.cfg.spreadsheet_title != self.book.title:
                self.cfg.spreadsheet_title = self.book.title
                self.cfg.save()
                self._refresh_sheet_label()
            self._make_plan()

        self._run("Reading the Google Sheet… (a browser window may open to sign in)", work, done)

    def _make_plan(self):
        self.plan = build_plan(self.doc, self.tabs, self.cfg.part_tabs)
        # Ask about parts we can't place, one question per part.
        asked = set()
        for pl in self.plan.unrouted:
            part = pl.line.sheet_part
            if part in asked:
                continue
            asked.add(part)
            choice = self._ask_tab(part, pl.candidates or list(self.tabs), pl.note)
            if choice:
                self.cfg.part_tabs[part] = choice
        if asked:
            self.cfg.save()
            self.plan = build_plan(self.doc, self.tabs, self.cfg.part_tabs)
        self._show_plan()

    def _ask_tab(self, part, options, note):
        win = tk.Toplevel(self)
        win.title("Which tab?")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text=f"Part {part} ({note}).\nWhich tab should its orders go on?",
                  justify="left").pack(padx=12, pady=8)
        var = tk.StringVar(value=options[0])
        ttk.Combobox(win, textvariable=var, values=options, state="readonly", width=35).pack(padx=12)
        result = {"v": None}

        def ok():
            result["v"] = var.get()
            win.destroy()
        btns = ttk.Frame(win)
        btns.pack(pady=10)
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Skip this part", command=win.destroy).pack(side="left", padx=4)
        self.wait_window(win)
        return result["v"]

    def _show_plan(self):
        self.tree.delete(*self.tree.get_children())
        for pl in self.plan.lines:
            ln = pl.line
            status = pl.status
            if pl.status == NEW:
                status = f"new → row {pl.row}" + (
                    " (after removals)" if self.plan.writes[pl.tab].delete_rows else "")
                tag = "new"
            elif pl.status == DUPLICATE:
                tag = "dup"
            else:
                status = UNROUTED + " (skipped)"
                tag = "unrouted"
            self.tree.insert("", "end", tags=(tag,), values=(
                pl.tab or "—", ln.sheet_part, f"{ln.delivery_date:%m/%d/%Y}", f"{ln.net_qty:g}",
                ln.firm_plan, status))
        for rr in self.plan.removed:
            self.tree.insert("", "end", tags=("removed",), values=(
                rr.tab, rr.part, f"{rr.delivery_date:%m/%d/%Y}" if rr.delivery_date else "",
                f"{rr.req_qty:g}" if isinstance(rr.req_qty, (int, float)) else (rr.req_qty or ""),
                "", f"REMOVE row {rr.row} (not shipped; replaced by the 9J)"))
        n, gone = self.plan.new_count, self.plan.removed_count
        dup = sum(1 for pl in self.plan.lines if pl.status == DUPLICATE)
        self.status_var.set(f"{n} line(s) to add, {gone} unshipped row(s) to remove, "
                            f"{dup} already shipped on the sheet.")
        self.update_btn.configure(state="normal" if self.plan.has_changes else "disabled")

    def update_sheet(self):
        if not self.plan or not self.plan.has_changes:
            return
        tabs = ", ".join(self.plan.writes)
        if not messagebox.askyesno(
                TITLE, f"Remove {self.plan.removed_count} unshipped row(s) and add "
                       f"{self.plan.new_count} line(s) on: {tabs}?"):
            return
        plan, book = self.plan, self.book

        def done(result, err):
            if err:
                messagebox.showerror(TITLE, f"The sheet was NOT updated:\n\n{err}")
                return
            messagebox.showinfo(TITLE, f"Done! Removed {plan.removed_count} unshipped row(s) and "
                                       f"added {result} line(s).")
            self.plan = None
            self.update_btn.configure(state="disabled")
            # No re-read of the sheet: it would see the rows we just added as stale unshipped
            # rows. Show what was done until a new 9J is chosen.
            self._show_summary(plan, result)

        self._run("Updating the Google Sheet…", lambda: google_book.apply(book, plan), done)

    def _show_summary(self, plan, added):
        """Replace the preview with a record of what the update just did."""
        self.tree.delete(*self.tree.get_children())
        for rr in plan.removed:
            self.tree.insert("", "end", tags=("removed",), values=(
                rr.tab, rr.part, f"{rr.delivery_date:%m/%d/%Y}" if rr.delivery_date else "",
                f"{rr.req_qty:g}" if isinstance(rr.req_qty, (int, float)) else (rr.req_qty or ""),
                "", f"REMOVED (was row {rr.row})"))
        for pl in plan.lines:
            if pl.status != NEW:
                continue
            ln = pl.line
            self.tree.insert("", "end", tags=("new",), values=(
                pl.tab, ln.sheet_part, f"{ln.delivery_date:%m/%d/%Y}", f"{ln.net_qty:g}",
                ln.firm_plan, f"ADDED at row {pl.row}"))
        skipped = sum(1 for pl in plan.lines if pl.status == DUPLICATE)
        self.info_var.set(
            f"Update complete: removed {plan.removed_count} unshipped row(s), added {added} line(s)"
            + (f", skipped {skipped} already shipped" if skipped else "")
            + ". Click Browse to load another 9J.")
        self.status_var.set("Sheet updated.")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
