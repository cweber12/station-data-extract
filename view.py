"""
view.py -- an interactive window on the z-score chart of a PAIR of series.

WHY THIS EXISTS
    Looking at a comparison used to mean exporting a workbook and opening it.
    That is a slow loop for a question as small as "do these two line up?", and
    it is the loop that has to get shorter before marking sections of a chart
    can be worth doing at all.

WHAT IT SHARES, AND WITH WHOM
    This module does NOT import `exporter`. It shares `identity` (colour and
    legend label) and `sensorkit` (standardisation) with the workbook writer,
    so a series looks the same and is standardised the same in both, while the
    two renderers know nothing about each other.

    matplotlib is an OPTIONAL dependency, guarded the way `pywin32` already is.
    Everything on the export path must keep working on a machine that has never
    installed it, so `exporter` must never import this module.

EXACTLY TWO SERIES
    A pair is the unit. `_scatter_pair` in the exporter once took the first two
    selected columns whatever they were, and silently answered a question
    nobody asked; the caller here is expected to refuse rather than choose.

    The exporter's other rule -- that a scatter may only pair series sharing a
    unit -- is deliberately NOT inherited. Temperature against water level is
    the intended use, and it is legitimate because a z-score is unitless.

THREADING
    Everything here runs on the main thread. A ViewWindow is constructed from
    the Tk event loop with a BuildResult a worker has already finished
    producing. This module starts no threads and owns no queue: the contract on
    ProgressDialog in compare.py explains why a worker touching Tk hangs the UI
    with no error.
"""

from __future__ import annotations

import datetime as dt
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo

import annotations as ann
import identity
import sensorkit as sk

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Optional dependency. Import failure is recorded, never raised at import time,
# so compare.py can start and explain the problem instead of refusing to run.
# ---------------------------------------------------------------------------
try:
    import matplotlib
    import numpy as np
    matplotlib.use("TkAgg")
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                   NavigationToolbar2Tk)
    from matplotlib.figure import Figure
    from matplotlib.widgets import SpanSelector
    IMPORT_ERROR: Exception | None = None
except Exception as exc:                       # pragma: no cover - environment
    matplotlib = mdates = np = None
    FigureCanvasTkAgg = NavigationToolbar2Tk = Figure = None
    SpanSelector = None
    IMPORT_ERROR = exc

SPAN_HINT = ("Drag across the chart to define a region.  "
             "Click a mark to select it.")

UNAVAILABLE_MESSAGE = (
    "The chart view needs matplotlib, which is not installed.\n\n"
    "    pip install matplotlib\n\n"
    "Everything else -- pulling data and writing workbooks -- works without "
    "it, which is why it is not installed automatically."
)


def available() -> bool:
    """True when the chart view can actually be opened."""
    return IMPORT_ERROR is None


def pair_refusal(n: int) -> str | None:
    """Why a selection of `n` series cannot be viewed, or None if it can."""
    if n == 2:
        return None
    return (f"The chart view takes exactly two series, and {n} "
            f"{'is' if n == 1 else 'are'} selected. It compares a pair; taking "
            "the first two of a longer list would compare something you did "
            "not choose.")


def subtitle_for(result) -> str:
    """One line describing the build, in the workbook's idiom."""
    lo = result.data.index[0].tz_convert(LOCAL_TZ)
    hi = result.data.index[-1].tz_convert(LOCAL_TZ)
    n = len(result.data)
    return (f"{result.interval} {result.aggregation}  ·  "
            f"{lo:%Y-%m-%d %H:%M} to {hi:%Y-%m-%d %H:%M} local  ·  "
            f"{n:,} intervals")


def duration_text(td: dt.timedelta) -> str:
    """'6 h', '1 d 4 h' -- how long a marked region lasts, in words."""
    total = int(round(td.total_seconds() / 60.0))
    days, rem = divmod(total, 1440)
    hours, minutes = divmod(rem, 60)
    parts = [f"{days} d" if days else "", f"{hours} h" if hours else "",
             f"{minutes} min" if minutes else ""]
    return " ".join(p for p in parts if p) or "0 min"


@dataclass
class Band:
    """One drawn occurrence, and what it is an occurrence OF.

    A patch on its own is a rectangle. Clicking one has to resolve to the set
    and the interval it came from, or neither adjusting nor deleting can say
    what it is acting on.

    A BORROWED occurrence carries the `candidate` it came from and is otherwise
    the same record. One collection, one flag: keeping foreign bands in a
    second parallel list would mean every hit test, restyle and redraw had to
    remember both, which is exactly the accretion that had to be cleaned up
    once already.
    """
    patch: object          # what is on the axes, or None when not drawn
    markset: object
    interval: object       # the ORIGINAL stored occurrence -- its identity
    drawn: object = None   # the same occurrence clamped to this window
    candidate: object = None   # set when the band was borrowed from another pair

    @property
    def is_drawn(self) -> bool:
        return self.patch is not None

    @property
    def is_foreign(self) -> bool:
        """Drawn from a set belonging to a DIFFERENT pair.

        Read-only here on purpose. Deleting one would remove a mark from a
        comparison that is not on screen, behind a confirmation naming a set
        the person is not currently looking at.
        """
        return self.candidate is not None


class GhostError(RuntimeError):
    """A borrowed set's absent partner could not be fetched.

    Always names the key that failed and the label it was stored under. A key
    stops resolving when the study it points into is not the one it was drawn
    against, or when the series it named is no longer there -- and the LABEL is
    what still communicates when the key does not, which is precisely why a
    SeriesRef carries both.
    """


@dataclass
class Ghost:
    """The absent partner of a borrowed pair, standardised over THIS window."""
    ref: object            # the SeriesRef it was fetched by
    label: str             # legend text, carrying depth and reference frame
    x: object              # naive local time, the axis this chart is drawn on
    values: object         # z-scores, computed over the window on screen
    line: object = None    # the artist, once drawn
    borrowers: tuple = ()  # the sets that asked for it


def resolve_series(study, key: str, config_root=None):
    """(TableInfo, ColumnInfo) for a stable key, inside ONE study.

    The key is `file::table::column` and is study-scoped by construction:
    `scan_parquet` gives every study the identical key for the same station and
    variable, so resolving one against a different study would silently answer
    with another snapshot's data. That is the mechanical reason cross-study
    overlay is out of scope, and it is enforced here by only ever scanning the
    study handed in.
    """
    if study is None:
        raise GhostError(
            f"there is no study to resolve {key!r} against, so the absent "
            f"partner of that pair cannot be fetched.")
    catalog = sk.build_catalog_study(study, config_root=config_root)
    available = []
    for _f, tables in sorted(catalog.items()):
        for t in tables:
            for c in t.data_columns:
                if c.key == key:
                    return t, c
                available.append(c.key)
    raise GhostError(
        f"no series with the key {key!r} is in study "
        f"{getattr(study, 'study_id', '?')}. It held {len(available)} series "
        f"when this was checked. The key is what re-fetches a series, and a "
        f"renamed or rebuilt source breaks it.")


def load_ghost(study, ref, *, interval: str, aggregation: str, lo, hi,
               config_root=None) -> Ghost:
    """Fetch a series by key and standardise it OVER THE CURRENT WINDOW.

    Standardised over this window rather than over its whole extent, and with
    `sensorkit.zscore` rather than an expression written here, so the ghost is
    comparable to the lines already plotted instead of to itself. `ddof=0` is
    the detail that would drift if this reimplemented it; see zscore.

    Built through `build_comparison` at the SAME interval and aggregation as
    the chart, so a 1 h mean is compared with a 1 h mean.
    """
    table, col = resolve_series(study, ref.key, config_root)
    try:
        res = sk.build_comparison(
            [(table, col)], interval=interval, aggregation=aggregation,
            overlap="union", min_samples=1, start=lo, end=hi,
            convert_units_flag=True, stratification=False)
    except Exception as exc:
        raise GhostError(
            f"{ref.label} ({ref.key}) could not be loaded: {exc}") from None
    if res.data.empty or not res.data.columns.size:
        raise GhostError(
            f"{ref.label} ({ref.key}) resolved, but has no data inside this "
            f"window. There is nothing to draw, which is not the same as a "
            f"flat line and must not look like one.")

    column = res.data.columns[0]
    z = sk.zscore(res.data)[column]
    if not bool(z.notna().any()):
        raise GhostError(
            f"{ref.label} ({ref.key}) is entirely empty inside this window "
            f"after standardising, so a ghost line would be a blank claim.")

    # Depth and reference frame on the ghost's legend entry too. It is a series
    # on a chart, and the rule that every legend entry states where the sensor
    # sits does not stop applying because the line is faint.
    label = identity.legend_label(column, res.units.get(column, ""), False,
                                  res.geometry.get(column, ""))
    return Ghost(ref=ref, label=f"{label} — ghost: context, not compared",
                 x=res.data.index.tz_convert(LOCAL_TZ).tz_localize(None),
                 values=z.to_numpy())


