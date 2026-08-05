"""
userfiles.py -- bring a user-supplied Excel or CSV file into the canonical frame.

A study has exactly two kinds of source:

  1. sensor feeds pulled by script      (erddap.py, coops.py, ndbc_realtime.py)
  2. files the user attaches at creation time  <- this module

Nothing is picked up implicitly. A file becomes part of a study because someone
chose it, and a copy of it is stored inside the study so the snapshot stays
self-contained and reproducible.

THE TIME ZONE PROBLEM
---------------------
A feed states its zone in its documentation and we assert it. An arbitrary
spreadsheet does not. So the header is all we have, and the header is exactly
what lied last time -- the workbook's `time (UTC)` column held Pacific local
time. See AGENT_TASK.md 0.1.

This module therefore never guesses silently. It classifies the time column:

  utc_explicit   header says UTC     -> attached as UTC, trust "unverified"
  local_labelled header names a local zone, e.g. `Date-Time (PDT)`
                                     -> local wall time via zoneinfo, trusted
  ambiguous      a bare `time`/`date` column
                                     -> ASSUMED local, trust "assumed", and the
                                        assumption is recorded in the manifest

Whatever it decides is written to the manifest and onto the provenance sheet, so
a reader can see what was assumed rather than having to reverse-engineer it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import CANONICAL_COLUMNS, StationConfig, empty_frame

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
CSV_SUFFIXES = {".csv", ".txt", ".tsv"}
SUPPORTED_SUFFIXES = EXCEL_SUFFIXES | CSV_SUFFIXES

# Time-column classification. First match wins, most specific first.
TIME_RULES = [
    (re.compile(r"^time_utc$", re.I), "utc_explicit", "verified"),
    # `Date-Time (PDT)` before any bare `time (pdt)`: the former is a logger
    # export stating the zone it recorded in, the latter was a computed column.
    (re.compile(r"date[-_ ]?time", re.I), "local_labelled", "local"),
    (re.compile(r"\butc\b|\bgmt\b|\bz\b", re.I), "utc_explicit", "unverified"),
    (re.compile(r"\b(pdt|pst|local|est|edt|cst|cdt|mst|mdt)\b", re.I),
     "local_labelled", "local"),
    (re.compile(r"date|time|timestamp", re.I), "ambiguous", "assumed"),
]

# Canonical variable names from a messy column header. Falls back to a slug.
VARIABLE_HINTS = [
    (re.compile(r"sea[_ ]?water[_ ]?temp|water[_ ]?temp|\bwtmp\b|\bsst\b"
                r"|tidbit|hobo|logger", re.I), "sea_water_temperature"),
    (re.compile(r"air[_ ]?temp|\batmp\b", re.I), "air_temperature"),
    (re.compile(r"salin|\bpsu\b", re.I), "sea_water_practical_salinity"),
    (re.compile(r"pressure|\bbar\b|\bhpa\b|\bmbar\b", re.I),
     "air_pressure_at_mean_sea_level"),
    (re.compile(r"water[_ ]?level|\btide\b", re.I), "water_level"),
    (re.compile(r"chlorophyll|\bchl\b", re.I),
     "mass_concentration_of_chlorophyll_in_sea_water"),
    (re.compile(r"turbid|\bntu\b", re.I), "sea_water_turbidity"),
    (re.compile(r"^ph\b|\bph\b|ph[_ ]?total", re.I),
     "sea_water_ph_reported_on_total_scale"),
    (re.compile(r"oxygen|dissolved[_ ]?o", re.I),
     "mass_concentration_of_oxygen_in_sea_water"),
    (re.compile(r"wind.*(dir|from)|\bwdir\b|\bwd\b", re.I), "wind_from_direction"),
    (re.compile(r"wind.*speed|\bwspd\b", re.I), "wind_speed"),
    (re.compile(r"wave.*height|\bwvht\b", re.I),
     "sea_surface_wave_significant_height"),
    (re.compile(r"depth", re.I), "depth"),
]

UNIT_HINTS = [
    (re.compile(r"°\s*F|\bdeg(ree)?[_ ]?F\b|\bfahrenheit\b", re.I), "degF"),
    (re.compile(r"°\s*C|\bdeg(ree)?[_ ]?C(elsius)?\b", re.I), "degC"),
    (re.compile(r"\bhPa\b|\bmbar\b|\bmillibar\b", re.I), "hPa"),
    (re.compile(r"\bNTU\b", re.I), "NTU"),
    (re.compile(r"mg[./ ]?L", re.I), "mg/L"),
    (re.compile(r"microg|ug[./ ]?L|µg", re.I), "ug/L"),
    (re.compile(r"\bpsu\b|\b1e-3\b", re.I), "1e-3"),
    (re.compile(r"\bm[/ ]?s\b|\bm s-1\b", re.I), "m s-1"),
    (re.compile(r"\bfeet\b|\bft\b", re.I), "ft"),
    (re.compile(r"\bmeters?\b|\(m\)", re.I), "m"),
    (re.compile(r"degrees?[_ ]?true|\bdeg\b", re.I), "degree_true"),
]

# Columns that are never measurements.
SKIP_COLUMNS = re.compile(r"^(#|unnamed:|index$|row$)", re.I)


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_").lower()
    return s or "value"


def canonical_variable(header: str) -> str:
    for pat, name in VARIABLE_HINTS:
        if pat.search(str(header)):
            return name
    return slug(header)


def detect_unit(header: str) -> str:
    """Unit from a column header, trailing bracket first.

    Order matters and first-match-wins over the whole string is wrong: a header
    like `Tidbit 1 , °F [degC]` (an already-converted column) matches the degF
    pattern earlier in the text and would be converted a second time. A unit in
    a trailing [...] or (...) is the authoritative one, so try that first.
    """
    text = str(header)
    tail = re.search(r"[\[(]\s*([^\[\]()]{1,20})\s*[\])]\s*$", text)
    if tail:
        for pat, unit in UNIT_HINTS:
            if pat.search(tail.group(1)):
                return unit
    for pat, unit in UNIT_HINTS:
        if pat.search(text):
            return unit
    return ""


def classify_time_column(headers) -> tuple[str | None, str, str]:
    """-> (column, kind, trust). kind is utc_explicit / local_labelled / ambiguous."""
    for pat, kind, trust in TIME_RULES:
        for h in headers:
            if isinstance(h, str) and pat.search(h):
                return h, kind, trust
    return None, "none", "none"


@dataclass
class SheetProbe:
    sheet: str
    time_column: str | None
    time_kind: str
    time_trust: str
    n_rows: int
    variables: list[dict] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.time_column and self.variables)


@dataclass
class FileProbe:
    path: Path
    station: str
    sheets: list[SheetProbe] = field(default_factory=list)
    error: str = ""

    @property
    def usable(self) -> bool:
        return any(s.usable for s in self.sheets)

    def summary(self) -> str:
        if self.error:
            return f"{self.path.name}: ERROR {self.error}"
        good = [s for s in self.sheets if s.usable]
        if not good:
            return f"{self.path.name}: no usable time + numeric columns found"
        n = sum(len(s.variables) for s in good)
        rows = sum(s.n_rows for s in good)
        kinds = {s.time_trust for s in good}
        return (f"{self.path.name}: {n} series, {rows:,} rows, "
                f"time {'/'.join(sorted(kinds))}")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _read_csv(path: Path) -> dict[str, pd.DataFrame]:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc, sep=None, engine="python")
            return {"csv": df}
        except UnicodeDecodeError:
            continue
        except Exception:
            break
    return {"csv": pd.read_csv(path, encoding="latin-1", engine="python",
                               on_bad_lines="skip")}


def _read_excel(path: Path) -> dict[str, pd.DataFrame]:
    out = {}
    xl = pd.ExcelFile(path)
    for sheet in xl.sheet_names:
        try:
            out[sheet] = xl.parse(sheet)
        except Exception:
            continue
    return out


def _frames(path: Path) -> dict[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return _read_excel(path)
    if suffix in CSV_SUFFIXES:
        return _read_csv(path)
    raise ValueError(f"unsupported file type {suffix!r} "
                     f"(want {sorted(SUPPORTED_SUFFIXES)})")


def default_station(path: Path) -> str:
    return slug(Path(path).stem)


def probe(path: Path, station: str | None = None) -> FileProbe:
    """Look at a file without importing it -- what the UI shows before Create."""
    path = Path(path)
    fp = FileProbe(path=path, station=station or default_station(path))
    try:
        frames = _frames(path)
    except Exception as e:
        fp.error = str(e)
        return fp

    for sheet, df in frames.items():
        if df is None or df.empty:
            continue
        tcol, kind, trust = classify_time_column(df.columns)
        sp = SheetProbe(sheet=sheet, time_column=tcol, time_kind=kind,
                        time_trust=trust, n_rows=len(df))
        if tcol is not None:
            for col in df.columns:
                if col == tcol or not isinstance(col, str):
                    continue
                if SKIP_COLUMNS.match(col.strip()):
                    continue
                s = pd.to_numeric(df[col], errors="coerce")
                if s.notna().sum() == 0:
                    continue
                sp.variables.append({
                    "column": col,
                    "variable": canonical_variable(col),
                    "unit": detect_unit(col),
                    "n_nonnull": int(s.notna().sum()),
                })
        fp.sheets.append(sp)
    return fp


def _to_utc(values, kind: str, tz: ZoneInfo) -> pd.Series:
    """Attach a zone according to the classification. Never guess silently."""
    idx = pd.to_datetime(values, errors="coerce")
    di = pd.DatetimeIndex(idx)
    if di.tz is not None:
        return pd.Series(di.tz_convert("UTC"))
    if kind == "utc_explicit":
        return pd.Series(di.tz_localize("UTC"))
    # local_labelled and ambiguous both mean wall time in `tz`. Ambiguous or
    # nonexistent stamps across a DST change land on NaT and get dropped, which
    # is the honest answer for the two hours a year that are undefined.
    return pd.Series(di.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
                       .tz_convert("UTC"))


def read(path: Path, cfg: StationConfig, *, station: str | None = None,
         fetched_utc: dt.datetime | None = None,
         log=print) -> tuple[pd.DataFrame, dict]:
    """Read one user file into the canonical long frame.

    Returns (frame, record) where `record` documents what was detected and
    assumed, for the study manifest.
    """
    path = Path(path)
    station = station or default_station(path)
    fetched = fetched_utc or dt.datetime.now(dt.timezone.utc)
    tz = ZoneInfo(cfg.defaults.get("timezone_display", "America/Los_Angeles"))

    fp = probe(path, station)
    record = {
        "file": path.name,
        "station": station,
        "error": fp.error,
        "sheets": [],
        "n_rows": 0,
        "assumed_local_time": False,
    }
    if fp.error:
        log(f"  {path.name}: ERROR {fp.error}")
        return empty_frame(), record

    # Geometry, if this station happens to be configured. Otherwise unknown --
    # never invented.
    st = cfg.stations.get(station)
    depth = st.depth_m if st else None
    frame_of_ref = st.reference_frame if st else "unknown"

    frames = _frames(path)
    out = []
    for sp in fp.sheets:
        if not sp.usable:
            continue
        df = frames[sp.sheet]
        time_utc = _to_utc(df[sp.time_column], sp.time_kind, tz)
        if sp.time_kind == "ambiguous":
            record["assumed_local_time"] = True
            log(f"  {path.name}[{sp.sheet}]: time column {sp.time_column!r} "
                f"names no zone -- ASSUMING {tz.key}. Recorded in the manifest.")

        for v in sp.variables:
            values = pd.to_numeric(df[v["column"]], errors="coerce")
            unit = v["unit"]
            if unit == "degF":                 # canonical degC, converted once
                values, unit = (values - 32.0) * 5.0 / 9.0, "degC"
            rows = pd.DataFrame({
                "time_utc": time_utc,
                "station": station,
                "variable": v["variable"],
                "value": values.astype("float64"),
                "unit": unit,
                "qc_flag": pd.Series([pd.NA] * len(df), dtype="Int64"),
                "depth_m": depth,
                "reference_frame": frame_of_ref,
                "source": f"file:{path.name}",
                "fetched_utc": fetched,
            }).dropna(subset=["time_utc", "value"])
            if rows.empty:
                continue
            out.append(rows)

        record["sheets"].append({
            "sheet": sp.sheet,
            "time_column": sp.time_column,
            "time_kind": sp.time_kind,
            "time_trust": sp.time_trust,
            "assumed_timezone": (tz.key if sp.time_kind != "utc_explicit" else None),
            "variables": [{"column": v["column"], "variable": v["variable"],
                           "unit": v["unit"], "n": v["n_nonnull"]}
                          for v in sp.variables],
        })

    if not out:
        log(f"  {path.name}: no usable series")
        return empty_frame(), record

    frame = pd.concat(out, ignore_index=True)[CANONICAL_COLUMNS]
    record["n_rows"] = int(len(frame))
    log(f"  {path.name}: {len(frame):,} rows as station '{station}'")
    return frame, record


def _main(argv=None):
    import argparse
    from .config import load_config
    ap = argparse.ArgumentParser(description="Probe a user file.")
    ap.add_argument("path", type=Path)
    ap.add_argument("--station", default=None)
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args(argv)

    fp = probe(args.path, args.station)
    print(fp.summary())
    for s in fp.sheets:
        if not s.usable:
            continue
        print(f"  sheet {s.sheet!r}: time={s.time_column!r} "
              f"({s.time_kind}, trust={s.time_trust}) rows={s.n_rows:,}")
        for v in s.variables:
            print(f"      {v['column']!r} -> {v['variable']} "
                  f"[{v['unit'] or '?'}] n={v['n_nonnull']:,}")
    return 0 if fp.usable else 1


if __name__ == "__main__":
    raise SystemExit(_main())
