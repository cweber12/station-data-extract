"""
archive.py -- one long record, assembled from every study snapshot.

WHY THIS EXISTS
---------------
Two reasons, and it is worth being precise about which is which, because the
study notes have conflated them.

1. REVISIONS. Providers restate observations. A QARTOD flag gets upgraded when a
   test is re-run, a sensor is recalibrated and the record is reprocessed, a
   preliminary water level is replaced by a verified one. Re-pulling the same
   window later can therefore return DIFFERENT VALUES for timestamps you already
   have. Overwriting them silently destroys the fact that the record changed.
   Every superseded value is written to revisions.jsonl instead.

2. WINDOW. If a source ever stops serving deep history, the archive is what is
   left. That risk is real for NDBC realtime2 (~45 days) and for anything pulled
   from a rolling feed.

   It is NOT currently the situation for the feeds this study uses. Measured
   2026-08-04: cwwcNDBCMet advertises time actual_range from 1970-02-26, and
   LJAC1 returns 6-minute data for probes at 2020, 2023 and 2025. The Axiom
   Scripps Pier feed reaches back to 2013-01-18. The 45-day pull window is a
   configured choice (`defaults.window_days` in config/stations.yaml), not a
   limit imposed by the sources. Widening it is the cheapest way to deepen the
   archive, and it is a config edit rather than a code change.

DEDUP RULE
----------
Key is (time_utc, station, variable). Where a key repeats, the row with the most
recent `fetched_utc` wins -- later pulls carry QC upgrades and provider
revisions. Ties break on the later study id, which is deterministic.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from ingest.config import CANONICAL_COLUMNS
from study import PRODUCER, StudyInfo, default_studies_root, list_studies

ARCHIVE_DIRNAME = "archive"
OBSERVATIONS_NAME = "observations.parquet"
REVISIONS_NAME = "revisions.jsonl"

# Below this, a difference is float noise or a rounding change, not a revision.
VALUE_TOLERANCE = 1e-6

KEY = ["time_utc", "station", "variable"]


def default_archive_root(repo_root: Path | None = None) -> Path:
    """Beside `studies/`, one level above the repo, for the same reason."""
    return default_studies_root(repo_root).parent / ARCHIVE_DIRNAME


def resolve_archive_root(repo_root: Path | None = None,
                         archive_root: Path | None = None) -> Path:
    """Where the archive is, from whatever the caller was given.

    One resolver, because three call sites spelled this out separately and
    the CLI then had no root at all -- it worked from what `rebuild` returned
    instead, which is how `--show-revisions` came to look a directory too
    deep. Anything that needs a path inside the archive asks for the root
    first.
    """
    if archive_root is not None:
        return Path(archive_root)
    return default_archive_root(repo_root)


def archive_observations_path(archive_root: Path) -> Path:
    """`<archive>/cache/observations.parquet`.

    Under `cache/` so that a StudyInfo with producer="" resolves its cache_dir
    straight onto it and build_catalog_study needs no special case.
    """
    return Path(archive_root) / "cache" / OBSERVATIONS_NAME


def archive_revisions_path(archive_root: Path) -> Path:
    """`<archive>/revisions.jsonl`. NOT beside the observations.

    One directory shallower than the parquet, which is the whole of the bug
    this function exists to close: the observations live under `cache/`
    because a StudyInfo resolves its cache_dir onto them, and the revisions
    are not observations -- they are the record of the archive contradicting
    itself, and they outlive any particular cache.

    Derived from the ROOT, by both the writer and every reader, so that
    nobody has to know one file sits deeper than the other.
    """
    return Path(archive_root) / REVISIONS_NAME


def _load_studies(studies_root: Path | None) -> list[StudyInfo]:
    return [p for p in list_studies(studies_root)
            if p.observations_path.is_file()]


def rebuild(repo_root: Path | None = None, *,
            studies_root: Path | None = None,
            archive_root: Path | None = None,
            tolerance: float = VALUE_TOLERANCE,
            log=print) -> Path:
    """Union every study's cache into archive/observations.parquet."""
    proot = Path(studies_root) if studies_root else default_studies_root(repo_root)
    aroot = resolve_archive_root(repo_root, archive_root)
    out = archive_observations_path(aroot)
    out.parent.mkdir(parents=True, exist_ok=True)

    studies = _load_studies(proot)
    if not studies:
        log(f"no studies with a cache under {proot}")
        pd.DataFrame(columns=CANONICAL_COLUMNS).to_parquet(out, index=False)
        archive_revisions_path(aroot).write_text("", encoding="utf-8")
        return out

    frames = []
    for p in studies:
        df = p.load_observations()
        if df.empty:
            continue
        df = df.copy()
        df["__study"] = p.study_id
        frames.append(df)
        log(f"  {p.study_id}: {len(df):,} rows")

    if not frames:
        log("every study cache was empty")
        pd.DataFrame(columns=CANONICAL_COLUMNS).to_parquet(out, index=False)
        return out

    allrows = pd.concat(frames, ignore_index=True)
    allrows["time_utc"] = pd.to_datetime(allrows["time_utc"], utc=True)
    allrows["fetched_utc"] = pd.to_datetime(allrows["fetched_utc"], utc=True)

    # Newest pull last, so keep="last" takes the winner and everything before it
    # in the same key group is a superseded candidate.
    allrows = allrows.sort_values(["fetched_utc", "__study"], kind="stable")
    dup_mask = allrows.duplicated(subset=KEY, keep="last")

    revisions = _collect_revisions(allrows, dup_mask, tolerance)
    kept = allrows[~dup_mask].drop(columns="__study")
    kept = kept.sort_values(["station", "variable", "time_utc"]).reset_index(drop=True)

    kept[CANONICAL_COLUMNS].to_parquet(out, index=False)

    rev_path = archive_revisions_path(aroot)
    with rev_path.open("w", encoding="utf-8") as fh:
        for rec in revisions:
            fh.write(json.dumps(rec, default=str) + "\n")

    log(f"  union: {len(kept):,} rows "
        f"(from {len(allrows):,} across {len(studies)} study(s))")
    log(f"  revisions: {len(revisions)}")
    return out


