"""
exporter.py -- writes the generated comparison workbook.

Everything on the `stats` sheet is a live formula pointing at `data`, so if you
paste in a corrected series or trim a date range by hand, the numbers follow.
Only the lag table is precomputed, because scanning lags in Excel needs an
OFFSET mess that would be worse than the thing it replaces.
"""

from __future__ import annotations

import re

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import sensorkit as sk
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference, ScatterChart, Series
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.layout import Layout, ManualLayout
from openpyxl.chart.marker import Marker
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.text import RichText
from openpyxl.drawing.line import LineProperties
from openpyxl.drawing.text import (CharacterProperties, Font as Font_,
                                   Paragraph, ParagraphProperties,
                                   RichTextProperties)
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

BODY = Font(name="Arial", size=10)
HEAD = Font(name="Arial", size=10, bold=True)
TITLE = Font(name="Arial", size=12, bold=True)
NOTE = Font(name="Arial", size=9, italic=True, color="808080")
HEADFILL = PatternFill("solid", fgColor="D9D9D9")
WARNFILL = PatternFill("solid", fgColor="FFF2CC")

DT_FMT = "yyyy-mm-dd hh:mm"
NUM_FMT = "0.000"


def _style_header(ws, row=1, ncols=None):
    ncols = ncols or ws.max_column
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = HEAD
        cell.fill = HEADFILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = ws.cell(row=row + 1, column=3)


def _autosize(ws, minw=10, maxw=34):
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        longest = max((len(str(c.value)) for c in col[:60] if c.value is not None),
                      default=0)
        ws.column_dimensions[letter].width = min(max(minw, longest + 2), maxw)


def _write_data_sheet(wb, result):
    ws = wb.create_sheet("data")
    cols = list(result.data.columns)

    ws.cell(row=1, column=1, value="time (local)")
    ws.cell(row=1, column=2, value="time (UTC)")
    for j, c in enumerate(cols, start=3):
        unit = result.units.get(c, "")
        ws.cell(row=1, column=j, value=f"{c} [{unit}]" if unit else c)

    local = result.data.index.tz_convert(LOCAL_TZ)
    for i, (tl, tu) in enumerate(zip(local, result.data.index), start=2):
        ws.cell(row=i, column=1, value=tl.replace(tzinfo=None)).number_format = DT_FMT
        ws.cell(row=i, column=2, value=tu.replace(tzinfo=None)).number_format = DT_FMT
        for j, c in enumerate(cols, start=3):
            v = result.data.iloc[i - 2][c]
            cell = ws.cell(row=i, column=j,
                           value=None if pd.isna(v) else float(v))
            cell.number_format = NUM_FMT

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY
    _style_header(ws)
    _autosize(ws)
    return ws, cols


def _write_counts_sheet(wb, result, cols):
    ws = wb.create_sheet("counts")
    ws.cell(row=1, column=1, value="time (local)")
    for j, c in enumerate(cols, start=2):
        ws.cell(row=1, column=j, value=c)

    local = result.data.index.tz_convert(LOCAL_TZ)
    expected = {}
    for c in cols:
        try:
            cad = pd.Timedelta(result.cadences.get(c, "NaT"))
            expected[c] = max(1, int(pd.Timedelta(result.interval) / cad))
        except Exception:
            expected[c] = 1

    for i, tl in enumerate(local, start=2):
        ws.cell(row=i, column=1, value=tl.replace(tzinfo=None)).number_format = DT_FMT
        for j, c in enumerate(cols, start=2):
            n = int(result.counts.iloc[i - 2][c]) if c in result.counts else 0
            cell = ws.cell(row=i, column=j, value=n)
            if n < expected[c] / 2:
                cell.fill = WARNFILL

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY
    _style_header(ws)
    _autosize(ws)

    r = ws.max_row + 2
    ws.cell(row=r, column=1,
            value="Shaded = fewer than half the samples expected for this "
                  "interval, given the series' native cadence. Treat those "
                  "averages as thin.").font = NOTE
    return ws


