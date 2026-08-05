# Agent task — project snapshots, corrected Power Query, and a three-mode UI

**Repo:** `C:\Projects\la-jolla-buoy\station-data-extract`
**Audience:** a CLI coding agent working directly in this repo.
**Written:** 2026-08-05. Everything in *Background* was established by analysis of the actual
data — treat it as fact, not as suggestion.

Read this whole file before editing anything. Work in the phase order given. Each phase has an
acceptance gate; do not start the next phase until the previous one passes.

---

## 0. Background you must know

These findings came out of a long analysis session. They are load-bearing. Do not "improve" on
them without evidence.

### 0.1 The workbook's time columns both lie

`sources/ja_jolla_sensors.xlsx` has a column labelled **`time (UTC)`** in `src_NDBC`,
`src_LJAC1`, `src_LJPC1` and `src_46254`. **It contains Pacific local time, not UTC.**

Verified with two independent absolute clocks, fitting a harmonic to the hour-of-day composite
of the raw column value with no timezone assumed:

| signal | physical maximum | observed at column hour |
|---|---|---|
| LJAC1 **air temperature** | ~14:50 PDT (≈2 h after solar noon at −117.257°) | **14.45** |
| LJAC1 **barometer, S2 tide** | ~10:49 PDT (10:00 local solar) | **10.67** |

Two unrelated geophysical clocks agree within 0.25 h. If the column were UTC, air temperature
at Scripps Pier would peak at 07:26 local. It does not.

Consequently:

| column | claims to be | actually is |
|---|---|---|
| `time (UTC)` | UTC | **Pacific local** |
| `time (local)`, `time (PDT)` | Pacific local | **Pacific local − 7 h** (`fnToLocal` applied to already-local data) |

**Root cause:** the M code lets Power Query type-convert the ERDDAP ISO-8601 string
(`2026-07-11T14:00:00Z`) to `datetime`, and that conversion applies the machine's local offset
and discards the zone. Then `fnToLocal` subtracts 7 more hours.

**Consequences already measured.** Comparing the yellow buoy (labelled PDT) against these
columns read as UTC gave r = 0.119 vs the Scripps CTD and 0.084 vs LJAC1. With both series on
the same clock: **r = 0.685 and 0.703**, buoy leading the pier by ~0.8 h. The "8-hour phase
offset" and the "anti-phase" finding in `AUDIT.md` C4 are **artifacts of this bug**. The
README/AUDIT `−8 h` vs `+8 h` sign dispute is moot — neither number was real.

### 0.2 `sensorkit.py` currently trusts the lie

`sensorkit.TIME_PREFERENCE` puts `time (utc)` first, and its comment says *"UTC always wins so
we never have to trust a local label."* `to_utc()` then does `tz_localize("UTC")` on it. Given
0.1, **that is exactly backwards for this workbook.** This must be fixed at the source (the M),
and `sensorkit` must stop asserting a zone on the basis of a column name alone.

### 0.3 Sensor geometry — reference frames, not just depths

| sensor | depth | fixed to | frame |
|---|---|---|---|
| **Yellow buoy** (the subject) | on the anchor, on the seabed | the seabed | **Earth** |
| **SCCOOS autoss, Scripps Pier** | **5.0 m below MLLW** | pier piling | **Earth** |
| **LJAC1 / 9410230** (NOS sensor E1) | **−11.255939 ft = −3.431 m below MLLW** | pier piling | **Earth** |
| **46254 / CDIP 201** | ~0.45 m below the surface, on a 0.9 m sphere | **the sea surface** | **surface** |

MSL is **0.832 m above MLLW** at 9410230 (NAVD88, epoch 1983–2001). Below MSL: LJAC1 4.26 m,
autoss 5.83 m — a constant 1.57 m separation.

The logger is Earth-frame, so **autoss is the primary reference, LJAC1 the cross-check, and
46254 is NOT a peer** — it is the surface endpoint of a stratification index
(`SST(46254) − T(autoss)`). Every chart legend must carry sensor depth and frame.

**Depth is not in the ERDDAP feed.** Axiom's ERDDAP sets `z = 0.0 m` for both the Scripps Pier
station and CDIP 201. Depths must be hardcoded in `config/stations.yaml`.

