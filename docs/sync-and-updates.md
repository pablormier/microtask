# Install, sync, and updates

A microtask workspace is a Git repository with two remotes:

```
  your private repo (origin)  <── mt sync ──>  your workspace  <── mt update ──  microtask (upstream)
  cards, drafts, settings                      app + your data                    app files only
```

`mt sync` moves your data to and from **origin**. `mt update` pulls new
versions of the app from **upstream** without touching your data.

## First install

```sh
curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash -s -- ~/todo git@github.com:YOU/tasks.git
```

- Both arguments are optional. The folder defaults to `~/todo`; without a
  repository URL, the installer asks for one when run in a terminal, and you
  can skip it.
- If the repository is **empty**, a new workspace is pushed to it. If it
  **already holds your workspace**, it is cloned instead.
- The installer puts `mt` on your PATH (`~/.zshrc`, `~/.bash_profile` on macOS
  bash, or `~/.bashrc` on Linux) and says if Git still needs your name and
  email.
- Without Git, it downloads a source archive and prints the steps to add Git
  later.

## Another machine

```sh
gh repo clone YOU/tasks ~/todo      # or: git clone URL ~/todo
~/todo/install.sh
```

Inside an existing workspace, `install.sh` only sets up the machine: it adds
the `upstream` remote, puts `mt` on PATH, and checks your Git identity. Running
it again changes nothing that is already set up. If your shell config already puts
another workspace's `mt` on PATH, the installer lists it and says which one new
terminals will run: the last PATH line wins. `mt` always works on the folder it
lives in, so check with `command -v mt` or `mt info`. Repository aliases
(`mt repo add`) and `mt-policy.local.yaml` are per machine, so recreate them if
you use them.

## Sync

```sh
mt sync
```

This commits changes to cards, drafts, and `mt-policy.yaml`, then fetches,
merges, and pushes to `origin`. Set `rules.auto_sync: true` in `mt-policy.yaml`
to sync after every card change. Everything works locally without Git; sync is
optional. To connect a repository later:

```sh
mt run git remote add origin git@github.com:YOU/tasks.git
```

Your cards are personal, so origin must be a private repository. Do not fork
microtask for this: GitHub keeps forks of public repositories public. Create a
new empty private repository instead.

`mt sync` and the installer check origin before pushing:

- If origin is the microtask app repository (the `upstream` remote or
  `github.com/pablormier/microtask`), they always refuse.
- If origin is on GitHub and the [GitHub CLI](https://cli.github.com) (`gh`) is
  installed and signed in, they ask GitHub for its visibility and refuse a
  public repository. Without `gh` this check is skipped, so confirm the
  visibility yourself. To publish cards on purpose, set
  `rules.allow_public_origin: true` in `mt-policy.yaml`.

## Update

```sh
mt update --check     # preview changed app files
mt update             # install them
```

The update replaces app files with those from `upstream/main` (including
deletions) and keeps `cards/`, `drafts/`, `.mt/`, and both policy files. It
checks that the new version starts and the workspace validates before
committing locally; if a check fails, it restores the previous version. It
needs a clean working tree (draft changes are set aside and restored) and does
not push. Run `mt sync` afterwards to share it with your other machines.

Any tracked file outside those workspace paths belongs to the app and is
replaced on update, so keep your own working files in `.mt/`.

## Upgrade notes

### `area` became `topic`

In each existing workspace, stop dashboards and automatic sync, then run the
one-time rename. It also renames `suggested_areas` in `mt-policy.yaml`:

```sh
python3 scripts/migrate_area_to_topic.py --plan
python3 scripts/migrate_area_to_topic.py --apply
mt validate
```
