# Test suite

Tests cover the public deterministic CLI, task and note validation,
transactions and rollback, claims and timers,
Git synchronization, GitHub capture, search and matching, workspace selection,
previews, and the server-backed dashboard.

Card-format tests exercise opaque Markdown bodies, structured activity records,
property/body isolation, exact replacements and stale hashes. Migration tests
cover preserved prose, drafts and running timers, reviewed inventories, backups,
and rollback. UI tests cover separate body/activity rendering and draft recovery.

`test_scoped_validation.py` covers mutation validation: changed cards use the
same per-card validators as `mt validate`, while new IDs, time-session IDs and
increases in tasks in progress trigger the relevant cross-card checks. Ordinary
edits do not copy or validate unrelated cards. The explicit `mt validate`
command remains a full workspace audit.

Keep a test when it protects observable behavior, a data-loss or workspace
boundary, a reproduced defect, or a supported input branch not covered
elsewhere. Prefer one canonical test per behavior and extend existing end-to-
end scenarios when setup is shared. Avoid tests that only assert prose,
terminology, or exact documentation wording.

`test_timer_cards.py` covers running timers kept on their cards: switching between
the CLI and the dashboard, an interrupted switch, a pulled second timer, the upgrade
from `.mt/time-running`, simultaneous starts and the single-write stop.

`test_workspace_resolution.py` covers where cards live (`MT_WORKSPACE`, `workspace/`, an old-style root, first-run creation).
`test_settings_rename.py` covers the `mt settings` command, detection of the old `mt-policy.yaml` name, and the rename done by `mt migrate`.
`test_update.py`, `test_migrate.py` and `test_install.py` use local release tarballs and temporary homes, with no network.

`test_deploy.py` runs `deploy/add-user.sh` and `deploy/upgrade.sh` in a temporary root
with `--no-systemctl` and local release tags (skipped without bash). The scripts run a clone of this checkout with its uncommitted files laid over it. It checks the files
they write, the version links, and that a read-only app runs a data-only workspace.

`tests/support.py` provides shared CLI execution (`run_cli`) and a deterministic
`FakeClock` so time-tracking tests avoid wall-clock sleeps. `test_operations_api.py`
tests core write operations and typed errors directly against a data-only
`Workspace` in-process, while CLI contract and multi-process tests (`test_golden_cli.py`,
`test_publish.py`, `test_update.py`, `test_web.py`) cover process boundaries and locks.

Run the parallel suite (with the 60s time budget) or `unittest discover`:

```sh
python3 tests/run.py
env -u MT_WORKSPACE -u MT_PROJECT -u MT_TOPIC python3 -m unittest discover -s tests -v
```

The parallel runner clears inherited workspace and scope settings so the tests
use their temporary workspaces.

CLI tests start `./mt` without input. With no text, `mt add` and `mt note` read
the card body from stdin whenever it is not a terminal, so a test that inherits
a stdin that never closes (an editor task or an agent's shell) waits forever.
`tests/run.py` and `support.run_cli` give tests an empty stdin. When running
`unittest` directly from such a tool, add `< /dev/null`.