### 0.4 Which SCCOOS feed

- ✅ **`https://erddap.cencoos.org/erddap/tabledap/scripps-pier-automated-shore-sta-1`** — live,
  2013-01-18T22:29:25Z → present, full QARTOD `_qc_agg` + 11-char `_qc_tests`. **This is
  already what the workbook pulls** (`meta_ERDDAP_sccoos` records
  `time:actual_range = 1.358548165E9 …`, which is 2013-01-18T22:29:25Z, matching this dataset
  exactly). Do not change the source.
- ❌ `https://erddap.sccoos.org/erddap/tabledap/autoss` — **frozen since 2019-09-18**. Never use.
- ⚠️ `https://thredds.sccoos.org/thredds/dodsC/autoss/scripps_pier-<YYYY>.nc` — only for
  2005–2012 backfill and for the true `depth = 5.0` attribute.

### 0.5 NDBC realtime2 format

`https://www.ndbc.noaa.gov/data/realtime2/<STATION>.<ext>`,
`https://www.ndbc.noaa.gov/data/derived2/<STATION>.dmv`. No auth, ~45-day rolling window.
**"Both Realtime and Historical files show times in UTC only"** — the zone is documented, never
embedded. Traps: rows are **newest-first**; two `#` header lines; the month column is named
`MM`, the same token as the missing sentinel; there are **four** missing sentinels (`MM`, `N/A`,
`-99`, `9.999`) and historical files use `999`/`99.0`/`9999`; per-variable cadence differs from
file cadence (LJAC1's file is 6-min but ATMP and PTDY are hourly).

Station reality, verified at source: **LJPC1 reports no water and no air temperature at all**
(it is wave/wind only — this confirms AUDIT C1 and is not a query bug). 46254 has WTMP but no
ATMP. **LJAC1 is the only station with both**, which makes it the permanent clock anchor.
`TIDE` is empty at all three — get water level from the CO-OPS API.

### 0.6 The 45-day problem

NDBC realtime2 and the ERDDAP feeds hold a rolling window. **Data older than the window is lost
unless you keep it.** That is the whole reason for timestamped project snapshots, and it is why
Phase 4 (the merged archive) matters more than it looks.

---

## 1. Non-goals and guardrails

Do **not**:

- Reintroduce `fnBinKeys` or any precomputed `BinKey*` column. Resampling is a query-time
  operation. `sensorkit.NOISE_COLUMNS` already filters them; keep that filter as a safety net
  for old snapshots.
- Keep `fnToLocal`. Delete it.
- "Fix" the timezone by adding a constant 7 or 8 hours anywhere. Parse correctly instead.
- Store derived columns that depend on a choice not yet made (`wd_sin`, `wd_cos`, bin keys).
- Write anything into `sources/`. It is read-only, enforced in `build_catalog`.
- Scan `outputs/` as input. Ever.
- Add dual-axis charts. Use the existing z-score sheet to compare shape across units.
- Delete `AUDIT.md`. Move resolved findings into a `## Resolved` section with dates.
- Invent ERDDAP dataset IDs, NDBC station IDs, or URLs. If you cannot verify one, leave a
  `# TODO(verify)` and say so in your summary.
- Commit `.venv/`, `projects/`, `outputs/`, `*.xlsx`, `*.parquet`, or `~$*` files.

Do:

- Keep `sensorkit` and `exporter` free of GUI dependencies. They must stay scriptable.
- Preserve the existing behaviours that are already right: `sources/` ↔ `outputs/` separation
  enforced in code; the `counts` sheet with thin bins shaded; min-samples-per-bin as a
  first-class control; the `provenance` sheet; the lag scan reporting its **±12.42 h M2 alias**
  on every result.

---

## 2. Target layout

```
station-data-extract/
├── AGENT_TASK.md              ← this file
├── README.md                  ← update at the end
├── AUDIT.md                   ← add a "## Resolved" section
├── config/
│   └── stations.yaml          ← single source of truth: ids, coords, depths, frames, endpoints
├── ingest/
│   ├── __init__.py
│   ├── clockcheck.py          ← drop in if present; else implement per §6
│   ├── ndbc_realtime.py       ← drop in if present; else implement per §0.5
│   └── refresh.py             ← NEW: Excel COM refresh + snapshot
├── project.py                 ← NEW: project create/load/list, manifest, validation
├── archive.py                 ← NEW: merged long-window archive
├── compare.py                 ← project-aware; three-mode launcher
├── sensorkit.py               ← time contract fix; parquet support; project roots
├── exporter.py                ← provenance + legend changes
├── sources/                   ← unchanged, read-only, the live PQ workbook lives here
├── projects/                  ← NEW: timestamped immutable snapshots
│   └── 20260805T1432Z__baseline/
│       ├── manifest.json
│       ├── validation.json
│       ├── workbook/ja_jolla_sensors.xlsx   ← refreshed copy, immutable
│       ├── cache/*.parquet                  ← canonical long frame
│       └── outputs/                         ← workbooks generated against THIS project
├── archive/
│   ├── observations.parquet
│   └── revisions.jsonl
├── requirements.txt
└── run.ps1
```

---

## 3. Phase 1 — fix the Power Query M

This is first because everything downstream inherits it. Work in the Power Query editor of
`sources/ja_jolla_sensors.xlsx`, or edit the M and re-import — your choice, but the workbook in
`sources/` is the one that must end up correct.

### 3.1 Add `fnParseUtc`, delete `fnToLocal`

Create a new query `fnParseUtc`. It builds the datetime **from parts** on purpose: no locale, no
machine timezone, no inference. The result is a naive `datetime` that **is** UTC by construction.

```m
// fnParseUtc
// Parse an ISO-8601 / CO-OPS-GMT timestamp into a NAIVE datetime that IS UTC.
// Accepts "2026-07-11T14:00:00Z", "2026-07-11T14:00:00", "2026-07-11 14:00".
// Built from parts deliberately - M's implicit datetime conversion applies the
// machine's local offset and discards the zone, which is what broke this workbook.
(t as any) as nullable datetime =>
let
    s0  = if t = null then null else Text.Trim(Text.From(t)),
    s   = if s0 = null then null
          else Text.Replace(Text.Replace(Text.Replace(s0, "T", " "), "Z", ""), "+00:00", ""),
    ok  = s <> null and Text.Length(s) >= 16,
    dp  = if ok then Text.Split(Text.Start(s, 10), "-") else null,
    tp  = if ok then Text.Split(Text.Trim(Text.Middle(s, 11, 8)), ":") else null,
    sec = if ok and List.Count(tp) >= 3 then Number.FromText(tp{2}) else 0,
    out = if not ok then null else
          #datetime(Number.FromText(dp{0}), Number.FromText(dp{1}), Number.FromText(dp{2}),
                    Number.FromText(tp{0}), Number.FromText(tp{1}), sec)
in
    out
```

**Critical:** the raw time column must reach `fnParseUtc` **as text**. In every `Csv.Document` /
`Json.Document` / `Web.Contents` step, remove `time` from any
`Table.TransformColumnTypes(..., {{"time", type datetime}})`. Keep it `type text` until
`fnParseUtc` runs. If Power Query has already converted it, the damage is done and this function
cannot undo it.

Then **delete `fnToLocal` and every reference to it.**

### 3.2 One time column, honestly named

Every `src_*` query emits exactly one time column: **`time_utc`**, type `datetime`, containing
UTC.

Delete `time (UTC)`, `time (local)`, `time (PDT)`. Do not emit a local-time column at all —
local time is a display concern and `sensorkit` derives it via `zoneinfo`.

### 3.3 Fix the parameters

The triple-quoting bug (`let Source = """2026-07-11T14:00:00Z""" in Source`) produces a string
*containing* quote characters, which lands in the URL. Replace with dynamic UTC parameters:

```m
// p_StartUTC
DateTime.ToText(DateTimeZone.RemoveZone(DateTimeZone.UtcNow()) - #duration(45, 0, 0, 0),
                [Format = "yyyy-MM-ddTHH:mm:ss", Culture = "en-US"]) & "Z"

// p_EndUTC
DateTime.ToText(DateTimeZone.RemoveZone(DateTimeZone.UtcNow()),
                [Format = "yyyy-MM-ddTHH:mm:ss", Culture = "en-US"]) & "Z"
```

45 days is the NDBC realtime window; the ERDDAP sources will happily serve it too. Every
refresh then pulls the maximum available window and the snapshot captures it.

### 3.4 Make `p_NDBCStations` do its job

It is currently defined, loaded to a sheet, and **referenced by no query**, while the station
list is hardcoded in `src_NDBC`'s URL, in `meta_NDBC_stations`' row filter, and in
`meta_NDBC_positions`' URL — and `src_9410230_wl` hardcodes its dates in a *fourth* format
(`begin_date=20260711`).

```m
// p_NDBCStations
{"LJAC1", "LJPC1", "46254"}
```

Build every station constraint and every date constraint from the parameters.
**Do not invent a new ERDDAP constraint syntax** — take the constraint form already working in
`src_NDBC`'s URL and parameterize it with `Text.Combine(p_NDBCStations, "|")` (or whatever
separator that form already uses).