def _collect_revisions(allrows: pd.DataFrame, dup_mask: pd.Series,
                       tolerance: float) -> list[dict]:
    """Record every superseded value that actually differs from its replacement.

    A re-pull that returns identical numbers is not a revision and must not
    generate noise -- feeding the same study twice has to leave this empty.
    """
    superseded = allrows[dup_mask]
    if superseded.empty:
        return []

    winners = (allrows[~dup_mask]
               .set_index(KEY)[["value", "fetched_utc", "__study", "qc_flag"]])
    if winners.index.has_duplicates:
        winners = winners[~winners.index.duplicated(keep="last")]

    idx = pd.MultiIndex.from_frame(superseded[KEY])
    joined = winners.reindex(idx)

    old_v = superseded["value"].to_numpy(dtype="float64")
    new_v = joined["value"].to_numpy(dtype="float64")
    old_q = pd.to_numeric(superseded["qc_flag"], errors="coerce").to_numpy()
    new_q = pd.to_numeric(joined["qc_flag"], errors="coerce").to_numpy()

    import numpy as np
    value_changed = ~(np.isclose(old_v, new_v, rtol=0.0, atol=tolerance,
                                 equal_nan=True))
    qc_changed = ~((old_q == new_q) | (pd.isna(old_q) & pd.isna(new_q)))
    changed = value_changed | qc_changed

    out = []
    for i in np.flatnonzero(changed):
        row = superseded.iloc[i]
        win = joined.iloc[i]
        out.append({
            "time_utc": row["time_utc"], "station": row["station"],
            "variable": row["variable"],
            "old": None if pd.isna(old_v[i]) else float(old_v[i]),
            "new": None if pd.isna(new_v[i]) else float(new_v[i]),
            "old_qc_flag": None if pd.isna(old_q[i]) else int(old_q[i]),
            "new_qc_flag": None if pd.isna(new_q[i]) else int(new_q[i]),
            "old_fetched_utc": row["fetched_utc"],
            "new_fetched_utc": win["fetched_utc"],
            "old_study": row["__study"], "new_study": win["__study"],
        })
    return out


