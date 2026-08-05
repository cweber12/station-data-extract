# La Jolla sensor comparison

Pull the La Jolla station feeds into an immutable, timestamped **study**, pick
series from it, choose an averaging interval, and get a workbook with aligned
data, correlations and charts.

```
C:\Projects\la-jolla-buoy\
├── studies\                      <- snapshots live HERE, one level up
│   └── 20260805T0544Z__baseline\
│       ├── study.json               shared, tool-agnostic metadata
│       └── station-data\              this tool's namespace
│           ├── manifest.json          what was pulled, from where, with what QC
│           ├── validation.json        clock checks, coverage, cross-station sanity
│           ├── cache\observations.parquet    the canonical long frame
│           ├── workbook\              optional refreshed .xlsx snapshot
│           └── outputs\               workbooks generated against THIS study
├── archive\cache\observations.parquet  every study, deduped
├── station-data-extract\          <- this repo (code only)
├── hf-radar-extract\
└── cudem-extract\
```

Studies sit **above** the repo so the sibling extractors can read the same
snapshot, and so one study can eventually hold station data, HF radar and DEM
output for a single site and time window. `study.json` is the shared metadata
file; each tool writes its own subdirectory beside it.

## The one thing to know

**A column called `time (UTC)` is not evidence that a column holds UTC.**

The original workbook's `time (UTC)` columns contained **Pacific local time**.
Power Query's implicit text→datetime conversion applied the machine's UTC−7
offset to an ISO-8601 string and discarded the zone. `fnToLocal` then subtracted
another 7–8 h, so `time (local)` was 14 h from the truth.

The proof needs no physics: the workbook asked ERDDAP for
`time >= 2026-06-18T00:00:00Z` and the stored column began at
`2026-06-17 17:00` — seven hours before a bound the server had already enforced
in true UTC.

Everything now runs against `ingest/clockcheck.py`, which verifies a timestamp
column against signals whose phase in local solar time is known from physics:
air temperature peaks ~2 h after solar noon, and the S2 atmospheric pressure
tide peaks ~10:00 and ~22:00 local solar.

```
before (workbook `time (UTC)`)   air_temperature  offset -7.37 h   FAIL
after  (time_utc)                air_temperature  offset -0.66 h   OK
                                 air_pressure     offset -0.21 h   OK
```

The "8-hour phase offset" and "anti-phase" conclusions in `AUDIT.md` C4 were
artifacts of this bug. With both series on one clock the yellow buoy correlates
with the pier at **r ≈ 0.72**, leading it by about an hour.

## Running it

```powershell
.\run.ps1          # or: python compare.py
```

A launcher opens first with three modes:

1. **New study** — pull every configured source over the window in
   `config/stations.yaml`, normalise to the canonical long frame, run the clock
   check, write an immutable study folder. Shows a live log; it takes tens of
   seconds.
2. **Analyze current data** — open the newest study. With no studies yet you
   can still scan `sources/` the old way; legacy `time (UTC)` columns are loaded
   but flagged **unverified** in amber and recorded as such on the provenance
   sheet. No offset is applied — guessing one is what caused the original bug.
3. **Compare existing** — open one study, or select **two** and every series
   is prefixed with its study label (`baseline: LJAC1.sea_water_temperature`)
   so the same station can be compared across pulls. The merged `archive` shows
   up here as a pseudo-study.

`File → Switch study…` reopens the launcher without restarting.

### Without the GUI

```powershell
python study.py create --label baseline        # Python ingest, no Excel needed
python study.py create --label full --days 365 # a deeper window
python study.py list
python archive.py                                # rebuild the merged archive
python -m ingest.erddap --feed ndbc --days 10    # probe a feed
python -m ingest.coops --datums                  # tidal datums
```

## Requirements

```powershell
pip install -r requirements.txt
```

`pandas`, `numpy`, `openpyxl`, `pyarrow`, `pyyaml`, `requests`. **`pywin32` is
optional** — it is only needed for the Excel refresh path. The Python ingest
does not use Excel at all, so a study can be created headless.

