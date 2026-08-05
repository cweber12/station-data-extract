"""
sensorkit.py -- data layer for the La Jolla sensor comparison tool.

No GUI code here on purpose: everything in this module is importable and
testable from a plain Python prompt. compare.py is a thin Tkinter shell on top.

Design rules baked in:
  * sources/ is read-only input. outputs/ is never scanned. There is no path
    by which a generated file becomes an input.
  * Every timestamp is converted to UTC on load. Local time is derived for
    display only, via zoneinfo -- no hand-rolled DST arithmetic.
  * Every temperature is converted to degC on load. The original unit is
    recorded and carried through to the provenance sheet.
  * Resampling happens at query time. Nothing is pre-binned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from openpyxl import load_workbook

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

SOURCES_DIRNAME = "sources"
OUTPUTS_DIRNAME = "outputs"

# Time columns, in the order we prefer them. A table offering more than one
# gets the first match. UTC always wins so we never have to trust a local
# label -- see the audit note about `time (PDT)` vs `time (local)`.
TIME_PREFERENCE = [
    (re.compile(r"time\s*\(utc\)", re.I), "UTC"),
    (re.compile(r"\butc\b", re.I), "UTC"),
    (re.compile(r"time\s*\(local\)", re.I), "LOCAL"),
    (re.compile(r"time\s*\(pdt\)", re.I), "LOCAL"),
    (re.compile(r"date-?\s*time", re.I), "LOCAL"),
    (re.compile(r"^time$", re.I), "LOCAL"),
]

# Unit sniffing from column headers. First match wins.
UNIT_PATTERNS = [
    (re.compile(r"°\s*F|\bdeg(ree)?[_ ]?F\b", re.I), "degF"),
    (re.compile(r"°\s*C|\bdeg(ree)?[_ ]?C(elsius)?\b", re.I), "degC"),
    (re.compile(r"\(m\s*s-1\)", re.I), "m/s"),
    (re.compile(r"\(hPa\)", re.I), "hPa"),
    (re.compile(r"\(NTU\)", re.I), "NTU"),
    (re.compile(r"\(mg\.L-1\)", re.I), "mg/L"),
    (re.compile(r"\(microg\.L-1\)", re.I), "ug/L"),
    (re.compile(r"degrees_true", re.I), "deg"),
    (re.compile(r"\(1e-3\)", re.I), "PSU"),
    (re.compile(r"\(m\)", re.I), "m"),
    (re.compile(r"\(s\)", re.I), "s"),
    (re.compile(r"water\s*level", re.I), "ft"),
    (re.compile(r"_qc_agg$", re.I), "qc_flag"),
]

# The QC-screened `*_ok` columns carry no unit in their header, so map them
# explicitly. Add a line here when a new derived column appears.
UNIT_OVERRIDES = {
    "wtmp_ok": "degC", "sal_ok": "PSU", "ph_ok": "pH",
    "do_ok": "mg/L", "chl_ok": "ug/L", "turb_ok": "NTU",
}

# Columns that are bookkeeping, not data. BinKey* are the precomputed bin
# columns the audit flagged -- resampling replaces them entirely. `station`
# is numeric for 46254 and text elsewhere, and is an identifier either way.
NOISE_COLUMNS = re.compile(r"^(Unnamed:|#$|BinKey\d+$|station$)", re.I)

AGGREGATIONS = {
    "mean": "mean",
    "median": "median",
    "min": "min",
    "max": "max",
    "std": "std",
    "count": "count",
}

INTERVALS = [
    "5min", "10min", "15min", "20min", "30min",
    "1h", "2h", "3h", "6h", "12h", "1D",
]


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

@dataclass
class ColumnInfo:
    file: str
    table: str
    column: str
    kind: str            # "time" | "data"
    unit: str            # detected unit, or "" if unknown
    n_nonnull: int
    n_rows: int

    @property
    def key(self) -> str:
        return f"{self.file}::{self.table}::{self.column}"

    @property
    def label(self) -> str:
        """Short name used as the output column header."""
        return f"{self.table}.{self.column}"

    @property
    def coverage(self) -> float:
        return 0.0 if not self.n_rows else self.n_nonnull / self.n_rows


@dataclass
class TableInfo:
    file: str
    name: str
    sheet: str
    n_rows: int
    time_column: str | None
    time_basis: str | None          # "UTC" or "LOCAL"
    columns: list[ColumnInfo] = field(default_factory=list)

    @property
    def data_columns(self) -> list[ColumnInfo]:
        return [c for c in self.columns if c.kind == "data"]


def detect_unit(header: str) -> str:
    key = header.strip()
    if key in UNIT_OVERRIDES:
        return UNIT_OVERRIDES[key]
    for pat, unit in UNIT_PATTERNS:
        if pat.search(header):
            return unit
    return ""


def detect_time_column(headers) -> tuple[str | None, str | None]:
    for pat, basis in TIME_PREFERENCE:
        for h in headers:
            if isinstance(h, str) and pat.search(h):
                return h, basis
    return None, None


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    keep = [c for c in df.columns
            if isinstance(c, str) and not NOISE_COLUMNS.match(c.strip())]
    df = df[keep]
    return df.dropna(how="all")


def _read_table(path: Path, sheet: str, ref: str | None) -> pd.DataFrame:
    """Read one sheet, or one table range within a sheet."""
    if ref is None:
        df = pd.read_excel(path, sheet_name=sheet)
    else:
        m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", ref)
        first_row = int(m.group(2))
        df = pd.read_excel(
            path, sheet_name=sheet,
            skiprows=first_row - 1,
            usecols=f"{m.group(1)}:{m.group(3)}",
        )
    return _clean_frame(df)


def scan_workbook(path: Path) -> list[TableInfo]:
    """Catalogue every table (or bare sheet) in a workbook."""
    wb = load_workbook(path, read_only=True)
    tables: list[TableInfo] = []

    for ws in wb.worksheets:
        refs = {}
        try:
            for tname in ws.tables:
                obj = ws.tables[tname]
                refs[tname] = obj.ref if hasattr(obj, "ref") else obj
        except Exception:
            refs = {}

        targets = list(refs.items()) if refs else [(ws.title, None)]

        for tname, ref in targets:
            try:
                df = _read_table(path, ws.title, ref)
            except Exception:
                continue
            if df.empty or len(df.columns) < 2:
                continue

            tcol, basis = detect_time_column(df.columns)
            if tcol is None:
                continue  # nothing to align on; not comparable

            info = TableInfo(
                file=path.name, name=tname, sheet=ws.title,
                n_rows=len(df), time_column=tcol, time_basis=basis,
            )
            for col in df.columns:
                if col == tcol:
                    continue
                s = df[col]
                if not pd.api.types.is_numeric_dtype(s):
                    continue
                info.columns.append(ColumnInfo(
                    file=path.name, table=tname, column=str(col),
                    kind="data", unit=detect_unit(str(col)),
                    n_nonnull=int(s.notna().sum()), n_rows=len(df),
                ))
            if info.columns:
                tables.append(info)

    wb.close()
    return tables


def build_catalog(root: Path) -> dict[str, list[TableInfo]]:
    """Scan sources/ only. outputs/ is deliberately never touched."""
    src = root / SOURCES_DIRNAME
    if not src.is_dir():
        raise FileNotFoundError(f"no {SOURCES_DIRNAME}/ directory under {root}")

    catalog: dict[str, list[TableInfo]] = {}
    for p in sorted(src.glob("*.xlsx")):
        if p.name.startswith("~$"):
            continue
        tabs = scan_workbook(p)
        if tabs:
            catalog[p.name] = tabs
    return catalog


# --------------------------------------------------------------------------
# loading and normalisation
# --------------------------------------------------------------------------

def to_utc(series: pd.Series, basis: str) -> pd.DatetimeIndex:
    idx = pd.to_datetime(series, errors="coerce")
    if basis == "UTC":
        return pd.DatetimeIndex(idx).tz_localize("UTC")
    # Wall-clock local. ambiguous/nonexistent land on NaT and get dropped,
    # which is the honest answer for the two hours a year that are undefined.
    return pd.DatetimeIndex(idx).tz_localize(
        LOCAL_TZ, ambiguous="NaT", nonexistent="NaT"
    ).tz_convert("UTC")


def convert_units(values: pd.Series, unit: str) -> tuple[pd.Series, str]:
    if unit == "degF":
        return (values - 32.0) * 5.0 / 9.0, "degC"
    return values, unit


_frame_cache: dict[tuple[str, str, str], pd.DataFrame] = {}


def load_series(root: Path, table: TableInfo, column: ColumnInfo,
                convert: bool = True) -> tuple[pd.Series, str]:
    """Return one column as a UTC-indexed Series, plus its final unit."""
    path = root / SOURCES_DIRNAME / table.file
    ck = (table.file, table.sheet, table.name)
    if ck not in _frame_cache:
        wb = load_workbook(path, read_only=True)
        ws = wb[table.sheet]
        ref = None
        try:
            if table.name in ws.tables:
                obj = ws.tables[table.name]
                ref = obj.ref if hasattr(obj, "ref") else obj
        except Exception:
            ref = None
        wb.close()
        _frame_cache[ck] = _read_table(path, table.sheet, ref)

    df = _frame_cache[ck]
    idx = to_utc(df[table.time_column], table.time_basis)
    s = pd.Series(pd.to_numeric(df[column.column], errors="coerce").values,
                  index=idx, name=column.label)
    s = s[s.index.notna()].sort_index()
    s = s[~s.index.duplicated(keep="first")]

    unit = column.unit
    if convert:
        s, unit = convert_units(s, unit)
    return s.dropna(), unit


def native_cadence(s: pd.Series) -> pd.Timedelta | None:
    if len(s) < 3:
        return None
    d = pd.Series(s.index).diff().dropna()
    return d.mode().iloc[0] if len(d) else None


# --------------------------------------------------------------------------
# alignment
# --------------------------------------------------------------------------

@dataclass
class BuildResult:
    data: pd.DataFrame          # UTC-indexed, one column per series
    counts: pd.DataFrame        # samples that went into each cell
    units: dict[str, str]
    cadences: dict[str, str]
    sources: dict[str, str]
    interval: str
    aggregation: str
    overlap: str
    min_samples: int
    dropped: list[str] = field(default_factory=list)


def build_comparison(root: Path, selections, interval="1h", aggregation="mean",
                     overlap="intersection", min_samples=1,
                     start=None, end=None, convert_units_flag=True) -> BuildResult:
    """
    selections: list of (TableInfo, ColumnInfo)
    overlap:    "intersection" -> only bins where every series has data
                "union"        -> keep every bin, leave gaps blank
    """
    if not selections:
        raise ValueError("nothing selected")

    frames, counts, units, cadences, sources = {}, {}, {}, {}, {}
    dropped: list[str] = []

    for table, col in selections:
        s, unit = load_series(root, table, col, convert=convert_units_flag)
        if s.empty:
            # Never drop a requested series silently -- LJPC1's temperature
            # columns exist but are entirely null, and a quiet skip is how
            # that becomes an invisible hole in someone's analysis.
            dropped.append(f"{col.label} (no usable values in the source)")
            continue
        label = col.label
        # de-duplicate labels across workbooks
        if label in frames:
            label = f"{table.file.rsplit('.', 1)[0]}.{label}"

        cad = native_cadence(s)
        cadences[label] = str(cad) if cad is not None else "irregular"
        units[label] = unit
        sources[label] = f"{table.file} :: {table.name} :: {col.column}"

        r = s.resample(interval)
        frames[label] = getattr(r, AGGREGATIONS[aggregation])()
        counts[label] = r.count()

    if not frames:
        raise ValueError("every selected series was empty after loading")

    data = pd.concat(frames, axis=1)
    cnt = pd.concat(counts, axis=1).reindex(data.index).fillna(0).astype(int)

    # A bin backed by too few raw samples is worse than no bin at all.
    if min_samples > 1:
        data = data.where(cnt >= min_samples)

    if overlap == "intersection":
        keep = data.notna().all(axis=1)
        data, cnt = data[keep], cnt[keep]
    else:
        keep = data.notna().any(axis=1)
        data, cnt = data[keep], cnt[keep]

    if start is not None:
        data, cnt = data[data.index >= start], cnt[cnt.index >= start]
    if end is not None:
        data, cnt = data[data.index <= end], cnt[cnt.index <= end]

    return BuildResult(data, cnt, units, cadences, sources,
                       interval, aggregation, overlap, min_samples, dropped)


def lag_scan(data: pd.DataFrame, reference: str, interval: str,
             max_hours: float = 24.0) -> pd.DataFrame:
    """
    Cross-correlate every series against `reference` across a range of lags.

    Note for this dataset: the dominant period in La Jolla nearshore
    temperature is the ~12.4 h internal tide, so a peak at lag L is
    indistinguishable from one at L +/- 12.4 h. The `ambiguous_alt` column
    spells that alternative out rather than letting you forget it.
    """
    step = pd.Timedelta(interval).total_seconds() / 3600.0
    if step <= 0:
        raise ValueError("bad interval")
    n = int(max_hours / step)
    rows = []

    for col in data.columns:
        if col == reference:
            continue
        best_lag, best_r = 0.0, np.nan
        series = []
        for k in range(-n, n + 1):
            r = data[col].corr(data[reference].shift(k))
            if pd.notna(r):
                series.append((k * step, r))
                if pd.isna(best_r) or abs(r) > abs(best_r):
                    best_lag, best_r = k * step, r
        rows.append({
            "series": col,
            "r_at_lag_0": data[col].corr(data[reference]),
            # Sign convention: negative means this series LEADS the reference
            # (its features show up earlier); positive means it lags.
            "best_lag_h": best_lag,
            "r_at_best_lag": best_r,
            "ambiguous_alt_h": best_lag - 12.42 if best_lag > 0 else best_lag + 12.42,
            "n_overlapping": int((data[col].notna() & data[reference].notna()).sum()),
        })
    return pd.DataFrame(rows)
