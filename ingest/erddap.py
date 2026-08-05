"""
erddap.py -- fetch ERDDAP tabledap into the canonical long frame.

Covers both feeds this project uses:
  * NOAA CoastWatch `cwwcNDBCMet`                 -> LJAC1, LJPC1, 46254
  * Axiom `scripps-pier-automated-shore-sta-1`    -> the SCCOOS autoss sensors

THE TIME CONTRACT
-----------------
ERDDAP emits ISO-8601 with an explicit `Z`, e.g. `2026-07-11T14:00:00Z`. That
string is unambiguous and we parse it as such, with `utc=True`, and assert the
result is tz-aware UTC before returning.

We do NOT hand the string to anything that might apply a machine-local offset.
That is precisely what broke the workbook: Power Query's implicit text ->
datetime conversion applied the machine's UTC-7 offset and discarded the zone,
so a column labelled `time (UTC)` held Pacific local time. The proof is visible
in the request itself -- the workbook asked ERDDAP for
`time>=2026-06-18T00:00:00Z` and the stored column starts at `2026-06-17 17:00`,
exactly 7 h early. See AGENT_TASK.md 0.1.

We request `.csv`, not `.csvp`. `.csv` puts names on line 1 and units on line 2,
so units arrive as data instead of being glued into the header text.
"""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

from .config import CANONICAL_COLUMNS, StationConfig, empty_frame

USER_AGENT = "la-jolla-buoy/station-data-extract (research; contact via repo)"
TIMEOUT_S = 120

# QARTOD: 1 good, 2 not evaluated, 3 suspect, 4 fail, 9 missing.
# 1, 2 and 3 pass; 3 is kept but stays flagged so the provenance sheet can
# report it. The workbook's original screen nulled only 4, letting 3 and 9
# through under a column named `*_ok` -- see AGENT_TASK.md 3.7.
QC_REJECT = {4, 9}
QC_SUSPECT = 3


def _iso_z(t: dt.datetime) -> str:
    """Format as ERDDAP wants it. Naive input is treated as already-UTC."""
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time_utc(values) -> pd.Series:
    """ISO-8601-with-Z text -> tz-aware UTC. The one place time is interpreted."""
    s = pd.to_datetime(pd.Series(values).astype("string"), utc=True,
                       format="ISO8601", errors="coerce")
    if len(s) and s.dt.tz is None:            # defensive; ISO8601+utc always sets it
        raise AssertionError("parsed time is not tz-aware -- refusing to guess a zone")
    return s


def build_url(base: str, columns: list[str], constraints: list[str],
              fmt: str = "csv") -> str:
    """Assemble a tabledap query. Constraints are pre-encoded fragments."""
    q = ",".join(columns)
    tail = "".join("&" + c for c in constraints)
    return f"{base}.{fmt}?{q}{tail}"


def time_constraints(start: dt.datetime, end: dt.datetime) -> list[str]:
    return [f"time%3E={_iso_z(start)}", f"time%3C={_iso_z(end)}"]


def station_constraint(stations: list[str]) -> str:
    """The regex-match form ERDDAP already accepts, parameterised.

    Kept byte-compatible with the form that was working in the workbook's URL:
    station=~"(A|B|C)" , URL-encoded.
    """
    inner = "|".join(stations)
    return "station=~" + quote(f'"({inner})"', safe="")


def fetch_csv(url: str, timeout: int = TIMEOUT_S) -> pd.DataFrame:
    """GET a tabledap .csv and return it with every column as text.

    Everything stays text here on purpose. Types are applied downstream, once,
    where the rules are written down -- not by a CSV parser's inference.
    """
    r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    if r.status_code == 404 and "nRows" in r.text:
        return pd.DataFrame()                 # ERDDAP's "no matching data"
    r.raise_for_status()
    text = r.text
    if not text.strip():
        return pd.DataFrame()

    # Line 1 = column names, line 2 = units.
    head = pd.read_csv(io.StringIO(text), nrows=1, dtype="string")
    units = {c: (str(head.iloc[0][c]) if pd.notna(head.iloc[0][c]) else "")
             for c in head.columns}
    body = pd.read_csv(io.StringIO(text), skiprows=[1], dtype="string",
                       keep_default_na=True)
    body.attrs["units"] = units
    return body


def _apply_qc(value: pd.Series, flag: pd.Series | None) -> tuple[pd.Series, pd.Series]:
    """Return (screened value, qc flag). Rejects 4 and 9; keeps and flags 3."""
    if flag is None:
        return value, pd.Series([pd.NA] * len(value), dtype="Int64",
                                index=value.index)
    f = pd.to_numeric(flag, errors="coerce").astype("Int64")
    screened = value.mask(f.isin(list(QC_REJECT)))
    return screened, f


def _melt(df: pd.DataFrame, station: str, mapping: dict[str, str],
          cfg: StationConfig, source: str, fetched: dt.datetime,
          units: dict[str, str]) -> pd.DataFrame:
    """Wide ERDDAP table -> canonical long rows, one variable at a time."""
    st = cfg.station(station) if station in cfg.stations else None
    time_utc = parse_time_utc(df["time"])
    out = []

    for erddap_col, canonical in mapping.items():
        if canonical.endswith("__qc") or erddap_col not in df.columns:
            continue
        value = pd.to_numeric(df[erddap_col], errors="coerce")
        if value.notna().sum() == 0:
            continue          # station does not report it; a silent all-null
            # series is how LJPC1's absent temperature became an invisible hole

        qc_col = next((c for c, n in mapping.items()
                       if n == f"{canonical}__qc" and c in df.columns), None)
        value, flags = _apply_qc(value, df[qc_col] if qc_col else None)

        unit = (st.unit_for(canonical) if st else "") or units.get(erddap_col, "")
        rows = pd.DataFrame({
            "time_utc": time_utc,
            "station": station,
            "variable": canonical,
            "value": value.astype("float64"),
            "unit": unit,
            "qc_flag": flags,
            "depth_m": (st.depth_m if st else None),
            "reference_frame": (st.reference_frame if st else "unknown"),
            "source": source,
            "fetched_utc": fetched,
        })
        out.append(rows.dropna(subset=["time_utc", "value"]))

    if not out:
        return empty_frame()
    return pd.concat(out, ignore_index=True)[CANONICAL_COLUMNS]