def _write_stats_sheet(wb, result, cols, lag_table=None, reference=None):
    ws = wb.create_sheet("stats")
    n = len(result.data)
    last = n + 1  # data sheet last row

    def dref(c_index):
        L = get_column_letter(c_index + 3)
        return f"data!${L}$2:${L}${last}"

    ws.cell(row=1, column=1, value="Summary").font = TITLE
    heads = ["series", "unit", "n", "mean", "std", "min", "max", "native cadence"]
    for j, h in enumerate(heads, start=1):
        ws.cell(row=2, column=j, value=h)
    for i, c in enumerate(cols):
        r = 3 + i
        ws.cell(row=r, column=1, value=c)
        ws.cell(row=r, column=2, value=result.units.get(c, ""))
        ws.cell(row=r, column=3, value=f"=COUNT({dref(i)})")
        ws.cell(row=r, column=4, value=f"=AVERAGE({dref(i)})").number_format = NUM_FMT
        ws.cell(row=r, column=5, value=f"=STDEV({dref(i)})").number_format = NUM_FMT
        ws.cell(row=r, column=6, value=f"=MIN({dref(i)})").number_format = NUM_FMT
        ws.cell(row=r, column=7, value=f"=MAX({dref(i)})").number_format = NUM_FMT
        ws.cell(row=r, column=8, value=result.cadences.get(c, ""))
    _style_header(ws, row=2, ncols=len(heads))

    base = 3 + len(cols) + 2
    ws.cell(row=base, column=1, value="Correlation matrix (lag 0)").font = TITLE
    hr = base + 1
    for j, c in enumerate(cols, start=2):
        ws.cell(row=hr, column=j, value=c).font = HEAD
    for i, c in enumerate(cols):
        r = hr + 1 + i
        ws.cell(row=r, column=1, value=c).font = HEAD
        for j, _ in enumerate(cols):
            cell = ws.cell(row=r, column=2 + j)
            if i == j:
                cell.value = 1.0
            else:
                cell.value = f"=IFERROR(CORREL({dref(i)},{dref(j)}),\"\")"
            cell.number_format = "0.000"

    r = hr + len(cols) + 3
    if lag_table is not None and len(lag_table):
        ws.cell(row=r, column=1,
                value=f"Lag scan against: {reference}").font = TITLE
        r += 1
        lheads = ["series", "r at lag 0", "lags reference by (h)",
                  "r at best lag", "alt lag (h), 12.42 h ambiguity",
                  "n overlapping"]
        for j, h in enumerate(lheads, start=1):
            ws.cell(row=r, column=j, value=h)
        _style_header(ws, row=r, ncols=len(lheads))
        ws.freeze_panes = None
        for _, rec in lag_table.iterrows():
            r += 1
            ws.cell(row=r, column=1, value=rec["series"])
            ws.cell(row=r, column=2, value=float(rec["r_at_lag_0"])).number_format = "0.000"
            ws.cell(row=r, column=3, value=float(rec["best_lag_h"])).number_format = "0.00"
            ws.cell(row=r, column=4, value=float(rec["r_at_best_lag"])).number_format = "0.000"
            ws.cell(row=r, column=5, value=float(rec["ambiguous_alt_h"])).number_format = "0.00"
            ws.cell(row=r, column=6, value=int(rec["n_overlapping"]))
        r += 2
        ws.cell(row=r, column=1,
                value="Negative lag means the series leads the reference. "
                      "La Jolla nearshore temperature is dominated by the "
                      "~12.42 h internal tide, so a peak at lag L cannot be "
                      "distinguished from one at L +/- 12.42 h. Both are "
                      "listed. Resolve with physics, not with this table.").font = NOTE

    for row in ws.iter_rows():
        for cell in row:
            if cell.font is BODY or cell.font.name is None:
                cell.font = BODY
    _autosize(ws)
    return ws


def _write_zscore_sheet(wb, result, cols):
    """Standardised copy, so series in different units share one axis."""
    ws = wb.create_sheet("normalized")
    z = (result.data - result.data.mean()) / result.data.std(ddof=0)

    ws.cell(row=1, column=1, value="time (local)")
    for j, c in enumerate(cols, start=2):
        ws.cell(row=1, column=j, value=c)

    local = result.data.index.tz_convert(LOCAL_TZ)
    for i, tl in enumerate(local, start=2):
        ws.cell(row=i, column=1, value=tl.replace(tzinfo=None)).number_format = DT_FMT
        for j, c in enumerate(cols, start=2):
            v = z.iloc[i - 2][c]
            ws.cell(row=i, column=j,
                    value=None if pd.isna(v) else float(v)).number_format = NUM_FMT

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY
    _style_header(ws)
    _autosize(ws)

    r = ws.max_row + 2
    ws.cell(row=r, column=1,
            value="Each series minus its own mean, divided by its own standard "
                  "deviation. Use this chart to compare shape and timing across "
                  "different units; use the raw chart for magnitudes.").font = NOTE
    return ws


SERIES_COLORS = ["2A78D6", "EB6834", "1BAF7A", "EDA100",
                 "E87BA4", "4A3AA7", "E34948", "008300"]

GRID = "D9D9D9"
AXIS = "808080"
EXCEL_EPOCH = datetime(1899, 12, 30)

# Nice tick spacings in days, smallest first.
TICK_STEPS = [1 / 24, 2 / 24, 3 / 24, 6 / 24, 12 / 24, 1, 2, 3, 5, 7, 14, 30]


def _serial(dt) -> float:
    """Excel date serial for a naive datetime."""
    return (dt - EXCEL_EPOCH).total_seconds() / 86400.0


def _tick_step(span_days: float, target: int = 11) -> float:
    ideal = span_days / max(target, 1)
    for step in TICK_STEPS:
        if step >= ideal:
            return step
    return TICK_STEPS[-1]


