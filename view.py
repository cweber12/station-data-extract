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
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo

import annotations as ann
import identity
import sensorkit as sk

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# ---------------------------------------------------------------------------
# Optional dependency. Import failure is recorded, never raised at import time,
# so compare.py can start and explain the problem instead of refusing to run.
# ---------------------------------------------------------------------------
try:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                   NavigationToolbar2Tk)
    from matplotlib.figure import Figure
    from matplotlib.widgets import SpanSelector
    IMPORT_ERROR: Exception | None = None
except Exception as exc:                       # pragma: no cover - environment
    matplotlib = mdates = None
    FigureCanvasTkAgg = NavigationToolbar2Tk = Figure = None
    SpanSelector = None
    IMPORT_ERROR = exc

SPAN_HINT = "Drag across the chart to define a region."

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
        canvas = FigureCanvasTkAgg(self.figure, master=frame)
        self.canvas = canvas
        canvas.draw()

        toolbar_holder = ttk.Frame(frame)
        toolbar_holder.pack(fill="x")
        self.toolbar = NavigationToolbar2Tk(canvas, toolbar_holder,
                                            pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side="left")

        canvas.get_tk_widget().pack(fill="both", expand=True)

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

        marks = ttk.Frame(frame)
        marks.pack(fill="x", pady=(2, 0))
        ttk.Label(marks, text=self.marks_note,
                  foreground="#B4531A" if self._marks_need_attention()
                  else "#777", wraplength=1100,
                  justify="left").pack(side="left")

        # NOTE the dialog is NOT raised here. A modal box opened during
        # construction blocks whoever is building the window -- including a
        # gate driving it with update() -- until somebody clicks, which is a
        # hang with no error, the same shape of bug the threading contract on
        # ProgressDialog exists to prevent. The caller raises it once the
        # window is up; see report_rejections().

    # ----------------------------------------------------------------- marks

    def _load_marks(self):
        """The sets already drawn on THIS pair, in THIS study."""
        self.store = None
        self.pair_refs = None
        self.marks = []
        self.mark_problems = []
        self.omitted = 0
        self.mark_patches = []
        self.mark_legend = []

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
        self.marks = ann.sets_for_pair(sets, [r.key for r in self.pair_refs],
                                       self.study_id)
        self.marks_note = ""            # filled in by _draw_marks

    def _draw_marks(self):
        """One band per interval, per set, clipped to this window."""
        self.mark_patches = []
        self.mark_legend = []
        self.omitted = 0
        if not self.marks:
            self.marks_note = self.marks_note or "No marks on this pair yet."
            self._append_rejection_note()
            return

        ax = self.figure.axes[0]
        lo, hi = self._utc[0], self._utc[-1]
        drawn = 0

        for ms in self.marks:
            kept, omitted = ann.clip_to_window(ms.intervals, lo, hi)
            self.omitted += omitted
            if not kept:
                continue
            for iv in kept:
                patch = ax.axvspan(
                    self._to_axis(iv.start_utc), self._to_axis(iv.end_utc),
                    facecolor="#" + ms.color, alpha=0.22,
                    edgecolor="#" + ms.color, linewidth=1.0, zorder=0)
                self.mark_patches.append(patch)
                drawn += 1
            # One legend entry per SET, not per band -- the whole point of a
            # set is that many occurrences are one phenomenon.
            self.mark_legend.append((patch, f"{ms.name}  ({len(kept)}×)"))

        n_sets = len({id(m) for m in self.marks})
        parts = [f"{drawn} mark(s) in {n_sets} set(s) on this pair."]
        if self.omitted:
            parts.append(
                f"{self.omitted} fall outside this window and are NOT drawn — "
                f"rebuild wider to see them.")
        self.marks_note = "  ".join(parts)
        self._append_rejection_note()

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
        ax.legend(handles, labels, loc="upper center",
                  bbox_to_anchor=(0.5, -0.16), ncol=2, frameon=False,
                  fontsize=9)

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
        self.descent = ann.first_descent(self._xnum)
        if self.descent is not None:
            when = ann.local_text(self._utc[self.descent])
            self.span = None
            self.span_text.set(
                f"Marking is off for this window: local time runs backwards "
                f"around {when}, where a DST fall-back makes one wall clock "
                f"hour cover two different hours of real time. Rebuild the "
                f"window to exclude it.")
            return

        ax = self.figure.axes[0]
        self.span = SpanSelector(
            ax, self._on_span_select, "horizontal",
            onmove_callback=self._on_span_move,
            useblit=False, button=1, minspan=0,
            snap_values=self._xnum or None,
            props=dict(facecolor="#808080", alpha=0.20),
        )

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
                f"  ·  {i1 - i0} × {self.result.interval}")

    def _on_span_move(self, x0: float, x1: float):
        """Live, while the mouse is still down. Shows the SNAPPED times."""
        got = self.span_interval(x0, x1)
        if got is None:
            self.span_text.set(
                f"Shorter than one {self.result.interval} interval — "
                f"nothing to mark.")
            return
        self.span_text.set("Marking:  " + self._span_summary(*got))

    def _on_span_select(self, x0: float, x1: float):
        """On release. Slice 4 records the region; storing it lands next."""
        got = self.span_interval(x0, x1)
        self.selection = None if got is None else (got[0], got[1])
        if got is None:
            self.span_text.set(
                f"That drag was shorter than one {self.result.interval} "
                f"interval, so there is nothing to mark. Drag further.")
            return
        self.span_text.set("Region:  " + self._span_summary(*got)
                           + "   (not stored yet)")

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

        # Plain floats, in the same order as `_utc`. A list rather than an
        # array so that `annotations.snap_span` stays pure-Python and can be
        # exercised with no numeric stack present at all.
        self._xnum = [float(v) for v in mdates.date2num(x)]

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

def _main(argv=None):
    import argparse
    import json
    import sys

    import numpy as np

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
        chosen = [by_key[k] for k in sorted(by_key)[:2]]

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
        checks.append(("the window's mark bookkeeping matches the axes exactly",
                       spans == win.mark_patches))

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

        colors = {p.get_facecolor()[:3] for p in spans}
        series_rgb = {tuple(round(int(c[i:i + 2], 16) / 255, 6)
                            for i in (0, 2, 4))
                      for c in identity.SERIES_COLORS}
        rounded = {tuple(round(v, 6) for v in c) for c in colors}
        checks.append((f"no band is drawn in a series colour "
                       f"[{len(rounded)} band colour(s) checked]",
                       bool(rounded) and not (rounded & series_rgb)))
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
