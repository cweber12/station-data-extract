"""
ndbc_realtime.py — parse NDBC realtime2 / derived2 files into one canonical table.

    https://www.ndbc.noaa.gov/data/realtime2/<STATION>.<ext>
    https://www.ndbc.noaa.gov/data/derived2/<STATION>.dmv

Facts this module encodes, so nobody has to rediscover them:

  * NDBC realtime and historical files are **UTC only**. There is no timezone
    field in the data; the zone lives in the documentation. So we attach UTC
    explicitly and never infer.
  * Rows are ordered NEWEST FIRST. We sort ascending on the way out.
  * Two comment header lines, both starting with '#': names, then units.
  * Missing values are NOT one sentinel. Observed in live files:
        MM      most numeric fields
        N/A     SwD, WWD, STEEPNESS in .spec
        -99     MWD in .spec
        9.999   Sep_Freq in .data_spec
        999 / 99.0 / 9999   historical files only
  * Files hold a rolling ~45 days. Anything older needs the historical archive
    or ERDDAP. Poll no more than hourly: NDBC asks users to "limit your
    retrievals to a minimal level", and most stations report hourly with data
    available about 25 minutes past the hour.

Output is always the same long-format frame:

    time_utc | station | variable | value | unit | source | fetched_utc

Requires: pandas, requests.
"""
from __future__ import annotations

import io
import datetime as dt
from typing import Iterable

import pandas as pd
import requests

BASE_REALTIME = "https://www.ndbc.noaa.gov/data/realtime2/{station}.{ext}"
BASE_DERIVED  = "https://www.ndbc.noaa.gov/data/derived2/{station}.dmv"

# Every missing-value token seen in live realtime2 / derived2 files.
NA_TOKENS = ["MM", "N/A", "-99", "-99.0", "999", "999.0", "99.0", "9999", "9.999"]

TIME_COLS = ["#YY", "MM", "DD", "hh", "mm"]

# Canonical names + units, keyed by the NDBC column name.
# Note the collision: the month column is also called MM. We rename on read.
STDMET = {
    "WDIR": ("wind_from_direction", "degree_true"),
    "WSPD": ("wind_speed",          "m s-1"),
    "GST":  ("wind_speed_of_gust",  "m s-1"),
    "WVHT": ("sea_surface_wave_significant_height", "m"),
    "DPD":  ("sea_surface_wave_period_at_variance_spectral_density_maximum", "s"),
    "APD":  ("sea_surface_wave_mean_period", "s"),
    "MWD":  ("sea_surface_wave_from_direction", "degree_true"),
    "PRES": ("air_pressure_at_mean_sea_level", "hPa"),
    "ATMP": ("air_temperature",     "degree_C"),
    "WTMP": ("sea_water_temperature", "degree_C"),
    "DEWP": ("dew_point_temperature", "degree_C"),
    "VIS":  ("visibility_in_air",   "nmi"),
    "PTDY": ("tendency_of_air_pressure", "hPa"),
    "TIDE": ("water_level",         "ft"),
}
SPEC = {
    "WVHT": ("sea_surface_wave_significant_height", "m"),
    "SwH":  ("sea_surface_swell_wave_significant_height", "m"),
    "SwP":  ("sea_surface_swell_wave_period", "s"),
    "WWH":  ("sea_surface_wind_wave_significant_height", "m"),
    "WWP":  ("sea_surface_wind_wave_period", "s"),
    "APD":  ("sea_surface_wave_mean_period", "s"),
    "MWD":  ("sea_surface_wave_from_direction", "degree_true"),
    # SwD / WWD are compass strings ("W", "WSW"), STEEPNESS is a word. Dropped.
}
DMV = {
    "CHILL":  ("wind_chill_temperature", "degree_C"),
    "HEAT":   ("heat_index_temperature", "degree_C"),
    "ICE":    ("ice_accretion_rate",     "cm hr-1"),
    "WSPD10": ("wind_speed_at_10m",      "m s-1"),
    "WSPD20": ("wind_speed_at_20m",      "m s-1"),
}
MAPS = {"txt": STDMET, "spec": SPEC, "dmv": DMV}


