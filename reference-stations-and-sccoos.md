# Reference stations, sensor geometry, and the right SCCOOS feed

*2026-08-05.*

## 1. You're right about 46254, and the reason is sharper than "variable depth"

The distinction that matters isn't whether a sensor's depth changes with the tide. It's which
**reference frame** it samples.

| sensor | depth | fixed relative to | frame |
|---|---|---|---|
| **Yellow buoy** | on the anchor, on the bed | the seabed | **Earth** |
| **SCCOOS autoss, Scripps Pier** | **5.0 m below MLLW** | the pier piling | **Earth** |
| **LJAC1 / 9410230** (sensor E1) | **−11.255939 ft = −3.431 m below MLLW** | the pier piling | **Earth** |
| **46254 / CDIP 201** | ~0.45 m below the surface, on the hull of a 0.9 m sphere | **the sea surface** | **surface** |

An Earth-frame sensor sits at a fixed absolute depth and samples whatever water mass is at that
level — so it registers internal-wave heave, thermocline shoaling, and cold-water intrusion. A
surface-frame sensor rides up and down with the tide and swell and therefore always sits inside
the surface mixed layer — it registers solar heating and air–sea exchange and is largely blind
to what's happening below.

**Your logger is Earth-frame.** So:

- **SCCOOS autoss at 5.0 m is the closest analogue you have.** Same frame, and the depth is in
  the same neighbourhood as your sensor's if the 26 ft figure is right.
- **LJAC1 at 3.43 m is second.** Same frame, shallower.
- **46254 is not a like-for-like reference at all.** Comparing your bed-mounted sensor to a
  floating surface thermistor and treating disagreement as error is a category mistake — it's
  the same shape of mistake that produced the "anti-phase" reading before the timezone bug
  turned up.

The exact vertical numbers, since MLLW is not the same as mean sea level: at 9410230, MSL sits
**0.83 m above MLLW** (NAVD88, epoch 1983–2001: MSL 7.10 ft, MLLW 4.37 ft). So below mean sea
level, LJAC1 is at **4.26 m** and autoss at **5.83 m** — **a constant 1.57 m separation, autoss
deeper.** Constant, because both are Earth-frame and swing together.

### 46254 is still worth having — as the *other* endpoint

Don't drop it. Use it deliberately:

> **stratification index = SST(46254) − T(autoss 5 m)**

That's a direct measure of how stratified the column is, from two instruments you already pull.
When it's large, the column is stratified, the thermocline is somewhere between 0.5 m and 5 m,
and your bed sensor can plausibly do things the pier doesn't. When it's near zero the column is
mixed and your sensor should track the pier closely. **That's the covariate that tells you when
to expect agreement** — which is exactly the question you've been circling.

Plot it as a second panel under every buoy-vs-pier comparison, with the tide as a third.

### One caveat on the "r = 0.97" pair

LJAC1 and the pier CTD agreeing at 0.97 validates their **timing and calibration** against each
other. It does not make either of them a good proxy for your site: they're 1.57 m apart
vertically inside the nearshore thermocline, so a real, seasonally varying difference between
them is expected — largest during summer stratification and internal-wave events, near zero when
well mixed. Treat their disagreement as stratification signal before calling it instrument error.

### Are they genuinely independent? Yes.

Different operators (NOAA NOS/CO-OPS vs SIO/SCCOOS), different instruments (a NOS thermistor vs
a Sea-Bird 16plus CTD), different elevations, different telemetry and different QC chains.
Neither re-serves the other. They can validate each other. Their shared limitation is the pier
itself — same piling, same local environment — which is what makes them good for a calibration
and timing check and limited as a proxy for a site a kilometre offshore.

---

## 2. Which SCCOOS feed — you're already on the best one

Of the three links you sent, **two are worse than what your workbook already does.** Don't
switch.

### `erddap.sccoos.org/erddap/tabledap/autoss` — dead. Do not use.

Verified on the dataset info page:

| | |
|---|---|
| `time` actual_range | **2005-06-16T19:27:53Z → 2019-09-18T05:20:19Z** |
| `date_created` | 2019-09-16 |
| `processing_level` | *"QA/QC have been performed"* |
| `temperature:comment` | *"The following QC tests were done on temperature: **None**."* |

Frozen for nearly seven years, with a `processing_level` attribute that flatly contradicts the
temperature variable's own comment. It also carries only the legacy `_flagPrimary` /
`_flagSecondary` flags, no QARTOD. Switching to this would cost you seven years of record and
every QC flag you currently have.

### `data.caloos.org` station 120738 — this is the good one, and you're already using it

