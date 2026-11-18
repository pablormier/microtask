# Hosting for a few people

This guide sets up microtask for people who will not install anything. Each
person gets a private workspace and a web address, and signs in with their
Google account. It suits a handful of users on one small server.

For one person who wants the dashboard on a phone, [Tailscale](mobile.md) is
simpler. Run `mt web` on your machine and open it over your tailnet. You need
no domain, proxy or certificates.

**Untested.** The scripts and tests in this repository were run on macOS only.
Nothing here was run against a real Caddy, oauth2-proxy or systemd. The
`add-user.sh` script and the update exclusion are covered by tests. The Caddy
syntax, the oauth2-proxy options and the systemd directives were written from
memory. They have not been checked against the documentation or run. Before the
first real user, run these:

```sh
caddy validate --config /etc/caddy/Caddyfile
systemd-analyze verify /etc/systemd/system/mt-web@.service
```

Then try one sign-in as the owner, and one as a second account that should get
403. Fix whatever fails before you add more users.

## How it works

```
browser --> Caddy (HTTPS, one site per user) --> mt web on 127.0.0.1:810x
               |
               +--> oauth2-proxy on 127.0.0.1:4180 (Google sign-in)
```

- Each user has a workspace at `/srv/mt/NAME` and a systemd service
  `mt-web@NAME` that runs that workspace's own `mt web` on a local port.
- The service sees an empty `/srv/mt` with only its own workspace inside, so a
  dashboard cannot read another user's cards.
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

2. Get this repository onto the server, for example in `/opt/microtask-deploy`.
   `add-user.sh` uses its `install.sh` to create each workspace.

   ```sh
   sudo git clone https://github.com/pablormier/microtask.git /opt/microtask-deploy
   ```

3. Install the service template.

   ```sh
   sudo cp /opt/microtask-deploy/deploy/systemd/mt-web@.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

4. Configure oauth2-proxy. Copy `deploy/oauth2-proxy/oauth2-proxy.cfg.example` to
   `/etc/oauth2-proxy/oauth2-proxy.cfg`, fill in the client id, client secret and
   a new cookie secret (the file shows the command), and run oauth2-proxy as a
   service with `--config /etc/oauth2-proxy/oauth2-proxy.cfg`. Keep the file
   readable only by the account that runs it.

5. Install the Caddyfile. Copy `deploy/caddy/Caddyfile` to `/etc/caddy/Caddyfile`
   and edit the domain. The script reads `deploy/caddy/user.caddy.template` from
   the deploy checkout.

   ```sh
   sudo systemctl reload caddy
   ```

## Add a user

Run this as root from the deploy checkout:

```sh
sudo /opt/microtask-deploy/deploy/add-user.sh alice alice@example.com alice.mt.example.com
```

The script checks the name, refuses to overwrite an existing user, creates
`/srv/mt/alice`, picks the next free port from 8101, writes `/etc/mt/alice.env`
and `/etc/caddy/mt-users/alice.caddy`, adds the email to
`/etc/oauth2-proxy/emails.txt`, starts `mt-web@alice` and reloads Caddy.

Then open `https://alice.mt.example.com`. The first visit asks for a Google
sign-in. oauth2-proxy may need a restart to read a new line in the emails file.

Useful options: `--dry-run` prints the plan and writes nothing. `--root DIR`
puts every path under `DIR`, and `--no-systemctl` skips the chown, systemd and
Caddy steps. These two are for trying the script on another machine. `--source`
(or `MT_SOURCE`) sets the git URL the workspace is cloned from.

A workspace created this way has an `upstream` remote and no `origin`, and
`auto_sync` is off in the shipped policy. Nothing in it syncs anywhere.

## Backups

Snapshot `/srv/mt` on a schedule. The cards are plain files, so any file backup
works. For example, with restic:

```sh
restic -r /path/to/repo backup /srv/mt /etc/mt /etc/caddy/mt-users /etc/oauth2-proxy
```

Keep the backup repository on a different machine or provider. Restore by
putting the files back and running `systemctl restart mt-web@NAME`.

## Upgrading

Each workspace holds its own copy of the code. To upgrade a user:

```sh
sudo -u mt /srv/mt/alice/mt update --check
sudo -u mt /srv/mt/alice/mt update
sudo systemctl restart mt-web@alice
```

`mt update` needs network access to GitHub and a clean working tree. It does not
install or change the `deploy/` directory. Update the deploy checkout itself
with `git pull`, and copy a changed service template to
`/etc/systemd/system/` followed by `systemctl daemon-reload`.

## Removing a user

```sh
sudo systemctl disable --now mt-web@alice
sudo rm /etc/mt/alice.env /etc/caddy/mt-users/alice.caddy
sudo systemctl reload caddy
```

Remove the email from `/etc/oauth2-proxy/emails.txt`. Delete `/srv/mt/alice` only
after you have a backup of it.

## What this kit does not cover

- Docker or other container setups.
- Several users sharing one workspace, or any team features.
- Multi-tenant hosting in one process. Each user runs a separate dashboard.
- Servers other than one Linux machine, and identity providers other than the
  one you configure in oauth2-proxy.
- Syncing a hosted workspace to a Git remote. You can add one by hand, but it is
  not part of this setup.
