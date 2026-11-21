# Hosting dashboards on a server

This sets up microtask on a Linux server for people who will not install it.
Each person gets a private workspace and their own web address, and signs in
with their Google account. To serve one workspace to a team instead, see
[Shared workspace](#shared-workspace).

To reach your own dashboard from your phone, [Tailscale](mobile.md#mt-web-over-tailscale)
is simpler: no domain, proxy or certificates.

The scripts have been tested with `--root` and `--no-systemctl`, not yet on a
real server with Caddy, oauth2-proxy and systemd. Before adding people, check
the configuration and try two sign-ins: one as the owner, and one as another
account that should get 403.

```sh
caddy validate --config /etc/caddy/Caddyfile
systemd-analyze verify /etc/systemd/system/mt-web@.service
```

## How it works

```
browser --> Caddy (HTTPS, one site per user) --> mt web on 127.0.0.1:810x
               |
               +--> oauth2-proxy on 127.0.0.1:4180 (Google sign-in)
```

The app and the data are kept apart:

```
/opt/microtask/v0.2.0/    one read-only checkout per release, owned by root
/opt/microtask/v0.2.1/
/opt/microtask/current    link to the active version
/opt/microtask/previous   link to the version before it
/srv/mt/alice/            alice's workspace folder: cards, settings, local history (Git optional)
/srv/mt/bob/
```

- Every user runs the same app from `/opt/microtask/current`. A workspace in
  `/srv/mt/NAME` holds cards, `settings.yaml`, Git and `.mt/` state, and no app
  files. The service `mt-web@NAME` runs
  `/opt/microtask/current/mt -R /srv/mt/NAME web` on a local port.
- The service sees an empty `/srv/mt` with only its own workspace inside, so a
  dashboard cannot read another user's cards. `/opt/microtask` is read-only to
  the service.
- Caddy gets a certificate for each user's name and asks oauth2-proxy who the
  visitor is. Only the owner's email reaches the dashboard. Anyone else gets 403.
- Caddy removes any `X-Auth-Request-*` headers the client sends, so they cannot
  be forged.
- The dashboard's `X-Microtask-Token` protects against cross-site requests. The
  proxy provides the sign-in.
- The dashboard accepts the public name in the Host header because the service
  passes `--allow-host`.

## Prerequisites

- A Linux server with systemd, Python 3.9 or newer, git, Caddy 2 and
  oauth2-proxy installed.
- A domain you control. Create DNS A (and AAAA) records for `auth.mt.example.com`
  and for each user name, such as `alice.mt.example.com`. Each name gets its own
  certificate through the normal HTTP challenge, so ports 80 and 443 must be
  open to the internet.
- An OAuth app with your identity provider (the example uses Google). Its
  redirect URI is `https://auth.mt.example.com/oauth2/callback`.
- A local disk for `/srv/mt`. Do not use NFS or SMB, because microtask relies on
  file locks.

## Set up the server

Replace `mt.example.com` with your domain in every file below.

1. Create the service account and the folders.

   ```sh
   sudo useradd --system --home-dir /srv/mt --shell /usr/sbin/nologin mt
   sudo mkdir -p /srv/mt /etc/mt /etc/caddy/mt-users /etc/oauth2-proxy
   ```

2. Install the first version. Pick a release tag, such as `v0.2.0`. There is no
   app on the server yet, so run the script from any checkout of the
   repository. This one can be deleted afterwards.

   ```sh
   git clone --branch v0.2.0 https://github.com/pablormier/microtask.git /tmp/microtask-kit
   sudo /tmp/microtask-kit/deploy/upgrade.sh v0.2.0
   ```

   The script clones the tag into `/opt/microtask/v0.2.0`, makes it read-only
   and points `/opt/microtask/current` at it. With no workspaces yet there is
   nothing to check. From now on use the scripts in
   `/opt/microtask/current/deploy/`.

3. Install the service template.

   ```sh
   sudo cp /opt/microtask/current/deploy/systemd/mt-web@.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

4. Configure oauth2-proxy. Copy `deploy/oauth2-proxy/oauth2-proxy.cfg.example` to
   `/etc/oauth2-proxy/oauth2-proxy.cfg`, fill in the client id, client secret and
   a new cookie secret (the file shows the command), and run oauth2-proxy as a
   service with `--config /etc/oauth2-proxy/oauth2-proxy.cfg`. Keep the file
   readable only by the account that runs it.

5. Install the Caddyfile. Copy `deploy/caddy/Caddyfile` to `/etc/caddy/Caddyfile`
   and edit the domain. `add-user.sh` reads `deploy/caddy/user.caddy.template`
   from its own checkout.

   ```sh
   sudo systemctl reload caddy
   ```

## Add a user

Run this as root:

```sh
sudo /opt/microtask/current/deploy/add-user.sh alice alice@example.com alice.mt.example.com
```

The script checks the name, refuses to overwrite an existing user, creates the
workspace `/srv/mt/alice` with `mt init --data`, picks the next free port from
8101, writes `/etc/mt/alice.env` and `/etc/caddy/mt-users/alice.caddy`, adds the
email to `/etc/oauth2-proxy/emails.txt`, starts `mt-web@alice` and reloads Caddy.
It needs no network.

Then open `https://alice.mt.example.com`. The first visit asks for a Google
sign-in. oauth2-proxy may need a restart to read a new line in the emails file.

Useful options: `--dry-run` prints the plan and writes nothing. `--root DIR`
puts every path under `DIR`, and `--no-systemctl` skips the chown, systemd and
Caddy steps. These two are for trying the script on another machine. `--app DIR`
(or `MT_APP_DIR`) sets which version creates the workspace. The default is
`/opt/microtask/current`.

A new workspace has a Git repository with one commit, no `origin` remote, and
`auto_sync` off. Nothing in it syncs anywhere.

## Upgrading

Upgrade every user at once to a release tag:

```sh
sudo /opt/microtask/current/deploy/upgrade.sh v0.2.1
```

Upgrade one minor version at a time, taking the newest patch of each, because a
minor version can carry migrations that the next one removes. `upgrade.sh`
refuses a TAG that skips a minor version after the current one. Pass
`--allow-skip` only if you know the versions in between need nothing from you.

### From 0.1 to 0.2.0

In 0.2.0 the settings file is called `settings.yaml` instead of
`mt-policy.yaml`, and 0.2.0 refuses a workspace that still has the old name.
The 0.1 version on the server cannot rename it, so use a copy of 0.2.0 for that
step, and stop the dashboards while you do it:

```sh
git clone --branch v0.2.0 https://github.com/pablormier/microtask.git /tmp/microtask-0.2.0
sudo systemctl stop 'mt-web@*'
sudo -u mt /tmp/microtask-0.2.0/mt -R /srv/mt/NAME migrate     # for each NAME in /srv/mt
sudo /tmp/microtask-0.2.0/deploy/upgrade.sh v0.2.0
```

`mt migrate` renames the settings files and, in a Git repository, commits only
the rename. `upgrade.sh` then checks every workspace with 0.2.0, switches
`current`, and starts the dashboards again. Delete `/tmp/microtask-0.2.0`
afterwards.

Each installed version folder gets a `.mt-hosted` file. It makes `mt update`
point to `upgrade.sh`, and makes `mt` without a workspace point to `/srv/mt`
(select one with `-R /srv/mt/NAME` or `MT_WORKSPACE`).

The script does this in order:

1. Clones the tag into `/opt/microtask/v0.2.1`, compiles the Python files and
   makes the folder read-only for everyone except root. It builds the folder in
   a temporary directory and renames it into place, so a failed clone leaves
   nothing behind.
2. Runs `mt -R /srv/mt/NAME validate` with the new version for every workspace.
   It runs as the `mt` user with `runuser` (or `sudo -u` where `runuser` is
   missing). If any workspace fails, the script names it and stops. `current`
   stays where it was and no service restarts. The new folder stays in place.
3. Points `previous` at the old version, then replaces `current` with a link to
   the new one in a single atomic rename.
4. Restarts every enabled `mt-web@NAME` service and prints the active version.

Running it again for the current version does nothing and exits 0. It refuses
a tag whose folder already exists but is not current.

Dashboards are down for a moment while they restart. Cards are not touched by
an upgrade. If a release changes the service template, compare it with the
installed copy, then copy it and reload systemd:

```sh
diff /opt/microtask/current/deploy/systemd/mt-web@.service /etc/systemd/system/mt-web@.service
sudo cp /opt/microtask/current/deploy/systemd/mt-web@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart 'mt-web@*'
```

## Rolling back

```sh
sudo /opt/microtask/current/deploy/upgrade.sh --rollback
```

This swaps `current` and `previous`, each with an atomic rename, and restarts
the services.
Running it twice returns to where you started. It refuses when there is no
`previous`. It does not validate the workspaces, so use it when the new version
misbehaves.

## Removing old versions

Version folders are kept. When you no longer need one, remove it by hand. Never
remove the versions that `current` and `previous` point to.

```sh
readlink /opt/microtask/current /opt/microtask/previous
sudo rm -rf /opt/microtask/v0.2.0
```

## Backups

Snapshot `/srv/mt` and the files under `/etc` on a schedule. Cards are plain
files, while each workspace's local history lives in `.mt/history.sqlite`. For
a consistent backup of cards and history together, take a filesystem snapshot
(such as ZFS, Btrfs, or LVM) or stop the `mt-web@*` / `mt-shared@*` services
first. To back up a live history database on its own without stopping writers,
use SQLite's online backup command (`sqlite3 /srv/mt/NAME/.mt/history.sqlite
".backup /path/to/history.sqlite"`). For example, with restic:

```sh
restic -r /path/to/repo backup /srv/mt /etc/mt /etc/caddy/mt-users /etc/oauth2-proxy
```

You do not need to back up `/opt/microtask`. Recreate it by running
`upgrade.sh TAG` for the version you were on. Keep the backup repository on a
different machine or provider. Restore by putting the files back, running
`upgrade.sh` for the tag, and running `systemctl restart mt-web@NAME`.

## Removing a user

```sh
sudo systemctl disable --now mt-web@alice
sudo rm /etc/mt/alice.env /etc/caddy/mt-users/alice.caddy
sudo systemctl reload caddy
```

Remove the email from `/etc/oauth2-proxy/emails.txt`. Delete `/srv/mt/alice` only
after you have a backup of it. Upgrades check every folder in `/srv/mt`, so move
or delete a removed user's workspace to keep it out of them.

## Shared workspace

A team of up to about a hundred people, few of them active at once, can share
one workspace served by one `mt web` process. Everyone signs in with their own
account and sees the same cards. The storage does not change: Markdown files,
file locks, a local SQLite history log, and optional Git for `mt sync`.

```
browser --> Caddy (HTTPS, email allowlist, adds the secret) --> mt web on 127.0.0.1:810x
               |
               +--> oauth2-proxy on 127.0.0.1:4180 (sign-in)
```

Single-user setups are not affected. Everything here applies only when
`mt web` starts with `--trust-proxy-user`.

### Who is asking

The dashboard acts for the person named in the `X-Auth-Request-Email` header.
A program on the same machine can send any header to a local port, so the
header counts only on a request that also carries `X-Microtask-Proxy-Secret`
with the right value. Caddy removes both headers from the browser's request and
adds them itself after oauth2-proxy has signed the visitor in. The dashboard
answers 403 to a request without the secret, and 401 or 403 to one without
exactly one email, for reads as well as writes.

The user name of an address is its line in the user map when it has one,
and otherwise the part before the `@` in lowercase, such as `alice` for
`Alice@example.org`. It becomes part of file names, so it may contain only
lowercase letters, digits, `.`, `_` and `-`, must start with a letter or
digit, and may be up to 64 characters long. Two addresses with the same user
name are the same person to the dashboard, so `add-shared.sh` refuses them,
whether the names come from the map or from the part before the `@`. It asks
the installed app for each name and for the `MT_ADMINS` list, so it resolves
them the way the dashboard does, and uses them for the administrator checks and
the retired list. Lines of a map file may end in `\n` or `\r\n`. To give an address another name,
add a line `EMAIL USER` to a map file. The dashboard reads the file named by
`--user-map FILE` or `MT_USER_MAP`, and `add-shared.sh` reads the file named
by `MT_USER_MAP` in `/etc/mt/NAME.env`, so set it there and restart
`mt-shared@NAME`. When `MT_USER_MAP` is set, `add-shared.sh` checks the file
before any change, dry runs included. It stops if the file is missing,
unreadable or invalid, because the dashboard would refuse the same file.
`MT_ADMINS` may separate names with commas, spaces or both.

The secret and the administrators are given at startup, not in `settings.yaml`,
because settings are workspace data that people can edit in the dashboard:

| Option | Environment variable | Meaning |
| --- | --- | --- |
| `--trust-proxy-user` | | turn on the shared mode |
| `--proxy-secret-file FILE` | `MT_PROXY_SECRET_FILE` | the secret, at least 16 characters |
| `--admin USER` (repeatable) | `MT_ADMINS` (comma-separated) | administrators |
| `--user-map FILE` | `MT_USER_MAP` | email to user name lines |
| `--sign-out-url URL` | `MT_SIGN_OUT_URL` | optional Sign out link (relative `/...` or `https://...`) |

In shared mode the toolbar shows an account menu with the signed-in user name,
email address, and an `Admin` badge for administrators. When `--sign-out-url` or
`MT_SIGN_OUT_URL` is set, the menu includes a **Sign out** link; `add-shared.sh`
sets `MT_SIGN_OUT_URL=/oauth2/sign_out`. When it is unset (for example behind
HTTP Basic Auth or a VPN), the menu shows the signed-in identity without a sign-out link.

### What is per person

- **Focus.** Each person has their own selected card, stored in
  `.mt/focus-users/USER`. The single-user file `.mt/focus` is not used. Focus
  offers only tasks that nobody else has claimed.
- **Drafts.** A draft has an `owner`, set to the person who first saved it. A
  person lists, saves, discards and finishes only their own drafts. Another
  person's draft ID gets a 404. Drafts that existed before the switch have no
  owner and are hidden from everyone until an administrator assigns them with
  `mt -R /srv/mt/NAME draft assign DRAFT-ID USER`. An older microtask that
  saves a draft drops its `owner`.
- **Comments.** Comments written in the dashboard have a `by` field with the
  user name, and so do comments from `mt comment --by NAME`. Comments from
  `mt done` and `mt move` are attributed when `--by` is given. Older versions
  of microtask reject a card with a `by` comment, so upgrade every `mt` that
  reads the workspace (servers, laptops, agents) before the first person
  comments in the shared dashboard.
- **Claims and timers.** Dashboard actions run as the signed-in person. Changing
  the properties or body of a task another person has claimed, and starting a
  timer on it, are refused with 409.
- **Tasks in progress.** The shared workspace sets `rules.doing_limit_scope:
  per_claimant`. Then `rules.max_doing_tasks` is the number each person may
  have in `doing`, and not the number for the whole workspace. Moving a task
  to `doing` and taking over a claim are both checked, by the CLI, the
  dashboard and `mt validate` alike. Tasks in `doing` that nobody has claimed
  count together as one more group with the same limit. The default,
  `workspace`, counts every task in `doing` together, as before.

### Administrators

These dashboard actions exist only in the shared mode and need a user listed
as an administrator. Anyone else gets 403 with the message "Only a workspace
administrator can do this.".

- `POST /api/delete` deletes a card.
- `POST /api/takeover` takes over a claim from another person.
- `POST /api/topic-rename` renames or merges a topic.

The dashboard has no controls for them yet. Administrators also work with the
`mt` command on the server, which is not restricted by this list.
`settings.yaml` has no web editor, so changing settings needs shell access to
the workspace.

### History

A workspace keeps its local audit and revision history in `.mt/history.sqlite`
(unless `rules.local_history` is `false` in `settings.yaml`). Git is optional
and is used only for `mt sync`. Every card mutation (from the dashboard or the
CLI) records the actor, action, source, and compressed before/after card bytes
under the same workspace lock hold. Attachment uploads record metadata without
storing binary blobs in SQLite.

If a non-destructive history write fails, only the person who made that change
sees a warning for that single response (shown alongside any sync warning), and
the card write is preserved. A delete, takeover, or topic rename requires a
pre-mutation snapshot in `.mt/history.sqlite` before any card file is touched.
If that snapshot fails, the dashboard answers `503`, changes nothing, and says
so. Check that the service account can write to `/srv/mt/NAME/.mt/history.sqlite`,
that the disk is not full, and that permissions on `.mt/` are intact, then try
again.

To inspect history or restore a deleted or overwritten card:

```sh
/opt/microtask/current/mt -R /srv/mt/NAME history --deleted
/opt/microtask/current/mt -R /srv/mt/NAME history show EVENT_ID --diff
/opt/microtask/current/mt -R /srv/mt/NAME restore EVENT_ID
```

### Conflicting edits

Saving a card body or its properties sends the hash of the version the person
loaded. If someone else changed the card in the meantime, the answer is 409 and
nothing is written. The body editor keeps its text in the browser and offers to
reload the card and keep it. The properties editor keeps the person's changes in
the open form and says that the card changed. Unsaved edits that the browser
keeps are stored per signed-in person, so two people who use the same browser
do not see each other's text.

### When the signed-in account changes

A page stays open while the proxy session behind it can change, for example
when someone signs out of one account and into another in a second tab. Each
page carries a write token that belongs to the person it was loaded for. The
dashboard accepts a write only with the token of the person the proxy names for
that request, and otherwise answers 409 with `member_changed` before anything is
read or changed, for card edits, draft saves and attachment uploads alike.

When the page sees this answer, or a refresh that belongs to another person, it
saves unsaved property and body edits and an open capture draft in the browser
under the previous person's key, sends nothing more for them, and reloads. The
reloaded page shows a notice. The previous person finds their capture draft
reopened, with a short note, the next time their page loads in that browser.

### Set it up

Do steps 1 to 5 of [Set up the server](#set-up-the-server) first. Then install
the unit for shared workspaces and create the workspace:

```sh
sudo cp /opt/microtask/current/deploy/systemd/mt-shared@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo /opt/microtask/current/deploy/add-shared.sh --admin alice team team.mt.example.com \
  alice@example.com bob@example.com carol@example.com
```

`add-shared.sh` takes a workspace name, a public host name and the allowed email
addresses (more with `--emails-file FILE`). It creates `/srv/mt/team` with
`mt init --data`, sets `rules.doing_limit_scope: per_claimant` with the
indentation of the other entries under `rules:`, checks with `mt validate` and
`mt settings --json` that the workspace accepts it, and commits that,
writes `/etc/mt/team.env` (port, host, administrators and the path of the
secret) and a new random secret in `/etc/mt/team.secret` (mode 600), writes
`/etc/caddy/mt-users/team.caddy`, adds the addresses to the oauth2-proxy emails
file, starts `mt-shared@team` and reloads Caddy. `--dry-run`, `--root`,
`--no-systemctl` and `--app` work as in `add-user.sh`. `upgrade.sh` restarts
`mt-shared@NAME` services too and validates the workspace with the new version.

The Caddy file holds a copy of the secret and is readable by root and the
`caddy` group. The allowlist is the `header_regexp` line in that file. Anyone
who signs in with an address outside the list gets 403 from Caddy. The
dashboard has no list of its own and accepts whoever Caddy lets through, so
Caddy's line is the one that decides.

### Add or remove people

The members are listed in `/etc/mt/NAME.members` (mode 600), one address per
line. Change them with the same script:

```sh
sudo /opt/microtask/current/deploy/add-shared.sh --add team carol@example.com dave@example.com
sudo /opt/microtask/current/deploy/add-shared.sh --remove team bob@example.com
```

Both modes apply the checks of the creation run: valid addresses, and a different
part before the `@` for each person. They rewrite the members file and the Caddy
site from the template, keeping the workspace's secret and port, update the
oauth2-proxy emails file and reload Caddy. The service does not restart.
`--dry-run`, `--root` and `--no-systemctl` work as before, and a failed update
puts the previous files back. Edits you made by hand to the Caddy site are
replaced by the template's version. The scripts take a lock before they check or
change anything, so two runs at once do not overwrite each other.
`add-user.sh` takes the same lock and refuses a name that `add-shared.sh` has
already created, and the other way round. The Caddy
site, the secret and the members list are created with mode 640, 600 and 600
before any secret is written, whatever the umask of the shell.

When someone is removed, their address leaves the oauth2-proxy emails file only if
no other members list or Caddy site still names it. Removing an administrator is
refused until `MT_ADMINS` in `/etc/mt/NAME.env` no longer lists them; edit that
line, restart `mt-shared@NAME`, and run the command again. The cards and comments
a removed person wrote stay in the workspace. oauth2-proxy may need a restart to
read a changed emails file.

A user name identifies a person's private data: their drafts, claims, focus
and timers. Removing someone therefore retires their user name. It is recorded
in `/etc/mt/NAME.retired` (mode 600), one line `USER EMAIL` per removed person.
`--add` refuses a different address that would get a retired name, for example
`alice@b.org` after `alice@a.org` was removed, because that person would
inherit the old drafts and claims. Two ways forward:

- Give the new person another name. Put a line `alice@b.org alice2` in a user
  map file, and set `MT_USER_MAP=/etc/mt/NAME.users` in `/etc/mt/NAME.env`
  (restart `mt-shared@NAME`), as described under
  [Who is asking](#who-is-asking). Add the map line before `--add`.
- Run `--add --reuse-name NAME alice@b.org` after confirming that it is the
  same person. The name is then free again.

Adding back the exact address that was removed needs no flag, since it is the
same account. Either way the name leaves the retired list once someone is
admitted under it.

A removed person's drafts stay in the workspace and no one else can see them.
To hand one over, run `mt -R /srv/mt/NAME draft assign DRAFT-ID USER`. Their
claims on tasks also stay. An administrator takes one over with
`POST /api/takeover` in the dashboard API, or on the server with
`mt -R /srv/mt/NAME move TASK doing --by USER --takeover`, which is not
restricted by the administrator list.

To rotate the secret, write a new one to `/etc/mt/team.secret`, put the same
value in the `request_header X-Microtask-Proxy-Secret` line of the Caddy file,
restart `mt-shared@team` and reload Caddy.

Try it before announcing it: sign in as an allowed account and as one that is
not listed, and check that `curl -H 'X-Auth-Request-Email: alice@example.com'
http://127.0.0.1:8101/api/items` on the server answers 403.

## What this kit does not cover

- Production container hosting. For local shared-workspace testing, see the
  [Docker Compose setup](../deploy/docker/README.md).
- Several workspaces in one process, or per-person permissions beyond the
  administrator list. Everyone on the allowlist can read and edit every card
  of a shared workspace.
- Dashboard controls for the administrator actions, bulk edits in the
  dashboard, and a settings editor.
- Servers other than one Linux machine, and identity providers other than the
  one you configure in oauth2-proxy.
- Syncing a hosted workspace to a Git remote. You can add one by hand, but it is
  not part of this setup.
