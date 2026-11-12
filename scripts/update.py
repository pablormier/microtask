#!/usr/bin/env python3
"""Update application files while retaining workspace-owned data."""
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = "refs/remotes/upstream/main"
WORKSPACE_FILES = {"mt-policy.yaml", "mt-policy.local.yaml"}
WORKSPACE_PREFIXES = ("cards/", "drafts/", ".mt/")


class UpdateError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)


def checked_git(*args: str) -> str:
    result = git(*args)
    if result.returncode:
        raise UpdateError(result.stderr.strip() or result.stdout.strip() or f"git {args[0]} failed",
                          result.returncode)
    return result.stdout


def application_path(path: str) -> bool:
    return path not in WORKSPACE_FILES and not path.startswith(WORKSPACE_PREFIXES)


def tree_paths(revision: str) -> set[str]:
    output = checked_git("ls-tree", "-r", "-z", "--name-only", revision)
    return {path for path in output.split("\0") if path and application_path(path)}


def changes(target: str, paths: set[str]) -> list[str]:
    if not paths:
        return []
    output = checked_git("diff", "--name-status", "HEAD", target, "--", *sorted(paths))
    return output.splitlines()


def describe_change(change: str) -> str:
    status, _, path = change.partition("\t")
    action = {"A": "Added", "D": "Removed", "M": "Modified", "T": "Changed type of",
              "R": "Renamed", "C": "Copied"}.get(status[:1], "Changed")
    if status[:1] in {"R", "C"}:
        path = path.replace("\t", " → ", 1)
    return f"{action}: {path}"


def clean_workspace() -> None:
    if checked_git("status", "--porcelain", "--untracked-files=all"):
        raise UpdateError("update requires a clean working tree; commit or remove local changes first")


def workspace_status_paths() -> list[bytes]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=ROOT, capture_output=True, check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise UpdateError(detail or "git status failed", result.returncode)
    records = result.stdout.split(b"\0")
    paths: list[bytes] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status, path = record[:2], record[3:]
        paths.append(path)
        if any(flag in status for flag in (b"R", b"C")) and index < len(records):
            paths.append(records[index])
            index += 1
    return paths


@contextmanager
def preserve_workspace_drafts():
    """Stash only local draft changes while replacing application files."""
    paths = workspace_status_paths()
    non_draft = [path for path in paths if not path.startswith(b"drafts/")]
    if non_draft:
        raise UpdateError("update requires a clean working tree outside drafts/; commit or remove local changes first")
    if not paths:
        yield
        return

    previous = git("rev-parse", "--verify", "--quiet", "refs/stash")
    previous_stash = previous.stdout.strip() if previous.returncode == 0 else ""
    saved = git("stash", "push", "--include-untracked", "--message", "microtask update: preserve workspace drafts",
                "--", "drafts/")
    if saved.returncode:
        raise UpdateError(saved.stderr.strip() or saved.stdout.strip() or "could not preserve workspace drafts",
                          saved.returncode)
    current = git("rev-parse", "--verify", "--quiet", "refs/stash")
    stash_oid = current.stdout.strip() if current.returncode == 0 else ""
    if not stash_oid or stash_oid == previous_stash:
        raise UpdateError("could not safely park local drafts for update")
    try:
        if any(path.startswith(b"drafts/") for path in workspace_status_paths()):
            raise UpdateError("could not safely park local drafts for update")
        yield
    finally:
        restored = git("stash", "apply", "--index", stash_oid)
        if restored.returncode:
            raise UpdateError(
                f"update finished, but local drafts could not be restored; they remain in git stash {stash_oid[:12]}"
            )
        stash_list = git("stash", "list", "--format=%gd %H")
        reference = next((line.split(" ", 1)[0] for line in stash_list.stdout.splitlines()
                         if len(line.split(" ", 1)) == 2 and line.split(" ", 1)[1] == stash_oid), None)
        if reference:
            dropped = git("stash", "drop", reference)
            if dropped.returncode:
                print(f"warning: restored drafts remain in git stash {stash_oid[:12]}", file=sys.stderr)