## config/stations.yaml

The single source of truth. Nothing downstream hardcodes a coordinate, a depth,
a station id or an endpoint.

**Depths must live here, not in the feed.** Axiom's ERDDAP reports `z = 0.0 m`
for both the Scripps Pier station and CDIP 201. Taking that at face value puts a
5 m CTD and a surface buoy at the same depth, which is most of the explanation
for why they disagree.

| station | depth | frame | role |
|---|---|---|---|
| yellow buoy | on the anchor, on the seabed | **earth** | subject |
| autoss (Scripps Pier) | 5.0 m below MLLW | **earth** | primary reference |
| LJAC1 / 9410230 | 3.431 m below MLLW | **earth** | cross-check, **clock anchor** |
| 46254 / CDIP 201 | 0.45 m below the surface | **surface** | stratification endpoint |
| LJPC1 | — | earth | context only |

MSL is 0.832 m above MLLW at 9410230, so below MSL the pier sensors sit at
4.26 m and 5.83 m — a constant 1.57 m apart.

**46254 is not a peer.** It follows the sea surface; the logger sits on the
seabed. Its correct use is the surface endpoint of a stratification index,
`SST(46254) − T(autoss)`, which the tool offers as a derived series on its own
chart panel. That index is the covariate that says *when* the buoy and the pier
should be expected to agree.

**LJPC1 reports no water and no air temperature at all.** That is the station,
not a query bug. It is wave and wind only.

### The pull window

`defaults.window_days: 45`. NDBC realtime2 holds a rolling ~45 days, so 45 is a
value any source can satisfy.

Worth knowing, measured 2026-08-04: **the feeds used here hold
far more than that.** ERDDAP `cwwcNDBCMet` advertises `time actual_range` from
1970-02-26, and LJAC1 returns 6-minute data for probes at 2020, 2023 and 2025.
The Axiom Scripps Pier feed reaches back to 2013-01-18. Widening `window_days`
is a config edit, not a code change, and it is the cheapest way to deepen the
archive.

## What comes out

| sheet | what's on it |
|---|---|
| `chart_raw` | line chart; every legend entry carries **depth and frame** |
| `chart_zscore` | z-scored, so different units share one axis |
| `chart_stratification` | `SST(46254) − T(autoss)` on its own panel, when both endpoints are selected |
| `chart_scatter` | first two series against each other |
| `data` | one row per interval, local and UTC timestamps |
| `stats` | summary and correlation matrix — **live formulas** |
| `counts` | raw samples behind each cell; shaded where thin |
| `normalized` | the z-scored data |
| `provenance` | study id, window, QC policy, clock-check verdicts, per-series depth/frame/time-basis and suspect counts |

No dual-axis charts, on purpose: two scales on one frame make any two series
look however you want them to. Use `chart_zscore` to compare shape and timing.

**Min samples/bin** is worth setting. At 1-hour bins, 46254 reports twice and
the Scripps CTD reports fifteen times; a bin holding one sample of a 4-minute
series is a gap wearing an average's clothes.

## Validation

Every study is checked and the result is written to `validation.json`.
**A failure never aborts the snapshot** — it sets `status: failed_validation`
and the UI shows the study in red. A failed pull is evidence about the feed,
and deleting it destroys the only record that it happened.

1. **Clock check** against LJAC1 air temperature (primary) and pressure
   (confirms, but is ambiguous modulo 12 h — never use it alone to measure a
   magnitude). LJAC1 is the only station reporting both, which is why it is the
   permanent anchor.
2. **Schema conformance** of the cached parquet.
3. **Coverage** — per series: span, median step, largest gap, % missing.
4. **Cross-station sanity** — `T(autoss)` vs `T(LJAC1)`. The **best lag** is the
   test and it should be ≈ 0. Their absolute difference is reported but is
   deliberately *not* a tight pass/fail: the two sensors are 1.57 m apart
   vertically on a coast with a strong internal tide, so a degree or two in
   stratified summer conditions is ocean, not error.

## QC

