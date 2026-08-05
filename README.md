# La Jolla sensor comparison

Pick columns from any workbook in `sources/`, choose an averaging interval, get a
new workbook in `outputs/` with aligned data, correlations, and charts.

```
lajolla/
├── sources/       your workbooks -- read only, never written to
├── outputs/       generated workbooks -- never scanned as input
├── compare.py     the window
├── sensorkit.py   discovery, loading, time/unit normalisation, resampling
├── exporter.py    writes the output workbook
└── run.ps1        launcher
```

The separation you asked for is enforced in code, not by naming discipline:
`build_catalog()` globs `sources/*.xlsx` and has no path to `outputs/`. Generated
files can never feed back in, no matter what they're called.

## Running it

Right-click `run.ps1` → **Run with PowerShell**. It checks for Python 3.9+,
checks `tkinter`, installs `pandas` / `openpyxl` / `numpy` if they're missing,
creates the folders, and opens the window.

If PowerShell refuses to run the script, once per session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Or skip the launcher entirely: `python compare.py`.

## Using it

The left pane is a tree: workbook → table → column. Each column shows its
non-null count, detected unit, and which time basis its table uses. Click a
column to tick it. **Hide all-empty columns** is on by default — untick it and
you'll see LJPC1's temperature columns sitting there at `0/1104`, which is the
thing that would otherwise waste an afternoon.

On the right: interval, aggregation, minimum samples per bin, and whether to keep
only bins where every series has data (`intersection`) or all bins with gaps left
blank (`union`).

**Min samples/bin** is worth setting. At a 1-hour interval, 46254 reports twice
and Scripps CTD reports fifteen times. A bin holding one sample of a six-minute
series is a gap wearing an average's clothes. Set it to roughly half what you'd
expect from the slowest series you selected; the `counts` sheet shades anything
thin so you can check afterwards.

## What comes out

| sheet | what's on it |
|---|---|
| `data` | one row per interval, local and UTC timestamps, one column per series |
| `charts` | raw line chart, z-scored line chart, scatter of the first two series |
| `stats` | per-series summary and a correlation matrix — **live formulas**, so edits to `data` update them |
| `counts` | how many raw samples backed each cell; shaded where thin |
| `normalized` | each series as a z-score, so different units share one axis |
| `provenance` | source file, table and column for every series, plus every setting used |

Use the **normalized** chart to compare timing and shape across different units
(temperature against water level), and the raw chart for magnitudes. There's no
dual-axis chart on purpose — two scales on one frame make any two series look
however you want them to.

## Things it does on load, so you don't have to

- **Time.** Columns headed `time (UTC)` are read as UTC. `time (local)`,
  `time (PDT)`, and `Date-Time (PDT)` are read as America/Los_Angeles wall time
  and converted. Everything is aligned in UTC internally; local time is derived
  for display via `zoneinfo`. Timestamps that are ambiguous or nonexistent across
  a DST transition are dropped rather than guessed.
- **Units.** °F becomes °C, and the original is recorded on the provenance sheet.
- **`BinKey*` columns are ignored.** Binning happens here, at whatever interval
  you pick, so you're not limited to 10/30/60/120/180 minutes.
- **`station` and index columns are hidden** — they're identifiers, not data.
- **Nothing is dropped silently.** A selected series with no usable values is
  reported in the log, in the completion dialog, and on the provenance sheet.

## Lag scan

On by default. It cross-correlates every series against a reference across
±24 hours and reports the best lag.

Read the results carefully. La Jolla nearshore temperature is dominated by the
~12.42 hour internal tide, so a peak at lag *L* cannot be distinguished from one
at *L* ± 12.42 h. The `alt lag` column spells out the alternative every time.
Negative lag means the series **leads** the reference.

For your yellow buoy against Scripps at 1-hour bins, this reports roughly −8 h
with r ≈ 0.72 (against 0.12 at lag zero), where the two pier stations agree with
each other at lag zero, r ≈ 0.97. The alternative reading is +4.4 h. Which one is
physical depends on the deployment depth.

## Extending it

**A new derived column with no unit in its header** — add a line to
`UNIT_OVERRIDES` in `sensorkit.py`.

**A new time column naming convention** — add a pattern to `TIME_PREFERENCE`.
Order matters; UTC patterns are listed first deliberately.

**Live API fetching.** Not built, because your source workbooks already hold the
data. When you want it, write `ingest.py` that pulls the ERDDAP and CO-OPS URLs
into `sources/` as dated `.xlsx` or `.parquet`, and nothing else changes — the
catalogue picks up whatever it finds. Two bugs to fix while you're there, both
detailed in the audit: `p_StartUTC` / `p_EndUTC` carry literal quote characters
and are landing 7 hours off, and `src_NDBC` ignores those parameters entirely in
favour of a hardcoded window.

**Scripting it without the window.** `sensorkit` and `exporter` have no GUI
dependency:

```python
from pathlib import Path
import sensorkit as sk, exporter as ex

root = Path(".")
cat = sk.build_catalog(root)

def find(f, table, col):
    for t in cat[f]:
        if t.name == table:
            for c in t.data_columns:
                if c.column == col:
                    return t, c

sel = [find("yellow_buoy_temps.xlsx", "Data", "Tidbit 1 , °F"),
       find("ja_jolla_sensors.xlsx", "src_SCCOOS_ctd", "wtmp_ok")]

res = sk.build_comparison(root, sel, interval="1h", min_samples=2)
ref = res.data.columns[0]
ex.write_workbook(res, root, root / "outputs" / "test.xlsx",
                  sk.lag_scan(res.data, ref, "1h"), ref)
```

That's the path to a scheduled job or a batch of preset comparisons later.
