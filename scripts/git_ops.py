#!/usr/bin/env python3
"""Small, explicit Git integration for microtask workspaces."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceGitStatus:
    """Git readiness for exactly one selected workspace."""

    state: str
    detail: str
    root: Path | None = None


def run_git(
    repo_root: Path,
    *arguments: str,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git in an explicit workspace, capturing output unless it should stream."""
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=capture_output,
            text=True,
            env=env,
        )
    except FileNotFoundError as error:
        # A selected workspace is expected to exist; distinguish it from a missing
        # executable so local callers can report the useful Git setup guidance.
        if not Path(repo_root).is_dir():
            raise
        return subprocess.CompletedProcess(
            ["git", *arguments], 127, "", "Git is not installed or not on PATH"
        )


def workspace_status(repo_root: Path) -> WorkspaceGitStatus:
    """Check that Git can operate on this workspace without accepting a parent repo."""
    try:
        root = Path(repo_root).resolve()
    except (OSError, RuntimeError) as error:
        return WorkspaceGitStatus("error", f"could not resolve workspace path: {error}")
    if shutil.which("git") is None:
        return WorkspaceGitStatus("missing", "Git is not installed or not on PATH")
    if not root.is_dir():
        return WorkspaceGitStatus("error", f"workspace directory does not exist: {root}")

    local_metadata = root / ".git"
    has_local_metadata = local_metadata.exists() or local_metadata.is_symlink()
    try:
        inspection_env = os.environ.copy()
        inspection_env["LC_ALL"] = "C"
        result = run_git(root, "rev-parse", "--show-toplevel", env=inspection_env)
    except OSError as error:
        return WorkspaceGitStatus("error", f"could not run Git: {error}")
    if result.returncode == 0:
        try:
            git_root = Path(result.stdout.strip()).resolve(strict=True)
        except (OSError, RuntimeError):
            return WorkspaceGitStatus(
                "error", "Git reported a repository root that cannot be resolved"
            )
        if git_root != root:
            if has_local_metadata:
                return WorkspaceGitStatus(
                    "error",
                    f"Git discovered a different repository at {git_root} despite local Git metadata at {local_metadata}",
                )
            return WorkspaceGitStatus(
                "uninitialized",
                f"Git found a parent repository at {git_root}; initialize this workspace with 'git init'",
            )
        return WorkspaceGitStatus("ready", "Git repository is ready", git_root)

    detail = (result.stderr or result.stdout).strip()
    lowered = detail.lower()
    if "not a git repository" in lowered or "outside a repository" in lowered:
        if has_local_metadata:
            return WorkspaceGitStatus(
                "error", detail or f"could not inspect Git metadata at {local_metadata}"
            )
        return WorkspaceGitStatus(
            "uninitialized", "this workspace is not initialized as a Git repository"
        )
    return WorkspaceGitStatus("error", detail or "could not inspect Git repository")


def _value(repo_root: Path, *arguments: str) -> str:
    try:
        result = run_git(repo_root, *arguments)
    except OSError:
        return "-"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "-"


def repository_info(repo_root: Path) -> dict[str, object]:
    """Return Git readiness plus branch and configured remotes for this workspace."""
    status = workspace_status(repo_root)
    result: dict[str, object] = {
        "state": status.state,
        "detail": status.detail,
        "branch": "-",
        "origin": "-",
        "upstream": "-",
    }
    if status.state == "ready":
        result.update({
            "branch": _value(repo_root, "symbolic-ref", "--short", "HEAD"),
            "origin": _value(repo_root, "remote", "get-url", "origin"),
            "upstream": _value(repo_root, "remote", "get-url", "upstream"),
        })
    return result