def _text_props(size=900, rotation=None, bold=False):
    body = RichTextProperties(vert="horz", anchor="ctr", anchorCtr=True)
    if rotation is not None:
        body.rot = rotation
    chars = CharacterProperties(sz=size, b=bold,
                                latin=Font_(typeface="Arial"))
    return RichText(bodyPr=body,
                    p=[Paragraph(pPr=ParagraphProperties(defRPr=chars),
                                 endParaRPr=chars)])


def _set_axis_title(axis, text, rotation=None, pin_left=False,
                    chart_h=None):
    """
    Set an axis title and force its rotation.

    Excel auto-places axis titles, and in a narrow chart it will happily drop a
    rotated title on top of the tick labels. `pin_left` overrides that with an
    explicit layout: hard against the left edge, vertically centred on the plot
    band, leaving the tick labels the space to its right.
    """
    axis.title = text
    if pin_left:
        px = (chart_h or CHART_H) / 2.54 * 96
        h_frac = min(0.6, (len(str(text)) * 5.5 + 10) / px)
        axis.title.layout = Layout(manualLayout=ManualLayout(
            xMode="edge", yMode="edge",
            x=0.0, y=round(PLOT_Y + PLOT_H / 2 - h_frac / 2, 4)))
    rich = axis.title.tx.rich
    body = RichTextProperties(vert="horz", anchor="ctr", anchorCtr=True)
    if rotation is not None:
        body.rot = rotation
    rich.bodyPr = body
    chars = CharacterProperties(sz=1000, b=False, latin=Font_(typeface="Arial"))
    for para in rich.p:
        para.pPr = ParagraphProperties(defRPr=chars)
        for run in (para.r or []):
            run.rPr = chars
    axis.title.overlay = False


def _style_common(ch):
    ch.style = None
    ch.dispBlanksAs = "gap"
    for ax in (ch.x_axis, ch.y_axis):
        ax.delete = False
        ax.majorTickMark = "out"
        ax.minorTickMark = "none"
        # "autoZero" would draw each axis where the other hits zero, which
        # lands mid-plot the moment a series goes negative (z-scores). Pin
        # both to the low end so the frame stays around the data.
        ax.crosses = "min"
        ax.tickLblPos = "low"
        ax.spPr = GraphicalProperties(
            ln=LineProperties(solidFill=AXIS, w=9525))
        ax.txPr = _text_props(900)
    gl = ChartLines()
    gl.spPr = GraphicalProperties(ln=LineProperties(solidFill=GRID, w=9525))
    ch.y_axis.majorGridlines = gl
    ch.x_axis.majorGridlines = None
    if ch.legend is not None:
        ch.legend.position = "b"
        ch.legend.overlay = False
        ch.legend.txPr = _text_props(900)


def _style_time_axis(ch, index, title="time (local)", target_ticks=11):
    """A real numeric axis carrying date serials -- no category collapsing."""
    lo = _serial(index[0].tz_convert(LOCAL_TZ).replace(tzinfo=None))
    hi = _serial(index[-1].tz_convert(LOCAL_TZ).replace(tzinfo=None))
    step = _tick_step(hi - lo, target_ticks)
    ch.x_axis.scaling.min = round(lo, 6)
    ch.x_axis.scaling.max = round(hi, 6)
    ch.x_axis.majorUnit = step
    ch.x_axis.number_format = "mm-dd hh:mm" if step < 1 else "mm-dd"
    ch.x_axis.txPr = _text_props(900, rotation=-2700000)
    _set_axis_title(ch.x_axis, title)


def _style_value_axis(ch, frame, cols, title, pin_left=False):
    vals = frame[cols].to_numpy(dtype="float64")
    vals = vals[~np.isnan(vals)]
    if vals.size:
        lo, hi = float(vals.min()), float(vals.max())
        if hi == lo:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.08
        ch.y_axis.scaling.min = round(lo - pad, 2)
        ch.y_axis.scaling.max = round(hi + pad, 2)
    if title:
        _set_axis_title(ch.y_axis, title, rotation=-5400000,
                        pin_left=pin_left)


def _add_line_series(ch, ws, col_ix, nrows, smooth=False):
    """One straight-line series per column, no markers.

    `col_ix` is the list of sheet column indices to plot, in legend order. It
    is a list rather than a start-plus-count because a chart now carries one
    unit group, and a group is an arbitrary subset of the sheet's columns --
    salinity can sit between two temperatures.
    """
    xref = Reference(ws, min_col=1, min_row=2, max_row=nrows + 1)
    for i, c in enumerate(col_ix):
        yref = Reference(ws, min_col=c, min_row=1, max_row=nrows + 1)
        ser = Series(yref, xref, title_from_data=True)
        ser.smooth = smooth
        ser.marker = Marker(symbol="none")
        ser.graphicalProperties = GraphicalProperties(
            ln=LineProperties(solidFill=SERIES_COLORS[i % len(SERIES_COLORS)],
                              w=17000, cap="rnd"))
        ch.series.append(ser)


