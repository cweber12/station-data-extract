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

SHOWING A SET MARKED ON ANOTHER PAIR
    A set marked on a DIFFERENT pair can be shown here when it shares exactly
    one series with what is on screen, because then its other member is a
    candidate explanation that is not currently plotted. Its regions are drawn
    as open brackets rather than as shaded blocks, the legend names the pair
    they were marked on, and the absent member is fetched by its stable key and
    drawn as a faint solid ghost line.

    The word "borrowed" was used for this throughout an earlier revision and is
    gone from everything a user reads: it named the mechanism rather than the
    thing, and someone opening the window for the first time could not act on
    it. Internal identifiers still say `foreign` and `overlay`; see
    docs/adr/0002 for why the code's vocabulary and the screen's differ.

    THE CORRELATING IS DONE BY EYE, DELIBERATELY. Nothing here computes a
    relationship between the borrowed series and the plotted ones. An earlier
    version of this idea ranked all-pairs correlations and offered the best;
    it was dropped because these series share tidal and diurnal harmonics, so
    raw-level correlation between any two of them produces confident nonsense.
    What this shows is where someone judged something interesting and what the
    series was doing there. The judgement stays with the person.

    A borrowed band is READ-ONLY here, and everything a borrowed set draws is
    attributed on the chart itself, not only in this window's state: a printed
    or exported copy has to carry the same warning the screen did.

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
    from matplotlib.patches import Patch
    from matplotlib.widgets import SpanSelector
    IMPORT_ERROR: Exception | None = None
except Exception as exc:                       # pragma: no cover - environment
    matplotlib = mdates = np = None
    FigureCanvasTkAgg = NavigationToolbar2Tk = Figure = None
    SpanSelector = Patch = None
    IMPORT_ERROR = exc

SPAN_HINT = ("Drag across the chart to define a region.  "
             "Click a mark to select it.")

# ---------------------------------------------------------------------------
# How a region is drawn. One place, because the fill, the layering and the
# ghost's grey are one decision: the ghost has to read against the region AND
# against the white outside it, so moving either without the other breaks it.
# ---------------------------------------------------------------------------
REGION_FILL = "#000000"
REGION_ALPHA = 1.00

# Selection GROWS THE BAR. It used to darken the block, which stops being
# possible the moment the block is fully opaque -- there is nothing past
# black, and a "darker" selected region would have had to be rendered
# LIGHTER, inverting the very cue it was supposed to give. Height of the bar
# works at any fill, adds no outline and moves nothing, so the one thing this
# treatment exists to avoid cannot creep back in through the selection.
REGION_BAR = 0.022
REGION_BAR_SELECTED = 0.075

# Stacking, low to high. The region is a BACKDROP: above the grid, which it is
# meant to cover, and below every line, which it may never obscure.
Z_GRID = 0.2
Z_REGION = 0.5
Z_REGION_BAR = 0.7
Z_GHOST = 1.5
Z_SERIES = 2.0

# The ghost's grey answers to REGION_ALPHA and must be re-chosen with it. At a
# 40% fill the region rendered about #999999 and NO grey worked: light vanished
# inside the region, dark vanished against the white outside it. An opaque
# block is what makes a light grey possible -- it reads clearly against black,
# and stays muted against the saturated series lines everywhere else.
GHOST_GREY = "#B0B0B0"

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
    # The colour bar, and the edge rules on a region from another pair. Kept
    # beside the patch rather than looked up later: `patch` stays the ONE
    # artist carrying the geometry, so hit testing, selection and removal each
    # have a single thing to ask, and these ride along with it.
    decorations: tuple = ()

    @property
    def is_drawn(self) -> bool:
        return self.patch is not None

    @property
    def artists(self) -> list:
        """Everything this occurrence put on the axes."""
        return ([] if self.patch is None else [self.patch]) \
            + list(self.decorations)

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


def _stock_icon_stats(master=None):
    """Canvas size, palette and ink weight of matplotlib's own toolbar icons.

    Gate support, so "cohesive with the other icons" is a measurement rather
    than an opinion. Read through `tk.PhotoImage`, which handles PNG in Tk 8.6
    and is the same reader the button itself uses -- no Pillow, and no second
    notion of what a pixel is.

    `cbook._get_data_path` is private. Fine HERE: a gate that breaks when
    matplotlib moves its icons is a gate reporting something true.
    """
    from matplotlib import cbook
    import pathlib
    folder = pathlib.Path(str(cbook._get_data_path("images")))
    sizes, colours, weights = set(), set(), []
    for name in ("home.png", "move.png", "zoom_to_rect.png", "filesave.png"):
        img = tk.PhotoImage(master=master, file=str(folder / name))
        sizes.add((img.width(), img.height()))
        n = 0
        for x in range(img.width()):
            for y in range(img.height()):
                if not img.transparency_get(x, y):
                    colours.add(img.get(x, y))
                    n += 1
        weights.append(n)
    return {"size": sizes.pop() if len(sizes) == 1 else None,
            "colours": colours, "low": min(weights), "high": max(weights)}


def _region_icon(master):
    """The standard "select area" glyph: a frame open at one corner, and a
    plus sitting in the opening.

    TRANSCRIBED FROM AN SVG, exactly. The source is the public-domain
    select-area icon from SVG Repo, whose `viewBox` is `0 0 24 24` -- the same
    canvas matplotlib's toolbar icons use, so every coordinate in its path
    maps 1:1 onto a pixel here and nothing is approximated. The original path,
    for anyone checking this against it:

        M13,8 L13,6 L19,6 C20.1,6 21,6.9 21,8 L21,19 C21,20.1 20.1,21 19,21
        L8,21 C6.9,21 6,20.1 6,19 L6,13 L8,13 L8,19 L19,19 L19,8 L13,8 Z
        M6,6 L6,3 L8,3 L8,6 L11,6 L11,8 L8,8 L8,11 L6,11 L6,8 L3,8 L3,6 L6,6 Z

    The file itself is not used, because THIS TK CANNOT READ ONE: SVG support
    arrived in Tk 8.7 and the interpreter here is 8.6.15, so `PhotoImage`
    refuses it with "couldn't recognize data in image file". Rasterising at
    build time would mean a new dependency and a committed binary, and a
    binary is the one file in this repo whose content cannot be read in a
    diff. Transcribed, the shape is reviewable and correctable in place.

    A fresh PhotoImage is fully transparent, so only the inked pixels are set
    and the button's own background shows through in any theme. The caller
    must keep the returned image alive: Tk drops an image the moment its last
    Python reference goes, and the button then renders empty.
    """
    ink = "#000000"
    size = 24
    img = tk.PhotoImage(master=master, width=size, height=size)

    def fill(xa, xb, ya, yb):
        """The SVG's coordinates are edges; a span [a,b) is b-a pixels."""
        for x in range(xa, xb):
            for y in range(ya, yb):
                img.put(ink, to=(x, y))

    # The frame, two units thick, open at the top left. Four segments, read
    # straight off the path above.
    fill(13, 19, 6, 8)        # top,    x 13->19
    fill(19, 21, 6, 21)       # right,  y 6->21
    fill(6, 21, 19, 21)       # bottom, x 6->21
    fill(6, 8, 13, 21)        # left,   y 13->21

    # The three rounded corners. The source rounds them with a radius-2 arc;
    # at 24 px that is one pixel off each outer corner, and the fourth corner
    # is not rounded because it is not there -- that opening is the point of
    # the glyph.
    for corner in ((20, 6), (20, 20), (6, 20)):
        img.transparency_set(*corner, True)

    # The plus, in the opening. Centred on the corner the frame gives up.
    fill(6, 8, 3, 11)         # vertical arm
    fill(3, 11, 6, 8)         # horizontal arm
    return img


