# 1. The chart's view belongs to the user

Date: 2026-08-07

## Status

Accepted

## Context

The view window pins its axes and turns autoscaling off. That pin was not a
design choice about framing — it was a bugfix. `SpanSelector` keeps its rubber
band as a `Rectangle` initialised at x = 0, the matplotlib epoch, and with
autoscaling on that rectangle joined the data limits: the first redraw after a
region was saved rescaled the chart to span 1970 to now, squeezing 45 days of
data into a few pixels. Observed as `xlim (-1033, 21704)` where the data
occupied `(20596, 20641)`.

The pin reads, to anyone opening the file, as the code intending to own the
frame. It does not. It intends to stop one artist from moving it.

Two things then pulled in opposite directions:

- A ghost line — a borrowed pair's absent series, standardised over the current
  window — can exceed the range of the two plotted series easily. Cropping a
  line whose entire purpose is to show what a series was doing, with nothing on
  screen saying it had been cropped, is not acceptable.
- Zooming in and marking a region is a normal thing to want, and a redraw
  happens every time a region is saved, adjusted or deleted.

With the frame recomputed inside the drawing path, those collide: saving a
region while zoomed snapped the vertical scale back to full range. Measured as
y `(-1.47, 1.46)` → `(-4.90, 4.86)` on a save. The x zoom survived and the y
zoom did not, which is worse than either being consistent.

## Decision

The view is owned by whoever is looking at it.

- **A redraw never moves the frame.** Saving, adjusting, deleting or reloading
  a region leaves both axes exactly where they were.
- **Explicit actions may move it**: zoom, pan and home, the scroll wheel on the
  time axis, borrowing or removing a region set, and selecting a region that
  lies outside the current view.
- **Selecting a region pans, and never rescales.** The zoom level is the user's
  statement about what scale they want to read at.
- Every programmatic change is pushed onto the toolbar's view history with
  `push_current()`, so Back returns to exactly the previous view.
- The x pin from the rubber-band fix stays, because the bug it fixes is real.

## Consequences

Widening the frame for a ghost line happens at the moment the set is borrowed,
which is an action the user took, rather than on every subsequent redraw. When
the last ghost is removed, the original frame is restored only if the user has
not moved the view since.

A gate has to assert this by driving a zoom and then a save, and comparing the
limits before and after — a check that the drawing code cannot make about
itself, because the bug was that the drawing code was making it.

## Alternatives considered

**Let the ghost line crop, and report the crop** in the same idiom as the
omitted-region count. Rejected: the ghost exists to show what a series was
doing, and a partially drawn line answers that question wrongly rather than
incompletely. Reporting it in words is weaker than showing it.

**Guard the rescale against a user zoom** by comparing the current limits with
the stored ones inside the redraw path. Rejected: it leaves the rule living
inside the drawing code as a condition bolted onto the bug, rather than as a
statement about who owns the frame.