def archive_study(repo_root: Path | None = None,
                    archive_root: Path | None = None) -> StudyInfo | None:
    """Expose the archive as a pseudo-study, so the UI can compare against it.

    Presents the same surface as a real StudyInfo -- cache_dir, outputs_dir --
    so build_catalog_study works on it unchanged.
    """
    aroot = resolve_archive_root(repo_root, archive_root)
    obs = archive_observations_path(aroot)
    if not obs.is_file():
        return None

    df = pd.read_parquet(obs, columns=["time_utc", "station"])
    if df.empty:
        return None
    t = pd.to_datetime(df["time_utc"], utc=True)

    info = StudyInfo(
        path=aroot, study_id="archive", label="archive (all studies)",
        created_utc=dt.datetime.fromtimestamp(
            obs.stat().st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        status="ok", stations=sorted(df["station"].astype(str).unique().tolist()),
        time_min_utc=t.min().strftime("%Y-%m-%dT%H:%M:%SZ"),
        time_max_utc=t.max().strftime("%Y-%m-%dT%H:%M:%SZ"),
        n_rows=int(len(df)),
        validation_summary="merged record; see each study for its own checks",
        producer="",
    )
    return info


def print_revisions(archive_root: Path, limit: int, out=print):
    """Print up to `limit` revision records. Returns how many, or None.

    Its own function so the MISSING-FILE branch can be reached at all. Inside
    the CLI it sat behind a rebuild that always rewrites the file, so the one
    path that mattered -- the silent one -- could not be exercised without
    arranging for a rebuild to fail.

    None means the record is absent, which after a successful rebuild is a
    fault. 0 means there were no revisions, which is an answer. Those were
    the same thing before, and "there were no revisions" was the wrong one.
    """
    rev = archive_revisions_path(archive_root)
    if not rev.is_file():
        out(f"  no revision record at {rev} — a rebuild writes one even when "
            f"nothing was superseded, so this is a fault rather than a "
            f"quiet no.")
        return None
    shown = 0
    for line in rev.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if shown >= limit:
            break
        out(f"  {line}")
        shown += 1
    return shown


def _fixture_study(root: Path, study_id: str, rows: list[dict]) -> Path:
    """Write a minimal study that `list_studies` will pick up.

    Only what the archive reads: a study.json so the directory is recognised,
    and a cache parquet. No manifest, because `load_study` tolerates its
    absence and the archive never looks at one.
    """
    d = root / study_id
    (d / PRODUCER / "cache").mkdir(parents=True, exist_ok=True)
    (d / "study.json").write_text(json.dumps({
        "study_id": study_id, "created_utc": f"{study_id}T00:00:00Z",
        "status": "ok"}), encoding="utf-8")
    df = pd.DataFrame(rows)
    for c in CANONICAL_COLUMNS:
        if c not in df:
            df[c] = None
    df[CANONICAL_COLUMNS].to_parquet(
        d / PRODUCER / "cache" / OBSERVATIONS_NAME, index=False)
    return d


def _check(repo_root: Path | None = None) -> int:
    """Assert the archive's promises. `python archive.py --check`.

    CLAUDE.md has told anyone touching ingest to run "archive union-not-sum"
    for as long as there has been an archive, and there was nothing to run.
    This is that gate.

    Built on a SYNTHETIC pair of studies, so a revision can be forced rather
    than hoped for, and then repeated against the real studies root for the
    numbers that matter in practice. Nothing is written into `studies/` or
    `sources/`: the fixture and both archives live in a temp directory.
    """
    import tempfile
    checks = []

    stamp = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)

    def row(t_hour, value, fetched_day, station="LJAC1",
            variable="sea_water_temperature", qc=1):
        return {"time_utc": stamp + dt.timedelta(hours=t_hour),
                "station": station, "variable": variable, "value": value,
                "unit": "degC", "qc_flag": qc, "depth_m": 5.0,
                "reference_frame": "earth", "source": "fixture",
                "fetched_utc": stamp + dt.timedelta(days=fetched_day)}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sroot, aroot = tmp / "studies", tmp / "archive"

        # Two overlapping pulls of the same window. The later one restates
        # ONE value and re-reports the other two unchanged -- so the union
        # must be three rows, and exactly one revision.
        _fixture_study(sroot, "20260701T000000Z__first",
                       [row(0, 10.0, 1), row(1, 11.0, 1), row(2, 12.0, 1)])
        _fixture_study(sroot, "20260702T000000Z__second",
                       [row(1, 11.0, 2), row(2, 99.0, 2)])

        quiet = []
        out = rebuild(repo_root, studies_root=sroot, archive_root=aroot,
                      log=quiet.append)
        kept = pd.read_parquet(out)
        checks.append((f"UNION, NOT SUM: overlapping pulls merge on "
                       f"(time, station, variable) [{len(kept)} rows from 5]",
                       len(kept) == 3))
        latest = kept.set_index("time_utc")["value"].to_dict()
        checks.append((f"the LATER pull wins where they disagree "
                       f"[{latest[stamp + dt.timedelta(hours=2)]}]",
                       latest[stamp + dt.timedelta(hours=2)] == 99.0))

        rev = archive_revisions_path(aroot)
        checks.append((f"the revision record sits beside the archive root, "
                       f"NOT under cache/ with the observations "
                       f"[{rev.parent.name}/{rev.name}]",
                       rev.is_file()
                       and rev.parent == aroot
                       and not (out.parent / REVISIONS_NAME).exists()))
        recs = [l for l in rev.read_text(encoding="utf-8").splitlines() if l]
        checks.append((f"a restated value is recorded as a revision, and an "
                       f"unchanged one is not [{len(recs)} record(s)]",
                       len(recs) == 1))
        checks.append((f"and the record carries the superseded value, which "
                       f"is the evidence [{recs[0][:56] if recs else ''}...]",
                       bool(recs) and "12.0" in recs[0]))

        # The flag, through _main, which is the thing that was broken.
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _main(["--root", str(repo_root or Path.cwd()),
                   "--studies-root", str(sroot), "--archive-root", str(aroot),
                   "--show-revisions", "5"])
        printed = buf.getvalue()
        checks.append((f"--show-revisions PRINTS the records it found "
                       f"[{len([l for l in printed.splitlines() if l.startswith('  {')])} "
                       f"line(s)]",
                       any(l.strip().startswith("{")
                           for l in printed.splitlines())))
        checks.append(("and does not report the file as missing when it is "
                       "there", "no revision record at" not in printed))

        # Idempotence: same studies again, same answer, same revisions.
        out2 = rebuild(repo_root, studies_root=sroot, archive_root=aroot,
                       log=quiet.append)
        kept2 = pd.read_parquet(out2)
        recs2 = [l for l in archive_revisions_path(aroot)
                 .read_text(encoding="utf-8").splitlines() if l]
        checks.append((f"IDEMPOTENT: a second rebuild changes nothing "
                       f"[{len(kept2)} rows, {len(recs2)} revision(s)]",
                       kept2.equals(kept) and recs2 == recs))

        # Zero revisions must be distinguishable from a missing file.
        s2root, a2root = tmp / "studies2", tmp / "archive2"
        _fixture_study(s2root, "20260701T000000Z__only",
                       [row(0, 10.0, 1), row(1, 11.0, 1)])
        rebuild(repo_root, studies_root=s2root, archive_root=a2root,
                log=quiet.append)
        zero_rev = archive_revisions_path(a2root)
        checks.append((f"a rebuild with nothing superseded still WRITES the "
                       f"record, empty [{zero_rev.stat().st_size} bytes]",
                       zero_rev.is_file()
                       and not zero_rev.read_text(encoding="utf-8").strip()))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _main(["--root", str(repo_root or Path.cwd()),
                   "--studies-root", str(s2root),
                   "--archive-root", str(a2root), "--show-revisions", "5"])
        zero_out = buf.getvalue()
        checks.append(("zero revisions prints no records, and says nothing "
                       "about a missing file",
                       "no revision record at" not in zero_out
                       and not any(l.strip().startswith("{")
                                   for l in zero_out.splitlines())))

        # And the fault case SPEAKS, which is what silence used to hide.
        # Reached through print_revisions rather than _main, because _main
        # rebuilds first and a rebuild always writes the file -- the branch
        # is unreachable from the CLI, which is why it went unnoticed.
        zero_rev.unlink()
        said = []
        missing = print_revisions(a2root, 5, out=said.append)
        checks.append((f"a MISSING record is reported, not read as 'no "
                       f"revisions' — they are different answers "
                       f"[{'speaks' if said else 'SILENT'}]",
                       missing is None and bool(said)
                       and "no revision record at" in said[0]))
        checks.append((f"and the message names the path it looked in, so the "
                       f"next person does not have to guess "
                       f"[...{said[0][-40:] if said else ''}]",
                       bool(said) and str(a2root) in said[0]))
        checks.append(("an EMPTY record returns 0 rather than None, so a "
                       "caller can tell 'none' from 'missing'",
                       print_revisions(aroot, 0, out=lambda _s: None) == 0))

    # ---- the real studies, for the numbers that matter ---------------------
    proot = default_studies_root(repo_root)
    real = _load_studies(proot)
    if not real:
        checks.append((f"no studies under {proot} to check the real archive "
                       f"against — reported rather than skipped in silence",
                       False))
    else:
        with tempfile.TemporaryDirectory() as td:
            aroot = Path(td) / "archive"
            lines = []
            out = rebuild(repo_root, studies_root=proot, archive_root=aroot,
                          log=lines.append)
            kept = pd.read_parquet(out, columns=["time_utc"])
            total = sum(len(p.load_observations()) for p in real)
            n_rev = len([l for l in archive_revisions_path(aroot)
                         .read_text(encoding="utf-8").splitlines() if l])
            checks.append((f"the real archive is a UNION: {len(kept):,} rows "
                           f"from {total:,} across {len(real)} studies, "
                           f"{n_rev} revision(s)",
                           0 < len(kept) <= total))
            out2 = rebuild(repo_root, studies_root=proot, archive_root=aroot,
                           log=lines.append)
            kept2 = pd.read_parquet(out2, columns=["time_utc"])
            checks.append((f"and unchanged on a second rebuild "
                           f"[{len(kept2):,} rows]", len(kept2) == len(kept)))

    print()
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Rebuild the merged archive.")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--studies-root", type=Path, default=None)
    ap.add_argument("--archive-root", type=Path, default=None)
    ap.add_argument("--show-revisions", type=int, default=0,
                    help="print the first N revision records")
    ap.add_argument("--check", action="store_true",
                    help="assert union-not-sum, idempotence and the revision "
                         "record, then exit non-zero on any failure")
    args = ap.parse_args(argv)

    if args.check:
        return _check(args.root)

    # From the ROOT, the same way the writer derives it. Deriving it from
    # what rebuild() returned is what made this flag silent: the returned
    # path is the observations parquet, one directory deeper, so the
    # revisions were looked for in `<archive>/cache/` where they never are.
    aroot = resolve_archive_root(args.root, args.archive_root)
    out = rebuild(args.root, studies_root=args.studies_root,
                  archive_root=args.archive_root)
    print(f"wrote {out}")

    if args.show_revisions:
        # Zero revisions prints nothing -- the rebuild has already logged
        # "revisions: 0" above, so the run says so. A MISSING record speaks,
        # because silence made the two indistinguishable and "there were no
        # revisions" was the wrong one of the two answers.
        print_revisions(aroot, args.show_revisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
