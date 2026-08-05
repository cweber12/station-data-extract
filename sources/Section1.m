section Section1;

// ===========================================================================
// La Jolla station data -- Power Query M
//
// This file is the authoritative copy of the workbook's query logic. It is
// tracked in git; ja_jolla_sensors.xlsx is not (it is 4 MB of cached results).
// Extract or re-apply it with:
//
//     python -m ingest.mashup extract sources/ja_jolla_sensors.xlsx -o sources/Section1.m
//     python -m ingest.mashup inject  sources/ja_jolla_sensors.xlsx sources/Section1.m -o <copy>.xlsx
//
// THE TIME CONTRACT -- read this before touching any query
// --------------------------------------------------------
// The previous version of this file emitted a column called `time (UTC)` that
// contained PACIFIC LOCAL TIME. The cause was one step:
//
//     Table.TransformColumnTypes(Raw, {{"time (UTC)", type datetime}, ...})
//
// M's implicit text -> datetime conversion applies the MACHINE'S LOCAL OFFSET
// to an ISO-8601 string carrying a `Z`, then discards the zone. On a UTC-7
// machine every timestamp silently moved back 7 hours. The proof needs no
// physics: the workbook asked ERDDAP for time >= 2026-06-18T00:00:00Z and the
// stored column began at 2026-06-17 17:00 -- seven hours before a bound the
// server had already enforced in true UTC.
//
// `fnToLocal` then subtracted another 7-8 h to build `time (local)`, so that
// column was 14 h from the truth. The "8-hour phase offset" and "anti-phase"
// findings in AUDIT.md C4 were artifacts of this, not physics.
//
// RULES, therefore:
//   1. Never let a time column reach Table.TransformColumnTypes. Keep it text.
//   2. Build the datetime from parts with fnParseUtc. No locale, no machine
//      zone, no inference.
//   3. Emit exactly ONE time column, `time_utc`, and let Python derive local
//      time via zoneinfo. Local time is a display concern.
//   4. Never "fix" a zone by adding a constant. Parse correctly instead.
//
// Verify with:  python -m ingest.clockcheck --workbook <xlsx> --sheet src_LJAC1
//                      --time time_utc --lon -117.258
// ===========================================================================


// --------------------------------------------------------------------------
// Parameters
// --------------------------------------------------------------------------

// The pull window, in days back from now. NDBC realtime2 holds ~45 days, so 45
// is the value any source can satisfy.
//
// NOTE, measured 2026-08-04: the sources actually used here hold far more.
// ERDDAP cwwcNDBCMet advertises time actual_range from 1970-02-26, and LJAC1
// returns 6-minute data for probes at 2020, 2023 and 2025. The Axiom Scripps
// Pier feed reaches back to 2013-01-18. Widening this is a one-number change.
// Keep it in step with `defaults.window_days` in config/stations.yaml.
shared p_WindowDays = 45;

// Dynamic UTC bounds. The previous values were literals wrapped in TRIPLE
// QUOTES -- """2026-07-11T14:00:00Z""" -- which in M is an escaped string
// CONTAINING quote characters, and those quotes went straight into the URL.
shared p_StartUTC =
    DateTime.ToText(
        DateTimeZone.RemoveZone(DateTimeZone.UtcNow()) - #duration(p_WindowDays, 0, 0, 0),
        [Format = "yyyy-MM-ddTHH:mm:ss", Culture = "en-US"]) & "Z";

shared p_EndUTC =
    DateTime.ToText(
        DateTimeZone.RemoveZone(DateTimeZone.UtcNow()),
        [Format = "yyyy-MM-ddTHH:mm:ss", Culture = "en-US"]) & "Z";

// A real list. Previously this was defined, loaded to a sheet, and referenced
// by NO query, while the station list was hardcoded in three separate places.
shared p_NDBCStations = {"LJAC1", "LJPC1", "46254"};

// CO-OPS caps a water_level request at 31 days. A 45-day window must therefore
// be several calls. Asking for the whole span at once returns HTTP 400, and
// Power Query answers a failed query by KEEPING THE PREVIOUS CACHED TABLE --
// so the sheet silently keeps stale data under a name that says otherwise.
// Verified 2026-08-04: begin_date=20260621&end_date=20260805 -> 400.
shared p_COOPSChunkDays = 30;

