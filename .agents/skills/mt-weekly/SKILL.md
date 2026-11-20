---
name: mt-weekly
description: Summarize what was done in microtask over the past week (or another period) as a short report with the most important points, and optionally save it as a weekly note. Use for weekly summaries, standup or status updates, and "what did I do this week" questions.
---

# Summarize the week

Follow the [microtask skill](../microtask/SKILL.md) for reading and creating
cards. This skill never edits, comments on, or completes existing cards; it
only reads them and, after the user reviews the report, can save the summary
as a new Note.

## Gather

The period is the last 7 days unless the user names another one, such as
"since Monday" or "last two weeks". Use `--by` only with the user's own
identity, and only when they ask for one person's work.

Check `mt topic list --paths` to see how the workspace is organized:
- When the user narrows the scope ("work only", "just the thesis", "skip
  personal"), map it to the `--topic`, `--project`, or `--tag` filters from
  existing cards. Ask once if the mapping is unclear.
- When the user does not specify a scope and the workspace separates
  work/project topics from personal ones, default to the work/project topics
  and note at the end that personal topics were omitted (offering to include
  them).
- Always name the included scope at the top of the report when a filter is
  applied.

Run these read queries:
- `mt list --completed-since PERIOD --full` for finished tasks. This is the
  core of the report.
- `mt list --updated-since PERIOD --compact` to spot work that moved without
  finishing (tasks in `doing` or waiting, new notes, and comments), followed by
  `mt read @REF [@REF2 ...] --json` on the relevant cards from this period.
- `mt time report --session-since PERIOD` when the workspace tracks time.
- `mt focus --json` for deadlines coming up soon.

Read the bodies, `next_action`, and recent comments of the cards that matter;
titles alone often hide what was actually achieved or overstate a quick skim or
minor check, so match your wording to what the card and comments actually say.

## Write

Keep it short enough to paste into a message: about five to seven bullets in
total, grouped under these headings, each left out when empty:

- **Done**: outcomes, not one bullet per card. Merge related cards into one
  point, lead with what matters most, and don't overstate light tasks (e.g.,
  say "Skimmed..." rather than "Prepared..." when the card only notes a quick
  read).
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

> *Scope: work*
>
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

## Review and save as a Note

After presenting the report, ask whether the user wants any adjustments (or
wants omitted personal topics included) and whether to save the summary as a
Note:
- Reuse an existing weekly/report topic from `mt topic list --paths` if one
  exists (such as a `.../weekly` topic), or suggest a `weekly` subtopic under
  the active scope.
- Follow the [microtask skill](../microtask/SKILL.md) creation workflow and
  save with `mt note` only after the user confirms.
