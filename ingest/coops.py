"""
coops.py -- NOAA CO-OPS water level and datums for station 9410230.

Two things this module exists to get right.

TIME ZONE. The API takes `time_zone=gmt` or `time_zone=lst_ldt`. The workbook
asked for `lst_ldt`, which returns LOCAL time, and then labelled the column
`time (local)` -- correct by luck, but it meant water level was on a different
clock from everything else. We always ask for `gmt` and attach UTC explicitly.
The returned format is `YYYY-MM-DD HH:MM` with no zone marker at all, so the
zone comes from the request we made, which is the only honest source for it.

REQUEST LENGTH. `water_level` is capped at 31 days per call. A 45-day window is
therefore two or more calls, chunked and concatenated here. Asking for 45 days
in one request returns an error JSON, not a truncated series -- which is at
least loud, but only if you look.

NDBC `TIDE` is empty for LJAC1, LJPC1 and 46254, which is why water level comes
from here rather than from the met feed.
"""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pandas as pd
import requests

from .config import CANONICAL_COLUMNS, StationConfig, empty_frame

USER_AGENT = "la-jolla-buoy/station-data-extract (research; contact via repo)"
TIMEOUT_S = 120
APPLICATION = "la_jolla_buoy_station_data_extract"

# CO-OPS caps water_level at 31 days per request. Stay under it.
MAX_DAYS_PER_REQUEST = 30

FEET_TO_M = 0.3048


def _yyyymmdd(t: dt.datetime) -> str:
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t.strftime("%Y%m%d")


def _chunks(start: dt.datetime, end: dt.datetime, days: int = MAX_DAYS_PER_REQUEST):
    cur = start
    step = dt.timedelta(days=days)
    while cur < end:
        stop = min(cur + step, end)
        yield cur, stop
        cur = stop


def fetch_water_level(cfg: StationConfig, start: dt.datetime, end: dt.datetime,
                      station_key: str = "LJAC1",
                      datum: str = "MLLW", units: str = "english",
                      fetched_utc: dt.datetime | None = None) -> pd.DataFrame:
    """Water level for the CO-OPS station behind `station_key`, in UTC."""
    ep = cfg.endpoint("coops")
    st = cfg.station(station_key)
    coops_id = st.raw.get("coops_id")
    if not coops_id:
        raise ValueError(f"{station_key} has no coops_id in stations.yaml")

    fetched = fetched_utc or dt.datetime.now(dt.timezone.utc)
    unit_label = "ft" if units == "english" else "m"
    frames = []

    for c_start, c_end in _chunks(start, end):
        params = {
            "product": "water_level",
            "application": APPLICATION,
            "station": str(coops_id),
            "begin_date": _yyyymmdd(c_start),
            "end_date": _yyyymmdd(c_end),
            "datum": datum,
            "units": units,
            "time_zone": "gmt",          # never lst_ldt -- see the module docstring
            "format": "csv",
        }
        r = requests.get(ep["datagetter"], params=params, timeout=TIMEOUT_S,
                         headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        text = r.text.strip()
        if not text or text.lstrip().startswith("{"):
            # CO-OPS reports "no data" and errors as JSON with a 200.
            continue
        df = pd.read_csv(io.StringIO(text), dtype="string")
        df.columns = [c.strip() for c in df.columns]
        if "Date Time" not in df.columns:
            continue

        # We asked for gmt, so this naive text IS UTC. Assert it; never infer.
        time_utc = pd.to_datetime(df["Date Time"], format="%Y-%m-%d %H:%M",
                                  errors="coerce").dt.tz_localize("UTC")
        value = pd.to_numeric(df.get("Water Level"), errors="coerce")

        frames.append(pd.DataFrame({
            "time_utc": time_utc,
            "station": station_key,
            "variable": "water_level",
            "value": value.astype("float64"),
            "unit": unit_label,
            "qc_flag": pd.Series([pd.NA] * len(df), dtype="Int64"),
            "depth_m": st.depth_m,
            "reference_frame": st.reference_frame,
            "source": f"coops:water_level:{datum}",
            "fetched_utc": fetched,
        }).dropna(subset=["time_utc", "value"]))

    if not frames:
        return empty_frame()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["time_utc", "station", "variable"])
    return out.sort_values("time_utc").reset_index(drop=True)[CANONICAL_COLUMNS]


def fetch_datums(cfg: StationConfig, station_key: str = "LJAC1") -> pd.DataFrame:
    """Tidal datums in feet and metres. MSL - MLLW is the number that matters."""
    ep = cfg.endpoint("coops")
    coops_id = cfg.station(station_key).raw.get("coops_id")
    if not coops_id:
        raise ValueError(f"{station_key} has no coops_id in stations.yaml")

    url = ep["datums"].format(station=coops_id) + "?units=english"
    r = requests.get(url, timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    rows = r.json().get("datums", [])
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["value_m"] = df["value"] * FEET_TO_M
    return df


def msl_above_mllw_m(cfg: StationConfig, station_key: str = "LJAC1") -> float | None:
    """The 0.832 m that converts a below-MLLW depth to a below-MSL depth."""
    df = fetch_datums(cfg, station_key)
    if df.empty:
        return None
    try:
        msl = float(df.loc[df["name"] == "MSL", "value_m"].iloc[0])
        mllw = float(df.loc[df["name"] == "MLLW", "value_m"].iloc[0])
    except (IndexError, ValueError):
        return None
    return msl - mllw


def _main(argv=None):
    import argparse
    from .config import load_config

    ap = argparse.ArgumentParser(description="Probe the CO-OPS feed.")
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--datums", action="store_true", help="print datums instead")
    args = ap.parse_args(argv)

    cfg = load_config(args.root)
    if args.datums:
        print(fetch_datums(cfg).to_string(index=False))
        sep = msl_above_mllw_m(cfg)
        print(f"\nMSL is {sep:.3f} m above MLLW" if sep else "\nno datum separation")
        return 0

    days = args.days if args.days is not None else cfg.window_days
    end = dt.datetime.now(dt.timezone.utc)
    df = fetch_water_level(cfg, end - dt.timedelta(days=days), end)
    if df.empty:
        print("no rows")
        return 1
    print(f"{len(df)} rows  {df.time_utc.min()} -> {df.time_utc.max()}  "
          f"unit={df.unit.iloc[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
