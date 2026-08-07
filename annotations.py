"""
annotations.py -- the on-disk store for marks drawn on a chart.

WHAT A MARK IS, AND WHAT IT IS NOT
    A mark is INTERPRETATION: a human looked at a chart, saw a window worth
    pointing at, and said why. It is not evidence, and it is emphatically not a
    QC flag. QARTOD flags come from the provider and describe the instrument;
    a mark describes what someone thought. Nothing here writes into `cache/`,
    and that is the same reason marks live in `<producer>/annotations/` rather
    than as an exception carved inside the evidence directory. See the
    IMMUTABILITY section of study.py.

STANDARD LIBRARY ONLY
    This module imports nothing outside the standard library -- no pandas, no
    tkinter, no matplotlib, not even `study`. That is deliberate and it is
    checked by `importgate.py`:

      * the store is a pure function of a DIRECTORY PATH, so a gate can point
        it at a temp directory with no study in sight, and the marking rules
        can be exercised without opening a window;
      * numbers that need pandas to compute -- how many intervals under a mark
        are empty, how many QARTOD-3 values it spans -- are computed by the
        CALLER and handed over as plain ints. This module stores them and
        never derives them.

    It is why `_slug` is three lines here rather than an import of
    `study.slugify`: taking that one function would drag pandas and pyyaml into
    a module whose whole value is being light enough to test anywhere.

TIME
    Display is local. Storage is UTC with an explicit offset. Every byte on
    disk is UTC; every string a human reads is local.

    A naive timestamp is REJECTED on load, and the rejection names the file,
    the field and the problem. This is the most expensive lesson in this
    project encoded as a schema rule: the original workbook's `time (UTC)`
    columns held Pacific local time, and every conclusion built on them was
    wrong. A timestamp that does not say what zone it is in cannot be trusted,
    however plausible its field name.

    An explicit non-UTC offset (`-07:00`) is ACCEPTED and normalised to UTC. It
    is unambiguous, which is the only thing being asked for. What is refused is
    silence about the zone, not a particular spelling of it.

    `start_local` / `end_local` are written beside the UTC values purely so the
    JSON reads correctly to a person opening it. THE LOADER IGNORES THEM
    COMPLETELY -- they are derived, never a source of truth, and the gate
    proves it by loading a file whose local field has been falsified.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

# The zone marks are RENDERED in. Spelled here rather than imported because
# this module refuses to depend on anything heavier than the standard library;
# the same constant already appears in compare, exporter, sensorkit and view,
# and consolidating all five is a refactor of its own, not this feature's work.
DISPLAY_TZ = "America/Los_Angeles"

# Band colours, deliberately DISJOINT from identity.SERIES_COLORS and chosen
# from a different family: these are muted, those are saturated. A band sits
# behind the lines, and a band that could be mistaken for a line would put a
# claim about a WINDOW into the visual language of a MEASUREMENT. The gate
# asserts both the disjointness and the saturation gap.
SET_COLORS = ["6E7B8B", "97786A", "5F8A8B", "8E6C9E", "858C63", "A47F72"]

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AnnotationError(ValueError):
    """A stored annotation could not be trusted. The message names the problem.

    Never raised with a bare "invalid" -- a rejection that does not say what is
    wrong with which field of which file leaves someone to guess, and guessing
    at a timestamp is how this project acquired the bug that the zone rules
    above exist to prevent.
    """


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def parse_utc(value, where: str) -> dt.datetime:
    """An ISO-8601 string with an explicit offset -> a UTC datetime.

    `where` names the field being read, so a rejection points at a place in a
    file rather than at the file as a whole.
    """
    if not isinstance(value, str) or not value.strip():
        raise AnnotationError(
            f"{where}: expected an ISO-8601 timestamp string, got {value!r}.")
    text = value.strip()

    if _DATE_ONLY.match(text):
        raise AnnotationError(
            f"{where}: {text!r} is a date with no time of day, so it names a "
            f"24-hour range rather than an instant. Give a full timestamp with "
            f"an explicit offset, e.g. '{text}T00:00:00+00:00'.")

    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise AnnotationError(
            f"{where}: {text!r} is not an ISO-8601 timestamp ({exc}). Expected "
            f"something like '2026-07-20T21:00:00+00:00'.") from None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnnotationError(
            f"{where}: {text!r} carries no timezone, so it is ambiguous -- it "
            f"could be UTC, or Pacific local, or anything else, and nothing in "
            f"the file says which. In this project's original workbook a column "
            f"named `time (UTC)` held Pacific local time and every conclusion "
            f"built on it was wrong, which is why a naive timestamp is refused "
            f"rather than assumed. Store UTC with an explicit offset -- "
            f"'{text}+00:00' if it really is UTC.")

    return _to_plain_utc(parsed)


def _to_plain_utc(value: dt.datetime) -> dt.datetime:
    """A tz-aware datetime -> a plain `datetime` in UTC.

    Rebuilt field by field rather than returned as-is so that a pandas
    Timestamp (a datetime subclass) becomes an ordinary datetime. Without this,
    what sits in memory after a write would not compare equal to what comes
    back off disk, and "round-tripping a set returns identical intervals" would
    be true only by luck of type coercion. Microsecond precision is kept, which
    is finer than any interval this tool bins to.
    """
    u = value.astimezone(dt.timezone.utc)
    return dt.datetime(u.year, u.month, u.day, u.hour, u.minute, u.second,
                       u.microsecond, tzinfo=dt.timezone.utc)


def coerce_utc(value, where: str) -> dt.datetime:
    """Accept a string or a tz-aware datetime; refuse a naive one."""
    if isinstance(value, str):
        return parse_utc(value, where)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise AnnotationError(
                f"{where}: {value!r} is a naive datetime. Nothing records what "
                f"zone it is in, so it cannot be stored. Attach one explicitly.")
        return _to_plain_utc(value)
    raise AnnotationError(
        f"{where}: expected a timestamp, got {type(value).__name__} {value!r}.")


def format_utc(t: dt.datetime) -> str:
    """'2026-07-20T21:00:00+00:00' -- the stored form, always UTC."""
    return _to_plain_utc(t).isoformat()


def format_local(t: dt.datetime, tzname: str = DISPLAY_TZ) -> str:
    """'2026-07-20T14:00:00-07:00' -- derived, for a human reading the file."""
    return _to_plain_utc(t).astimezone(ZoneInfo(tzname)).isoformat()


def local_text(t: dt.datetime, tzname: str = DISPLAY_TZ) -> str:
    """'2026-07-20 14:00' -- what a label or a readout shows."""
    return _to_plain_utc(t).astimezone(ZoneInfo(tzname)).strftime(
        "%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# the records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SeriesRef:
    """One member of the pair a set was drawn against.

    BOTH halves are stored on purpose. `key` is `file::table::column` and is
    what resolves back to real data -- it is what lets the absent partner of a
    pair be re-fetched later for the ghost line. `label` is `table.column` and
    is what a human or an agent reads; it still communicates if a source file
    is renamed and the key stops resolving, which is the moment the label is
    worth most.
    """
    key: str
    label: str

    def to_json(self) -> dict:
        return {"key": self.key, "label": self.label}

    @staticmethod
    def from_json(raw, where: str) -> SeriesRef:
        if not isinstance(raw, dict):
            raise AnnotationError(
                f"{where}: expected a series reference object, got {raw!r}.")
        for f in ("key", "label"):
            if not isinstance(raw.get(f), str) or not raw[f].strip():
                raise AnnotationError(
                    f"{where}.{f}: missing. A series reference carries both a "
                    f"resolvable key and a display label; neither substitutes "
                    f"for the other.")
        return SeriesRef(key=raw["key"].strip(), label=raw["label"].strip())


@dataclass
class Interval:
    """One occurrence of a marked region.

    `series` is RESERVED: unset means the mark applies to both series of the
    pair. Nothing reads it yet. It exists so that narrowing a mark to one
    series later is a data change rather than a schema migration.

    `coverage` records what the data underneath actually was, captured at
    confirmation, keyed by display label. See `coverage_entry`.
    """
    start_utc: dt.datetime
    end_utc: dt.datetime
    series: str | None = None
    coverage: dict = field(default_factory=dict)

    @property
    def duration(self) -> dt.timedelta:
        return self.end_utc - self.start_utc

    def to_json(self, tzname: str = DISPLAY_TZ) -> dict:
        return {
            "start_utc": format_utc(self.start_utc),
            "end_utc": format_utc(self.end_utc),
            # Derived, informational, NEVER read back. See the module docstring.
            "start_local": format_local(self.start_utc, tzname),
            "end_local": format_local(self.end_utc, tzname),
            "series": self.series,
            "coverage": self.coverage,
        }

    @staticmethod
    def from_json(raw, where: str) -> Interval:
        if not isinstance(raw, dict):
            raise AnnotationError(f"{where}: expected an interval object, "
                                  f"got {raw!r}.")
        start = parse_utc(raw.get("start_utc"), f"{where}.start_utc")
        end = parse_utc(raw.get("end_utc"), f"{where}.end_utc")
        if end <= start:
            raise AnnotationError(
                f"{where}: ends at or before it starts "
                f"({format_utc(start)} -> {format_utc(end)}). An interval with "
                f"no duration marks nothing.")
        series = raw.get("series")
        if series is not None and not isinstance(series, str):
            raise AnnotationError(
                f"{where}.series: expected a series label or null, "
                f"got {series!r}.")
        return Interval(start_utc=start, end_utc=end, series=series,
                        coverage=_coverage_from_json(raw.get("coverage"),
                                                     f"{where}.coverage"))


def coverage_entry(n_intervals: int, n_empty: int,
                   n_suspect_kept: int | None) -> dict:
    """What the data under a mark was, for one series.

    `n_suspect_kept` is None where the series carries NO QARTOD flags at all --
    CO-OPS water level and the yellow buoy logger both report `qc_flag` as
    null. Recording 0 there would be a fresh lie: it means NOT EVALUATED, not
    clean, and the two must not render the same way.

    `n_empty` matters because QARTOD screening happens at ingest, not here:
    flags 4 and 9 were masked to null before the parquet was written, so a gap
    under a mark can be a rejected instrument rather than a quiet ocean, and
    the chart cannot tell you which.
    """
    return {"n_intervals": int(n_intervals), "n_empty": int(n_empty),
            "n_suspect_kept": (None if n_suspect_kept is None
                               else int(n_suspect_kept))}


def _coverage_from_json(raw, where: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AnnotationError(f"{where}: expected an object keyed by series "
                              f"label, got {raw!r}.")
    out = {}
    for label, entry in raw.items():
        if not isinstance(entry, dict):
            raise AnnotationError(f"{where}[{label!r}]: expected an object, "
                                  f"got {entry!r}.")
        for f in ("n_intervals", "n_empty"):
            if not isinstance(entry.get(f), int) or isinstance(entry.get(f), bool):
                raise AnnotationError(
                    f"{where}[{label!r}].{f}: expected a whole number, "
                    f"got {entry.get(f)!r}.")
        susp = entry.get("n_suspect_kept")
        if susp is not None and (not isinstance(susp, int)
                                 or isinstance(susp, bool)):
            raise AnnotationError(
                f"{where}[{label!r}].n_suspect_kept: expected a whole number "
                f"or null, got {susp!r}. Null means the series carries no "
                f"QARTOD flags at all, which is not the same as zero.")
        out[label] = coverage_entry(entry["n_intervals"], entry["n_empty"], susp)
    return out


@dataclass
class MarkSet:
    """Several occurrences of ONE named thing, on one pair, in one study.

    The set is the unit a name and a reason attach to, so a recurring feature
    reads as one phenomenon with many occurrences rather than as a scatter of
    unrelated bands.
    """
    set_id: str
    name: str
    reason: str
    color: str
    created_utc: dt.datetime
    study_id: str
    pair: tuple[SeriesRef, SeriesRef]
    tz: str = DISPLAY_TZ
    intervals: list[Interval] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @property
    def pair_keys(self) -> frozenset:
        return frozenset(r.key for r in self.pair)

    @property
    def pair_text(self) -> str:
        return " × ".join(r.label for r in self.pair)

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "set_id": self.set_id,
            "name": self.name,
            "reason": self.reason,
            "color": self.color,
            "created_utc": format_utc(self.created_utc),
            "study_id": self.study_id,
            "tz": self.tz,
            "pair": [r.to_json() for r in self.pair],
            "intervals": [i.to_json(self.tz) for i in self.intervals],
        }

    @staticmethod
    def from_json(raw, where: str) -> MarkSet:
        if not isinstance(raw, dict):
            raise AnnotationError(f"{where}: expected an object, got {raw!r}.")

        def text(f, required=True):
            v = raw.get(f)
            if v is None and not required:
                return ""
            if not isinstance(v, str) or (required and not v.strip()):
                raise AnnotationError(f"{where}.{f}: expected text, got {v!r}.")
            return v

        pair_raw = raw.get("pair")
        if not isinstance(pair_raw, list) or len(pair_raw) != 2:
            raise AnnotationError(
                f"{where}.pair: expected exactly two series references, got "
                f"{0 if pair_raw is None else len(pair_raw)}. A set records the "
                f"PAIR it was drawn against; without both, its bands could be "
                f"mistaken for a claim about series that were not on screen.")
        pair = (SeriesRef.from_json(pair_raw[0], f"{where}.pair[0]"),
                SeriesRef.from_json(pair_raw[1], f"{where}.pair[1]"))

        intervals_raw = raw.get("intervals")
        if not isinstance(intervals_raw, list):
            raise AnnotationError(
                f"{where}.intervals: expected a list, got "
                f"{intervals_raw!r}.")

        tzname = raw.get("tz") or DISPLAY_TZ
        try:
            ZoneInfo(tzname)
        except Exception as exc:
            raise AnnotationError(
                f"{where}.tz: {tzname!r} is not a known time zone ({exc}).") \
                from None

        return MarkSet(
            set_id=text("set_id"),
            name=text("name"),
            reason=text("reason", required=False),
            color=text("color"),
            created_utc=parse_utc(raw.get("created_utc"),
                                  f"{where}.created_utc"),
            study_id=text("study_id"),
            pair=pair,
            tz=tzname,
            intervals=[Interval.from_json(r, f"{where}.intervals[{i}]")
                       for i, r in enumerate(intervals_raw)],
            schema_version=int(raw.get("schema_version") or SCHEMA_VERSION),
        )


# ---------------------------------------------------------------------------
# pure rules -- everything below is testable without a window or a study
# ---------------------------------------------------------------------------

def sets_for_pair(sets, keys, study_id: str | None = None) -> list:
    """The sets drawn against exactly this pair. Order-insensitive.

    Matching is on the resolvable KEY, never the display label -- two series
    can be relabelled and still be the same data.

    `study_id` must match when given, and callers should give it.
    `ColumnInfo.key` is not unique across studies: `scan_parquet` sets
    `file=observations.parquet` for every study, so the same station and
    variable in two studies produce the identical key. The study id is the only
    thing separating them, which is the mechanical reason cross-study overlay
    is out of scope.
    """
    want = frozenset(keys)
    return [s for s in sets
            if s.pair_keys == want
            and (study_id is None or s.study_id == study_id)]


def clip_to_window(intervals, lo: dt.datetime, hi: dt.datetime):
    """(intervals clamped to the window, count omitted entirely).

    Both halves are returned because "nothing is dropped silently" has to be an
    assertion rather than an intention. A mark drawn on a wide window and
    revisited on a narrow one can fall entirely outside it, and a band that
    quietly disappears reads as a region nobody ever marked.

    An interval overlapping an edge is KEPT and clamped, so it draws to the
    edge of the chart rather than vanishing. Only intervals with no overlap at
    all are counted as omitted.
    """
    kept, omitted = [], 0
    for iv in intervals:
        if iv.end_utc <= lo or iv.start_utc >= hi:
            omitted += 1
            continue
        kept.append(replace(iv,
                            start_utc=max(iv.start_utc, lo),
                            end_utc=min(iv.end_utc, hi)))
    return kept, omitted


def snap_span(x0: float, x1: float, xnum) -> tuple[int, int]:
    """A dragged span -> the SAMPLE INDICES nearest each end.

    This is the whole reason a drag never becomes a timezone bug. The chart's x
    axis is naive local time, so the obvious conversion -- float to local
    datetime, attach America/Los_Angeles, convert to UTC -- has to resolve a
    wall time, and on the November fall-back 01:30 local happens twice while on
    the March spring-forward 02:30 local never happens at all. `zoneinfo`
    answers both silently.

    So no conversion happens. The plotted x values and the UTC index are
    derived from each other position by position, so the drag is resolved to an
    INDEX here and the caller reads the instant straight out of the UTC index.
    Nothing infers a zone, so nothing can be ambiguous about it.

    `xnum` must be ascending. A right-to-left drag gives the same answer as the
    same drag left-to-right. Equal indices mean the drag was shorter than one
    sample, and the caller is expected to refuse it rather than store a mark of
    no duration.
    """
    if len(xnum) == 0:
        raise ValueError("no samples to snap to")
    lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
    return _nearest(xnum, lo), _nearest(xnum, hi)


def _nearest(xnum, x: float) -> int:
    """Index of the closest value in an ascending sequence. Clamps at the ends."""
    n = len(xnum)
    j = bisect.bisect_left(xnum, x)
    if j <= 0:
        return 0
    if j >= n:
        return n - 1
    return j if (xnum[j] - x) < (x - xnum[j - 1]) else j - 1


def _slug(label: str) -> str:
    """Windows-safe, sortable, no surprises in a path.

    Deliberately not `study.slugify`: see the module docstring on why this
    module refuses to import anything heavier than the standard library.
    """
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(label or "").strip()).strip("-").lower()
    return (s or "set")[:48]


def next_color(sets) -> str:
    """The first unused band colour, cycling once the palette is exhausted."""
    used = {s.color for s in sets}
    for c in SET_COLORS:
        if c not in used:
            return c
    return SET_COLORS[len(list(sets)) % len(SET_COLORS)]


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

class Store:
    """Annotation sets in one directory. One JSON file per set.

    A directory rather than a single file so that adding one set never rewrites
    another -- a crash while saving set B must not be able to damage set A.
    """

    def __init__(self, directory):
        self.dir = Path(directory)

    # ------------------------------------------------------------- reading

    def path_for(self, set_id: str) -> Path:
        return self.dir / f"{set_id}.json"

    def load_file(self, path) -> MarkSet:
        """One set. Raises AnnotationError naming the file and the problem."""
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AnnotationError(f"{path.name}: not valid JSON ({exc}).") \
                from None
        except OSError as exc:
            raise AnnotationError(f"{path.name}: could not be read ({exc}).") \
                from None
        return MarkSet.from_json(raw, path.name)

    def load_all(self) -> tuple[list, list]:
        """(sets, rejections). Newest last.

        A bad file is REPORTED, not skipped and not fatal. Skipping it silently
        would drop a mark without saying so; raising would let one bad file
        hide every good one. The caller is expected to show the rejections.
        """
        sets, problems = [], []
        if not self.dir.is_dir():
            return sets, problems
        for p in sorted(self.dir.glob("*.json")):
            try:
                sets.append(self.load_file(p))
            except AnnotationError as exc:
                problems.append(str(exc))
        sets.sort(key=lambda s: (s.created_utc, s.set_id))
        return sets, problems

    # ------------------------------------------------------------- writing

    def save(self, ms: MarkSet) -> Path:
        """Write one set. Atomic: a crash mid-write leaves the old file intact."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(ms.set_id)
        tmp = path.with_suffix(".json.tmp")
        payload = json.dumps(ms.to_json(), indent=2) + "\n"
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
        return path

    def confirm(self, *, study_id: str, pair, name: str, reason: str,
                start_utc, end_utc, coverage=None, tz: str = DISPLAY_TZ,
                now=None) -> MarkSet:
        """Record one marked region, and write it immediately.

        There is no unsaved state anywhere in this feature. Closing the window
        loses nothing, the main window closing first loses nothing, and a crash
        loses nothing, because by the time this returns the mark is on disk.

        GROUPING is by name within a pair: confirming with the name of a set
        that already exists on this pair appends an occurrence to it rather
        than starting a second set. That is what makes a recurring feature read
        as one phenomenon. A changed reason updates the set's -- the reason
        belongs to the set, and interpretation is expected to be revised.
        """
        start = coerce_utc(start_utc, "start_utc")
        end = coerce_utc(end_utc, "end_utc")
        if end <= start:
            raise AnnotationError(
                f"a mark must have a duration; got {format_utc(start)} -> "
                f"{format_utc(end)}. A drag shorter than one sample snaps to a "
                f"single point and marks nothing.")

        name = (name or "").strip()
        if not name:
            raise AnnotationError(
                "a mark needs a name. Its meaning has to survive outside your "
                "head, and an unnamed band cannot say anything to anyone.")

        pair = tuple(pair)
        if len(pair) != 2:
            raise AnnotationError(
                f"a set records the pair it was drawn against; got "
                f"{len(pair)} series, not 2.")

        existing, _problems = self.load_all()
        mine = sets_for_pair(existing, [r.key for r in pair], study_id)
        match = next((s for s in mine if s.name == name), None)

        interval = Interval(start_utc=start, end_utc=end,
                            coverage=dict(coverage or {}))

        if match is not None:
            match.intervals.append(interval)
            match.intervals.sort(key=lambda i: i.start_utc)
            if reason and reason.strip() != match.reason:
                match.reason = reason.strip()
            self.save(match)
            return match

        created = _to_plain_utc(now or dt.datetime.now(dt.timezone.utc))
        taken = {s.set_id for s in existing}
        base = f"{created.strftime('%Y%m%dT%H%M%SZ')}__{_slug(name)}"
        set_id, n = base, 2
        while set_id in taken or self.path_for(set_id).exists():
            set_id, n = f"{base}-{n}", n + 1

        ms = MarkSet(set_id=set_id, name=name, reason=(reason or "").strip(),
                     color=next_color(existing), created_utc=created,
                     study_id=study_id, pair=pair, tz=tz, intervals=[interval])
        self.save(ms)
        return ms


