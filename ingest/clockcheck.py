"""
clockcheck.py — verify that a timestamp column really is the timezone it claims.

The La Jolla project lost a week to a column labelled `time (UTC)` that actually
held Pacific local time. No amount of internal cross-checking finds that: every
timestamp in a file usually comes from one clock through one assumed zone, so
they are consistent with each other by construction.

The way out is to check against a signal whose phase in LOCAL SOLAR time is
known from physics, not from metadata:

  air temperature      peaks ~2 h after solar noon
  barometric pressure  the S2 atmospheric tide peaks ~10:00 and ~22:00
                       local solar time. Very stable, works anywhere.

Both are available from any NDBC station that reports ATMP and PRES.

Call `verify_utc()` in your ingest pipeline and let it raise. It costs one pass
over data you already downloaded.

THE CLI EXISTS BECAUSE A WORKBOOK ARRIVES BEFORE A STUDY DOES
    `study.py` already runs this check on every study it creates, against the
    anchor station's ingested series. That covers data the repo has already
    pulled and normalised. It does NOT cover the case the check was written
    for: an arbitrary spreadsheet someone hands you, whose zone is claimed by
    nothing but a column name. That is where the original `time (UTC)`-holding-
    Pacific-local bug came from, and until this module had a `__main__` the only
    way to interrogate such a file was to write a script by hand.

        python -m ingest.clockcheck --workbook sources/ja_jolla_sensors.xlsx \
            --sheet src_LJAC1 --time time_utc --lon -117.257

    Exit codes are three-valued on purpose, because "the clock is wrong" and
    "there was not enough here to tell" are different answers and collapsing
    them into one non-zero sends people hunting for a bug that is not there:

        0  every signal checked verifies as UTC
        1  a signal was MEASURED outside tolerance -- a real clock problem
        2  inconclusive, or nothing checkable was found -- no verdict reached
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# Hours after local MEAN solar noon at which each signal peaks.
EXPECTED = {"air_temperature": 2.0, "air_pressure_at_mean_sea_level": -2.0}
HARMONIC = {"air_temperature": 1, "air_pressure_at_mean_sea_level": 2}


@dataclass
class ClockVerdict:
    signal: str
    n_days: int
    observed_peak_hour_utc: float
    expected_peak_hour_utc: float
    offset_hours: float          # + means the column runs AHEAD of true UTC
    amplitude: float
    ok: bool
    reason: str = ""             # why it failed; empty when ok

    @property
    def inconclusive(self) -> bool:
        """Not enough evidence to judge -- which is NOT the same as a bad clock.

        A short window or a flat signal means the harmonic fit has nothing to
        work with. Reporting that as a timezone error sends people hunting for
        a bug that is not there.
        """
        return not self.ok and self.reason.startswith(("too few days",
                                                       "signal too weak"))

    def __str__(self) -> str:
        flag = "OK " if self.ok else ("?? " if self.inconclusive else "FAIL")
        base = (f"[{flag}] {self.signal:<34} peak {self.observed_peak_hour_utc:5.2f} UTC "
                f"(expect {self.expected_peak_hour_utc:5.2f})  "
                f"offset {self.offset_hours:+.2f} h  amp {self.amplitude:.2f}")
        return base if self.ok else f"{base}  -- {self.reason}"


def _phase_of_max(times: pd.Series, values: pd.Series, harmonic: int) -> tuple[float, float]:
    """Least-squares harmonic fit to the hour-of-day composite. Returns (hour, amp)."""
    df = pd.DataFrame({"t": pd.to_datetime(times), "v": pd.to_numeric(values, errors="coerce")}).dropna()
    if df.empty:
        return float("nan"), 0.0
    df["day"] = df.t.dt.floor("D")
    df["anom"] = df.v - df.groupby("day").v.transform("mean")   # kill the synoptic signal
    df["hour"] = df.t.dt.hour + df.t.dt.minute / 60
    hrs = np.arange(24)
    comp = df.groupby(df.hour.astype(int)).anom.mean().reindex(hrs)
    if comp.isna().any():
        comp = comp.interpolate(limit_direction="both")
    w = 2 * np.pi * harmonic / 24
    A = np.c_[np.cos(w * hrs), np.sin(w * hrs), np.ones(24)]
    c, *_ = np.linalg.lstsq(A, comp.values, rcond=None)
    return float((np.arctan2(c[1], c[0]) / w) % (24 / harmonic)), float(np.hypot(c[0], c[1]))


def verify_utc(times, values, signal: str, longitude_deg: float,
               tolerance_h: float = 1.5, min_days: int = 10) -> ClockVerdict:
    """Check that `times` is genuine UTC, using the known solar phase of `signal`.

    times     tz-aware or naive datetimes, ASSUMED to be UTC (that's the claim under test)
    values    the measurements
    signal    'air_temperature' or 'air_pressure_at_mean_sea_level'
    longitude_deg  negative west, e.g. -117.257 for Scripps Pier
    """
    if signal not in EXPECTED:
        raise ValueError(f"no known solar phase for {signal!r}; use {list(EXPECTED)}")
    t = pd.to_datetime(pd.Series(times))
    t = t.dt.tz_localize(None) if getattr(t.dt, "tz", None) is not None else t
    n_days = int((t.max() - t.min()).total_seconds() // 86400)

    harmonic = HARMONIC[signal]
    obs, amp = _phase_of_max(t, pd.Series(values), harmonic)

    # Local mean solar noon, expressed in UTC hours, at this longitude.
    solar_noon_utc = (12.0 - longitude_deg / 15.0) % 24
    exp = (solar_noon_utc + EXPECTED[signal]) % (24 / harmonic)
    period = 24 / harmonic
    off = (obs - exp + period / 2) % period - period / 2     # wrap to +/- half a period

    # Distinguish "the clock is wrong" from "there is not enough here to tell".
    # Both fail, but only the first is a data problem.
    if n_days < min_days:
        reason = (f"too few days for a harmonic fit: {n_days} < {min_days}. "
                  f"Not evidence of a bad clock -- widen the window.")
    elif not amp > 0.05:
        reason = (f"signal too weak to fit: amplitude {amp:.3f}. "
                  f"Not evidence of a bad clock.")
    elif abs(off) > tolerance_h:
        reason = (f"offset {off:+.2f} h exceeds the {tolerance_h} h tolerance. "
                  f"Do not ingest this column as UTC.")
    else:
        reason = ""
    return ClockVerdict(signal, n_days, obs, exp, off, amp, not reason, reason)


def assert_utc(times, values, signal, longitude_deg, **kw) -> ClockVerdict:
    v = verify_utc(times, values, signal, longitude_deg, **kw)
    if not v.ok:
        raise AssertionError(
            f"Timestamp column fails the clock check: {v}\n"
            f"  The column is offset from true UTC by about {v.offset_hours:+.1f} h.\n"
            f"  Do not ingest it as UTC. Establish the real zone first."
        )
    return v


# ---------------------------------------------------------------------------
# Reading an arbitrary workbook
#
# Everything below serves the CLI. It resolves which column carries which
# signal and hands the two Series to `verify_utc` above -- it never touches the
# fit, the tolerance or the expected phase.
# ---------------------------------------------------------------------------

# Column-name stems accepted for each signal. These are the NDBC/ERDDAP short
# names the feeds actually use (`config/stations.yaml` maps the same pair), not
# guesses: a column whose stem is not in here is reported as unmatched rather
# than assumed, because assuming which column is air temperature is the same
# class of mistake as assuming which zone a column is in.
COLUMN_ALIASES = {
    "air_temperature": ("atmp", "air_temperature", "airtemp", "air_temp"),
    "air_pressure_at_mean_sea_level": (
        "bar", "baro", "pres", "mslp", "air_pressure",
        "air_pressure_at_mean_sea_level"),
}


def column_stem(name: str) -> str:
    """The comparable part of a column header.

    Feed columns arrive as `atmp (degree_C)` -- name, then a parenthesised
    unit. The unit is real information but it is not identity, and two files
    reporting the same quantity in different units must still match.
    """
    stem = str(name).split("(")[0].strip().lower()
    for ch in " -/.":
        stem = stem.replace(ch, "_")
    return stem.strip("_")


def find_signal_column(columns, signal: str) -> str | None:
    """The column carrying `signal`, or None. Ambiguity is not resolved silently."""
    aliases = COLUMN_ALIASES.get(signal, ())
    hits = [c for c in columns if column_stem(c) in aliases]
    if len(hits) != 1:
        return None
    return hits[0]


def read_time_column(df: pd.DataFrame, time_col: str) -> tuple[pd.Series, bool]:
    """The time column, zone handled explicitly. Returns (series, was_tz_aware).

    A tz-AWARE column is converted to UTC, not stripped. `verify_utc` drops the
    zone on entry, so handing it `15:00-07:00` unconverted would check 15:00 --
    the wall time -- and quietly report a 7 h error against a column that was
    perfectly correct. That is the same shape as the bug this module exists to
    catch, so it is resolved here rather than left to the caller.
    """
    t = pd.to_datetime(df[time_col], errors="coerce")
    if getattr(t.dt, "tz", None) is not None:
        return t.dt.tz_convert("UTC").dt.tz_localize(None), True
    return t, False


def check_workbook(path, sheet, time_col: str, longitude_deg: float,
                   signals=None, value_col: str | None = None,
                   tolerance_h: float = 1.5, min_days: int = 10,
                   shift_hours: float = 0.0, log=print):
    """Run the clock check on one sheet of a workbook. Returns list[ClockVerdict].

    An empty list means nothing checkable was found -- which is a real answer
    about a file, not a pass, and is why the caller distinguishes it from ok.
    """
    path = Path(path)
    df = pd.read_excel(path, sheet_name=sheet)
    if time_col not in df.columns:
        log(f"  no time column {time_col!r} in {sheet!r}. "
            f"columns present: {[str(c) for c in df.columns]}")
        return []

    t, was_aware = read_time_column(df, time_col)
    n_bad = int(t.isna().sum())
    t = t + pd.Timedelta(hours=shift_hours) if shift_hours else t
    good = t.dropna()
    if good.empty:
        log(f"  time column {time_col!r} parsed to nothing usable "
            f"({len(df):,} rows, all unparseable)")
        return []

    # NOT "... UTC". The column is UTC only if it passes, and stamping the
    # designator on it here would assert the very thing under test -- which is
    # how a column labelled `time (UTC)` came to hold Pacific local time in the
    # first place. Say whose claim it is instead.
    origin = ("carried a zone; converted to UTC" if was_aware
              else "no zone; read as UTC -- the claim under test")
    span = f"{good.min():%Y-%m-%d %H:%M} .. {good.max():%Y-%m-%d %H:%M}"
    log(f"  time {time_col!r}  {len(good):,} rows  {span}  [{origin}]"
        + (f"  [{n_bad:,} unparseable]" if n_bad else "")
        + (f"  [SHIFTED +{shift_hours} h -- corrupted fixture]" if shift_hours else ""))
    log(f"  longitude {longitude_deg:+.3f} deg  ->  local mean solar noon "
        f"{(12.0 - longitude_deg / 15.0) % 24:.2f} UTC")

    wanted = list(signals) if signals else list(EXPECTED)
    verdicts = []
    for sig in wanted:
        col = value_col if (value_col and len(wanted) == 1) \
            else find_signal_column(df.columns, sig)
        if col is None:
            # Reported, never dropped: a file with no air temperature in it is
            # a fact about the file, and staying quiet about it would let an
            # unverifiable workbook look like a verified one.
            log(f"  [--  ] {sig:<34} no column found "
                f"(stems accepted: {', '.join(COLUMN_ALIASES.get(sig, ()))})")
            continue
        v = verify_utc(t, df[col], sig, longitude_deg,
                       tolerance_h=tolerance_h, min_days=min_days)
        log(f"  {v}   [column {col!r}]")
        verdicts.append(v)
    return verdicts


# ---------------------------------------------------------------------------
# The repo gate
#
# CLAUDE.md names "clock check on sources/" as one of the checks to run before
# committing anything that touches ingest or time handling. This is that check,
# in the same shape as every other module gate here: `--check`, a table of
# PASS/FAIL, exit non-zero on any failure.
# ---------------------------------------------------------------------------

REQUIRED_SENTENCE = "Do not ingest it as UTC. Establish the real zone first."

# The corrupted fixture is GENERATED, not committed. AGENT_TASK.md 6.4 asks for
# a time column shifted by +7 h; shifting the real 3 MB workbook in memory makes
# "the corrupted fixture still fails" a command anyone can run, and avoids a
# second near-identical binary in the repo that could drift from the first.
FIXTURE_SHIFT_H = 7.0

# A shift of a whole day moves every timestamp while leaving hour-of-day -- and
# therefore the diurnal fit -- untouched. It must still PASS. Without it, a
# generator that mangled the column outright would produce a FAIL at +7 h and
# the gate would read that as success.
CONTROL_SHIFT_H = 24.0


def _anchor(root: Path) -> tuple[str, float]:
    """The clock anchor station and its longitude, from config/stations.yaml.

    Not a literal in this file. Geometry comes from the config -- and a gate
    that hard-coded the longitude would keep passing after someone corrected
    the station's position, while measuring against the old one.
    """
    from .config import load_config
    cfg = load_config(root)
    st = cfg.clock_anchor
    if st is None or st.lon is None:
        raise RuntimeError("no clock_anchor station with a longitude in "
                           "config/stations.yaml")
    return st.id, float(st.lon)


def _check(root: Path) -> int:
    checks: list[tuple[str, bool, str]] = []

    def record(label, ok, note=""):
        checks.append((label, bool(ok), note))

    try:
        anchor, lon = _anchor(root)
        record(f"clock anchor is {anchor} at lon {lon:+.3f} (config/stations.yaml)",
               True)
    except Exception as e:
        record(f"read the clock anchor from config/stations.yaml", False, str(e))
        anchor, lon = "LJAC1", -117.257

    wb = root / "sources" / "ja_jolla_sensors.xlsx"
    quiet = lambda *_a, **_k: None

    if not wb.exists():
        record(f"{wb.relative_to(root)} is present to check", False,
               "missing -- reported rather than skipped in silence")
        clean = {}
    else:
        # --- the real sources verify as UTC -------------------------------
        got = check_workbook(wb, f"src_{anchor}", "time_utc", lon, log=quiet)
        clean = {v.signal: v for v in got}
        record(f"sources/ja_jolla_sensors.xlsx [src_{anchor}] offers both "
               f"clock-checkable signals", len(got) == 2,
               f"found {sorted(clean)}" if len(got) != 2 else "")
        for sig in EXPECTED:
            v = clean.get(sig)
            record(f"{sig:<34} verifies as UTC", bool(v and v.ok),
                   str(v) if v else "signal not found")

        # --- the +7 h fixture still fails ---------------------------------
        got7 = check_workbook(wb, f"src_{anchor}", "time_utc", lon,
                              signals=["air_temperature"],
                              shift_hours=FIXTURE_SHIFT_H, log=quiet)
        v7 = got7[0] if got7 else None
        record(f"the +{FIXTURE_SHIFT_H:g} h corrupted fixture FAILS air_temperature",
               bool(v7 and not v7.ok and not v7.inconclusive),
               str(v7) if v7 else "fixture produced no verdict")

        # A fixture that failed for being inconclusive would satisfy "not ok"
        # while proving nothing. The offset has to be the one that was injected.
        if v7 and clean.get("air_temperature"):
            period = 24 / HARMONIC["air_temperature"]
            base = clean["air_temperature"].offset_hours
            want = (base + FIXTURE_SHIFT_H + period / 2) % period - period / 2
            err = abs(v7.offset_hours - want)
            record(f"and MEASURES the injected shift: {v7.offset_hours:+.2f} h "
                   f"vs {want:+.2f} h expected from {base:+.2f} h + "
                   f"{FIXTURE_SHIFT_H:g} h", err < 0.5, f"differs by {err:.2f} h")

        # --- the required sentence, from assert_utc -----------------------
        df = pd.read_excel(wb, sheet_name=f"src_{anchor}")
        t, _ = read_time_column(df, "time_utc")
        col = find_signal_column(df.columns, "air_temperature")
        try:
            assert_utc(t + pd.Timedelta(hours=FIXTURE_SHIFT_H), df[col],
                       "air_temperature", lon)
            raised = "<did not raise>"
        except AssertionError as e:
            raised = str(e)
        record("assert_utc raises AssertionError carrying "
               f"{REQUIRED_SENTENCE!r}",
               REQUIRED_SENTENCE in raised,
               raised.replace("\n", " ")[:160] if REQUIRED_SENTENCE not in raised
               else "")

        # --- control: the generator is not simply breaking the data -------
        gotc = check_workbook(wb, f"src_{anchor}", "time_utc", lon,
                              signals=["air_temperature"],
                              shift_hours=CONTROL_SHIFT_H, log=quiet)
        vc = gotc[0] if gotc else None
        record(f"control: a +{CONTROL_SHIFT_H:g} h shift is invisible to a "
               f"diurnal fit and still PASSES", bool(vc and vc.ok),
               str(vc) if not (vc and vc.ok) else "")

    # --- the second workbook, classified rather than skipped --------------
    yb = root / "sources" / "yellow_buoy_temps.xlsx"
    if not yb.exists():
        record(f"{yb.name} is present to classify", False, "missing")
    else:
        gy = check_workbook(yb, "Data", "Date-Time (PDT)", lon, log=quiet)
        record("sources/yellow_buoy_temps.xlsx carries NO signal with a known "
               "solar phase, so it gets no verdict", not gy,
               "it is a seabed Tidbit water temperature declared PDT; this "
               "method needs air temperature or pressure"
               if not gy else f"unexpectedly checkable: {[v.signal for v in gy]}")

    print("\nclock check gate:")
    for label, ok, note in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if note:
            print(f"          [{note}]")
    passed = sum(1 for _l, ok, _n in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


def _main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m ingest.clockcheck",
        description="Verify that a workbook's timestamp column really is UTC, "
                    "using the known solar phase of air temperature (primary) "
                    "and barometric pressure (confirming).",
        epilog="exit 0 = verified UTC; 1 = measured outside tolerance; "
               "2 = inconclusive or nothing checkable found.")
    ap.add_argument("--workbook", type=Path, help="path to the .xlsx to check")
    ap.add_argument("--sheet", default=0, help="sheet name (default: the first)")
    ap.add_argument("--time", dest="time_col", default="time_utc",
                    help="the timestamp column under test (default: time_utc)")
    ap.add_argument("--lon", dest="lon", type=float, default=None,
                    help="station longitude in degrees, negative west. Required: "
                         "the expected phase is computed from it and there is no "
                         "safe default")
    ap.add_argument("--signal", action="append", choices=sorted(EXPECTED),
                    help="restrict to one signal; repeatable (default: both)")
    ap.add_argument("--value", dest="value_col", default=None,
                    help="name the value column explicitly, for a file whose "
                         "header this module does not recognise. Only valid "
                         "with a single --signal")
    ap.add_argument("--tolerance-h", type=float, default=1.5)
    ap.add_argument("--min-days", type=int, default=10)
    ap.add_argument("--corrupt-hours", type=float, default=0.0,
                    help="shift the time column by N hours before checking, to "
                         "reproduce the corrupted fixture by hand. "
                         f"--corrupt-hours {FIXTURE_SHIFT_H:g} must FAIL")
    ap.add_argument("--check", action="store_true",
                    help="run the repo gate over sources/ and exit non-zero on "
                         "any failure")
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent,
                    help="repo root, for --check")
    args = ap.parse_args(argv)

    if args.check:
        return _check(args.root)
    if not args.workbook:
        ap.print_help()
        return 0
    if args.lon is None:
        ap.error("--lon is required: the expected solar phase is computed from "
                 "the station's actual longitude, and guessing it would make "
                 "every verdict meaningless")
    if args.value_col and len(args.signal or EXPECTED) != 1:
        ap.error("--value names the column for ONE signal; pass exactly one --signal")

    print(f"\nclock check: {args.workbook}  [sheet {args.sheet!r}]")
    verdicts = check_workbook(args.workbook, args.sheet, args.time_col, args.lon,
                              signals=args.signal, value_col=args.value_col,
                              tolerance_h=args.tolerance_h, min_days=args.min_days,
                              shift_hours=args.corrupt_hours)
    if not verdicts:
        # The specific cause -- missing time column, unparseable times, no
        # recognised signal -- has already been logged above. Naming one of
        # them here would be wrong two times in three.
        print("\nNO VERDICT: nothing in this sheet could be clock-checked, for "
              "the reason given above.\n  That is not the same as passing.")
        return 2
    if all(v.ok for v in verdicts):
        print(f"\n{len(verdicts)}/{len(verdicts)} signals verify as UTC")
        return 0

    bad = [v for v in verdicts if not v.ok]
    measured = [v for v in bad if not v.inconclusive]
    print(f"\n{len(verdicts) - len(bad)}/{len(verdicts)} signals verify as UTC")
    if measured:
        primary = max(measured, key=lambda v: abs(v.offset_hours))
        print(f"  The column is offset from true UTC by about "
              f"{primary.offset_hours:+.1f} h.\n"
              f"  Do not ingest it as UTC. Establish the real zone first.")
        return 1
    print("  Inconclusive -- the fit had nothing to work with. Not evidence of "
          "a bad clock; widen the window or pick a stronger signal.")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
