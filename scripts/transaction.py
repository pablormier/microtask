"""Transactional card writes, file locks, validation, and git sync."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import fcntl
from functools import wraps
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import uuid

from card_format import is_timestamp, parse_frontmatter, parse_yaml
import drafts as draft_store
import git_ops
import store
from store import (
    _ws,
    active_root,
    cards_root,
    current_username,
    task_by_id,
    time_timer_path,
)
import validation
from workspace import Workspace

SYNC_PATHS = (":(glob)cards/*.md", ":(glob)drafts/*.md", "mt-policy.yaml")
_TIME_LOCK_STATE = threading.local()
atomic_write = draft_store.atomic_write


@dataclass
class MutationReceipt:
    payload: dict[str, object]



def _require_mutation_lock() -> None:
    if not getattr(_TIME_LOCK_STATE, "held", False):
        raise RuntimeError("mutation lock must be held before modifying workspace state")



@contextmanager
def time_mutation_lock(ws: Workspace | None = None):
    """Serialize task and timer mutations across CLI and web processes."""
    lock_path = _ws(ws).mutation_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        _TIME_LOCK_STATE.held = True
        try:
            yield
        finally:
            _TIME_LOCK_STATE.held = False
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



def serialized_time_mutation(function):
    @wraps(function)
    def locked(*args, **kwargs):
        ws = _ws(kwargs.get("ws"))
        _TIME_LOCK_STATE.sync_requested = False
        with time_mutation_lock(ws):
            result = function(*args, **kwargs)
            sync_requested = _TIME_LOCK_STATE.sync_requested
        _TIME_LOCK_STATE.sync_requested = False
        if isinstance(result, MutationReceipt):
            if sync_requested:
                sync_error = run_auto_sync_for_receipt(ws=ws)
                if sync_error:
                    result.payload["sync_error"] = sync_error
            print(json.dumps(result.payload, ensure_ascii=False))
            return 0
        if sync_requested:
            sync_result = _run_auto_sync(ws=ws)
            return result or sync_result
        return result

    return locked



def read_time_timers(ws: Workspace | None = None) -> dict[str, dict[str, object]]:
    resolved = _ws(ws)
    if not getattr(_TIME_LOCK_STATE, "held", False):
        with time_mutation_lock(resolved):
            return read_time_timers(resolved)
    path = time_timer_path(resolved)
    try:
        timers = _read_time_timers(resolved)
    except (ValueError, UnicodeError):
        recovery = resolved.root / ".mt" / "recovery" / f"time-running-{uuid.uuid4().hex}.json"
        recovery.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, recovery)
        print(f"warning: malformed running timer state moved to {recovery}; continuing without timers", file=sys.stderr)
        return {}
    migrated = False
    for task_id, timer in timers.items():
        if "by" not in timer:
            task = task_by_id(task_id, ws=resolved)
            timer["by"] = (task.frontmatter.get("claimed_by") if task else None) or current_username(ws=resolved)
            migrated = True
    if migrated:
        write_time_timers(timers, ws=resolved)
    return timers



def _read_time_timers(ws: Workspace | None = None) -> dict[str, dict[str, object]]:
    path = time_timer_path(ws)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"running timer state is malformed: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"running timer state is malformed: {path}")
    timers: dict[str, dict[str, object]] = {}
    for task_id, timer in value.items():
        if not isinstance(timer, dict):
            raise ValueError(f"running timer state is malformed: {path}")
        session_id = timer.get("session_id")
        start = timer.get("start")
        start_epoch = timer.get("start_epoch")
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", session_id)
            or not isinstance(start, str)
            or not is_timestamp(start)
            or not isinstance(start_epoch, int)
            or isinstance(start_epoch, bool)
        ):
            raise ValueError(f"running timer state is malformed: {path}")
        owner = timer.get("by")
        if "by" in timer and (not isinstance(owner, str) or not owner.strip()
                              or any(char in owner for char in "\r\n\x00")):
            raise ValueError(f"running timer state is malformed: {path}")
        timers[task_id] = timer
    return timers



def write_time_timers(timers: dict[str, dict[str, object]], ws: Workspace | None = None) -> None:
    path = time_timer_path(ws)
    if not timers:
        path.unlink(missing_ok=True)
        return
    atomic_write(path, (json.dumps(timers, indent=2) + "\n").encode("utf-8"))



def clear_time_timer(task_id: str, session_id: str, ws: Workspace | None = None) -> bool:
    timers = read_time_timers(ws)
    timer = timers.get(task_id)
    if timer is None or timer.get("session_id") != session_id:
        return False
    del timers[task_id]
    write_time_timers(timers, ws=ws)
    return True



def managed_temp_directory(prefix: str, ws: Workspace | None = None) -> tempfile.TemporaryDirectory[str]:
    root = _ws(ws).scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=root)



def run_validator(ws: Workspace | None = None) -> bool:
    resolved = _ws(ws)
    try:
        effective_policy = store.policy(resolved)
    except (OSError, ValueError) as error:
        print("FAILED: invalid policy:", file=sys.stderr)
        for line in str(error).splitlines():
            print(f"  - {line}", file=sys.stderr)
        return False
    report = validation.audit_workspace(active_root(resolved), effective_policy)
    if report.errors:
        print(f"FAILED: {len(report.errors)} error(s):", file=sys.stderr)
        for error in report.errors:
            print(f"  - {error}", file=sys.stderr)
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
    base = source_root or active_root(resolved)
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
                raise ValueError(f"item changed before transaction: {relative}")
    validate_item_changes(changes, deletions=deletions, source_root=base, previous=original, ws=resolved)
    try:
        for relative, before in original.items():
            path = base / relative
            current = path.read_bytes() if path.is_file() else None
            if current != before:
                raise ValueError(f"item changed before transaction: {relative}")
        for relative in deletions:
            path = base / relative
            if path.exists():
                path.unlink()
        for relative, content in changes.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content)
        validate_data_root(base, changes=changes, deletions=deletions, previous=original, ws=resolved)
    except Exception:
        for relative in reversed(sorted(affected)):
            path = base / relative
            before = original[relative]
            try:
                current = path.read_bytes() if path.is_file() else None
                expected_after = changes.get(relative, None)
                if relative in deletions and relative not in changes:
                    expected_after = None
                # Do not overwrite an unexpected concurrent edit while rolling back.
                if current != expected_after and current != before:
                    continue
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, before)
            except OSError:
                pass
        raise



def auto_sync_after_mutation(*, changed: bool = True, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    if not changed or not store.policy(resolved).get("rules", {}).get("auto_sync", False):
        return 0
    if getattr(_TIME_LOCK_STATE, "held", False):
        _TIME_LOCK_STATE.sync_requested = True
        return 0
    return _run_auto_sync(ws=resolved)



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



def _run_auto_sync(ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    if sync_deferred(resolved):
        return 0
    status = git_ops.workspace_status(resolved.root)
    if status.state in {"missing", "uninitialized"}:
        print(auto_sync_unavailable_message(status, resolved))
        return 0
    print("Auto-syncing task changes...")
    result = command_sync(argparse.Namespace(message=None), ws=resolved)
    if result != 0:
        print(
            "error: local change succeeded, but auto-sync failed; local changes were preserved",
            file=sys.stderr,
        )
    return result



def run_auto_sync_for_receipt(ws: Workspace | None = None) -> str | None:
    resolved = _ws(ws)
    if sync_deferred(resolved):
        return None
    status = git_ops.workspace_status(resolved.root)
    if status.state in {"missing", "uninitialized"}:
        return auto_sync_unavailable_message(status, resolved)
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        result = _run_auto_sync(resolved)
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
    frontmatter, _body = parse_frontmatter(raw) if raw else (None, "")
    properties = parse_yaml(frontmatter) if frontmatter is not None else {}
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
    cards = [change for change in markdown_changes if not change[1].startswith("drafts/")]
    if any(old_path == "mt-policy.yaml" for _, old_path, _ in changes):
        message = "Update tasks and policy" if cards else "Update workspace policy"
        if drafts:
            message += f" and {len(drafts)} draft{'s' if len(drafts) != 1 else ''}"
        return message
    if not cards and drafts:
        return f"Update {len(drafts)} workspace draft{'s' if len(drafts) != 1 else ''}"
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
    if len(actions) == 1:
        verb, title, destination, kind = actions[0]
        if verb == "Move":
            return f"Move task {title} to {destination}{drafts_suffix}"
        return f"{verb} {kind} {title}{drafts_suffix}"
    kinds = {kind for *_rest, kind in actions}
    noun = "notes" if kinds == {"note"} else "tasks" if kinds == {"task"} else "items"
    if len({verb for verb, *_rest in actions}) == 1:
        return f"{actions[0][0]} {len(actions)} {noun}{drafts_suffix}"
    return f"Update {len(actions)} {noun}{drafts_suffix}"



def command_sync(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    with sync_serialization_lock(resolved):
        return _command_sync_serialized(args, ws=resolved)



def _sync_preflight_branch(resolved: Workspace) -> str | None:
    """Validate workspace, origin remote, and branch before staging sync changes."""
    git_status = git_ops.workspace_status(resolved.root)
    if git_status.state != "ready":
        print(f"error: {git_setup_message(git_status, 'sync', resolved)}", file=sys.stderr)
        return None
    if not run_validator(resolved):
        return None
    if git_text("remote", "get-url", "origin", ws=resolved).returncode != 0:
        print("error: sync requires an 'origin' remote", file=sys.stderr)
        return None
    problem = git_ops.origin_publication_problem(
        resolved.root, allow_public=store.policy(resolved).get("rules", {}).get("allow_public_origin") is True
    )
    if problem:
        print(f"error: refusing to sync: {problem}", file=sys.stderr)
        return None
    branch = git_text("rev-parse", "--abbrev-ref", "HEAD", ws=resolved)
    if branch.returncode != 0 or branch.stdout.strip() == "HEAD":
        print("error: sync requires a named current branch", file=sys.stderr)
        return None
    return branch.stdout.strip()


def _sync_stage_and_commit_local(resolved: Workspace, message_arg: str | None) -> tuple[int | None, bool]:
    """Stage tracked/untracked task paths, commit changes if any, and verify a clean tree."""
    listed = git_text(
        "ls-files", "--cached", "--others", "--exclude-standard", "--", *SYNC_PATHS, ws=resolved,
    )
    if listed.returncode != 0:
        detail = listed.stderr.strip() or listed.stdout.strip()
        print(f"error: could not list task files: {detail}", file=sys.stderr)
        return 1, False
    task_paths = sorted(set(listed.stdout.splitlines()))
    if task_paths:
        stage = git_text("add", "-A", "--", *task_paths, ws=resolved)
        if stage.returncode != 0:
            detail = stage.stderr.strip() or stage.stdout.strip()
            print(f"error: git add failed: {detail}", file=sys.stderr)
            return 1, False
    try:
        changes = staged_task_changes(ws=resolved)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1, False
    committed = False
    if not changes:
        print("No local task changes to commit.")
    else:
        message = message_arg.strip() if message_arg is not None else task_commit_message(changes, ws=resolved)
        if not message:
            print("error: sync message must not be empty", file=sys.stderr)
            return 2, False
        paths = sorted(
            {path for _, old_path, new_path in changes for path in (old_path, new_path) if path is not None}
        )
        commit = git_text("commit", "--only", "-m", message, "--", *paths, ws=resolved)
        if commit.returncode != 0:
            detail = commit.stderr.strip() or commit.stdout.strip()
            print(f"error: git commit failed: {detail}", file=sys.stderr)
            return 1, False
        committed = True
        print(f"Committed {len(changes)} workspace change(s): {message}")
    status = git_text("status", "--porcelain", "--untracked-files=all", ws=resolved)
    if status.returncode != 0 or status.stdout:
        print(
            "error: sync requires a clean working tree after committing cards and policy; "
            "commit or remove non-task changes first",
            file=sys.stderr,
        )
        return 1, committed
    return None, committed


def _sync_reconcile_remote(resolved: Workspace, branch_name: str) -> tuple[int | None, bool]:
    """Verify tree cleanliness after fetch, merge remote branch if present, and check ahead count."""
    status = git_text("status", "--porcelain", "--untracked-files=all", ws=resolved)
    if status.returncode != 0:
        detail = status.stderr.strip() or status.stdout.strip()
        print(f"error: could not inspect workspace after fetch: {detail}", file=sys.stderr)
        return 1, False
    if status.stdout:
        print(
            "error: workspace changed while sync was fetching; no merge was performed. "
            "Run 'mt sync' again after the current task changes finish",
            file=sys.stderr,
        )
        return 1, False
    remote_ref = f"refs/remotes/origin/{branch_name}"
    if git_text("rev-parse", "--verify", "--quiet", remote_ref, ws=resolved).returncode != 0:
        return None, True
    preflight = git_text("merge-tree", "--write-tree", "HEAD", f"origin/{branch_name}", ws=resolved)
    if preflight.returncode != 0:
        detail = preflight.stderr.strip() or preflight.stdout.strip()
        if detail:
            print(detail, file=sys.stderr)
        print(
            f"error: sync would conflict with origin/{branch_name}; "
            "no merge was performed. Resolve the conflicting paths manually, "
            "then run 'mt sync' again.",
            file=sys.stderr,
        )
        return 1, False
    merge = git_text("merge", "--no-edit", f"origin/{branch_name}", ws=resolved)
    if merge.returncode != 0:
        print(
            "Sync stopped. Resolve the merge conflicts shown by 'git status', "
            "or run 'git merge --abort'.",
            file=sys.stderr,
        )
        return merge.returncode, False
    if not run_validator(resolved):
        return 1, False
    ahead = git_text("rev-list", "--count", f"origin/{branch_name}..HEAD", ws=resolved)
    if ahead.returncode != 0:
        detail = ahead.stderr.strip() or ahead.stdout.strip()
        print(f"error: could not inspect sync state: {detail}", file=sys.stderr)
        return 1, False
    return None, int(ahead.stdout.strip() or "0") > 0


def _command_sync_serialized(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    with time_mutation_lock(resolved):
        branch_name = _sync_preflight_branch(resolved)
        if branch_name is None:
            return 1
        error_code, committed = _sync_stage_and_commit_local(resolved, args.message)
        if error_code is not None:
            return error_code

    fetch = git_text("fetch", "origin", ws=resolved)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        print(f"error: git fetch failed: {detail}", file=sys.stderr)
        return 1

    with time_mutation_lock(resolved):
        error_code, should_push = _sync_reconcile_remote(resolved, branch_name)
        if error_code is not None:
            return error_code

    if not should_push:
        print(f"Synced with origin/{branch_name}. Nothing to push.")
        return 0
    push = git_text("push", "origin", f"HEAD:{branch_name}", ws=resolved)
    if push.returncode != 0:
        detail = push.stderr.strip() or push.stdout.strip()
        print(f"error: git push failed: {detail}", file=sys.stderr)
        return 1
    if committed:
        print(f"Synced task changes with origin/{branch_name}.")
    else:
        print(f"Synced with origin/{branch_name}.")
    return 0



def validate_item_changes(changes: dict[str, bytes], *, deletions: set[str] | None = None,
                          source_root: Path | None = None,
                          previous: dict[str, bytes | None] | None = None,
                          ws: Workspace | None = None) -> None:
    """Validate proposed card bytes without copying the workspace."""
    resolved = _ws(ws)
    deletions = deletions or set()
    for relative in set(changes) | deletions:
        if Path(relative).is_absolute() or ".." in Path(relative).parts or not re.fullmatch(r"cards/[^/]+\.md", relative):
            raise ValueError(f"invalid item path in item changes: {relative}")
    base = source_root or active_root(resolved)
    errors = validation.check_changes(base, store.policy(resolved), changes, deletions, previous)
    if errors:
        detail = "\n".join(
            ["FAILED: card changes failed validation:", *(f"  - {error}" for error in errors)]
        )
        raise ValueError(detail)

