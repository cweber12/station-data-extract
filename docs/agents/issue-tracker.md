# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Repo-specific rules

`CLAUDE.md` ("How to work") governs. Where it is stricter than the conventions
above, it wins:

- A PRD is published as an issue labelled `ready-for-agent` *before* slice 1.
- **Never close or edit the PRD issue itself.** It is the record of what was
  decided, not a checklist.
- Issues split from a PRD point at it as parent, are published in dependency
  order, and each names a real blocker.
- Every issue is additionally marked `AFK` or `HITL`. That axis is separate
  from triage state — see `docs/agents/triage-labels.md`.
- A PR body carries `Closes #<issue>` and the actual gate output.
