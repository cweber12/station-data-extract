# Audit — `ja_jolla_sensors.xlsx`

19 sheets · 17 loaded tables · 20 Power Query queries · 4 MB packed / 27 MB unpacked
No charts, no pivot tables, no defined names.

---

## A. Correctness bugs — the data is wrong right now

### A1. `meta_ERDDAP` is a duplicate of `meta_COOPS_datums`
The M code for `meta_ERDDAP` fetches the **COOPS datums JSON** for station 9410230 — the exact
same URL and the exact same steps as `meta_COOPS_datums`. The two loaded tables are byte-for-byte
identical (verified with a dataframe equality check). Copy/paste error; `meta_ERDDAP` contains no
ERDDAP metadata at all.

### A2. Your date parameters contain literal quote characters, and the window you get is not the window you asked for
```m
shared p_StartUTC = let Source = """2026-07-11T14:00:00Z""" in Source;
```
Triple-quoting in M produces the string **including** the quote marks. The cell value is
`"2026-07-11T14:00:00Z"`, quotes and all. Concatenated into the ERDDAP URL you get
`time>="2026-07-11T14:00:00Z"`.

The consequence is measurable in the loaded data:

| | parameter says | data actually starts/ends |
|---|---|---|
| start | 2026-07-11 **14:00** UTC | 2026-07-11 **07:00** UTC |
| end | 2026-08-01 **16:00** UTC | 2026-08-01 **09:00** UTC |

Both ends land exactly **7 hours earlier** than requested. The constraint is being interpreted as
Pacific local time despite the `Z` and despite the parameter being named `UTC`. You are silently
analysing a different window than the one documented in the workbook.

### A3. `src_NDBC` ignores the parameters entirely
The NDBC URL hardcodes its own window:
```
&time>=2026-06-18T00:00:00Z&time<=2026-08-03T00:00:00Z
```
So the loaded ranges are:

| table | coverage |
|---|---|
| `src_NDBC` / LJAC1 / LJPC1 / 46254 | 2026-06-17 → 2026-08-02 |
| SCCOOS CTD / eco / seaphox | 2026-07-11 → 2026-08-01 |
| `src_WaterLevel` | 2026-07-11 → 2026-08-01 |
| `src_yellow_buoy` | 2026-07-11 → 2026-08-01 |

Roughly **24 days of NDBC data has no counterpart in any other series.** A join that doesn't
explicitly handle this will either drop it or, worse, include it in period statistics computed over
a nominally shared window.

### A4. `p_NDBCStations` is dead code; the station list is hardcoded three times
The parameter is defined, loaded to its own sheet, and **never referenced by any query**. The
station list appears independently in `src_NDBC`'s URL, in `meta_NDBC_stations`' row filter, and in
`meta_NDBC_positions`' URL. `src_9410230_wl` hardcodes its dates in a *fourth* format
(`begin_date=20260711`). Changing a station or a date range means editing four to five places
correctly and consistently.

### A5. Four competing time conventions in one workbook
- `src_NDBC` adds `time (PDT)` = UTC − 7 h, flat, no DST logic.
- `src_LJAC1` / `src_LJPC1` / `src_46254` inherit that column **and** add `time (local)` via
  `fnToLocal`, which is DST-aware. Two local-time columns, same rows.
- COOPS water level uses the API's `lst_ldt` — already local, already DST-aware, never converted.
- `src_yellow_buoy` uses whatever HOBOconnect wrote, labelled "PDT".

Right now `time (PDT)` and `time (local)` are identical because the whole window sits inside DST, so
the bug is invisible. Any window crossing the second Sunday in March or the first Sunday in November
makes them diverge, and nothing in the workbook records which one a downstream calculation used.

Separately, `fnToLocal` hand-implements US DST rules. The arithmetic happens to be correct, but it
is unnecessary risk — the rules are a policy decision that has changed before and may change again.

### A6. The `_ok` columns do not do what their name says
```m
if [..._qc_agg] = 4 then null else [value]
```
Only QARTOD flag **4 (fail)** is nulled. Flag **3 (suspect)** and flag **9 (missing)** pass through
as "ok". In this window it happens to be moot — the CTD temperature flags are 1 (7,586 rows) and
2 "not evaluated" (2,529 rows), with no 3s or 4s — so the columns currently do nothing at all
except carry a reassuring name. The 2,529 nulls in `wtmp_ok` come from the raw value being NaN, not
from QC screening.

---

## B. Structural problems

