# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## `AFK` and `HITL` are a second axis, not triage states

This repo also labels every issue `AFK` (can be implemented and merged without a
human) or `HITL` (needs a human decision, or a look at the artifact). That is a
statement about **how the work gets done**, and it is independent of where the
issue sits in the triage state machine.

Do not read `AFK`/`HITL` as triage state, and do not set a triage state from
them. An issue can be `needs-info` *and* `AFK`: it is blocked on the reporter,
and once unblocked no human is needed to finish it.

`CLAUDE.md` sets the default: prefer `AFK`. A slice whose whole purpose is that
something reads correctly *to a person* is `HITL`, because no gate can assert it.
