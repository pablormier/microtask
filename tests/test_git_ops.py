from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
import git_ops


class GitOperationsTests(unittest.TestCase):
    def git(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments], cwd=cwd, text=True, capture_output=True, check=False
        )

    def initialize_repo(self, root: Path) -> None:
        self.assertEqual(self.git(root, "init", "-q").returncode, 0)
        self.assertEqual(self.git(root, "config", "user.name", "Git ops test").returncode, 0)
        self.assertEqual(
            self.git(root, "config", "user.email", "git-ops@example.invalid").returncode, 0
        )
        (root / "README.md").write_text("fixture\n", encoding="utf-8")
        self.assertEqual(self.git(root, "add", "README.md").returncode, 0)
        self.assertEqual(self.git(root, "commit", "-qm", "Initial").returncode, 0)

    def test_missing_git_is_reported_as_optional_integration_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(git_ops.shutil, "which", return_value=None):
                status = git_ops.workspace_status(Path(temporary))
            self.assertEqual(status.state, "missing")

    def test_parent_repository_is_not_used_for_workspace_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            self.initialize_repo(parent)
            workspace = parent / "workspace"
            workspace.mkdir()

            status = git_ops.workspace_status(workspace)

            self.assertEqual(status.state, "uninitialized")
            self.assertIn("parent repository", status.detail)

    def test_uninitialized_probe_uses_locale_independent_git_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            def inspect(root: Path, *arguments: str, env: dict[str, str] | None = None):
                self.assertEqual(arguments, ("rev-parse", "--show-toplevel"))
                self.assertEqual(env["LC_ALL"], "C")
                return subprocess.CompletedProcess(
                    ["git", *arguments], 128, "", "fatal: not a git repository"
                )

            with (
                patch.object(git_ops.shutil, "which", return_value="/usr/bin/git"),
                patch.object(git_ops, "run_git", side_effect=inspect),
            ):
                status = git_ops.workspace_status(Path(temporary))

            self.assertEqual(status.state, "uninitialized")

    def test_malformed_local_git_metadata_is_a_real_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            self.initialize_repo(parent)
            workspace = parent / "workspace"
            workspace.mkdir()
            (workspace / ".git").write_text("gitdir: /missing/worktree\n", encoding="utf-8")

            status = git_ops.workspace_status(workspace)

            self.assertEqual(status.state, "error")
            self.assertNotEqual(status.state, "uninitialized")

    def test_linked_worktree_is_a_usable_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self.initialize_repo(repository)
            worktree = root / "linked"
            result = self.git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
            self.assertEqual(result.returncode, 0, result.stderr)

            status = git_ops.workspace_status(worktree)

            self.assertEqual(status.state, "ready")
            self.assertEqual(status.root, worktree.resolve())

    def test_git_launch_failure_is_reported_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(git_ops.shutil, "which", return_value="/bad/git"),
                patch.object(git_ops, "run_git", side_effect=OSError("cannot execute Git")),
            ):
                status = git_ops.workspace_status(Path(temporary))
            self.assertEqual(status.state, "error")
            self.assertIn("cannot execute Git", status.detail)


class OriginPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.bin = Path(self.temporary.name) / "bin"
        self.bin.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def set_remote(self, name: str, url: str) -> None:
        subprocess.run(["git", "remote", "add", name, url], cwd=self.root, check=True)

    def fake_gh(self, output: str, returncode: int = 0) -> dict[str, str]:
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\ncat <<'JSON'\n{output}\nJSON\nexit {returncode}\n")
        gh.chmod(0o755)
        return {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"}

    def test_origin_matching_upstream_or_canonical_app_is_refused(self) -> None:
        self.set_remote("upstream", "https://github.com/example/microtask.git")
        self.set_remote("origin", "git@github.com:Example/microtask")
        self.assertIn("application repository", git_ops.origin_publication_problem(self.root, allow_public=True))

        subprocess.run(["git", "remote", "remove", "upstream"], cwd=self.root, check=True)
        subprocess.run(["git", "remote", "set-url", "origin", "ssh://git@github.com/pablormier/microtask/"],
                       cwd=self.root, check=True)
        self.assertIn("application repository", git_ops.origin_publication_problem(self.root))

    def test_public_github_origin_is_refused_only_when_gh_reports_it(self) -> None:
        self.set_remote("origin", "git@github.com:me/tasks.git")
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            self.assertIsNone(git_ops.origin_publication_problem(self.root))
        with patch.dict(os.environ, self.fake_gh('{"visibility":"PUBLIC","isFork":true}')):
            problem = git_ops.origin_publication_problem(self.root)
            self.assertIn("public GitHub repository", problem)
            self.assertIn("fork", problem)
            self.assertIsNone(git_ops.origin_publication_problem(self.root, allow_public=True))
        with patch.dict(os.environ, self.fake_gh('{"visibility":"PRIVATE","isFork":false}')):
            self.assertIsNone(git_ops.origin_publication_problem(self.root))
        with patch.dict(os.environ, self.fake_gh("not signed in", returncode=1)):
            self.assertIsNone(git_ops.origin_publication_problem(self.root))

    def test_local_origin_skips_github_check(self) -> None:
        self.set_remote("origin", str(Path(self.temporary.name) / "origin.git"))
        with patch.dict(os.environ, self.fake_gh('{"visibility":"PUBLIC","isFork":false}')):
            self.assertIsNone(git_ops.origin_publication_problem(self.root))


if __name__ == "__main__":
    unittest.main()