# Horizontal scale. Width grows linearly with the number of plotted points, so
# the gap between adjacent points is constant no matter what interval you pick:
# a 30-minute build over a given range comes out four times wider than a 2-hour
# one. MIN_W only kicks in for very short records, MAX_W keeps Excel from
# choking on an absurd object.
CM_PER_POINT = 0.15
MIN_W, MAX_W = 30.0, 800.0

AXIS_COL_W = 15.0                    # frozen column -- wide enough for the
                                     # rotated title AND the tick labels beside
                                     # it, with a margin so neither collides
SWATCH_W = 2.2                       # legend swatch columns
DEFAULT_COL_PX = 64                  # an untouched column, in pixels
CHAR_PX = 5.4                        # 9pt Arial, near enough for layout
CHART_H = 12.0                       # short enough to fit a laptop window
PLOT_Y, PLOT_H = 0.04, 0.76          # shared plot band, so the two charts align
ROW_PT = 15.0                        # Excel's default row height, in points
ROW_CM = ROW_PT / 72 * 2.54          # 0.529 cm -- not 0.4
LEGEND_ROW_PT = 30.0                 # uniform, so every swatch is the same size
CHART_TOP_ROW = 4                    # title on row 1, subtitle on row 2


def _cols_to_cm(*widths) -> float:
    """Excel column width (characters) -> centimetres."""
    px = sum(round(w * 7) + 5 for w in widths)
    return round(px / 96 * 2.54, 3)


AXIS_W = _cols_to_cm(AXIS_COL_W)


def _plot_width(n_points: int) -> float:
    return round(min(max(n_points * CM_PER_POINT, MIN_W), MAX_W), 1)


def _layout(x, w):
    return Layout(manualLayout=ManualLayout(
        xMode="edge", yMode="edge", x=x, y=PLOT_Y, w=w, h=PLOT_H))


def _chrome(ch):
    """Square corners, no chart-area border, no fill -- sits flat on the sheet."""
    ch.roundedCorners = False
    ch.graphical_properties = GraphicalProperties(
        noFill=True, ln=LineProperties(noFill=True))
    ch.plot_area.graphicalProperties = GraphicalProperties(
        noFill=True, ln=LineProperties(noFill=True))
    return ch


def _axis_chart(source_ws, col_ix, nrows, y_title, frame, y_cols, x_lo):
    """
    The y-axis only. Sits in the frozen columns so it stays put while the wide
    plot scrolls. The series are parked off to the side of the data so nothing
    draws; they exist because a chart with no series renders no axis.
    """
    ch = ScatterChart()
    ch.title = None
    ch.legend = None
    ch.height, ch.width = CHART_H, AXIS_W
    _add_line_series(ch, source_ws, col_ix, nrows)
    _style_common(ch)
    _style_value_axis(ch, frame, y_cols, y_title, pin_left=True)
    ch.x_axis.delete = True
    ch.x_axis.scaling.min = round(x_lo - 10, 6)
    ch.x_axis.scaling.max = round(x_lo - 9.99, 6)
    ch.layout = _layout(0.84, 0.15)
    return _chrome(ch)


def _plot_chart(source_ws, col_ix, nrows, index, frame, y_cols, width):
    """The wide, scrolling half. No title, no legend, no y-axis labels."""
    ch = ScatterChart()
    ch.title = None
    ch.height, ch.width = CHART_H, width
    _add_line_series(ch, source_ws, col_ix, nrows)
    _style_common(ch)
    ch.legend = None
    _style_value_axis(ch, frame, y_cols, None)
    _style_time_axis(ch, index, target_ticks=max(6, int(width / 7)))
    # openpyxl's NoneSet maps the string "none" to None and then omits the
    # attribute, so tickLblPos can't suppress these -- hide the whole axis
    # instead. The valAx element is still written, so its gridlines survive.
    ch.y_axis.delete = True
    ch.y_axis.title = None
    ch.layout = _layout(0.004, 0.992)
    return _chrome(ch)


def _legend_label(col, unit, mixed, geometry=""):
    """
    Trim the label to what identifies the series, then state WHERE IT SITS.

    Depth and reference frame are not decoration. A reader comparing
    `LJAC1 wtmp` with `46254 SST` has no way to know from the names that one is
    bolted to a pier piling 3.4 m down and the other rides the sea surface on a
    0.9 m sphere -- and that difference is most of the explanation for why they
    disagree. So every legend entry carries `(depth, frame)`.
    """
    name = re.sub(r"\s*\([^)]*\)", "", col)          # (degree_Celsius)
    name = re.sub(r"[,\s]*\u00b0\s*[FC]\b", "", name)    # ", degF"
    name = re.sub(r"(^|\.)src_", r"\1", name)          # src_LJAC1 -> LJAC1
    name = re.sub(r"\s{2,}", " ", name).strip(" ,.")
    if mixed and unit:
        name = f"{name} [{unit}]"
    return f"{name} {geometry}".strip() if geometry else name


