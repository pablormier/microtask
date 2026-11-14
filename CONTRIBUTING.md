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
  - `workspace.py`: explicit workspace paths, active/demo root resolution, and `Workspace`.
  - `store.py`: card reading/discovery, policy, timers, focus state, and typed `TaskError`s.
  - `validation.py`: card/workspace validation (`audit_workspace` and scoped write validation).
  - `transaction.py`: file locking (`time_mutation_lock`), atomic multi-file writes, and `mt sync`.
  - `operations.py`: the core operations API — **the only write path** for cards and timers.
  - `cli.py`: CLI argument parsing, formatting, and interactive commands.
- Do not spawn a subprocess unless it runs another program or another version
  (`git`, `gh`, the user's editor, browser, `mt update`, `install.sh`, or
  `os.execve` into another workspace's `mt`). Internal commands, validation, and
  the web dashboard call the Python API in-process.
- `next_action` and `processing_instructions` are optional properties.
  `templates/` contains generated references for contributors, not operational
  inputs.
- Keep the core zero-install with Python 3 and macOS Bash 3.2. The approved
  exception to standard-library-only dependencies is bundled pure-Python PyYAML
  under `scripts/vendor`, including its license and pinned provenance. Do not
  require a system YAML package, pip installation, or compiled extension.
- Preserve `scripts/validate` as the trust boundary.
- Routine card operations must be possible from `mt help`, `mt policy`,
  `mt read`, and CLI diagnostics. If an agent needs to read source to perform
  one, treat that as a help gap and fix the help.

Before finishing, run the relevant tests (see the [test guide](tests/README.md)),
`bash -n mt install.sh`, `python3 -m py_compile scripts/*.py`, and
`./mt validate`.

## Testing interactive features

Human-only modes (`mt capture`, editor modes, `mt show`, `mt web`) may be
driven by agents only in an isolated scratch workspace with fixture data and
auto-sync disabled. Allocate it with `./mt scratch`.

## Web UI

Inspect nearby templates and `tools/web/styles/` first. Reuse shared component
styles and theme variables. Match related controls' placement, order,
dimensions, spacing, and icon treatment. Account for Pico's CSS cascade,
including `role="group"` styling.

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
needs, and leave command details to `--help` and `mt policy`. Do not add rules
for behavior the CLI already enforces.

Skills live in `.agents/skills/`. `CLAUDE.md` and `GEMINI.md` only import
`AGENTS.md` for agents that don't read it by default; keep them as one-line
imports. Do not add symlinks: the installer without Git accepts regular files
only.

## Commits

Do not stage or commit card data by hand; `mt sync` and `mt update` own their
Git operations.
