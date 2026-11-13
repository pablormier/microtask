# microtask

This folder is a microtask workspace. Tasks and notes ("cards") are Markdown
files in `cards/`, managed with the `./mt` CLI. Opening the workspace does not
assign its tasks to you.

To work with cards, read the
[microtask skill](.agents/skills/microtask/SKILL.md). Related skills cover
[triage](.agents/skills/mt-triage/SKILL.md),
[choosing what to do next](.agents/skills/mt-focus/SKILL.md), and
[weekly summaries](.agents/skills/mt-weekly/SKILL.md).

The rules that matter most:

- Change cards only through `./mt`, never by editing files in `cards/`.
- Show a proposed new card and wait for agreement before creating it, unless
  the user asked for immediate creation.
- Ask before completing, deleting, converting, or taking over a card, and
  before `mt sync` or `mt update`. Do not stage or commit yourself.
- `mt capture`, editors, `mt show`, and `mt web` are for humans. Use explicit
  arguments and `--file -` instead.

Use `./mt help`, `./mt COMMAND --help`, and `./mt policy --json` for syntax and
defaults instead of reading the source.

To change microtask itself (code, dashboard, tests, skills), follow
[CONTRIBUTING.md](CONTRIBUTING.md).