`src_9410230_wl` must use `p_StartUTC`/`p_EndUTC` reformatted to CO-OPS' `yyyyMMdd`, and must
pass **`time_zone=gmt`**, not `lst_ldt`. If it currently uses `lst_ldt`, that column is local and
must be switched, not converted.

### 3.5 Stop storing the payload twice

`src_NDBC` holds 12,607 rows; `src_LJAC1` + `src_LJPC1` + `src_46254` hold 9,336 + 1,104 + 2,167
= 12,607. The same data, materialized twice, is most of the workbook's 4 MB.

Make `src_NDBC` a **staging query with "Enable load" off**, and load only the three per-station
tables. Filter at the query, not on a materialized table.

### 3.6 Drop the stored derived columns

Remove `wd_sin` / `wd_cos` from all stored tables. The instinct is right — you cannot
arithmetic-mean a compass bearing — but it belongs at aggregation time in `sensorkit`, and the
current version is **unweighted**, which gives mean *direction*, not mean *vector wind*. If a
vector wind is wanted later, compute it in Python as
`u = -WSPD·sin(WDIR·π/180)`, `v = -WSPD·cos(WDIR·π/180)` (negative because WDIR is the direction
the wind comes *from*).

Remove the `Table.Sort` step — sorting a stored table costs refresh time and buys nothing.