Station **120738 = "Scripps Pier Automated Shore Station with SeapHOx"**, SIO, 32.867 / −117.257.
The portal is a discovery and visualisation UI; the machine-readable dataset behind it is
**`scripps-pier-automated-shore-sta-1`** on `erddap.cencoos.org` (Axiom operates the
CeNCOOS/CalOOS ERDDAP jointly).

| | |
|---|---|
| time coverage | **2013-01-18T22:29:25Z → 2026-08-05T04:24:10Z** — live |
| QC | full QARTOD: `_qc_agg` plus an 11-character `_qc_tests` string (Gap, Syntax, Location, Gross Range, Climatology, Spike, Rate of Change, Flat Line, Multi-Variate, Attenuated Signal, Neighbor) |
| structure | CTD / ECO / SeapHOx exposed as separate variable suffixes |

**Proof you're already on it:** your workbook's `meta_ERDDAP_sccoos` records
`time:actual_range = 1.358548165E9 … 1.7858148E9`, which is
**2013-01-18T22:29:25Z → 2026-08-04T03:40:00Z**. That start date matches this dataset exactly,
and is nothing like `autoss`'s 2005-06-16. Your column names —
`sea_water_temperature_ctd`, `sea_water_temperature_ctd_qc_agg`,
`sea_water_ph_reported_on_total_scale_seaphox_external` — are Axiom's naming convention.

So: **right feed, wrong plumbing.** The problem was never the source; it was the Power Query
timezone handling on top of it.

### `thredds.sccoos.org/thredds/catalog/autoss/` — two narrow but real uses

One NetCDF per station per year, flat: `scripps_pier-2005.nc` … `scripps_pier-2023.nc`.
OPeNDAP at `https://thredds.sccoos.org/thredds/dodsC/autoss/scripps_pier-<YYYY>.nc`. Newest file
is 2023, so it's stale as a live feed — but:

1. **It covers 2005–2012**, which the Axiom dataset does not. If you ever want the full shore
   station record, this is the only route to those years.
2. **It carries the true depth attribute** — `depth = 5.0`, `positive = "down"`,
   `comment = "depth of sensor"`.

### ⚠️ The depth metadata is missing from the feed you use

Axiom's ERDDAP sets **`z = 0.0 m`** for both the Scripps Pier station *and* CDIP 201.
`geospatial_vertical_min/max` are both 0.0. **The 5 m and 0.45 m depths are not in the data you
pull** — they exist only on the SCCOOS website, in the THREDDS files, and in the NOS metadata
API respectively.

Which means depth cannot be inherited from the source and **must be hardcoded in
`config/stations.yaml`.** This is exactly what that file is for. Given the whole argument in
§1 turns on depth, this is not a detail.

---

## 3. Recommended reference architecture

| role | station | depth | frame | why |
|---|---|---|---|---|
| subject | yellow buoy | on the bed | Earth | — |
| **primary reference** | **SCCOOS autoss CTD** | 5.0 m MLLW | Earth | closest analogue; QARTOD-flagged; salinity and sigma-t say when the column is stratified |
| cross-check | LJAC1 / 9410230 | 3.43 m MLLW | Earth | independent operator and instrument; validates autoss's timing and calibration |
| **surface endpoint** | 46254 / CDIP 201 | 0.45 m below surface | **surface** | not a peer — the top of the stratification index |
| clock anchor | LJAC1 ATMP + PRES | — | — | the only station with both; see `clockcheck.py` |
| derived | `SST(46254) − T(autoss)` | — | — | stratification index; the covariate that predicts agreement |
| context | SIO Shore Stations, Scripps Pier | **bottom ~5 m since 1926** | Earth | a century of context **at your reference depth** |

That last row is worth a second look. The SIO Shore Stations manual record at Scripps Pier runs
**surface from 1916 and bottom from 1926**, sampled from the pier end — and the bottom series is
at essentially the same nominal depth as autoss. So you can put a 2026 anomaly in
hundred-year context at the depth that actually matters to you, not just at the surface.
DOI `10.6075/J06T0K0M`. Caveat from the program: *"Scripps Pier daily temperature is adjusted to
account for the sampling time of day"* — a processing step, not raw.

### `stations.yaml` patch