class MarkDialog(tk.Toplevel):
    """Give a region a label and a reason.

    Constructing does NOT block. `ask()` is what makes it modal and waits, so
    the window can be built and inspected without a modal loop standing in the
    way -- the same split as ViewWindow.rejection_message / report_rejections,
    for the same reason.
    """

    def __init__(self, parent, *, summary: str, coverage_lines, known):
        super().__init__(parent)
        self.result_value = None
        self.known = {s.name: s.reason for s in known}

        self.title("Mark this region")
        self.resizable(False, False)
        if parent is not None and parent.winfo_viewable():
            self.transient(parent)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=summary,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")

        # What the data under this region actually was. A mark over a stretch
        # the QC screen emptied would otherwise read exactly like a mark over a
        # measured event.
        for line in coverage_lines:
            ttk.Label(body, text=line, foreground="#777").pack(anchor="w")

        ttk.Label(body, text="Name", font=("Segoe UI", 9, "bold")).pack(
            anchor="w", pady=(12, 0))
        ttk.Label(body, foreground="#777", wraplength=460, justify="left",
                  text="Reuse a name to record this as another occurrence of "
                       "the same thing.").pack(anchor="w")
        self.name_var = tk.StringVar()
        self.name_box = ttk.Combobox(body, textvariable=self.name_var,
                                     values=sorted(self.known), width=52)
        self.name_box.pack(anchor="w", pady=(2, 0))
        self.name_box.bind("<<ComboboxSelected>>", self._prefill_reason)

        ttk.Label(body, text="Reason", font=("Segoe UI", 9, "bold")).pack(
            anchor="w", pady=(10, 0))
        ttk.Label(body, foreground="#777", wraplength=460, justify="left",
                  text="Why it matters. Someone reading this later — "
                       "including you — has only this.").pack(anchor="w")
        self.reason_box = tk.Text(body, width=54, height=3, wrap="word")
        self.reason_box.pack(anchor="w", pady=(2, 0))

        ttk.Label(body, foreground="#777", wraplength=460, justify="left",
                  text="Saved the moment you confirm. There is no save step.").pack(
            anchor="w", pady=(10, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(
            side="right")
        ttk.Button(buttons, text="Save mark", command=self._accept).pack(
            side="right", padx=(0, 6))

        self.bind("<Escape>", lambda _e: self._cancel())
        self.name_box.focus_set()

    def _prefill_reason(self, _event=None):
        """Adopt the set's existing reason, so it is edited rather than lost."""
        reason = self.known.get(self.name_var.get())
        if reason:
            self.reason_box.delete("1.0", "end")
            self.reason_box.insert("1.0", reason)

    def _accept(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showinfo(
                "The mark needs a name",
                "Its meaning has to survive outside your head, and an unnamed "
                "band cannot say anything to anyone.", parent=self)
            return
        self.result_value = (name,
                             self.reason_box.get("1.0", "end").strip())
        self.destroy()

    def _cancel(self):
        self.result_value = None
        self.destroy()

    def ask(self):
        """Go modal and wait. Returns (name, reason) or None."""
        self.grab_set()
        self.wait_window(self)
        return self.result_value


class ViewWindow(tk.Toplevel):
    """The z-score chart of a pair, in a window. Read-only for now."""

    def __init__(self, parent, result, study=None, annotations_dir=None):
        """`annotations_dir` defaults to the study's.

        Taken as a path rather than read off the study on purpose: the store is
        a pure function of a directory, so the window has no business knowing
        how a study lays itself out, and a gate can point it somewhere harmless.
        """
        if not available():
            raise RuntimeError(UNAVAILABLE_MESSAGE)
        super().__init__(parent)

        self.result = result
        self.cols = list(result.data.columns)
        self.zframe = sk.zscore(result.data)     # the SAME function the
                                                 # workbook standardises with

        # The UTC index is the TRUTH about time in this window. `_xnum` is the
        # same instants as matplotlib date numbers on the naive-local axis,
        # built position for position from it, which is what lets a drag be
        # resolved to an index and read back out of `_utc` without any zone
        # ever being inferred. See annotations.snap_span.
        self._utc = result.data.index
        self._xnum = []
        self.selection: tuple | None = None
        self._selected_indices = (0, 0)
        self.occurrences = []
        self.selected_band = None

        # Which foreign sets are borrowed onto this chart. Held as IDS, not as
        # loaded sets: every redraw reloads from disk, because the file is the
        # source of truth, and a set held in memory across a redraw would go on
        # drawing bands that another window had already deleted.
        self.overlay_ids = []
        self.candidates = []
        self.overlays = []

        # The absent partner of each borrowed pair. Cached by key -- including
        # the FAILURES, so a key that does not resolve is not re-scanned on
        # every redraw -- because the study is immutable evidence and cannot
        # answer differently between two redraws of one window.
        self.ghosts = []
        self.ghost_problems = []
        self._ghost_cache = {}

        self.study = study
        self.study_id = getattr(study, "study_id", None)
        if annotations_dir is None and study is not None:
            annotations_dir = study.annotations_dir
        self.annotations_dir = annotations_dir

        self.title(self._title_text(study))
        self.geometry("1180x680")
        self.minsize(720, 420)
        # Never become transient for a master that is not on screen, or this
        # window inherits its withdrawn state and is built but never seen.
        if parent is not None and parent.winfo_viewable():
            self.transient(parent)

        self._build(study)

    # ---------------------------------------------------------------- layout

    def _title_text(self, study) -> str:
        who = f"{study.study_id}  —  " if study is not None else ""
        return f"{who}{'  vs  '.join(self.cols)}"

    def _build(self, study):
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Standardised (z-score)",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text=subtitle_for(self.result),
                  foreground="#777").pack(anchor="w", pady=(0, 6))

        self.figure = self._figure()
        self._load_marks()
        self._draw_marks()
        self._refresh_legend()

        # Chart on the left, the list of marks on the right. The list is not a
        # convenience: an occurrence clipped out of this window is drawn
        # nowhere, so without it there would be no way to select one, and no
        # way to delete it short of rebuilding a wider window.
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        canvas = FigureCanvasTkAgg(self.figure, master=left)
        self.canvas = canvas
        canvas.draw()

        toolbar_holder = ttk.Frame(left)
        toolbar_holder.pack(fill="x")
        self.toolbar = NavigationToolbar2Tk(canvas, toolbar_holder,
                                            pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side="left")

        canvas.get_tk_widget().pack(fill="both", expand=True)

        self._build_mark_list(body)

        # The readout sits directly under the chart because it is read WHILE
        # dragging, not after. Its whole purpose is that the hour being aimed
        # at is legible -- placing a band by eye against an axis labelled
        # `mm-dd` is the thing this feature replaces.
        self.span_text = tk.StringVar(value=SPAN_HINT)
        readout = ttk.Frame(frame)
        readout.pack(fill="x", pady=(6, 0))
        ttk.Label(readout, textvariable=self.span_text,
                  font=("Segoe UI", 10)).pack(side="left")

        self._install_span_selector()

        note = ttk.Frame(frame)
        note.pack(fill="x", pady=(8, 0))
        ttk.Label(note, text=self._coverage_note(),
                  foreground="#777").pack(side="left")
        ttk.Button(note, text="Close", command=self.destroy).pack(side="right")
        self.delete_btn = ttk.Button(note, text="Delete mark",
                                     command=self.prompt_delete,
                                     state="disabled")
        self.delete_btn.pack(side="right", padx=(0, 6))
        # Delete key as well as the button. There is no text entry in this
        # window, so the key cannot be stolen from something being typed into.
        self.bind("<Delete>", lambda _e: self.prompt_delete())

        marks = ttk.Frame(frame)
        marks.pack(fill="x", pady=(2, 0))
        self.marks_label = ttk.Label(
            marks, text=self.marks_note, wraplength=1100, justify="left",
            foreground="#B4531A" if self._marks_need_attention() else "#777")
        self.marks_label.pack(side="left")

        # Borrowing. The dropdown offers only sets sharing EXACTLY ONE series
        # with this pair -- see annotations.eligible_overlays for why that is
        # the rule rather than "everything in the study".
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(8, 0))
        ttk.Label(controls, text="Borrow marks from:").pack(side="left")
        self.overlay_var = tk.StringVar()
        self.overlay_box = ttk.Combobox(controls, textvariable=self.overlay_var,
                                        state="readonly", width=62)
        self.overlay_box.pack(side="left", padx=(6, 6))
        self.overlay_btn = ttk.Button(controls, text="Overlay",
                                      command=self.prompt_overlay)
        self.overlay_btn.pack(side="left")
        self.remove_btn = ttk.Button(controls, text="Remove overlay",
                                     command=self.prompt_remove_overlay,
                                     state="disabled")
        self.remove_btn.pack(side="left", padx=(6, 0))
        self.overlay_hint = ttk.Label(controls, foreground="#777", text="")
        self.overlay_hint.pack(side="left", padx=(10, 0))

        # What is borrowed, and what of it is NOT drawn. Its own line rather
        # than appended to the marks note: the counts belong to different
        # pairs, and one sentence carrying both would attribute neither.
        borrowed = ttk.Frame(frame)
        borrowed.pack(fill="x", pady=(2, 0))
        self.overlay_label = ttk.Label(
            borrowed, text=self.overlay_note, wraplength=1100, justify="left",
            foreground="#B4531A" if self._overlay_needs_attention() else "#777")
        self.overlay_label.pack(side="left")

        self._offer_ids = []
        self._refresh_overlay_controls()

        # NOTE the dialog is NOT raised here. A modal box opened during
        # construction blocks whoever is building the window -- including a
        # gate driving it with update() -- until somebody clicks, which is a
        # hang with no error, the same shape of bug the threading contract on
        # ProgressDialog exists to prevent. The caller raises it once the
        # window is up; see report_rejections().

    # `occurrences` is the only stored collection; these are views of it.
    # Three lists kept in step by hand is three chances for them to drift, and
    # the chart-facing code only ever wants the drawn subset.

    @property
    def bands(self) -> list:
        """The occurrences that are actually on the axes."""
        return [o for o in self.occurrences if o.is_drawn]

    @property
    def mark_patches(self) -> list:
        """Their patches, in the same order."""
        return [o.patch for o in self.bands]

    # ------------------------------------------------------------ mark list

    def _build_mark_list(self, parent):
        """Every occurrence on this pair, including the ones not drawn."""
        right = ttk.Frame(parent, padding=(10, 0, 0, 0))
        right.pack(side="right", fill="y")

        # "in this view", not "on this pair": once a set can be borrowed from
        # another pair, the older heading would be a small lie about half the
        # rows under it.
        ttk.Label(right, text="Marks in this view",
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")

        holder = ttk.Frame(right)
        holder.pack(fill="both", expand=True)
        self.mark_tree = ttk.Treeview(holder, columns=("where",),
                                      show="tree headings", height=16,
                                      selectmode="browse")
        self.mark_tree.heading("#0", text="set / occurrence")
        self.mark_tree.heading("where", text="")
        self.mark_tree.column("#0", width=250, stretch=False)
        self.mark_tree.column("where", width=86, stretch=False, anchor="e")
        bar = ttk.Scrollbar(holder, orient="vertical",
                            command=self.mark_tree.yview)
        self.mark_tree.configure(yscrollcommand=bar.set)
        self.mark_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        self.mark_tree.tag_configure("offwindow", foreground="#B4531A")
        self.mark_tree.tag_configure("borrowed", foreground="#5A5A5A")
        self.mark_tree.bind("<<TreeviewSelect>>", self._on_row_selected)
        self._row_for = {}
        self._syncing = False
        self.refresh_mark_list()

    def refresh_mark_list(self):
        """Rebuild the rows from `occurrences`. Sets are parents."""
        tree = getattr(self, "mark_tree", None)
        if tree is None:
            return
        self._syncing = True
        try:
            tree.delete(*tree.get_children())
            self._row_for = {}
            by_set = {}
            for entry in self.occurrences:
                by_set.setdefault(entry.markset.set_id, []).append(entry)
            for set_id, entries in by_set.items():
                ms = entries[0].markset
                foreign = entries[0].is_foreign
                node = tree.insert("", "end",
                                   text=f"{ms.name}  ({len(entries)})",
                                   values=("borrowed" if foreign else "",),
                                   tags=("borrowed",) if foreign else (),
                                   open=True)
                for entry in entries:
                    iv = entry.interval
                    outside = entry.patch is None
                    tags = ("borrowed",) if foreign else ()
                    if outside:
                        tags = tags + ("offwindow",)
                    row = tree.insert(
                        node, "end",
                        text=f"   {ann.local_text(iv.start_utc)} → "
                             f"{ann.local_text(iv.end_utc)}",
                        values=("outside" if outside else "",),
                        tags=tags)
                    self._row_for[row] = entry
        finally:
            self._syncing = False
        self._sync_row_selection()

    def _on_row_selected(self, _event=None):
        """A row picked in the list selects that occurrence."""
        if self._syncing:
            return
        rows = self.mark_tree.selection()
        entry = self._row_for.get(rows[0]) if rows else None
        # A set header is not an occurrence; clicking one selects nothing
        # rather than guessing which of its occurrences was meant.
        self.select_band(entry)

    def _sync_row_selection(self):
        """Point the list at whatever is selected, without looping back."""
        tree = getattr(self, "mark_tree", None)
        if tree is None:
            return
        self._syncing = True
        try:
            wanted = next((row for row, entry in self._row_for.items()
                           if entry is self.selected_band), None)
            if wanted is None:
                tree.selection_remove(*tree.selection())
            else:
                tree.selection_set(wanted)
                tree.see(wanted)
        finally:
            self._syncing = False

    # ----------------------------------------------------------------- marks

    def _load_marks(self):
        """The sets already drawn on THIS pair, in THIS study."""
        self.store = None
        self.pair_refs = None
        self.marks = []
        self.mark_problems = []
        self.omitted = 0
        self.mark_legend = []
        self.occurrences = []
        self.candidates = []
        self.overlays = []
        self.overlay_omitted = {}
        self.overlay_lost = []
        self.overlay_note = ""

        if self.annotations_dir is None:
            self.marks_note = (
                "Marks are stored in a study, and this is a legacy sources/ "
                "session, so there is nowhere to put them.")
            return

        columns = getattr(self.result, "columns", None) or {}
        missing = [c for c in self.cols if c not in columns]
        if missing:
            # A derived column has no ColumnInfo and therefore no resolvable
            # key. Without one a mark could not say what it was drawn against.
            self.marks_note = (
                f"Marking is off: {', '.join(missing)} is derived and has no "
                f"resolvable source key, so a mark could not record the pair "
                f"it was drawn against.")
            return

        self.pair_refs = tuple(ann.SeriesRef(columns[c].key, c)
                               for c in self.cols)
        self.store = ann.Store(self.annotations_dir)
        sets, self.mark_problems = self.store.load_all()
        keys = [r.key for r in self.pair_refs]
        self.marks = ann.sets_for_pair(sets, keys, self.study_id)

        # The sets that could be BORROWED. Recomputed on every load rather than
        # cached: a set drawn in another window since this one opened should be
        # offered here without reopening, and one deleted there must stop being
        # offered rather than failing when it is picked.
        self.candidates = ann.eligible_overlays(sets, keys, self.study_id)
        by_id = {c.markset.set_id: c for c in self.candidates}
        live = [by_id[i] for i in self.overlay_ids if i in by_id]
        # An overlay whose set is gone or no longer eligible is dropped, and
        # said so. Silently keeping the id would leave a chart that claims to
        # show a set it is not drawing.
        self.overlay_lost = [i for i in self.overlay_ids if i not in by_id]
        self.overlay_ids = [c.markset.set_id for c in live]
        self.overlays = live
        self.marks_note = ""            # filled in by _draw_marks

    def _draw_marks(self):
        """Native bands, then borrowed ones. Both clipped to this window."""
        self.mark_legend = []
        self.occurrences = []
        self.omitted = 0
        self.overlay_omitted = {}
        self._draw_native()
        self._draw_borrowed()
        self._draw_ghosts()
        self._append_rejection_note()

    def _band_patch(self, ax, clamped, color: str, foreign: bool):
        """One band on the axes. Borrowed ones are UNFILLED, hatched, dashed.

        Unfilled is the whole distinction: a solid block is a claim about the
        pair on screen, and a borrowed band is not a claim about it at all.

        Borrowed bands also sit ABOVE native ones. The question being asked is
        whether a borrowed window coincides with a native one, so the case
        where they overlap is precisely the interesting case -- a hatch drawn
        over a fill still reads as both, while a fill drawn over a hatch hides
        the answer exactly where it matters.
        """
        x0, x1 = (self._to_axis(clamped.start_utc),
                  self._to_axis(clamped.end_utc))
        if foreign:
            return ax.axvspan(x0, x1, facecolor="none", edgecolor="#" + color,
                              hatch="//", linewidth=1.4, linestyle=(0, (5, 3)),
                              alpha=0.9, zorder=0.5)
        return ax.axvspan(x0, x1, facecolor="#" + color, alpha=0.22,
                          edgecolor="#" + color, linewidth=1.0, zorder=0)

    def _draw_native(self):
        """One band per interval, per set drawn on THIS pair."""
        if not self.marks:
            self.marks_note = self.marks_note or "No marks on this pair yet."
            return

        ax = self.figure.axes[0]
        lo, hi = self._utc[0], self._utc[-1]
        drawn = 0

        for ms in self.marks:
            # Pairs, not just the clamped form. The clamped interval says where
            # to draw; the ORIGINAL is the occurrence's identity, and it is what
            # update_interval and delete_interval match on. Keeping only the
            # clamped one meant any mark overlapping the window edge could be
            # selected but never adjusted or deleted, because the store had
            # never held the value being looked up.
            pairs, omitted = ann.clip_to_window(ms.intervals, lo, hi)
            self.omitted += omitted
            clamped_for = {id(original): clamped for original, clamped in pairs}

            # Walked in stored order, so the list reads chronologically, and
            # EVERY occurrence is indexed -- including the ones clipped out of
            # this window, which are drawn nowhere and would otherwise be
            # reported as omitted and then impossible to select or delete.
            for original in ms.intervals:
                clamped = clamped_for.get(id(original))
                if clamped is None:
                    self.occurrences.append(Band(None, ms, original, None))
                    continue
                patch = self._band_patch(ax, clamped, ms.color, foreign=False)
                self.occurrences.append(Band(patch, ms, original, clamped))
                drawn += 1
            # One legend entry per SET, not per band -- the whole point of a
            # set is that many occurrences are one phenomenon.
            if pairs:
                self.mark_legend.append((patch, f"{ms.name}  ({len(pairs)}×)"))

        n_sets = len({id(m) for m in self.marks})
        parts = [f"{drawn} mark(s) in {n_sets} set(s) on this pair."]
        if self.omitted:
            parts.append(
                f"{self.omitted} fall outside this window and are NOT drawn — "
                f"rebuild wider to see them.")
        self.marks_note = "  ".join(parts)

    def _draw_borrowed(self):
        """The bands of every set borrowed from another pair.

        Counted and reported PER SET, separately from the native count. Merging
        the two totals would leave "3 outside this window" naming no set, and
        the whole reason the number is here is that a partial overlay must not
        pass for a complete one.
        """
        lines = []
        for set_id in self.overlay_lost:
            lines.append(
                f"An overlay was dropped: the set {set_id} is no longer in "
                f"this study, or no longer shares exactly one series with "
                f"this pair.")
        if not self.overlays:
            self.overlay_note = "\n".join(lines)
            return

        ax = self.figure.axes[0]
        lo, hi = self._utc[0], self._utc[-1]

        for cand in self.overlays:
            ms = cand.markset
            pairs, omitted = ann.clip_to_window(ms.intervals, lo, hi)
            self.overlay_omitted[ms.set_id] = omitted
            clamped_for = {id(original): clamped for original, clamped in pairs}
            patch = None
            for original in ms.intervals:
                clamped = clamped_for.get(id(original))
                if clamped is None:
                    self.occurrences.append(
                        Band(None, ms, original, None, candidate=cand))
                    continue
                patch = self._band_patch(ax, clamped, ms.color, foreign=True)
                self.occurrences.append(
                    Band(patch, ms, original, clamped, candidate=cand))
            if patch is not None:
                self.mark_legend.append(
                    (patch, f"{ms.name}  ({len(pairs)}×)  ·  borrowed from "
                            f"{ms.pair_text}"))

            line = (f"Borrowed “{ms.name}” from {ms.pair_text} — "
                    f"{len(pairs)} band(s) drawn")
            line += (f", {omitted} outside this window and NOT drawn."
                     if omitted else ".")
            lines.append(line)
        self.overlay_note = "\n".join(lines)

    def _draw_ghosts(self):
        """The absent partner of every borrowed pair, as a faint dashed line.

        ONE PER DISTINCT KEY. Two borrowed sets drawn against the same series
        are two interpretations of one thing, and drawing that series twice
        would put two identical lines on the chart and two entries in a legend
        that is already carrying four categories.

        A band says "I judged this window interesting". The ghost is what the
        series was actually doing inside it, which is the difference between
        being told and being shown.
        """
        self.ghosts = []
        self.ghost_problems = []
        wanted = {}
        for cand in self.overlays:
            ref, names = wanted.setdefault(cand.foreign.key,
                                           (cand.foreign, []))
            names.append(cand.markset.name)

        ax = self.figure.axes[0]
        lo, hi = self._utc[0], self._utc[-1]
        for _key, (ref, names) in wanted.items():
            try:
                ghost = self._ghost_for(ref, lo, hi)
            except GhostError as exc:
                # Loud, and on the same line as the omitted counts. The bands
                # stay: they are still an attributed claim about windows. What
                # must never happen is a chart that simply has no ghost on it
                # and says nothing about why.
                self.ghost_problems.append(
                    f"The ghost line for “{'”, “'.join(names)}” is MISSING: "
                    f"{exc}")
                continue
            # Grey, thin, dashed, and under the series lines. It is context: a
            # reader must not be able to mistake it for one of the two series
            # being compared, and colour is the only thing identifying a line
            # on a chart whose y axis has no units.
            ghost.line, = ax.plot(ghost.x, ghost.values, color="#8A8A8A",
                                  linewidth=1.1, linestyle=(0, (6, 3)),
                                  alpha=0.75, zorder=1.5, label=ghost.label)
            ghost.borrowers = tuple(names)
            self.ghosts.append(ghost)
            self.mark_legend.append((ghost.line, ghost.label))

        if self.ghost_problems:
            self.overlay_note = "\n".join(
                [self.overlay_note] + self.ghost_problems).strip()
        self._apply_ylim()

    def _ghost_for(self, ref, lo, hi) -> Ghost:
        """One fetch per key per window, failures included. Raises GhostError."""
        got = self._ghost_cache.get(ref.key)
        if got is None:
            try:
                got = load_ghost(self.study, ref,
                                 interval=self.result.interval,
                                 aggregation=self.result.aggregation,
                                 lo=lo, hi=hi, config_root=ROOT)
            except GhostError as exc:
                got = exc
            except Exception as exc:            # anything else, still visibly
                got = GhostError(f"{ref.label} ({ref.key}): {exc}")
            self._ghost_cache[ref.key] = got
        if isinstance(got, GhostError):
            raise got
        return got

    def _apply_ylim(self):
        """Widen y to fit the ghosts; keep the x pin exactly as it was.

        Only the X axis was ever the bug that pinning fixed: the span
        selector's rubber band is a Rectangle initialised at x = 0, the
        matplotlib epoch, and left autoscaling it dragged the chart back to
        1970. A band contributes nothing to the y limits at all -- axvspan
        spans y in AXES coordinates -- so y can be recomputed safely.

        And it has to be. A ghost standardised over a window it does not
        dominate exceeds the range of the two plotted series easily, and a line
        whose entire job is to show what a series was doing must not be cropped
        by a frame that says nothing about having cropped it.
        """
        ax = self.figure.axes[0]
        base = getattr(self, "_series_ylim", None)
        if base is None:
            return
        if not self.ghosts:
            ax.set_ylim(base)                 # exactly what it was before
            return
        lows = [base[0]]
        highs = [base[1]]
        for g in self.ghosts:
            finite = g.values[np.isfinite(g.values)]
            if finite.size:
                lows.append(float(finite.min()))
                highs.append(float(finite.max()))
        span = max(highs) - min(lows)
        pad = 0.05 * span if span > 0 else 0.5
        ax.set_ylim(min(lows) - pad, max(highs) + pad)

    def ghost_message(self) -> str:
        """What to tell someone about a ghost that could not be drawn, or ''.

        Split from showing it for the reason every other message here is: a
        modal dialog raised from inside the drawing path blocks whoever is
        driving the window, including a gate, with no error.
        """
        if not self.ghost_problems:
            return ""
        return ("The bands of the borrowed set ARE drawn, but the series they "
                "were drawn against could not be fetched, so there is no "
                "ghost line showing what it was doing:\n\n"
                + "\n\n".join(self.ghost_problems[:5]))

    def report_ghost_problems(self):
        """Raise the dialog, if there is anything to say. Caller's job."""
        msg = self.ghost_message()
        if msg:
            messagebox.showwarning("Ghost line not drawn", msg, parent=self)

    def _to_axis(self, when):
        """A UTC instant -> the naive local value the axis is drawn on.

        This IS the conversion snap_span refuses to make, and it is safe in
        this direction only. One instant has exactly one local rendering; it is
        the reverse -- a wall time back to an instant -- that is ambiguous for
        an hour each November and undefined for an hour each March.
        """
        return when.astimezone(LOCAL_TZ).replace(tzinfo=None)

    def _append_rejection_note(self):
        if self.mark_problems:
            self.marks_note += (f"  {len(self.mark_problems)} annotation "
                                f"file(s) REJECTED and not shown.")

    def _marks_need_attention(self) -> bool:
        return bool(self.mark_problems or self.omitted)

    def _overlay_needs_attention(self) -> bool:
        return bool(self.overlay_lost or self.ghost_problems
                    or any(self.overlay_omitted.values()))

    def rejection_message(self) -> str:
        """What to tell someone about files that would not load, or ''.

        Separate from showing it so the wording can be asserted without a modal
        dialog standing in the way.
        """
        if not self.mark_problems:
            return ""
        return ("These files are in the study but could not be trusted, so "
                "their marks are NOT on the chart:\n\n"
                + "\n\n".join(self.mark_problems[:5]))

    def report_rejections(self):
        """Raise the dialog, if there is anything to say. Caller's job.

        A mark that failed to load is one someone made and can no longer see,
        which is the single failure this feature must not be quiet about -- so
        it gets a dialog, not only the grey line under the chart.
        """
        msg = self.rejection_message()
        if msg:
            messagebox.showwarning("Annotation files rejected", msg,
                                   parent=self)

    def _refresh_legend(self):
        """Series first, then the sets. Bands are named or they say nothing."""
        ax = self.figure.axes[0]
        handles = list(self.series_lines.values())
        labels = [h.get_label() for h in handles]
        for patch, label in self.mark_legend:
            handles.append(patch)
            labels.append(label)
        # Two columns while the entries are short, one once a borrowed set has
        # made them long. A borrowed entry NAMES ITS SOURCE PAIR -- that is the
        # attribution, and it is most of a line on its own; two of them side by
        # side would be truncated into saying nothing.
        ncol = 1 if any(len(t) > 44 for t in labels) else 2
        rows = -(-len(labels) // ncol)
        ax.legend(handles, labels, loc="upper center",
                  bbox_to_anchor=(0.5, -0.16), ncol=ncol, frameon=False,
                  fontsize=9)
        # And give it the room it needs. The strip was sized for two entries;
        # attribution and a ghost line make four categories, and a legend that
        # outgrows its margin walks off the bottom of the figure unremarked.
        self.figure.subplots_adjust(
            bottom=min(0.50, max(0.28, 0.14 + 0.07 * rows)))

    # ------------------------------------------------------------- selecting

    def _install_span_selector(self):
        """Drag horizontally to define a region.

        `snap_values` makes the rubber band itself land on sample boundaries,
        so the rectangle on screen is the interval that would be stored rather
        than an approximation of it. The authority is still
        `annotations.snap_span`, which is applied to whatever comes back --
        snapping an already-snapped value is idempotent, and the rule belongs
        in the module a gate can reach without opening a window.

        No conflict with the toolbar: `_SelectorWidget.ignore` consults
        `canvas.widgetlock`, which zoom and pan hold while active, so a drag
        means one thing at a time.
        """
        # Marking is refused outright on an axis that doubles back. Local time
        # runs 01:00, 01:30, 01:00, 01:30 across the November fall-back, so an
        # hour of the axis is not ascending and a bisect through it would
        # return a confident wrong index. Better to say so than to store a mark
        # that means an hour other than the one that was dragged.
        # Click-to-select is wired up FIRST and unconditionally. Creating and
        # adjusting need a monotonic axis to snap against; selecting and
        # deleting do not, and a window that cannot be marked must not become
        # one whose existing marks can never be removed.
        self._press_x = None
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)

        self.descent = ann.first_descent(self._xnum)
        if self.descent is not None:
            when = ann.local_text(self._utc[self.descent])
            self.span = None
            self.span_text.set(
                f"Marking is off for this window: local time runs backwards "
                f"around {when}, where a DST fall-back makes one wall clock "
                f"hour cover two different hours of real time. Existing marks "
                f"can still be selected and deleted. Rebuild the window to "
                f"exclude it.")
            return

        ax = self.figure.axes[0]
        self.span = SpanSelector(
            ax, self._on_span_select, "horizontal",
            onmove_callback=self._on_span_move,
            useblit=False, button=1, minspan=0,
            snap_values=self._xnum if len(self._xnum) else None,
            # Edge handles, so a selected mark can be adjusted by dragging its
            # ends. Turned on only now that dragging one does something.
            interactive=True,
            handle_props=dict(color="#404040", linewidth=1.6),
            props=dict(facecolor="#808080", alpha=0.20),
        )
        self.span.set_visible(False)

        # NOTE the press/release handlers are connected above, before this
        # point, because they must exist even when the selector is refused.
        # They ride alongside onselect rather than on it: SpanSelector._release
        # only calls onselect for a zero-span click when a selection ALREADY
        # exists (`span <= minspan` is guarded by `_selection_completed`), so
        # the first click on a band would never arrive.

    # ------------------------------------------------------- selecting a mark

    def _on_press(self, event):
        """Remember where a press landed, so release can tell click from drag."""
        if event.inaxes is self.figure.axes[0]:
            self._press_x = event.xdata

    def _on_release(self, event):
        """A click that did not move is a SELECT, not a failed drag."""
        if event.inaxes is not self.figure.axes[0] or self._press_x is None:
            return
        # With no selector there is no create gesture to be confused with, and
        # snap_span cannot be trusted on the axis that refused it, so every
        # release is a click.
        moved = (self.span is not None
                 and self.span_interval(self._press_x, event.xdata) is not None)
        self._press_x = None
        if moved:
            return                       # a real drag; onselect owns it
        self.select_band_at(event.xdata)

    def band_at(self, x: float):
        """The (set, interval, patch) whose band covers x, or None.

        Hit-tested on x alone. A band spans the full height of the axes, so the
        y coordinate carries no information about which mark was clicked, and
        consulting it would only make a click near the top or bottom edge fail
        for no reason a user could see.

        NATIVE BANDS WIN a tie. Where a borrowed band overlaps one of this
        pair's own -- the coincidence the overlay exists to look for -- the
        click resolves to the mark that can actually be edited from here.
        """
        drawn = self.bands
        for entry in [e for e in drawn if not e.is_foreign] + \
                [e for e in drawn if e.is_foreign]:
            if entry.patch.get_x() <= x <= entry.patch.get_x() \
                    + entry.patch.get_width():
                return entry
        return None

    def select_band_at(self, x: float):
        """Select the mark under x, or clear the selection. Returns the entry."""
        return self.select_band(self.band_at(x))

    def select_band(self, entry):
        """Make `entry` the selected mark, or clear when None."""
        self.selected_band = entry
        self._restyle_bands()
        self._sync_delete_button()
        self._sync_row_selection()

        if entry is None:
            if self.span is not None:
                self.span.set_visible(False)
            self.selection = None
            self.span_text.set(SPAN_HINT)
            self.canvas.draw_idle()
            return None

        # Put the selector's handles ON the chosen band, so its edges are what
        # a drag adjusts. Setting extents marks the selection completed, which
        # is what makes the handles live.
        #
        # NOT for a borrowed band: it belongs to a pair that is not on screen,
        # and handles on it would invite a drag that has to be refused. It is
        # selectable so that clicking one says where it came from, and that is
        # all it is.
        shown = entry.drawn or entry.interval
        if entry.is_foreign and self.span is not None:
            self.span.set_visible(False)
        elif self.span is not None:
            self.span.set_visible(True)
            self.span.extents = (self._to_num(shown.start_utc),
                                 self._to_num(shown.end_utc))
        self.selection = (entry.interval.start_utc, entry.interval.end_utc)
        self._selected_indices = self._indices_for(shown)
        self.span_text.set(self._selected_summary(entry))
        self.canvas.draw_idle()
        return entry

    def _selected_summary(self, entry) -> str:
        iv = entry.interval
        text = (f"Selected “{entry.markset.name}”:  "
                f"{ann.local_text(iv.start_utc)}  →  "
                f"{ann.local_text(iv.end_utc)}  local"
                f"  ·  {duration_text(iv.end_utc - iv.start_utc)}")
        if entry.is_foreign:
            # Where it came from, on selection rather than only in the legend.
            # It is read-only here and the readout has to say so, or the first
            # thing anyone learns about the rule is that Delete did nothing.
            text += (f"   ·   BORROWED from {entry.markset.pair_text}, so it "
                     f"is read-only here — open that pair to change it.")
            return text
        if entry.patch is None:
            # Say why there is nothing to drag, rather than leaving someone
            # hunting for handles that cannot exist.
            text += ("   ·   outside this window, so it is not drawn: it can "
                     "be deleted, but adjusting means rebuilding wider.")
        return text

    def _restyle_bands(self):
        """The selected band reads as selected. Colour still identifies the set."""
        for entry in self.bands:
            chosen = entry is self.selected_band
            if entry.is_foreign:
                # Heavier outline, still unfilled and still hatched. Selecting
                # a borrowed band must not be the one moment it starts looking
                # like a native one.
                entry.patch.set_linewidth(2.8 if chosen else 1.4)
                entry.patch.set_alpha(1.0 if chosen else 0.9)
                continue
            entry.patch.set_alpha(0.34 if chosen else 0.22)
            entry.patch.set_linewidth(2.2 if chosen else 1.0)
            entry.patch.set_linestyle("solid" if chosen else "dotted")

    def _to_num(self, when) -> float:
        return float(mdates.date2num(self._to_axis(when)))

    def _indices_for(self, interval) -> tuple:
        """Which samples an interval's edges sit on, for coverage."""
        return ann.snap_span(self._to_num(interval.start_utc),
                             self._to_num(interval.end_utc), self._xnum)

    def span_interval(self, x0: float, x1: float):
        """A dragged span -> (start_utc, end_utc, i0, i1), or None if degenerate.

        The conversion never touches wall time. `snap_span` resolves the drag to
        two SAMPLE INDICES and the instants are read straight out of the UTC
        index, so the November hour that happens twice and the March hour that
        never happens cannot arise. Returns None when the drag was shorter than
        one sample, which marks nothing and must be refused rather than stored.
        """
        i0, i1 = ann.snap_span(x0, x1, self._xnum)
        if i0 == i1:
            return None
        return self._utc[i0], self._utc[i1], i0, i1

    def _span_summary(self, start, end, i0: int, i1: int) -> str:
        return (f"{ann.local_text(start)}  →  {ann.local_text(end)}  local"
                f"  ·  {duration_text(end - start)}"
                f"  ·  {i1 - i0 + 1} intervals of {self.result.interval}")

    def _on_span_move(self, x0: float, x1: float):
        """Live, while the mouse is still down. Shows the SNAPPED times."""
        got = self.span_interval(x0, x1)
        if got is None:
            self.span_text.set(
                f"Shorter than one {self.result.interval} interval — "
                f"nothing to mark.")
            return
        verb = (f"Adjusting “{self.selected_band.markset.name}”:  "
                if self._adjusting() else "Marking:  ")
        self.span_text.set(verb + self._span_summary(*got))

    def _adjusting(self) -> bool:
        """True when this drag moved an edge of the selected mark.

        `_active_handle` is set on press and cleared AFTER onselect fires, so
        it is still readable here -- which is the only thing distinguishing an
        adjustment from a brand new region, since both arrive as one span.
        """
        return (self.selected_band is not None
                and getattr(self.span, "_active_handle", None) is not None)

    def _on_span_select(self, x0: float, x1: float):
        """On release: adjust the selected mark, or start a new one."""
        got = self.span_interval(x0, x1)

        if self._adjusting():
            if got is None:
                # Dragged an edge past the other one. Refuse and snap back,
                # rather than storing a mark with no duration.
                self.select_band(self.selected_band)
                self.span_text.set(
                    f"An edge cannot cross the other one — a mark has to be at "
                    f"least one {self.result.interval} long. Nothing changed.")
                return
            try:
                ms = self.adjust_selected(got[0], got[1])
            except Exception as exc:
                messagebox.showerror("Could not adjust the mark", str(exc),
                                     parent=self)
                self.redraw_marks()
                return
            self.span_text.set(
                f"Adjusted “{ms.name}”:  {self._span_summary(*got)}")
            return

        self.selection = None if got is None else (got[0], got[1])
        if got is None:
            self.span_text.set(
                f"That drag was shorter than one {self.result.interval} "
                f"interval, so there is nothing to mark. Drag further.")
            return
        self.span_text.set("Region:  " + self._span_summary(*got))
        self._selected_indices = (got[2], got[3])

        if self.store is None:
            return
        # Deferred rather than opened here. This runs inside the selector's
        # release handler, and a modal loop entered while the widget is still
        # settling is asking for re-entrancy trouble. `after` from the MAIN
        # thread is fine -- what the ProgressDialog contract forbids is a
        # worker calling it.
        self.after(0, self._prompt_for_mark)

    # -------------------------------------------------------------- marking

    def _prompt_for_mark(self):
        """Ask for a label and a reason, then write. UI only; see commit_mark."""
        if self.selection is None or self.store is None:
            return
        start, end = self.selection
        i0, i1 = self._selected_indices
        dialog = MarkDialog(
            self, summary=self._span_summary(start, end, i0, i1),
            coverage_lines=self.coverage_lines(self.coverage_for(i0, i1)),
            known=self.marks)
        got = dialog.ask()
        if got is None:
            self.span_text.set(SPAN_HINT)
            return
        try:
            ms = self.commit_mark(*got)
        except Exception as exc:
            messagebox.showerror("Could not save the mark", str(exc),
                                 parent=self)
            return
        self.span_text.set(
            f"Saved to “{ms.name}” — {len(ms.intervals)} occurrence(s). "
            f"Already on disk; there is no save step.")

    def commit_mark(self, name: str, reason: str):
        """Write the current selection as a mark, and redraw. Returns the set.

        The seam. Everything above this is a dialog; everything below is the
        store. It is public so the rule can be exercised without a modal window
        standing in the way.
        """
        if self.selection is None:
            raise RuntimeError("nothing is selected")
        if self.store is None or self.pair_refs is None:
            raise RuntimeError("this window has nowhere to store a mark")
        start, end = self.selection
        i0, i1 = self._selected_indices
        ms = self.store.confirm(
            study_id=self.study_id, pair=self.pair_refs, name=name,
            reason=reason, start_utc=start, end_utc=end,
            coverage=self.coverage_for(i0, i1), tz=str(LOCAL_TZ))
        self.redraw_marks()
        return ms

    def adjust_selected(self, start_utc, end_utc):
        """Move the selected occurrence's edges, and write. Returns the set.

        The seam for adjusting, matching commit_mark for creating: no UI above
        this line, so the rule can be exercised without a window being dragged.

        Coverage is RECOMPUTED, never carried over. It describes the marked
        window, and moving an edge makes the stored numbers describe a window
        that no longer exists.
        """
        entry = self.selected_band
        if entry is None:
            raise RuntimeError("no mark is selected")
        if self.store is None:
            raise RuntimeError("this window has nowhere to store a mark")
        if entry.is_foreign:
            raise RuntimeError(self.foreign_refusal(entry, "Adjusting"))
        if entry.patch is None:
            raise RuntimeError(
                f"“{entry.markset.name}” falls outside this window, so it is "
                f"not drawn and has no edges to drag. Rebuild the window wide "
                f"enough to show it, and its edges become adjustable. It can "
                f"be deleted from here either way.")

        i0, i1 = ann.snap_span(self._to_num(start_utc),
                               self._to_num(end_utc), self._xnum)
        ms = self.store.update_interval(
            entry.markset.set_id,
            entry.interval.start_utc, entry.interval.end_utc,
            start_utc=start_utc, end_utc=end_utc,
            coverage=self.coverage_for(i0, i1))
        self.redraw_marks()
        self.select_interval(ms.set_id, start_utc, end_utc)
        return ms

    # ------------------------------------------------------------- borrowing

    def foreign_refusal(self, entry, verb: str) -> str:
        """Why a borrowed band cannot be edited from here. Names the pair.

        The pair is the point of the message. Editing a borrowed band would
        change a set belonging to a comparison that is not on screen, and the
        person doing it would be looking at a chart of two other series while
        it happened.
        """
        return (f"{verb} “{entry.markset.name}” from here is refused: it is "
                f"borrowed from {entry.markset.pair_text}, which is not the "
                f"pair on this chart. It is drawn so you can see whether its "
                f"windows line up with what is here, not to be edited through "
                f"them. Open that pair to change it.")

    def _no_candidates_reason(self) -> str:
        """Why the dropdown is empty. Never merely disabled and silent."""
        if self.store is None:
            return "There is no study here to borrow marks from."
        return ("Nothing to borrow: no saved set in this study shares exactly "
                "one series with this pair.")

    def _refresh_overlay_controls(self):
        """Rebuild the dropdown from the candidates. Selection survives it.

        Kept by SET ID rather than by the displayed string, because the string
        changes the moment a set goes on the chart -- and a selection that
        silently moved to a different set would borrow one thing while the box
        showed another.
        """
        box = getattr(self, "overlay_box", None)
        if box is None:
            return
        was = self.overlay_choice()
        self._offer_ids = [c.markset.set_id for c in self.candidates]
        values = [c.offer_text + ("   ·   on chart"
                                  if c.markset.set_id in self.overlay_ids
                                  else "")
                  for c in self.candidates]
        box.configure(values=values)
        if was in self._offer_ids:
            box.current(self._offer_ids.index(was))
        elif values:
            box.current(0)
        else:
            self.overlay_var.set("")

        live = "readonly" if values else "disabled"
        box.configure(state=live)
        self.overlay_btn.configure(state="normal" if values else "disabled")
        self.overlay_hint.configure(
            text="" if values else self._no_candidates_reason())

    def overlay_choice(self):
        """The set id the dropdown is showing, or None. The seam under the UI."""
        box = getattr(self, "overlay_box", None)
        if box is None:
            return None
        i = box.current()
        return self._offer_ids[i] if 0 <= i < len(self._offer_ids) else None

    def prompt_overlay(self):
        """Borrow whatever is chosen. UI only; the rule is in overlay()."""
        set_id = self.overlay_choice()
        if set_id is None:
            return
        # The ghost is fetched from the study's parquet on this thread. It is
        # one series over one window and the frame is cached after the first
        # read, but the first one is not instant, and a window that stops
        # repainting with no explanation reads as a hang.
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            cand = self.overlay(set_id)
        except Exception as exc:
            messagebox.showerror("Could not borrow that set", str(exc),
                                 parent=self)
            return
        finally:
            self.configure(cursor="")
        self.span_text.set(
            f"Borrowed “{cand.markset.name}” from {cand.markset.pair_text}. "
            f"Its bands are outlined, not filled.")
        # A key that no longer resolves gets a dialog, not only the note under
        # the chart. The bands are drawn and attributed; what is missing is the
        # line showing what the borrowed pair's other series was doing, and
        # that absence is exactly what nobody would notice.
        self.report_ghost_problems()

    def prompt_remove_overlay(self):
        """Stop borrowing the selected band's set. UI only.

        Deliberately says that nothing was deleted. This button sits beside
        Delete mark, acts on a band, and removes bands from the chart -- three
        good reasons for someone to wonder whether their marks are gone.
        """
        entry = self.selected_band
        if entry is None or not entry.is_foreign:
            return
        name, pair_text = entry.markset.name, entry.markset.pair_text
        self.remove_overlay(entry.markset.set_id)
        self.span_text.set(
            f"Stopped borrowing “{name}” from {pair_text}. Nothing was "
            f"deleted — the set is still saved on its own pair.")

    def overlay(self, set_id: str):
        """Borrow a saved set onto this chart, and redraw. Returns its offer.

        The seam, matching commit_mark and delete_selected: the rule lives
        here, and the dropdown above it only calls this. Eligibility is
        re-checked rather than trusted from whatever the widget was showing --
        the offer may have been built before another window deleted the set.
        """
        cand = next((c for c in self.candidates
                     if c.markset.set_id == set_id), None)
        if cand is None:
            raise RuntimeError(
                f"{set_id!r} cannot be borrowed onto this pair. A set is "
                f"offered only when it shares EXACTLY ONE series with what is "
                f"on screen, in this study: sharing both makes it the same "
                f"comparison, which loads as native marks, and sharing neither "
                f"gives it no bearing on what is drawn.")
        if set_id not in self.overlay_ids:
            self.overlay_ids.append(set_id)
        self.redraw_marks()
        return cand

    def remove_overlay(self, set_id: str) -> bool:
        """Stop drawing a borrowed set. True if it was on the chart."""
        if set_id not in self.overlay_ids:
            return False
        self.overlay_ids = [i for i in self.overlay_ids if i != set_id]
        self.redraw_marks()
        return True

    # ------------------------------------------------------------- deleting

    def deletion_message(self) -> str:
        """What deleting the selection would destroy, in full, or ''.

        Built separately from being shown so the wording can be asserted
        without a modal dialog in the way -- the same split as
        rejection_message, and for the same reason.
        """
        entry = self.selected_band
        if entry is None:
            return ""
        ms, iv = entry.markset, entry.interval
        lines = [f"Delete this mark?", "",
                 f"    {ms.name}",
                 f"    {ann.local_text(iv.start_utc)} → "
                 f"{ann.local_text(iv.end_utc)} local"]
        if ms.reason:
            lines.append(f"    “{ms.reason}”")
        lines.append("")
        if len(ms.intervals) == 1:
            # Not an ordinary delete: the set and the written reason go too,
            # and a reason cannot be regenerated from anything.
            lines.append(
                f"This is the only occurrence of “{ms.name}”, so the set is "
                f"removed as well, along with its reason.")
        else:
            lines.append(
                f"“{ms.name}” has {len(ms.intervals)} occurrences; the other "
                f"{len(ms.intervals) - 1} are kept.")
        lines.append("This cannot be undone.")
        return "\n".join(lines)

    def delete_selected(self):
        """Delete the selected occurrence, and write. Returns the set, or None.

        The seam. No confirmation here -- the caller asks first, because delete
        is the undo for this whole feature and has none of its own.
        """
        entry = self.selected_band
        if entry is None:
            raise RuntimeError("no mark is selected")
        if self.store is None:
            raise RuntimeError("this window has nowhere to delete from")
        if entry.is_foreign:
            # The trap this rule exists to close: #6 shipped click-a-band and
            # press Delete, and a borrowed band joined to that selection model
            # would remove an occurrence from another pair's set behind a
            # confirmation naming a comparison nobody is looking at.
            raise RuntimeError(self.foreign_refusal(entry, "Deleting"))
        ms = self.store.delete_interval(entry.markset.set_id,
                                        entry.interval.start_utc,
                                        entry.interval.end_utc)
        self.redraw_marks()
        self.span_text.set(SPAN_HINT)
        return ms

    def prompt_delete(self):
        """Confirm, then delete. UI only; see delete_selected."""
        if self.selected_band is None or self.store is None:
            return
        if self.selected_band.is_foreign:
            # Reached by the Delete key, which is bound window-wide; the button
            # is already disabled. Says why rather than doing nothing, which
            # would read as a bug.
            messagebox.showinfo(
                "Borrowed mark", self.foreign_refusal(self.selected_band,
                                                      "Deleting"), parent=self)
            return
        name = self.selected_band.markset.name
        if not messagebox.askyesno("Delete mark", self.deletion_message(),
                                   icon="warning", default="no", parent=self):
            return
        try:
            self.delete_selected()
        except Exception as exc:
            messagebox.showerror("Could not delete the mark", str(exc),
                                 parent=self)
            self.redraw_marks()
            return
        self.span_text.set(f"Deleted an occurrence of “{name}”.")

    def _sync_delete_button(self):
        """The buttons that act on a selection follow it. Exactly one applies:
        a native mark can be deleted, a borrowed one can only be handed back."""
        entry = self.selected_band
        button = getattr(self, "delete_btn", None)
        if button is not None:
            live = (entry is not None and self.store is not None
                    and not entry.is_foreign)
            button.configure(state="normal" if live else "disabled")
        remove = getattr(self, "remove_btn", None)
        if remove is not None:
            remove.configure(state="normal" if entry is not None
                             and entry.is_foreign else "disabled")

    def select_interval(self, set_id: str, start_utc, end_utc):
        """Re-select an occurrence by VALUE after the bands were rebuilt."""
        for entry in self.bands:
            if (entry.markset.set_id == set_id
                    and entry.interval.start_utc == start_utc
                    and entry.interval.end_utc == end_utc):
                return self.select_band(entry)
        return None

    def redraw_marks(self):
        """Reload from disk and repaint. The file is the source of truth."""
        for patch in self.mark_patches:
            patch.remove()
        for ghost in self.ghosts:
            if ghost.line is not None:
                ghost.line.remove()
        self.ghosts = []
        # Every Band held a patch that has just been removed, so any selection
        # now points at an artist that is no longer on the axes. Cleared here
        # rather than left dangling; a caller that wants the selection back
        # re-selects by value after the redraw.
        self.selected_band = None
        self._sync_delete_button()
        self._load_marks()
        self._draw_marks()
        self._refresh_legend()
        self.refresh_mark_list()
        if getattr(self, "marks_label", None) is not None:
            self.marks_label.configure(
                text=self.marks_note,
                foreground="#B4531A" if self._marks_need_attention()
                else "#777")
        if getattr(self, "overlay_label", None) is not None:
            self.overlay_label.configure(
                text=self.overlay_note,
                foreground="#B4531A" if self._overlay_needs_attention()
                else "#777")
        self._refresh_overlay_controls()
        if getattr(self, "canvas", None) is not None:
            self.canvas.draw_idle()

    # ------------------------------------------------------------- coverage

    def coverage_for(self, i0: int, i1: int) -> dict:
        """What the data under samples [i0, i1] actually was, per series.

        Captured at confirmation because it describes THIS window: rebuild at a
        different interval and the numbers change, so recomputing them later
        would answer a different question from the one that was marked.
        """
        sub = self.result.data.iloc[i0:i1 + 1]
        start, end = self._utc[i0], self._utc[i1]
        return {c: ann.coverage_entry(len(sub), int(sub[c].isna().sum()),
                                      self._suspect_count(c, start, end))
                for c in self.cols}

    def coverage_lines(self, coverage: dict) -> list:
        """Coverage as sentences. `None` must never read as 'clean'."""
        lines = []
        for label, entry in coverage.items():
            n, empty = entry["n_intervals"], entry["n_empty"]
            part = (f"{label}: {n - empty} of {n} intervals have data"
                    if empty else f"{label}: all {n} intervals have data")
            suspect = entry["n_suspect_kept"]
            if suspect is None:
                part += "  ·  QARTOD not evaluated for this series"
            elif suspect:
                part += f"  ·  {suspect} QARTOD-3 (suspect) value(s) kept"
            lines.append(part)
        return lines

    def _observations(self):
        """The study's long frame, loaded once. None when unavailable."""
        if not hasattr(self, "_obs_cache"):
            try:
                self._obs_cache = (None if self.study is None
                                   else self.study.load_observations())
            except Exception:
                self._obs_cache = None
        return self._obs_cache

    def _suspect_count(self, label: str, start, end):
        """QARTOD-3 values kept under a mark, or None when not countable.

        None rather than 0 whenever a real count cannot be produced -- the
        series carries no flags (CO-OPS water level and the yellow buoy logger
        report qc_flag as null), or it cannot be located in the study's frame.
        Zero would say CLEAN, and the truth in both cases is NOT EVALUATED.

        The flags survive into the parquet but not into a BuildResult:
        load_series takes `value` alone, so the count comes from the study's
        canonical frame rather than from what is on the chart.
        """
        import pandas as pd

        info = (getattr(self.result, "columns", None) or {}).get(label)
        obs = self._observations()
        if info is None or obs is None or obs.empty or "qc_flag" not in obs:
            return None
        sel = obs[(obs["station"].astype(str) == str(info.station))
                  & (obs["variable"].astype(str) == str(info.variable))]
        if sel.empty:
            return None
        flags = pd.to_numeric(sel["qc_flag"], errors="coerce")
        if not flags.notna().any():
            return None                     # no QARTOD on this series at all
        when = pd.to_datetime(sel["time_utc"], utc=True)
        return int(((flags == 3) & (when >= start) & (when <= end)).sum())

    def _coverage_note(self) -> str:
        """Say what is missing. An empty stretch must never pass for agreement."""
        parts = []
        for c in self.cols:
            s = self.result.data[c]
            missing = int(s.isna().sum())
            if missing:
                parts.append(f"{c}: {missing:,} of {len(s):,} intervals empty")
        return "  ·  ".join(parts) if parts else "No gaps in either series."

    # ----------------------------------------------------------------- chart

    def _labels(self) -> dict:
        units = self.result.units
        geometry = getattr(self.result, "geometry", None) or {}
        mixed = len({units.get(c, "") for c in self.cols if units.get(c)}) > 1
        return {c: identity.legend_label(c, units.get(c, ""), mixed,
                                         geometry.get(c, ""))
                for c in self.cols}

    def _figure(self):
        colors = identity.series_colors(self.cols)
        labels = self._labels()

        # Naive local time on the axis, matching how the workbook builds its
        # x values. A tz-aware index would have matplotlib pick its own
        # display zone, which is exactly the class of mistake this repo has
        # already paid for once.
        x = self.result.data.index.tz_convert(LOCAL_TZ).tz_localize(None)

        # The plotted x of every sample, in the same order as `_utc`. An
        # ARRAY, because matplotlib's own snapping does arithmetic on it
        # directly and a list raises inside _set_extents. `snap_span` and
        # `first_descent` bisect and compare, which work on any sequence, so
        # one representation serves both -- annotations stays free of the
        # numeric stack because of what it IMPORTS, not what it is handed.
        self._xnum = np.asarray(mdates.date2num(x), dtype=float)

        fig = Figure(figsize=(11.5, 5.0), dpi=100)
        ax = fig.add_subplot(111)
        # Keep a handle on the SERIES lines specifically. `ax.get_lines()` also
        # returns the zero reference line below, so anything reading lines back
        # off the axes positionally is right only by accident of draw order.
        self.series_lines = {}
        for c in self.cols:
            line, = ax.plot(x, self.zframe[c].to_numpy(), linewidth=1.4,
                            color="#" + colors[c], label=labels[c])
            self.series_lines[c] = line

        ax.set_ylabel("standard deviations")
        ax.set_xlabel("time (local)")
        ax.grid(True, color="#D9D9D9", linewidth=0.8)
        ax.axhline(0, color="#808080", linewidth=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

        # PIN THE VIEW TO THE SERIES, and stop autoscaling.
        #
        # Everything added after this point is furniture, not data: the mark
        # bands are clipped to this window by construction, and the span
        # selector's rubber band is a Rectangle it initialises at x = 0. Left
        # autoscaling, that rectangle drags the x axis back to the matplotlib
        # epoch, so the first redraw after a mark is saved rescales the chart
        # to span 1970 to now and squeezes 45 days of data into a few pixels.
        # Observed as xlim (-1033, 21704) where the data occupies (20596,
        # 20641).
        # autoscale_view computes the limits from the series; setting them
        # explicitly is what turns autoscaling off, so no further call is
        # needed and adding one would only imply it was load-bearing.
        ax.autoscale_view()
        ax.set_xlim(ax.get_xlim())
        ax.set_ylim(ax.get_ylim())
        # Kept so the frame can be restored EXACTLY when a ghost that widened
        # it is handed back. Recomputing it later from the lines would include
        # whatever else has since been drawn.
        self._series_ylim = ax.get_ylim()

        # The legend is built by _refresh_legend once the mark bands exist, so
        # that sets are named alongside the series rather than in a second
        # legend nobody reads.
        fig.subplots_adjust(left=0.06, right=0.99, top=0.97, bottom=0.28)
        return fig

    # ------------------------------------------------------------ self-check

    def plotted(self) -> dict:
        """The y data actually handed to matplotlib, per series.

        Exposed so a gate can assert what is ON THE SCREEN rather than what
        was passed in -- constructing a chart is not evidence that it drew the
        right numbers.
        """
        return {c: line.get_ydata() for c, line in self.series_lines.items()}

    def plotted_colors(self) -> dict:
        """The colour each series line was actually drawn in."""
        return {c: line.get_color().lstrip("#").upper()
                for c, line in self.series_lines.items()}


# ---------------------------------------------------------------------------
# Gate. `python view.py --check` opens the window against a real study and
# asserts the two things that matter: that it is VIEWABLE rather than merely
# constructed, and that the lines carry the same z-scores the workbook writes.
# ---------------------------------------------------------------------------

def _default_pair(study, by_key):
    """One QARTOD-flagged series and one unflagged, when the study has both.

    Not cosmetic. The suspect count has two branches -- a real number, and
    `null` for a series carrying no flags at all -- and a default pair drawn
    from one station exercises only one of them, leaving `None == None` to pass
    for a comparison that never ran. Picking one of each makes the no-argument
    gate assert the distinction that matters. Falls back to the first two, so
    this stays study-agnostic rather than naming stations.
    """
    import pandas as pd

    try:
        obs = study.load_observations()
        flags = pd.to_numeric(obs["qc_flag"], errors="coerce")
        flagged = {(s, v) for (s, v), any_flag
                   in obs.assign(_q=flags).groupby(
                       ["station", "variable"])["_q"].apply(
                           lambda c: bool(c.notna().any())).items()
                   if any_flag}
    except Exception:
        flagged = set()

    def has_flags(item):
        _t, c = item
        return (str(c.station), str(c.variable)) in flagged

    ordered = [by_key[k] for k in sorted(by_key)]
    with_flags = [i for i in ordered if has_flags(i)]
    without = [i for i in ordered if not has_flags(i)]
    if with_flags and without:
        return [with_flags[0], without[0]]
    return ordered[:2]


def _main(argv=None):
    import argparse
    import json
    import sys

    import numpy as np
    import pandas as pd

    import study as st

    # The readout reads better with real arrows, and Tk renders them happily,
    # but a Windows console defaults to cp1252 and cannot encode one -- which
    # would crash the gate on the way to printing a PASS.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="Open the z-score view on a pair of series.")
    ap.add_argument("--study", default=None,
                    help="study id; default is the most recent")
    ap.add_argument("--series", action="append", default=[], metavar="KEY",
                    help="ColumnInfo key (file::table::column); give twice")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--aggregation", default="mean")
    ap.add_argument("--overlap", default="union")
    ap.add_argument("--check", action="store_true",
                    help="assert the window is viewable and correct, then exit")
    ap.add_argument("--shot", default=None, metavar="PATH",
                    help="write the chart to a PNG and exit. With --check it "
                         "writes the gate's own window, which carries a native "
                         "set, two borrowed ones and a ghost line at once -- "
                         "the picture the overlay's visual review is of, since "
                         "no gate can assert that a borrowed band READS as "
                         "borrowed")
    args = ap.parse_args(argv)

    from pathlib import Path
    root = Path(__file__).resolve().parent

    studies = st.list_studies()
    if not studies:
        print("no studies found")
        return 1
    info = (next((s for s in studies if s.study_id == args.study), None)
            if args.study else studies[0])
    if info is None:
        print(f"no study {args.study!r}")
        return 1

    catalog = sk.build_catalog_study(info, config_root=root)
    by_key = {}
    for _f, tables in sorted(catalog.items()):
        for t in tables:
            for c in t.data_columns:
                if c.n_nonnull > 0:
                    by_key[c.key] = (t, c)

    if args.series:
        missing = [k for k in args.series if k not in by_key]
        if missing:
            print(f"unknown series: {missing}")
            return 1
        chosen = [by_key[k] for k in args.series]
    else:
        chosen = _default_pair(info, by_key)

    refusal = pair_refusal(len(chosen))
    if refusal:
        print(refusal)
        return 1

    print(f"study : {info.study_id}")
    for _t, c in chosen:
        print(f"series: {c.key}")

    if not available():
        print(f"matplotlib unavailable: {IMPORT_ERROR}")
        return 1

    res = sk.build_comparison(chosen, interval=args.interval,
                              aggregation=args.aggregation,
                              overlap=args.overlap, min_samples=1,
                              convert_units_flag=True, stratification=False)
    if res.data.empty:
        print("no rows survived; try --overlap union or a coarser interval")
        return 1
    print(f"rows  : {len(res.data)}  cols: {list(res.data.columns)}")

    root_tk = tk.Tk()
    root_tk.title("view gate")
    root_tk.geometry("300x120")
    root_tk.update()
    win = ViewWindow(root_tk, res, study=info)
    win.update()
    win.update_idletasks()

    if not args.check:
        if args.shot:
            win.figure.savefig(args.shot, dpi=110, facecolor="white")
            print(f"shot  : {args.shot}")
            return 0
        root_tk.mainloop()
        return 0

    checks = []

    viewable = bool(win.winfo_viewable())
    checks.append(("window is viewable, not merely constructed", viewable))

    mapped = bool(win.winfo_ismapped())
    checks.append(("window is mapped", mapped))

    expected = sk.zscore(res.data)
    drawn = win.plotted()
    n_lines = len(drawn)
    checks.append(("one line per series", n_lines == len(res.data.columns)))

    exact = True
    for c, y in drawn.items():
        want = expected[c].to_numpy()
        if len(y) != len(want) or not np.allclose(y, want, equal_nan=True,
                                                  rtol=0, atol=0):
            exact = False
            print(f"    MISMATCH on {c}")
    checks.append(("plotted y data == sk.zscore(frame), exactly", exact))

    colors = identity.series_colors(list(res.data.columns))
    want_colors = {c: colors[c].upper() for c in res.data.columns}
    checks.append((f"line colours match identity {list(want_colors.values())}",
                   win.plotted_colors() == want_colors))

    geom = getattr(res, "geometry", None) or {}
    legend_texts = [t.get_text()
                    for t in win.figure.axes[0].get_legend().get_texts()]
    has_geom = all(any(g and g in txt for txt in legend_texts)
                   for g in (geom.get(c, "") for c in res.data.columns)
                   if g)
    checks.append((f"legend carries depth/frame {legend_texts}", has_geom))

    # ---- marking ----------------------------------------------------------
    checks.append(("this window's local axis ascends, so marking is on",
                   win.descent is None and win.span is not None))

    # matplotlib snaps by doing arithmetic on snap_values, so a list raises
    # inside _set_extents -- and the CallbackRegistry swallows it, leaving the
    # rubber band silently not snapping. Asserted by exercising it.
    try:
        win.span._set_extents((win._xnum[10] + 0.001, win._xnum[20] + 0.001))
        snapped = tuple(float(v) for v in win.span.extents)
        rubber_ok = (abs(snapped[0] - win._xnum[10]) < 1e-9
                     and abs(snapped[1] - win._xnum[20]) < 1e-9)
    except Exception as exc:
        snapped, rubber_ok = repr(exc), False
    win.span.set_visible(False)
    checks.append((f"the drag rubber band snaps to samples too, so what is "
                   f"drawn is what would be stored [{snapped}]", rubber_ok))

    i0, i1 = 20, 40
    want0, want1 = res.data.index[i0], res.data.index[i1]

    # The pure conversion, asserted EXACTLY. Pixel rounding cannot blur this
    # one, so it is the place to demand identity rather than proximity.
    got = win.span_interval(win._xnum[i0], win._xnum[i1])
    checks.append((f"a span resolves to exactly the sampled instants "
                   f"[{i0}, {i1}]",
                   got is not None and got[0] == want0 and got[1] == want1
                   and (got[2], got[3]) == (i0, i1)))
    checks.append(("a right-to-left drag yields the same interval",
                   win.span_interval(win._xnum[i1], win._xnum[i0]) == got))
    checks.append(("the stored instants are tz-aware UTC, not wall time",
                   want0.tzinfo is not None
                   and want0.utcoffset() == dt.timedelta(0)))

    tiny = win.span_interval(win._xnum[i0], win._xnum[i0] + 1e-9)
    checks.append(("a drag shorter than one sample is refused, not stored",
                   tiny is None))

    # The readout must show the local rendering of the SAME instants that
    # would be stored -- a readout drifting from the record is how someone
    # marks one hour and saves another.
    win._on_span_select(win._xnum[i0], win._xnum[i1])
    text = win.span_text.get()
    checks.append((f"readout shows local time of the stored instants "
                   f"[{text[:64]}...]",
                   ann.local_text(want0) in text
                   and ann.local_text(want1) in text))
    checks.append(("release records the selection",
                   win.selection == (want0, want1)))

    win._on_span_move(win._xnum[i0], win._xnum[i0] + 1e-9)
    checks.append((f"a sub-sample drag says so while dragging "
                   f"[{win.span_text.get()[:52]}...]",
                   "nothing to mark" in win.span_text.get()))

    # Drive the REAL widget through synthetic mouse events, so the wiring is
    # exercised and not only the function behind it. Asserted to within one
    # sample: at ~1,000 points across ~1,100 px the samples are about a pixel
    # apart, and integer pixel coordinates cannot address them more finely
    # than that. Exactness is the check above; this one is that it is CONNECTED.
    from matplotlib.backend_bases import MouseButton, MouseEvent
    ax = win.figure.axes[0]
    win.selection = None

    def _at(name, xdata):
        px, py = ax.transData.transform((xdata, 0.0))
        return MouseEvent(name, win.canvas, int(px), int(py),
                          button=MouseButton.LEFT)

    for name, xd in [("button_press_event", win._xnum[i0]),
                     ("motion_notify_event", win._xnum[i1]),
                     ("button_release_event", win._xnum[i1])]:
        win.canvas.callbacks.process(name, _at(name, xd))

    step = res.data.index[1] - res.data.index[0]
    sel = win.selection
    near = (sel is not None
            and abs(sel[0] - want0) <= step and abs(sel[1] - want1) <= step)
    checks.append((f"a real mouse drag sets the selection, within one sample "
                   f"[{sel[0] if sel else None} .. {sel[1] if sel else None}]",
                   near))

    # ---- reopening a marked pair renders the marks --------------------------
    # Against a temp directory, not the study's: the store is a function of a
    # path, and a gate should not leave marks in someone's real study.
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="view-gate-"))
    win.destroy()
    try:
        store = ann.Store(tmp)
        refs = tuple(ann.SeriesRef(res.columns[c].key, c)
                     for c in res.data.columns)
        idx = res.data.index

        # two occurrences inside the window, one entirely outside it
        inside = [(idx[100], idx[140]), (idx[300], idx[360])]
        for a, b in inside:
            store.confirm(study_id=info.study_id, pair=refs,
                          name="internal tide", reason="gate fixture",
                          start_utc=a, end_utc=b)
        store.confirm(study_id=info.study_id, pair=refs, name="internal tide",
                      reason="", start_utc=idx[0] - dt.timedelta(days=30),
                      end_utc=idx[0] - dt.timedelta(days=29))
        # a set on a DIFFERENT pair, which must not appear
        store.confirm(study_id=info.study_id, reason="", name="other pair",
                      pair=(refs[0], ann.SeriesRef("x::y::z", "x.y.z")),
                      start_utc=idx[100], end_utc=idx[140])
        # and a file that cannot be trusted
        (tmp / "corrupt.json").write_text(json.dumps({
            "set_id": "corrupt", "name": "zone-less", "color": "6E7B8B",
            "created_utc": "2026-08-06T00:00:00+00:00",
            "study_id": info.study_id,
            "pair": [refs[0].to_json(), refs[1].to_json()],
            "intervals": [{"start_utc": "2026-07-20 21:00:00",
                           "end_utc": "2026-07-20T23:00:00+00:00"}],
        }, indent=2), encoding="utf-8")

        win = ViewWindow(root_tk, res, study=info, annotations_dir=tmp)
        win.update()
        win.update_idletasks()

        checks.append(("reopening the pair loads only ITS sets, not the one "
                       f"drawn on another pair [{[m.name for m in win.marks]}]",
                       [m.name for m in win.marks] == ["internal tide"]))

        # Read off the AXES, not off the window's own bookkeeping -- what is on
        # the chart is the claim being made. The SpanSelector keeps its own
        # rubber-band Rectangle on the same axes, so it is excluded by
        # IDENTITY rather than by type or colour, either of which would start
        # silently swallowing real bands the day one of them matched.
        rubber_band = getattr(win.span, "_selection_artist", None)
        spans = [p for p in win.figure.axes[0].patches if p is not rubber_band]
        checks.append(("the drag rubber-band is on the axes and was excluded",
                       rubber_band is not None
                       and any(p is rubber_band
                               for p in win.figure.axes[0].patches)))
        checks.append((f"one band is drawn per interval inside the window "
                       f"[{len(spans)} bands]", len(spans) == len(inside)))
        checks.append(("the derived band views agree with the axes exactly",
                       spans == win.mark_patches
                       and [b.patch for b in win.bands] == spans
                       and all(o.is_drawn for o in win.bands)))

        # The band must sit where the STORED instant says, not merely somewhere.
        # The length is part of the assertion: `all()` over an empty zip is
        # True, so without it this passes loudest when nothing was drawn at all.
        drawn_x = sorted((p.get_x(), p.get_x() + p.get_width()) for p in spans)
        want_x = sorted((mdates.date2num(win._to_axis(a)),
                         mdates.date2num(win._to_axis(b)))
                        for a, b in inside)
        placed = (len(drawn_x) == len(want_x)
                  and all(abs(g[0] - w[0]) < 1e-6 and abs(g[1] - w[1]) < 1e-6
                          for g, w in zip(drawn_x, want_x)))
        checks.append((f"each band spans exactly its stored interval "
                       f"[{len(drawn_x)} compared]", placed))

        checks.append((f"the interval outside the window is reported, not "
                       f"dropped silently [omitted={win.omitted}]",
                       win.omitted == 1))

        legend = [t.get_text()
                  for t in win.figure.axes[0].get_legend().get_texts()]
        checks.append((f"the legend names the set beside the series {legend}",
                       any("internal tide" in t for t in legend)
                       and len(legend) == len(res.data.columns) + 1))

        checks.append((f"the untrusted file is reported [{len(win.mark_problems)}"
                       f" rejected]", len(win.mark_problems) == 1
                       and "corrupt.json" in win.mark_problems[0]))
        msg = win.rejection_message()
        checks.append(("the rejection names the file, the field and the "
                       "problem, without a modal dialog to block a caller",
                       "corrupt.json" in msg and "start_utc" in msg
                       and "no timezone" in msg))
        checks.append((f"the note says what happened [{win.marks_note}]",
                       "REJECTED" in win.marks_note
                       and "NOT drawn" in win.marks_note))

        # ---- confirming a region writes it immediately ---------------------
        before = {p.name for p in tmp.glob("*.json")}
        win.selection = (idx[500], idx[560])
        win._selected_indices = (500, 560)
        saved = win.commit_mark("internal tide", "third occurrence")

        # The set already holds three: two inside this window and one outside
        # it. Appending makes four, of which three are drawable.
        checks.append((f"confirming appends to the existing set rather than "
                       f"starting a new one [{len(saved.intervals)} occurrences]",
                       len(saved.intervals) == 4))
        after = {p.name for p in tmp.glob("*.json")}
        checks.append((f"no new file was created for an existing name "
                       f"[{sorted(after - before)}]", after == before))

        on_disk = ann.Store(tmp).load_file(tmp / f"{saved.set_id}.json")
        checks.append(("the mark is on disk the moment it is confirmed, with "
                       "no save step",
                       any(i.start_utc == idx[500].to_pydatetime()
                           and i.end_utc == idx[560].to_pydatetime()
                           for i in on_disk.intervals)))

        spans_now = [p for p in win.figure.axes[0].patches
                     if p is not getattr(win.span, "_selection_artist", None)]
        checks.append((f"the new band is on the chart without reopening "
                       f"[{len(spans_now)} bands]", len(spans_now) == 3))

        cov = next(i.coverage for i in on_disk.intervals
                   if i.start_utc == idx[500].to_pydatetime())
        n_expected = 61                       # samples 500..560 inclusive
        checks.append((f"coverage is captured per series {cov}",
                       set(cov) == set(res.data.columns)
                       and all(e["n_intervals"] == n_expected
                               for e in cov.values())))

        empties = {c: int(res.data[c].iloc[500:561].isna().sum())
                   for c in res.data.columns}
        checks.append((f"the empty count matches the data under the mark "
                       f"{empties}",
                       all(cov[c]["n_empty"] == empties[c] for c in empties)))

        # QARTOD-3 counted from the study's frame, since load_series drops the
        # flag. Verified against an independent count over the same window.
        obs = info.load_observations()
        want_suspect = {}
        for c in res.data.columns:
            ci = res.columns[c]
            sel = obs[(obs["station"].astype(str) == str(ci.station))
                      & (obs["variable"].astype(str) == str(ci.variable))]
            flags = pd.to_numeric(sel["qc_flag"], errors="coerce")
            if not flags.notna().any():
                want_suspect[c] = None
            else:
                when = pd.to_datetime(sel["time_utc"], utc=True)
                want_suspect[c] = int(((flags == 3) & (when >= idx[500])
                                       & (when <= idx[560])).sum())
        checks.append((f"QARTOD-3 kept is counted from the study's frame, "
                       f"independently reproduced {want_suspect}",
                       {c: cov[c]["n_suspect_kept"] for c in cov}
                       == want_suspect))
        checks.append(("a series with no QARTOD flags records null, never 0 — "
                       "'not evaluated' must not read as 'clean'",
                       all(v is None or isinstance(v, int)
                           for v in want_suspect.values())
                       and all(cov[c]["n_suspect_kept"] is None
                               for c, v in want_suspect.items() if v is None)))

        lines = win.coverage_lines(cov)
        checks.append((f"the dialog would say so in words {lines}",
                       all(("not evaluated" in ln) ==
                           (want_suspect[c] is None)
                           for c, ln in zip(cov, lines))))

        # The dialog itself: constructed and VIEWABLE, not merely built. `ask()`
        # is what blocks, and it is deliberately not called here.
        dlg = MarkDialog(win, summary="gate", coverage_lines=lines,
                         known=win.marks)
        dlg.update()
        dlg.update_idletasks()
        checks.append(("the mark dialog is viewable, not merely constructed",
                       bool(dlg.winfo_viewable())))
        checks.append((f"it offers the names already on this pair "
                       f"[{dlg.name_box.cget('values')}]",
                       "internal tide" in dlg.name_box.cget("values")))
        dlg.name_var.set("internal tide")
        dlg._prefill_reason()
        checks.append(("picking an existing name adopts its reason rather "
                       "than losing it",
                       dlg.reason_box.get("1.0", "end").strip()
                       == "third occurrence"))
        dlg._cancel()
        checks.append(("cancelling yields nothing", dlg.result_value is None))

        # ---- clicking a band selects it ------------------------------------
        win.redraw_marks()
        target = win.bands[0]
        mid = target.patch.get_x() + target.patch.get_width() / 2

        checks.append((f"a band is found by the x it covers "
                       f"[{len(win.bands)} bands indexed]",
                       win.band_at(mid) is target))
        checks.append(("clicking a band selects that occurrence, not merely "
                       "some band",
                       win.select_band_at(mid) is target
                       and win.selected_band is target
                       and win.selection == (target.interval.start_utc,
                                             target.interval.end_utc)))
        checks.append((f"the readout names the selected mark "
                       f"[{win.span_text.get()}]",
                       target.markset.name in win.span_text.get()
                       and ann.local_text(target.interval.start_utc)
                       in win.span_text.get()))
        checks.append(("the selected band is styled apart from the others",
                       all(target.patch.get_linewidth() > b.patch.get_linewidth()
                           for b in win.bands if b is not target)))
        checks.append(("selecting resolves to the interval's samples, so a "
                       "later edit can recompute coverage",
                       win._selected_indices
                       == win._indices_for(target.interval)))

        # A gap between two bands, to click on nothing.
        gap = max(b.patch.get_x() + b.patch.get_width() for b in win.bands) \
            + 0.01
        checks.append(("clicking away from every band clears the selection",
                       win.select_band_at(gap) is None
                       and win.selected_band is None
                       and win.selection is None))
        checks.append((f"and the hint comes back [{win.span_text.get()[:38]}...]",
                       win.span_text.get() == SPAN_HINT))

        win.select_band(target)
        win.redraw_marks()
        checks.append(("a redraw drops the selection rather than leaving it "
                       "pointing at a patch removed from the axes",
                       win.selected_band is None
                       and all(b.patch in win.figure.axes[0].patches
                               for b in win.bands)))

        # Through the real event path, not the method behind it.
        target = win.bands[0]
        mid = target.patch.get_x() + target.patch.get_width() / 2
        px, py = win.figure.axes[0].transData.transform((mid, 0.0))
        for name in ("button_press_event", "button_release_event"):
            win.canvas.callbacks.process(
                name, MouseEvent(name, win.canvas, int(px), int(py),
                                 button=MouseButton.LEFT))
        checks.append((f"a real click selects, through the event path "
                       f"[{win.selected_band.markset.name if win.selected_band else None}]",
                       win.selected_band is not None))
        checks.append(("a click did not create a mark",
                       len(win.bands) == 3))

        # ---- adjusting the selected mark -----------------------------------
        entry = win.select_band(win.bands[0])
        was = (entry.interval.start_utc, entry.interval.end_utc)
        set_id = entry.markset.set_id
        neighbours = [(b.markset.set_id, b.interval.start_utc,
                       b.interval.end_utc) for b in win.bands[1:]]

        checks.append(("selecting puts the selector's handles on the mark, so "
                       "its edges are what a drag moves",
                       win.span.get_visible()
                       and win.span.extents
                       == (win._to_num(was[0]), win._to_num(was[1]))))

        # Move the start earlier by 10 samples and the end later by 5.
        i0, i1 = win._indices_for(entry.interval)
        new = (win._utc[i0 - 10], win._utc[i1 + 5])
        adjusted = win.adjust_selected(*new)

        checks.append((f"the adjusted interval is what the window now shows "
                       f"[{ann.local_text(new[0])} → {ann.local_text(new[1])}]",
                       win.selection == new
                       and win.selected_band is not None
                       and (win.selected_band.interval.start_utc,
                            win.selected_band.interval.end_utc) == new))
        on_disk = ann.Store(tmp).load_file(tmp / f"{set_id}.json")
        checks.append(("round trip after an edit returns what the window shows",
                       any((i.start_utc, i.end_utc) == new
                           for i in on_disk.intervals)
                       and not any((i.start_utc, i.end_utc) == was
                                   for i in on_disk.intervals)))
        checks.append((f"the occurrence count did not change "
                       f"[{len(on_disk.intervals)}]",
                       len(on_disk.intervals) == len(adjusted.intervals)))
        checks.append(("the other occurrences were not touched",
                       all(n in [(b.markset.set_id, b.interval.start_utc,
                                  b.interval.end_utc) for b in win.bands]
                           for n in neighbours)))

        edited = next(i for i in on_disk.intervals
                      if (i.start_utc, i.end_utc) == new)
        want_cov = win.coverage_for(*win._indices_for(edited))
        checks.append((f"coverage was RECOMPUTED for the new window, not "
                       f"carried over {edited.coverage}",
                       edited.coverage == want_cov
                       and edited.coverage != cov))

        band = win.selected_band.patch
        checks.append(("the band on the chart moved with it",
                       abs(band.get_x() - win._to_num(new[0])) < 1e-6
                       and abs(band.get_x() + band.get_width()
                               - win._to_num(new[1])) < 1e-6))

        # An edge dragged past the other one must change nothing.
        before_bytes = (tmp / f"{set_id}.json").read_bytes()
        win.span._active_handle = "min"          # as a real handle drag leaves it
        win._on_span_select(win._to_num(new[0]), win._to_num(new[0]))
        win.span._active_handle = None
        checks.append((f"an edge dragged past the other is refused and nothing "
                       f"is written [{win.span_text.get()[:46]}...]",
                       (tmp / f"{set_id}.json").read_bytes() == before_bytes
                       and "cannot cross" in win.span_text.get()))
        checks.append(("and the mark stays selected at its stored extent",
                       win.selection == new))

        # ---- deleting ------------------------------------------------------
        entry = win.select_band(win.bands[0])
        set_id, doomed = entry.markset.set_id, (entry.interval.start_utc,
                                                entry.interval.end_utc)
        n_before = len(entry.markset.intervals)
        survivors = [(b.markset.set_id, b.interval.start_utc,
                      b.interval.end_utc) for b in win.bands[1:]]

        msg = win.deletion_message()
        checks.append((f"the confirmation names the mark, its local times and "
                       f"its reason [{msg.splitlines()[2].strip()}]",
                       entry.markset.name in msg
                       and ann.local_text(doomed[0]) in msg
                       and entry.markset.reason in msg
                       and "cannot be undone" in msg))
        checks.append((f"and says how many occurrences survive "
                       f"[{msg.splitlines()[-2]}]",
                       f"{n_before - 1} are kept" in msg))
        checks.append(("the Delete button is live only while something is "
                       "selected",
                       str(win.delete_btn.cget("state")) == "normal"))

        left = win.delete_selected()
        checks.append((f"the occurrence is gone from the set "
                       f"[{n_before} -> {len(left.intervals)}]",
                       left is not None and len(left.intervals) == n_before - 1
                       and all((i.start_utc, i.end_utc) != doomed
                               for i in left.intervals)))
        checks.append(("round trip after a delete returns what the window shows",
                       [(i.start_utc, i.end_utc) for i in
                        ann.Store(tmp).load_file(tmp / f"{set_id}.json").intervals]
                       == [(i.start_utc, i.end_utc) for i in left.intervals]))
        checks.append(("its band is off the chart, and the others remain",
                       all(abs(b.patch.get_x() - win._to_num(doomed[0])) > 1e-9
                           for b in win.bands)
                       and all(s in [(b.markset.set_id, b.interval.start_utc,
                                      b.interval.end_utc) for b in win.bands]
                               for s in survivors)))
        checks.append(("nothing is selected afterwards, and the button is off",
                       win.selected_band is None
                       and str(win.delete_btn.cget("state")) == "disabled"))

        # Emptying a set needs one whose every occurrence is DRAWN. The set
        # above has an interval outside the window, which is unreachable by
        # clicking -- the gap slice 5 exists to close, and a reason this check
        # gets its own fixture rather than reusing that one.
        solo = ann.Store(tmp)
        solo.confirm(study_id=info.study_id, pair=refs, name="short lived",
                     reason="exists to be deleted",
                     start_utc=idx[800], end_utc=idx[830])
        solo.confirm(study_id=info.study_id, pair=refs, name="short lived",
                     reason="", start_utc=idx[850], end_utc=idx[880])
        win.redraw_marks()
        solo_id = next(b.markset.set_id for b in win.bands
                       if b.markset.name == "short lived")

        warned = False
        while True:
            here = [b for b in win.bands if b.markset.set_id == solo_id]
            if not here:
                break
            win.select_band(here[0])
            if len(here[0].markset.intervals) == 1:
                note = win.deletion_message()
                warned = "only occurrence" in note and "reason" in note
                checks.append((f"deleting the last occurrence warns that the "
                               f"set and its reason go too "
                               f"[{note.splitlines()[-2][:52]}...]", warned))
            if win.delete_selected() is None:
                break
        checks.append(("the warning was actually reached, not skipped", warned))
        checks.append(("deleting the last occurrence removes the file, leaving "
                       "no named set with nothing in it",
                       not (tmp / f"{solo_id}.json").exists()))
        _sets, _bad = ann.Store(tmp).load_all()
        checks.append((f"the store is still valid afterwards "
                       f"[{len(_sets)} set(s), {len(_bad)} rejected]",
                       len(_bad) == 1 and "corrupt.json" in _bad[0]))

        # A mark overlapping the window EDGE is drawn clamped, but its identity
        # in the store is the original. Selecting one and deleting it must find
        # it -- keeping only the clamped form made every such mark permanently
        # unadjustable and undeletable.
        straddle = ann.Store(tmp)
        straddle_start = idx[0] - dt.timedelta(days=2)
        straddle.confirm(study_id=info.study_id, pair=refs, name="straddler",
                         reason="starts before this window does",
                         start_utc=straddle_start, end_utc=idx[30])
        win.redraw_marks()
        edge = next(b for b in win.bands if b.markset.name == "straddler")
        checks.append((f"a mark overlapping the window edge keeps its ORIGINAL "
                       f"extent as identity, and is drawn clamped "
                       f"[{ann.local_text(edge.interval.start_utc)} stored, "
                       f"{ann.local_text(edge.drawn.start_utc)} drawn]",
                       edge.interval.start_utc == straddle_start
                       and edge.drawn.start_utc == win._utc[0]))
        win.select_band(edge)
        try:
            win.delete_selected()
            edge_ok, why = True, ""
        except Exception as exc:
            edge_ok, why = False, str(exc)[:70]
        checks.append((f"and it can actually be deleted [{why}]",
                       edge_ok and not any(b.markset.name == "straddler"
                                           for b in win.bands)))

        # A window whose axis was refused for marking must still be deletable.
        # #5 turns the selector off across a DST fall-back; only creating and
        # adjusting need a monotonic axis, and a window that cannot be marked
        # must not become one whose marks can never be removed.
        keep_span, win.span = win.span, None
        stuck = ann.Store(tmp)
        stuck.confirm(study_id=info.study_id, pair=refs, name="stranded",
                      reason="left on an unmarkable window",
                      start_utc=idx[700], end_utc=idx[740])
        win.redraw_marks()
        target = next(b for b in win.bands if b.markset.name == "stranded")
        picked = win.select_band(target)
        checks.append(("with the selector refused, a mark can still be "
                       "selected", picked is target
                       and win.selected_band is target))
        win.delete_selected()
        checks.append(("and deleted, so an unmarkable window is not one whose "
                       "marks are stuck there forever",
                       not any(b.markset.name == "stranded"
                               for b in win.bands)))
        win.span = keep_span

        # ---- the mark list reaches what the chart cannot --------------------
        far = ann.Store(tmp)
        far_start = idx[0] - dt.timedelta(days=40)
        far.confirm(study_id=info.study_id, pair=refs, name="long ago",
                    reason="before this window starts",
                    start_utc=far_start,
                    end_utc=far_start + dt.timedelta(hours=6))
        far.confirm(study_id=info.study_id, pair=refs, name="long ago",
                    reason="", start_utc=idx[900], end_utc=idx[930])
        win.redraw_marks()

        mine = [o for o in win.occurrences if o.markset.name == "long ago"]
        checks.append((f"every occurrence is indexed, drawn or not "
                       f"[{len(mine)} indexed, "
                       f"{sum(1 for o in mine if o.patch is None)} not drawn]",
                       len(mine) == 2
                       and sum(1 for o in mine if o.patch is None) == 1))

        offscreen = next(o for o in mine if o.patch is None)
        checks.append(("the one outside the window is drawn nowhere, so the "
                       "chart cannot reach it",
                       win.band_at(win._to_num(offscreen.interval.start_utc))
                       is not offscreen))

        rows = {win.mark_tree.item(r, "text").strip(): r
                for r in win._row_for}
        listed = ann.local_text(offscreen.interval.start_utc)
        checks.append((f"but the list shows it, flagged [{listed}]",
                       any(win._row_for[r] is offscreen for r in win._row_for)))
        row = next(r for r in win._row_for
                   if win._row_for[r] is offscreen)
        checks.append((f"and says it is outside "
                       f"[{win.mark_tree.item(row, 'values')}]",
                       win.mark_tree.item(row, "values")[0] == "outside"))

        win.mark_tree.selection_set(row)
        win._on_row_selected()
        checks.append(("picking that row selects the occurrence",
                       win.selected_band is offscreen))
        checks.append((f"the readout says why it has no handles "
                       f"[...{win.span_text.get()[-58:]}]",
                       "not drawn" in win.span_text.get()))

        try:
            win.adjust_selected(idx[10], idx[20])
            adjust_refused, why = False, "it was allowed"
        except RuntimeError as exc:
            adjust_refused, why = True, str(exc)[:60]
        checks.append((f"adjusting it is refused, saying what to do instead "
                       f"[{why}...]", adjust_refused))

        win.mark_tree.selection_set(row)
        win._on_row_selected()
        gone = win.delete_selected()
        still_there = any(o.markset.name == "long ago" and o.patch is None
                          for o in win.occurrences)
        checks.append(("BUT IT CAN BE DELETED from the list -- the gap this "
                       "list exists to close",
                       gone is not None and not still_there))

        # chart and list stay in step, in both directions
        drawn_one = next(o for o in win.occurrences if o.patch is not None)
        win.select_band(drawn_one)
        sel = win.mark_tree.selection()
        checks.append(("selecting on the chart highlights the matching row",
                       bool(sel) and win._row_for.get(sel[0]) is drawn_one))
        header = win.mark_tree.parent(sel[0])
        win.mark_tree.selection_set(header)
        win._on_row_selected()
        checks.append(("picking a set header selects nothing, rather than "
                       "guessing which occurrence was meant",
                       win.selected_band is None))

        lo, hi = win.figure.axes[0].get_xlim()
        first, last = win._xnum[0], win._xnum[-1]
        checks.append((f"the x axis still frames the data after a save and a "
                       f"redraw [{lo:.0f}..{hi:.0f} vs data {first:.0f}.."
                       f"{last:.0f}]",
                       lo > first - 5 and hi < last + 5))

        colors = {p.get_facecolor()[:3] for p in spans}
        series_rgb = {tuple(round(int(c[i:i + 2], 16) / 255, 6)
                            for i in (0, 2, 4))
                      for c in identity.SERIES_COLORS}
        rounded = {tuple(round(v, 6) for v in c) for c in colors}
        checks.append((f"no band is drawn in a series colour "
                       f"[{len(rounded)} band colour(s) checked]",
                       bool(rounded) and not (rounded & series_rgb)))

        # ---- borrowing a set drawn on another pair --------------------------
        # The case the whole feature exists for: a set drawn on this pair's
        # first series against a THIRD series, which is therefore a candidate
        # explanation that is not on screen.
        on_screen = {c.key for _t, c in chosen}
        third_key = next(k for k in sorted(by_key) if k not in on_screen)
        third = by_key[third_key][1]
        foreign_ref = ann.SeriesRef(third_key, third.label)
        home = (refs[0], foreign_ref)

        borrow = ann.Store(tmp)
        for a, b in [(idx[200], idx[260]), (idx[400], idx[470])]:
            borrow.confirm(study_id=info.study_id, pair=home,
                           name="third series", reason="candidate explanation",
                           start_utc=a, end_utc=b)
        borrow.confirm(study_id=info.study_id, pair=home, name="third series",
                       reason="", start_utc=idx[0] - dt.timedelta(days=20),
                       end_utc=idx[0] - dt.timedelta(days=19))
        # A set sharing NEITHER series with what is on screen.
        borrow.confirm(study_id=info.study_id, name="unrelated", reason="",
                       pair=(foreign_ref, ann.SeriesRef("p::q::r", "p.q.r")),
                       start_utc=idx[200], end_utc=idx[260])
        win.redraw_marks()
        pinned_x = win.figure.axes[0].get_xlim()   # must survive every overlay

        offered = {c.markset.name for c in win.candidates}
        native_names = {m.name for m in win.marks}
        checks.append((f"the sets sharing exactly one series are offered "
                       f"[{sorted(offered)}]",
                       offered == {"third series", "other pair"}))
        checks.append((f"a set on THIS pair is not offered as an overlay — it "
                       f"is already native here [{sorted(native_names)}]",
                       bool(native_names) and not (native_names & offered)))
        checks.append(("a set sharing neither series is not offered",
                       "unrelated" not in offered))

        wl = next(c for c in win.candidates
                  if c.markset.name == "third series")
        checks.append((f"the offer names the absent partner, which is what "
                       f"gets borrowed [{wl.foreign.label}]",
                       wl.foreign.key == third_key
                       and wl.shared.key == refs[0].key))

        def native_state():
            """What this pair's own bands are, and where they are drawn."""
            return [(b.markset.set_id, b.interval.start_utc, b.interval.end_utc,
                     round(b.patch.get_x(), 9), round(b.patch.get_width(), 9))
                    for b in win.bands if not b.is_foreign]

        native_before = native_state()
        cand = win.overlay(wl.markset.set_id)
        foreign_bands = [b for b in win.bands if b.is_foreign]
        native_bands = [b for b in win.bands if not b.is_foreign]
        checks.append((f"borrowing draws one band per occurrence inside the "
                       f"window [{len(foreign_bands)} borrowed]",
                       cand is not None and len(foreign_bands) == 2))
        checks.append((f"and leaves every native band exactly where it was "
                       f"[{len(native_before)} compared]",
                       bool(native_before)
                       and native_state() == native_before))

        # Read off the AXES. What is on the chart is the claim being made, and
        # the distinction this issue is HITL for is a property of the artists.
        fill_alpha = [b.patch.get_facecolor()[3] for b in foreign_bands]
        hatches = [b.patch.get_hatch() for b in foreign_bands]
        native_hatch = [b.patch.get_hatch() for b in native_bands]
        native_fill = [b.patch.get_facecolor()[3] for b in native_bands]
        checks.append((f"a borrowed band is UNFILLED and hatched where a "
                       f"native one is filled and plain [fill {fill_alpha} vs "
                       f"{native_fill}, hatch {hatches} vs {native_hatch}]",
                       bool(foreign_bands) and bool(native_bands)
                       and all(a == 0 for a in fill_alpha)
                       and all(h for h in hatches)
                       and all(a > 0 for a in native_fill)
                       and not any(native_hatch)))
        checks.append(("a borrowed band is drawn ABOVE the native ones, so a "
                       "coincidence between them still reads as both",
                       min(b.patch.get_zorder() for b in foreign_bands)
                       > max(b.patch.get_zorder() for b in native_bands)))

        legend = [t.get_text()
                  for t in win.figure.axes[0].get_legend().get_texts()]
        attributed = [t for t in legend if "borrowed from" in t]
        checks.append((f"the legend names the SOURCE PAIR of the borrowed set "
                       f"{attributed}",
                       len(attributed) == 1
                       and wl.markset.pair_text in attributed[0]
                       and "third series" in attributed[0]))

        checks.append((f"the interval outside the window is omitted and "
                       f"COUNTED against its own set "
                       f"[{win.overlay_omitted}]",
                       win.overlay_omitted[wl.markset.set_id] == 1))
        checks.append((f"and the window says so, naming the set and the pair "
                       f"[{win.overlay_note}]",
                       "third series" in win.overlay_note
                       and wl.markset.pair_text in win.overlay_note
                       and "NOT drawn" in win.overlay_note))
        checks.append(("the borrowed count is reported apart from the native "
                       "one, so neither is attributed to the wrong pair",
                       "borrowed" not in win.marks_note.lower()
                       and str(win.overlay_omitted[wl.markset.set_id])
                       in win.overlay_note))

        rows = [r for r, entry in win._row_for.items() if entry.is_foreign]
        parents = {win.mark_tree.item(win.mark_tree.parent(r), "values")[0]
                   for r in rows}
        checks.append((f"the mark list flags the borrowed rows [{parents}]",
                       len(rows) == 3 and parents == {"borrowed"}))

        # ---- a borrowed band is read-only -----------------------------------
        edge = foreign_bands[0]
        win.select_band(edge)
        checks.append(("a borrowed band can be selected, so clicking one says "
                       "where it came from",
                       win.selected_band is edge))
        checks.append((f"the readout says it is borrowed and names the pair "
                       f"[...{win.span_text.get()[-72:]}]",
                       "BORROWED" in win.span_text.get()
                       and wl.markset.pair_text in win.span_text.get()))
        checks.append(("no adjust handles are put on it, so there is no drag "
                       "to have to refuse",
                       not win.span.get_visible()))
        checks.append(("the Delete button is OFF for a borrowed mark",
                       str(win.delete_btn.cget("state")) == "disabled"))

        borrowed_bytes = (tmp / f"{wl.markset.set_id}.json").read_bytes()
        try:
            win.delete_selected()
            del_refused, del_why = False, "it was allowed"
        except RuntimeError as exc:
            del_refused, del_why = True, str(exc)
        checks.append((f"deleting it is refused, naming the set and the pair "
                       f"it belongs to [{del_why[:60]}...]",
                       del_refused and "third series" in del_why
                       and wl.markset.pair_text in del_why))
        try:
            win.adjust_selected(idx[210], idx[250])
            adj_refused, adj_why = False, "it was allowed"
        except RuntimeError as exc:
            adj_refused, adj_why = True, str(exc)
        checks.append((f"and so is adjusting it [{adj_why[:52]}...]",
                       adj_refused and wl.markset.pair_text in adj_why))
        checks.append(("neither attempt wrote anything to the borrowed set's "
                       "file",
                       (tmp / f"{wl.markset.set_id}.json").read_bytes()
                       == borrowed_bytes))

        # ---- the dropdown ----------------------------------------------------
        offers = list(win.overlay_box.cget("values"))
        checks.append((f"the dropdown offers exactly the eligible sets, each "
                       f"naming its source pair {offers}",
                       len(offers) == len(win.candidates) == 2
                       and all(c.markset.name in o and c.markset.pair_text in o
                               for c, o in zip(win.candidates, offers))))
        checks.append((f"and marks the one already on the chart "
                       f"[{[o[-9:] for o in offers if 'on chart' in o]}]",
                       sum("on chart" in o for o in offers) == 1))

        # Hand it back through the button, then borrow it again through the
        # button, so the control is exercised and not only the seam under it.
        win.select_band(next(b for b in win.bands if b.is_foreign))
        checks.append(("Remove overlay is live only while a BORROWED band is "
                       "selected",
                       str(win.remove_btn.cget("state")) == "normal"))
        win.prompt_remove_overlay()
        checks.append((f"pressing it stops the borrowing and says nothing was "
                       f"deleted [...{win.span_text.get()[-46:]}]",
                       not [b for b in win.bands if b.is_foreign]
                       and "Nothing was deleted" in win.span_text.get()
                       and (tmp / f"{wl.markset.set_id}.json").exists()))
        win.select_band(next(b for b in win.bands if not b.is_foreign))
        checks.append(("and it is off again for a native mark, which is "
                       "deleted rather than handed back",
                       str(win.remove_btn.cget("state")) == "disabled"
                       and str(win.delete_btn.cget("state")) == "normal"))

        win.overlay_box.current(win._offer_ids.index(wl.markset.set_id))
        checks.append(("the choice resolves to a SET ID, not to the label the "
                       "box happens to be showing",
                       win.overlay_choice() == wl.markset.set_id))
        win.prompt_overlay()
        checks.append((f"pressing Overlay draws its bands "
                       f"[{len([b for b in win.bands if b.is_foreign])} "
                       f"borrowed]",
                       [b.markset.set_id for b in win.bands if b.is_foreign]
                       == [wl.markset.set_id] * 2))

        bare = ViewWindow(root_tk, res, study=info,
                          annotations_dir=tmp / "no-sets-here")
        bare.update_idletasks()
        hint = str(bare.overlay_hint.cget("text"))
        checks.append((f"with nothing eligible the control is disabled and "
                       f"SAYS WHY [{hint}]",
                       str(bare.overlay_box.cget("state")) == "disabled"
                       and str(bare.overlay_btn.cget("state")) == "disabled"
                       and "exactly one series" in hint))
        bare.destroy()

        # ---- the ghost line --------------------------------------------------
        ghosts = win.ghosts
        ghost_lines = [g.line for g in ghosts]
        checks.append((f"the absent partner is fetched by its stable key and "
                       f"drawn [{[g.ref.label for g in ghosts]}]",
                       len(ghosts) == 1 and ghosts[0].ref.key == third_key
                       and all(ln in win.figure.axes[0].get_lines()
                               for ln in ghost_lines)))

        gt, gc = resolve_series(info, third_key, ROOT)
        checks.append((f"resolve_series finds it in THIS study, without a "
                       f"window [{gc.key}]", gc.key == third_key))

        ghost_res = sk.build_comparison(
            [(gt, gc)], interval=args.interval, aggregation=args.aggregation,
            overlap="union", min_samples=1, start=idx[0], end=idx[-1],
            convert_units_flag=True, stratification=False)
        want_z = sk.zscore(ghost_res.data)[ghost_res.data.columns[0]].to_numpy()
        got_z = ghosts[0].values
        checks.append((f"its y data is sk.zscore of that series, exactly — the "
                       f"same function the workbook standardises with "
                       f"[{len(got_z)} points]",
                       len(got_z) == len(want_z)
                       and np.allclose(got_z, want_z, equal_nan=True,
                                       rtol=0, atol=0)))
        drawn_y = ghost_lines[0].get_ydata()
        checks.append(("and that is what was handed to matplotlib, not merely "
                       "what was computed",
                       len(drawn_y) == len(want_z)
                       and np.allclose(drawn_y, want_z, equal_nan=True,
                                       rtol=0, atol=0)))
        checks.append((f"standardised over THIS window: mean {np.nanmean(got_z):+.1e}, "
                       f"sd {np.nanstd(got_z):.6f} — a series standardised over "
                       f"its own wider extent would not centre here",
                       abs(float(np.nanmean(got_z))) < 1e-9
                       and abs(float(np.nanstd(got_z)) - 1.0) < 1e-9))

        legend = [t.get_text()
                  for t in win.figure.axes[0].get_legend().get_texts()]
        context = [t for t in legend if "ghost" in t]
        checks.append((f"the legend labels it as CONTEXT, not as a compared "
                       f"series {context}",
                       len(context) == 1 and "not compared" in context[0]
                       and gc.geometry_label in context[0]))
        checks.append(("it is not one of the compared series: two lines are "
                       "still the pair, and the ghost is neither",
                       len(win.plotted()) == 2
                       and ghost_lines[0] not in win.series_lines.values()))
        checks.append((f"it is drawn faint and dashed, under the series lines "
                       f"[lw {ghost_lines[0].get_linewidth()}, "
                       f"alpha {ghost_lines[0].get_alpha()}]",
                       ghost_lines[0].get_linestyle() not in ("-", "solid")
                       and ghost_lines[0].get_alpha() < 1.0
                       and ghost_lines[0].get_zorder()
                       < min(ln.get_zorder()
                             for ln in win.series_lines.values())))
        ghost_rgb = ghost_lines[0].get_color().lstrip("#").upper()
        checks.append((f"and in no series colour [{ghost_rgb}]",
                       ghost_rgb not in
                       {c.upper() for c in identity.SERIES_COLORS}))

        # The frame. Only the X pin was ever load-bearing; y is recomputed so
        # that a line whose whole job is to show what a series did is never
        # silently cropped. Forced with a synthetic ghost, because real data
        # may happen to fit inside the pinned range and a check that passes
        # only when the situation does not arise asserts nothing.
        axis = win.figure.axes[0]
        with_ghost = axis.get_ylim()
        checks.append((f"the real ghost is inside the frame "
                       f"[{with_ghost[0]:.2f}..{with_ghost[1]:.2f}]",
                       float(np.nanmin(got_z)) >= with_ghost[0]
                       and float(np.nanmax(got_z)) <= with_ghost[1]))
        win.ghosts.append(Ghost(ref=wl.foreign, label="synthetic", x=None,
                                values=np.array([-9.0, 11.0])))
        win._apply_ylim()
        widened = axis.get_ylim()
        win.ghosts.pop()
        win._apply_ylim()
        checks.append((f"a ghost exceeding the pinned range WIDENS it rather "
                       f"than being cropped [{with_ghost[0]:.2f}.."
                       f"{with_ghost[1]:.2f} -> {widened[0]:.2f}.."
                       f"{widened[1]:.2f}]",
                       widened[0] <= -9.0 and widened[1] >= 11.0))
        checks.append(("and dropping it restores the frame exactly",
                       axis.get_ylim() == with_ghost))
        checks.append((f"the X pin is untouched throughout "
                       f"[{axis.get_xlim()[0]:.0f}..{axis.get_xlim()[1]:.0f}]",
                       axis.get_xlim() == pinned_x))

        # Two sets drawn against the SAME absent series are two readings of one
        # thing. One ghost, named for both.
        twin = ann.Store(tmp)
        twin.confirm(study_id=info.study_id, name="same partner", reason="",
                     pair=(refs[1], foreign_ref),
                     start_utc=idx[600], end_utc=idx[650])
        win.redraw_marks()
        twin_id = next(c.markset.set_id for c in win.candidates
                       if c.markset.name == "same partner")
        win.overlay(twin_id)
        checks.append((f"two borrowed sets sharing one absent series get ONE "
                       f"ghost line, named for both "
                       f"[{sorted(win.ghosts[0].borrowers) if win.ghosts else []}]",
                       len(win.ghosts) == 1
                       and sorted(win.ghosts[0].borrowers)
                       == ["same partner", "third series"]))
        win.remove_overlay(twin_id)
        checks.append(("removing one of them keeps the ghost the other still "
                       "needs", len(win.ghosts) == 1))
        n_lines_with = len(win.figure.axes[0].get_lines())
        win.remove_overlay(wl.markset.set_id)
        checks.append((f"removing the last borrower takes the ghost off the "
                       f"axes [{n_lines_with} -> "
                       f"{len(win.figure.axes[0].get_lines())} lines]",
                       not win.ghosts
                       and len(win.figure.axes[0].get_lines())
                       == n_lines_with - 1
                       and win.figure.axes[0].get_ylim() == win._series_ylim))
        win.overlay(wl.markset.set_id)

        # ---- native wins a tie ----------------------------------------------
        overlap = ann.Store(tmp)
        overlap.confirm(study_id=info.study_id, pair=refs, name="coincides",
                        reason="drawn over a borrowed band on purpose",
                        start_utc=idx[210], end_utc=idx[250])
        win.redraw_marks()
        checks.append(("borrowing survives a redraw rather than quietly "
                       "falling off the chart",
                       len([b for b in win.bands if b.is_foreign]) == 2))
        mine = next(b for b in win.bands if b.markset.name == "coincides")
        mid = mine.patch.get_x() + mine.patch.get_width() / 2
        hit = win.band_at(mid)
        checks.append((f"where a borrowed band overlaps a native one, the "
                       f"click resolves to the NATIVE mark — the one that can "
                       f"be edited from here [{hit.markset.name}]",
                       hit is mine and not hit.is_foreign))

        # ---- more than one set at a time -------------------------------------
        second = next(c for c in win.candidates
                      if c.markset.name == "other pair")
        win.overlay(second.markset.set_id)
        sets_drawn = {b.markset.name for b in win.bands if b.is_foreign}
        legend = [t.get_text()
                  for t in win.figure.axes[0].get_legend().get_texts()]
        checks.append((f"two sets can be borrowed at once [{sorted(sets_drawn)}]",
                       sets_drawn == {"third series", "other pair"}
                       and len(win.overlays) == 2))
        checks.append((f"and the legend attributes BOTH "
                       f"[{len([t for t in legend if 'borrowed from' in t])} "
                       f"attributed]",
                       len([t for t in legend if "borrowed from" in t]) == 2))

        # Everything the visual review is about is on the chart at exactly this
        # point: this pair's own solid bands, two borrowed sets outlined and
        # hatched, and a ghost line. Whether a borrowed band READS as borrowed
        # is a judgement about visual weight, and no assertion above can make
        # it -- so the gate can hand over the picture instead of a window
        # someone has to open.
        if args.shot:
            win.figure.savefig(args.shot, dpi=110, facecolor="white")
            print(f"shot  : {args.shot}")

        # ---- a key that no longer resolves ----------------------------------
        # "other pair" was drawn against x::y::z, which is in no study. Its
        # bands are still an attributed claim about windows and stay on the
        # chart; what must not happen is a chart with no ghost on it saying
        # nothing about why.
        checks.append((f"the unresolvable partner is REPORTED, not left as a "
                       f"silently absent line "
                       f"[{len(win.ghost_problems)} problem(s)]",
                       len(win.ghost_problems) == 1
                       and len(win.ghosts) == 1))
        checks.append((f"the report names the set, the key and the label "
                       f"[{win.ghost_problems[0][:96]}...]",
                       "other pair" in win.ghost_problems[0]
                       and "x::y::z" in win.ghost_problems[0]
                       and "MISSING" in win.ghost_problems[0]))
        gmsg = win.ghost_message()
        checks.append(("the dialog wording says the bands are drawn and the "
                       "ghost is not, and is readable without a modal box "
                       "standing in the way",
                       "bands" in gmsg and "x::y::z" in gmsg
                       and "no ghost line" in gmsg))
        checks.append((f"and the note under the chart says so too, in orange "
                       f"[{win._overlay_needs_attention()}]",
                       "MISSING" in win.overlay_note
                       and win._overlay_needs_attention()))

        try:
            resolve_series(info, "nowhere::in::this-study", ROOT)
            resolved, why = True, ""
        except GhostError as exc:
            resolved, why = False, str(exc)
        checks.append((f"resolve_series refuses an unknown key by name, "
                       f"without a window [{why[:70]}...]",
                       not resolved and "nowhere::in::this-study" in why
                       and info.study_id in why))

        win.remove_overlay(second.markset.set_id)
        checks.append(("removing one leaves the other drawn",
                       {b.markset.name for b in win.bands if b.is_foreign}
                       == {"third series"}))
        checks.append(("and removing something not borrowed reports it rather "
                       "than pretending",
                       win.remove_overlay(second.markset.set_id) is False))

        native_id = win.marks[0].set_id
        try:
            win.overlay(native_id)
            refused_native, why = False, "it was allowed"
        except RuntimeError as exc:
            refused_native, why = True, str(exc)
        checks.append((f"a set drawn on THIS pair cannot be borrowed onto it, "
                       f"and the refusal says why [{why[:56]}...]",
                       refused_native and "EXACTLY ONE" in why))

        # A borrowed set deleted elsewhere must stop being drawn, and say so,
        # rather than leaving a chart claiming a set it no longer has.
        vanishing = win.overlays[0].markset.set_id
        ann.Store(tmp).delete_set(vanishing)
        win.redraw_marks()
        checks.append((f"a borrowed set deleted from under the window is "
                       f"dropped and REPORTED [{win.overlay_note[:64]}...]",
                       not [b for b in win.bands if b.is_foreign]
                       and vanishing in win.overlay_note
                       and win.overlay_ids == []))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nview gate:")
    for what, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    passed = sum(1 for _w, ok in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")

    win.destroy()
    root_tk.destroy()
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
