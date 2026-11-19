# microtask workspace

This folder holds microtask cards: tasks and notes as Markdown files in
`cards/`. Change them only through the `mt` command, never by editing the
files yourself.

From this workspace root, run `mt -R . info` to verify the selected workspace.
Follow the local microtask skill in `.agents/skills/microtask/SKILL.md` or
`.claude/skills/microtask/SKILL.md`. If you have neither copy, run
`mt -R . skills install --workspace` and read the installed skill.

If `mt` is not installed, for example in a cloud container, install it for
this session, then use `/tmp/microtask/mt -R .` wherever the instructions say
`mt`:

```sh
curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash -s -- --yes --no-path --dir /tmp/microtask --workspace "$PWD"
```
