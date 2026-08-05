"""
compare.py -- pick series from a project snapshot, choose an interval, generate
a comparison workbook in that project's outputs/.

Run from PowerShell:   .\run.ps1
Or directly:           python compare.py

Three modes, chosen before the main window opens:

  1. New project              pull fresh data, snapshot it, validate it
  2. Analyze current data     open the newest project
  3. Compare existing         open one project, or two side by side

Projects live one level ABOVE this repo, in la-jolla-buoy/projects/, so the
sibling extractors can see them. See project.py for the layout.

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

import archive as arch
import exporter as ex
import project as pj
import sensorkit as sk

ROOT = Path(__file__).resolve().parent
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
CHECK, UNCHECK = "\u2713  ", "\u2002\u2002\u2002 "
PANEL_W = 360

STATUS_COLORS = {pj.STATUS_OK: "#1a7f37",
                 pj.STATUS_FAILED: "#b00020",
                 pj.STATUS_INCOMPLETE: "#8a6d00"}


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


class ProgressDialog(tk.Toplevel):
    """A live log while a pull runs. A refresh takes tens of seconds and must
    never look frozen -- a silent window is indistinguishable from a hang."""

    def __init__(self, parent, title="Working"):
        super().__init__(parent)
        self.title(title)
        # Same trap as ProjectChooser: never become transient for a master that
        # is not on screen, or this dialog inherits its withdrawn state.
        if parent is not None and parent.winfo_viewable():
            self.transient(parent)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", lambda: None)   # no closing mid-pull
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Starting ...")
        ttk.Label(frame, textvariable=self.status,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.bar = ttk.Progressbar(frame, mode="indeterminate", length=460)
        self.bar.pack(fill="x", pady=(8, 8))
        self.bar.start(12)
        self.text = tk.Text(frame, height=13, width=72, wrap="word",
                            font=("Consolas", 9), state="disabled")
        self.text.pack(fill="both", expand=True)
        self.close_btn = ttk.Button(frame, text="Close", command=self.destroy,
                                    state="disabled")
        self.close_btn.pack(anchor="e", pady=(8, 0))
        _center(self, parent)

    def log(self, msg: str):
        def _w():
            self.text.configure(state="normal")
            self.text.insert("end", str(msg).rstrip() + "\n")
            self.text.see("end")
            self.text.configure(state="disabled")
            self.status.set(str(msg).strip()[:80])
        try:
            self.after(0, _w)
        except tk.TclError:
            pass

    def destroy(self):
        # An indeterminate Progressbar reschedules itself with `after`. If the
        # window is destroyed while one of those callbacks is pending, Tk prints
        # a traceback from ttk::progressbar::Autoincrement -- harmless, but it
        # looks like a crash to whoever is watching the console. Stop the
        # animation first.
        try:
            self.bar.stop()
        except Exception:
            pass
        super().destroy()

    def finish(self, msg: str):
        def _f():
            self.bar.stop()
            self.bar.configure(mode="determinate", value=100)
            self.status.set(msg)
            self.close_btn.configure(state="normal")
            self.protocol("WM_DELETE_WINDOW", self.destroy)
        try:
            self.after(0, _f)
        except tk.TclError:
            pass


def _center(win, parent=None):
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    if parent is not None and parent.winfo_viewable():
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


class ProjectChooser(tk.Toplevel):
    """The launcher. Returns a list of one or two ProjectInfo, or None."""

    def __init__(self, parent, projects_root: Path):
        super().__init__(parent)
        self.title("La Jolla sensor comparison -- choose a project")
        self.projects_root = projects_root
        self.result: list[pj.ProjectInfo] | None = None
        self.resizable(True, True)
        self.minsize(560, 420)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.nb = nb
        nb.add(self._tab_new(nb), text="New project")
        nb.add(self._tab_latest(nb), text="Analyze current data")
        nb.add(self._tab_existing(nb), text="Compare existing")

        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text=str(projects_root),
                  foreground="#777").pack(side="left")
        ttk.Button(bar, text="Cancel", command=self._cancel).pack(side="right")

        self.projects = pj.list_projects(projects_root)
        self._fill_lists()
        _center(self, parent)
        # `wm transient` ties this window's visibility to its master: Tk
        # withdraws a transient whenever its master is withdrawn. The launcher
        # runs on a deliberately hidden root, so setting it unconditionally
        # made this window withdrawn too -- invisible, with wait_window()
        # blocking forever and nothing on screen. Only claim a master that is
        # actually on screen (the File -> Switch project... case).
        if parent is not None and parent.winfo_viewable():
            self.transient(parent)
        self.deiconify()
        self.lift()
        self.focus_force()
        self.grab_set()

    # ------------------------------------------------------------ mode 1

    def _tab_new(self, nb):
        f = ttk.Frame(nb, padding=14)
        ttk.Label(f, text="Pull fresh data and snapshot it.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=520, foreground="#555", text=(
            "Fetches every configured source over the window in "
            "config/stations.yaml, normalises to a canonical long frame, runs "
            "the clock check against LJAC1, and writes an immutable project "
            "folder one level above this repo.")).pack(anchor="w", pady=(4, 12))

        row = ttk.Frame(f); row.pack(fill="x", pady=4)
        ttk.Label(row, text="Label", width=10).pack(side="left")
        self.label_var = tk.StringVar(value="session")
        ttk.Entry(row, textvariable=self.label_var, width=32).pack(side="left")

        row = ttk.Frame(f); row.pack(fill="x", pady=4)
        ttk.Label(row, text="Window", width=10).pack(side="left")
        self.days_var = tk.IntVar(value=self._default_days())
        ttk.Spinbox(row, from_=1, to=3650, textvariable=self.days_var,
                    width=8).pack(side="left")
        ttk.Label(row, text="days back from now",
                  foreground="#777").pack(side="left", padx=6)

        self.refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f, variable=self.refresh_var,
            text="Also refresh the Excel workbook (needs Excel; slow)"
        ).pack(anchor="w", pady=(10, 2))
        ttk.Label(f, wraplength=520, foreground="#777", text=(
            "Optional. The Python ingest does not need Excel. Leave this off "
            "unless you want a refreshed .xlsx snapshot inside the project.")
        ).pack(anchor="w", padx=(20, 0))

        ttk.Button(f, text="Create project",
                   command=self._create).pack(anchor="w", pady=(16, 0), ipady=4)
        return f

    def _default_days(self) -> int:
        try:
            from ingest.config import load_config
            return int(load_config(ROOT).window_days)
        except Exception:
            return 45

    def _create(self):
        label = self.label_var.get().strip() or "session"
        days = int(self.days_var.get())
        dlg = ProgressDialog(self, "Creating project")

        def worker():
            try:
                info = pj.create_project(
                    ROOT, label, projects_root=self.projects_root,
                    refresh=bool(self.refresh_var.get()),
                    window_days=days, log=dlg.log)
            except Exception as e:
                dlg.log(f"\nFAILED: {e}")
                dlg.finish("Failed")
                self.after(0, lambda: messagebox.showerror(
                    "Create failed", str(e), parent=self))
                return
            dlg.finish(f"{info.status}: {info.n_rows:,} rows")
            self.after(0, lambda: self._created(info, dlg))

        threading.Thread(target=worker, daemon=True).start()

    def _created(self, info: pj.ProjectInfo, dlg):
        if info.status == pj.STATUS_OK:
            dlg.destroy()
            self.result = [info]
            self.destroy()
            return
        # Validation failed: show the failing verdict verbatim, then let the
        # user decide. The snapshot is kept either way -- a failed pull is
        # evidence about the feed and deleting it destroys the only record.
        checks = (info.manifest.get("validation") or {}).get("clock_checks") or []
        detail = "\n".join(c.get("detail", "") for c in checks if not c.get("ok"))
        problems = "\n".join((info.manifest.get("ingest") or {}).get("problems", []))
        body = (f"Project {info.project_id} finished with status "
                f"'{info.status}'.\n\n{detail or problems or 'see validation.json'}"
                f"\n\nOpen it anyway?")
        if messagebox.askyesno("Validation failed", body, parent=self):
            dlg.destroy()
            self.result = [info]
            self.destroy()
        else:
            dlg.finish("Kept on disk, not opened")
            self.projects = pj.list_projects(self.projects_root)
            self._fill_lists()

    # ------------------------------------------------------------ mode 2

    def _tab_latest(self, nb):
        f = ttk.Frame(nb, padding=14)
        ttk.Label(f, text="Open the most recent project.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.latest_lbl = ttk.Label(f, wraplength=520, foreground="#555")
        self.latest_lbl.pack(anchor="w", pady=(6, 12))
        ttk.Button(f, text="Open latest",
                   command=self._open_latest).pack(anchor="w", ipady=4)

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=16)
        ttk.Label(f, text="No projects yet?", foreground="#555").pack(anchor="w")
        ttk.Label(f, wraplength=520, foreground="#777", text=(
            "You can still scan sources/ directly, the way this tool worked "
            "before projects existed. Legacy `time (UTC)` columns there are "
            "loaded but marked UNVERIFIED -- in this workbook that column held "
            "Pacific local time, not UTC.")).pack(anchor="w", pady=(2, 8))
        ttk.Button(f, text="Scan sources/ instead (legacy)",
                   command=self._open_legacy).pack(anchor="w")
        return f

    def _open_latest(self):
        latest = pj.latest_project(self.projects_root)
        if latest is None:
            messagebox.showinfo(
                "No projects",
                f"Nothing under {self.projects_root}.\n\n"
                "Create one on the first tab, or scan sources/ directly.",
                parent=self)
            return
        self.result = [latest]
        self.destroy()

    def _open_legacy(self):
        self.result = []          # empty list = legacy sources/ mode
        self.destroy()

    # ------------------------------------------------------------ mode 3

    def _tab_existing(self, nb):
        f = ttk.Frame(nb, padding=14)
        ttk.Label(f, text="Open a project, or select two to compare snapshots.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=520, foreground="#555", text=(
            "With two selected, every series is prefixed with its project label "
            "so the same station can be compared across pulls.")
        ).pack(anchor="w", pady=(4, 8))

        cols = ("created", "stations", "coverage", "rows", "status")
        self.tree = ttk.Treeview(f, columns=cols, show="tree headings",
                                 selectmode="extended", height=11)
        self.tree.heading("#0", text="label")
        self.tree.column("#0", width=150, stretch=True)
        for c, w in zip(cols, (120, 140, 210, 70, 110)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w", stretch=False)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._open_selected())

        btns = ttk.Frame(f); btns.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(btns, text="Open", command=self._open_selected).pack(fill="x")
        ttk.Button(btns, text="Rebuild archive",
                   command=self._rebuild_archive).pack(fill="x", pady=(6, 0))
        for tag, colour in STATUS_COLORS.items():
            self.tree.tag_configure(tag, foreground=colour)
        return f

    def _fill_lists(self):
        latest = self.projects[0] if self.projects else None
        self.latest_lbl.configure(text=(
            f"{latest.label}  ({latest.status})\n{latest.created_utc}\n"
            f"{latest.n_rows:,} rows, {len(latest.stations)} stations\n"
            f"{latest.validation_summary}" if latest else
            f"No projects under {self.projects_root} yet."))

        self.tree.delete(*self.tree.get_children())
        self.row_map = {}
        entries = list(self.projects)
        a = arch.archive_project(ROOT)
        if a is not None:
            entries.append(a)
        for p in entries:
            iid = self.tree.insert(
                "", "end", text=p.label,
                values=(p.created_utc[:16], ", ".join(p.stations)[:34],
                        p.coverage, f"{p.n_rows:,}", p.status),
                tags=(p.status,))
            self.row_map[iid] = p

    def _selected(self) -> list[pj.ProjectInfo]:
        return [self.row_map[i] for i in self.tree.selection()
                if i in self.row_map]

    def _open_selected(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("Nothing selected",
                                "Click a project in the list first.", parent=self)
            return
        if len(sel) > 2:
            messagebox.showinfo(
                "Too many", "Select at most two projects.", parent=self)
            return
        bad = [p for p in sel if p.status != pj.STATUS_OK]
        if bad and not messagebox.askyesno(
                "Project did not pass validation",
                "\n\n".join(f"{p.label} [{p.status}]\n{p.validation_summary}"
                            for p in bad) + "\n\nOpen anyway?", parent=self):
            return
        self.result = sel
        self.destroy()

    def _rebuild_archive(self):
        dlg = ProgressDialog(self, "Rebuilding archive")

        def worker():
            try:
                out = arch.rebuild(ROOT, projects_root=self.projects_root,
                                   log=dlg.log)
                dlg.log(f"\nwrote {out}")
                dlg.finish("Archive rebuilt")
            except Exception as e:
                dlg.log(f"\nFAILED: {e}")
                dlg.finish("Failed")
                return
            self.after(0, self._fill_lists)

        threading.Thread(target=worker, daemon=True).start()

    def _cancel(self):
        self.result = None
        self.destroy()


def choose_projects(projects_root: Path) -> list[pj.ProjectInfo] | None:
    """Run the chooser on a hidden root window. Returns None if cancelled."""
    root = tk.Tk()
    root.withdraw()
    dlg = ProjectChooser(root, projects_root)
    root.wait_window(dlg)
    result = dlg.result
    root.destroy()
    return result


class App(tk.Tk):
    def __init__(self, projects: list[pj.ProjectInfo] | None = None,
                 projects_root: Path | None = None):
        super().__init__()
        self.projects: list[pj.ProjectInfo] = list(projects or [])
        self.projects_root = projects_root or pj.default_projects_root(ROOT)
        self.title("La Jolla sensor comparison")
        self.geometry("1100x700")
        self.minsize(860, 520)

        self.catalog: dict[str, list[sk.TableInfo]] = {}
        self.node_map: dict[str, tuple[sk.TableInfo, sk.ColumnInfo]] = {}
        self.selected: list[tuple[sk.TableInfo, sk.ColumnInfo]] = []

        self._retitle()
        self._build_menu()
        self._build_ui()
        self.after(120, self.rescan)

    # -------------------------------------------------------------- project

    @property
    def project(self) -> pj.ProjectInfo | None:
        return self.projects[0] if self.projects else None

    @property
    def legacy_mode(self) -> bool:
        return not self.projects

    def _retitle(self):
        if self.legacy_mode:
            self.title("La Jolla sensor comparison -- sources/ (legacy)")
        else:
            ids = " + ".join(p.project_id for p in self.projects)
            self.title(f"La Jolla sensor comparison -- {ids}")

    def _build_menu(self):
        bar = tk.Menu(self)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Switch project…", command=self.switch_project)
        m.add_command(label="Rebuild archive", command=self.rebuild_archive)
        m.add_separator()
        m.add_command(label="Open project folder", command=self.open_project_dir)
        m.add_command(label="Open outputs folder", command=self.open_outputs)
        m.add_separator()
        m.add_command(label="Exit", command=self.destroy)
        bar.add_cascade(label="File", menu=m)
        self.config(menu=bar)

    def switch_project(self):
        """Reopen the chooser without restarting the app."""
        dlg = ProjectChooser(self, self.projects_root)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        self.projects = list(dlg.result)
        self.selected.clear()
        sk._frame_cache.clear()
        self._retitle()
        self._update_project_label()
        self.refresh_selected()
        self.rescan()

    def rebuild_archive(self):
        dlg = ProgressDialog(self, "Rebuilding archive")

        def worker():
            try:
                out = arch.rebuild(ROOT, projects_root=self.projects_root,
                                   log=dlg.log)
                dlg.log(f"\nwrote {out}")
                dlg.finish("Archive rebuilt")
            except Exception as e:
                dlg.log(f"\nFAILED: {e}")
                dlg.finish("Failed")

        threading.Thread(target=worker, daemon=True).start()

    def _project_caption(self) -> str:
        if self.legacy_mode:
            return f"sources/  (legacy scan of {ROOT / sk.SOURCES_DIRNAME})"
        return "   |   ".join(
            f"{p.label}  [{p.status}]  {p.created_utc[:16]}" for p in self.projects)

    def _update_project_label(self):
        if hasattr(self, "src_var"):
            self.src_var.set(self._project_caption())
        if hasattr(self, "status_dot"):
            worst = pj.STATUS_OK
            for p in self.projects:
                if p.status != pj.STATUS_OK:
                    worst = p.status
            self.status_dot.configure(
                foreground=STATUS_COLORS.get(worst, "#444")
                if not self.legacy_mode else "#8a6d00",
                text="●")

    @property
    def outputs_dir(self) -> Path:
        if self.legacy_mode:
            return ROOT / sk.OUTPUTS_DIRNAME
        return self.project.outputs_dir

    # ---------------------------------------------------------------- layout

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)      # body is the only growing row

        # --- row 0: toolbar ---------------------------------------------
        bar = ttk.Frame(self, padding=(10, 8))
        bar.grid(row=0, column=0, sticky="ew")
        self.status_dot = ttk.Label(bar, text="●", foreground="#444")
        self.status_dot.pack(side="left")
        ttk.Label(bar, text="Project:").pack(side="left", padx=(4, 0))
        self.src_var = tk.StringVar(value=self._project_caption())
        ttk.Label(bar, textvariable=self.src_var,
                  foreground="#444").pack(side="left", padx=(6, 14))
        ttk.Button(bar, text="Switch…",
                   command=self.switch_project).pack(side="left")
        ttk.Button(bar, text="Rescan", command=self.rescan).pack(side="left", padx=6)
        ttk.Button(bar, text="Open outputs folder",
                   command=self.open_outputs).pack(side="left")
        self._update_project_label()

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
        # Amber, not red: an unverified time column is loaded and usable, it
        # just has no evidence behind its label. See sensorkit's time contract.
        self.tree.tag_configure("unverified", background="#FFF2CC")

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

        # Surface minus 5 m. Offered rather than assumed: it only means anything
        # when both endpoints are selected.
        self.strat = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            g, text="Add stratification index (46254 SST \u2212 autoss T)",
            variable=self.strat).grid(row=5, column=0, columnspan=2,
                                      sticky="w", pady=(2, 0))

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
        """Build the catalog from the open project(s), never from outputs/."""
        try:
            if self.legacy_mode:
                self.write_log(f"Scanning {ROOT / sk.SOURCES_DIRNAME} (legacy) ...")
                self.catalog = sk.build_catalog(ROOT, config_root=ROOT)
                unverified = [t for tabs in self.catalog.values() for t in tabs
                              if not t.time_is_verified]
                if unverified:
                    self.write_log(
                        f"WARNING: {len(unverified)} table(s) carry an UNVERIFIED "
                        "time column. In this workbook `time (UTC)` held Pacific "
                        "local time, not UTC -- see AGENT_TASK.md 0.1. No offset "
                        "has been applied; the provenance sheet records this.")
            else:
                self.catalog = {}
                two = len(self.projects) > 1
                for p in self.projects:
                    self.write_log(f"Scanning project {p.project_id} ...")
                    self.catalog.update(sk.build_catalog_project(
                        p, config_root=ROOT,
                        label_prefix=p.label if two else None))
        except FileNotFoundError as e:
            messagebox.showerror("Nothing to scan", str(e))
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
                    tags=() if t.time_is_verified else ("unverified",),
                    values=(f"{t.n_rows} rows",
                            f"time: {t.time_basis or '?'}"
                            + ("" if t.time_is_verified else " (unverified)")))
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
                self.selected,
                interval=self.interval.get(),
                aggregation=self.agg.get(),
                overlap=self.overlap.get(),
                min_samples=int(self.min_samples.get()),
                start=start, end=end,
                convert_units_flag=bool(self.convert.get()),
                stratification=bool(self.strat.get()),
            )
            if self.strat.get() and not res.derived:
                self.write_log(
                    "Stratification index not added: it needs BOTH 46254 SST "
                    "and autoss temperature selected.")
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

            out_dir = self.outputs_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / ex.default_output_name(cols, self.interval.get())
            ex.write_workbook(res, ROOT, out, lag_table, ref,
                              project=self.project)
            self.write_log(f"Wrote {out}")
            self.after(0, lambda: self.status.set(f"Wrote {out.name}"))
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
        d = self.outputs_dir
        d.mkdir(parents=True, exist_ok=True)
        self.open_path(d)

    def open_project_dir(self):
        d = ROOT if self.legacy_mode else Path(self.project.path)
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


def main():
    enable_dpi_awareness()
    (ROOT / sk.SOURCES_DIRNAME).mkdir(exist_ok=True)
    projects_root = pj.default_projects_root(ROOT)
    projects_root.mkdir(parents=True, exist_ok=True)

    chosen = choose_projects(projects_root)
    if chosen is None:
        return 0                      # cancelled at the launcher
    if not chosen:
        # Legacy mode: no project, scan sources/ the old way.
        (ROOT / sk.OUTPUTS_DIRNAME).mkdir(exist_ok=True)
    App(chosen, projects_root).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
