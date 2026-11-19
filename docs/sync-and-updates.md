# Install, sync, and updates

The app and your data are separate folders:

```
  your private repo (origin)  <── mt sync ──>  workspace/  (cards, drafts, settings)
  microtask releases          ── mt update ──>  the app folder (code only)
```

`mt sync` moves your data to and from **origin**. `mt update` replaces the app
files with the latest release and never touches your data.

## First install

```sh
curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash
```

The installer asks four questions and each has a default:

```
Install microtask in [~/microtask]:
Workspace folder [~/microtask/workspace]:
Private git repo to sync cards (optional, Enter to skip):
Add mt to ~/.zshrc? [Y/n]:
```

- It downloads the latest release and needs Python 3 and, only for a
  repository, Git. Without a terminal it uses the defaults.
- A workspace folder outside the app folder is recorded as `MT_WORKSPACE` in
  your shell config. The PATH and `MT_WORKSPACE` lines are added once, however
  often you run the installer.
- If the repository is **empty**, the new workspace is pushed to it. If it
  **already holds your workspace**, it is cloned into the workspace folder.
- The shell config is `~/.zshrc` for zsh, `~/.bash_profile` for bash on macOS,
  and `~/.bashrc` for bash on Linux. For another shell the installer prints
  the lines to add.
- If your shell config already puts another copy of `mt` on PATH, the installer
  lists it and says which one new terminals will run: the last PATH line wins.
  Check with `command -v mt` or `mt info`.

`install.sh --help` lists the options for scripts.

## Without the installer

```sh
git clone https://github.com/pablormier/microtask.git && cd microtask && ./mt init
```

`./mt init` creates `workspace/` next to the app, with `cards/`, `drafts/`,
`settings.yaml` and a `.gitignore`. Pasted and attached files go into `attachments/`
there, which `mt sync` commits with the cards. The repository ignores `workspace/`, so
your cards never end up in the app's history. Run `./install.sh --here` in the
clone to put `mt` on PATH.

## Another machine

Run the installer again and give it the same private repository. It clones
your workspace into the new machine. `settings.local.yaml`, `.mt/`, and
repository aliases (`mt repo add`) are per machine, so recreate them if you
use them.

## Sync

```sh
mt sync
```

This commits changes to cards, drafts, attachments, and `settings.yaml` in the workspace,
then fetches, merges, and pushes to `origin`. Set `rules.auto_sync: true` in
`settings.yaml` to sync after every card change. Everything works locally
without Git; sync is optional. If a sync fails, including one run in the
background, `mt info` shows the time and the error under "LAST SYNC FAILED"
until the next sync succeeds. To connect a repository later, make the
workspace a Git repository if it is not one yet, then add origin:

```sh
mt run git init
mt run git remote add origin git@github.com:YOU/tasks.git
```

Your cards are personal, so origin must be a private repository. Do not fork
microtask for this: GitHub keeps forks of public repositories public. Create a
new empty private repository instead.

`mt sync` and the installer check origin before pushing:

- If origin is the microtask app repository (`github.com/pablormier/microtask`),
  they always refuse.
