# Local shared workspace with Docker Compose

From the repository root, start the fixture stack with Docker running:

```sh
docker compose -f deploy/docker/compose.yaml up --build -d
```

Open `http://127.0.0.1:8080` and sign in as `alice` (administrator) or `bob`
(member). Both use the password `microtask`. To use both at once, open the
dashboard in two browser profiles. To change the host port, set `MT_TEST_PORT`
before starting the stack.

Caddy uses fixture accounts and sends the authenticated email and proxy secret
to `mt web --trust-proxy-user`. Only Caddy is published, on IPv4 loopback. The
stack is for local development with disposable data. On first startup it seeds
the shared workspace with the same sample cards and attachments as `mt demo`.
The workspace lives in a Docker volume, so edits survive restarts. Dashboard
writes create local Git commits, auto-sync is off, and the in-progress limit
is counted per claimant. Set `MT_TEST_WORKSPACE=team` when starting the stack
to use a separate workspace that starts empty.

To try claims, sign in as Alice, open **Re-run the benchmark baseline**, and
set **Task status** to **In progress**. The card shows **Working: alice**.
Open it as Bob and try saving a description edit. The server rejects the
edit, but Bob can still leave a comment. As Alice, move the task back to
**Todo** to release the claim.

The stack also serves a personal dashboard for Alice at `http://127.0.0.1:8081`,
like the `mt-web@` service of the server kit. Only `alice` can sign in there,
with the same password. It is a single-user dashboard started with
`--require-proxy-secret`, so it answers 403 to any request that does not come
through Caddy with its secret. On first startup it creates an empty workspace.
To change the host port, set `MT_PERSONAL_PORT`. Its **Sign out** link opens
`/logout`, which answers 401 so that the browser asks for the password again
and forgets the stored one. Basic auth has no session to end, so some browsers
keep the password until you close them.

Run the smoke test against the running stack:

```sh
docker compose -f deploy/docker/compose.yaml exec -T shared python3 smoke.py
```

The test checks proxy authentication, private drafts and shared card access,
and that the personal dashboard refuses requests that skip Caddy.
It submits two saves from the same card version and checks for one success and
one `409 Conflict`. Each run leaves one uniquely named fixture card in the
workspace for inspection.

Stop the stack with `docker compose -f deploy/docker/compose.yaml down`.
The workspace volume persists. Add `--volumes` only when you want to delete
the fixture workspace and its history.