### B1. The NDBC payload is stored twice
`src_NDBC` holds 12,607 rows. The three per-station tables hold 9,336 + 1,104 + 2,167 = **12,607**.
Same data, duplicated, both saved into the file. This is most of your 4 MB.

### B2. `fnBinKeys` is the wrong abstraction
It stamps five integer columns (`BinKey10/30/60/120/180`) onto every row of every source table.
Costs:
- ~200,000 derived cell values that exist only to support a join.
- Your available intervals are frozen at 10 / 30 / 60 / 120 / 180 minutes. 15-min, 6-hour, and daily
  are not options without editing the function and refreshing everything.
- Bins are computed on **local** time and anchored to the Excel epoch, so a DST-crossing window
  yields one duplicated bin and one missing bin.
- The key is a bare integer, not a timestamp — it has to be converted back before it means anything.

Resampling is a query-time operation. Materialising it into the source tables is what forced this
into a fixed menu.

### B3. ~25% of every SCCOOS table is padding
`src_SCCOOS_ctd`, `src_SCCOOS_eco`, and `src_SCCOOS_seaphox` all have exactly **10,115 rows**,
because each queries the same platform over the same window and receives the union of every
sensor's timestamps. Each table then populates only its own subset. This is why CTD row spacing
alternates 4:00 / 3:50 / 0:10 rather than being a clean cadence.

### B4. The yellow buoy sheets are second-class citizens
`src_yellow_buoy` and `meta_yellow_buoy` are pasted plain ranges — not tables, not queries, no
structured references, no refresh path. Everything else in the workbook is a table. `meta_yellow_buoy`
is a four-column indented key/value dump that no formula can read without manual cell references.

### B5. Refresh and staleness
Only **1 of 17** query tables has `refreshOnLoad` set. Every connection has `saveData="1"`. Opening
the file therefore shows you cached data of unknown age with nothing on screen indicating when it
was pulled.

### B6. There is no analysis layer
All 19 sheets are landing pads for extracts. No charts, no pivots, no named ranges, no comparison
sheet. The workbook is a data dump, not a model — which is exactly why building a second workbook
from it feels awkward.

---

## C. Fitness for the stated goal

### C1. LJPC1 cannot participate in a temperature comparison
Non-null counts by station:

| variable | 46254 | LJAC1 | LJPC1 |
|---|---:|---:|---:|
| `wtmp (degree_C)` | 2,167 | 7,767 | **0** |
| `atmp (degree_C)` | **0** | 5,595 | **0** |
| `wvht (m)` | 2,167 | 0 | 1,101 |
| `wspd (m s-1)` | 0 | 9,284 | 1,102 |

LJPC1 returns **zero** water temperature and **zero** air temperature across all 1,104 rows — it's a
wave/wind station. It is in the station regex, in both metadata queries, and on its own sheet, and
it contributes nothing to your objective. 46254 has water temp but no air temp. **LJAC1 is the only
station with both.**

### C2. Unit mismatch
`src_yellow_buoy` is in **°F** (`Tidbit 1 , °F`, range 63.96–75.35). Every other temperature series
is in **°C**. Nothing in the workbook converts. The column header also carries a stray comma from
the HOBO export.

### C3. The yellow buoy sheet is missing 7 samples from its own export
`meta_yellow_buoy` reports:
- Samples: **3,029** · First: 2026/07/11 **07:00** · Last: 2026/08/01 **07:40**

The data sheet has **3,022** rows running **08:00 → 07:30**. Six rows lopped off the head, one off
the tail. Spacing is a clean 10 minutes with no exceptions across all 3,022 rows, so this is a paste
range error, not a logger gap.

### C4. The correlation you're looking for is not present at lag 0
Hourly means over the 504-hour overlap window:

| pair | r |
|---|---:|
| yellow_buoy ↔ Scripps CTD | **0.13** |
| yellow_buoy ↔ LJAC1 | **0.07** |
| yellow_buoy ↔ 46254 | **0.22** |
| *Scripps CTD ↔ LJAC1 (reference check)* | *0.97* |

The reference stations agree with each other almost perfectly. The yellow buoy does not agree with
any of them. Scanning lags from −24 h to +24 h, the yellow buoy peaks at **+8 hours, r = 0.72**.

Mean by local hour:

