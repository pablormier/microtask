# Install, sync, and updates

The app and your data are separate folders:

```
  your private repo (origin)  <── mt sync ──>  workspace/  (cards, drafts, attachments, settings)
  microtask releases          ── mt update ──>  the app folder (code only)
```

`mt sync` moves your data to and from your private repository. `mt update`
replaces the app files with a newer release and leaves your data alone.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash
```

The installer asks four questions. Enter accepts the default shown:

```
Install microtask in [~/microtask]:
Workspace folder [~/microtask/workspace]:
Private git repo to sync cards (optional, Enter to skip):
Add mt to ~/.zshrc? [Y/n]:
```

- It needs Python 3, and Git only if you give a repository. Without a
  terminal it uses the defaults.
- If the repository is empty, your new workspace is pushed to it. If it
  already holds your workspace, it is cloned into the workspace folder.
- A workspace folder outside the app folder is recorded as `MT_WORKSPACE` in
  your shell config.
- The shell config is `~/.zshrc` for zsh, `~/.bash_profile` for bash on macOS,
  and `~/.bashrc` for bash on Linux. For other shells the installer prints the
  lines to add. Running the installer again adds nothing twice.
- If another `mt` is already on your PATH, the installer says which one new
  terminals will run. Check with `command -v mt` or `mt info`.

`install.sh --help` lists the options for scripts.

### From a clone

```sh
git clone https://github.com/pablormier/microtask.git && cd microtask && ./mt init
```

`./mt init` creates `workspace/` next to the app with `cards/`, `drafts/`,
`settings.yaml` and a `.gitignore`. The clone ignores `workspace/`, so your
cards stay out of the app's history. Run `./install.sh --here` to put this
`mt` on your PATH.

### Another machine

Run the installer and give it the same private repository. `settings.local.yaml`,
`.mt/` and repository aliases (`mt repo add`) belong to one machine, so set
them up again if you use them.

## Sync

```sh
mt sync
```

This commits changes to cards, drafts, attachments and `settings.yaml`, then
fetches, merges and pushes to `origin`. With `rules.auto_sync: true` in
`settings.yaml`, every change syncs on its own, including changes made in the
dashboard. Sync is optional: everything works without Git.

If a sync fails, `mt info` shows the time and the error under "LAST SYNC
FAILED" until the next sync succeeds.

To connect a repository later:

```sh
mt run git init                                    # only if the workspace is not a Git repository yet
mt run git remote add origin git@github.com:YOU/tasks.git
```

Use a new, empty private repository. Do not fork microtask for this, because
GitHub keeps forks of public repositories public. `mt sync` and the installer
refuse to push to the microtask app repository. If the [GitHub
CLI](https://cli.github.com) (`gh`) is installed and signed in, they also
refuse a public repository; without `gh`, check the visibility yourself. To
publish cards on purpose, set `rules.allow_public_origin: true`.

## Update

```sh
mt update --check     # current, next and latest version, and what would change
mt update             # install the next release
```

`mt update` downloads a release from GitHub, replaces the app files, and runs
`mt validate` with the new version. Your workspace is untouched, wherever it
is. A file or folder you added at the top of the app folder yourself, such as
`.env`, stays. Files you add inside a folder that comes with the release, such
as `docs/`, are replaced with it. `deploy/` is never changed.

Before 1.0, a minor version (0.1 to 0.2) can change how data is stored, and the
code that converts old data is removed again in the following minor version.
So `mt update` goes one minor version at a time:

- If your minor version has a newer patch, it installs that.
- Otherwise it installs the newest patch of the next minor version and tells
  you to run `mt update` again.
- If the new version asks you to run `mt migrate`, do that before updating
  again.

The installer always installs the latest release, because a new workspace has
nothing to convert. To install a specific release, set
`MT_RELEASE_TAG=vX.Y.Z`. If GitHub refuses because of too many requests, wait
a while and try again.

In a Git clone of the app, use `git pull`; `mt update` refuses to run there.

## Moving an old install

Before 0.2.0, the app and your cards shared one folder, usually a Git clone
whose `origin` was your private repository. On such an install, `mt update`
still brings in 0.2.0 the old way. Then move your data out of the app:

```sh
mt migrate --check     # show what would move and what the commit contains
mt migrate
```

`mt migrate`:

- moves `.git`, `cards/`, `drafts/`, `attachments/`, `.mt/` and the settings
  files into `workspace/`,
- renames `mt-policy.yaml` to `settings.yaml` (and `mt-policy.local.yaml` to
  `settings.local.yaml`),
- removes a remote named `upstream` that points at the microtask app
  repository,
- makes one commit in `workspace/` that removes the app files from your
  repository. Nothing is pushed.

Stop `mt web` before migrating, because a running dashboard keeps the old
paths. `mt migrate` refuses when `workspace/` already exists or the Git tree
has uncommitted changes outside `drafts/`.

### Your other machines

Once the migration commit is pushed, an old install that pulls it loses its
app files, and 0.1 versions cannot read `settings.yaml`. So before any other
machine syncs, update it to 0.2.0 and run `mt migrate` there, or reinstall it
with the installer and the same repository.

If a machine already pulled the commit, its old folder now holds only your
cards and settings. Run the installer and enter that folder at the "Workspace
folder" prompt (or pass `--workspace FOLDER`).

### Workspace folders without the app

A workspace folder made by `mt init DIRECTORY --data`, or a server workspace in
`/srv/mt/NAME`, only needs the settings file renamed. Select it with `-R`:

```sh
mt -R ~/work/pkg-tasks migrate --check
mt -R ~/work/pkg-tasks migrate
```

This renames the settings files and, in a Git repository, commits only the
rename. Nothing is pushed.
