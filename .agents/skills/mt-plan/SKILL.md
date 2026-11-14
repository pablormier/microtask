---
name: mt-plan
description: Plan the week ahead in microtask, highlighting deadlines, started work, and follow-ups in plain language, then hand off to focus for the first task. Use for Monday morning planning or "what should I do this week" questions.
---

# Plan the week ahead

Follow the [microtask skill](../microtask/SKILL.md) for reading and editing
cards. This skill plans the week; [triage](../mt-triage/SKILL.md) organizes
captures, [focus](../mt-focus/SKILL.md) picks one task to start right now, and
[weekly](../mt-weekly/SKILL.md) summarizes the week that passed.

## Gather

Do not just reformat `mt focus` output. Gather enough context to make good
judgments about what actually matters, what is blocked, and what needs a head
start—then keep the output short.

Use `--by` only with the user's own identity. Support scope filters
(`--topic`, `--project`, `--tag`) the same way as [weekly](../mt-weekly/SKILL.md)
when the user narrows the week (for example, "work only").

- `mt focus --json` for the saved focus, ranked candidates, nearby deadline
  alerts, and `followup_ids`.
- `mt list --due next --full` and `mt list --due-before DATE --full` (two weeks
  out) for deadlines or targets coming up soon, including next week's items
  that need lead time now.
- `mt list --status doing --full` and `mt list --status waiting --full` to see
  what is already started or waiting on someone else, and how long it has sat.
- `mt list --completed-since 7d --full` and `mt list --updated-since 7d --full`
  to see recent momentum and Friday's unfinished threads.
- `mt list --triage --limit 5 --full` to note whether unreviewed inbox items
  are piling up (or if an urgent capture belongs in this week's plan), without
  triaging them here.

Read the bodies and recent comments of the cards that matter, and search with
`--all` for related notes, project cards, or blockers (for example, meeting
notes, dependencies, or who a task is waiting on). Check whether an upcoming
deadline needs preparation or input from someone else before the due date, and
use `updated` dates and comments to tell active work apart from stale `doing`
or `waiting` cards.

Ask at most one short question, and only if knowing the user's capacity or
schedule this week would change the plan.

## Present the plan

Keep it easy to read on low energy: about five to seven bullets in total, in
plain, everyday language. Never dump full backlogs or use raw status names and
jargon. Omit any section that is empty, and add `@ref`s so every item is easy
to find.

Group by what the user needs to decide:

- **Must do this week**: overdue items, hard deadlines this week, and anything
  due next week that needs a head start now.
- **Already started**: work in progress—flag anything stale so the user can
  decide whether to finish it or pause it.
- **Waiting on others**: items that need a quick follow-up or nudge this week.
- **If you have time**: one or two good next picks from `active` candidates.

Add one short note at the end if unreviewed inbox items are waiting for
[triage](../mt-triage/SKILL.md), or if a few quick board updates would clean
things up (for example, pausing stale started tasks, adjusting priorities, or
deferring things that clearly won't happen this week). Propose any edits in one
small batch and apply them only after the user agrees.

## Hand off to focus

End by recommending one concrete starting task and a small first step,
following [focus](../mt-focus/SKILL.md), and ask **"Focus on this?"**. Save
`mt focus REF` only after the user agrees.
