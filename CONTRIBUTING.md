# Contributing to microtask

These rules apply to changes to microtask itself: the CLI, the dashboard,
the installer, tests, and the bundled skills. For using microtask to manage
cards, see [AGENTS.md](AGENTS.md) and the
[microtask skill](.agents/skills/microtask/SKILL.md).

## Code

Prefer small, explicit code and fewer concepts.

- `scripts/card_format.py` owns card parsing, serialization, and schema. Keep
  property edits separate from body operations. Do not reserve Markdown
  headings, encode capture wrappers, or introduce alternate runtime
  representations.
- Module ownership in `scripts/`:
  - `workspace.py`: workspace resolution (`MT_WORKSPACE`, `workspace/`, an old-style root), creating a workspace from `templates/` (`mt init`, the installer), demo seeding (`seed_demo_workspace`), and `Workspace`.
  - `update.py` and `migrate.py`: `mt update` (release tarballs, no Git) and `mt migrate` (old-style workspace into `workspace/`).
  - `store.py`: card reading/discovery, settings, timers, focus state, and typed `TaskError`s.
  - `validation.py`: card/workspace validation (`audit_workspace`, `check_changes`, and `format_audit_report`).
  - `transaction.py`: file locking (`time_mutation_lock`), atomic multi-file writes, and `mt sync`.
  - `card_render.py`: card document rendering, body editing, metadata parsing/validation, and topic tree helpers.
  - `focus.py`: read-only focus ranking and context helpers.
  - `timer_ops.py`: timer lifecycle and time-session helpers.
  - `operations.py`: the core operations API (`op_*`) — **the only write path** for cards, timers, focus, and drafts.
  - `cli.py`: CLI argument parsing, formatting, and interactive commands.
- Do not spawn a subprocess unless it runs another program or another version
  (`git`, `gh`, the user's editor, browser, `mt update`, `install.sh`, or
  `os.execve` into another workspace's `mt`). Internal commands, validation, and
  the web dashboard call the Python API in-process.
- `next_action` and `processing_instructions` are optional properties.
  `templates/task.md` and `templates/note.md` are generated references for
  contributors. `templates/settings.yaml` and `templates/workspace.gitignore`
  are operational: a new workspace is created from them.
- Keep the core zero-install with Python 3 and macOS Bash 3.2. The approved
  exception to standard-library-only dependencies is bundled pure-Python PyYAML
  under `scripts/vendor`, including its license and pinned provenance. The
  dashboard's browser libraries (Pico, Alpine, marked, DOMPurify, KaTeX) are
  bundled the same way under `tools/web/vendor`, each with its license and a
  `VENDOR.json` provenance record, so the dashboard works offline. Do not
  require a system YAML package, pip installation, or compiled extension.
- Keep `scripts/validate` working on its own. It shares its checks and output with `mt validate`.
- Routine card operations must be possible from `mt help`, `mt settings`,
  `mt read`, and CLI diagnostics. If an agent needs to read source to perform
  one, treat that as a help gap and fix the help.

Before finishing, run the relevant tests (see the [test guide](tests/README.md)),
`bash -n mt install.sh`, `python3 -m py_compile scripts/*.py tools/*.py`, and
`./mt validate`.

## Working in a clone

A clone is an app folder like any other. Nothing creates a workspace by
accident: plain `./mt` stops with "no workspace yet". To try things with test
cards, run `./mt init`; the repository's `.gitignore` keeps `workspace/` out of
Git. Update a clone with `git pull`; `mt update` refuses to run next
to a `.git`. To test with an isolated workspace, set `MT_WORKSPACE` to an
absolute path (a folder with `cards/` and `settings.yaml`). Never point tests at
a workspace you use for real, and give any copy of one no Git remote and
`rules.auto_sync: false` before writing to it.

Tests that need an old-style workspace (cards and `settings.yaml` next to the
code) copy the checkout and call `support.legacy_workspace` on the copy. Tests
that exercise `mt update` and `install.sh` build a release tarball with
`support.make_release` and point `MT_ARCHIVE_URL` and `MT_RELEASE_TAG` at it, so
nothing touches the network.

## Testing interactive features

Human-only modes (`mt capture`, editor modes, `mt show`, `mt web`) may be
driven by agents only in an isolated temporary workspace (e.g. via `mktemp -d`)
with fixture data and auto-sync disabled.

## Web UI

Inspect nearby templates and `tools/web/styles/` first. Reuse shared component
styles and theme variables. Match related controls' placement, order,
dimensions, spacing, and icon treatment. Account for Pico's CSS cascade,
including `role="group"` styling.

The dashboard loads no third-party URLs. To change a browser library, edit its
version in `tools/vendor_web.py`, run `python3 tools/vendor_web.py` (it verifies
npm integrity and rewrites `tools/web/vendor`), and review the diff. The server
serves those files under `/vendor/`, and the `mt show` preview (`tools/mdviz.py`)
inlines the same files into its generated page, with the KaTeX fonts embedded as
data URIs. Tests never run the script.

Before finishing, verify the rendered UI in a browser at desktop and narrow
widths, including hover, focus, and disabled states; fix visual defects and
save a screenshot. Passing markup or behavior tests alone does not establish
visual correctness.

## README demo

`docs/images/demo.gif` is recorded with [VHS](https://github.com/charmbracelet/vhs)
from `docs/demo/demo.tape`, against a throwaway copy of the example cards. After
changing CLI output, re-record it from the repository root with
`vhs docs/demo/demo.tape`.

## Skills

When adding a skill, review the existing skills first. Give it a distinct
scope, reference existing skills rather than repeating their instructions, and
resolve any contradictions. Keep it short: include only what the workflow
needs, and leave command details to `--help` and `mt settings`. Do not add rules
for behavior the CLI already enforces.

Skills live in `.agents/skills/`. `CLAUDE.md` and `GEMINI.md` only import
`AGENTS.md` for agents that don't read it by default; keep them as one-line
imports. Do not add symlinks: the installer and `mt update` accept regular files only.

## Commits

Do not stage or commit card data by hand; `mt sync` owns the workspace's Git
operations, and `mt migrate` makes one commit there.

## Later cleanups

These wait until after 0.2.0, so that 0.1.x installs can still update and
existing scripts keep working:

- Rename `policy` to `settings` inside the code (`policy_parser.py`,
  `PolicyCache`, `store.policy()`, and the JS `*Policy` names).
- Remove `mt update --force`, which now does nothing.
- Decide whether to retire `scripts/validate`, and give every `--json` output
  the same shape.
