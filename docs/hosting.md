# Hosting dashboards on a server

This sets up microtask on a Linux server for people who will not install it.
Each person gets a private workspace and their own web address, and signs in
with their Google account.

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
/srv/mt/alice/            alice's workspace folder: cards, settings, Git
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

Snapshot `/srv/mt` and the files under `/etc` on a schedule. The cards are plain
files, so any file backup works. For example, with restic:

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

## What this kit does not cover

- Docker or other container setups.
- Several users sharing one workspace, or any team features.
- Multi-tenant hosting in one process. Each user runs a separate dashboard.
- Servers other than one Linux machine, and identity providers other than the
  one you configure in oauth2-proxy.
- Syncing a hosted workspace to a Git remote. You can add one by hand, but it is
  not part of this setup.
