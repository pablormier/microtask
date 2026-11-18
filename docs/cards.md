# Card files

Each task or note is one self-contained Markdown file in `cards/`. You can
open, read, and edit it with any Markdown editor, and it stays readable
without microtask.

```markdown
---
id: abc123
kind: task
title: Assess the paper's causal claim
status: active
type: task
priority: none
created: 2026-11-10
updated: 2026-11-10
next_action: Compare the baselines.
comments:
  - at: "2026-11-10T10:00:00"
    text: Added the experimental comparison.
---

## Review

The experiments establish prediction performance.

## Questions

- Do they establish causation?
```

- **Frontmatter** (between the `---` lines) holds the title, kind, status,
  other properties, comments, and saved time entries.
- **Time**: finished sessions are `time_entries`; a timer that is running is
  the task's `timer` property (below).
- **Body**: everything after the frontmatter is ordinary Markdown with no
  required sections. Headings have no special meaning, so you can use tables,
  equations, images, checklists, or code however you like.
- `next_action` is task-only. `processing_instructions` is optional guidance
  for triage. microtask never reads instructions from the body.

## Running timers

`mt time start` writes the running timer onto the card, together with the
status and claim change it makes. `mt time stop` removes it and appends the
finished session to `time_entries` in the same write.

```yaml
status: doing
claimed_by: alice
claimed_at: "2026-11-10T09:00:00"
timer:
  session_id: 0123456789abcdef0123456789abcdef
  start: "2026-11-10T09:00:00"
  start_epoch: 1794301200
  by: alice
time_entries:
  - session_id: fedcba9876543210fedcba9876543210
    start: "2026-11-09T14:00:00"
    end: "2026-11-09T14:30:00"
    elapsed_seconds: 1800
```

`timer` is task-only and holds exactly these four fields: a 32-character
lowercase hexadecimal `session_id`, the local `start` timestamp, `start_epoch`
as whole seconds since 1970, and the single-line name `by` of the person who
owns it. A task has at most one. A person may have several timers running on
different tasks (`--parallel`, or a sync that brings one in); `mt time status`
and the dashboard list every one, and `mt time stop REF` or `mt time cancel
REF` settles a specific one. Starting a timer without `--parallel` starts the new
one first and then stops each of your other timers at that moment, one card per
write, so an interrupted switch leaves both running.

Timers are part of the card and travel with it through Git. A timer file from
an older version (`.mt/time-running`) is imported onto its cards the first
time any command takes the workspace lock, then renamed
`.mt/time-running.migrated`. An entry whose task no longer exists is reported
and stays in that file.

## Editing from scripts and agents

Property edits (`mt edit REF --set ...`, `--title`, `--tags`) never touch the
body. To change part of the body, name the passage; it must match exactly once,
so no hash is needed. To replace the whole body, read the card's hash first and
pass it back. If the card changed in the meantime, nothing is written:

```sh
mt read @4e91a7c2 --json                 # ref, path, properties, body, content_hash
mt edit @4e91a7c2 --old "Score: 0.81" --new "Score: 0.83"
mt edit @4e91a7c2 --replace-file replacements.json
mt edit @4e91a7c2 --body --append --file discussion.md
mt edit @4e91a7c2 --body --file revised.md --expected-hash HASH
```

A replacements file is a JSON array such as
`[{"old":"Score: 0.81","new":"Score: 0.83"}]`; use `--replace-file -` with a
quoted heredoc to pass it on stdin. Replacements must not overlap and apply
all together or not at all. `--expected-hash` is optional for both forms and
enforced when given. Input files contain body text only, never frontmatter.

With `--json`, `add`, `note`, `edit`, `move`, `done`, and `comment` return the
saved card (`ref`, `path`, `content_hash`, `properties`, `body`) along with
`changed` and `updated`.

A claim on a task blocks status changes, archiving, and body rewrites by other
names. Appending to the body, comments, and other property edits stay open.

`mt validate` checks every card. Each edit already validates the cards it
touches.
