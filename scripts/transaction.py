"""Transactional card writes, file locks, validation, and git sync."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from typing import TextIO
import uuid

import card_format
from card_format import atomic_write, parse_frontmatter, parse_yaml
import git_ops
import store
from store import (
    _ws,
    cards_root,
    current_username,
    task_by_id,
)
import validation
from workspace import Workspace

# mt-policy.yaml is the settings file of old-style installs until 'mt migrate' renames it.
SYNC_PATHS = (":(glob)cards/*.md", ":(glob)drafts/*.md", "attachments", "settings.yaml", "mt-policy.yaml")
_TIME_LOCK_STATE = threading.local()


def _held_locks() -> dict[Path, int]:
    locks = getattr(_TIME_LOCK_STATE, "locks", None)
    if locks is None:
        locks = {}
        _TIME_LOCK_STATE.locks = locks
    return locks


def _lock_key(ws: Workspace | None = None) -> Path:
    return _ws(ws).mutation_lock_path().resolve()


def _is_lock_held(ws: Workspace | None = None) -> bool:
    locks = _held_locks()
    if ws is None:
        return bool(locks)
    return _lock_key(ws) in locks


def _require_mutation_lock(ws: Workspace | None = None) -> None:
    if not _is_lock_held(ws):
        raise RuntimeError("mutation lock must be held before modifying workspace state")



@contextmanager
def time_mutation_lock(ws: Workspace | None = None):
    """Serialize task and timer mutations across CLI and web processes."""
    resolved = _ws(ws)
    lock_path = resolved.mutation_lock_path()
    key = lock_path.resolve()
    locks = _held_locks()
    depth = locks.get(key, 0)
    if depth > 0:
        locks[key] = depth + 1
        try:
            yield
        finally:
            if locks[key] <= 1:
                locks.pop(key, None)
            else:
                locks[key] -= 1
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        locks[key] = 1
        try:
            # Every operation sees timers on cards, so import a pre-upgrade timer file first.
            migrate_legacy_timers(resolved)
            yield
        finally:
            locks.pop(key, None)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)



@contextmanager
def sync_serialization_lock(ws: Workspace | None = None):
    """Serialize Git sync operations without blocking task edits during network waits."""
    lock_path = _ws(ws).sync_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)



def migrate_legacy_timers_if_pending(ws: Workspace | None = None) -> None:
    """Import a pending legacy timer file, taking the lock only when there is one."""
    resolved = _ws(ws)
    if not resolved.legacy_timer_path().exists():
        return
    if _is_lock_held(resolved):
        migrate_legacy_timers(resolved)
        return
    with time_mutation_lock(resolved):
        pass  # Entering the lock imports the file.



def read_time_timer_cards(ws: Workspace | None = None) -> list[tuple[store.Card, dict[str, object]]]:
    """Every card with a running timer, read under the mutation lock."""
    resolved = _ws(ws)
    if not _is_lock_held(resolved):
        with time_mutation_lock(resolved):
            return read_time_timer_cards(resolved)
    return store.running_timer_cards(resolved)



def read_time_timers(ws: Workspace | None = None) -> dict[str, dict[str, object]]:
    """Running timers by task id, read from the cards under the mutation lock."""
    return {store.card_task_id(card): timer for card, timer in read_time_timer_cards(ws)}



def _legacy_timer_problem(task_id: object, timer: object) -> str | None:
    """Why a legacy timer entry cannot be imported, or None."""
    if not isinstance(task_id, str) or not task_id.strip() or not isinstance(timer, dict):
        return "malformed entry"
    # Older entries had no owner; the import fills it in.
    candidate = {"timer": {"by": "unknown", **timer}}
    errors = card_format.validate_timer(candidate)
    return "; ".join(errors) if errors else None



def migrate_legacy_timers(ws: Workspace | None = None) -> None:
    """Import ``.mt/time-running`` onto its cards once, then rename it ``time-running.migrated``.

    Entries whose card is gone (or that cannot be imported) are reported on
    stderr and stay in the renamed file. Safe to repeat: a card that already
    holds a timer is left alone.
    """
    _require_mutation_lock()
    resolved = _ws(ws)
    path = resolved.legacy_timer_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as error:
        print(f"warning: cannot read {path}: {error}; running timers were not imported", file=sys.stderr)
        return
    try:
        legacy = json.loads(raw.decode("utf-8"))
        if not isinstance(legacy, dict):
            raise ValueError("not a mapping")
    except ValueError as error:  # Includes bad UTF-8 and bad JSON.
        print(f"warning: {path} is malformed ({error}); kept as {path.name}.migrated", file=sys.stderr)
        legacy = {}
    base = resolved.root
    for task_id, timer in legacy.items():
        problem = _legacy_timer_problem(task_id, timer)
        task = task_by_id(task_id, ws=resolved) if problem is None else None
        if problem is None and task is None:
            problem = "its task no longer exists"
        if problem is None and task.frontmatter.get("timer") is not None:
            if task.frontmatter["timer"].get("session_id") != timer.get("session_id"):
                problem = "its task already has a different running timer"
            else:
                continue
        if problem is None:
            imported = {"session_id": timer["session_id"], "start": timer["start"],
                        "start_epoch": timer["start_epoch"],
                        "by": timer.get("by") or task.frontmatter.get("claimed_by") or current_username(ws=resolved)}
            relative = str(task.path.relative_to(base))
            try:
                # Render from the snapshot and guard the write with the same bytes.
                properties, body = card_format.parse_document(task.raw)
                properties["timer"] = imported
                updated = card_format.render_document(properties, body)
                apply_item_transaction({relative: updated.encode("utf-8")},
                                       expected={relative: task.raw.encode("utf-8")},
                                       source_root=base, ws=resolved)
            except (OSError, ValueError) as error:
                problem = f"could not write the timer to its card: {error}"
        if problem is not None:
            print(f"warning: running timer for {task_id} was not imported ({problem}); "
                  f"it is kept in {path.name}.migrated", file=sys.stderr)
    target = path.with_name(path.name + ".migrated")
    if target.exists():
        target = path.with_name(f"{path.name}.migrated.{uuid.uuid4().hex[:8]}")
    os.replace(path, target)



def managed_temp_directory(prefix: str, ws: Workspace | None = None) -> tempfile.TemporaryDirectory[str]:
    root = _ws(ws).scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=root)



def run_validator(ws: Workspace | None = None, *, err: TextIO | None = None) -> bool:
    resolved = _ws(ws)
    err_stream = sys.stderr if err is None else err
    try:
        effective_policy = store.policy(resolved)
    except (OSError, ValueError) as error:
        print("FAILED: invalid settings:", file=err_stream)
        for line in str(error).splitlines():
            print(f"  - {line}", file=err_stream)
        return False
    report = validation.audit_workspace(resolved.root, effective_policy)
    if report.errors:
        print(f"FAILED: {len(report.errors)} error(s):", file=err_stream)
        for error in report.errors:
            print(f"  - {error}", file=err_stream)
        return False
    return True



def validate_data_root(data_root: Path, *, changes: dict[str, bytes] | None = None,
                       deletions: set[str] | None = None,
                       previous: dict[str, bytes | None] | None = None,
                       ws: Workspace | None = None) -> None:
    """Validate a full tree, or verify the files just committed."""
    if changes is not None:
        for relative, expected in changes.items():
            if (data_root / relative).read_bytes() != expected:
                raise ValueError(f"item changed during transaction: {relative}")
        for relative in set(deletions or ()) - set(changes):
            if (data_root / relative).exists():
                raise ValueError(f"item reappeared during transaction: {relative}")
        return
    errors = validation.audit(data_root, store.policy(ws))
    if errors:
        detail = "\n".join(
            [f"FAILED: {len(errors)} error(s):", *(f"  - {error}" for error in errors)]
        )
        raise ValueError(detail)



def apply_item_transaction(
    changes: dict[str, bytes],
    *,
    deletions: set[str] | None = None,
    source_root: Path | None = None,
    expected: dict[str, bytes | None] | None = None,
    ws: Workspace | None = None,
) -> None:
    """Validate, atomically apply, and conditionally roll back item-file changes.

    Callers serialize mutations with ``time_mutation_lock``. ``expected`` is a
    compare-and-swap guard: each path must contain its expected bytes, or be
    absent when the value is ``None``.
    """
    resolved = _ws(ws)
    deletions = set(deletions or ())
    base = source_root or resolved.root
    affected = set(changes) | deletions
    original: dict[str, bytes | None] = {}
    for relative in affected:
        path = base / relative
        original[relative] = path.read_bytes() if path.is_file() else None
    if expected is not None:
        for relative, value in expected.items():
            path = base / relative
            current = original[relative] if relative in original else (path.read_bytes() if path.is_file() else None)
            if current != value:
                raise store.StaleCardError(f"item changed before transaction: {relative}")
    warnings = validate_item_changes(changes, deletions=deletions, source_root=base, previous=original, ws=resolved)
    for relative, before in original.items():
        path = base / relative
        current = path.read_bytes() if path.is_file() else None
        if current != before:
            raise store.StaleCardError(f"item changed before transaction: {relative}")
    written: set[str] = set()
    try:
        for relative in deletions:
            path = base / relative
            if path.exists():
                path.unlink()
                written.add(relative)
        for relative, content in changes.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content)
            written.add(relative)
        validate_data_root(base, changes=changes, deletions=deletions, previous=original, ws=resolved)
    except Exception:
        for relative in reversed(sorted(written)):
            path = base / relative
            before = original[relative]
            try:
                current = path.read_bytes() if path.is_file() else None
                expected_after = changes.get(relative, None)
                if relative in deletions and relative not in changes:
                    expected_after = None
                # Do not overwrite an unexpected concurrent edit while rolling back,
                # and avoid rewriting files that already match their pre-transaction bytes.
                if current != expected_after or current == before:
                    continue
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, before)
            except OSError:
                pass
        raise
    for warning in warnings or ():
        print(f"WARNING: {warning}", file=sys.stderr)



def git_setup_message(status: git_ops.WorkspaceGitStatus, operation: str, ws: Workspace | None = None) -> str:
    root = _ws(ws).root
    if status.state == "missing":
        return (
            f"{operation} requires Git; install Git, then run 'git init' in {root} "
            "to initialize this workspace"
        )
    if status.state == "uninitialized":
        return f"{operation} requires this workspace to be a Git repository; run 'git init' in {root}"
    return f"{operation} could not inspect Git: {status.detail}"



def auto_sync_unavailable_message(status: git_ops.WorkspaceGitStatus, ws: Workspace | None = None) -> str:
    root = _ws(ws).root
    if status.state == "missing":
        reason = f"Git is not installed or not on PATH; install Git, then run 'git init' in {root}"
    else:
        reason = f"this workspace is not initialized for Git ({status.detail})"
    return f"Git sync is unavailable: {reason}; changes remain local."



def sync_deferred(ws: Workspace | None = None) -> bool:
    """The caller, such as the dashboard, runs auto-sync itself after this command returns."""
    env = _ws(ws).environ if ws is not None else os.environ
    return env.get("MT_DEFER_SYNC") == "1"



def _run_auto_sync(ws: Workspace | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    resolved = _ws(ws)
    if sync_deferred(resolved):
        return 0
    out_stream = sys.stdout if out is None else out
    err_stream = sys.stderr if err is None else err
    status = git_ops.workspace_status(resolved.root)
    if status.state in {"missing", "uninitialized"}:
        message = auto_sync_unavailable_message(status, resolved)
        record_sync_result(message, resolved)
        print(message, file=out_stream)
        return 0
    print("Auto-syncing task changes...", file=out_stream)
    result = command_sync(argparse.Namespace(message=None), ws=resolved, out=out_stream, err=err_stream)
    if result != 0:
        print(
            "error: local change succeeded, but auto-sync failed; local changes were preserved",
            file=err_stream,
        )
    return result



def run_auto_sync_for_receipt(ws: Workspace | None = None) -> str | None:
    resolved = _ws(ws)
    if sync_deferred(resolved):
        return None
    status = git_ops.workspace_status(resolved.root)
    if status.state in {"missing", "uninitialized"}:
        message = auto_sync_unavailable_message(status, resolved)
        record_sync_result(message, resolved)
        return message
    output = io.StringIO()
    errors = io.StringIO()
    result = _run_auto_sync(resolved, out=output, err=errors)
    if result == 0:
        return None
    detail = (errors.getvalue() + output.getvalue()).strip()
    return detail or "automatic sync failed"



def git_text(*arguments: str, capture_output: bool = True, ws: Workspace | None = None) -> subprocess.CompletedProcess[str]:
    """Compatibility entry point for Git calls in the active workspace."""
    return git_ops.run_git(_ws(ws).root, *arguments, capture_output=capture_output)



def git_task_text(revision: str, path: str, ws: Workspace | None = None) -> str | None:
    result = git_text("show", f"{revision}:{path}", ws=ws)
    return result.stdout if result.returncode == 0 else None



def staged_card(revision: str, path: str, ws: Workspace | None = None) -> tuple[str, str, str]:
    """Title, status and kind of a card as committed (HEAD) or staged (""), tolerating unreadable text."""
    raw = git_task_text(revision, path, ws=ws)
    try:
        frontmatter, _body = parse_frontmatter(raw) if raw else (None, "")
        properties = parse_yaml(frontmatter) if frontmatter is not None else {}
    except card_format.CardFormatError:
        properties = {}
    title = properties.get("title")
    return (title if isinstance(title, str) and title.strip() else Path(path).stem,
            str(properties.get("status") or ""), "note" if properties.get("kind") == "note" else "task")



def staged_task_changes(ws: Workspace | None = None) -> list[tuple[str, str, str | None]]:
    result = git_text(
        "diff",
        "--cached",
        "--name-status",
        "--find-renames",
        "--",
        *SYNC_PATHS,
        ws=ws,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not inspect task changes")
    changes: list[tuple[str, str, str | None]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        code = fields[0][0]
        if code == "R" and len(fields) == 3:
            changes.append((code, fields[1], fields[2]))
        elif code in {"A", "M", "D"} and len(fields) == 2:
            changes.append((code, fields[1], None))
        else:
            raise RuntimeError(f"unsupported task change: {line}")
    return changes



def task_commit_message(changes: list[tuple[str, str, str | None]], ws: Workspace | None = None) -> str:
    markdown_changes = [change for change in changes if Path(change[1]).suffix == ".md"]
    drafts = [change for change in markdown_changes if change[1].startswith("drafts/")]
    cards = [
        change for change in markdown_changes
        if not change[1].startswith(("drafts/", "attachments/"))
    ]
    attachments = [change for change in changes if change[1].startswith("attachments/")]
    attachment_suffix = (
        f" and {len(attachments)} attachment{'s' if len(attachments) != 1 else ''}" if attachments else ""
    )
    if any(old_path in {"settings.yaml", "mt-policy.yaml"} for _, old_path, _ in changes):
        message = "Update tasks and settings" if cards else "Update workspace settings"
        if drafts:
            message += f" and {len(drafts)} draft{'s' if len(drafts) != 1 else ''}"
        return message + attachment_suffix
    if not cards and drafts:
        return f"Update {len(drafts)} workspace draft{'s' if len(drafts) != 1 else ''}{attachment_suffix}"
    if not cards and attachments:
        return f"Update {len(attachments)} attachment{'s' if len(attachments) != 1 else ''}"
    if not cards:
        return "Update workspace"
    actions: list[tuple[str, str, str | None, str]] = []  # verb, title, destination status, kind
    for code, old_path, new_path in cards:
        if code == "A":
            title, _status, kind = staged_card("", old_path, ws=ws)
            actions.append(("Add", title, None, kind))
        elif code == "D":
            title, _status, kind = staged_card("HEAD", old_path, ws=ws)
            actions.append(("Delete", title, None, kind))
        else:
            _title, before, _kind = staged_card("HEAD", old_path, ws=ws)
            title, after, kind = staged_card("", new_path or old_path, ws=ws)
            if before and after and before != after:
                actions.append(("Complete" if after == "done" else "Move", title, after, kind))
            else:
                actions.append(("Update", title, None, kind))
    drafts_suffix = f" and {len(drafts)} draft{'s' if len(drafts) != 1 else ''}" if drafts else ""
    extra_suffix = drafts_suffix + attachment_suffix
    if len(actions) == 1:
        verb, title, destination, kind = actions[0]
        if verb == "Move":
            return f"Move task {title} to {destination}{extra_suffix}"
        return f"{verb} {kind} {title}{extra_suffix}"
    kinds = {kind for *_rest, kind in actions}
    noun = "notes" if kinds == {"note"} else "tasks" if kinds == {"task"} else "items"
    if len({verb for verb, *_rest in actions}) == 1:
        return f"{actions[0][0]} {len(actions)} {noun}{extra_suffix}"
    return f"Update {len(actions)} {noun}{extra_suffix}"



class _TeeStderr:
    """Pass writes to the real stderr and keep a copy."""

    def __init__(self, target, copy: io.StringIO) -> None:
        self.target = target
        self.copy = copy

    def write(self, text: str) -> int:
        self.copy.write(text)
        return self.target.write(text)

    def flush(self) -> None:
        self.target.flush()


def record_sync_result(message: str, ws: Workspace | None = None) -> None:
    """Keep the last sync failure in .mt/last-sync-error; an empty message clears it."""
    path = _ws(ws).sync_error_path()
    try:
        if message:
            record = {"time": datetime.now().replace(microsecond=0).isoformat(), "message": message}
            atomic_write(path, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass  # The record is a convenience; it must not turn a finished sync into a failure.


def read_sync_error(ws: Workspace | None = None) -> tuple[str, str]:
    """Return (time, message) of the last failed sync, or ("", "") when the last sync succeeded."""
    try:
        record = json.loads(_ws(ws).sync_error_path().read_text(encoding="utf-8"))
        return str(record.get("time", "")), str(record["message"])
    except (OSError, ValueError, KeyError, AttributeError):
        return "", ""


def command_sync(
    args: argparse.Namespace,
    ws: Workspace | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    resolved = _ws(ws)
    out_stream = sys.stdout if out is None else out
    err_target = sys.stderr if err is None else err
    errors = io.StringIO()
    tee_err = _TeeStderr(err_target, errors)
    with sync_serialization_lock(resolved):
        result = _command_sync_serialized(args, ws=resolved, out=out_stream, err=tee_err)
    lines = [line for line in errors.getvalue().splitlines() if not line.startswith(("Workspace: ", "Application: "))]
    record_sync_result("" if result == 0 else "\n".join(lines).strip() or f"sync failed (exit {result})", resolved)
    return result



def _sync_preflight_branch(resolved: Workspace, *, err: TextIO) -> str | None:
    """Validate workspace, origin remote, and branch before staging sync changes."""
    git_status = git_ops.workspace_status(resolved.root)
    if git_status.state != "ready":
        print(f"error: {git_setup_message(git_status, 'sync', resolved)}", file=err)
        return None
    if not run_validator(resolved, err=err):
        return None
    if git_text("remote", "get-url", "origin", ws=resolved).returncode != 0:
        print("error: sync requires an 'origin' remote", file=err)
        return None
    problem = git_ops.origin_publication_problem(
        resolved.root, allow_public=store.policy(resolved).get("rules", {}).get("allow_public_origin") is True
    )
    if problem:
        print(f"error: refusing to sync: {problem}", file=err)
        return None
    branch = git_text("rev-parse", "--abbrev-ref", "HEAD", ws=resolved)
    if branch.returncode != 0 or branch.stdout.strip() == "HEAD":
        print("error: sync requires a named current branch", file=err)
        return None
    return branch.stdout.strip()


def _sync_stage_and_commit_local(
    resolved: Workspace, message_arg: str | None, *, out: TextIO, err: TextIO
) -> tuple[int | None, bool]:
    """Stage tracked/untracked task paths, commit changes if any, and verify a clean tree."""
    listed = git_text(
        "ls-files", "--cached", "--others", "--exclude-standard", "--", *SYNC_PATHS, ws=resolved,
    )
    if listed.returncode != 0:
        detail = listed.stderr.strip() or listed.stdout.strip()
        print(f"error: could not list task files: {detail}", file=err)
        return 1, False
    task_paths = sorted(set(listed.stdout.splitlines()))
    if task_paths:
        stage = git_text("add", "-A", "--", *task_paths, ws=resolved)
        if stage.returncode != 0:
            detail = stage.stderr.strip() or stage.stdout.strip()
            print(f"error: git add failed: {detail}", file=err)
            return 1, False
    try:
        changes = staged_task_changes(ws=resolved)
    except RuntimeError as error:
        print(f"error: {error}", file=err)
        return 1, False
    committed = False
    if not changes:
        print("No local task changes to commit.", file=out)
    else:
        message = message_arg.strip() if message_arg is not None else task_commit_message(changes, ws=resolved)
        if not message:
            print("error: sync message must not be empty", file=err)
            return 2, False
        paths = sorted(
            {path for _, old_path, new_path in changes for path in (old_path, new_path) if path is not None}
        )
        commit = git_text("commit", "--only", "-m", message, "--", *paths, ws=resolved)
        if commit.returncode != 0:
            detail = commit.stderr.strip() or commit.stdout.strip()
            print(f"error: git commit failed: {detail}", file=err)
            return 1, False
        committed = True
        print(f"Committed {len(changes)} workspace change(s): {message}", file=out)
    status = git_text("status", "--porcelain", "--untracked-files=all", ws=resolved)
    if status.returncode != 0 or status.stdout:
        print(
            "error: sync requires a clean working tree after committing cards and settings; "
            "commit or remove non-task changes first",
            file=err,
        )
        return 1, committed
    return None, committed


def _sync_reconcile_remote(resolved: Workspace, branch_name: str, *, err: TextIO) -> tuple[int | None, bool]:
    """Verify tree cleanliness after fetch, merge remote branch if present, and check ahead count."""
    status = git_text("status", "--porcelain", "--untracked-files=all", ws=resolved)
    if status.returncode != 0:
        detail = status.stderr.strip() or status.stdout.strip()
        print(f"error: could not inspect workspace after fetch: {detail}", file=err)
        return 1, False
    if status.stdout:
        print(
            "error: workspace changed while sync was fetching; no merge was performed. "
            "Run 'mt sync' again after the current task changes finish",
            file=err,
        )
        return 1, False
    remote_ref = f"refs/remotes/origin/{branch_name}"
    if git_text("rev-parse", "--verify", "--quiet", remote_ref, ws=resolved).returncode != 0:
        return None, True
    preflight = git_text("merge-tree", "--write-tree", "HEAD", f"origin/{branch_name}", ws=resolved)
    if preflight.returncode != 0:
        detail = preflight.stderr.strip() or preflight.stdout.strip()
        if detail:
            print(detail, file=err)
        print(
            f"error: sync would conflict with origin/{branch_name}; "
            "no merge was performed. Resolve the conflicting paths manually, "
            "then run 'mt sync' again.",
            file=err,
        )
        return 1, False
    merge = git_text("merge", "--no-edit", f"origin/{branch_name}", ws=resolved)
    if merge.returncode != 0:
        print(
            "Sync stopped. Resolve the merge conflicts shown by 'git status', "
            "or run 'git merge --abort'.",
            file=err,
        )
        return merge.returncode, False
    if not run_validator(resolved, err=err):
        return 1, False
    ahead = git_text("rev-list", "--count", f"origin/{branch_name}..HEAD", ws=resolved)
    if ahead.returncode != 0:
        detail = ahead.stderr.strip() or ahead.stdout.strip()
        print(f"error: could not inspect sync state: {detail}", file=err)
        return 1, False
    return None, int(ahead.stdout.strip() or "0") > 0


def _command_sync_serialized(
    args: argparse.Namespace,
    ws: Workspace | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    resolved = _ws(ws)
    out_stream = sys.stdout if out is None else out
    err_stream = sys.stderr if err is None else err
    with time_mutation_lock(resolved):
        branch_name = _sync_preflight_branch(resolved, err=err_stream)
        if branch_name is None:
            return 1
        error_code, committed = _sync_stage_and_commit_local(resolved, args.message, out=out_stream, err=err_stream)
        if error_code is not None:
            return error_code

    fetch = git_text("fetch", "origin", ws=resolved)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        print(f"error: git fetch failed: {detail}", file=err_stream)
        return 1

    with time_mutation_lock(resolved):
        error_code, should_push = _sync_reconcile_remote(resolved, branch_name, err=err_stream)
        if error_code is not None:
            return error_code

    if not should_push:
        print(f"Synced with origin/{branch_name}. Nothing to push.", file=out_stream)
        return 0
    push = git_text("push", "origin", f"HEAD:{branch_name}", ws=resolved)
    if push.returncode != 0:
        detail = push.stderr.strip() or push.stdout.strip()
        print(f"error: git push failed: {detail}", file=err_stream)
        return 1
    if committed:
        print(f"Synced task changes with origin/{branch_name}.", file=out_stream)
    else:
        print(f"Synced with origin/{branch_name}.", file=out_stream)
    return 0



def validate_item_changes(changes: dict[str, bytes], *, deletions: set[str] | None = None,
                          source_root: Path | None = None,
                          previous: dict[str, bytes | None] | None = None,
                          ws: Workspace | None = None) -> list[str]:
    """Validate proposed card bytes without copying the workspace and return lint warnings."""
    resolved = _ws(ws)
    deletions = deletions or set()
    for relative in set(changes) | deletions:
        if Path(relative).is_absolute() or ".." in Path(relative).parts or not re.fullmatch(r"cards/[^/]+\.md", relative):
            raise ValueError(f"invalid item path in item changes: {relative}")
    base = source_root or resolved.root
    report = validation.check_changes(base, store.policy(resolved), changes, deletions, previous)
    if report.errors:
        detail = "\n".join(
            ["FAILED: card changes failed validation:", *(f"  - {error}" for error in report.errors)]
        )
        raise ValueError(detail)
    return report.warnings

