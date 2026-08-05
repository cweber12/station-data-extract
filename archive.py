"""
archive.py -- one long record, assembled from every project snapshot.

WHY THIS EXISTS
---------------
Two reasons, and it is worth being precise about which is which, because the
project notes have conflated them.

1. REVISIONS. Providers restate observations. A QARTOD flag gets upgraded when a
   test is re-run, a sensor is recalibrated and the record is reprocessed, a
   preliminary water level is replaced by a verified one. Re-pulling the same
   window later can therefore return DIFFERENT VALUES for timestamps you already
   have. Overwriting them silently destroys the fact that the record changed.
   Every superseded value is written to revisions.jsonl instead.

2. WINDOW. If a source ever stops serving deep history, the archive is what is
   left. That risk is real for NDBC realtime2 (~45 days) and for anything pulled
   from a rolling feed.

   It is NOT currently the situation for the feeds this project uses. Measured
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
revisions. Ties break on the later project id, which is deterministic.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from ingest.config import CANONICAL_COLUMNS
from project import (PRODUCER, ProjectInfo, default_projects_root, list_projects)

ARCHIVE_DIRNAME = "archive"
OBSERVATIONS_NAME = "observations.parquet"
REVISIONS_NAME = "revisions.jsonl"

# Below this, a difference is float noise or a rounding change, not a revision.
VALUE_TOLERANCE = 1e-6

KEY = ["time_utc", "station", "variable"]


def default_archive_root(repo_root: Path | None = None) -> Path:
    """Beside `projects/`, one level above the repo, for the same reason."""
    return default_projects_root(repo_root).parent / ARCHIVE_DIRNAME


def archive_observations_path(archive_root: Path) -> Path:
    """`<archive>/cache/observations.parquet`.

    Under `cache/` so that a ProjectInfo with producer="" resolves its cache_dir
    straight onto it and build_catalog_project needs no special case.
    """
    return Path(archive_root) / "cache" / OBSERVATIONS_NAME


def _load_projects(projects_root: Path | None) -> list[ProjectInfo]:
    return [p for p in list_projects(projects_root)
            if p.observations_path.is_file()]


def rebuild(repo_root: Path | None = None, *,
            projects_root: Path | None = None,
            archive_root: Path | None = None,
            tolerance: float = VALUE_TOLERANCE,
            log=print) -> Path:
    """Union every project's cache into archive/observations.parquet."""
    proot = Path(projects_root) if projects_root else default_projects_root(repo_root)
    aroot = Path(archive_root) if archive_root else default_archive_root(repo_root)
    out = archive_observations_path(aroot)
    out.parent.mkdir(parents=True, exist_ok=True)

    projects = _load_projects(proot)
    if not projects:
        log(f"no projects with a cache under {proot}")
        pd.DataFrame(columns=CANONICAL_COLUMNS).to_parquet(out, index=False)
        (aroot / REVISIONS_NAME).write_text("", encoding="utf-8")
        return out

    frames = []
    for p in projects:
        df = p.load_observations()
        if df.empty:
            continue
        df = df.copy()
        df["__project"] = p.project_id
        frames.append(df)
        log(f"  {p.project_id}: {len(df):,} rows")

    if not frames:
        log("every project cache was empty")
        pd.DataFrame(columns=CANONICAL_COLUMNS).to_parquet(out, index=False)
        return out

    allrows = pd.concat(frames, ignore_index=True)
    allrows["time_utc"] = pd.to_datetime(allrows["time_utc"], utc=True)
    allrows["fetched_utc"] = pd.to_datetime(allrows["fetched_utc"], utc=True)

    # Newest pull last, so keep="last" takes the winner and everything before it
    # in the same key group is a superseded candidate.
    allrows = allrows.sort_values(["fetched_utc", "__project"], kind="stable")
    dup_mask = allrows.duplicated(subset=KEY, keep="last")

    revisions = _collect_revisions(allrows, dup_mask, tolerance)
    kept = allrows[~dup_mask].drop(columns="__project")
    kept = kept.sort_values(["station", "variable", "time_utc"]).reset_index(drop=True)

    kept[CANONICAL_COLUMNS].to_parquet(out, index=False)

    rev_path = aroot / REVISIONS_NAME
    with rev_path.open("w", encoding="utf-8") as fh:
        for rec in revisions:
            fh.write(json.dumps(rec, default=str) + "\n")

    log(f"  union: {len(kept):,} rows "
        f"(from {len(allrows):,} across {len(projects)} project(s))")
    log(f"  revisions: {len(revisions)}")
    return out


def _collect_revisions(allrows: pd.DataFrame, dup_mask: pd.Series,
                       tolerance: float) -> list[dict]:
    """Record every superseded value that actually differs from its replacement.

    A re-pull that returns identical numbers is not a revision and must not
    generate noise -- feeding the same project twice has to leave this empty.
    """
    superseded = allrows[dup_mask]
    if superseded.empty:
        return []

    winners = (allrows[~dup_mask]
               .set_index(KEY)[["value", "fetched_utc", "__project", "qc_flag"]])
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
            "old_project": row["__project"], "new_project": win["__project"],
        })
    return out


def archive_project(repo_root: Path | None = None,
                    archive_root: Path | None = None) -> ProjectInfo | None:
    """Expose the archive as a pseudo-project, so the UI can compare against it.

    Presents the same surface as a real ProjectInfo -- cache_dir, outputs_dir --
    so build_catalog_project works on it unchanged.
    """
    aroot = Path(archive_root) if archive_root else default_archive_root(repo_root)
    obs = archive_observations_path(aroot)
    if not obs.is_file():
        return None

    df = pd.read_parquet(obs, columns=["time_utc", "station"])
    if df.empty:
        return None
    t = pd.to_datetime(df["time_utc"], utc=True)

    info = ProjectInfo(
        path=aroot, project_id="archive", label="archive (all projects)",
        created_utc=dt.datetime.fromtimestamp(
            obs.stat().st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        status="ok", stations=sorted(df["station"].astype(str).unique().tolist()),
        time_min_utc=t.min().strftime("%Y-%m-%dT%H:%M:%SZ"),
        time_max_utc=t.max().strftime("%Y-%m-%dT%H:%M:%SZ"),
        n_rows=int(len(df)),
        validation_summary="merged record; see each project for its own checks",
        producer="",
    )
    return info


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Rebuild the merged archive.")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--projects-root", type=Path, default=None)
    ap.add_argument("--archive-root", type=Path, default=None)
    ap.add_argument("--show-revisions", type=int, default=0,
                    help="print the first N revision records")
    args = ap.parse_args(argv)

    out = rebuild(args.root, projects_root=args.projects_root,
                  archive_root=args.archive_root)
    print(f"wrote {out}")

    if args.show_revisions:
        rev = out.parent / REVISIONS_NAME
        if rev.is_file():
            for i, line in enumerate(rev.read_text(encoding="utf-8").splitlines()):
                if i >= args.show_revisions:
                    break
                print(" ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