// The station constraint, in the exact form already proven to work against
// ERDDAP -- station=~"(A|B|C)", URL-encoded -- merely parameterised.
shared p_StationFilter =
    "&station=~%22(" & Text.Combine(p_NDBCStations, "%7C") & ")%22";

shared p_TimeFilter = "&time%3E=" & p_StartUTC & "&time%3C=" & p_EndUTC;


// --------------------------------------------------------------------------
// Functions
// --------------------------------------------------------------------------

shared fnLoadCsv = (url as text, optional codepage as number) as table =>
let
    Source   = Csv.Document(
                   Web.Contents(url),
                   [Delimiter=",", Encoding = if codepage = null then 28591 else codepage,
                    QuoteStyle=QuoteStyle.Csv]),
    Headers  = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    NullNaN  = Table.ReplaceValue(Headers, "NaN", null, Replacer.ReplaceValue,
                   Table.ColumnNames(Headers))
in
    NullNaN;

// Parse an ISO-8601 / CO-OPS-GMT timestamp into a NAIVE datetime that IS UTC.
// Accepts "2026-07-11T14:00:00Z", "2026-07-11T14:00:00", "2026-07-11 14:00".
//
// Built from parts DELIBERATELY. M's implicit datetime conversion applies the
// machine's local offset and discards the zone, which is exactly what broke
// this workbook. Number.FromText on the individual fields cannot do that.
shared fnParseUtc = (t as any) as nullable datetime =>
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
    out;

// QARTOD: 1 good, 2 not evaluated, 3 suspect, 4 fail, 9 missing.
//
// The old screen nulled ONLY flag 4, letting 3 (suspect) and 9 (missing)
// through under a column named `*_ok`. 9 in particular is a missing-value
// marker, so it was being handed downstream as if it were a measurement.
// 1, 2 and 3 pass; 3 is kept but the raw *_qc_agg column stays in the output
// so Python can report how many suspect values were accepted.
shared fnQc = (value as nullable number, flag as nullable number) as nullable number =>
    if flag = null then value
    else if flag = 4 or flag = 9 then null
    else value;


// --------------------------------------------------------------------------
// NDBC, via NOAA CoastWatch ERDDAP
// --------------------------------------------------------------------------

