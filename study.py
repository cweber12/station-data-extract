"""
study.py -- timestamped, immutable snapshots of a pull.

WHERE PROJECTS LIVE
-------------------
One level ABOVE this repo, in `la-jolla-buoy/studies/`, not inside it. The
other extractors in the parent directory (hf-radar-extract, cudem-extract) can
then read and write the same study without reaching into this repo, and a
study can accumulate several tools' output for one site and one time window:

    la-jolla-buoy/
      studies/20260804T2230Z__baseline/
        study.json          <- shared, tool-agnostic. THE metadata file.
        station-data/         <- this tool's namespace, written here
          manifest.json         detail record for the station pull
          validation.json       clock checks, coverage, cross-station sanity
          cache/observations.parquet
          workbook/             optional refreshed Excel snapshot
          outputs/              generated comparison workbooks
        hf-radar/             <- another tool's namespace, later
        cudem/
      station-data-extract/   <- this repo (code only)
      hf-radar-extract/
      cudem-extract/

Because `studies/` sits outside the repo it needs no .gitignore entry and can
never be committed by accident.

IMMUTABILITY
------------
A study is written once. Only `<producer>/outputs/` may change afterwards. If
creation fails partway the directory is LEFT IN PLACE with status "incomplete"
rather than deleted -- a failed pull is evidence about the feed, and deleting it
destroys the only record that the failure happened.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ingest.config import CANONICAL_COLUMNS, StationConfig, load_config

STUDIES_DIRNAME = "studies"
PRODUCER = "station-data"
SCHEMA_VERSION = 1
TOOL_VERSION = "station-data-extract 2026-08-05-a"

MANIFEST_NAME = "manifest.json"
VALIDATION_NAME = "validation.json"
STUDY_META_NAME = "study.json"
CACHE_DIRNAME = "cache"
WORKBOOK_DIRNAME = "workbook"
OUTPUTS_DIRNAME = "outputs"
OBSERVATIONS_NAME = "observations.parquet"

STATUS_OK = "ok"
STATUS_FAILED = "failed_validation"
STATUS_INCOMPLETE = "incomplete"


# --------------------------------------------------------------------------
# paths and ids
# --------------------------------------------------------------------------

def default_studies_root(repo_root: Path | None = None) -> Path:
    """`<repo>/../studies` -- one level up, visible to the sibling extractors."""
    repo_root = Path(repo_root or Path(__file__).resolve().parent).resolve()
    return repo_root.parent / STUDIES_DIRNAME


def slugify(label: str) -> str:
    """Windows-safe, sortable, no surprises in a path."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(label or "").strip()).strip("-").lower()
    return (s or "session")[:48]


