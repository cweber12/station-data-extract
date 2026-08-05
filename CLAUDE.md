# Working in this repo

## How to work: confirm, plan, slice, commit

Follow this for any task beyond a one-line fix.

### 1. Confirm understanding before doing anything

Say back what you think is being asked, in your own words, including anything
ambiguous and how you intend to read it. If two readings would lead to
materially different work, ask — don't pick one silently.

Also say what you think is *out* of scope. Most misunderstandings here have been
about scope, not intent.

### 2. Produce a plan in logical slices

A slice is a change that:

- does one thing you can name in a short sentence,
- leaves the repo working and the tests/gates passing,
- can be committed on its own and understood from its commit message alone.

Rename, refactor, bugfix and new feature are **separate slices**, even when they
touch the same file. If a slice cannot be described without the word "and", it
is probably two slices.

State the slices in order, with the dependencies between them. Estimate nothing;
just make the order defensible.

### 3. Confirm the plan

Present the slice list and wait for agreement before implementing. If the plan
changes mid-flight — and it will, because verification surfaces real bugs — say
so and re-confirm rather than quietly expanding scope.

### 4. Implement one slice, verify it, commit it

**Commit after every slice.** Not at the end of the task, not once per session —
after each slice. A clean working tree between slices is the point: it means any
slice can be reverted or bisected on its own.

Before committing a slice:

- run whatever verifies it (see *Verification* below),
- confirm the working tree contains only that slice's changes,
- write a message that says what changed and *why*, not just what.

Then move to the next slice. Do not batch commits.

### 5. Report honestly

If a slice is blocked, say so and finish the others. If verification fails, show
the output. If you found a bug in your own earlier work, say that plainly — it
has happened repeatedly here and catching it is worth more than looking tidy.

---

## Non-negotiable invariants

These were expensive to learn. Violating one silently corrupts results.

### Time

**A column name is not evidence of a timezone.** The original workbook's
`time (UTC)` columns held Pacific local time; Power Query's implicit
text→datetime conversion applied the machine's UTC−7 offset and discarded the
zone. Every downstream conclusion built on it was wrong.

- Parse timestamps explicitly, or build them from parts. Never let a library
  infer a zone.
- Never "fix" a zone by adding a constant. Parse correctly instead.
- Where a zone genuinely cannot be known (an arbitrary user file), classify it,
  assume the documented default, and **record the assumption** in the manifest
  and on the provenance sheet.
- `ingest/clockcheck.py` is the only real evidence. A study that changes ingest
  must still pass it: air temperature within ±1.5 h at LJAC1.

### Data boundaries

- `sources/` is read-only input. Never write into it from code.
- `outputs/` is never scanned as input. Generated files must not become sources.
- Studies live **one level above this repo**, in `../studies/`, so the sibling
  extractors can read them. `study.json` is shared; `station-data/` is ours.
- A study is immutable once created, apart from its `outputs/`. A failed
  creation is left on disk with `status: incomplete` — a failed pull is evidence
  about the feed, and deleting it destroys the only record it happened.
- A study's sources are exactly: the script pulls, plus files the user attached.
  **Nothing is included implicitly.** The Power Query workbook is not part of a
  study; it duplicates the feeds.

### Geometry and QC

- Depths and reference frames come from `config/stations.yaml`, never from the
  feed — ERDDAP reports `z = 0.0` for both the 5 m pier CTD and the surface buoy.
- 46254 follows the sea surface; the logger sits on the seabed. They are not
  peers. Their relationship is the stratification index, not a correlation.
- LJPC1 reports no temperature at all. Don't re-add it to a temperature
  pipeline; `role: context_only` exists to prevent that.
- QARTOD: reject flags 4 and 9, keep 1 and 2, keep-but-flag 3. Report the count
  of suspect values kept.
- Never invent a station id, dataset id or URL. Leave `# TODO(verify)` and say
  so in your summary.

### Presentation

- No dual-axis charts. Two scales on one frame make any two series look however
  you want. Use the z-score sheet to compare shape.
- Every chart legend carries sensor depth and reference frame.
- The lag scan always reports the ±12.42 h M2 alias alongside any result.
- Nothing is dropped silently. An empty or unusable series is reported in the
  log, the dialog and the provenance sheet.

### Tkinter threading

Tk is not thread-safe, and `after()` is itself a Tk call. Workers must touch
**only a `queue.Queue`**; the main thread drains it on a timer. Read every Tk
variable on the main thread before starting a worker. Getting this wrong hangs
the UI with no error, because the exception lands inside the error handler.
See the threading contract on `ProgressDialog` in `compare.py`.

---

## Environment

- Use `.venv\Scripts\python.exe`. The system Python lacks several dependencies,
  and `run.ps1` prefers the venv for that reason.
- `pywin32` is optional and only used for the Excel paths. Everything else runs
  headless.
- Excel automation requires Excel closed. A `~$name.xlsx` file next to a
  workbook means it is open, or crashed and left a lock.

## Verification

Don't claim something works because it constructed without raising. Two real
bugs shipped past exactly that reasoning: a Tk dialog that was fully populated
but `withdrawn` (invisible), and a Power Query refresh that "succeeded" while
silently keeping the previous cached table.

Assert the property that matters:

- a window is `winfo_viewable()`, not merely constructed;
- a refresh changed the data, not merely returned;
- a study validates, not merely wrote files;
- a parser produced the right value, not merely no exception.

Before committing anything that touches ingest, the time handling or the study
layout, run the gate checks: clock check on `sources/`, study validation, the
`+7 h` corrupted fixture still failing, archive union-not-sum, and
`sensorkit`/`exporter` importing with `tkinter` blocked.
