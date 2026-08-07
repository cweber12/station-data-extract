# 2. Region on the screen, MarkSet in the code

Date: 2026-08-07

## Status

Accepted

## Context

The feature was built calling the thing an analyst draws a **mark**, grouped
into a **mark set**, each drawn span an **occurrence** stored as an
**interval**. That vocabulary is in the module name (`annotations.py`), the
dataclasses (`MarkSet`, `Interval`), the on-disk schema (`set_id`, `name`,
`reason`, `intervals`), PRD #1, and every issue under it.

The analyst using it does not call them marks. They are **regions of
interest** — and the panel heading `set / occurrence` was read as a button
rather than as a description, because it named an internal structure rather
than the thing on screen.

A rename is not free. The schema is on disk in seven existing studies. `set_id`
is part of a filename. PRD #1 is, per this repo's working agreement, the record
of what was decided and is explicitly not to be edited. And a half-finished
rename is worse than either end state.

## Decision

The user interface says **region** and **region of interest**. The code and the
stored schema keep **mark**, **MarkSet** and **Interval** for now.

`CONTEXT.md` carries the mapping explicitly:

- `region` == `annotations.Interval`
- `region set` == `annotations.MarkSet`

New user-facing strings use the analyst's vocabulary. New internal identifiers
match the code they sit in, rather than introducing a third dialect.

## Consequences

The screen and the source speak differently, which is normally a smell. Anyone
noticing it will find this file and the glossary rather than a mystery, and
should resist "fixing" one half.

Finishing the rename later is mechanical but not small: the dataclasses, the
module name, the JSON keys, a schema version bump, and a migration for existing
studies — plus the issues and the PRD, which describe decisions taken in the
old vocabulary and are not rewritten lightly.

## Alternatives considered

**Rename everything now**, including the schema. Rejected for this round: it
turns a legibility fix into a data migration, and the migration is the risky
part of it, with no user-visible benefit over renaming the strings.

**Keep calling them marks on screen.** Rejected: the words were the reported
problem. Someone opening the window for the first time could not tell what it
wanted them to do, and `set / occurrence` was actively misleading.