Remove every `fnBinKeys` call and the function itself.

### 3.7 Fix the QC screen

The `*_ok` columns currently null only QARTOD flag 4, letting 3 (suspect) and 9 (missing)
through under a reassuring name. Replace with:

```m
// fnQc  - QARTOD: 1 good, 2 not evaluated, 3 suspect, 4 fail, 9 missing
(value as nullable number, flag as nullable number) as nullable number =>
    if flag = null then value
    else if flag = 4 or flag = 9 then null
    else value          // 1, 2 and 3 pass; 3 is kept but must stay flagged
```

**Keep the raw `*_qc_agg` flag columns in the output** so Python can mark suspect values on the
provenance sheet rather than silently accepting them.

### 3.8 Fix `meta_ERDDAP` and add `meta_refresh`

`meta_ERDDAP` currently fetches the **COOPS datums JSON** for 9410230 — the same URL and steps
as `meta_COOPS_datums`, byte-for-byte identical output. It contains no ERDDAP metadata. Either
point it at a real ERDDAP `.das`/`info` endpoint or delete it.

Add a new one-row query so every snapshot self-documents when it was pulled:

```m
// meta_refresh
let
    now = DateTimeZone.RemoveZone(DateTimeZone.UtcNow()),
    txt = DateTime.ToText(now, [Format = "yyyy-MM-ddTHH:mm:ss", Culture = "en-US"]) & "Z",
    t = #table(
          type table [key = text, value = text],
          {
            {"fetched_utc",    txt},
            {"p_StartUTC",     p_StartUTC},
            {"p_EndUTC",       p_EndUTC},
            {"p_NDBCStations", Text.Combine(p_NDBCStations, ",")},
            {"m_version",      "2026-08-05-a"}
          })
in
    t
```

This is the single most valuable addition to the workbook — it turns the `provenance` sheet from
"this came from a workbook" into a real record.

### 3.9 Refresh behaviour

Set **`refreshOnLoad` on every loaded query** (currently only 1 of 17 has it). Leave
`saveData="1"` so a snapshot workbook is self-contained after the refresh.