def _url(station: str, ext: str) -> str:
    st = station.upper()
    return BASE_DERIVED.format(station=st) if ext == "dmv" \
        else BASE_REALTIME.format(station=st, ext=ext)


def parse(text: str, station: str, ext: str, fetched_utc: dt.datetime | None = None
          ) -> pd.DataFrame:
    """Parse the raw body of one NDBC file into the canonical long frame."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#"):
        raise ValueError(f"{station}.{ext}: no '#' header — got {lines[:1]}")

    names = lines[0].lstrip("#").split()
    # The month column is literally named "MM", same as the missing sentinel and
    # same as nothing else. Rename the five leading time columns positionally.
    names[:5] = ["_yr", "_mo", "_dy", "_hr", "_mn"]

    body = [ln for ln in lines[1:] if not ln.startswith("#")]
    df = pd.read_csv(io.StringIO("\n".join(body)), sep=r"\s+", header=None,
                     names=names, na_values=NA_TOKENS, keep_default_na=True,
                     engine="python", on_bad_lines="warn")

    # ---- the one line that matters: UTC is asserted, never inferred ----
    df["time_utc"] = pd.to_datetime(
        dict(year=df._yr, month=df._mo, day=df._dy, hour=df._hr, minute=df._mn),
        errors="coerce",
    ).dt.tz_localize("UTC")
    df = df.dropna(subset=["time_utc"]).sort_values("time_utc")   # NDBC ships newest-first

    fetched = fetched_utc or dt.datetime.now(dt.timezone.utc)
    mapping = MAPS[ext]
    out = []
    for col, (canonical, unit) in mapping.items():
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().sum() == 0:
            continue                       # station does not report this variable
        out.append(pd.DataFrame({
            "time_utc": df["time_utc"], "station": station.upper(),
            "variable": canonical, "value": s, "unit": unit,
            "source": f"ndbc:{ext}", "fetched_utc": fetched,
        }).dropna(subset=["value"]))
    if not out:
        return pd.DataFrame(columns=["time_utc", "station", "variable", "value",
                                     "unit", "source", "fetched_utc"])
    return pd.concat(out, ignore_index=True).sort_values(["variable", "time_utc"])


def fetch(station: str, ext: str = "txt", timeout: int = 60) -> pd.DataFrame:
    """Download and parse. Caller is responsible for not hammering NDBC."""
    url = _url(station, ext)
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": "la-jolla-buoy/1.0 (research)"})
    r.raise_for_status()
    return parse(r.text, station, ext)


def inventory(station: str, exts: Iterable[str] = ("txt", "spec", "dmv")) -> pd.DataFrame:
    """What does this station actually report right now, and how fresh is it?

    Run this before trusting a station. Files go stale independently: a station
    can keep publishing .spec and .dmv long after its .txt has stopped.
    """
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for ext in exts:
        try:
            df = fetch(station, ext)
        except Exception as e:
            rows.append({"station": station.upper(), "file": ext, "status": f"ERROR {e}"})
            continue
        if df.empty:
            rows.append({"station": station.upper(), "file": ext, "status": "empty"})
            continue
        for var, g in df.groupby("variable"):
            rows.append({
                "station": station.upper(), "file": ext, "variable": var,
                "n": len(g), "unit": g["unit"].iloc[0],
                "oldest_utc": g["time_utc"].min(), "newest_utc": g["time_utc"].max(),
                "age_hours": round((now - g["time_utc"].max()).total_seconds() / 3600, 1),
                "median_step_min": round(
                    g["time_utc"].diff().dt.total_seconds().median() / 60, 1),
                "status": "ok",
            })
    return pd.DataFrame(rows)
