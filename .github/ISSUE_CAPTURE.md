# Issue capture

This repository can capture task requests, notes, and polished reference
material through GitHub Issues. Create an issue only when the user asks to
capture something.

Create one issue per requested item and use the `microtask-capture` label from
the issue form. Keep the issue title concise and put the complete source text
in its body. Preserve useful headings, tables, code, examples, constraints,
links, and acceptance criteria. Do not add classification markers or invent
deadlines, priorities, projects, topics, owners, statuses, or implementation
details.

The `microtask-capture` label must exist in the GitHub repository for the form
to apply it. Earlier capture forms used `enhancement`; relabel only those
existing issues that are intentionally still waiting for capture. The importer
does not treat ordinary enhancement issues as captures.

Before creating the issue, show the proposed title, body, and label, then ask
for confirmation. After creating it, return its issue number and URL.

The importer saves each issue as an unassigned note marked `needs_review`. The
note title is the issue title; its content is the issue body, or the title when
the body is empty. The original issue URL remains available in the note, and
the importer closes an issue only after the local capture is durable and any
configured sync succeeds. Closing means “captured,” not “work completed.”

Triage notes with `mt list --needs-review` and inspect them with `mt read`. Keep
useful source text with minimal edits. Leave reference material as a note. For
a pure action, convert the note to a task after reviewing it and choose a
concrete next action before starting work. For mixed reference and action
content, keep the reference as a note and create a separate task for the
action. Clear `needs_review` only after the full disposition is complete and
approved. Relative Markdown links in the dashboard resolve from the captured
card's local folder, so use absolute URLs for links that should keep working
after capture. The issue URL is kept with the note for source context.

If an open issue changes after capture, the importer leaves it open and stops
instead of replacing a note that may already contain triage edits. Compare the
issue with the local note, reconcile the wanted text through `mt`, sync the
approved result when required by workspace policy, then close the issue once
the capture is reconciled. The source hash is command-owned; do not edit it to
bypass this conflict. There is no automatic conflict-reset command.