// STAGING QUERY -- set "Enable load" OFF for this one in the Queries pane.
// It held 12,607 rows and the three per-station tables held the same 12,607
// between them, which was most of the workbook's 4 MB.
shared src_NDBC = let
    Url = "https://coastwatch.pfeg.noaa.gov/erddap/tabledap/cwwcNDBCMet.csvp"
        & "?station,time,wd,wspd,gst,wvht,dpd,apd,mwd,bar,atmp,wtmp"
        & p_StationFilter
        & p_TimeFilter
        & "&orderBy(%22station,time%22)",
    Raw = fnLoadCsv(Url),
    // NOTE the absence of {"time (UTC)", type datetime}. That single omission
    // is the entire fix. The column must stay TEXT until fnParseUtc runs.
    Typed = Table.TransformColumnTypes(Raw, {
        {"station", type text},
        {"wd (degrees_true)", type number}, {"wspd (m s-1)", type number},
        {"gst (m s-1)", type number}, {"wvht (m)", type number},
        {"dpd (s)", type number}, {"apd (s)", type number},
        {"mwd (degrees_true)", type number}, {"bar (hPa)", type number},
        {"atmp (degree_C)", type number}, {"wtmp (degree_C)", type number}
    }, "en-US"),
    Utc  = Table.AddColumn(Typed, "time_utc",
               each fnParseUtc([#"time (UTC)"]), type datetime),
    Drop = Table.RemoveColumns(Utc, {"time (UTC)"})
in
    Drop;

// The three station tables. No local-time column, no wd_sin/wd_cos (a compass
// bearing cannot be arithmetic-averaged, and the unweighted version gave mean
// DIRECTION rather than mean vector wind -- compute u/v in Python if wanted),
// no bin keys, and no Table.Sort (sorting a stored table costs refresh time
// and buys nothing; resampling is a query-time operation).
shared src_LJAC1 =
    Table.SelectRows(src_NDBC, each Text.Upper(Text.Trim([station])) = "LJAC1");

shared src_LJPC1 =
    Table.SelectRows(src_NDBC, each Text.Upper(Text.Trim([station])) = "LJPC1");

shared src_46254 =
    Table.SelectRows(src_NDBC, each Text.Trim([station]) = "46254");

shared meta_NDBC_stations = let
    Source  = Csv.Document(
                  Web.Contents("https://www.ndbc.noaa.gov/station_metadata.txt"),
                  [Delimiter="|", Encoding=28591, QuoteStyle=QuoteStyle.None]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Clean   = Table.RenameColumns(Headers,
                  List.Transform(Table.ColumnNames(Headers),
                      each {_, Text.Trim(Text.Replace(_, "#", ""))})),
    NoCmt   = Table.SelectRows(Clean, each not Text.StartsWith([STATION_ID], "#")),
    // Driven by the parameter now, not by a second hardcoded list.
    Lower   = List.Transform(p_NDBCStations, each Text.Lower(_)),
    Mine    = Table.SelectRows(NoCmt,
                  each List.Contains(Lower, Text.Lower(Text.Trim([STATION_ID])))),
    Ents    = Table.TransformColumns(Mine, {
                  {"LOCATION", each Text.Replace(Text.Replace(_, "&#176;", "°"), "&#039;", "'"), type text}}),
    Status  = Table.AddColumn(Ents, "STATUS_TEXT", each
                  if Text.Trim([STATUS]) = "1" then "Established"
                  else if Text.Trim([STATUS]) = "2" then "Disestablished"
                  else if Text.Trim([STATUS]) = "3" then "Test"
                  else "Unknown", type text),
    Typed   = Table.TransformColumnTypes(Status, {
                  {"SITE HEIGHT", type number}, {"ATMP HEIGHT", type number},
                  {"ANEMOMETER HEIGHT", type number}, {"BAROMETER HEIGHT", type number},
                  {"WTMP HEIGHT", type number}, {"WATER DEPTH", type number}}, "en-US")
in
    Typed;

shared meta_NDBC_positions = let
    Url = "https://coastwatch.pfeg.noaa.gov/erddap/tabledap/cwwcNDBCMet.csvp"
        & "?station,longitude,latitude"
        & p_StationFilter
        & "&distinct()",
    Raw = fnLoadCsv(Url),
    Typed = Table.TransformColumnTypes(Raw, {
        {"station", type text}, {"longitude (degrees_east)", type number},
        {"latitude (degrees_north)", type number}}, "en-US")
in
    Typed;


// --------------------------------------------------------------------------
// CO-OPS water level -- 9410230, La Jolla (Scripps Pier)
// --------------------------------------------------------------------------

// time_zone=gmt, NOT lst_ldt. The old query asked for local time and then
// labelled the column `time (local)`, which put water level on a different
// clock from everything else in the workbook.
// NDBC TIDE is empty at all three stations, which is why water level comes
// from here rather than from the met feed.
// Fetch water level in <= 31-day chunks and stitch them together.
shared fnCoopsWaterLevel = (station as text, startUtc as text, endUtc as text) as table =>
let
    d0     = Date.From(fnParseUtc(startUtc)),
    d1     = Date.From(fnParseUtc(endUtc)),
    Starts = List.Generate(() => d0,
                           each _ <= d1,
                           each Date.AddDays(_, p_COOPSChunkDays)),
    Fetch  = (s as date) as nullable table =>
        let
            eRaw = Date.AddDays(s, p_COOPSChunkDays - 1),
            e    = if eRaw > d1 then d1 else eRaw,
            Url  = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
                 & "?product=water_level&application=la_jolla_buoy_cmp"
                 & "&station=" & station
                 & "&begin_date=" & Date.ToText(s, [Format="yyyyMMdd", Culture="en-US"])
                 & "&end_date="   & Date.ToText(e, [Format="yyyyMMdd", Culture="en-US"])
                 & "&datum=MLLW&units=english&time_zone=gmt&format=csv"
        in
            try fnLoadCsv(Url, 65001) otherwise null,
    Parts  = List.RemoveNulls(List.Transform(Starts, each Fetch(_))),
    // CO-OPS reports errors as a JSON body with a 200, which parses to a table
    // with no "Date Time" column. Drop those rather than let them poison the
    // combine.
    Good   = List.Select(Parts, each Table.HasColumns(_, "Date Time")),
    Out    = if List.IsEmpty(Good)
             then #table(type table [#"Date Time" = text], {})
             else Table.Combine(Good)
in
    Out;

shared src_9410230_wl = let
    Raw   = fnCoopsWaterLevel("9410230", p_StartUTC, p_EndUTC),
    Typed = Table.TransformColumnTypes(Raw, {
                {" Water Level", type number},
                {" Sigma", type number}, {" Quality ", type text}}, "en-US"),
    Utc   = Table.AddColumn(Typed, "time_utc",
                each fnParseUtc([#"Date Time"]), type datetime),
    Drop  = Table.RemoveColumns(Utc, {"Date Time"}),
    // Chunk boundaries are exclusive by construction, but a provider hiccup
    // could still repeat a minute. One row per timestamp.
    Dedup = Table.Distinct(Drop, {"time_utc"})
in
    Dedup;


// --------------------------------------------------------------------------
// SCCOOS automated shore station, Scripps Pier -- via Axiom ERDDAP
// --------------------------------------------------------------------------
// Host is erddap.sensors.axds.co, which is what this workbook has always used
// and what is verified working. erddap.cencoos.org mirrors the same dataset id.
// NEVER erddap.sccoos.org/erddap/tabledap/autoss -- frozen since 2019-09-18.

shared src_SCCOOS_ctd = let
    Url = "https://erddap.sensors.axds.co/erddap/tabledap/scripps-pier-automated-shore-sta-1.csvp"
        & "?time,sea_water_temperature_ctd,sea_water_temperature_ctd_qc_agg"
        & ",sea_water_practical_salinity_ctd,sea_water_practical_salinity_ctd_qc_agg"
        & p_TimeFilter & "&orderBy(%22time%22)",
    Raw   = fnLoadCsv(Url),
    Typed = Table.TransformColumnTypes(Raw, {
                {"sea_water_temperature_ctd (degree_Celsius)", type number},
                {"sea_water_temperature_ctd_qc_agg", Int64.Type},
                {"sea_water_practical_salinity_ctd (1e-3)", type number},
                {"sea_water_practical_salinity_ctd_qc_agg", Int64.Type}}, "en-US"),
    Wtmp  = Table.AddColumn(Typed, "wtmp_ok", each
                fnQc([#"sea_water_temperature_ctd (degree_Celsius)"],
                     [sea_water_temperature_ctd_qc_agg]), type number),
    Sal   = Table.AddColumn(Wtmp, "sal_ok", each
                fnQc([#"sea_water_practical_salinity_ctd (1e-3)"],
                     [sea_water_practical_salinity_ctd_qc_agg]), type number),
    Utc   = Table.AddColumn(Sal, "time_utc",
                each fnParseUtc([#"time (UTC)"]), type datetime),
    Drop  = Table.RemoveColumns(Utc, {"time (UTC)"})
in
    Drop;

shared src_SCCOOS_eco = let
    Url = "https://erddap.sensors.axds.co/erddap/tabledap/scripps-pier-automated-shore-sta-1.csvp"
        & "?time,mass_concentration_of_chlorophyll_in_sea_water_eco"
        & ",mass_concentration_of_chlorophyll_in_sea_water_eco_qc_agg"
        & ",sea_water_turbidity_eco,sea_water_turbidity_eco_qc_agg"
        & p_TimeFilter & "&orderBy(%22time%22)",
    Raw   = fnLoadCsv(Url),
    Typed = Table.TransformColumnTypes(Raw, {
                {"mass_concentration_of_chlorophyll_in_sea_water_eco (microg.L-1)", type number},
                {"mass_concentration_of_chlorophyll_in_sea_water_eco_qc_agg", Int64.Type},
                {"sea_water_turbidity_eco (NTU)", type number},
                {"sea_water_turbidity_eco_qc_agg", Int64.Type}}, "en-US"),
    Chl   = Table.AddColumn(Typed, "chl_ok", each
                fnQc([#"mass_concentration_of_chlorophyll_in_sea_water_eco (microg.L-1)"],
                     [mass_concentration_of_chlorophyll_in_sea_water_eco_qc_agg]), type number),
    Turb  = Table.AddColumn(Chl, "turb_ok", each
                fnQc([#"sea_water_turbidity_eco (NTU)"],
                     [sea_water_turbidity_eco_qc_agg]), type number),
    Utc   = Table.AddColumn(Turb, "time_utc",
                each fnParseUtc([#"time (UTC)"]), type datetime),
    Drop  = Table.RemoveColumns(Utc, {"time (UTC)"})
in
    Drop;

shared src_SCCOOS_seaphox = let
    Url = "https://erddap.sensors.axds.co/erddap/tabledap/scripps-pier-automated-shore-sta-1.csvp"
        & "?time,sea_water_ph_reported_on_total_scale_seaphox_external"
        & ",sea_water_ph_reported_on_total_scale_seaphox_external_qc_agg"
        & ",mass_concentration_of_oxygen_in_sea_water_seaphox"
        & ",mass_concentration_of_oxygen_in_sea_water_seaphox_qc_agg"
        & p_TimeFilter & "&orderBy(%22time%22)",
    Raw   = fnLoadCsv(Url),
    Typed = Table.TransformColumnTypes(Raw, {
                {"sea_water_ph_reported_on_total_scale_seaphox_external (1)", type number},
                {"sea_water_ph_reported_on_total_scale_seaphox_external_qc_agg", Int64.Type},
                {"mass_concentration_of_oxygen_in_sea_water_seaphox (mg.L-1)", type number},
                {"mass_concentration_of_oxygen_in_sea_water_seaphox_qc_agg", Int64.Type}}, "en-US"),
    Ph    = Table.AddColumn(Typed, "ph_ok", each
                fnQc([#"sea_water_ph_reported_on_total_scale_seaphox_external (1)"],
                     [sea_water_ph_reported_on_total_scale_seaphox_external_qc_agg]), type number),
    Do_   = Table.AddColumn(Ph, "do_ok", each
                fnQc([#"mass_concentration_of_oxygen_in_sea_water_seaphox (mg.L-1)"],
                     [mass_concentration_of_oxygen_in_sea_water_seaphox_qc_agg]), type number),
    Utc   = Table.AddColumn(Do_, "time_utc",
                each fnParseUtc([#"time (UTC)"]), type datetime),
    Drop  = Table.RemoveColumns(Utc, {"time (UTC)"})
in
    Drop;


// --------------------------------------------------------------------------
// Metadata
// --------------------------------------------------------------------------

shared meta_COOPS_datums = let
    Src   = Json.Document(Web.Contents(
                "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/9410230/datums.json?units=english")),
    Tbl   = Table.FromRecords(Src[datums]),
    Feet  = Table.TransformColumnTypes(Tbl, {{"value", type number}}, "en-US"),
    Metre = Table.AddColumn(Feet, "value_m", each [value] * 0.3048, type number)
in
    Metre;

// `meta_ERDDAP` used to live here. It fetched the CO-OPS datums JSON -- the
// same URL and the same steps as meta_COOPS_datums, byte-for-byte identical
// output -- and contained no ERDDAP metadata whatsoever. Deleted, since
// meta_ERDDAP_sccoos and meta_ERDDAP_ndbc below are the real thing.

shared meta_ERDDAP_sccoos = let
    Raw = fnLoadCsv("https://erddap.sensors.axds.co/erddap/info/scripps-pier-automated-shore-sta-1/index.csv"),
    Keep = Table.SelectRows(Raw, each List.Contains(
               {"units","long_name","standard_name","actual_range","ioos_category"},
               [#"Attribute Name"]))
in
    Keep;

shared meta_ERDDAP_ndbc = let
    Raw = fnLoadCsv("https://coastwatch.pfeg.noaa.gov/erddap/info/cwwcNDBCMet/index.csv"),
    Keep = Table.SelectRows(Raw, each List.Contains(
               {"units","long_name","standard_name","actual_range","ioos_category"},
               [#"Attribute Name"]))
in
    Keep;

// One row per refresh, so every snapshot self-documents when it was pulled and
// with what. This is what turns the provenance sheet from "this came from a
// workbook" into a record you can act on: project.py reads m_version,
// fetched_utc, p_StartUTC and p_EndUTC out of here, and marks a project
// "incomplete" rather than guessing if this sheet is absent.
shared meta_refresh = let
    now = DateTimeZone.RemoveZone(DateTimeZone.UtcNow()),
    txt = DateTime.ToText(now, [Format = "yyyy-MM-ddTHH:mm:ss", Culture = "en-US"]) & "Z",
    t = #table(
          type table [key = text, value = text],
          {
            {"fetched_utc",    txt},
            {"p_StartUTC",     p_StartUTC},
            {"p_EndUTC",       p_EndUTC},
            {"p_WindowDays",   Text.From(p_WindowDays)},
            {"p_NDBCStations", Text.Combine(p_NDBCStations, ",")},
            {"m_version",      "2026-08-05-a"}
          })
in
    t;
