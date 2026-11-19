# microtask workspace

This folder holds microtask cards: tasks and notes as Markdown files in
`cards/`. Change them only through the `mt` command, never by editing the
files yourself.

Run `mt info` first. Its `Application:` line is the microtask app folder. Read
the `AGENTS.md` and `.agents/skills/microtask/SKILL.md` there before working
with cards, and follow them. Run `mt` from this folder; there is no `./mt`
here.

If `mt` is not installed, for example in a cloud container, install it for
this session, then use `/tmp/microtask/mt -R .` wherever the instructions say
`mt`:

```sh
curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash -s -- --yes --no-path --dir /tmp/microtask --workspace "$PWD"
```