def _write_header(ws, title, subtitle, col):
    """Title and subtitle sit beside the frozen axis, so they scroll with it."""
    letter = get_column_letter(col)
    ws[f"{letter}1"] = title
    ws[f"{letter}1"].font = Font(name="Arial", size=12, bold=True)
    ws[f"{letter}2"] = subtitle
    ws[f"{letter}2"].font = Font(name="Arial", size=9, color="808080")
    ws.row_dimensions[1].height = 18
    ws.row_dimensions[2].height = 13


def _cell_legend(ws, cols, units, mixed, row, start_col, geometry=None):
    """
    A conventional horizontal legend, laid out in cells below the plot and to
    the right of the frozen axis, so it scrolls with the chart. Each entry gets
    a narrow swatch column plus however many default-width columns its label
    needs, so nothing wraps and nothing collides.
    """
    ws.row_dimensions[row].height = LEGEND_ROW_PT
    col = start_col
    for i, c in enumerate(cols):
        label = _legend_label(c, units.get(c, ""), mixed,
                              (geometry or {}).get(c, ""))
        ws.column_dimensions[get_column_letter(col)].width = SWATCH_W
        sw = ws.cell(row=row, column=col)
        sw.fill = PatternFill("solid",
                              fgColor=SERIES_COLORS[i % len(SERIES_COLORS)])

        cell = ws.cell(row=row, column=col + 1, value=label)
        cell.font = Font(name="Arial", size=9)
        cell.alignment = Alignment(vertical="center", indent=1)

        span = max(1, int(np.ceil((len(label) * CHAR_PX + 14) / DEFAULT_COL_PX)))
        col += 1 + span + 1          # swatch + label + a gap


# A panel is a title row, the chart, a legend row, then a gap before the next.
CHART_ROWS = int(np.ceil(CHART_H / ROW_CM))
PANEL_GAP_ROWS = 3

# What to call a unit on an axis when the bare symbol would not read as one.
UNIT_AXIS_LABELS = {
    "": "unit not detected",
    "1": "dimensionless",
    "qc_flag": "QARTOD flag",
}


def _unit_groups(result, cols):
    """[(axis label, [column, ...]), ...] -- one entry per data type.

    Series share a y-axis when, and only when, they share a unit. Two scales
    on one frame make any two series look however you want, so the grouping
    key is the CANONICAL unit: `m s-1` and `m/s` are one group, because they
    are one unit spelled two ways by two feeds.

    Derived columns are kept apart even when the unit matches. The
    stratification index is degC, but it is a temperature DIFFERENCE, not a
    temperature, and putting it on the temperature axis is the same mistake as
    a second axis -- one frame, two quantities.

    Order follows first appearance in `cols`, so the panels come out in the
    order the series were selected.
    """
    derived = set(getattr(result, "derived", []) or [])
    groups: dict[tuple, list] = {}
    for c in cols:
        info = (getattr(result, "columns", {}) or {}).get(c)
        unit = sk.canonical_unit(result.units.get(c, ""),
                                 getattr(info, "column", None) or c)
        groups.setdefault((c in derived, unit), []).append(c)
    out = []
    for (is_derived, unit), gcols in groups.items():
        label = UNIT_AXIS_LABELS.get(unit, unit)
        out.append((f"{label} (derived)" if is_derived else label, gcols))
    return out


def _panel_title(ws, row, label, gcols, col):
    letter = get_column_letter(col)
    ws[f"{letter}{row}"] = (f"{label}  \u00b7  {len(gcols)} "
                            f"series" if len(gcols) != 1 else f"{label}  \u00b7  1 series")
    ws[f"{letter}{row}"].font = Font(name="Arial", size=10, bold=True)
    ws.row_dimensions[row].height = 15