```yaml
  - id: scripps_pier_autoss
    name: SCCOOS Automated Shore Station, Scripps Pier (SeapHOx)
    caloos_station_id: 120738
    lon: -117.257
    lat: 32.867
    sensor_depth_m: 5.0            # below MLLW. NOT in the feed - z=0 in Axiom ERDDAP.
    depth_reference: MLLW          # Earth frame: fixed to the piling
    depth_below_msl_m: 5.83        # MSL is 0.832 m above MLLW at 9410230
    reference_frame: earth
    operator: Scripps Institution of Oceanography
    instruments: [SeaBird 16plus CTD, ECO Triplet, SeapHOx, Aanderaa 5730 optode]
    role: primary_reference
    endpoints:
      erddap: "https://erddap.cencoos.org/erddap/tabledap/scripps-pier-automated-shore-sta-1.csv"
      thredds_backfill: "https://thredds.sccoos.org/thredds/dodsC/autoss/scripps_pier-{YYYY}.nc"  # 2005-2012 only
    coverage: 2013-01-18 .. present         # THREDDS covers 2005-2012
    qc: QARTOD _qc_agg + 11-char _qc_tests
    do_not_use:
      - "https://erddap.sccoos.org/erddap/tabledap/autoss  # FROZEN 2019-09-18, no temperature QC"

  - id: LJAC1
    sensor_depth_m: 3.431          # -11.255939 ft MLLW, NOS mdapi sensor E1
    depth_reference: MLLW
    depth_below_msl_m: 4.26
    reference_frame: earth
    role: cross_check

  - id: "46254"
    sensor_depth_m: 0.45
    depth_reference: sea_surface   # FLOATING - different frame from everything else
    reference_frame: surface
    role: surface_endpoint
    note: >
      Not a peer of the bed-mounted logger. Use as the top of the stratification
      index (SST_46254 - T_autoss), never as a like-for-like temperature reference.
```

### Working query

```
https://erddap.cencoos.org/erddap/tabledap/scripps-pier-automated-shore-sta-1.csv
  ?time,sea_water_temperature_ctd,sea_water_temperature_ctd_qc_agg
  ,sea_water_practical_salinity_ctd,sea_water_practical_salinity_ctd_qc_agg
  ,sea_water_sigma_t_ctd
  &time>=2026-07-11T00:00:00Z&time<=2026-08-02T00:00:00Z
```

Filter `_qc_agg` to 1 and 2, keep-and-flag 3, reject 4 and 9 — the policy already in
`stations.yaml`. **Verify this URL yourself before scripting it**: I could confirm the variable
names and time coverage from the dataset info page, but `erddap.cencoos.org` tabledap is
robots-disallowed from my side, so I could not execute the query.

---

## 4. Quality caveats worth designing around

SCCOOS states, verbatim: *"Data shown below include limited data quality checks and flags in
live feed mode, and may not capture all sensor drift, biofouling or malfunctions."*

Mitigation on their side: **divers service the Scripps Pier package monthly** to reduce
biofouling and remove sediment; sensors are swapped roughly annually and returned to Sea-Bird
for calibration.

That gives you a checkable diagnostic. Biofouling drifts slowly and is reset abruptly at each
dive. **If `T(autoss) − T(LJAC1)` shows a sawtooth with a ~30-day period, that's the fouling and
service cycle, not the ocean.** Worth computing once over a year of data — if the sawtooth is
absent, you can trust the pair over a 3-week deployment without further thought.

The published comparison of the Scripps Pier records against each other is Rasmussen et al.
2020, *JGR Oceans*, `10.1029/2019JC015673` — I could not read the full text (403), so treat
that as a pointer rather than a summary.

---

## Do these four things

1. **Demote 46254 from reference to surface endpoint**, and add
   `SST(46254) − T(autoss)` as a stratification index panel on every comparison.
2. **Make SCCOOS autoss CTD the primary reference**, LJAC1 the cross-check.
3. **Hardcode the depths in `stations.yaml`** — they are not in the ERDDAP feed (`z = 0.0`).
4. **Don't switch SCCOOS sources.** You're on the live Axiom dataset already. Add the THREDDS
   route only if you want 2005–2012, and never use `erddap.sccoos.org/…/autoss`.

*Sources:* [SCCOOS autoss page](https://sccoos.org/autoss/) ·
[erddap.sccoos.org autoss info](https://erddap.sccoos.org/erddap/info/autoss/index.html) ·
[CeNCOOS/Axiom Scripps Pier dataset](https://erddap.cencoos.org/erddap/info/scripps-pier-automated-shore-sta-1/index.html) ·
[SCCOOS THREDDS autoss](https://thredds.sccoos.org/thredds/catalog/autoss/catalog.html) ·
[NOS 9410230 sensors](https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/9410230/sensors.json) ·
[NOS 9410230 datums](https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/9410230/datums.json) ·
[CDIP instrumentation](http://cdip.ucsd.edu/m/documents/instrumentation.html) ·
[SIO Shore Stations, Scripps Pier](https://shorestations.ucsd.edu/about/scripps-pier/) ·
[Shore Stations data DOI](https://doi.org/10.6075/J06T0K0M)