### Gate 1 — do not proceed until all of these pass

1. `fnToLocal` and `fnBinKeys` do not appear anywhere in the workbook.
2. Every `src_*` table has exactly one time column, named `time_utc`.
3. Refresh the workbook, then run:
   ```
   python -m ingest.clockcheck --workbook sources/ja_jolla_sensors.xlsx \
       --sheet src_LJAC1 --time time_utc --lon -117.257
   ```
   Both the air-temperature and barometric checks must return **OK with |offset| < 1.5 h**.
   (Before the fix, air temperature returns FAIL at −7.37 h. That is the regression test.)
4. `p_NDBCStations`, `p_StartUTC`, `p_EndUTC` are each referenced by at least one query.
5. No parameter value contains a literal `"` character.

---

## 4. Phase 2 — projects

### 4.1 `config/stations.yaml`

Create it. It is the single source of truth; nothing downstream hardcodes a coordinate, a depth,
a station id, or an endpoint. Minimum content per station: `id`, `name`, `lon`, `lat`,
`sensor_depth_m`, `depth_reference` (`MLLW` / `sea_surface`), `reference_frame`
(`earth` / `surface`), `role` (`subject` / `primary_reference` / `cross_check` /
`surface_endpoint` / `context_only`), `variables`, `endpoints`, `cadence_min`, and a free-text
`note`. Populate from §0.3 and §0.4.

Include `LJPC1` with `role: context_only` and a note that it reports no temperature, so nobody
re-adds it to the temperature pipeline.

### 4.2 `project.py`

```python
PROJECTS_DIRNAME = "projects"
SCHEMA_VERSION = 1

def new_project_id(label: str, now_utc: datetime) -> str:
    """'20260805T1432Z__baseline'. UTC, sortable, no characters Windows rejects."""

def create_project(root: Path, label: str, *, refresh: bool = True) -> Path:
    """Create projects/<id>/, refresh+snapshot the workbook, normalise to cache/,
    validate, write manifest.json and validation.json. Returns the project path."""

def list_projects(root: Path) -> list[ProjectInfo]:
    """Newest first. Reads manifest.json only - must stay fast enough for a UI list."""

def load_project(path: Path) -> ProjectInfo:
def latest_project(root: Path) -> ProjectInfo | None:
```

`ProjectInfo` is a dataclass carrying at least: `path`, `project_id`, `label`, `created_utc`,
`status` (`ok` / `failed_validation` / `incomplete`), `stations`, `time_min_utc`,
`time_max_utc`, `n_rows`, `validation_summary`.

**`manifest.json`** must record enough to reproduce the snapshot:

```json
{
  "schema_version": 1,
  "project_id": "20260805T1432Z__baseline",
  "label": "baseline",
  "created_utc": "2026-08-05T14:32:07Z",
  "tool_version": "station-data-extract 2026-08-05-a",
  "source_workbook": {
    "path": "sources/ja_jolla_sensors.xlsx",
    "sha256": "...",
    "mtime_utc": "...",
    "m_version": "2026-08-05-a",
    "fetched_utc": "2026-08-05T14:31:52Z",
    "p_StartUTC": "2026-06-21T14:31:52Z",
    "p_EndUTC": "2026-08-05T14:31:52Z"
  },
  "config_snapshot": { "...": "verbatim copy of config/stations.yaml" },
  "series": [
    {"station": "LJAC1", "variable": "sea_water_temperature", "unit": "degC",
     "n": 9336, "time_min_utc": "...", "time_max_utc": "...",
     "median_step_min": 6.0, "qc_policy": "accept 1,2; flag 3; reject 4,9",
     "n_flagged_suspect": 0, "sensor_depth_m": 3.431, "reference_frame": "earth"}
  ],
  "validation": {"status": "ok", "clock_checks": [ /* see 6 */ ]},
  "notes": ""
}
```

`fetched_utc`, `p_StartUTC`, `p_EndUTC` and `m_version` come from the `meta_refresh` sheet added
in §3.8. If that sheet is absent, set them to `null` and mark the project
`status: "incomplete"` — do not guess.

**Projects are immutable once created**, apart from `outputs/`. If `create_project` fails
partway, leave the directory with `status: "incomplete"` rather than deleting it; the UI shows
it greyed out. Never silently discard evidence of a failed pull.