| | 00 | 03 | 06 | 09 | 12 | 15 | 18 | 21 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| yellow_buoy | 21.47 | 22.01 | 21.82 | 21.24 | 20.84 | 21.41 | 22.05 | 21.98 |
| Scripps CTD | 22.52 | 22.26 | 22.85 | 23.53 | 23.24 | 22.52 | 23.08 | 22.80 |

The yellow buoy troughs around 11:00–12:00 and peaks around 19:00–20:00. The CTD peaks around
09:00–10:00 and troughs around 01:00–04:00. They are close to **anti-phase**. Mean daily range is
comparable (2.98 °C vs 3.28 °C), so it isn't a sensitivity difference.

Mean offsets vs yellow_buoy: CTD +1.22 °C, LJAC1 +1.03 °C, 46254 +2.19 °C.

Against tide, yellow_buoy ↔ water level r = **−0.05**, so it isn't a simple tidal signal either.

**Two candidate explanations, and this is blocking:**

1. **Clock or export-timezone error.** An 8-hour shift is suspiciously close to the 7-hour PDT↔UTC
   offset. If HOBOconnect exported in UTC and the header labelled it PDT, that alone would produce
   most of what's observed. Note that A2 shows the ERDDAP side is *also* off by 7 hours, so there
   are already two independent timezone faults in this workbook.
2. **It's real.** A shallow, sun-exposed deployment lagging solar forcing while the pier CTD at depth
   is driven by internal tides would genuinely look like this.

Nothing inside this workbook can distinguish them. `meta_yellow_buoy` records `Location: Off`, so
there is no latitude, longitude, or deployment depth recorded for the logger.

### C5. Sampling cadences for interval design

| series | nominal cadence |
|---|---|
| yellow_buoy | 10 min, perfectly regular |
| LJAC1 | 6 min, with gaps at 12/18/24/30/48 min |
| 46254 | 30 min (41 gaps of 60 min) |
| Scripps CTD | ~4 min nominal, irregular (see B3) |
| water level | 6 min |

Any averaging interval below 30 minutes leaves 46254 with at most one sample per bin.

---

## D. Recommended rebuild

### Principle
Excel stops being the pipeline and becomes an output format. Fetching, aligning, and resampling move
to Python, where they're testable and where the interval isn't frozen into the stored data.

### Folder layout

```
lajolla/
├── config/
│   └── sources.yaml          # single source of truth: stations, URLs, units, tz, QC policy
├── sources/                  # read-only; the script only ever reads from here
│   ├── ja_jolla_sensors.xlsx
│   └── yellow_buoy.xlsx
├── cache/                    # raw API pulls, parquet, timestamped filenames
├── outputs/                  # generated workbooks + charts; NEVER scanned as input
├── compare.py                # the GUI
├── ingest.py                 # fetch + normalise
└── run.ps1                   # double-click / right-click → Run with PowerShell
```

The `sources/` vs `outputs/` split gives you the separation you asked for, enforced by the discovery
function rather than by naming discipline.

### Canonical internal shape
One tidy long table, everything normalised on ingest:

```
timestamp_utc | source | station | variable | value | unit | qc_flag | depth_m
```

- **All timestamps stored UTC.** Local time computed only for display, via `zoneinfo`
  (`America/Los_Angeles`). Delete `fnToLocal`.
- **All temperatures stored °C.** The °F → °C conversion happens once, at the boundary, and is
  recorded in the unit column.
- **QC policy lives in config**, not scattered across queries, and applies uniformly.

Resampling then happens at query time with `df.resample(interval).agg(...)`, so 5 min, 15 min, 6 h,
and daily all come free. `BinKey*` columns disappear entirely.

### GUI
Tkinter is the pragmatic choice — it ships with Python, so `run.ps1` needs no virtualenv activation
for the UI layer, and it behaves on Windows. Layout:

- **Left:** tree of `sources/` → workbook → table → column, with checkboxes. Columns annotated with
  non-null count and detected unit so LJPC1's empty temperature columns are visibly empty *before*
  you select them.
- **Right:** interval dropdown, aggregation (mean/median/min/max/count), date range (defaulting to
  the intersection of selected series), minimum-coverage threshold, optional lag scan range.
- **Generate** → writes a timestamped workbook to `outputs/`.

### Output workbook
- `data` — aligned wide table, one row per interval, plus a per-cell sample-count companion so you
  can see which averages are thin.
- `stats` — correlation matrix, N per pair, mean offset, best lag and its r.
- `chart` — line chart, secondary axis when units differ, plus a scatter for the strongest pair.
- `provenance` — source file, URL, fetch timestamp, row counts, QC policy, and every parameter used.

