# Hosting kit

Files for hosting microtask dashboards on a Linux server, one per person. The full
guide is in [docs/hosting.md](../docs/hosting.md).

- `systemd/mt-web@.service`: one dashboard per user, run as the system account `mt-NAME`
  and started with `--require-proxy-secret`; its env file also sets the Sign out link
- `caddy/Caddyfile` and `caddy/user.caddy.template`: HTTPS and per-user sites
- `oauth2-proxy/oauth2-proxy.cfg.example`: sign-in
- `systemd/mt-shared@.service`: one dashboard for a workspace shared by a group of people,
  also run as `mt-NAME`
- `caddy/shared.caddy.template`: the site of a shared workspace, with an email allowlist
- `add-user.sh`: creates a user's account `mt-NAME`, workspace folder, port, secret, service and site
- `add-shared.sh`: creates a shared workspace, its account `mt-NAME`, service and site, and the secret that
  lets Caddy tell the dashboard who is signed in; `--add` and `--remove` change who may use it
- `upgrade.sh`: installs a release tag in `/opt/microtask`, checks every
  workspace with it, switches `current` and restarts the services; `--rollback`
  goes back to the previous version
- [`docker/`](docker/README.md): local Compose stack and smoke test for a shared workspace
