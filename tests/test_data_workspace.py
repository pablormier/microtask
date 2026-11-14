"""A data-only workspace (cards and policy, no application) run by another mt."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns(".git", ".mt", ".DS_Store", "cards", "drafts")


def tree(root: Path, folders: tuple[str, ...] = ("cards", "drafts", ".mt")) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for folder in folders if (root / folder).exists()
            for path in (root / folder).rglob("*") if path.is_file()}


class DataWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        # The application keeps its default policy and examples, so it would show
        # the demo; it must neither show it to nor take writes from the workspace.
        self.app = self.home / "app"
        shutil.copytree(SOURCE_ROOT, self.app, ignore=IGNORED)
        (self.app / "cards").mkdir()
        self.data = self.home / "project tasks"
        (self.data / "cards").mkdir(parents=True)
        policy = (SOURCE_ROOT / "mt-policy.yaml").read_text(encoding="utf-8")
        self.assertIn("default_priority: none", policy)
        (self.data / "mt-policy.yaml").write_text(
            policy.replace("default_priority: none", "default_priority: low"), encoding="utf-8")
        self.env = {**os.environ, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
                    "MT_USERNAME": "tester", "MT_WORKSPACE": str(self.data)}
        for name in ("MT_PROJECT", "MT_TOPIC"):
            self.env.pop(name, None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mt(self, *arguments: str, env: dict[str, str] | None = None,
           input_text: str | None = None, ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([str(self.app / "mt"), *arguments], cwd=self.home, env=env or self.env,
                                input=input_text, text=True, capture_output=True, check=False)
        if ok:
            self.assertEqual(result.returncode, 0, f"mt {' '.join(arguments)}\n{result.stderr}")
        return result

    def test_every_operation_reads_and_writes_only_the_workspace(self) -> None:
        app_before = tree(self.app)
        self.assertEqual(json.loads(self.mt("list", "--json").stdout), [], "demo cards leaked in")

        added = self.mt("add", "Data task", "--json")
        self.assertIn(f"Workspace: {self.data}", added.stderr)
        ref = json.loads(added.stdout)["ref"]
        self.mt("add", "Second task")
        self.mt("note", "Data note")
        self.mt("edit", ref, "--priority", "high")
        self.mt("comment", ref, "-m", "Progress")
        self.mt("focus", "--set", ref)
        self.mt("time", "start", ref)
        self.mt("time", "stop")
        self.mt("draft", "create", "note", "--title", "Draft note", "--file", "-", input_text="Body\n")
        self.mt("validate")
        self.mt("done", ref)
        self.mt("view", "--name", "notes", "--kind", "note")
        view = self.data / ".mt" / "views" / "notes"
        self.assertEqual(len(list((view / "cards").glob("*.md"))), 1)
        self.assertIn("default_priority: low", (view / "mt-policy.yaml").read_text(encoding="utf-8"))
        own_env = {key: value for key, value in self.env.items() if key != "MT_WORKSPACE"}
        listed = subprocess.run([str(view / "mt"), "list", "--all", "--json"], env=own_env, text=True,
                                capture_output=True, check=True)
        self.assertEqual(len(json.loads(listed.stdout)), 1, "the view did not use its own cards")
        # A view stays on its own cards even when it inherits MT_WORKSPACE, directly or through mt run.
        subprocess.run([str(view / "mt"), "add", "In view"], env=self.env, capture_output=True, check=True)
        self.mt("run", str(view / "mt"), "add", "Also in view")
        self.assertEqual(len(list((view / "cards").glob("*.md"))), 3)

        second = json.loads(self.mt("read", "Second task", "--json").stdout)
        record = second[0] if isinstance(second, list) else second
        self.assertEqual(record["properties"]["priority"], "low", "the workspace policy was not used")
        self.assertEqual(len(list((self.data / "cards").glob("*.md"))), 3)
        self.assertTrue((self.data / ".mt").is_dir())
        self.assertTrue(any((self.data / "drafts").glob("*.md")))
        self.assertEqual(tree(self.app), app_before, "the application folder changed")
        self.assertFalse((self.app / ".mt").exists())

    def test_handing_off_to_another_application_drops_the_workspace(self) -> None:
        other = self.home / "other"
        shutil.copytree(SOURCE_ROOT, other, ignore=IGNORED)
        (other / "cards").mkdir()
        policy = other / "mt-policy.yaml"
        policy.write_text(policy.read_text(encoding="utf-8").replace("demo_workspace: true", "demo_workspace: false"),
                          encoding="utf-8")
        self.mt("-R", str(other), "add", "Other task")
        self.assertEqual(len(list((other / "cards").glob("*.md"))), 1)
        self.assertEqual(tree(self.data), {})

    def test_invalid_workspace_is_refused_before_any_write(self) -> None:
        full = self.home / "full"
        (full / "cards").mkdir(parents=True)
        shutil.copy2(self.data / "mt-policy.yaml", full / "mt-policy.yaml")
        (full / "mt").write_text("#!/bin/sh\n", encoding="utf-8")
        for value in ("relative/path", str(self.home / "missing"), str(self.home), str(full)):
            with self.subTest(value=value):
                result = self.mt("add", "Lost task", env={**self.env, "MT_WORKSPACE": value}, ok=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("MT_WORKSPACE", result.stderr)
        self.assertEqual(tree(self.app), {})

    def test_repo_selects_a_data_workspace_by_path_or_alias(self) -> None:
        plain = {key: value for key, value in self.env.items() if key != "MT_WORKSPACE"}
        added = self.mt("-R", str(self.data), "add", "Via path", env=plain)
        self.assertIn(f"Workspace: {self.data}", added.stderr)
        self.assertIn(f"Application: {self.app}", added.stderr)
        self.mt("repo", "add", "proj", str(self.data), env=plain)
        listed = self.mt("-R", "proj", "list", "--json", env=plain)
        self.assertEqual([card["title"] for card in json.loads(listed.stdout)], ["Via path"])
        self.assertEqual(tree(self.app), {})

        neither = self.home / "neither"
        (neither / "cards").mkdir(parents=True)
        for arguments in (("-R", str(neither), "list"), ("repo", "add", "neither", str(neither))):
            with self.subTest(arguments=arguments):
                result = self.mt(*arguments, env=plain, ok=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not a microtask workspace", result.stderr)

    def test_update_refuses_to_run_for_a_data_workspace(self) -> None:
        plain = {key: value for key, value in self.env.items() if key != "MT_WORKSPACE"}
        for arguments, env in ((("update", "--check"), self.env),
                               (("-R", str(self.data), "update", "--check"), plain)):
            with self.subTest(arguments=arguments):
                result = self.mt(*arguments, env=env, ok=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(self.app), result.stderr)
                self.assertIn("without -R", result.stderr)


class DataWorkspaceInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.app = self.home / "app"
        shutil.copytree(SOURCE_ROOT, self.app, ignore=IGNORED)
        (self.app / "cards").mkdir()
        policy = self.app / "mt-policy.yaml"
        policy.write_text(policy.read_text(encoding="utf-8").replace("default_priority: none", "default_priority: low"),
                          encoding="utf-8")
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {"MT_WORKSPACE", "MT_PROJECT", "MT_TOPIC"} and not key.startswith("GIT_")}
        self.env.update({"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
                         "MT_USERNAME": "tester", "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.invalid",
                         "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.invalid"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mt(self, *arguments: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([str(self.app / "mt"), *arguments], cwd=self.home, env=self.env,
                                text=True, capture_output=True, check=False)
        if ok:
            self.assertEqual(result.returncode, 0, f"mt {' '.join(arguments)}\n{result.stdout}{result.stderr}")
        return result

    def git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(root), *arguments], env=self.env, text=True,
                              capture_output=True, check=True).stdout

    def test_init_data_creates_a_synced_workspace_this_mt_runs(self) -> None:
        origin = self.home / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], env=self.env, check=True)
        data = self.home / "project tasks"
        created = self.mt("init", str(data), str(origin), "--data")
        self.assertIn("mt repo add NAME", created.stdout)
        self.assertIn(str(data), created.stdout)

        self.assertFalse((data / "mt").exists())
        self.assertEqual((data / "mt-policy.yaml").read_text(encoding="utf-8"),
                         (self.app / "mt-policy.yaml").read_text(encoding="utf-8"))
        self.assertIn(".mt/", (data / ".gitignore").read_text(encoding="utf-8"))
        self.assertEqual(self.git(data, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.git(data, "remote", "get-url", "origin").strip(), str(origin))

        self.mt("-R", str(data), "add", "Project task")
        self.mt("-R", str(data), "sync")
        pushed = self.git(origin, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("mt-policy.yaml", pushed)
        self.assertEqual(sum(name.startswith("cards/") and name.endswith(".md") for name in pushed.splitlines()), 1)
        self.assertEqual(tree(self.app), {})

        again = self.mt("init", str(data), "--data")
        self.assertIn("already", again.stdout)

    def test_init_data_refuses_other_paths_and_options(self) -> None:
        existing = self.home / "existing"
        existing.mkdir()
        for arguments in (("init", str(existing), "--data"), ("init", str(self.home / "x"), "--data", "--local")):
            with self.subTest(arguments=arguments):
                result = self.mt(*arguments, ok=False)
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "x").exists())

if __name__ == "__main__":
    unittest.main()