### 4.3 `ingest/refresh.py` — Excel COM refresh

```python
def refresh_and_snapshot(src_xlsx: Path, dest_xlsx: Path, timeout_s: int = 600) -> dict:
    """Refresh all Power Query connections in src_xlsx and SaveAs to dest_xlsx.
    Never writes back to src_xlsx. Returns the meta_refresh key/value dict."""
```

Implementation notes that will save you an afternoon:

- Requires `pywin32` and a real Excel install. Add `pywin32` to `requirements.txt` and guard the
  import so the rest of the tool still runs without it.
- Use `DispatchEx("Excel.Application")` (a fresh instance), not `Dispatch` — `Dispatch` attaches
  to the user's open Excel and will fight them.
- **Refuse to run if a `~$<name>.xlsx` lock file exists** next to the source, or if the file is
  open. Raise a clear error telling the user to close the workbook. Orphaned `~$` lock files
  have already been observed in `outputs/`, so this is a real failure mode, not a hypothetical.
- `RefreshAll()` is asynchronous. Force synchronous behaviour first:
  ```python
  xl = win32.DispatchEx("Excel.Application")
  xl.Visible = False
  xl.DisplayAlerts = False
  wb = xl.Workbooks.Open(str(src.resolve()), UpdateLinks=0, ReadOnly=False)
  for conn in wb.Connections:
      try:
          conn.OLEDBConnection.BackgroundQuery = False
      except Exception:
          pass          # not every connection type exposes it
  wb.RefreshAll()
  xl.CalculateUntilAsyncQueriesDone()
  wb.SaveAs(str(dest.resolve()), FileFormat=51)   # xlOpenXMLWorkbook
  wb.Close(SaveChanges=False)
  xl.Quit()
  ```
  Wrap all of it in `try/finally` so Excel is always quit, and poll with a wall-clock timeout —
  a privacy-level or credential prompt will otherwise hang forever with `Visible = False`.
- **Privacy levels**: a first run may block on "Information is required about data privacy". This
  cannot be set reliably over COM. Document a one-time manual step in `README.md`:
  *Data → Get Data → Query Options → Privacy → "Always ignore Privacy Level settings"*.
- After `SaveAs`, read the `meta_refresh` sheet out of `dest_xlsx` with `openpyxl` and return it.
- **`SaveAs` to the project folder. Never save over `sources/`.**

Provide a `--no-refresh` path (`create_project(refresh=False)`) that just copies the current
workbook — needed for testing, and for when Excel isn't available.

### 4.4 Normalise into `cache/`

After the snapshot, convert every `src_*` table into the canonical long frame and write parquet
into `<project>/cache/`:

```
time_utc | station | variable | value | unit | qc_flag | depth_m | source | fetched_utc
```

- `time_utc` tz-aware UTC.
- `variable` uses CF standard names (`sea_water_temperature`, `air_temperature`,
  `wind_from_direction`, …) so `WDIR` / `wd` / `WTMP` / `wtmp_ok` all map to one row type.
- `unit` canonical: temperatures in **degC**, converted once here.
- `depth_m` and `reference_frame` joined from `config/stations.yaml` — **not** from the feed,
  which reports `z = 0.0` (§0.3).
- Long, not wide, because stations report different variables at different cadences and a wide
  table forces an interval choice before the user has made one.

### Gate 2

1. `python -c "import project; project.create_project(Path('.'), 'smoke', refresh=False)"`
   produces a complete `projects/<id>/` with all four artifacts and `status: "ok"`.
2. `list_projects` returns it, newest first, in under 200 ms with 20 projects present.
3. `sources/` is byte-identical before and after (check a hash).
4. A project created with `refresh=True` has a non-null `fetched_utc` in its manifest.

---

## 5. Phase 3 — `compare.py` gets three modes

### 5.1 Launcher

Before the main window, show a small modal `ProjectChooser` (Tk, ~460×300, centred):

1. **New project** — text field for a label (slugified; default `session`), a
   *Refresh data from Excel* checkbox (default on), then Create. Show a progress/log dialog
   during the refresh — it takes tens of seconds and must not look frozen. On success, open the
   main window on the new project. On validation failure, show the failing clock check verbatim
   and ask *Open anyway / Discard*.
