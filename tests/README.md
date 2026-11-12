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

Run the suite with:

```sh
python3 -m unittest discover -s tests -v
```
