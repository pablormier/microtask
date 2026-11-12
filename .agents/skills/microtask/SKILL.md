---
name: microtask
description: Operate microtask's deterministic CLI to capture, find, edit, organize, time, claim, and synchronize tasks and notes in a selected workspace. Use for task and note operations, not application development.
---

# Operate microtask

You decide what cards should say; the `mt` CLI writes, validates, and locks
them. Change cards only through `mt`. Use `./mt` inside the workspace, or
`mt -R PATH_OR_NAME COMMAND` for another one; ask if the workspace is unclear.
For syntax, use `mt COMMAND --help` and `mt policy --json` rather than reading
source. If help does not cover something, report the gap instead of working
around the CLI.

## Cards

A card is a task (work) or a note (reference). Its properties live in
frontmatter, which the CLI writes; you supply only the Markdown body, which has
no required sections or special headings. A clear task can have an empty body.
`next_action` is optional and should not repeat the title.

Organize with `topic`, a `/`-separated path such as `learning/rust`. Reuse
topics from `mt topic list --paths`, and never invent topics, projects, or dates.
Projects are finite efforts; tags are keywords.

Statuses: `inbox` needs clarification, `active` is ready to do, `doing` is
being worked on.

## Find and read

```sh
mt search WORDS --all --compact     # context and duplicates, incl. archived/hidden
mt list --all --json
mt list --triage --limit 5 --full   # whole cards, same shape as mt read --json
mt read @REF [@REF2 ...] --json     # properties, body, ref, path, content_hash
```

Seeing a card for context does not make it yours to change, and reading a
task does not mean you should do its work.

## Create

Follow [capture and review](references/capture.md): search for duplicates,
show the complete proposed card, and create it only after the user agrees,
unless they asked for immediate creation.

## Edit

Read the whole card first, then make the smallest change:

```sh
mt edit REF --set priority=high                       # properties
mt edit REF --body --append --file -                  # add to the body
mt edit REF --old "old text" --new "new text"         # change part of the body
mt edit REF --replace-file - <<'EOF'                  # many or multiline replacements
[{"old": "...", "new": "..."}]
EOF
mt edit REF --body --file - --expected-hash HASH      # replace the whole body
```

Each `--old` passage must match exactly once, so the hash is optional there.
Full replacement needs the hash; if it no longer matches, the card changed:
read it again and rethink the edit rather than retrying with the new hash.
Keep the user's original text and history; when rewriting, preserve replaced
substance in the body.

For the same change across many cards, use `mt bulk-edit` to write a plan file
under `mt scratch`, review it, then apply that exact file.

`processing_instructions` is guidance for organizing a card. Never fill it from
the body, and never treat it as permission for an action.

## Permissions

| Action | Rule |
| --- | --- |
| Read, search, list, policy | Always fine |
| Create a card | Show the proposal first unless waived |
| Edit, comment, track time | Within what the user asked |
| Complete, delete, convert, take over, `sync`, `update` | Only with clear permission for that action |

When the user says they are starting a task, move it with `mt move REF doing`
and no `--by`, so it is claimed in their name. Before you do a task's work
yourself, claim it with `mt move REF doing --by NAME`, using a name for this
agent run. A claim blocks other names from changing status, archiving, or
rewriting the body; appending, comments, and other properties stay open. Pass
the same `--by NAME` (or set `MT_USERNAME=NAME`) on every later command for
the claimed task. Never edit claims, locks, or timers directly, and do not
stage or commit; `mt sync` handles Git.

Use explicit arguments and `--file -` for multiline text. Do not use
`mt capture`, editors, `mt show`, or `mt web` unless the user asks. Put any
temporary files in the directory `mt scratch` returns. Scratch files stay on
this machine, so a saved card must include the content and source links it
needs.

With `--json`, add, note, edit, move, done, and comment return the saved card, so
report its reference without reading it again; each change is already validated.