- If origin is on GitHub and the [GitHub CLI](https://cli.github.com) (`gh`) is
  installed and signed in, they ask GitHub for its visibility and refuse a
  public repository. Without `gh` this check is skipped, so confirm the
  visibility yourself. To publish cards on purpose, set
  `rules.allow_public_origin: true` in `settings.yaml`.

## Update

```sh
mt update --check     # current, next and latest version, and what would change
mt update             # install the next release
```

Before 1.0, a minor version (0.1 to 0.2) can break things and carry migration
code that the following minor version removes. So `mt update` moves one minor
version at a time. If a newer patch of your minor version exists, it installs
that. Otherwise it installs the newest patch of the next minor version, and
tells you to run `mt update` again to continue. If the new version asks you to
run `mt migrate`, do that before the next `mt update`. Migrations run between
steps, never skipped. `mt update` refuses to move to a new minor version while
the workspace still uses `mt-policy.yaml`; run `mt migrate` first. Patch
updates are always allowed. A fresh install (`install.sh`) gets the latest
release directly, because a new workspace has nothing to migrate. If the
installed version is unknown (no `VERSION` file), `mt update` goes to the
latest release and says so.

`mt update` lists the releases from the GitHub releases list (drafts,
prereleases, and tags that are not `vMAJOR.MINOR.PATCH` are ignored), downloads
the tarball of the release it picked, and replaces the app folder's
top-level entries one by one. New entries are added, and entries that came with
the previous release but are gone from the new one are removed. The folder
itself is never renamed. It then runs the new `mt validate` and prints the old
and new version. If GitHub limits the requests, wait a while, or choose a
release yourself with `MT_RELEASE_TAG=vX.Y.Z`, which installs exactly that tag.
`MT_RELEASES_URL` replaces the releases list, which is useful for tests. The
installer and `mt update` write two small files: `VERSION`
holds the installed version (a folder without it reports `unknown`), and
`.release-files` lists the top-level entries that release brought.

`mt update --check --json` reports `current_version`, `target_version` (the
step, or null when up to date), `latest_version` (the newest release),
`steps_remaining`, and `up_to_date`, plus the file changes.

The workspace is not touched, whether it is `workspace/` or the folder
`MT_WORKSPACE` names. A top-level file or folder you added to the app folder
yourself, such as `.env`, is not in `.release-files`, so it stays. `mt update
--check` lists those entries as kept. Files you add inside a folder that comes
with the release, such as `docs/`, are replaced along with that folder. The
exception is `deploy/`, the [hosting kit](hosting.md): `mt update` never adds,
changes or removes it.

A Git clone of the app is updated with `git pull`, and `mt update` refuses to
run there. It also refuses in a folder that still keeps cards next to the app
code; see below.

## Moving an old install

Earlier versions kept the app and the cards in one folder, usually a Git clone
with your private repository as `origin`. Version 0.2.0 still works in such a
folder, reading its `mt-policy.yaml`, and `mt validate` reminds you to run
`mt migrate`. Run this in that folder:

```sh
mt migrate --check     # show what would move and what the commit contains
mt migrate
```

`mt migrate` moves `.git`, `cards/`, `drafts/`, `attachments/`, `.mt/` and the settings files
into `workspace/`, renaming `mt-policy.yaml` to `settings.yaml` and
`mt-policy.local.yaml` to `settings.local.yaml` on the way. It adds `.mt/` and
`settings.local.yaml` to the workspace's `.gitignore`, and makes one commit
there, "Move microtask app out of the workspace", that deletes the app files
from your repository and records the rename. Nothing is pushed. It refuses when `workspace/` exists,
when the tree has uncommitted changes (changes in `drafts/` are fine), or when
`origin` is the microtask app repository.

A remote named `upstream` that points at the microtask app repository is
removed, because pulling from it would merge app files into your cards. Stop
`mt web` before migrating; a dashboard started earlier keeps the old paths.

After that the app folder has no `.git`, so `mt update` works there.

A workspace folder with no app, such as one made by `mt init DIRECTORY --data`
or a server workspace in `/srv/mt/NAME`, can also still use the old file name.
`mt` stops with a message to run `mt migrate` for it. Select the folder with
`-R` or `MT_WORKSPACE`:

```sh
mt -R /srv/mt/NAME migrate --check
mt -R /srv/mt/NAME migrate
```

This renames `mt-policy.yaml` to `settings.yaml` (and `mt-policy.local.yaml` to
`settings.local.yaml`) and, if the folder is a Git repository, commits just the
rename as "Rename mt-policy.yaml to settings.yaml". It refuses when the Git tree
has uncommitted changes. Nothing is pushed.

Once the commit is pushed, for example by `mt sync`, other machines still on
the old layout will lose their app files on their next pull. On each other
machine, run the installer or `mt migrate` before syncing. The commit also renames
`mt-policy.yaml` to `settings.yaml`, which older versions do not read, so every machine
must be on 0.2.0 or later before it syncs.

If another machine already pulled that commit, its app files are gone and the
old folder now holds only your cards and settings, which is a valid workspace
folder. Run the installer and enter that folder at the "Workspace folder"
prompt (or pass `--workspace FOLDER`) to get it working again.