2. **Analyze current data** — opens the main window on `latest_project()`. If there are no
   projects, fall back to the existing behaviour (scan `sources/`) and say so in the log.
3. **Compare data from an existing project** — a list of projects showing label, `created_utc`,
   stations, coverage span, row count and status. Selecting one opens the main window on it.
   **Allow multi-select of up to two projects**; when two are chosen, series labels are prefixed
   with the project label (`baseline: LJAC1 wtmp`) so the same station can be compared across
   snapshots. Grey out `status != "ok"` projects but allow opening them with a warning.

### 5.2 Make the app project-aware

`compare.py` currently hardcodes `ROOT = Path(__file__).resolve().parent` and calls
`sk.build_catalog(ROOT)`, which scans `ROOT/sources`. Replace with `self.project: ProjectInfo`
and:

- Toolbar shows the project label and `created_utc` next to the existing *Sources:* label.
- `rescan()` builds the catalog from the project (`workbook/*.xlsx` **and** `cache/*.parquet`),
  not from `ROOT/sources`.
- `generate()` writes into `<project>/outputs/`, and *Open outputs folder* opens that.
- Add **File → Switch project…** which reopens the chooser without restarting.
- The window title carries the project id.

### 5.3 `sensorkit.py` changes

- **`TIME_PREFERENCE` / `to_utc()`.** After Phase 1 the only time column is `time_utc`. Make
  `TIME_PREFERENCE` match `^time_utc$` → `"UTC"` first, keep the legacy patterns *below* it, and
  **change the comment** — the current one ("UTC always wins so we never have to trust a local
  label") is what the bug hid behind. Replace it with a pointer to §0.1: a column name is not
  evidence of a zone; only `clockcheck` is.
- When a table offers a legacy `time (UTC)` column and **no** `time_utc`, treat it as
  **unverified**: load it, but set a flag on `TableInfo` so the UI shows the table in amber and
  the provenance sheet records `time_basis: "UTC (unverified — legacy column, see AGENT_TASK §0.1)"`.
  Do not silently apply a correction.
- `build_catalog(root, *, sources_dirname=SOURCES_DIRNAME)` — add the keyword so a project can
  point it at `workbook/`. Add `build_catalog_project(project)` that scans both `workbook/*.xlsx`
  and `cache/*.parquet`.
- `_read_table` dispatches on suffix; add a parquet path and a `scan_parquet()` that yields
  `TableInfo` from the long frame (one `TableInfo` per `(station, variable)`).
- Keep `NOISE_COLUMNS` filtering `BinKey*` — old snapshots still contain them.
- Add `depth_m` and `reference_frame` to `ColumnInfo`, populated from `config/stations.yaml`.

### 5.4 `exporter.py` changes

- **Legends carry depth and frame**: `LJAC1 wtmp (3.4 m, earth)`, `46254 SST (0.45 m, surface)`,
  `yellow buoy (bed, earth)`. `_legend_label` is the place.
- **Provenance sheet** gains: project id and label, `created_utc`, `fetched_utc`, source workbook
  sha256, `p_StartUTC`/`p_EndUTC`, `m_version`, the QC policy string, per-series counts of
  QARTOD-3 (suspect) values kept, and the full clock-check verdicts.
- **Stratification index**: when both `46254` SST and `autoss` temperature are selected, offer an
  extra derived series `SST(46254) − T(autoss)` and plot it on the charts sheet as its own panel.
  This is the covariate that says when the buoy and the pier should agree — see §0.3.
- Keep the ±12.42 h M2 alias on every lag result. Keep the no-dual-axis rule.

### Gate 3

1. All three modes open the main window on the right project.
2. Generating a comparison writes into `<project>/outputs/` and nowhere else.
3. A legend entry shows depth and frame.
4. The provenance sheet shows a non-null `fetched_utc` and the clock-check verdicts.
5. `python -c "import sensorkit, exporter"` works with no Tk import.

---

## 6. Phase 4 — validation and the long archive

### 6.1 `ingest/clockcheck.py`

If the file has been dropped into the repo, use it. If not, implement:

`verify_utc(times, values, signal, longitude_deg, tolerance_h=1.5, min_days=10)` →
`ClockVerdict(signal, n_days, observed_peak_hour_utc, expected_peak_hour_utc, offset_hours,
amplitude, ok)`.

Method: remove each **day's** mean from the values (so multi-day synoptic swings don't
contaminate the phase), composite by hour of day, least-squares fit a harmonic — **harmonic 1
for air temperature, harmonic 2 for pressure** — and take the phase of the maximum. Expected
phase is computed from the station's actual longitude:
`solar_noon_utc = (12 − longitude_deg / 15) mod 24`, then `+2 h` for air temperature and `−2 h`
for the S2 pressure maximum. Wrap the offset to ±half a period.

