# 4. The exported workbook is charted on UTC; local time rides beside it

Date: 2026-08-09

## Status

Accepted. Extends [0003](0003-time-representation-standards.md), which decided
the same question for the interactive view and reached the opposite answer for
a reason that does not hold here.

## Context

`exporter.py` wrote naive local time and charted against it. Two problems fell
out of that, and the second was the worse one.

**The charts distorted across both transitions.** An Excel scatter chart's x
values are date serials, and local serials are not monotonic:

```text
fall-back    01:00 PDT  serial 46327.041667
             01:30 PDT         46327.062500   +30 min
             01:00 PST         46327.041667   -30 min   <- the line doubles back
             01:30 PST         46327.062500

spring-fwd   01:30 PST  serial 46089.062500
             03:00 PDT         46089.125000   +90 min   <- 30 min drawn 3x wide
```

November doubled the line back on itself for an hour. March produced no kink at
all — just a feature drawn three times as wide as it should have been, which is
the more dangerous of the two because nothing looks wrong. Both read as
instrument behaviour, and nothing on the sheet said otherwise.

**`counts` and `normalized` could not be resolved back to instants.** They wrote
`time (local)` alone. Across the fall-back they held two rows with a
byte-identical timestamp naming two different hours, with nothing in the file to
tell them apart. `data` survived only because it happened to carry a second UTC
column. This is the `AUDIT.md` C4 lesson one step removed: not a column whose
name lies about its zone, but a column whose *values* cannot name an instant.

A workbook is a deliverable. Once it is open on someone else's machine, that
hour is gone.

## Decision

1. **Every data sheet leads with the same time block**: `time (UTC)` first as a
   native datetime — the record, and what the charts are drawn against — then
   `time (local, America/Los_Angeles)` as display. The header names the IANA
   zone, per [RFC 9557](https://www.rfc-editor.org/rfc/rfc9557.html).
2. **A `zone` column carrying PDT/PST appears only when the window spans a
   transition.** Outside one, a wall time in a named zone is already
   unambiguous. This is the rule 0003 already applies to the view's tick
   labels, for the same reason.
3. **The charts are drawn on UTC serials**, and the x axis is titled `time
   (UTC)`.
4. **The transition is stated in prose on the provenance sheet** — the instant,
   what a reader would see, and which column resolves it — alongside the chart's
   time basis. When no transition is spanned, that is said out loud rather than
   left blank.

## Why not the alternatives

**Keep the local axis and only fix the unrecoverable columns.** Rejected: it
leaves the charts distorting. The whole reason this was worth doing before any
real window spans a transition is that the March stretch produces no visible
artefact to prompt anyone to look.

**Chart UTC geometry but label the ticks in local time**, which is what 0003
does for the view and would have been the best of both. **Not possible in
Excel.** A scatter chart's x axis is a `valAx`; openpyxl exposes `tickLblPos`
(where labels sit) and nothing for their content, because Excel derives value-axis
labels from the numeric scale and a number format. Per-tick text requires a
*category* axis, which spaces points equally by index — destroying non-uniform
sampling and the `CM_PER_DAY` elapsed-time width rule that exists so two
intervals can be compared. It trades one distortion for a worse one.

**A drawn device marking the transition** — a bracket, shading, a vertical
rule. Rejected in 0003 for the view, and worse here: on an axis that no longer
distorts, they would mark a discontinuity the drawing does not have.

## Why this differs from 0003

0003 rejected a UTC axis **for the view**, because that tool exists so an
analyst can point at a chart and say why something matters, the phenomena are
diurnal, and a reader reasoning about a night-time signal in UTC does
arithmetic on every glance.

A delivered workbook is the archive half of the same split. It is read later,
by someone else, possibly without this repo — which is exactly the case every
authority cited in 0003 answers with UTC. NDBC ships data in UTC and offers
local to a *viewer*; NCEI/IOOS asks for UTC on submission. The view is the
viewer. The workbook is the submission.

The local rendering does not disappear: it is a column on every data sheet, and
both endpoints appear with designators in the subtitle and on the provenance
sheet.

## Consequences

Charts produced before this read local on the x axis and charts produced after
read UTC, and the two are not comparable by eye across the change. No migration
was written — existing workbooks under `outputs/` are regenerable by re-exporting,
`outputs/` is never scanned as input, and nothing in the repo reads a generated
workbook back.

The `zone` column shifts the data columns one to the right on a spanning window.
Anything indexing the `data` sheet by a fixed column number is wrong; `stats`
was, and its formulas silently pointed at the time block until the gate caught
it.

`exporter.py --check` is new, and is the first gate over the workbook. It writes
real files over three windows — one spanning nothing, one crossing 1 November,
one crossing 8 March — and asserts against the reopened file rather than against
what the writer intended.

## See also

- [0003](0003-time-representation-standards.md) — the standards, and the view's
  answer to the same question
- `CLAUDE.md`, "Time" — the invariants
- Issue #31; issue #15 for the view chart