# --------------------------------------------------------------------------
# NDBC via CoastWatch ERDDAP
# --------------------------------------------------------------------------

def fetch_ndbc(cfg: StationConfig, start: dt.datetime, end: dt.datetime,
               stations: list[str] | None = None,
               fetched_utc: dt.datetime | None = None) -> pd.DataFrame:
    """Pull every configured NDBC station from cwwcNDBCMet in one request."""
    ep = cfg.endpoint("ndbc_erddap")
    sts = [s.key for s in cfg.by_source("ndbc_erddap")]
    if stations:
        sts = [s for s in sts if s in stations]
    if not sts:
        return empty_frame()

    # Union of the ERDDAP columns every selected station wants.
    mapping: dict[str, str] = {}
    for key in sts:
        mapping.update(cfg.station(key).erddap_columns())
    feed_cols = [c for c in mapping if not mapping[c].endswith("__qc")]
    columns = ["station", "time"] + sorted(set(feed_cols))

    url = build_url(ep["base"], columns,
                    [station_constraint(sts)] + time_constraints(start, end)
                    + [quote('orderBy("station,time")', safe="()=")])
    raw = fetch_csv(url)
    if raw.empty:
        return empty_frame()
    units = raw.attrs.get("units", {})

    fetched = fetched_utc or dt.datetime.now(dt.timezone.utc)
    frames = []
    for key in sts:
        sub = raw[raw["station"].astype("string").str.strip().str.upper()
                  == key.upper()]
        if sub.empty:
            continue
        frames.append(_melt(sub.reset_index(drop=True), key,
                            cfg.station(key).erddap_columns(), cfg,
                            "erddap:cwwcNDBCMet", fetched, units))
    if not frames:
        return empty_frame()
    return pd.concat(frames, ignore_index=True)[CANONICAL_COLUMNS]


# --------------------------------------------------------------------------
# SCCOOS autoss via Axiom ERDDAP
# --------------------------------------------------------------------------

def fetch_sccoos(cfg: StationConfig, start: dt.datetime, end: dt.datetime,
                 fetched_utc: dt.datetime | None = None) -> pd.DataFrame:
    """Pull the Scripps Pier shore station, one request per sensor group.

    Split by group because asking for every variable at once makes ERDDAP inner-
    join across sensors that run at different cadences, which silently drops rows
    wherever one sensor was down.
    """
    ep = cfg.endpoint("sccoos_erddap")
    st = cfg.station("autoss")
    fetched = fetched_utc or dt.datetime.now(dt.timezone.utc)

    groups: dict[str, list[str]] = {}
    for canonical, spec in st.variables.items():
        col = spec.get("erddap")
        if not col:
            continue
        # Sensor suffix: ..._ctd, ..._eco, ..._seaphox_external -> group key.
        for tag in ("ctd", "eco", "seaphox"):
            if tag in str(col):
                groups.setdefault(tag, []).append(canonical)
                break

    frames = []
    for tag, canonicals in groups.items():
        mapping: dict[str, str] = {}
        cols = ["time"]
        for canonical in canonicals:
            spec = st.variables[canonical]
            cols.append(str(spec["erddap"]))
            mapping[str(spec["erddap"])] = canonical
            if spec.get("qc"):
                cols.append(str(spec["qc"]))
                mapping[str(spec["qc"])] = f"{canonical}__qc"

        url = build_url(ep["base"], cols,
                        time_constraints(start, end)
                        + [quote('orderBy("time")', safe="()=")])
        try:
            raw = fetch_csv(url)
        except requests.HTTPError as e:
            raise RuntimeError(f"SCCOOS {tag} group failed: {e}") from e
        if raw.empty:
            continue
        frames.append(_melt(raw, "autoss", mapping, cfg,
                            f"erddap:scripps-pier:{tag}", fetched,
                            raw.attrs.get("units", {})))

    if not frames:
        return empty_frame()
    return pd.concat(frames, ignore_index=True)[CANONICAL_COLUMNS]


# --------------------------------------------------------------------------
# CLI -- useful on its own for probing a feed
# --------------------------------------------------------------------------

def _main(argv=None):
    import argparse
    from .config import load_config

    ap = argparse.ArgumentParser(description="Probe an ERDDAP feed.")
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--feed", choices=["ndbc", "sccoos"], default="ndbc")
    ap.add_argument("--days", type=int, default=None,
                    help="window in days back from now (default: config)")
    args = ap.parse_args(argv)

    cfg = load_config(args.root)
    days = args.days if args.days is not None else cfg.window_days
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)

    df = (fetch_ndbc if args.feed == "ndbc" else fetch_sccoos)(cfg, start, end)
    if df.empty:
        print("no rows")
        return 1
    g = (df.groupby(["station", "variable"])
           .agg(n=("value", "size"), first=("time_utc", "min"),
                last=("time_utc", "max"), unit=("unit", "first"),
                suspect=("qc_flag", lambda s: int((s == QC_SUSPECT).sum())))
           .reset_index())
    print(g.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
