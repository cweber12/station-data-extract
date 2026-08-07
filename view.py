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
from tkinter import ttk
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

    def __init__(self, parent, result, study=None):
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

        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16),
                  ncol=2, frameon=False, fontsize=9)
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