QARTOD flags: 1 good, 2 not evaluated, 3 suspect, 4 fail, 9 missing.
**4 and 9 are rejected; 1, 2 and 3 pass, and 3 stays flagged.** The count of
suspect values kept is reported per series on the provenance sheet.

The workbook's original `*_ok` columns nulled only flag 4, so 3 *and* 9 came
through under a reassuring name — 9 being a missing-value marker handed
downstream as if it were a measurement.

## The Power Query workbook (optional)

The Python ingest is the primary path. The workbook is kept as a human-facing
view and a second opinion.

`sources/Section1.m` is the **authoritative, version-controlled copy** of the
query logic. The `.xlsx` is not tracked — it is megabytes of cached results —
so without this file the query logic had no history at all.

```powershell
# read the M out of a workbook
python -m ingest.mashup extract sources/ja_jolla_sensors.xlsx -o sources/Section1.m

# apply the corrected M: inject -> refresh through Excel -> fix what loads where
python -m ingest.mashup rebuild sources/ja_jolla_sensors.xlsx -o build/ja_jolla_sensors.xlsx
```

`rebuild` only ever reads the source. Inspect the result, then move it into
`sources/` yourself.

**Excel must be closed.** A `~$ja_jolla_sensors.xlsx` lock file next to the
workbook means it is open, or that Excel crashed and left the lock behind; the
refresh refuses up front rather than silently saving a read-only copy.

**Privacy levels, one time only.** A first refresh may block on "Information is
required about data privacy", which cannot be set reliably over COM and is
invisible with `Visible = False`. Set it by hand once:
**Data → Get Data → Query Options → Privacy → "Always ignore Privacy Level
settings"**. There is a wall-clock timeout so a prompt cannot hang forever.

**`refreshOnLoad` stays off in a snapshot.** A study workbook that re-refreshes
itself when opened is not immutable, and its manifest would stop describing its
own contents. Turn it on for `sources/` only.

## Extending it

- **A new derived column with no unit in its header** — add a line to
  `UNIT_OVERRIDES` in `sensorkit.py`.
- **A new time column convention** — add a pattern to `TIME_PREFERENCE`. Order
  matters, and each entry carries a *trust level*: `time_utc` is verified,
  everything else is legacy and gets flagged.
- **A new station or variable** — `config/stations.yaml` only.
- **Another extractor** — write into `studies/<id>/<your-tool>/` and append an
  entry to the `producers` list in `study.json`.

### Scripting it without the window

`sensorkit`, `exporter`, `study`, `archive` and `ingest` have no GUI
dependency.

```python
from pathlib import Path
import sensorkit as sk, exporter as ex, study as pj

info = pj.latest_project()
tabs = sk.build_catalog_project(info, config_root=Path("."))["observations.parquet"]

sel = [(t, t.columns[0]) for t in tabs
       if t.columns[0].variable == "sea_water_temperature"]

res = sk.build_comparison(sel, interval="1h", min_samples=2, stratification=True)
ref = next(c for c in res.data.columns if "yellow" in c)
ex.write_workbook(res, Path("."), info.outputs_dir / "test.xlsx",
                  sk.lag_scan(res.data, ref, "1h"), ref, study=info)
```

## Lag scan

Cross-correlates every series against a reference across ±24 h.

La Jolla nearshore temperature is dominated by the ~12.42 h internal tide, so a
peak at lag *L* cannot be distinguished from one at *L* ± 12.42 h. The `alt lag`
column spells out the alternative every time. **Negative lag means the series
leads the reference.**

Against the yellow buoy at 1-hour bins, on a correct clock:

| series | r at lag 0 | best lag | r at best lag |
|---|---|---|---|
| LJAC1 (3.43 m, earth) | 0.664 | −1.0 h | 0.723 |
| autoss (5 m, earth) | 0.660 | −1.0 h | 0.715 |
| 46254 (0.45 m, **surface**) | 0.351 | +20.0 h | 0.426 |

The buoy leads the pier by about an hour. 46254 correlates poorly because it is
in a different reference frame — which is the point of the stratification index,
not a defect.
