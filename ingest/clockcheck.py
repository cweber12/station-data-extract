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
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

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