# ---------------------------------------------------------------------------
# Gate. `python annotations.py --check` exercises every rule above against a
# temp directory -- no study, no window, no plotting backend.
# ---------------------------------------------------------------------------

def _main(argv=None):
    import argparse
    import shutil
    import tempfile

    ap = argparse.ArgumentParser(
        description="Annotation store: schema, validation, round-trip.")
    ap.add_argument("--check", action="store_true",
                    help="run the gate and exit non-zero on failure")
    ap.add_argument("--dir", default=None,
                    help="list the sets in an annotations directory")
    args = ap.parse_args(argv)

    if args.dir:
        store = Store(args.dir)
        sets, problems = store.load_all()
        for s in sets:
            print(f"{s.set_id}  #{s.color}  {s.name!r}  {s.pair_text}  "
                  f"{len(s.intervals)} interval(s)")
            for i in s.intervals:
                print(f"    {local_text(i.start_utc, s.tz)} -> "
                      f"{local_text(i.end_utc, s.tz)} local")
        for p in problems:
            print(f"REJECTED  {p}")
        return 1 if problems else 0

    if not args.check:
        ap.print_help()
        return 0

    checks = []

    def ok(what, cond, detail=""):
        checks.append((what, bool(cond), detail))

    def rejects(what, fn, *must_mention):
        """Assert fn() raises, and that the message names the problem."""
        try:
            fn()
        except AnnotationError as exc:
            msg = str(exc)
            missing = [m for m in must_mention if m.lower() not in msg.lower()]
            ok(what, not missing,
               msg if not missing else f"message omits {missing}: {msg}")
        except Exception as exc:               # wrong type is still a failure
            ok(what, False, f"raised {type(exc).__name__}, not AnnotationError:"
                            f" {exc}")
        else:
            ok(what, False, "no rejection at all")

    tmp = Path(tempfile.mkdtemp(prefix="annotations-gate-"))
    try:
        store = Store(tmp)
        pair = (SeriesRef("observations.parquet::LJAC1::sea_water_temperature",
                          "LJAC1.sea_water_temperature"),
                SeriesRef("observations.parquet::LJAC1::water_level",
                          "LJAC1.water_level"))
        other = (SeriesRef("observations.parquet::46254::sea_water_temperature",
                           "46254.sea_water_temperature"),
                 SeriesRef("observations.parquet::LJAC1::water_level",
                           "LJAC1.water_level"))
        study_id = "20260805T2352Z__yellow-buoy"
        utc = dt.timezone.utc

        # ---- round trip ---------------------------------------------------
        cov = {"LJAC1.sea_water_temperature": coverage_entry(6, 0, 2),
               "LJAC1.water_level": coverage_entry(6, 5, None)}
        reason = "temperature dips while water level is still rising"
        # The instants that go in, kept independently of anything the store
        # hands back -- comparing the store against itself would pass even if
        # every timestamp were mangled the same way on the way in and out.
        sent = [(dt.datetime(2026, 7, 20, 21, 0, tzinfo=utc),
                 dt.datetime(2026, 7, 21, 3, 0, tzinfo=utc)),
                (dt.datetime(2026, 7, 21, 20, 0, tzinfo=utc),
                 dt.datetime(2026, 7, 22, 2, 0, tzinfo=utc)),
                (dt.datetime(2026, 7, 22, 19, 0, tzinfo=utc),
                 dt.datetime(2026, 7, 23, 1, 0, tzinfo=utc))]
        first = store.confirm(
            study_id=study_id, pair=pair, name="internal tide", reason=reason,
            start_utc=sent[0][0], end_utc=sent[0][1], coverage=cov)
        for start, end in sent[1:]:
            # Empty reason on purpose: an occurrence appended to an existing set
            # must not blank out the reason the set already carries.
            store.confirm(study_id=study_id, pair=pair, name="internal tide",
                          reason="", start_utc=start, end_utc=end)

        loaded, problems = store.load_all()
        ok("no rejections on a store we just wrote", not problems, problems)
        ok("three occurrences grouped into ONE named set",
           len(loaded) == 1 and len(loaded[0].intervals) == 3,
           f"{len(loaded)} set(s), "
           f"{len(loaded[0].intervals) if loaded else 0} interval(s)")

        back = loaded[0]
        ok("round trip returns IDENTICAL intervals",
           [(i.start_utc, i.end_utc) for i in back.intervals] == sent,
           " / ".join(f"{format_utc(a)}->{format_utc(b)}"
                      for a, b in ((i.start_utc, i.end_utc)
                                   for i in back.intervals)))
        ok("round trip preserves name, colour, study, pair and tz",
           (back.name, back.color, back.study_id, back.tz, back.pair) ==
           (first.name, first.color, first.study_id, first.tz, first.pair))
        ok("an appended occurrence does not blank the set's reason",
           back.reason == reason, back.reason)
        ok("round trip preserves coverage, and null suspect stays NULL "
           "(not evaluated != clean)",
           back.intervals[0].coverage == cov and
           back.intervals[0].coverage["LJAC1.water_level"]["n_suspect_kept"]
           is None,
           str(back.intervals[0].coverage))
        ok("the reserved per-interval `series` field survives, unread",
           all(i.series is None for i in back.intervals))

        # ---- writing one set never rewrites another ------------------------
        second = store.confirm(
            study_id=study_id, pair=pair, name="fouling",
            reason="flat line, looks like the sensor stopped responding",
            start_utc=dt.datetime(2026, 7, 25, 12, 0, tzinfo=utc),
            end_utc=dt.datetime(2026, 7, 26, 12, 0, tzinfo=utc))
        ok("a second name starts a SECOND set, in a different colour",
           second.set_id != first.set_id and second.color != first.color,
           f"{first.color} vs {second.color}")

        before_bytes = store.path_for(first.set_id).read_bytes()
        store.confirm(
            study_id=study_id, pair=pair, name="fouling", reason="",
            start_utc=dt.datetime(2026, 7, 28, 12, 0, tzinfo=utc),
            end_utc=dt.datetime(2026, 7, 29, 12, 0, tzinfo=utc))
        ok("appending to one set leaves the other file byte-identical",
           store.path_for(first.set_id).read_bytes() == before_bytes)

        # ---- the local fields are derived and never read --------------------
        p = store.path_for(first.set_id)
        raw = json.loads(p.read_text(encoding="utf-8"))
        ok("the file carries a local rendering beside the UTC values",
           raw["intervals"][0]["start_local"].startswith("2026-07-20T14:00:00-07:00"),
           raw["intervals"][0]["start_local"])
        raw["intervals"][0]["start_local"] = "1999-01-01T00:00:00-08:00"
        p.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        refetched = store.load_file(p)
        ok("a FALSIFIED start_local does not affect the loaded interval",
           refetched.intervals[0].start_utc ==
           dt.datetime(2026, 7, 20, 21, 0, tzinfo=utc),
           format_utc(refetched.intervals[0].start_utc))

        # ---- offsets --------------------------------------------------------
        want = dt.datetime(2026, 7, 20, 21, 0, tzinfo=utc)
        spellings = ["2026-07-20T21:00:00+00:00", "2026-07-20T21:00:00Z",
                     "2026-07-20T14:00:00-07:00"]
        ok("Z, +00:00 and -07:00 all land on the same instant",
           all(parse_utc(s, "t") == want for s in spellings),
           " / ".join(spellings))

        # ---- rejections, each naming the problem ----------------------------
        rejects("a zone-less timestamp is rejected, naming the field and why",
                lambda: parse_utc("2026-07-20T21:00:00", "intervals[0].start_utc"),
                "intervals[0].start_utc", "no timezone", "ambiguous",
                "Pacific local time")
        rejects("a date with no time of day is rejected",
                lambda: parse_utc("2026-07-20", "intervals[0].end_utc"),
                "intervals[0].end_utc", "no time of day")
        rejects("an unparseable timestamp is rejected",
                lambda: parse_utc("last tuesday", "created_utc"),
                "created_utc", "not an ISO-8601 timestamp")
        rejects("a naive datetime handed in by a caller is rejected",
                lambda: coerce_utc(dt.datetime(2026, 7, 20, 21, 0), "start_utc"),
                "start_utc", "naive")
        rejects("an interval ending before it starts is rejected",
                lambda: Interval.from_json(
                    {"start_utc": "2026-07-20T21:00:00+00:00",
                     "end_utc": "2026-07-20T20:00:00+00:00"}, "intervals[0]"),
                "intervals[0]", "before it starts")
        rejects("a zero-length drag is refused rather than stored",
                lambda: store.confirm(
                    study_id=study_id, pair=pair, name="x", reason="",
                    start_utc=want, end_utc=want),
                "must have a duration")
        rejects("an unnamed mark is refused",
                lambda: store.confirm(
                    study_id=study_id, pair=pair, name="  ", reason="",
                    start_utc=want,
                    end_utc=dt.datetime(2026, 7, 20, 23, 0, tzinfo=utc)),
                "needs a name")
        rejects("suspect count of the wrong type is rejected, and the message "
                "says null != zero",
                lambda: _coverage_from_json(
                    {"a": {"n_intervals": 6, "n_empty": 0,
                           "n_suspect_kept": "two"}}, "coverage"),
                "n_suspect_kept", "null means", "not the same as zero")
        rejects("a set missing half its pair is rejected",
                lambda: MarkSet.from_json(
                    {"set_id": "x", "name": "n", "color": "6E7B8B",
                     "created_utc": "2026-07-20T21:00:00+00:00",
                     "study_id": "s", "pair": [pair[0].to_json()],
                     "intervals": []}, "bad.json"),
                "pair", "exactly two series references")

        # a bad file is reported, not fatal, and does not hide the good ones
        (tmp / "broken.json").write_text(json.dumps({
            "set_id": "broken", "name": "n", "color": "6E7B8B",
            "created_utc": "2026-07-20T21:00:00+00:00", "study_id": study_id,
            "pair": [pair[0].to_json(), pair[1].to_json()],
            "intervals": [{"start_utc": "2026-07-20 21:00:00",
                           "end_utc": "2026-07-20T23:00:00+00:00"}],
        }, indent=2), encoding="utf-8")
        good, bad = store.load_all()
        ok("one bad file is REPORTED and does not hide the good ones",
           len(good) == 2 and len(bad) == 1 and "broken.json" in bad[0],
           f"{len(good)} loaded, {len(bad)} rejected: "
           f"{bad[0][:70] if bad else ''}...")

        # ---- pair matching --------------------------------------------------
        keys = [r.key for r in pair]
        ok("sets_for_pair matches regardless of the order of the pair",
           len(sets_for_pair(good, list(reversed(keys)), study_id)) == 2)
        ok("a set drawn on a DIFFERENT pair is not returned",
           sets_for_pair(good, [r.key for r in other], study_id) == [])
        ok("a set from a different study is not returned",
           sets_for_pair(good, keys, "20260805T0544Z__baseline") == [])

        # ---- clipping -------------------------------------------------------
        ivs = [Interval(dt.datetime(2026, 7, 20, 0, tzinfo=utc),
                        dt.datetime(2026, 7, 20, 6, tzinfo=utc)),   # inside
               Interval(dt.datetime(2026, 7, 19, 20, tzinfo=utc),
                        dt.datetime(2026, 7, 20, 2, tzinfo=utc)),   # straddles
               Interval(dt.datetime(2026, 7, 10, 0, tzinfo=utc),
                        dt.datetime(2026, 7, 11, 0, tzinfo=utc))]   # outside
        lo = dt.datetime(2026, 7, 19, 22, tzinfo=utc)
        hi = dt.datetime(2026, 7, 21, 0, tzinfo=utc)
        kept, omitted = clip_to_window(ivs, lo, hi)
        ok("clipping keeps and clamps a straddling interval, omits one outside",
           len(kept) == 2 and omitted == 1 and kept[1].start_utc == lo,
           f"{len(kept)} kept, {omitted} omitted, "
           f"straddler clamped to {format_utc(kept[1].start_utc)}")

        # ---- snapping -------------------------------------------------------
        xnum = [float(i) for i in range(100)]        # 100 evenly spaced samples
        ok("a drag snaps to the nearest sample at each end",
           snap_span(10.4, 20.6, xnum) == (10, 21),
           str(snap_span(10.4, 20.6, xnum)))
        ok("a right-to-left drag gives the SAME interval",
           snap_span(20.6, 10.4, xnum) == snap_span(10.4, 20.6, xnum))
        ok("a drag past the ends clamps to the data",
           snap_span(-50.0, 500.0, xnum) == (0, 99))
        ok("a drag shorter than one sample collapses, for the caller to refuse",
           snap_span(10.1, 10.2, xnum) == (10, 10))

        # ---- palette --------------------------------------------------------
        import identity                     # pure; imported HERE, not at module
        #                                     scope, so the store stays stdlib
        overlap = set(SET_COLORS) & set(identity.SERIES_COLORS)
        ok("band colours are disjoint from series colours", not overlap,
           f"overlap: {overlap}" if overlap else "")

        def sat(h):
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            return 0.0 if max(r, g, b) == 0 else (max(r, g, b) - min(r, g, b)) \
                / max(r, g, b)

        worst_band = max(sat(c) for c in SET_COLORS)
        best_series = min(sat(c) for c in identity.SERIES_COLORS)
        ok("every band colour is less saturated than every series colour",
           worst_band < best_series,
           f"bands <= {worst_band:.2f}, series >= {best_series:.2f}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nannotation store gate:")
    for what, good_, detail in checks:
        print(f"  {'PASS' if good_ else 'FAIL'}  {what}")
        if detail:
            print(f"          {detail}")
    passed = sum(1 for _w, g, _d in checks if g)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