def _add_tooltip(widget, text):
    """A hover label, because an unlabelled icon says nothing on its own.

    matplotlib gives every one of its own toolbar buttons a tooltip, so a
    neighbour without one is the odd button out. Written here rather than
    imported from `matplotlib.backends._backend_tk`, which is private.
    """
    state = {"tip": None}

    def show(_event=None):
        if state["tip"] is not None:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tk.Label(tip, text=text, justify="left", relief="solid", borderwidth=1,
                 background="#FFFFE0", font=("Segoe UI", 8)).pack()
        tip.wm_geometry(f"+{widget.winfo_rootx()}"
                        f"+{widget.winfo_rooty() + widget.winfo_height() + 2}")
        state["tip"] = tip

    def hide(_event=None):
        if state["tip"] is not None:
            state["tip"].destroy()
            state["tip"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<ButtonPress>", hide)


class NameCheckList(ttk.Frame):
    """A scrolling column of checkboxes, one per name.

    Its own class because the scrolling is machinery and none of it is the
    window's business: Tk has no scrollable frame, so it is a Canvas with a
    Frame drawn onto it and the two kept in step by hand. Behind `set_items`
    the window never touches a scrollregion.

    The list is REBUILT wholesale on every refresh rather than diffed. The
    number of rows is the number of named sets in a study that share one
    series with this pair -- a handful -- and a diff would be more code than
    the thing it optimises, with a stale-row failure mode that a rebuild
    cannot have.
    """

    ROW_HEIGHT = 34

    def __init__(self, parent, on_toggle, visible_rows=4):
        super().__init__(parent)
        self._on_toggle = on_toggle
        self._vars = {}
        self._boxes = {}
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                 height=self.ROW_HEIGHT * visible_rows)
        self._bar = ttk.Scrollbar(self, orient="vertical",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._bar.set)
        self._inner = ttk.Frame(self._canvas)
        self._window = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", self._resize_scrollregion)
        self._canvas.bind("<Configure>", self._match_inner_width)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._bar.pack(side="right", fill="y")

    def _resize_scrollregion(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _match_inner_width(self, event):
        # Without this the inner frame keeps its REQUESTED width, so a row
        # narrower than the column would not fill it and a row wider would be
        # clipped rather than scrolled to.
        self._canvas.itemconfigure(self._window, width=event.width)

    def set_items(self, items):
        """Rebuild the rows. `items` is [(key, label, checked, detail)]."""
        for child in self._inner.winfo_children():
            child.destroy()
        self._vars, self._boxes = {}, {}
        for key, label, checked, detail in items:
            var = tk.BooleanVar(value=checked)
            self._vars[key] = var
            box = ttk.Checkbutton(self._inner, text=label, variable=var,
                                  command=lambda k=key: self._toggled(k))
            box.pack(anchor="w", fill="x")
            self._boxes[key] = box
            if detail:
                ttk.Label(self._inner, text=detail, foreground="#777",
                          font=("Segoe UI", 8), justify="left").pack(
                              anchor="w", padx=(20, 0))
        self._resize_scrollregion()

    def _toggled(self, key):
        self._on_toggle(key, bool(self._vars[key].get()))

    def keys(self) -> list:
        """The rows on offer, in order."""
        return list(self._vars)

    def checked(self) -> set:
        """Which rows are ticked. The seam a gate reads instead of pixels."""
        return {k for k, v in self._vars.items() if v.get()}

    def invoke(self, key):
        """Tick or untick a row the way a click does, callback and all.

        The gate drives THIS rather than calling the window's methods, for
        the reason #19 was opened: a check that bypasses the widget cannot
        see a widget that is wired up wrongly.
        """
        self._boxes[key].invoke()
        return bool(self._vars[key].get())


_MODE_TOOLBAR = None


def _mode_toolbar_class():
    """The stock toolbar, but it SAYS when Zoom or Pan is engaged.

    Subclassed lazily because `NavigationToolbar2Tk` is None when matplotlib
    failed to import, and this module has to import anyway so compare.py can
    explain why rather than refuse to start.

    Only the PUBLIC `zoom()` and `pan()` are overridden. They are the entry
    points every toolbar button goes through, and they are the two the window
    needs to hear about. Reaching into `mode` or the old `_active` instead
    would be reaching for internals that matplotlib has already renamed once.
    """
    global _MODE_TOOLBAR
    if _MODE_TOOLBAR is None:
        class _ModeToolbar(NavigationToolbar2Tk):
            def __init__(self, canvas, master, on_mode_change=None, **kw):
                # Set BEFORE super().__init__: it builds the buttons, and a
                # notification arriving during construction must not find the
                # attribute missing.
                self._on_mode_change = on_mode_change
                super().__init__(canvas, master, **kw)

            def zoom(self, *args):
                super().zoom(*args)
                self._notify_mode()

            def pan(self, *args):
                super().pan(*args)
                self._notify_mode()

            def _notify_mode(self):
                if self._on_mode_change is not None:
                    self._on_mode_change()

        _MODE_TOOLBAR = _ModeToolbar
    return _MODE_TOOLBAR


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
        self._ghost_ylim = None

        self.study = study
        self.study_id = getattr(study, "study_id", None)
        if annotations_dir is None and study is not None:
            annotations_dir = study.annotations_dir
        self.annotations_dir = annotations_dir

        self.title(self._title_text(study))
        # Open filling the screen. Everything this window has to SAY sits below
        # the chart -- the omitted-region counts, the missing-ghost error, the
        # control for showing another pair's regions -- and a fixed 1180x680
        # was smaller than its own content, so those were not merely cramped,
        # they were never mapped. A window whose controls are off the bottom is
        # the same failure as the dialog that was populated but withdrawn.
        self.geometry(f"{max(900, self.winfo_screenwidth() - 60)}x"
                      f"{max(560, self.winfo_screenheight() - 120)}+20+20")
        try:
            self.state("zoomed")            # a real maximise where there is one
        except tk.TclError:                 # pragma: no cover - platform
            pass
        self.minsize(900, 620)
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

        # PACKING ORDER IS LOAD-BEARING, and this is the whole of a bug that
        # shipped twice. Tk gives space to the slaves packed FIRST. The chart
        # expands, so packed first it takes everything and every fixed-height
        # row below it is allocated nothing -- not squeezed, NOT MAPPED. At
        # 1180x680 the content needed 754 px and the overlay controls, both
        # status lines and the buttons were simply absent from the window, so
        # "the omitted count is reported in the window" was true only for
        # someone who thought to drag it taller.
        #
        # So the furniture is packed against the bottom FIRST, in reverse
        # visual order, and the chart is packed last and takes what is left.
        # It cannot push anything off, because there is nothing left to push.
        body = ttk.Frame(frame)
        left = ttk.Frame(body)

        canvas = FigureCanvasTkAgg(self.figure, master=left)
        self.canvas = canvas
        canvas.draw()

        toolbar_holder = ttk.Frame(left)
        toolbar_holder.pack(fill="x")
        self.toolbar = _mode_toolbar_class()(
            canvas, toolbar_holder, on_mode_change=self._on_toolbar_mode,
            pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side="left")

        # Region sits FLUSH beside Zoom and Pan because it is a third member of
        # that set, not a separate control that happens to be nearby.
        #
        # Packed INSIDE the toolbar frame, after its stock buttons. Not a
        # `toolitems` entry: matplotlib resolves toolitem icons through the
        # private `cbook._get_data_path` and renders only `zoom` and `pan` as
        # pressed toggles, so adding one that way means copying the whole of
        # `__init__` and inheriting its churn.
        #
        # Inside rather than beside, because beside CANNOT WORK and the gate
        # said so: `NavigationToolbar2Tk.__init__` sets the frame's width to
        # the figure's and calls `pack_propagate(False)`, so the toolbar
        # demands the full 1150 px of the row whatever its buttons need.
        # Packed after it, this button was allocated 1 px and never mapped --
        # the regions-list bug again, sideways. The same fixed width is what
        # makes inside work: the stock buttons use about half of it.
        #
        # A Checkbutton styled Toolbutton, so "pressed" is what the widget
        # already means rather than something painted on. The mode has to be
        # VISIBLE -- marking being an invisible gesture is the bug.
        #
        # An ICON, matching its neighbours: a text button in a row of glyphs
        # reads as something bolted on rather than as a third mode. The image
        # is held on the instance because Tk drops an image as soon as its
        # last Python reference goes, and the button then renders empty.
        self._region_mode = True
        self._region_var = tk.BooleanVar(value=True)
        self.region_icon = _region_icon(self.toolbar)
        self.region_btn = ttk.Checkbutton(
            self.toolbar, image=self.region_icon, style="Toolbutton",
            variable=self._region_var, command=self._on_region_button)
        _add_tooltip(self.region_btn,
                     "Region: drag on the chart to mark a span of time")
        # Immediately after Zoom, so the three MODES are one group and the
        # actions that follow are another. Appended after Save otherwise --
        # `_buttons` is matplotlib's, and the worst a rename can do here is
        # put the button 70 px further along a row it is still in. The gate
        # asserts the adjacency, so the fallback cannot pass unnoticed.
        after_zoom = getattr(self.toolbar, "_buttons", {}).get("Subplots")
        where = {"before": after_zoom} if after_zoom is not None else {}
        self.region_btn.pack(side="left", padx=(8, 0), pady=2, **where)

        canvas.get_tk_widget().pack(fill="both", expand=True)

        # The list claims its width, then the chart takes the rest. The same
        # rule as the rows below, and the same bug it prevents: the figure asks
        # for 1150 px and expands, so packed first it took the lot and the list
        # was allocated 10 px and never mapped. A window had to be dragged
        # wider than this screen before the list appeared at all.
        self._build_side_panel(body)
        left.pack(side="left", fill="both", expand=True)

        # The readout sits directly under the chart because it is read WHILE
        # dragging, not after. Its whole purpose is that the hour being aimed
        # at is legible -- placing a band by eye against an axis labelled
        # `mm-dd` is the thing this feature replaces.
        self.span_text = tk.StringVar(value=SPAN_HINT)
        readout = ttk.Frame(frame)
        ttk.Label(readout, textvariable=self.span_text,
                  font=("Segoe UI", 10)).pack(side="left")

        self._install_span_selector()

        note = ttk.Frame(frame)
        ttk.Label(note, text=self._coverage_note(),
                  foreground="#777").pack(side="left")
        ttk.Button(note, text="Close", command=self.destroy).pack(side="right")
        self.delete_btn = ttk.Button(note, text="Delete region",
                                     command=self.prompt_delete,
                                     state="disabled")
        self.delete_btn.pack(side="right", padx=(0, 6))
        # Delete key as well as the button. There is no text entry in this
        # window, so the key cannot be stolen from something being typed into.
        self.bind("<Delete>", lambda _e: self.prompt_delete())

        marks = ttk.Frame(frame)
        self.marks_label = ttk.Label(
            marks, text=self.marks_note, wraplength=1100, justify="left",
            foreground="#B4531A" if self._marks_need_attention() else "#777")
        self.marks_label.pack(side="left")

        # What is borrowed, and what of it is NOT drawn. Its own line rather
        # than appended to the marks note: the counts belong to different
        # pairs, and one sentence carrying both would attribute neither.
        borrowed = ttk.Frame(frame)
        self.overlay_label = ttk.Label(
            borrowed, text=self.overlay_note, wraplength=1100, justify="left",
            foreground="#B4531A" if self._overlay_needs_attention() else "#777")
        self.overlay_label.pack(side="left")

        # The rest bottom-up, then the chart. See the note above `body`.
        #
        # The borrowing control USED TO BE A ROW HERE, packed above the chart
        # to keep it off the bottom edge. It now lives in the side panel with
        # the regions list, because both answer "what is on this chart" and
        # because a checklist needs height that a single row cannot give it.
        # The rule that put it above the chart still holds where it applies:
        # the panel is packed before the chart for the same reason.
        borrowed.pack(side="bottom", fill="x", pady=(2, 0))
        marks.pack(side="bottom", fill="x", pady=(2, 0))
        note.pack(side="bottom", fill="x", pady=(8, 0))
        readout.pack(side="bottom", fill="x", pady=(6, 0))
        body.pack(side="top", fill="both", expand=True)

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

    def _build_side_panel(self, parent):
        """The right-hand column: what CAN be shown, then what IS here.

        One column, because both halves answer the same question -- what is
        on this chart -- and in that order, because the control that brings
        regions in belongs above the list of the regions it brings.

        Packed before the chart by the caller. That is load-bearing; see the
        note at the call site.
        """
        right = ttk.Frame(parent, padding=(10, 0, 0, 0))
        right.pack(side="right", fill="y")
        self._build_overlay_controls(right)
        self._build_mark_list(right)

    def _build_overlay_controls(self, parent):
        """Tick a name to show every region saved under it on another pair.

        A CHECKLIST, not a dropdown and two buttons. The old control offered
        one set per line and acted on the CHART SELECTION rather than on the
        line, so "Stop showing" was dead until you had clicked a borrowed
        region -- and after zoom became a mode, dead until you had left the
        mode too. A checkbox has no such gap: the thing you tick is the thing
        that changes, and unticking it is how it comes off.
        """
        ttk.Label(parent, text="Regions from other pairs",
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.overlay_list = NameCheckList(parent, self._on_overlay_toggled)
        self.overlay_list.pack(fill="x")
        self.overlay_hint = ttk.Label(parent, foreground="#777", text="",
                                      wraplength=330, justify="left")
        self.overlay_hint.pack(anchor="w", pady=(2, 10))

    def _on_overlay_toggled(self, name: str, checked: bool):
        """A name was ticked or unticked. UI only; the rules are show/hide."""
        # The ghost is fetched from the study's parquet on this thread. It is
        # one series over one window and the frame is cached after the first
        # read, but the first one is not instant, and a window that stops
        # repainting with no explanation reads as a hang.
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            if checked:
                group = self.show_name(name)
                self.span_text.set(
                    f"Showing “{group.name}”, marked on "
                    f"{' and '.join(group.pair_texts)}. Its colour bar is "
                    f"along the BOTTOM of each region, not the top.")
            else:
                self.hide_name(name)
                self.span_text.set(
                    f"Stopped showing “{name}”. Nothing was deleted — the "
                    f"regions are still saved on their own pair.")
        except Exception as exc:
            messagebox.showerror("Could not show that set", str(exc),
                                 parent=self)
            self._refresh_overlay_controls()   # put the tick back
            return
        finally:
            self.configure(cursor="")
        # A key that no longer resolves gets a dialog, not only the note under
        # the chart. The regions are drawn and attributed; what is missing is
        # the line showing what the borrowed pair's other series was doing,
        # and that absence is exactly what nobody would notice.
        if checked:
            self.report_ghost_problems()

    def _build_mark_list(self, parent):
        """Every occurrence on this pair, including the ones not drawn."""
        right = parent

        # "in this view", not "on this pair": once a set can be borrowed from
        # another pair, the older heading would be a small lie about half the
        # rows under it.
        ttk.Label(right, text="Regions of interest",
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")

        holder = ttk.Frame(right)
        holder.pack(fill="both", expand=True)
        self.mark_tree = ttk.Treeview(holder, columns=("where",),
                                      show="tree headings", height=16,
                                      selectmode="browse")
        self.mark_tree.heading("#0", text="Set / region")
        self.mark_tree.heading("where", text="Status")
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
        self.refresh_mark_list()

    def refresh_mark_list(self):
        """Rebuild the rows from `occurrences`. Sets are parents."""
        tree = getattr(self, "mark_tree", None)
        if tree is None:
            return
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
                               values=("other pair" if foreign else "",),
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
        self._sync_row_selection()

    def _on_row_selected(self, _event=None):
        """A row picked in the list selects that occurrence.

        Selected `from_list`, because the tree is ALREADY showing this row.
        Sending the selection back would be a second write to the tree, and
        every write to the tree is another queued event to answer.
        """
        rows = self.mark_tree.selection()
        entry = self._row_for.get(rows[0]) if rows else None
        # A set header is not an occurrence; clicking one selects nothing
        # rather than guessing which of its occurrences was meant.
        self.select_band(entry, from_list=True)

    def _sync_row_selection(self):
        """Point the list at whatever is selected. Writes only if it must.

        Comparing before writing is the ONLY thing that ends the loop here.
        Tk *queues* <<TreeviewSelect>> rather than dispatching it, so a flag
        set around `selection_set` and cleared in a `finally` is always back
        to False by the time the handler reads it -- it never suppressed a
        single event. And `selection_set` fires the event even when it names
        the row that is already selected, so a sync that always writes always
        calls itself back, with nothing to stop it.
        """
        tree = getattr(self, "mark_tree", None)
        if tree is None:
            return
        wanted = next((row for row, entry in self._row_for.items()
                       if entry is self.selected_band), None)
        current = tuple(tree.selection())
        if wanted is None:
            if current:
                tree.selection_remove(*current)
            return
        if current != (wanted,):
            tree.selection_set(wanted)
        tree.see(wanted)

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
        """(patch, decorations) for one region. A solid block, and a bar.

        Every region is the same shape: a dark block with no border at all,
        and the set's colour in a bar along one edge. The bar sits at the TOP
        for this pair's own regions and at the BOTTOM for a region marked on
        another pair. Position is now the whole of the distinction.

        NO EDGES, and that is the point of this treatment rather than an
        omission from it. Edge rules in the set's colour were added when the
        fill was 15% grey and a narrow region was genuinely invisible without
        them. They solved that and caused a worse thing: two saturated
        vertical lines per region, crossing the series lines at every region
        boundary, which is exactly the visual noise a marked window is
        supposed to cut through. A block dark enough to see does not need an
        outline to be found, so the outline goes.

        The fill is neutral -- black, not the set's colour. A coloured wash
        would put a ninth and tenth hue on a chart that already carries one
        per series, and the colour that identifies the set is carried by the
        bar, which is what the legend swatch matches.

        The block sits ABOVE the gridlines and BELOW the data. Gridlines
        crossing a dark region are the same noise the edge rules were, and
        the region is a backdrop: what the series were doing inside it is the
        entire question, so nothing may be drawn over them.

        `patch` always spans the region, so hit testing has one artist to ask
        whatever the treatment.
        """
        x0, x1 = (self._to_axis(clamped.start_utc),
                  self._to_axis(clamped.end_utc))
        patch = ax.axvspan(x0, x1, facecolor=REGION_FILL,
                           alpha=REGION_ALPHA, edgecolor="none",
                           linewidth=0, zorder=Z_REGION)
        lo, hi = self._bar_extent(foreign, chosen=False)
        bar = ax.axvspan(x0, x1, ymin=lo, ymax=hi, facecolor="#" + color,
                         linewidth=0, zorder=Z_REGION_BAR)
        return patch, (bar,)

    @staticmethod
    def _bar_extent(foreign: bool, chosen: bool) -> tuple:
        """(ymin, ymax) in axes coordinates for a region's colour bar.

        One function, so the bar drawn at creation and the bar resized on
        selection cannot drift apart -- they did not, but they were two
        literals in two places, which is how they would have.

        Grows INWARD from whichever edge it is anchored to, so the edge it
        names stays the edge it names: a bottom bar that grew downward would
        leave the plot area, and a top bar that grew upward would too.
        """
        thick = REGION_BAR_SELECTED if chosen else REGION_BAR
        return (0.0, thick) if foreign else (1.0 - thick, 1.0)

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
                patch, extra = self._band_patch(ax, clamped, ms.color,
                                                foreign=False)
                self.occurrences.append(
                    Band(patch, ms, original, clamped, decorations=extra))
                drawn += 1
            # One legend entry per SET, not per band -- the whole point of a
            # set is that many occurrences are one phenomenon.
            #
            # The swatch is a PROXY, not a band off the axes: the band's own
            # fill is a neutral shade so that the data underneath survives, and
            # a swatch of that would identify nothing. The proxy carries the
            # set's colour, filled, matching the bar along the top of its
            # bands. See _draw_borrowed for the open swatch that answers it.
            if pairs:
                self.mark_legend.append(
                    (Patch(facecolor="#" + ms.color, alpha=0.85,
                           edgecolor="none"), f"{ms.name}  ({len(pairs)}×)"))

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
            any_drawn = False
            for original in ms.intervals:
                clamped = clamped_for.get(id(original))
                if clamped is None:
                    self.occurrences.append(
                        Band(None, ms, original, None, candidate=cand))
                    continue
                patch, extra = self._band_patch(ax, clamped, ms.color,
                                                foreign=True)
                self.occurrences.append(
                    Band(patch, ms, original, clamped, candidate=cand,
                         decorations=extra))
                any_drawn = True
            # FILLED, the same as this pair's own. The open swatch this used to
            # be answered the open bracket the bands used to be drawn as, and
            # both regions are now the same block. A swatch still claiming a
            # distinction the chart no longer makes would send someone looking
            # for an outline that is not there.
            #
            # The distinction is not lost: it is the bar's position on the
            # chart, and in the legend it is the "marked on <pair>" that
            # follows the name -- which says it in words rather than asking
            # anyone to decode a swatch.
            if any_drawn:
                self.mark_legend.append(
                    (Patch(facecolor="#" + ms.color, alpha=0.85,
                           edgecolor="none"),
                     f"{ms.name}  ({len(pairs)}×)  ·  marked on "
                     f"{ms.pair_text}"))

            line = (f"“{ms.name}”, marked on {ms.pair_text} — "
                    f"{len(pairs)} region(s) drawn")
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
            # Solid, under the series lines. It was dashed once, and a dashed
            # line plus hatched bands put two competing textures on a chart
            # whose whole job is showing the shape of two lines.
            #
            # The grey has to work TWICE, which is what makes it delicate: it
            # must stay visible against a dark region and stay muted against
            # the white outside one, or the single line you are meant to
            # follow end to end fades in and out along its length. See
            # GHOST_GREY -- it is chosen against REGION_ALPHA and cannot be
            # moved independently of it.
            ghost.line, = ax.plot(ghost.x, ghost.values, color=GHOST_GREY,
                                  linewidth=1.3, zorder=Z_GHOST,
                                  label=ghost.label)
            ghost.borrowers = tuple(names)
            self.ghosts.append(ghost)
            self.mark_legend.append((ghost.line, ghost.label))

        if self.ghost_problems:
            self.overlay_note = "\n".join(
                [self.overlay_note] + self.ghost_problems).strip()
        # NOTE the frame is NOT touched here. Drawing is not a reason to move
        # the view; see _apply_ylim, called by the two actions that are.

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
        """Widen y to fit the ghosts. Called by BORROWING, never by drawing.

        THE VIEW BELONGS TO WHOEVER IS LOOKING AT IT. Zoom, pan, home and an
        action taken on purpose may move the frame; a redraw may not. This ran
        inside the drawing path once, and saving a region while zoomed snapped
        the vertical scale back to full range -- measured as y (-1.47, 1.46) ->
        (-4.90, 4.86) on a save, with the x zoom surviving, which is worse than
        either being consistent.

        Widening for a ghost is legitimate because BORROWING IS AN ACTION. A
        ghost standardised over a window it does not dominate exceeds the range
        of the two plotted series easily, and a line whose entire job is to
        show what a series was doing must not be cropped by a frame that says
        nothing about having cropped it.

        Only y. The x pin stays exactly as the rubber-band fix left it: a band
        contributes nothing to the y limits at all -- axvspan spans y in AXES
        coordinates -- which is why y can be recomputed and x cannot.
        """
        ax = self.figure.axes[0]
        base = getattr(self, "_series_ylim", None)
        if base is None:
            return
        if not self.ghosts:
            # Restore the frame this widened -- but ONLY if it is still the one
            # this widened. Anything else means the view was moved since, and
            # handing back a borrowed set is not a reason to undo a zoom.
            if self._ghost_ylim is not None \
                    and ax.get_ylim() == self._ghost_ylim:
                ax.set_ylim(base)
            self._ghost_ylim = None
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
        # Remembered so that removing the ghost can tell "the frame I set" from
        # "the frame the user has since chosen", and only undo the former.
        self._ghost_ylim = ax.get_ylim()

    def ghost_message(self) -> str:
        """What to tell someone about a ghost that could not be drawn, or ''.

        Split from showing it for the reason every other message here is: a
        modal dialog raised from inside the drawing path blocks whoever is
        driving the window, including a gate, with no error.
        """
        if not self.ghost_problems:
            return ""
        return ("The regions of that set ARE drawn, but the series they "
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

    # ----------------------------------------------------------- region mode

    def region_mode_active(self) -> bool:
        """Is Region the active mode? THE predicate, and there is only one.

        Both the span selector and the click-to-select handlers ask this and
        nothing else. Before this, the selector consulted `canvas.widgetlock`
        and the click handlers consulted NOTHING, so a click during a zoom
        gesture still moved the selection: two rules answering one question,
        and only one of them right.
        """
        if not self._region_mode:
            return False
        return self._toolbar_mode_name() == "NONE"

    def _toolbar_mode_name(self) -> str:
        """"ZOOM", "PAN" or "NONE". Tolerates being asked before the toolbar
        exists, which happens while the window is still being built."""
        toolbar = getattr(self, "toolbar", None)
        mode = getattr(toolbar, "mode", None)
        return getattr(mode, "name", "NONE") or "NONE"

    def set_region_mode(self, on: bool):
        """Engage Region, releasing Zoom or Pan, or step out of it."""
        if on:
            self._release_toolbar_mode()
        self._region_mode = bool(on)
        self._sync_region_mode()

    def _release_toolbar_mode(self):
        """Un-press whichever of Zoom or Pan is engaged, if either is.

        Through the toolbar's OWN toggle, so the widgetlock is released by the
        code that took it. Dispatched on the mode's enum NAME rather than its
        text: the text for pan is "pan/zoom", so a substring test for "zoom"
        matches the wrong one. If matplotlib ever renames these the gate below
        fails on the widgetlock, which is the point of asserting the release
        rather than the call.
        """
        name = self._toolbar_mode_name()
        if name == "ZOOM":
            self.toolbar.zoom()
        elif name == "PAN":
            self.toolbar.pan()

    def _on_region_button(self):
        """The Region toggle was pressed."""
        self.set_region_mode(bool(self._region_var.get()))

    def _on_toolbar_mode(self):
        """Zoom or Pan was toggled on the toolbar.

        Engaging either steps OUT of Region. Releasing one does not step back
        in: leaving Region is a decision, and re-entering it should be one too,
        by pressing the button that says so.
        """
        if self._toolbar_mode_name() != "NONE":
            self._region_mode = False
        self._sync_region_mode()

    def _sync_region_mode(self):
        """Make the button and the selector agree with the predicate."""
        active = self.region_mode_active()
        if getattr(self, "_region_var", None) is not None:
            if bool(self._region_var.get()) != bool(self._region_mode):
                self._region_var.set(bool(self._region_mode))
        if not active:
            # Drop any half-finished press. Leaving Region between a press and
            # its release must not leave a click waiting to land later.
            self._press_x = None
        span = getattr(self, "span", None)
        if span is not None:
            # One predicate, one switch. `set_active(False)` makes the
            # selector ignore presses AND its own edge handles, so a zoom
            # gesture cannot drag a region's edge by accident either.
            span.set_active(active)

    # ------------------------------------------------------------- selecting

    def _install_span_selector(self):
        """Drag horizontally to define a region.

        `snap_values` makes the rubber band itself land on sample boundaries,
        so the rectangle on screen is the interval that would be stored rather
        than an approximation of it. The authority is still
        `annotations.snap_span`, which is applied to whatever comes back --
        snapping an already-snapped value is idempotent, and the rule belongs
        in the module a gate can reach without opening a window.

        No conflict with the toolbar, and now only one rule saying so: the
        selector is switched off whenever `region_mode_active()` is False.
        `_SelectorWidget.ignore` also consults `canvas.widgetlock`, which zoom
        and pan hold while active, but that was never the whole answer -- the
        click handlers below never consulted it at all.
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
        self._sync_region_mode()        # opens in Region mode, so: active

        # NOTE the press/release handlers are connected above, before this
        # point, because they must exist even when the selector is refused.
        # They ride alongside onselect rather than on it: SpanSelector._release
        # only calls onselect for a zero-span click when a selection ALREADY
        # exists (`span <= minspan` is guarded by `_selection_completed`), so
        # the first click on a band would never arrive.

    # ------------------------------------------------------- selecting a mark

    def _on_press(self, event):
        """Remember where a press landed, so release can tell click from drag.

        Ignored outside Region mode. A press that begins a zoom rectangle is
        not the start of a selection, and remembering it is what let a zoom
        gesture change the selection on release.
        """
        if not self.region_mode_active():
            return
        if event.inaxes is self.figure.axes[0]:
            self._press_x = event.xdata

    def _on_release(self, event):
        """A click that did not move is a SELECT, not a failed drag."""
        if not self.region_mode_active():
            return
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

    def select_band(self, entry, *, from_list=False):
        """Make `entry` the selected mark, or clear when None.

        `from_list` when the selection was made IN the list. The round trip
        back into the tree exists so that a click on the chart moves the
        highlight in the list; a list selection is already there, and pushing
        it back only queues another <<TreeviewSelect>> to answer.
        """
        self.selected_band = entry
        self._restyle_bands()
        self._sync_delete_button()
        if not from_list:
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
            # Says where it came from AND how to get rid of it. The removal
            # used to be a button right here; now it is a tick in the panel,
            # so the readout has to point at it or the gesture is invisible.
            text += (f"   ·   marked on {entry.markset.pair_text}, not on this "
                     f"pair, so it is read-only here — open that pair to "
                     f"change it, or untick “{entry.markset.name}” under "
                     f"“Regions from other pairs” to stop showing it.")
            return text
        if entry.patch is None:
            # Say why there is nothing to drag, rather than leaving someone
            # hunting for handles that cannot exist.
            text += ("   ·   outside this window, so it is not drawn: it can "
                     "be deleted, but adjusting means rebuilding wider.")
        return text

    def _restyle_bands(self):
        """The selected region reads as selected, without changing what it is.

        Selection GROWS THE BAR, and does nothing else. It does not add an
        outline, because an outline appearing on click is the same noise this
        treatment removed, arriving one region at a time. It does not touch
        the fill either: the fill is opaque black and there is nothing past
        black, so a "darker" selection would have to render LIGHTER, which
        inverts the cue.

        Both kinds restyle identically and the bar stays on its own edge, so
        selection is not the one moment a borrowed region and one of this
        pair's own look alike.
        """
        for entry in self.bands:
            chosen = entry is self.selected_band
            bar = next((a for a in entry.decorations
                        if hasattr(a, "set_height")), None)
            if bar is None:
                continue
            lo, hi = self._bar_extent(entry.is_foreign, chosen)
            bar.set_y(lo)
            bar.set_height(hi - lo)

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
                f"marked on {entry.markset.pair_text}, which is not the "
                f"pair on this chart. It is drawn so you can see whether its "
                f"windows line up with what is here, not to be edited through "
                f"them. Open that pair to change it.")

    def _no_candidates_reason(self) -> str:
        """Why the checklist is empty. Never merely blank and silent."""
        if self.store is None:
            return "There is no study here to show regions from."
        return ("Nothing to show: no saved set in this study shares exactly one "
                "series with this pair.")

    def overlay_rows(self) -> list:
        """[(name, label, ticked, detail)] for the checklist. The seam.

        Built here rather than inside the widget so that what the list SAYS
        can be asserted without reading a pixel, and so the widget stays a
        widget: it is handed rows, and knows nothing about sets or pairs.
        """
        rows = []
        for group in self.overlay_groups():
            state = group.state(self.overlay_ids)
            detail = " · ".join(group.pair_texts)
            if state == "some":
                # Never let a tick claim the whole name is on the chart when
                # it is not. Reachable through the per-set seam, and silence
                # here would be a chart disagreeing with its own controls.
                on = sum(1 for i in group.set_ids if i in self.overlay_ids)
                detail = f"{on} of {len(group.set_ids)} shown  ·  {detail}"
            rows.append((group.name, group.offer_text, state != "none",
                         detail))
        return rows

    def _refresh_overlay_controls(self):
        """Rebuild the checklist from the candidates.

        Wholesale, and the ticks are recomputed from `overlay_ids` rather
        than preserved from the widget: what is on the chart is the truth,
        and a tick carried across a refresh could outlive the set it stood
        for -- a set deleted from another window leaves the offer entirely.
        """
        widget = getattr(self, "overlay_list", None)
        if widget is None:
            return
        rows = self.overlay_rows()
        widget.set_items(rows)
        self.overlay_hint.configure(
            text="" if rows else self._no_candidates_reason())

    def overlay(self, set_id: str):
        """Borrow a saved set onto this chart, and redraw. Returns its offer.

        The seam, matching commit_mark and delete_selected: the rule lives
        here, and the checklist above it only calls this. Eligibility is
        re-checked rather than trusted from whatever the widget was showing --
        the offer may have been built before another window deleted the set.
        """
        cand = next((c for c in self.candidates
                     if c.markset.set_id == set_id), None)
        if cand is None:
            raise RuntimeError(
                f"{set_id!r} cannot be shown on this pair. A set is "
                f"offered only when it shares EXACTLY ONE series with what is "
                f"on screen, in this study: sharing both makes it the same "
                f"comparison, which loads as native marks, and sharing neither "
                f"gives it no bearing on what is drawn.")
        self._set_overlay_ids(self.overlay_ids + [set_id])
        return cand

    def remove_overlay(self, set_id: str) -> bool:
        """Stop drawing a borrowed set. True if it was on the chart."""
        if set_id not in self.overlay_ids:
            return False
        self._set_overlay_ids([i for i in self.overlay_ids if i != set_id])
        return True

    def _set_overlay_ids(self, ids) -> bool:
        """Assign what is borrowed and repaint ONCE. True if it changed.

        The single place `overlay_ids` moves. Showing a name can bring
        several sets on at once, and doing that through the per-set entry
        point redrew once per set -- and a redraw RELOADS THE WHOLE STORE
        FROM DISK, so borrowing a name covering three sets read every
        annotation file three times and repainted three times to arrive at
        one picture. Worse, each intermediate repaint was a state no one
        asked for.

        Order is preserved and duplicates dropped, so the caller can hand in
        `existing + more` without having to think about either.
        """
        wanted = list(dict.fromkeys(ids))
        if wanted == self.overlay_ids:
            return False
        self.overlay_ids = wanted
        self.redraw_marks()
        # Here, and not inside the redraw: borrowing is an action, and an
        # action may move the frame. Drawing may not.
        self._apply_ylim()
        return True

    # ------------------------------------------- borrowing, by NAME
    # What the control offers. A name is the unit a person thinks in, and
    # `annotations.group_by_name` is where the rule lives; these are the two
    # verbs over it plus the state the checkboxes read back.

    def overlay_groups(self) -> list:
        """The offers, grouped by name and ordered. The seam under the list."""
        return ann.group_by_name(self.candidates)

    def group_named(self, name: str):
        """The offer carrying `name`, or None."""
        return next((g for g in self.overlay_groups() if g.name == name), None)

    def shown_names(self) -> set:
        """Names with at least one set on the chart."""
        return {g.name for g in self.overlay_groups()
                if g.state(self.overlay_ids) != "none"}

    def show_name(self, name: str):
        """Borrow EVERY eligible set carrying `name`. Returns the group.

        All of them, because the name is what was asked for. Showing
        whichever set happened to sort first would answer a question nobody
        asked and leave the rest of the phenomenon off the chart.
        """
        group = self.group_named(name)
        if group is None:
            raise RuntimeError(
                f"“{name}” is not on offer for this pair. A set is offered "
                f"only when it shares EXACTLY ONE series with what is on "
                f"screen, in this study.")
        self._set_overlay_ids(self.overlay_ids + list(group.set_ids))
        return group

    def hide_name(self, name: str) -> bool:
        """Stop drawing every set carrying `name`. True if anything changed.

        Every set, including the case where only some of them were on the
        chart: unticking a name means the name is gone, and leaving a
        remnant behind an unticked box is how a chart starts disagreeing
        with its own controls.
        """
        group = self.group_named(name)
        if group is None:
            return False
        drop = set(group.set_ids)
        return self._set_overlay_ids(
            [i for i in self.overlay_ids if i not in drop])

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
                "Region from another pair", self.foreign_refusal(self.selected_band,
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
        """Delete follows the selection: live only for a region of THIS pair.

        There is no longer a second button beside it. "Stop showing" acted on
        the selection too, which meant it was dead until a borrowed region
        had been clicked -- the thing that made handing a set back feel
        broken. Unticking its name in the panel does that now, and needs no
        selection at all.
        """
        entry = self.selected_band
        button = getattr(self, "delete_btn", None)
        if button is not None:
            live = (entry is not None and self.store is not None
                    and not entry.is_foreign)
            button.configure(state="normal" if live else "disabled")

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
        for entry in self.bands:
            for art in entry.artists:
                art.remove()
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
                            color="#" + colors[c], label=labels[c],
                            zorder=Z_SERIES)
            self.series_lines[c] = line

        ax.set_ylabel("standard deviations")
        ax.set_xlabel("time (local)")
        # UNDER the regions, both of them. A gridline or a zero rule crossing
        # a dark region is the same noise the region's own edges were, and the
        # region is a backdrop rather than an overlay. `axisbelow` is what
        # moves the grid; without it matplotlib draws it at 1.5, above any
        # patch, and the region would be striped whatever zorder it asked for.
        ax.set_axisbelow(True)
        ax.grid(True, color="#D9D9D9", linewidth=0.8, zorder=Z_GRID)
        ax.axhline(0, color="#808080", linewidth=0.8, zorder=Z_GRID)
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

    # Asserted at the size the window OPENS at, never after a resize -- being
    # visible only to someone who thought to drag the window bigger is what
    # made this invisible for three slices. Tk allocates space to the slaves
    # packed first, so an expanding chart packed first left every fixed-height
    # row below it unmapped, including the two lines that carry the counts
    # this feature promises to report.
    furniture = {"other-pairs checklist": win.overlay_list,
                 "checklist hint": win.overlay_hint,
                 "Delete button": win.delete_btn,
                 "regions note": win.overlay_label,
                 "marks note": win.marks_label,
                 "the regions list": win.mark_tree,
                 "the Region button": win.region_btn}
    absent = [n for n, w in furniture.items() if not w.winfo_ismapped()]
    checks.append((f"every control and status line is on screen at the size "
                   f"the window opens at "
                   f"[{len(furniture) - len(absent)}/{len(furniture)}"
                   f"{'; MISSING ' + ', '.join(absent) if absent else ''}]",
                   not absent))

    # Mapped is not the same as REACHABLE. A row can be mapped and still sit
    # past the bottom edge of the window, which is what a user sees as "it is
    # overflowing". Asserted against the window's own height, not its request.
    def below_edge(w):
        return (w.winfo_rooty() - win.winfo_rooty()) + w.winfo_height() \
            > win.winfo_height()

    over = [n for n, w in furniture.items() if below_edge(w)]
    checks.append((f"and none of them runs past the bottom edge of the window "
                   f"[{'; OVER: ' + ', '.join(over) if over else 'all inside'}]",
                   not over))

    # The SAME assertion sideways, which did not exist until something was
    # added to a horizontal row. The Region button is packed flush after a
    # toolbar of nine buttons plus a coordinate readout; if that row overruns,
    # the button is gone in exactly the way the regions list was gone, and the
    # bottom-edge check above cannot see it.
    def past_right(w):
        return (w.winfo_rootx() - win.winfo_rootx()) + w.winfo_width() \
            > win.winfo_width()

    wide = [n for n, w in furniture.items() if past_right(w)]
    checks.append((f"and none past the right edge either "
                   f"[{'; OVER: ' + ', '.join(wide) if wide else 'all inside'}]",
                   not wide))

    # "Beside Pan and Zoom" is a claim about geometry, so assert the geometry
    # rather than trusting the packing order to have meant it. Reaching into
    # the toolbar's private `_buttons` is fine HERE and not in the window: a
    # gate that breaks on a matplotlib rename is a gate doing its job, where
    # app code that breaks on one is a bug in the field.
    zoom_btn = getattr(win.toolbar, "_buttons", {}).get("Zoom")
    if zoom_btn is None:
        checks.append(("the toolbar exposes a Zoom button to sit beside "
                       "[matplotlib renamed _buttons]", False))
    else:
        same_row = abs(zoom_btn.winfo_rooty() - win.region_btn.winfo_rooty()) < 12
        to_right = win.region_btn.winfo_rootx() > zoom_btn.winfo_rootx()
        gap = win.region_btn.winfo_rootx() - (zoom_btn.winfo_rootx()
                                              + zoom_btn.winfo_width())
        checks.append((f"the Region button is in the toolbar ROW, to the right "
                       f"of Zoom and close to it [same row {same_row}, "
                       f"gap {gap} px]",
                       same_row and to_right and 0 <= gap < 40))

    # An ICON, and one that is still ALIVE. Tk drops an image the moment its
    # last Python reference goes, and the button then renders empty while
    # staying mapped, sized and clickable -- `winfo_ismapped()` cannot tell
    # the difference, which is the withdrawn-dialog lesson wearing a third
    # costume. So: read the pixels back.
    shows_image = bool(str(win.region_btn.cget("image")))
    checks.append((f"the Region button carries an image rather than a text "
                   f"label [{'image' if shows_image else 'NO IMAGE'}]",
                   shows_image and not str(win.region_btn.cget("text"))))

    icon = win.region_icon
    opaque = [(x, y) for x in range(icon.width()) for y in range(icon.height())
              if not icon.transparency_get(x, y)]
    inked = {icon.get(x, y) for x, y in opaque}
    checks.append((f"and the image survived, with pixels actually drawn "
                   f"[{icon.width()}x{icon.height()}, {len(opaque)} inked]",
                   (icon.width(), icon.height()) == (24, 24)
                   and len(opaque) > 80))

    # COHESION, measured rather than asserted in a comment: the same canvas
    # and the same single pure black as matplotlib's own toolbar icons.
    #
    # WEIGHT IS DELIBERATELY NOT COMPARED. It used to be, and the bound said
    # 0.6x the lightest stock icon. This glyph is an OUTLINE -- a frame two
    # units thick -- where the stock ones are filled silhouettes, so it inks
    # about 105 px against their 228-284 and always will. That is the style
    # that was asked for, not a defect, and a bound loose enough to admit it
    # would be loose enough to admit anything. The floor above is what still
    # catches an icon that failed to draw.
    stock = _stock_icon_stats()
    checks.append((f"and match the stock toolbar icons: same canvas, ONE "
                   f"colour, pure black [{sorted(inked)} vs stock "
                   f"{sorted(stock['colours'])}]",
                   inked == {(0, 0, 0)}
                   and (icon.width(), icon.height()) == stock["size"]))

    # The transcription, checked against the SVG it came from rather than
    # against how it looks. Four claims, each a coordinate in that path:
    # the frame is HOLLOW, it is OPEN at the top left, and the plus is in the
    # opening -- above the frame and to the left of it.
    def lit(x, y):
        return not icon.transparency_get(x, y)

    shape = {"hollow interior": not lit(14, 14),
             "right edge": lit(19, 12),
             "bottom edge": lit(12, 19),
             "corner left open": not lit(11, 6) and not lit(12, 6),
             "plus above frame": lit(6, 3) and lit(7, 4),
             "plus left of frame": lit(3, 6) and lit(4, 7)}
    wrong = [k for k, v in shape.items() if not v]
    checks.append((f"and depict a select-area frame with a plus in its open "
                   f"corner, transcribed from the source path "
                   f"[{'all 6 hold' if not wrong else 'WRONG: ' + ', '.join(wrong)}]",
                   not wrong))
    checks.append((f"and the button is drawn at least as wide as its icon, so "
                   f"the image is rendered and not merely attached "
                   f"[{win.region_btn.winfo_width()} px]",
                   win.region_btn.winfo_width() >= icon.width()))

    # The control that brings another pair's regions in sits in the SIDE
    # PANEL, above the list of regions it adds to. It used to be a row above
    # the chart, which is where it went after being the last thing on a window
    # it fitted by ten pixels; the panel is packed before the chart for that
    # same reason, so it still cannot be pushed anywhere.
    list_top = win.overlay_list.winfo_rooty() - win.winfo_rooty()
    tree_top = win.mark_tree.winfo_rooty() - win.winfo_rooty()
    checks.append((f"the other-pairs checklist is in the panel ABOVE the "
                   f"regions list, so the control comes before what it "
                   f"controls [checklist y={list_top}, list y={tree_top}]",
                   0 < list_top < tree_top))
    chart_right = (win.canvas.get_tk_widget().winfo_rootx()
                   + win.canvas.get_tk_widget().winfo_width())
    checks.append((f"and beside the chart rather than under it, so a shorter "
                   f"window cannot cut it off [checklist x="
                   f"{win.overlay_list.winfo_rootx() - win.winfo_rootx()}]",
                   win.overlay_list.winfo_rootx() >= chart_right))

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
        # The colour bar along a region's edge is a patch too, and it is
        # excluded the same way and for the same reason: by identity, from the
        # window's own record of what it drew as decoration, never by type or
        # position -- either of which would start swallowing real regions the
        # day one matched.
        def geometry_spans():
            rubber = getattr(win.span, "_selection_artist", None)
            decor = {id(a) for b in win.occurrences for a in b.decorations}
            return [p for p in win.figure.axes[0].patches
                    if p is not rubber and id(p) not in decor]

        rubber_band = getattr(win.span, "_selection_artist", None)
        spans = geometry_spans()
        checks.append(("the drag rubber-band is on the axes and was excluded",
                       rubber_band is not None
                       and any(p is rubber_band
                               for p in win.figure.axes[0].patches)))
        checks.append((f"each region's colour bar is on the axes beside its "
                       f"band, and was excluded too "
                       f"[{sum(len(b.decorations) for b in win.bands)} "
                       f"decoration(s)]",
                       all(all(a in win.figure.axes[0].patches
                               or a in win.figure.axes[0].get_lines()
                               for a in b.decorations) for b in win.bands)
                       and bool(win.bands)))
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

        spans_now = geometry_spans()
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
        others = [b for b in win.bands if b is not target]

        def bar_height(band):
            bar = next(a for a in band.decorations if hasattr(a, "get_height"))
            return abs(bar.get_height())

        checks.append((f"the selected region reads apart from the others, by a "
                       f"TALLER BAR [{bar_height(target):.3f} vs "
                       f"{[round(bar_height(b), 3) for b in others]}]",
                       bool(others)
                       and all(bar_height(target) > bar_height(b)
                               for b in others)))
        # The cue that stopped being available. Darkening cannot signal
        # anything once the fill is opaque, and a selection rendered lighter
        # would invert the signal, so assert the fill is NOT what moved.
        checks.append((f"and NOT by a change of fill, which is opaque for "
                       f"every region and has nowhere darker to go "
                       f"[{target.patch.get_alpha():.2f} vs "
                       f"{sorted({round(b.patch.get_alpha(), 2) for b in others})}]",
                       all(target.patch.get_alpha() == b.patch.get_alpha()
                           for b in others)))
        # The bar must stay on its own edge while it grows, or a selected
        # borrowed region would creep toward looking like one of ours.
        def bar_edge(band):
            bar = next(a for a in band.decorations if hasattr(a, "get_y"))
            return bar.get_y() + (0 if band.is_foreign else bar.get_height())

        checks.append((f"and the bar grows INWARD, staying anchored to the "
                       f"edge that says which pair it belongs to "
                       f"[anchored at {bar_edge(target):.2f}]",
                       abs(bar_edge(target) - (0.0 if target.is_foreign
                                               else 1.0)) < 1e-9))
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

        # ---- Region mode, and the modes it is exclusive with ----------------
        # Driven through the toolbar's OWN public toggles, which is what its
        # buttons call. Asserting the widgetlock rather than the call is what
        # makes this survive matplotlib renaming the mode enum: the release
        # would stop happening and this would say so.
        def click_chart(xdata):
            """A real press-release on the chart, at a data x."""
            cx, cy = win.figure.axes[0].transData.transform((xdata, 0.0))
            for nm in ("button_press_event", "button_release_event"):
                win.canvas.callbacks.process(
                    nm, MouseEvent(nm, win.canvas, int(cx), int(cy),
                                   button=MouseButton.LEFT))

        checks.append((f"the window OPENS in Region mode, with the button "
                       f"pressed [mode {win._toolbar_mode_name()}, button "
                       f"{'on' if win._region_var.get() else 'off'}]",
                       win.region_mode_active()
                       and bool(win._region_var.get())))
        checks.append(("and the span selector is live in it, so a drag marks "
                       "immediately", win.span is not None and win.span.active))

        win.toolbar.zoom()
        checks.append((f"engaging Zoom steps OUT of Region and un-presses its "
                       f"button [mode {win._toolbar_mode_name()}, button "
                       f"{'on' if win._region_var.get() else 'off'}]",
                       not win.region_mode_active()
                       and not bool(win._region_var.get())))
        checks.append(("and switches the span selector off, so a zoom drag "
                       "cannot mark or drag a region's edge",
                       win.span is not None and not win.span.active))

        # The stray-click bug: the click handlers never consulted the lock.
        win.select_band(win.bands[0])
        held = win.selected_band
        other = win.bands[1]
        click_chart(other.patch.get_x() + other.patch.get_width() / 2)
        checks.append((f"a click while Zoom is active leaves the selection "
                       f"alone [{held.markset.name if held else None}]",
                       win.selected_band is held))

        locked_during_zoom = not win.canvas.widgetlock.available(win)
        win.set_region_mode(True)
        checks.append((f"pressing Region releases the widgetlock Zoom was "
                       f"holding [held during zoom: {locked_during_zoom}]",
                       locked_during_zoom
                       and win.canvas.widgetlock.available(win)))
        checks.append((f"and Region is the active mode again "
                       f"[mode {win._toolbar_mode_name()}]",
                       win.region_mode_active()
                       and bool(win._region_var.get())))
        checks.append(("with the selector live once more",
                       win.span is not None and win.span.active))

        click_chart(other.patch.get_x() + other.patch.get_width() / 2)
        checks.append((f"and a click selects again, so leaving Zoom did not "
                       f"leave the chart inert "
                       f"[{win.selected_band.markset.name if win.selected_band else None}]",
                       win.selected_band is other))

        # Pan is the other half of the exclusivity, and "pan/zoom" contains
        # the substring "zoom" -- a naive release would have called zoom() and
        # engaged the mode it meant to clear.
        win.toolbar.pan()
        checks.append((f"engaging Pan also steps out of Region "
                       f"[mode {win._toolbar_mode_name()}]",
                       win._toolbar_mode_name() == "PAN"
                       and not win.region_mode_active()))
        win.set_region_mode(True)
        checks.append((f"and pressing Region releases PAN rather than "
                       f"toggling Zoom on [mode {win._toolbar_mode_name()}]",
                       win._toolbar_mode_name() == "NONE"
                       and win.region_mode_active()))

        # Stepping out by the button, not by the toolbar: the selector goes
        # quiet, and the chart stops taking selections.
        win.set_region_mode(False)
        checks.append(("un-pressing Region switches the selector off without "
                       "any toolbar mode being engaged",
                       win._toolbar_mode_name() == "NONE"
                       and not win.region_mode_active()
                       and win.span is not None and not win.span.active))
        win.select_band(None)
        click_chart(other.patch.get_x() + other.patch.get_width() / 2)
        checks.append(("and a click does nothing while it is off",
                       win.selected_band is None))
        win.set_region_mode(True)

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

        # Drive the list the way a click does: selection_set, then update() so
        # Tk delivers the queued <<TreeviewSelect>> itself. Calling
        # _on_row_selected by hand -- what this gate used to do -- skips the
        # only part of the path that ever broke, and a runaway re-entry
        # shipped straight past it. The chart checks above already drive real
        # MouseEvents through canvas.callbacks.process; this is the same rule,
        # finally applied to the list.
        #
        # BOUNDED, because an unbounded runaway hangs update() forever and a
        # gate that hangs reads as a slow machine. Past the bound the wrapper
        # stops feeding the real handler, so the loop starves, update()
        # returns, and the count fails the check instead.
        row_calls = [0]
        ROW_CALL_BOUND = 20
        _row_handler = win._on_row_selected

        def _counted_row_select(event=None):
            row_calls[0] += 1
            if row_calls[0] > ROW_CALL_BOUND:
                return
            return _row_handler(event)

        win.mark_tree.bind("<<TreeviewSelect>>", _counted_row_select)
        row_counts = []

        def pick_row(target):
            """Select a row through Tk. Returns how many times it dispatched."""
            win.update()            # drain anything already queued
            row_calls[0] = 0
            win.mark_tree.selection_set(target)
            win.update()            # deliver the queued event, and its answers
            row_counts.append(row_calls[0])
            return row_calls[0]

        n = pick_row(row)
        checks.append((f"picking a row runs the handler ONCE through the real "
                       f"Tk event, not by hand [{n} dispatch(es)]", n == 1))
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

        pick_row(row)
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
        pick_row(header)
        checks.append(("picking a set header selects nothing, rather than "
                       "guessing which occurrence was meant",
                       win.selected_band is None))

        # The regression this issue is about is a COUNT, not a wrong value:
        # every check above passes just as well when the handler ran sixty
        # times on its way there. Assert the count itself, once, over every
        # selection the gate made.
        worst = max(row_counts) if row_counts else 0
        checks.append((f"no list selection re-entered -- every one dispatched "
                       f"at most once [{row_counts}]",
                       bool(row_counts) and worst <= 1))

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

        # Read off the AXES. What is on the chart is the claim being made.
        # EVERY region is now the same shape -- a neutral block, no border --
        # and the bar's position is the entire distinction, so that is what
        # these assert.
        every = native_bands + foreign_bands
        fill_alpha = [b.patch.get_facecolor()[3] for b in foreign_bands]
        native_fill = [b.patch.get_facecolor()[3] for b in native_bands]
        checks.append((f"every region is filled the SAME, borrowed or not -- "
                       f"the fill no longer carries the distinction "
                       f"[{native_fill} vs {fill_alpha}]",
                       bool(foreign_bands) and bool(native_bands)
                       and len(set(native_fill + fill_alpha)) == 1))
        neutral = {tuple(round(v, 4) for v in b.patch.get_facecolor()[:3])
                   for b in every}
        checks.append((f"and filled NEUTRALLY, not in the set's colour -- one "
                       f"hue per series is already on this chart [{neutral}]",
                       neutral == {(0.0, 0.0, 0.0)}))

        # This replaces "the shading is light enough that the data survives".
        # The old rule capped the fill because the fill was drawn over
        # nothing in particular; the guarantee now comes from LAYERING, which
        # holds at any darkness: the series are above the region, always.
        region_z = max(b.patch.get_zorder() for b in every)
        series_z = min(ln.get_zorder() for ln in win.series_lines.values())
        checks.append((f"the data survives the block by being drawn ON TOP of "
                       f"it, at any darkness [region z {region_z}, series z "
                       f"{series_z}]", region_z < series_z))
        grid_z = max(ln.get_zorder()
                     for ln in win.figure.axes[0].get_ygridlines())
        checks.append((f"and the gridlines are UNDER it, so no horizontal rule "
                       f"crosses a region [grid z {grid_z}, region z "
                       f"{region_z}]", grid_z < region_z))

        # The whole point of the change: no borders. Two saturated vertical
        # rules per region, crossing the series lines at every boundary, were
        # the visual noise a marked window is supposed to cut through.
        bordered = [b for b in every
                    if b.patch.get_linewidth()
                    or b.patch.get_edgecolor()[3] > 0]
        strays = [a for b in every for a in b.decorations
                  if hasattr(a, "get_xdata")]
        checks.append((f"NO region has a border, and none has edge rules "
                       f"[{len(bordered)} bordered, {len(strays)} rule(s)]",
                       not bordered and not strays))
        checks.append((f"each carries exactly one decoration, the colour bar "
                       f"[{sorted({len(b.decorations) for b in every})}]",
                       all(len(b.decorations) == 1 for b in every)))
        checks.append(("nothing is hatched any more, on either kind",
                       not any(b.patch.get_hatch()
                               for b in foreign_bands + native_bands)))

        def bar_of(band):
            """The colour bar: the one decoration that is a patch."""
            return next(a for a in band.decorations if hasattr(a, "get_xy"))

        native_bar_y = [bar_of(b).get_y() for b in native_bands]
        foreign_bar_y = [bar_of(b).get_y() for b in foreign_bands]
        checks.append((f"the set's colour is a bar along the TOP of this "
                       f"pair's regions and the BOTTOM of another pair's, so "
                       f"position says which before colour does "
                       f"[{[round(v, 2) for v in native_bar_y]} vs "
                       f"{[round(v, 2) for v in foreign_bar_y]}]",
                       bool(native_bar_y) and bool(foreign_bar_y)
                       and all(v > 0.9 for v in native_bar_y)
                       and all(v < 0.1 for v in foreign_bar_y)))
        set_rgb = {tuple(round(int(wl.markset.color[i:i + 2], 16) / 255, 4)
                         for i in (0, 2, 4))}
        checks.append(("and the bar carries the SET's colour, which is what "
                       "the legend swatch matches",
                       all(tuple(round(v, 4)
                                 for v in bar_of(b).get_facecolor()[:3])
                           in set_rgb for b in foreign_bands)))

        # The bar spans the region's full width, so it locates as well as
        # identifies. Dropping the edge rules removed the other thing that
        # marked where a region starts and stops, and a bar that only sat
        # over part of it would leave that unanswered.
        spans = all(abs(bar_of(b).get_x() - b.patch.get_x()) < 1e-9
                    and abs(bar_of(b).get_width() - b.patch.get_width()) < 1e-9
                    for b in every)
        checks.append(("and spans the region's full width, which is what marks "
                       "its bounds now that the edge rules are gone", spans))

        # The narrow-region case the edge rules were added for. A six-hour
        # region on a 45-day window is about 6 px, and the reason a fill alone
        # was not enough at 15% grey. At this fill it is, and that is the
        # trade being made -- so assert the fill is actually dark enough to
        # see, rather than assuming it.
        checks.append((f"the fill is dark enough to find a narrow region "
                       f"without an outline [alpha {native_fill[0]:.2f}]",
                       native_fill[0] >= 0.35))

        legend = [t.get_text()
                  for t in win.figure.axes[0].get_legend().get_texts()]
        attributed = [t for t in legend if "marked on" in t]
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
        checks.append((f"the list flags rows from another pair [{parents}]",
                       len(rows) == 3 and parents == {"other pair"}))

        # ---- a borrowed band is read-only -----------------------------------
        edge = foreign_bands[0]
        win.select_band(edge)
        checks.append(("a borrowed band can be selected, so clicking one says "
                       "where it came from",
                       win.selected_band is edge))
        checks.append((f"the readout says which pair it was marked on, and "
                       f"that it is read-only here "
                       f"[...{win.span_text.get()[-78:]}]",
                       "marked on" in win.span_text.get()
                       and "read-only" in win.span_text.get()
                       and wl.markset.pair_text in win.span_text.get()))
        checks.append(("no adjust handles are put on it, so there is no drag "
                       "to have to refuse",
                       not win.span.get_visible()))
        checks.append(("the Delete button is OFF for a region from another pair",
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

        # ---- the checklist ---------------------------------------------------
        # Driven through the WIDGET, not through show_name/hide_name. The
        # seams are checked headless in annotations.py; what is left to
        # establish here is that the boxes are wired to them, which is
        # precisely what calling the seam by hand cannot see -- the lesson
        # #19 was opened for.
        rows = {name: (label, ticked, detail)
                for name, label, ticked, detail in win.overlay_rows()}
        checks.append((f"the checklist offers one row per NAME, not one per "
                       f"set [{sorted(rows)}]",
                       sorted(rows) == sorted({c.markset.name
                                               for c in win.candidates})))
        checks.append((f"each row counts its REGIONS and names its source "
                       f"pair [{rows[wl.markset.name][0]}]",
                       "region" in rows[wl.markset.name][0]
                       and wl.markset.pair_text in rows[wl.markset.name][2]))
        checks.append((f"and the one already on the chart is TICKED "
                       f"[{sorted(win.overlay_list.checked())}]",
                       win.overlay_list.checked() == {wl.markset.name}))

        # Untick it. No selection first, which is the entire point: the old
        # control was dead until a borrowed region had been clicked.
        win.select_band(None)
        win.overlay_list.invoke(wl.markset.name)
        checks.append((f"unticking a name with NOTHING selected stops showing "
                       f"it, and says nothing was deleted "
                       f"[...{win.span_text.get()[-46:]}]",
                       not [b for b in win.bands if b.is_foreign]
                       and "Nothing was deleted" in win.span_text.get()
                       and (tmp / f"{wl.markset.set_id}.json").exists()))
        checks.append(("and the box is left unticked, so the panel agrees "
                       "with the chart",
                       wl.markset.name not in win.overlay_list.checked()))

        win.overlay_list.invoke(wl.markset.name)
        checks.append((f"ticking it again draws every region under that name "
                       f"[{len([b for b in win.bands if b.is_foreign])} "
                       f"borrowed]",
                       [b.markset.set_id for b in win.bands if b.is_foreign]
                       == [wl.markset.set_id] * 2))
        checks.append(("Delete stays off for a borrowed region, which is "
                       "handed back rather than deleted",
                       win.select_band(next(b for b in win.bands
                                            if b.is_foreign)) is not None
                       and str(win.delete_btn.cget("state")) == "disabled"))
        checks.append((f"and the readout says how to stop showing it, now "
                       f"that no button does [...{win.span_text.get()[-38:]}]",
                       "untick" in win.span_text.get()))
        win.select_band(next(b for b in win.bands if not b.is_foreign))
        checks.append(("while Delete is live for one of this pair's own",
                       str(win.delete_btn.cget("state")) == "normal"))

        # ---- one NAME, several sets ------------------------------------------
        # The case the checklist exists for. "Internal tide" marked on two
        # comparisons is two sets and one phenomenon, and the old dropdown
        # offered them as two lines differing only in a trailing pair.
        twins = ann.Store(tmp)
        for partner, when in [(refs[0], idx[520]), (refs[1], idx[600])]:
            twins.confirm(study_id=info.study_id,
                          pair=(partner, foreign_ref), name="same phenomenon",
                          reason="seen on two comparisons",
                          start_utc=when, end_utc=when + dt.timedelta(hours=8))
        win.redraw_marks()

        twin_ids = {c.markset.set_id for c in win.candidates
                    if c.markset.name == "same phenomenon"}
        checks.append((f"two sets under one name are TWO sets in the store "
                       f"[{len(twin_ids)}]", len(twin_ids) == 2))
        twin_rows = [r for r in win.overlay_rows() if r[0] == "same phenomenon"]
        checks.append((f"but ONE row in the checklist [{len(twin_rows)} row(s), "
                       f"'{twin_rows[0][1] if twin_rows else ''}']",
                       len(twin_rows) == 1))

        win.overlay_list.invoke("same phenomenon")
        drawn_ids = {b.markset.set_id for b in win.bands if b.is_foreign}
        checks.append((f"and ticking it draws EVERY set under that name, not "
                       f"whichever sorted first [{len(twin_ids & drawn_ids)} "
                       f"of {len(twin_ids)}]",
                       twin_ids <= drawn_ids))

        win.overlay_list.invoke("same phenomenon")
        left_over = {b.markset.set_id for b in win.bands if b.is_foreign}
        checks.append((f"unticking takes all of them off, leaving no remnant "
                       f"behind an unticked box [{len(twin_ids & left_over)} "
                       f"left]", not (twin_ids & left_over)))

        # The half-shown case. Reachable because the per-set seam is still
        # there, and a tick that claimed the whole name was on the chart
        # would be a control disagreeing with the chart it drives.
        win.overlay(sorted(twin_ids)[0])
        half = next(r for r in win.overlay_rows() if r[0] == "same phenomenon")
        checks.append((f"with only one of the two borrowed, the row SAYS SO "
                       f"rather than claiming the name is shown [{half[3]}]",
                       half[2] and "1 of 2 shown" in half[3]))
        win.hide_name("same phenomenon")
        checks.append(("and unticking a half-shown name clears the remnant",
                       not [b for b in win.bands
                            if b.markset.set_id in twin_ids]))

        bare = ViewWindow(root_tk, res, study=info,
                          annotations_dir=tmp / "no-sets-here")
        bare.update_idletasks()
        hint = str(bare.overlay_hint.cget("text"))
        checks.append((f"with nothing eligible the list is empty and SAYS WHY "
                       f"[{hint}]",
                       not bare.overlay_list.keys()
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
        checks.append((f"it is SOLID and thinner than a compared series, and "
                       f"sits under them [lw {ghost_lines[0].get_linewidth()} "
                       f"vs {[ln.get_linewidth() for ln in win.series_lines.values()]}]",
                       ghost_lines[0].get_linestyle() in ("-", "solid")
                       and ghost_lines[0].get_linewidth()
                       < min(ln.get_linewidth()
                             for ln in win.series_lines.values())
                       and ghost_lines[0].get_zorder()
                       < min(ln.get_zorder()
                             for ln in win.series_lines.values())))
        ghost_rgb = ghost_lines[0].get_color().lstrip("#").upper()
        r, g, b = (int(ghost_rgb[i:i + 2], 16) for i in (0, 2, 4))
        grey = abs(r - g) < 8 and abs(g - b) < 8
        # LIGHT, and that reverses what this check used to assert. It wanted a
        # MID grey, because a light line vanished inside the old 15% shading
        # and the ghost had to survive both backdrops. The region is opaque
        # black now, so the two backdrops swapped: mid grey is what disappears
        # inside a region, and light grey is what reads against it while
        # staying muted against the saturated series lines outside.
        #
        # Tied to the fill on purpose. A ghost grey chosen against one
        # REGION_ALPHA is wrong for another, and the pair of them was the one
        # thing that could not be got right at 40% -- no grey worked at all.
        region_lum = 255 * (1 - REGION_ALPHA)     # the fill, over white
        ghost_lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        checks.append((f"and grey, at no series colour, and LIGHT enough to "
                       f"read against an opaque region while staying muted on "
                       f"white [{ghost_rgb}, luminance {ghost_lum:.0f} vs "
                       f"region {region_lum:.0f}]",
                       grey and 0xA0 <= r <= 0xD0
                       and ghost_lum - region_lum >= 100
                       and ghost_rgb not in
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
        # By IDENTITY, not by counting lines: a region from another pair draws
        # edge rules, which are Line2D too, so a count would be measuring the
        # bands going away rather than the ghost.
        doomed_line = win.ghosts[0].line
        doomed_artists = [a for b in win.bands if b.is_foreign
                          for a in b.artists]
        win.remove_overlay(wl.markset.set_id)
        checks.append((f"removing the last borrower takes the ghost off the "
                       f"axes [{len(win.figure.axes[0].get_lines())} lines "
                       f"left]",
                       not win.ghosts
                       and doomed_line not in win.figure.axes[0].get_lines()
                       and win.figure.axes[0].get_ylim() == win._series_ylim))
        checks.append((f"and takes EVERY artist of its regions with it, "
                       f"leaving no colour bar or edge rule behind "
                       f"[{len(doomed_artists)} checked]",
                       bool(doomed_artists)
                       and not any(a in win.figure.axes[0].patches
                                   or a in win.figure.axes[0].get_lines()
                                   for a in doomed_artists)))
        win.overlay(wl.markset.set_id)

        # ---- the view belongs to whoever is looking at it --------------------
        # docs/adr/0001. This is the assertion the frame code cannot make about
        # itself: _apply_ylim ran inside the drawing path, so saving a region
        # while zoomed snapped the vertical scale back to full range while the
        # x zoom survived. Nothing above would have noticed -- band positions
        # are asserted in DATA coordinates, which stay correct however absurd
        # the view becomes.
        zoomed_x = (float(win._xnum[300]), float(win._xnum[380]))
        zoomed_y = (-1.5, 1.5)
        axis.set_xlim(zoomed_x)
        axis.set_ylim(zoomed_y)
        win.selection = (idx[320], idx[340])
        win._selected_indices = (320, 340)
        win.commit_mark("zoomed in", "saved without leaving the zoom")
        moved = (axis.get_xlim(), axis.get_ylim())
        checks.append((f"saving a region while zoomed leaves the view exactly "
                       f"where it was [y {moved[1][0]:.2f}..{moved[1][1]:.2f}, "
                       f"wanted {zoomed_y[0]:.2f}..{zoomed_y[1]:.2f}]",
                       moved == (zoomed_x, zoomed_y)))
        checks.append(("and the region saved at that zoom is on the chart, so "
                       "the frame was kept by not moving it rather than by "
                       "not drawing",
                       any(b.markset.name == "zoomed in" for b in win.bands)))
        axis.set_xlim(pinned_x)
        win._apply_ylim()          # back to the borrowed frame for what follows

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
                       f"[{len([t for t in legend if 'marked on' in t])} "
                       f"attributed]",
                       len([t for t in legend if "marked on" in t]) == 2))

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
                       "regions of that set ARE drawn" in gmsg
                       and "x::y::z" in gmsg
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
