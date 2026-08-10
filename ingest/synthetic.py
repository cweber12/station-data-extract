"""
ingest/synthetic.py -- solar-phased series, generated rather than read off disk.

WHY GENERATED
    The clock checks are the only thing standing between this project and the
    mistake it was built to correct: a `time (UTC)` column that was not UTC.
    Testing them needs a series whose true phase is KNOWN, and until now that
    came from two workbooks in `sources/` -- 3.2 MB of cached Power Query
    results pinned into the repo forever because two gates read them.

    A file cannot state its own offset. Generating the series means the
    corruption is declared in the call (`shift_h=7.0`) instead of being a
    property of a binary nobody can inspect, and the same generator can put a
    signal at any longitude, which no fixture file can.

WHAT IT IS NOT
    These values are a phase to fit a harmonic to. They are NOT a claim about
    the atmosphere or the ocean, and nothing should read them as one.

THE DUPLICATION IN `solar_noon_hour_utc` IS DELIBERATE -- see its docstring.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def solar_noon_hour_utc(longitude_deg: float) -> float:
    """UTC hour of local MEAN solar noon at `longitude_deg` (negative west).

    A SECOND, INDEPENDENT DERIVATION, ON PURPOSE. `clockcheck.verify_utc`
    computes the same quantity for itself and this does not call it, nor it
    this.

    Sharing one implementation would make the longitude term cancel out of
    every gate: data built with a given mistake, checked by a verifier making
    the identical mistake, reports a perfect zero offset. A verifier that
    ignored longitude altogether would look flawless. The duplication is what
    makes generating at two longitudes mean anything at all.
    """
    return (12.0 - longitude_deg / 15.0) % 24.0


def solar_series(*, start: str, days: int, longitude_deg: float,
                 peak_hours_after_solar_noon: float, harmonic: int = 1,
                 amplitude: float = 3.0, mean: float = 18.0,
                 interval_min: int = 60, shift_h: float = 0.0,
                 noise: float = 0.0, seed: int = 0
                 ) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """A series peaking a stated number of hours after solar noon.

    Returns `(times, values)` with `times` tz-aware UTC.

    `shift_h` is the corruption, and it moves the STAMPS, not the values: the
    physics happens when it happens, and the column claims an instant
    `shift_h` hours later. That is the shape of the original bug -- Pacific
    local time carried in a column labelled UTC -- and it is the sign
    convention `ClockVerdict.offset_hours` uses, where positive means the
    column runs ahead of true UTC.

    `noise` is drawn from a seeded generator so a gate built on this gives the
    same answer twice. An unseeded fixture that fails one run in fifty is
    worse than no fixture.
    """
    true_times = pd.date_range(start, periods=int(days * 24 * 60 / interval_min),
                               freq=f"{interval_min}min", tz="UTC")

    peak_hour = solar_noon_hour_utc(longitude_deg) + peak_hours_after_solar_noon
    hours = true_times.hour + true_times.minute / 60.0
    values = mean + amplitude * np.cos(
        2 * np.pi * harmonic * (hours - peak_hour) / 24.0)

    if noise:
        values = values + np.random.default_rng(seed).normal(0.0, noise,
                                                             len(values))

    return true_times + pd.Timedelta(hours=shift_h), np.asarray(values, float)