def _write_chart_sheets(wb, result, cols, data_ws, norm_ws):
    """chart_raw stacks one panel per data type; chart_zscore stays combined."""
    n = len(result.data)
    width = _plot_width(n)
    units = sorted({result.units.get(c, "") for c in cols if result.units.get(c)})
    mixed = len(units) > 1
    x_lo = _serial(result.data.index[0].tz_convert(LOCAL_TZ).replace(tzinfo=None))
    lo = result.data.index[0].tz_convert(LOCAL_TZ)
    hi = result.data.index[-1].tz_convert(LOCAL_TZ)
    legend_row = CHART_TOP_ROW + CHART_ROWS + 1
    geometry = getattr(result, "geometry", None)
    made = []

    # Sheet column of each series: the data sheet has two time columns before
    # the data, the z-score sheet only one.
    raw_ix = {c: 3 + i for i, c in enumerate(cols)}
    z_ix = {c: 2 + i for i, c in enumerate(cols)}

    groups = _unit_groups(result, cols)

    subtitle = (f"{result.interval} {result.aggregation} \u00b7 "
                f"{lo:%Y-%m-%d %H:%M} to {hi:%Y-%m-%d %H:%M} local \u00b7 "
                f"{n:,} intervals \u00b7 {len(cols)} series \u00b7 "
                f"{len(groups)} data type{'s' if len(groups) != 1 else ''}")

    # ---- chart_raw: one stacked panel per data type ------------------------
    # Each panel is scaled to its own group. A single shared axis had to span
    # every unit at once, which is what made it unreadable as soon as a second
    # data type was selected.
    ws = wb.create_sheet("chart_raw")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = AXIS_COL_W
    ws.freeze_panes = "B1"           # column A only -- the axis and nothing else
    _write_header(ws, "Raw values", subtitle, 2)

    row = CHART_TOP_ROW
    for label, gcols in groups:
        idx = [raw_ix[c] for c in gcols]
        _panel_title(ws, row, label, gcols, 2)
        top = row + 1
        ws.add_chart(_axis_chart(data_ws, idx, n, label,
                                 result.data, gcols, x_lo), f"A{top}")
        ws.add_chart(_plot_chart(data_ws, idx, n, result.data.index,
                                 result.data, gcols, width), f"B{top}")
        # `mixed` is False within a panel: every series here shares a unit and
        # the panel title already states it, so repeating it on each entry is
        # noise.
        lrow = top + CHART_ROWS + 1
        _cell_legend(ws, gcols, result.units, False, lrow, 2, geometry)
        row = lrow + PANEL_GAP_ROWS
    made.append("chart_raw")

    # ---- chart_zscore: deliberately NOT split ------------------------------
    # A z-score is unitless by construction, so every series already shares a
    # scale. Splitting it would defeat the only sheet that can compare shape
    # and timing across different units.
    zframe = (result.data - result.data.mean()) / result.data.std(ddof=0)
    ws = wb.create_sheet("chart_zscore")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = AXIS_COL_W
    ws.freeze_panes = "B1"
    _write_header(ws, "Standardised (z-score)", subtitle, 2)
    zidx = [z_ix[c] for c in cols]
    ws.add_chart(_axis_chart(norm_ws, zidx, n, "standard deviations",
                             zframe, cols, x_lo), f"A{CHART_TOP_ROW}")
    ws.add_chart(_plot_chart(norm_ws, zidx, n, result.data.index,
                             zframe, cols, width), f"B{CHART_TOP_ROW}")
    _cell_legend(ws, cols, result.units, mixed, legend_row, 2, geometry)
    made.append("chart_zscore")

    # ---- stratification: its own panel, because it is a different quantity --
    made += _write_stratification_sheet(wb, result, cols, data_ws, subtitle,
                                        legend_row)

    # ---- scatter: square, self-contained ----------------------------------
    if len(cols) >= 2:
        ws = wb.create_sheet("chart_scatter")
        ws.sheet_view.showGridLines = False
        _write_header(ws, f"{cols[0]} vs {cols[1]}", subtitle, 1)
        sc = ScatterChart()
        sc.title = None
        sc.height, sc.width = 15, 15
        ser = Series(Reference(data_ws, min_col=4, min_row=2, max_row=n + 1),
                     Reference(data_ws, min_col=3, min_row=2, max_row=n + 1))
        ser.smooth = False
        ser.marker = Marker(symbol="circle", size=4)
        ser.marker.graphicalProperties = GraphicalProperties(
            solidFill=SERIES_COLORS[0], ln=LineProperties(noFill=True))
        ser.graphicalProperties = GraphicalProperties(ln=LineProperties(noFill=True))
        sc.series.append(ser)
        sc.legend = None
        _style_common(sc)
        _style_value_axis(sc, result.data, [cols[1]],
                          f"{cols[1]} [{result.units.get(cols[1], '')}]")
        xv = result.data[cols[0]].to_numpy(dtype="float64")
        xv = xv[~np.isnan(xv)]
        if xv.size:
            pad = (xv.max() - xv.min()) * 0.08 or 1
            sc.x_axis.scaling.min = round(float(xv.min()) - pad, 2)
            sc.x_axis.scaling.max = round(float(xv.max()) + pad, 2)
        sc.x_axis.number_format = "0.0"
        _set_axis_title(sc.x_axis,
                        f"{cols[0]} [{result.units.get(cols[0], '')}]")
        ws.add_chart(_chrome(sc), f"A{CHART_TOP_ROW}")
        made.append("chart_scatter")

    return made


