"""
identity.py -- what makes a series recognisably itself, in every artifact.

PURE BY DESIGN. This module imports nothing that touches a file, a window or a
plotting backend. That is the whole point: the workbook exporter and the
interactive view both need to agree on how a series looks, and neither should
have to import the other to get it. If this module ever needs `openpyxl`,
`matplotlib` or `tkinter`, something has been put in the wrong place.

A reader identifies a series by two things: its colour and its label. Both have
to be decided once, in one place. A colour assigned per chart, or a label built
per sheet, turns one series into two as far as the reader is concerned -- and on
a standardised chart, where the y axis carries no units at all, colour is the
ONLY thing left identifying a line.
"""

from __future__ import annotations

import re

SERIES_COLORS = ["2A78D6", "EB6834", "1BAF7A", "EDA100",
                 "E87BA4", "4A3AA7", "E34948", "008300"]


def series_colors(cols) -> dict:
    """One colour per series, fixed by its position in the SELECTION.

    Colour is the only thing tying a line on the raw panel to the same line on
    the z-score sheet, so it cannot be assigned per chart. Once chart_raw split
    into one panel per data type, per-chart assignment gave the first series of
    every panel the same colour and moved series between colours from sheet to
    sheet -- two different lies at once.
    """
    return {c: SERIES_COLORS[i % len(SERIES_COLORS)] for i, c in enumerate(cols)}


def legend_label(col, unit, mixed, geometry="") -> str:
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
