"""
compare.py -- pick columns from the workbooks in sources/, choose an interval,
generate a comparison workbook in outputs/.

Run from PowerShell:   .\run.ps1
Or directly:           python compare.py

Layout notes: the root uses grid with explicit weights so no panel can be
squeezed to zero. The options column scrolls if the window is short, and the
Generate button lives outside the scroll area so it is always reachable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import tkinter as tk
from tkinter import messagebox, ttk

import pandas as pd

import sensorkit as sk
import exporter as ex

ROOT = Path(__file__).resolve().parent
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
CHECK, UNCHECK = "\u2713  ", "\u2002\u2002\u2002 "
PANEL_W = 360


def enable_dpi_awareness():
    """Stop Windows from bitmap-stretching the window on scaled displays."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


class ScrollFrame(ttk.Frame):
    """A frame whose contents scroll vertically when they don't fit."""

    def __init__(self, parent, width=PANEL_W):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0,
                                width=width)
        self.vsb = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.inner = ttk.Frame(self.canvas, padding=(0, 0, 8, 0))
        self.win = self.canvas.create_window((0, 0), window=self.inner,
                                             anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _on_inner(self, _):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self.win, width=event.width)

    def _bind_wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
            self.canvas.bind_all("<Button-4>", self._wheel)
            self.canvas.bind_all("<Button-5>", self._wheel)
        else:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(seq)

    def _wheel(self, event):
        if getattr(event, "num", None) == 4:
            delta = 1
        elif getattr(event, "num", None) == 5:
            delta = -1
        else:
            delta = int(event.delta / 120) or (1 if event.delta > 0 else -1)
        self.canvas.yview_scroll(-delta, "units")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("La Jolla sensor comparison")
        self.geometry("1100x700")
        self.minsize(860, 520)

        self.catalog: dict[str, list[sk.TableInfo]] = {}
        self.node_map: dict[str, tuple[sk.TableInfo, sk.ColumnInfo]] = {}
        self.selected: list[tuple[sk.TableInfo, sk.ColumnInfo]] = []

        self._build_ui()
        self.after(120, self.rescan)

    # ---------------------------------------------------------------- layout

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)      # body is the only growing row

        # --- row 0: toolbar ---------------------------------------------
        bar = ttk.Frame(self, padding=(10, 8))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Sources:").pack(side="left")
        self.src_var = tk.StringVar(value=str(ROOT / sk.SOURCES_DIRNAME))
        ttk.Label(bar, textvariable=self.src_var,
                  foreground="#444").pack(side="left", padx=(6, 14))
        ttk.Button(bar, text="Rescan", command=self.rescan).pack(side="left")
        ttk.Button(bar, text="Open outputs folder",
                   command=self.open_outputs).pack(side="left", padx=6)

        # --- row 1: body -------------------------------------------------
        body = ttk.Frame(self, padding=(10, 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1, minsize=380)   # tree grows
        body.columnconfigure(1, weight=0, minsize=PANEL_W)  # panel never shrinks

        self._build_tree(body)
        self._build_panel(body)

        # --- row 2: status -----------------------------------------------
        self.status = tk.StringVar(value="")
        sf = ttk.Frame(self, padding=(10, 4))
        sf.grid(row=2, column=0, sticky="ew")
        ttk.Label(sf, textvariable=self.status,
                  foreground="#333").pack(side="left")
        self.log_shown = tk.BooleanVar(value=True)
        ttk.Checkbutton(sf, text="Show log", variable=self.log_shown,
                        command=self.toggle_log).pack(side="right")

        # --- row 3: log ----------------------------------------------------
        self.log_frame = ttk.Frame(self, padding=(10, 0, 10, 8))
        self.log_frame.grid(row=3, column=0, sticky="ew")
        self.log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(self.log_frame, height=6, wrap="word",
                           font=("Consolas", 9))
        lsb = ttk.Scrollbar(self.log_frame, orient="vertical",
                            command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set, state="disabled")
        self.log.grid(row=0, column=0, sticky="ew")
        lsb.grid(row=0, column=1, sticky="ns")

    def _build_tree(self, body):
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        f = ttk.Frame(left)
        f.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(f, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.populate())
        ttk.Entry(f, textvariable=self.filter_var,
                  width=24).pack(side="left", padx=6)
        self.hide_empty = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Hide all-empty columns",
                        variable=self.hide_empty,
                        command=self.populate).pack(side="left", padx=10)

        cols = ("coverage", "unit")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings",
                                 selectmode="none")
        self.tree.heading("#0", text="workbook  /  table  /  column")
        self.tree.column("#0", width=380, minwidth=200, stretch=True)
        for c, w, t in zip(cols, (110, 70), ("non-null", "unit")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, minwidth=50, anchor="w", stretch=False)

        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<ButtonRelease-1>", self.on_click)
        self.tree.tag_configure("empty", foreground="#a00")

    def _build_panel(self, body):
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        scroller = ScrollFrame(right)
        scroller.grid(row=0, column=0, sticky="nsew")
        p = scroller.inner

        ttk.Label(p, text="Selected series",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.sel_list = tk.Listbox(p, height=7, activestyle="none",
                                   exportselection=False)
        self.sel_list.pack(fill="x", pady=(4, 4))
        row = ttk.Frame(p)
        row.pack(fill="x", pady=(0, 10))
        ttk.Button(row, text="Remove",
                   command=self.remove_selected).pack(side="left")
        ttk.Button(row, text="Clear all",
                   command=self.clear_selected).pack(side="left", padx=6)

        g = ttk.LabelFrame(p, text="Averaging", padding=8)
        g.pack(fill="x", pady=(0, 10))

        ttk.Label(g, text="Interval").grid(row=0, column=0, sticky="w", pady=3)
        self.interval = tk.StringVar(value="1h")
        ttk.Combobox(g, textvariable=self.interval, values=sk.INTERVALS,
                     width=12).grid(row=0, column=1, sticky="w")

        ttk.Label(g, text="Aggregate").grid(row=1, column=0, sticky="w", pady=3)
        self.agg = tk.StringVar(value="mean")
        ttk.Combobox(g, textvariable=self.agg, values=list(sk.AGGREGATIONS),
                     width=12, state="readonly").grid(row=1, column=1, sticky="w")

        ttk.Label(g, text="Min samples/bin").grid(row=2, column=0, sticky="w", pady=3)
        self.min_samples = tk.IntVar(value=1)
        ttk.Spinbox(g, from_=1, to=200, textvariable=self.min_samples,
                    width=10).grid(row=2, column=1, sticky="w")

        ttk.Label(g, text="Rows to keep").grid(row=3, column=0, sticky="w", pady=3)
        self.overlap = tk.StringVar(value="intersection")
        ttk.Combobox(g, textvariable=self.overlap,
                     values=["intersection", "union"], width=12,
                     state="readonly").grid(row=3, column=1, sticky="w")

        self.convert = tk.BooleanVar(value=True)
        ttk.Checkbutton(g, text="Convert \u00b0F to \u00b0C",
                        variable=self.convert).grid(row=4, column=0, columnspan=2,
                                                    sticky="w", pady=(6, 0))

        w = ttk.LabelFrame(p, text="Window (local, optional)", padding=8)
        w.pack(fill="x", pady=(0, 10))
        ttk.Label(w, text="Start").grid(row=0, column=0, sticky="w", pady=3)
        self.start = tk.StringVar()
        ttk.Entry(w, textvariable=self.start, width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(w, text="End").grid(row=1, column=0, sticky="w", pady=3)
        self.end = tk.StringVar()
        ttk.Entry(w, textvariable=self.end, width=18).grid(row=1, column=1, sticky="w")
        ttk.Label(w, text="YYYY-MM-DD or YYYY-MM-DD HH:MM",
                  foreground="#777").grid(row=2, column=0, columnspan=2,
                                          sticky="w", pady=(4, 0))

        lg = ttk.LabelFrame(p, text="Lag scan", padding=8)
        lg.pack(fill="x", pady=(0, 10))
        self.do_lag = tk.BooleanVar(value=True)
        ttk.Checkbutton(lg, text="Cross-correlate vs reference",
                        variable=self.do_lag).grid(row=0, column=0, columnspan=2,
                                                   sticky="w")
        ttk.Label(lg, text="Reference").grid(row=1, column=0, sticky="w", pady=3)
        self.lag_ref = tk.StringVar()
        self.lag_ref_box = ttk.Combobox(lg, textvariable=self.lag_ref, width=18,
                                        state="readonly")
        self.lag_ref_box.grid(row=1, column=1, sticky="w")
        ttk.Label(lg, text="Range \u00b1 hours").grid(row=2, column=0, sticky="w", pady=3)
        self.lag_hours = tk.DoubleVar(value=24.0)
        ttk.Spinbox(lg, from_=1, to=72, increment=1, textvariable=self.lag_hours,
                    width=10).grid(row=2, column=1, sticky="w")

        # Outside the scroll area, so it is always on screen.
        self.go = ttk.Button(right, text="Generate workbook",
                             command=self.generate)
        self.go.grid(row=1, column=0, sticky="ew", pady=(8, 0), ipady=6)

    def toggle_log(self):
        if self.log_shown.get():
            self.log_frame.grid()
        else:
            self.log_frame.grid_remove()

    # ---------------------------------------------------------------- catalog

    def rescan(self):
        self.write_log("Scanning sources/ ...")
        try:
            self.catalog = sk.build_catalog(ROOT)
        except FileNotFoundError as e:
            messagebox.showerror("No sources folder", str(e))
            return
        except Exception as e:
            self.write_log("Scan failed: " + str(e))
            messagebox.showerror("Scan failed", str(e))
            return
        sk._frame_cache.clear()
        n = sum(len(t.data_columns) for ts in self.catalog.values() for t in ts)
        self.write_log(f"Found {len(self.catalog)} workbook(s), "
                       f"{sum(len(v) for v in self.catalog.values())} table(s), "
                       f"{n} numeric column(s).")
        self.populate()

    def populate(self):
        self.tree.delete(*self.tree.get_children())
        self.node_map.clear()
        needle = self.filter_var.get().strip().lower()
        chosen = {(t.file, t.name, c.column) for t, c in self.selected}

        for fname, tables in self.catalog.items():
            fnode = self.tree.insert("", "end", text=fname, open=True,
                                     values=("", ""))
            any_table = False
            for t in tables:
                cols = t.data_columns
                if self.hide_empty.get():
                    cols = [c for c in cols if c.n_nonnull > 0]
                if needle:
                    cols = [c for c in cols
                            if needle in c.column.lower()
                            or needle in t.name.lower()]
                if not cols:
                    continue
                any_table = True
                tnode = self.tree.insert(
                    fnode, "end", text=t.name, open=bool(needle),
                    values=(f"{t.n_rows} rows", f"time: {t.time_basis or '?'}"))
                for c in cols:
                    mark = CHECK if (c.file, c.table, c.column) in chosen else UNCHECK
                    tags = ("empty",) if c.n_nonnull == 0 else ()
                    iid = self.tree.insert(
                        tnode, "end", text=mark + c.column, tags=tags,
                        values=(f"{c.n_nonnull}/{c.n_rows}", c.unit or "-"))
                    self.node_map[iid] = (t, c)
            if not any_table:
                self.tree.delete(fnode)

    def on_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.node_map:
            self.tree.item(iid, open=not self.tree.item(iid, "open"))
            return
        t, c = self.node_map[iid]
        key = (c.file, c.table, c.column)
        existing = [(tt, cc) for tt, cc in self.selected
                    if (cc.file, cc.table, cc.column) == key]
        if existing:
            self.selected = [x for x in self.selected if x not in existing]
            self.tree.item(iid, text=UNCHECK + c.column)
        else:
            if c.n_nonnull == 0 and not messagebox.askyesno(
                    "Empty column",
                    f"{c.column} in {c.table} has no values at all.\n\n"
                    "Add it anyway?"):
                return
            self.selected.append((t, c))
            self.tree.item(iid, text=CHECK + c.column)
        self.refresh_selected()

    def refresh_selected(self):
        self.sel_list.delete(0, "end")
        for t, c in self.selected:
            self.sel_list.insert("end", f"{c.label}   [{c.unit or '-'}]")
        labels = [c.label for _, c in self.selected]
        self.lag_ref_box["values"] = labels
        if labels and self.lag_ref.get() not in labels:
            self.lag_ref.set(labels[0])
        if not labels:
            self.lag_ref.set("")
        self.status.set(f"{len(labels)} series selected")

    def remove_selected(self):
        idx = list(self.sel_list.curselection())
        if not idx:
            messagebox.showinfo("Nothing highlighted",
                                "Click an entry in the list above first.")
            return
        for i in reversed(idx):
            self.selected.pop(i)
        self.refresh_selected()
        self.populate()

    def clear_selected(self):
        self.selected.clear()
        self.refresh_selected()
        self.populate()

    # -------------------------------------------------------------- generate

    def parse_dt(self, text):
        text = text.strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return (datetime.strptime(text, fmt)
                        .replace(tzinfo=LOCAL_TZ)
                        .astimezone(ZoneInfo("UTC")))
            except ValueError:
                continue
        raise ValueError(f"cannot read date: {text!r}")

    def generate(self):
        if not self.selected:
            messagebox.showinfo("Nothing selected",
                                "Click one or more columns in the tree first.")
            return
        if len(self.selected) < 2 and not messagebox.askyesno(
                "Only one series",
                "You've selected one series, so there's nothing to correlate "
                "against.\n\nGenerate anyway?"):
            return
        self.go.configure(state="disabled")
        self.status.set("Working ...")
        threading.Thread(target=self._generate_worker, daemon=True).start()

    def _generate_worker(self):
        try:
            start = self.parse_dt(self.start.get())
            end = self.parse_dt(self.end.get())
            self.write_log(f"Building at {self.interval.get()} / "
                           f"{self.agg.get()} / {self.overlap.get()} ...")
            res = sk.build_comparison(
                ROOT, self.selected,
                interval=self.interval.get(),
                aggregation=self.agg.get(),
                overlap=self.overlap.get(),
                min_samples=int(self.min_samples.get()),
                start=start, end=end,
                convert_units_flag=bool(self.convert.get()),
            )
            for d in res.dropped:
                self.write_log("DROPPED: " + d)
            if res.data.empty:
                raise ValueError(
                    "No rows survived. The selected series may not overlap in "
                    "time -- try 'union', a coarser interval, or a lower "
                    "minimum sample count.")

            cols = list(res.data.columns)
            self.write_log(
                f"{len(res.data)} rows, {len(cols)} series, "
                f"{res.data.index[0].tz_convert(LOCAL_TZ):%Y-%m-%d %H:%M} to "
                f"{res.data.index[-1].tz_convert(LOCAL_TZ):%Y-%m-%d %H:%M} local.")

            lag_table, ref = None, None
            if self.do_lag.get() and len(cols) > 1:
                ref = self.lag_ref.get() if self.lag_ref.get() in cols else cols[0]
                lag_table = sk.lag_scan(res.data, ref, self.interval.get(),
                                        float(self.lag_hours.get()))
                self.write_log(f"Lag scan against {ref}:")
                for _, r in lag_table.iterrows():
                    self.write_log(
                        f"   {r['series']}: r0={r['r_at_lag_0']:+.3f}  "
                        f"best {r['best_lag_h']:+.2f} h "
                        f"r={r['r_at_best_lag']:+.3f}  "
                        f"(alt {r['ambiguous_alt_h']:+.2f} h)")

            out = ROOT / sk.OUTPUTS_DIRNAME / ex.default_output_name(
                cols, self.interval.get())
            ex.write_workbook(res, ROOT, out, lag_table, ref)
            self.write_log(f"Wrote {out.name}")
            self.after(0, lambda: self.status.set(f"Wrote outputs/{out.name}"))
            self.after(0, lambda: self.done(out, res.dropped))
        except Exception as exc:
            msg = "".join(traceback.format_exception_only(
                type(exc), exc)).strip()
            self.write_log("ERROR: " + msg)
            self.after(0, lambda m=msg: messagebox.showerror("Failed", m))
            self.after(0, lambda: self.status.set("Failed -- see log below."))
        finally:
            self.after(0, lambda: self.go.configure(state="normal"))

    def done(self, path: Path, dropped=()):
        extra = ("\n\nDropped (no usable values):\n  " + "\n  ".join(dropped)
                 if dropped else "")
        if messagebox.askyesno("Done",
                               f"Wrote {path.name}{extra}\n\nOpen it now?"):
            self.open_path(path)

    # ------------------------------------------------------------------ misc

    def open_outputs(self):
        d = ROOT / sk.OUTPUTS_DIRNAME
        d.mkdir(exist_ok=True)
        self.open_path(d)

    @staticmethod
    def open_path(p: Path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(p))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(p)])
            else:
                subprocess.run(["xdg-open", str(p)])
        except Exception:
            pass

    def write_log(self, msg: str):
        def _w():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _w)


if __name__ == "__main__":
    enable_dpi_awareness()
    (ROOT / sk.SOURCES_DIRNAME).mkdir(exist_ok=True)
    (ROOT / sk.OUTPUTS_DIRNAME).mkdir(exist_ok=True)
    App().mainloop()