@contextmanager
def lock(name: str):
    path = ROOT / ".mt" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def check_candidate(target: str, paths: set[str]) -> None:
    if paths and git("diff", "--quiet", target, "--", *sorted(paths)).returncode != 0:
        raise UpdateError("candidate application files differ from upstream/main")
    for command in ((str(ROOT / "mt"), "help"), (str(ROOT / "mt"), "validate")):
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise UpdateError(f"candidate failed {' '.join(command[1:])}: {detail}")


def apply_update(target: str, paths: set[str]) -> None:
    before = checked_git("rev-parse", "HEAD").strip()
    try:
        merge = git("merge", "--no-commit", "--no-ff", "-s", "ours", target)
        if merge.returncode:
            raise UpdateError(merge.stderr.strip() or merge.stdout.strip() or "could not prepare update",
                              merge.returncode)
        if paths:
            checked_git("restore", "--source", target, "--staged", "--worktree", "--", *sorted(paths))
        check_candidate(target, paths)
        merge_head = Path(checked_git("rev-parse", "--git-path", "MERGE_HEAD").strip())
        if not merge_head.is_absolute():
            merge_head = ROOT / merge_head
        if merge_head.exists() or checked_git("diff", "--cached", "--name-only"):
            checked_git("commit", "-m", f"Update microtask application to {target[:12]}")
        if checked_git("status", "--porcelain", "--untracked-files=all"):
            raise UpdateError("update left unexpected working tree changes")
    except (OSError, UpdateError):
        rollback = git("reset", "--hard", before)
        if rollback.returncode:
            raise UpdateError("update failed and rollback failed: " + rollback.stderr.strip())
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mt update", description="Update microtask application files from upstream/main while preserving workspace data.")
    parser.add_argument("--check", action="store_true", help="fetch and preview application changes without applying them")
    parser.add_argument("--json", action="store_true", help="print the --check preview as JSON")
    parser.add_argument("--force", action="store_true", help="compatibility option; application files always follow upstream")
    args = parser.parse_args(argv)
    if args.json and not args.check:
        parser.error("--json requires --check")
    try:
        if Path(checked_git("rev-parse", "--show-toplevel").strip()).resolve() != ROOT:
            raise UpdateError("update requires this workspace to be the Git repository root")
        checked_git("remote", "get-url", "upstream")
        with lock("sync.lock"):
            with lock("drafts.lock"):
                with preserve_workspace_drafts():
                    clean_workspace()
                    checked_git("fetch", "upstream")
                    target = checked_git("rev-parse", "--verify", f"{UPSTREAM}^{{commit}}").strip()
                    if git("merge-base", "HEAD", target).returncode == 1:
                        raise UpdateError(
                            "this workspace and upstream/main share no history, usually because the "
                            "microtask history was rewritten. Check that upstream is microtask with "
                            "'mt run git remote get-url upstream', then join the histories once without "
                            "changing any files and run 'mt update' again:\n"
                            "  mt run git merge -s ours --allow-unrelated-histories "
                            "-m \"Adopt rewritten microtask history\" upstream/main"
                        )
                    paths = tree_paths("HEAD") | tree_paths(target)
                    change_list = changes(target, paths)
                    upstream_commits = int(checked_git("rev-list", "--count", f"HEAD..{target}").strip())
                    if args.check:
                        preview = {"upstream_commit": target, "application_changes": change_list,
                                   "workspace_data": "preserved"}
                        if args.json:
                            print(json.dumps(preview))
                        else:
                            print(f"Upstream: {target}")
                            print(f"Application changes: {len(change_list)}")
                            for change in change_list:
                                print(change)
                            print("Workspace cards and policy are preserved.")
                        return 0
                    with lock("time-mutation.lock"):
                        clean_workspace()
                        apply_update(target, paths)
                    if change_list:
                        print(f"Update complete: microtask files now match upstream/main ({target[:12]}).")
                        print(f"Application files changed: {len(change_list)}")
                        for change in change_list:
                            print(f"  {describe_change(change)}")
                    elif upstream_commits:
                        print(f"Update complete: applied {upstream_commits} upstream "
                              f"commit{'s' if upstream_commits != 1 else ''} ({target[:12]}).")
                        print("No application files changed.")
                    else:
                        print(f"Already up to date with upstream/main ({target[:12]}). No files changed.")
                    print("Nothing was pushed.")
                    return 0
    except (OSError, UpdateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return error.code if isinstance(error, UpdateError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