def _write_stratification_sheet(wb, result, cols, data_ws, subtitle,
                                legend_row):
    """A separate panel for SST(46254) - T(autoss).

    It gets its own sheet rather than a second axis on the raw chart: it is a
    temperature DIFFERENCE, not a temperature, and the no-dual-axis rule exists
    precisely so two different quantities never share one frame. Read it as the
    covariate -- when it is large the column is stratified, and a seabed logger
    and a near-surface sensor have no reason to agree.
    """
    derived = [c for c in getattr(result, "derived", []) if c in cols]
    if not derived:
        return []

    ws = wb.create_sheet("chart_stratification")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = AXIS_COL_W
    ws.freeze_panes = "B1"
    _write_header(ws, "Stratification index", subtitle, 2)

    n = len(result.data)
    width = _plot_width(n)
    x_lo = _serial(result.data.index[0].tz_convert(LOCAL_TZ).replace(tzinfo=None))
    frame = result.data[derived]
    idx = [3 + cols.index(c) for c in derived]

    ws.add_chart(_axis_chart(data_ws, idx, n,
                             "temperature difference [degC]", frame, derived,
                             x_lo), f"A{CHART_TOP_ROW}")
    ws.add_chart(_plot_chart(data_ws, idx, n, result.data.index,
                             frame, derived, width), f"B{CHART_TOP_ROW}")
    _cell_legend(ws, derived, result.units, False, legend_row, 2,
                 getattr(result, "geometry", None))

    r = legend_row + 2
    ws.cell(row=r, column=2, value=(
        "Surface minus 5 m. Positive means the surface is warmer, i.e. a "
        "stratified water column. This is the covariate that says WHEN the "
        "seabed logger and the pier sensors should be expected to track each "
        "other -- not a sensor reading in its own right.")).font = NOTE
    return ["chart_stratification"]


