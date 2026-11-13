---
name: mt-weekly
description: Summarize what was done in microtask over the past week (or another period) as a short report with the most important points. Use for weekly summaries, standup or status updates, and "what did I do this week" questions.
---

# Summarize the week

Follow the [microtask skill](../microtask/SKILL.md) for reading cards. This
skill only reads: it never edits, comments on, or completes cards.

## Gather

The period is the last 7 days unless the user names another one, such as
"since Monday" or "last two weeks". Use `--by` only with the user's own
identity, and only when they ask for one person's work.

When the user narrows the scope ("work only", "just the thesis", "skip
personal"), map it to the `--topic`, `--project`, or `--tag` filters, using
names from `mt topic list --paths` and existing cards. Ask once if the mapping
is unclear, and name the scope at the top of the report.

- `mt list --completed-since PERIOD --full` for finished tasks. This is the
  core of the report.
- `mt list --updated-since PERIOD --full` for work that moved without
  finishing: tasks in `doing` or waiting, new notes, and comments.
- `mt time report --session-since PERIOD` when the workspace tracks time.
- `mt focus --json` for deadlines coming up soon.

Read the bodies and recent comments of the cards that matter; titles alone
often hide what was actually achieved.

## Write

Keep it short enough to paste into a message: about five to seven bullets in
total, grouped under these headings, each left out when empty:

- **Done**: outcomes, not one bullet per card. Merge related cards into one
  point, and lead with what matters most.
- **In progress**: notable work that moved, and anything blocked or waiting
  on someone.
- **Next**: one or two upcoming deadlines or commitments.

End with one line on tracked time when there is any, such as where most of it
went. Add `@ref`s so each point can be traced to its card. Leave out small
chores unless the week had little else, and say plainly if the week was
quiet.

Use plain, complete sentences in the user's language. Without instructions,
always use the format below so reports stay comparable from week to week.
Change it only when the user asks, such as for Slack, email, or a single
paragraph.

> **Done**
> - Sent the proposal draft to Marta with the updated budget (@a12bc345)
> - Fixed the dashboard filter bug reported by two users (@c34de567)
>
> **In progress**
> - Paper review is half done; waiting on the final figures (@d45ef678)
>
> **Next**
> - Grant application due on the 20th (@e56fa789)
>
> About 14 hours tracked, most of it on the proposal.