def new_study_id(label: str, now_utc: dt.datetime | None = None) -> str:
    """'20260804T2230Z__baseline'. UTC, sortable, no characters Windows rejects."""
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(dt.timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%MZ')}__{slugify(label)}"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _iso(t) -> str | None:
    if t is None or (isinstance(t, float) and pd.isna(t)):
        return None
    ts = pd.Timestamp(t)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.write("\n")


def _read_json(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# the info record the UI lists
# --------------------------------------------------------------------------

@dataclass
class StudyInfo:
    path: Path
    study_id: str
    label: str
    created_utc: str
    status: str = STATUS_INCOMPLETE
    stations: list[str] = field(default_factory=list)
    time_min_utc: str | None = None
    time_max_utc: str | None = None
    n_rows: int = 0
    validation_summary: str = ""
    producer: str = PRODUCER
    manifest: dict = field(default_factory=dict)

    # ------------------------------------------------------------- locations

    @property
    def producer_dir(self) -> Path:
        return self.path / self.producer

    @property
    def cache_dir(self) -> Path:
        return self.producer_dir / CACHE_DIRNAME

    @property
    def workbook_dir(self) -> Path:
        return self.producer_dir / WORKBOOK_DIRNAME

    @property
    def outputs_dir(self) -> Path:
        return self.producer_dir / OUTPUTS_DIRNAME

    @property
    def observations_path(self) -> Path:
        return self.cache_dir / OBSERVATIONS_NAME

    @property
    def is_archive(self) -> bool:
        return self.study_id == "archive"

    # ------------------------------------------------------------- rendering

    @property
    def coverage(self) -> str:
        if not self.time_min_utc or not self.time_max_utc:
            return "-"
        return f"{self.time_min_utc[:16]} to {self.time_max_utc[:16]}"

    def display(self) -> str:
        return (f"{self.label}  [{self.status}]  {self.created_utc[:16]}  "
                f"{len(self.stations)} stations  {self.n_rows:,} rows")

    def load_observations(self) -> pd.DataFrame:
        p = self.observations_path
        if not p.is_file():
            return pd.DataFrame(columns=CANONICAL_COLUMNS)
        df = pd.read_parquet(p)
        if "time_utc" in df and getattr(df["time_utc"].dtype, "tz", None) is None:
            df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        return df


# --------------------------------------------------------------------------
# reading studies back
# --------------------------------------------------------------------------

def load_study(path: Path, producer: str = PRODUCER) -> StudyInfo:
    """Read one study from disk. Cheap: JSON only, never the parquet."""
    path = Path(path)
    meta_path = path / STUDY_META_NAME
    man_path = path / producer / MANIFEST_NAME

    meta = _read_json(meta_path) if meta_path.is_file() else {}
    man = _read_json(man_path) if man_path.is_file() else {}

    series = man.get("series") or []
    stations = sorted({s.get("station") for s in series if s.get("station")})
    times_min = [s.get("time_min_utc") for s in series if s.get("time_min_utc")]
    times_max = [s.get("time_max_utc") for s in series if s.get("time_max_utc")]

    validation = man.get("validation") or {}
    checks = validation.get("clock_checks") or []
    failed = [c for c in checks if not c.get("ok")]
    if not checks:
        summary = "no clock check recorded"
    elif failed:
        summary = "; ".join(f"{c.get('signal')} offset {c.get('offset_hours'):+.2f} h"
                            for c in failed)
    else:
        summary = f"{len(checks)} clock check(s) passed"

    status = man.get("status") or meta.get("status") or STATUS_INCOMPLETE

    return StudyInfo(
        path=path,
        study_id=meta.get("study_id") or man.get("study_id") or path.name,
        label=meta.get("label") or man.get("label") or path.name,
        created_utc=meta.get("created_utc") or man.get("created_utc") or "",
        status=status,
        stations=stations,
        time_min_utc=min(times_min) if times_min else None,
        time_max_utc=max(times_max) if times_max else None,
        n_rows=int(man.get("n_rows") or sum(int(s.get("n") or 0) for s in series)),
        validation_summary=summary,
        producer=producer,
        manifest=man,
    )


def list_studies(root: Path | None = None, producer: str = PRODUCER
                  ) -> list[StudyInfo]:
    """Newest first. Reads JSON only, so it stays fast enough for a UI list."""
    proot = Path(root) if root is not None else default_studies_root()
    if not proot.is_dir():
        return []
    out = []
    for d in proot.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        if not (d / STUDY_META_NAME).is_file() and not (d / producer).is_dir():
            continue
        try:
            out.append(load_study(d, producer))
        except Exception:
            continue                     # a corrupt study must not hide the rest
    out.sort(key=lambda p: (p.created_utc or "", p.study_id), reverse=True)
    return out


def latest_study(root: Path | None = None, producer: str = PRODUCER
                   ) -> StudyInfo | None:
    ps = list_studies(root, producer)
    return ps[0] if ps else None


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _clock_checks(df: pd.DataFrame, cfg: StationConfig) -> list[dict]:
    """Verify the ingested clock against the anchor station's solar signals."""
    from ingest.clockcheck import verify_utc

    spec = cfg.comparisons.get("clock_check") or {}
    key = spec.get("station") or (cfg.clock_anchor.key if cfg.clock_anchor else None)
    if not key or key not in cfg.stations:
        return []
    st = cfg.station(key)
    if st.lon is None:
        return [{"signal": "-", "ok": False,
                 "detail": f"{key} has no longitude in stations.yaml"}]

    tol = float(spec.get("tolerance_h", 1.5))
    min_days = int(spec.get("min_days", 10))
    out = []
    for sig in spec.get("signals") or []:
        var = sig.get("variable")
        sub = df[(df.station == key) & (df.variable == var)].sort_values("time_utc")
        if sub.empty:
            out.append({"signal": var, "role": sig.get("role"), "ok": False,
                        "detail": f"no {var} rows for {key}"})
            continue
        try:
            v = verify_utc(sub.time_utc, sub.value, var, float(st.lon),
                           tolerance_h=tol, min_days=min_days)
        except Exception as e:                      # a bad fit must not abort
            out.append({"signal": var, "role": sig.get("role"), "ok": False,
                        "detail": f"check raised: {e}"})
            continue
        out.append({
            "signal": v.signal, "role": sig.get("role"), "station": key,
            "n_days": v.n_days,
            "observed_peak_hour_utc": round(v.observed_peak_hour_utc, 3),
            "expected_peak_hour_utc": round(v.expected_peak_hour_utc, 3),
            "offset_hours": round(v.offset_hours, 3),
            "amplitude": round(v.amplitude, 4),
            "tolerance_h": tol, "ok": bool(v.ok), "detail": str(v),
        })
    return out


def _coverage(df: pd.DataFrame) -> list[dict]:
    """Per series: span, median step, largest gap, rough % missing."""
    rows = []
    for (station, variable), g in df.groupby(["station", "variable"], sort=True):
        t = pd.to_datetime(g["time_utc"]).sort_values()
        steps = t.diff().dropna().dt.total_seconds() / 60.0
        med = float(steps.median()) if len(steps) else float("nan")
        biggest = float(steps.max()) if len(steps) else float("nan")
        span_min = (t.max() - t.min()).total_seconds() / 60.0 if len(t) > 1 else 0.0
        expected = span_min / med + 1 if med and med > 0 else float("nan")
        pct_missing = (100.0 * (1 - len(t) / expected)
                       if expected and expected == expected and expected > 0
                       else float("nan"))
        rows.append({
            "station": station, "variable": variable, "n": int(len(g)),
            "time_min_utc": _iso(t.min()), "time_max_utc": _iso(t.max()),
            "median_step_min": None if med != med else round(med, 3),
            "largest_gap_min": None if biggest != biggest else round(biggest, 1),
            "pct_missing": (None if pct_missing != pct_missing
                            else round(max(0.0, pct_missing), 2)),
            "unit": str(g["unit"].iloc[0]),
            "n_flagged_suspect": int((pd.to_numeric(g.get("qc_flag"),
                                                    errors="coerce") == 3).sum()),
        })
    return rows


def _schema_conformance(df: pd.DataFrame) -> dict:
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    tz_ok = bool(len(df) == 0 or getattr(df["time_utc"].dtype, "tz", None) is not None)
    return {
        "ok": (not missing) and tz_ok,
        "missing_columns": missing,
        "time_utc_is_tz_aware": tz_ok,
        "n_rows": int(len(df)),
    }


def _cross_station(df: pd.DataFrame, cfg: StationConfig) -> dict:
    """Best lag between the two Earth-frame temperature references.

    The LAG is the diagnostic. Their absolute difference is reported but is not
    a pass/fail criterion at a tight threshold: the two sensors are 1.57 m apart
    vertically on a coast with a strong internal tide, so a degree or two of
    separation in stratified conditions is ocean, not error.
    """
    spec = cfg.comparisons.get("cross_station_sanity") or {}
    a, b = spec.get("a") or {}, spec.get("b") or {}
    if not a or not b:
        return {"ok": True, "detail": "not configured"}

    def series(s):
        sub = df[(df.station == s.get("station")) & (df.variable == s.get("variable"))]
        if sub.empty:
            return None
        return (sub.set_index(pd.to_datetime(sub["time_utc"]))["value"]
                   .sort_index().resample("1h").mean())

    sa, sb = series(a), series(b)
    if sa is None or sb is None:
        return {"ok": False, "detail": "one or both series absent",
                "a": a, "b": b}

    joined = pd.concat({"a": sa, "b": sb}, axis=1).dropna()
    if len(joined) < 24:
        return {"ok": False, "detail": f"only {len(joined)} overlapping hours"}

    best_lag, best_r = 0.0, float("nan")
    for k in range(-12, 13):
        r = joined["a"].corr(joined["b"].shift(k))
        if pd.notna(r) and (pd.isna(best_r) or abs(r) > abs(best_r)):
            best_lag, best_r = float(k), float(r)

    median_diff = float((joined["a"] - joined["b"]).median())
    max_lag = float(spec.get("max_abs_lag_h", 1.0))
    max_diff = float(spec.get("max_abs_median_diff_c", 2.5))
    lag_ok = abs(best_lag) <= max_lag

    return {
        "ok": bool(lag_ok),
        "best_lag_h": best_lag,
        "r_at_best_lag": None if best_r != best_r else round(best_r, 4),
        "median_diff": round(median_diff, 3),
        "n_overlapping_hours": int(len(joined)),
        "max_abs_lag_h": max_lag,
        "median_diff_advisory_limit": max_diff,
        "median_diff_within_advisory": bool(abs(median_diff) <= max_diff),
        "detail": ("lag is the test; median difference is informational because "
                   "the two sensors are 1.57 m apart vertically"),
    }


def validate(df: pd.DataFrame, cfg: StationConfig) -> dict:
    """Everything a study is checked for. Never raises; records instead."""
    clock = _clock_checks(df, cfg)
    schema = _schema_conformance(df)
    coverage = _coverage(df)
    cross = _cross_station(df, cfg)

    clock_ok = bool(clock) and all(c.get("ok") for c in clock)
    status = STATUS_OK if (clock_ok and schema["ok"] and cross.get("ok")) \
        else STATUS_FAILED

    return {
        "status": status,
        "checked_utc": _iso(dt.datetime.now(dt.timezone.utc)),
        "clock_checks": clock,
        "schema": schema,
        "cross_station_sanity": cross,
        "coverage": coverage,
        "qc_policy": cfg.qc_policy,
    }


# --------------------------------------------------------------------------
# creating a study
# --------------------------------------------------------------------------

def _gather(cfg: StationConfig, start: dt.datetime, end: dt.datetime,
            fetched: dt.datetime, log=print) -> tuple[pd.DataFrame, list[str]]:
    """Run every Python ingest source. A failing source is recorded, not fatal."""
    from ingest import coops, erddap
    from ingest.config import empty_frame

    frames, problems = [], []

    for name, fn in [
        ("ndbc/erddap", lambda: erddap.fetch_ndbc(cfg, start, end, fetched_utc=fetched)),
        ("sccoos/erddap", lambda: erddap.fetch_sccoos(cfg, start, end, fetched_utc=fetched)),
        ("coops/water_level", lambda: coops.fetch_water_level(cfg, start, end,
                                                              fetched_utc=fetched)),
    ]:
        try:
            log(f"  fetching {name} ...")
            df = fn()
            log(f"    {len(df):,} rows")
            if not df.empty:
                frames.append(df)
        except Exception as e:
            problems.append(f"{name}: {e}")
            log(f"    FAILED: {e}")

    if not frames:
        return empty_frame(), problems
    out = pd.concat(frames, ignore_index=True)
    out = cfg.attach_geometry(out)
    out = out.sort_values(["station", "variable", "time_utc"]).reset_index(drop=True)
    return out[CANONICAL_COLUMNS], problems


def _local_sources(repo_root: Path, cfg: StationConfig, fetched: dt.datetime,
                   log=print) -> pd.DataFrame:
    """The yellow buoy logger export -- a local file, not a feed.

    Its `Date-Time (PDT)` column is honestly labelled: HOBOconnect writes local
    wall time with the configured zone. Converted here via zoneinfo, never by
    adding a constant.
    """
    from zoneinfo import ZoneInfo
    from ingest.config import empty_frame

    path = repo_root / "sources" / "yellow_buoy_temps.xlsx"
    if not path.is_file():
        return empty_frame()
    try:
        df = pd.read_excel(path, sheet_name="Data")
    except Exception as e:
        log(f"  yellow buoy: unreadable ({e})")
        return empty_frame()

    tcol = next((c for c in df.columns if isinstance(c, str)
                 and "date" in c.lower() and "time" in c.lower()), None)
    vcol = next((c for c in df.columns if isinstance(c, str)
                 and "tidbit" in c.lower()), None)
    if tcol is None or vcol is None:
        log("  yellow buoy: expected columns not found")
        return empty_frame()

    local = ZoneInfo(cfg.defaults.get("timezone_display", "America/Los_Angeles"))
    idx = pd.to_datetime(df[tcol], errors="coerce")
    time_utc = (pd.DatetimeIndex(idx)
                .tz_localize(local, ambiguous="NaT", nonexistent="NaT")
                .tz_convert("UTC"))
    degf = pd.to_numeric(df[vcol], errors="coerce")
    st = cfg.station("yellow_buoy")

    out = pd.DataFrame({
        "time_utc": time_utc,
        "station": "yellow_buoy",
        "variable": "sea_water_temperature",
        "value": (degf - 32.0) * 5.0 / 9.0,      # canonical degC, converted once
        "unit": "degC",
        "qc_flag": pd.Series([pd.NA] * len(df), dtype="Int64"),
        "depth_m": st.depth_m,
        "reference_frame": st.reference_frame,
        "source": f"local:{path.name}",
        "fetched_utc": fetched,
    }).dropna(subset=["time_utc", "value"])
    log(f"  yellow buoy: {len(out):,} rows")
    return out[CANONICAL_COLUMNS]


def create_study(repo_root: Path, label: str, *,
                   studies_root: Path | None = None,
                   refresh: bool = True,
                   window_days: int | None = None,
                   log=print) -> StudyInfo:
    """Create studies/<id>/, ingest, validate, write metadata. Returns the info.

    `refresh=True` also runs an Excel COM refresh of the Power Query workbook and
    snapshots it into the study. `refresh=False` skips Excel entirely -- the
    Python ingest is the primary path and does not need it.
    """
    repo_root = Path(repo_root).resolve()
    cfg = load_config(repo_root)
    proot = Path(studies_root) if studies_root else default_studies_root(repo_root)

    now = dt.datetime.now(dt.timezone.utc)
    pid = new_study_id(label, now)
    pdir = proot / pid
    prod = pdir / PRODUCER
    (prod / CACHE_DIRNAME).mkdir(parents=True, exist_ok=True)
    (prod / OUTPUTS_DIRNAME).mkdir(parents=True, exist_ok=True)

    days = int(window_days if window_days is not None else cfg.window_days)
    end, start = now, now - dt.timedelta(days=days)

    # Write an incomplete marker FIRST, so a crash still leaves evidence.
    meta = {
        "schema_version": SCHEMA_VERSION,
        "study_id": pid,
        "label": str(label),
        "created_utc": _iso(now),
        "created_by": TOOL_VERSION,
        "site": {"name": "La Jolla, CA",
                 "stations": {k: {"lon": s.lon, "lat": s.lat, "role": s.role}
                              for k, s in cfg.stations.items()}},
        "time_window_utc": {"start": _iso(start), "end": _iso(end),
                            "window_days": days},
        "producers": [{"name": PRODUCER, "dir": PRODUCER,
                       "tool_version": TOOL_VERSION, "status": STATUS_INCOMPLETE,
                       "created_utc": _iso(now)}],
        "status": STATUS_INCOMPLETE,
        "notes": "",
    }
    _write_json(pdir / STUDY_META_NAME, meta)

    problems: list[str] = []

    # ---- 1. optional Excel refresh + workbook snapshot --------------------
    refresh_meta: dict[str, Any] = {}
    workbook_rec: dict[str, Any] | None = None
    src_xlsx = repo_root / "sources" / "ja_jolla_sensors.xlsx"
    if refresh and src_xlsx.is_file():
        try:
            from ingest.refresh import refresh_and_snapshot
            dest = prod / WORKBOOK_DIRNAME / src_xlsx.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            log("  refreshing Excel workbook (this takes tens of seconds) ...")
            refresh_meta = refresh_and_snapshot(src_xlsx, dest) or {}
            workbook_rec = {
                "path": str(src_xlsx.relative_to(repo_root)).replace("\\", "/"),
                "snapshot": f"{PRODUCER}/{WORKBOOK_DIRNAME}/{dest.name}",
                "sha256": sha256_file(dest),
                "source_sha256": sha256_file(src_xlsx),
                "mtime_utc": _iso(dt.datetime.fromtimestamp(
                    src_xlsx.stat().st_mtime, dt.timezone.utc)),
                "m_version": refresh_meta.get("m_version"),
                "fetched_utc": refresh_meta.get("fetched_utc"),
                "p_StartUTC": refresh_meta.get("p_StartUTC"),
                "p_EndUTC": refresh_meta.get("p_EndUTC"),
            }
        except Exception as e:
            problems.append(f"excel refresh: {e}")
            log(f"  Excel refresh failed ({e}); continuing with the Python ingest")
    elif src_xlsx.is_file():
        # No refresh requested: copy the workbook as-is so the snapshot is whole.
        try:
            dest = prod / WORKBOOK_DIRNAME / src_xlsx.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_xlsx, dest)
            workbook_rec = {
                "path": str(src_xlsx.relative_to(repo_root)).replace("\\", "/"),
                "snapshot": f"{PRODUCER}/{WORKBOOK_DIRNAME}/{dest.name}",
                "sha256": sha256_file(dest),
                "source_sha256": sha256_file(src_xlsx),
                "mtime_utc": _iso(dt.datetime.fromtimestamp(
                    src_xlsx.stat().st_mtime, dt.timezone.utc)),
                "m_version": None, "fetched_utc": None,
                "p_StartUTC": None, "p_EndUTC": None,
                "note": "copied without refresh (refresh=False)",
            }
        except Exception as e:
            problems.append(f"workbook copy: {e}")

    # ---- 2. Python ingest -------------------------------------------------
    log(f"  window {days} d: {_iso(start)} -> {_iso(end)}")
    df, fetch_problems = _gather(cfg, start, end, now, log=log)
    problems += fetch_problems

    local = _local_sources(repo_root, cfg, now, log=log)
    if not local.empty:
        df = (pd.concat([df, local], ignore_index=True) if not df.empty else local)
        df = df.sort_values(["station", "variable", "time_utc"]).reset_index(drop=True)

    # ---- 3. cache ---------------------------------------------------------
    obs_path = prod / CACHE_DIRNAME / OBSERVATIONS_NAME
    if not df.empty:
        df.to_parquet(obs_path, index=False)
        log(f"  wrote {len(df):,} rows -> {obs_path.name}")
    else:
        problems.append("no observations were ingested")

    # ---- 4. validate ------------------------------------------------------
    val = validate(df, cfg) if not df.empty else {
        "status": STATUS_FAILED, "clock_checks": [], "schema": {"ok": False},
        "coverage": [], "cross_station_sanity": {"ok": False, "detail": "no data"},
        "qc_policy": cfg.qc_policy,
    }
    val["problems"] = problems
    _write_json(prod / VALIDATION_NAME, val)

    # No rows at all means the snapshot never happened, whatever the checks say.
    # Otherwise the validation verdict stands: a source that failed while others
    # succeeded is recorded in `problems` and does not by itself invalidate the
    # data that WAS collected.
    status = STATUS_INCOMPLETE if df.empty else val["status"]

    # ---- 5. manifest ------------------------------------------------------
    series = [{
        "station": c["station"], "variable": c["variable"], "unit": c["unit"],
        "n": c["n"], "time_min_utc": c["time_min_utc"],
        "time_max_utc": c["time_max_utc"],
        "median_step_min": c["median_step_min"],
        "qc_policy": cfg.qc_policy,
        "n_flagged_suspect": c["n_flagged_suspect"],
        "sensor_depth_m": cfg.station(c["station"]).depth_m
                          if c["station"] in cfg.stations else None,
        "reference_frame": cfg.station(c["station"]).reference_frame
                           if c["station"] in cfg.stations else "unknown",
    } for c in val.get("coverage", [])]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "study_id": pid,
        "label": str(label),
        "producer": PRODUCER,
        "created_utc": _iso(now),
        "tool_version": TOOL_VERSION,
        "status": status,
        "n_rows": int(len(df)),
        "window_utc": {"start": _iso(start), "end": _iso(end),
                       "window_days": days},
        "ingest": {"mode": "python",
                   "sources": sorted(df["source"].unique().tolist()) if not df.empty else [],
                   "problems": problems},
        "source_workbook": workbook_rec,
        "config_snapshot": cfg.raw,
        "series": series,
        "validation": val,
        "notes": "",
    }
    _write_json(prod / MANIFEST_NAME, manifest)

    # ---- 6. finish the shared metadata ------------------------------------
    meta["status"] = status
    meta["producers"][0]["status"] = status
    meta["producers"][0]["n_rows"] = int(len(df))
    meta["producers"][0]["stations"] = sorted(df["station"].unique().tolist()) \
        if not df.empty else []
    _write_json(pdir / STUDY_META_NAME, meta)

    log(f"  study {pid} -> {status}")
    return load_study(pdir)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Create and list study snapshots.")
    ap.add_argument("command", choices=["create", "list", "show"])
    ap.add_argument("--label", default="session")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--studies-root", type=Path, default=None)
    ap.add_argument("--refresh", action="store_true",
                    help="also refresh the Excel workbook via COM")
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args(argv)

    proot = args.studies_root or default_studies_root(args.root)

    if args.command == "create":
        info = create_study(args.root, args.label, studies_root=proot,
                              refresh=args.refresh, window_days=args.days)
        print(f"\n{info.study_id}  {info.status}")
        print(f"  {info.path}")
        print(f"  {info.n_rows:,} rows, stations: {', '.join(info.stations)}")
        print(f"  {info.validation_summary}")
        return 0 if info.status == STATUS_OK else 1

    if args.command == "list":
        ps = list_studies(proot)
        if not ps:
            print(f"no studies under {proot}")
            return 1
        for p in ps:
            print(p.display())
        return 0

    p = latest_study(proot)
    if p is None:
        print("no studies")
        return 1
    print(json.dumps(p.manifest.get("validation", {}), indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