def _write_provenance_sheet(wb, result, root, lag_reference=None, study=None):
    ws = wb.create_sheet("provenance")
    ws.cell(row=1, column=1, value="How this file was made").font = TITLE

    man = (study.manifest if study is not None else {}) or {}
    attached = man.get("source_files") or []
    val = man.get("validation") or {}

    rows = [
        ("generated (local)", datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")),
    ]
    if study is not None:
        rows += [
            ("study id", study.study_id),
            ("study label", study.label),
            ("study created (UTC)", study.created_utc),
            ("study status", study.status),
            ("study folder", str(Path(study.path).resolve())),
            ("pull window (UTC)",
             f"{(man.get('window_utc') or {}).get('start', '-')} to "
             f"{(man.get('window_utc') or {}).get('end', '-')}"
             f"  ({(man.get('window_utc') or {}).get('window_days', '-')} days)"),
            ("ingest sources",
             ", ".join((man.get("ingest") or {}).get("sources", [])) or "-"),
            ("tool version", man.get("tool_version", "-")),
            ("QC policy", (val.get("qc_policy")
                           or man.get("qc_policy") or "-")),
        ]
        rows += [("attached files",
                  f"{len(attached)} file(s)" if attached else "none")]
        for rec in attached:
            sha = (rec.get("sha256") or "-")[:16]
            rows.append((f"  {rec.get('file', '?')}",
                         f"station={rec.get('station', '?')}  "
                         f"rows={rec.get('n_rows', 0):,}  sha256={sha}..."))
            rows.append((f"    from", rec.get("original_path", "-")))
            for sh in rec.get("sheets", []):
                # State the time basis explicitly. For an attached file the
                # header is the only evidence there is, and for an ambiguous
                # one an assumption was made -- say which.
                basis = sh.get("time_kind", "?")
                if sh.get("assumed_timezone"):
                    basis += f" -> {sh['assumed_timezone']}"
                if sh.get("time_kind") == "ambiguous":
                    basis += "  (ASSUMED: the column named no zone)"
                rows.append((f"    sheet {sh.get('sheet', '?')}",
                             f"time={sh.get('time_column', '?')}  {basis}"))
    else:
        rows += [("source folder", str((root / "sources").resolve()))]

    rows += [
        ("interval", result.interval),
        ("aggregation", result.aggregation),
        ("overlap rule", result.overlap),
        ("min samples per bin", result.min_samples),
        ("rows written", len(result.data)),
        ("window start (local)",
         result.data.index[0].tz_convert(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
         if len(result.data) else "-"),
        ("window end (local)",
         result.data.index[-1].tz_convert(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
         if len(result.data) else "-"),
        ("lag reference", lag_reference or "-"),
        ("timezone", "America/Los_Angeles via zoneinfo; stored UTC internally"),
        ("series dropped",
         "; ".join(result.dropped) if result.dropped else "none"),
    ]
    r = 3
    for k, v in rows:
        ws.cell(row=r, column=1, value=k).font = HEAD
        ws.cell(row=r, column=2, value=v).font = BODY
        r += 1

    # ---- clock check verdicts ---------------------------------------------
    checks = val.get("clock_checks") or []
    if checks:
        r += 1
        ws.cell(row=r, column=1, value="Clock verification").font = TITLE
        r += 1
        for j, h in enumerate(["signal", "role", "observed peak (UTC h)",
                               "expected (UTC h)", "offset (h)", "amplitude",
                               "verdict"], start=1):
            ws.cell(row=r, column=j, value=h)
        _style_header(ws, row=r, ncols=7)
        ws.freeze_panes = None
        for c in checks:
            r += 1
            ws.cell(row=r, column=1, value=c.get("signal", "-")).font = BODY
            ws.cell(row=r, column=2, value=c.get("role", "-")).font = BODY
            ws.cell(row=r, column=3, value=c.get("observed_peak_hour_utc")).font = BODY
            ws.cell(row=r, column=4, value=c.get("expected_peak_hour_utc")).font = BODY
            ws.cell(row=r, column=5, value=c.get("offset_hours")).font = BODY
            ws.cell(row=r, column=6, value=c.get("amplitude")).font = BODY
            cell = ws.cell(row=r, column=7, value="OK" if c.get("ok") else "FAIL")
            cell.font = BODY
            if not c.get("ok"):
                cell.fill = WARNFILL
        r += 2
        ws.cell(row=r, column=1, value=(
            "A timestamp column is only UTC if a signal with a known solar phase "
            "says so. Air temperature peaks ~2 h after local solar noon; the S2 "
            "pressure tide peaks ~10:00 and ~22:00 local solar. See "
            "ingest/clockcheck.py.")).font = NOTE
        r += 1

    # ---- per-series detail -------------------------------------------------
    r += 1
    ws.cell(row=r, column=1, value="Series").font = TITLE
    r += 1
    heads = ["output column", "unit", "depth / frame", "time basis",
             "QARTOD-3 kept", "source", "native cadence"]
    for j, h in enumerate(heads, start=1):
        ws.cell(row=r, column=j, value=h)
    _style_header(ws, row=r, ncols=len(heads))
    ws.freeze_panes = None

    suspect = {}
    for s in (man.get("series") or []):
        suspect[f"{s.get('station')}.{s.get('variable')}"] = s.get("n_flagged_suspect")

    for c in result.data.columns:
        r += 1
        ws.cell(row=r, column=1, value=c).font = BODY
        ws.cell(row=r, column=2, value=result.units.get(c, "")).font = BODY
        ws.cell(row=r, column=3,
                value=getattr(result, "geometry", {}).get(c, "")).font = BODY
        basis = getattr(result, "time_trust", {}).get(c, "")
        cell = ws.cell(row=r, column=4, value=basis)
        cell.font = BODY
        if "unverified" in basis.lower():
            cell.fill = WARNFILL
        ws.cell(row=r, column=5,
                value=suspect.get(c.split(" ")[0], "")).font = BODY
        ws.cell(row=r, column=6, value=result.sources.get(c, "")).font = BODY
        ws.cell(row=r, column=7, value=result.cadences.get(c, "")).font = BODY

    r += 2
    for line in [
        "Assumptions applied on load:",
        "  - A column named 'time_utc' is UTC, verified by ingest/clockcheck.py",
        "    against the solar phase of air temperature and barometric pressure.",
        "  - A legacy column headed 'time (UTC)' is loaded but marked UNVERIFIED",
        "    and shaded above. In this study's original workbook that column",
        "    held Pacific local time, not UTC -- a column name is not evidence of",
        "    a zone. No offset is applied to it here; guessing one is what caused",
        "    the original error.",
        "  - Columns headed 'Date-Time (PDT)' are logger-local wall time and are",
        "    converted via zoneinfo, never by adding a constant.",
        "  - Fahrenheit columns are converted to Celsius; the original unit is",
        "    recorded above.",
        "  - Rows whose timestamp is ambiguous or nonexistent across a DST",
        "    transition are dropped rather than guessed.",
        "  - QARTOD flags 4 (fail) and 9 (missing) are rejected; 1, 2 and 3 pass,",
        "    and the count of suspect (3) values KEPT is listed per series above.",
        "  - BinKey* columns in old snapshots are ignored; binning is done here,",
        "    at the interval named above.",
    ]:
        ws.cell(row=r, column=1, value=line).font = NOTE
        r += 1

    _autosize(ws, maxw=60)
    return ws


def write_workbook(result, root: Path, out_path: Path,
                   lag_table=None, lag_reference=None, study=None) -> Path:
    wb = Workbook()
    wb.remove(wb.active)

    data_ws, cols = _write_data_sheet(wb, result)
    norm_ws = _write_zscore_sheet(wb, result, cols)
    _write_counts_sheet(wb, result, cols)
    _write_stats_sheet(wb, result, cols, lag_table, lag_reference)
    charts = _write_chart_sheets(wb, result, cols, data_ws, norm_ws)
    _write_provenance_sheet(wb, result, root, lag_reference, study)

    order = ([wb[c] for c in charts]
             + [wb["data"], wb["stats"], wb["counts"],
                wb["normalized"], wb["provenance"]])
    wb._sheets = order
    wb.active = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def default_output_name(cols, interval) -> str:
    stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M")
    def slug(c):
        return re.sub(r"[^A-Za-z0-9]+", "", c.split(".")[-1])[:12] or "series"
    short = "_".join(slug(c) for c in cols[:3])
    more = f"_plus{len(cols) - 3}" if len(cols) > 3 else ""
    return f"compare_{short}{more}_{interval}_{stamp}.xlsx"
