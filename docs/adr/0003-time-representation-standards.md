# 3. UTC is the record; a displayed local time carries its zone designator

Date: 2026-08-08

## Status

Accepted

## Context

This project's most expensive bug was a timezone bug. The original workbook's
`time (UTC)` columns held Pacific local time; Power Query's implicit
text→datetime conversion applied the machine's UTC−7 offset and discarded the
zone, and every conclusion built on it was wrong — including the "anti-phase"
result recorded in `AUDIT.md` C4.

The rules that came out of that were written down as invariants but never
sourced. When #15 fixed the view chart's x axis, the question came up directly:
is there a standard for this, and does the implementation follow it? Answering
it changed the design, so the sources belong in the repo rather than in a
transcript.

### What the authorities actually say

**[NDBC](https://www.ndbc.noaa.gov/faq/measdes.shtml)** — the closest thing to
a governing convention for this kind of data, and the operator of 46254:

> "Both Realtime and Historical files show times in UTC only."
> "Station pages show current observations in station local time by default,
> but can be changed by the viewer to UTC (formerly GMT)."

Data in UTC; local time as a viewer-side display option. Its
[web data guide](https://www.ndbc.noaa.gov/docs/ndbc_web_data_guide.pdf) also
demonstrates NOAA's own way of disambiguating in prose — "midnight U.S. Central
Time (05:00 UTC during daylight saving time or 06:00 UTC during standard time)"
— which resolves the ambiguity with **offsets**, not with an explanation.

**[RFC 3339](https://www.rfc-editor.org/rfc/rfc3339)** §4.1:

> "Because the daylight saving rules for local time zones are so convoluted and
> can change based on local law at unpredictable times, true interoperability is
> best achieved by using Coordinated Universal Time (UTC)."

§4.4:

> "the interoperability problems of unqualified local time are deemed
> unacceptable"

**[RFC 9557](https://www.rfc-editor.org/rfc/rfc9557.html)** (2024) extends RFC
3339 with an IANA zone annotation — `1996-12-19T16:39:57-08:00[America/Los_Angeles]`
— because an offset alone does not identify a zone. It notes that
"use of offset time zones is strongly discouraged" for the annotation itself.

**[CF Conventions §4.4](https://cfconventions.org/Data/cf-conventions/cf-conventions-1.7/build/ch04s04.html)**,
which governs the netCDF this project's ERDDAP feeds come from: a reference
time may carry a zone, and "if the time zone is omitted the default is UTC".
Its own example spells the offset numerically (`-6:00`) rather than as an
abbreviation.

**[Unicode LDML, TR 35](https://unicode.org/reports/tr35/tr35-dates.html)** —
the standard for _display_. Presenting a specific time uses the **specific
non-location format** (`z` → PDT/PST): "This is the format that should be used
when formatting a specific time for presentation." The generic non-location
format (`v` → PT) is for recurring times and does not distinguish daylight from
standard time.

**[NIST](https://www.nist.gov/pml/time-and-frequency-division/time-realization/utcnist-time-scale)**
maintains UTC(NIST), "the U.S. national standard for time-of-day". Worth
recording explicitly: the NIST time-services and time-distribution pages say
**nothing** about time zones, local time, DST, or how a timestamp should be
represented. They establish the reference scale and stop there. They should not
be cited as support for a display convention.

**[NCEI / IOOS](https://ioos.github.io/ncei-archiving-cookbook/)** archiving
guidance: submit UTC, not local.

### What no authority says

**There is no standard for annotating a DST transition on a chart axis.**
Nothing in NDBC, NIST, CF, ISO 8601, RFC 3339, RFC 9557 or CLDR describes one.
What they give instead is a single consistent instruction one level up: an
instant is identified by its UTC offset or zone designator, and a local time
without one is not an instant at all.

## Decision

1. **UTC is the record.** Every stored timestamp is UTC with an explicit
   offset. A naive timestamp is refused on load, and the refusal names the
   file and the field. `annotations.parse_utc` enforces this.
2. **Local time is display only**, exactly as NDBC treats it. The truth in a
   `ViewWindow` is `_utc`; the chart's x values are those instants unchanged,
   and the zone is handed to matplotlib's locator and formatter rather than
   inferred.
3. **Every displayed local time carries its zone designator** — the LDML
   specific non-location format, `%Z` → PDT/PST. This applies to
   `annotations.local_text`, the chart's tick labels when the window spans a
   transition, and the window subtitle.
4. **The axis names the IANA zone**: `time (local, America/Los_Angeles)`.
5. **A DST transition is shown, not explained.** The locator restores the tick
   a fall-back would otherwise skip, so both instants either side of the change
   are ticked; the designators then resolve them.

Across the November fall-back the axis reads

```
21:00 PDT   01:00 PDT   01:00 PST   03:00 PST   06:00 PST
```

and across the March spring-forward

```
21:00 PST   01:00 PST   03:00 PDT   06:00 PDT
```

No prose, and nothing a reader has to be told.

## Consequences

Labels are longer, so they are rotated 30° on a window that spans a transition,
which costs vertical room below the axes (`ROTATED_TICK_PAD`).

Designators appear **only** where a window spans a transition. Outside one a
wall time in a named zone is already unambiguous, and a designator on every
tick is ink spent against a misreading that cannot occur. This is a deliberate
departure from a literal reading of LDML, which would designate every presented
time.

`local_text` output changed shape, so anything parsing it would break. Nothing
does — it is a display helper, and the stored form (`format_local`, full ISO
8601 offset) was always the machine-readable one.

## Alternatives considered

**A bracket under the axis with a caption naming the repeated hour.** Built,
reviewed, and rejected: it read poorly, and it was an invented device standing
in for a standard one. It also had to be re-laid-out by hand against the x
label and the legend, and did not survive zoom as cleanly as a locator does.

**Shading the repeated hour over the data.** Rejected: a dark block with a
colour bar is precisely what a marked region looks like here — it means "a
person judged this interesting" — and dressing a fact about the calendar in the
vocabulary of an interpretation is worse than saying nothing.

**A vertical rule at the transition instant.** Rejected: `_band_patch` records
that edge rules were removed because verticals crossing the series were exactly
the noise a marked window exists to cut through.

**Plotting the axis in UTC**, which is what every cited authority does for
_data_. Rejected for the view: this tool exists so an analyst can point at a
chart and say why something matters, and the phenomena are diurnal — a reader
reasoning about a night-time signal in UTC is doing arithmetic on every glance.
UTC remains the record and the storage; local remains the display, which is
NDBC's own split.

**Refusing to build a window that spans a transition.** Rejected: it makes a
legitimate class of study unviewable to avoid a labelling problem that has a
standard solution.

## See also

- `CLAUDE.md`, "Time" — the invariants, which now cite these sources
- `CONTEXT.md`, "Time" — instant, wall time, offset, zone designator
- Issue #15 (the view chart), issue #31 (the workbook, still open)