That last sheet is the thing this workbook most conspicuously lacks, and it's what would have caught
A2 and C3 immediately.

---

# Resolved

*Dated 2026-08-05. Each line says what actually fixed it. Findings stay in place
above; this section records their disposition rather than deleting the history.*

| # | Finding | Resolution |
|---|---|---|
| **A1** | `meta_ERDDAP` duplicates `meta_COOPS_datums` | Query deleted; its stale sheet is dropped by `ingest.mashup.configure_loading`. `meta_ERDDAP_sccoos` and `meta_ERDDAP_ndbc` were already the real ERDDAP metadata queries. |
| **A2** | Date parameters carry literal quote characters; the window is not the one requested | `p_StartUTC` / `p_EndUTC` are now computed from `DateTimeZone.UtcNow()` with an explicit format string. The triple-quoted literals are gone. Gate 1.5 checks for a literal `"` in any parameter. |
| **A3** | `src_NDBC` ignores the parameters entirely | Its URL is built from `p_StationFilter` and `p_TimeFilter`, both derived from the parameters. |
| **A4** | `p_NDBCStations` is dead code; the station list is hardcoded three times | Now a real M list, consumed by `p_StationFilter` (used by `src_NDBC` and `meta_NDBC_positions`), by `meta_NDBC_stations`' row filter, and by `meta_refresh`. |
| **A5** | Four competing time conventions in one workbook | Every `src_*` query emits exactly one column, `time_utc`, built from parts by `fnParseUtc`. `fnToLocal` is deleted. Local time is derived in Python via `zoneinfo` and is a display concern only. |
| **A6** | The `_ok` columns do not do what their name says | Replaced by `fnQc`: flags 4 (fail) and 9 (missing) are rejected, 1/2/3 pass, and the raw `*_qc_agg` columns are kept so the provenance sheet can report how many suspect values were accepted. |
| **B1** | The NDBC payload is stored twice | `src_NDBC` is now a staging query and is not materialised to a sheet; only the three per-station tables load. |
| **B2** | `fnBinKeys` is the wrong abstraction | Function and all `BinKey*` columns removed. Resampling is a query-time operation in `sensorkit`. `NOISE_COLUMNS` still filters `BinKey*` as a safety net for old snapshots. |
| **B5** | Refresh and staleness | `meta_refresh` records `fetched_utc`, the window, the station list and `m_version` on every refresh; `project.py` copies them into `manifest.json`, and marks a project `incomplete` rather than guessing if the sheet is absent. |
| **C3** | The yellow buoy sheet is missing 7 samples from its own export | The logger export is read directly from `sources/yellow_buoy_temps.xlsx` into the canonical frame, so the count is whatever the file holds; coverage and largest gap are reported per series in `validation.json`. |
| **C4** | "The correlation you're looking for is not present at lag 0" | **This conclusion was wrong, and the reason matters.** The near-zero correlation and the apparent anti-phase were entirely artifacts of **A2/A5** — the buoy (honestly labelled PDT) was being compared against columns labelled `time (UTC)` that actually held Pacific local time, a 7-hour error. On one clock the buoy correlates with LJAC1 at **r = 0.664 at lag 0**, rising to **0.723 at −1 h**, and with autoss at **0.660 / 0.715**. The buoy *leads* the pier by about an hour. There is no anti-phase and no 8-hour offset; neither number was ever real. The remaining weak correlation against 46254 (r = 0.351) is not a timing problem either — 46254 follows the sea surface while the logger sits on the seabed, so they are in different reference frames. |

## Still open

- **B3** (~25% of every SCCOOS table is padding) — moot for the Python ingest,
  which stores a long frame, but still true of the workbook.
- **B4** (yellow buoy sheets are second-class) — improved: the buoy is a
  first-class station in `config/stations.yaml` with `role: subject`. Its
  coordinates and deployment depth are still `TODO(verify)`; the HOBOconnect
  export records neither.
- **B6** (no analysis layer) — addressed by `sensorkit` + `exporter`, but the
  stratification index is currently the only derived quantity.
- **C1** (LJPC1 cannot participate in a temperature comparison) — confirmed at
  source and recorded in `config/stations.yaml` as `role: context_only`, so it
  cannot be re-added to the temperature pipeline by accident.
- **C2** (unit mismatch) — handled on load; °F becomes °C once, at ingest.
- **C5** (sampling cadences) — now measured per series into `validation.json`
  rather than assumed.
