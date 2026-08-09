# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This is a **single-context** repo. There is no `CONTEXT-MAP.md` and no per-context ADR directories.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the domain glossary.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The producer skill (`/grill-with-docs`) creates them lazily when terms or decisions actually get resolved.

## File structure

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-the-view-belongs-to-the-user.md
│   ├── 0002-region-in-the-ui-markset-in-the-code.md
│   ├── 0003-time-representation-standards.md
│   └── 0004-the-workbook-is-charted-on-utc.md
└── docs/agents/                        ← this file, and its siblings
```

Should this repo ever grow into separate contexts, the multi-context layout is a `CONTEXT-MAP.md` at the root pointing at one `CONTEXT.md` per context, with context-scoped decisions under `src/<context>/docs/adr/`. Nothing here assumes that yet.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids — it lists them, per term, under `_Avoid_:`.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/grill-with-docs`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0002 (region in the UI, markset in the code) — but worth reopening because…_

Note that `CLAUDE.md`'s "Non-negotiable invariants" are a stronger constraint than an ADR. Those are not reopenable in passing; violating one silently corrupts results.
