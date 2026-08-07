# Station data extract

Pulls oceanographic feeds for the La Jolla stations into immutable snapshots,
compares series against each other, and lets an analyst point at the parts of a
chart that matter and say why.

## Language

### What an analyst records

**Region**:
One marked span of time on a chart, from a start instant to an end instant.
What a drag across the chart produces. A span of TIME ONLY: it says nothing
about a range of values, which is why its band covers the chart's full height
and why zooming changes how precisely it can be placed but never what it means.
Written in full as _region of interest_ where a label has room; _ROI_ is the
short form.
_Avoid_: mark, occurrence, band, interval, highlight

**Region set**:
A named, reasoned group of regions that are all the same phenomenon seen more
than once. Carries the name, the reason, the colour, and the pair it was drawn
against.
_Avoid_: annotation set, mark set, group, category

**Reason**:
Why a region set matters, written by the analyst. Belongs to the set, not to
the individual region, and cannot be regenerated from anything.

### What is being looked at

**Pair**:
The two series a chart compares. The unit of viewing: a chart is always of
exactly two series, and a region set always records which two it was drawn
against.

**Window**:
The stretch of time a build covers. Fixed when the chart is built. A region
outside it cannot be drawn at all, and reaching it means rebuilding wider.
_Avoid_: range, period, extent

**View**:
The part of the window currently on screen after zooming or panning. A region
outside the view is drawn, just not where you are looking.
_Avoid_: window, frame, viewport

**Series**:
One measured quantity at one station over time, identified by a resolvable key
and carrying its own depth and reference frame.
_Avoid_: column, channel, signal

**Region set from another pair**:
A region set marked on a different pair that shares exactly one series with the
pair on screen, shown here to see whether its regions coincide with what is
displayed. Read-only wherever it is shown, and always labelled with the pair it
was marked on.
_Avoid_: borrowed set, foreign set, overlay, imported marks

**Ghost line**:
The member of a borrowed set's pair that is absent from the current chart,
drawn faintly for context. Never a compared series.

### Where it all lives

**Study**:
An immutable snapshot of what the feeds said at one moment, plus everything
derived from it. Evidence; only its outputs and its regions may change after
creation.
_Avoid_: session, snapshot, pull, run

**Evidence**:
What a feed reported and when. Never edited, never regenerated.

**Interpretation**:
What a person concluded from the evidence. Expected to accumulate and be
revised, which is why regions live apart from the evidence they describe.

## Notes on the vocabulary

_Region_ is the analyst's word; the code still says `MarkSet` and `Interval`,
and the on-disk schema still spells a set's fields `set_id`, `name`, `reason`
and `intervals`. `region == annotations.Interval` and
`region set == annotations.MarkSet`. The screen and the code will speak
differently until a mechanical rename, which is a schema migration across
existing studies and is not free.
