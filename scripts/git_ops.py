#!/usr/bin/env python3
"""Small, explicit Git integration for microtask workspaces."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# The public application repository. Cards must never be pushed here.
CANONICAL_UPSTREAM = "https://github.com/pablormier/microtask.git"
GITHUB_HOSTS = {"github.com", "www.github.com", "ssh.github.com"}
_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^/:]+):(?!//)(.+)$")


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


def remote_identity(url: str, repo_root: Path | None = None) -> tuple[str, str]:
    """Reduce a Git remote URL to (host, path) so equivalent spellings compare equal.

    Local paths become ("", absolute path). Hosted paths are lowercased and lose
    a trailing ".git", because GitHub and most hosts treat them that way.
    """
    text = url.strip()
    host = ""
    path = text
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme == "file":
            path = parts.path
        else:
            host = (parts.hostname or "").lower()
            path = parts.path
    else:
        match = _SCP_LIKE.match(text)
        if match and not Path(text).exists():
            host, path = match.group(1).lower(), match.group(2)
    if not host:
        local = Path(path).expanduser()
        if not local.is_absolute() and repo_root is not None:
            local = Path(repo_root) / local
        try:
            local = local.resolve()
        except (OSError, RuntimeError):
            pass
        return "", str(local).rstrip("/")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return host, path.lower()


def github_slug(url: str) -> str | None:
    """Return "owner/repo" for a GitHub remote URL, otherwise None."""
    host, path = remote_identity(url)
    if host not in GITHUB_HOSTS or path.count("/") != 1:
        return None
    return path


def github_visibility(slug: str) -> dict[str, object] | None:
    """Ask the GitHub CLI for a repository's visibility, if gh is installed and answers.

    Returns None when gh is missing, not signed in, offline, or the repository
    cannot be read; callers treat that as "unknown" rather than as an error.
    """
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "repo", "view", slug, "--json", "visibility,isFork,parent"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def origin_publication_problem(repo_root: Path, *, allow_public: bool = False) -> str | None:
    """Explain why pushing cards to origin could publish them, or return None when it looks safe.

    Origin pointing at the application repository is always refused. A public
    GitHub origin is refused unless allow_public is set; that check runs only
    when the GitHub CLI is available, so it never adds a hard dependency.
    """
    origin = _value(repo_root, "remote", "get-url", "origin")
    if origin == "-":
        return None
    origin_id = remote_identity(origin, repo_root)
    app_urls = [CANONICAL_UPSTREAM]
    upstream = _value(repo_root, "remote", "get-url", "upstream")
    if upstream != "-":
        app_urls.append(upstream)
    if any(remote_identity(url, repo_root) == origin_id for url in app_urls):
        return (
            f"origin ({origin}) is the microtask application repository, not your own task "
            "repository, so syncing would push your cards there. Point origin at a private "
            "repository you own: mt run git remote set-url origin YOUR-PRIVATE-REPOSITORY-URL"
        )
    if allow_public:
        return None
    slug = github_slug(origin)
    if slug is None:
        return None
    info = github_visibility(slug)
    if info is None or str(info.get("visibility", "")).upper() != "PUBLIC":
        return None
    detail = f"origin ({slug}) is a public GitHub repository, so syncing would publish your cards."
    if info.get("isFork"):
        detail += " It is a fork, and GitHub keeps forks of public repositories public; create a new private repository instead."
    else:
        detail += " Make it private on GitHub or point origin at a private repository."
    return detail + " To publish on purpose, set rules.allow_public_origin: true in mt-policy.yaml."
