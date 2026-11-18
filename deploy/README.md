# Hosting kit

Files for running microtask for a few people on one Linux server. The full
guide is in [docs/hosting.md](../docs/hosting.md).

- `systemd/mt-web@.service`: one dashboard per user
- `caddy/Caddyfile` and `caddy/user.caddy.template`: HTTPS and per-user sites
- `oauth2-proxy/oauth2-proxy.cfg.example`: sign-in
- `add-user.sh`: creates a user's data-only workspace, port, service and site
- `upgrade.sh`: installs a release tag in `/opt/microtask`, checks every
  workspace with it, switches `current` and restarts the services; `--rollback`
  goes back to the previous version