**Air temperature is the primary** (unambiguous over 24 h). **Pressure confirms** but is
ambiguous modulo 12 h — never use it alone to measure a magnitude.

`assert_utc(...)` raises `AssertionError` with the verdict and the sentence *"Do not ingest it as
UTC. Establish the real zone first."*

Add a `__main__` CLI so Gate 1 can call it:
`python -m ingest.clockcheck --workbook <xlsx> --sheet <name> --time <col> --lon <deg>`.

### 6.2 Validation gate on every project

`create_project` runs, in order:

1. **Clock check** against LJAC1 air temperature (primary) and barometric pressure (confirm), at
   `lon = -117.257`. LJAC1 is the only station reporting both (§0.5) — this is why it is the
   permanent anchor.
2. **Schema conformance** of every parquet in `cache/`.
3. **Coverage report** — per series, span, median step, largest gap, % missing.
4. **Cross-station sanity** — `T(autoss)` and `T(LJAC1)` should agree within a few tenths of a
   degree at lag 0. If their best lag is not ≈ 0, something is wrong with a clock, not with the
   ocean.

Write all of it to `validation.json`. **A failure does not abort the snapshot** — it sets
`status: "failed_validation"`, and the UI shows the project in red with the failing verdict.
Preserve the evidence.

### 6.3 `archive.py` — defeat the 45-day window

```python
def rebuild(root: Path) -> Path:
    """Union every project's cache/ into archive/observations.parquet."""
```

- Dedup on `(time_utc, station, variable)`, keeping the row with the **most recent
  `fetched_utc`** — later pulls may carry QC upgrades or provider revisions.
- When a kept row's `value` differs from a superseded one by more than a tolerance, append a
  record to `archive/revisions.jsonl`: `{time_utc, station, variable, old, new,
  old_fetched_utc, new_fetched_utc, old_project, new_project}`. Silent revision of an
  observational record is not acceptable.
- Expose the archive in the catalog as a pseudo-project named `archive` so the UI can compare
  against the full record rather than a single 45-day window.

### Gate 4

1. A project built from the corrected workbook passes the clock check on both signals.
2. A project built from a deliberately corrupted time column (shift by +7 h in a fixture)
   **fails** it, and the failure text names the offset.
3. `archive.rebuild()` over two overlapping projects produces a row count equal to the size of
   the union, not the sum.
4. Feeding the same project twice produces an empty `revisions.jsonl`.

---

## 7. Finally

- Update `README.md`: the three modes, the project layout, the Excel privacy-level one-time step,
  the `pywin32` requirement, and the time contract in §0.1. Delete the "Extending it → Live API
  fetching" paragraph's bug list — those bugs are fixed in Phase 1.
- Add a `## Resolved` section to `AUDIT.md` dated 2026-08-05 and move A2, A4, A5, A6, B1, B2,
  B5, C3 and C4 into it, each with one line on what fixed it. **C4's conclusion was wrong** —
  say so explicitly: the "anti-phase" result was the timezone artifact of A2, not physics.
- `requirements.txt`: `pandas`, `numpy`, `openpyxl`, `pyarrow`, `pyyaml`, `requests`, `pywin32`.
- `.gitignore`: `.venv/`, `projects/`, `archive/`, `outputs/`, `sources/*.xlsx`, `~$*`,
  `__pycache__/`, `*.parquet`.
- `git init` if not already a repo, and commit the code — it is ~130 KB and has never been
  versioned.

**Report back with:** which gates passed, any `# TODO(verify)` you left, and — specifically —
the before/after clock-check output for `src_LJAC1`. That one number is the point of the whole
exercise.
