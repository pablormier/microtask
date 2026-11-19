"""Which folder holds the cards: MT_WORKSPACE, workspace/ in the app folder, an old-style root, or a new workspace/."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

from support import ROOT, legacy_workspace, make_release

sys.path.insert(0, str(ROOT / "scripts"))
import workspace as workspace_module  # noqa: E402


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        archive = make_release(self.root / "release.tar.gz", "v0.1.0")
        shutil.unpack_archive(archive, self.root / "unpacked")
        self.app = self.root / "app"
        shutil.move(str(next((self.root / "unpacked").iterdir())), self.app)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        self.env.update({"HOME": str(self.root), "MT_USERNAME": "tester"})
        self.default = self.app / "workspace"

    def mt(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.app / "mt"), *arguments], cwd=self.root, env=env or self.env, text=True,
                              capture_output=True, check=False, stdin=subprocess.DEVNULL)

    def data_workspace(self, name: str) -> Path:
        path = self.root / name
        workspace_module.create_workspace(path, self.app)
        return path

    def titles(self, **env: str) -> list[str]:
        result = self.mt("list", "--json", env={**self.env, **env})
        self.assertEqual(result.returncode, 0, result.stderr)
        return [card["title"] for card in json.loads(result.stdout)]

    def init(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = self.mt("init", *arguments, env=env)
        return result

    def test_without_a_workspace_commands_stop_and_point_to_mt_init(self) -> None:
        for arguments in (("validate",), ("add", "Lost"), ("list",)):
            with self.subTest(arguments=arguments):
                result = self.mt(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("no workspace yet", result.stderr)
                self.assertIn(f"{self.app / 'mt'} init", result.stderr)
        self.assertFalse(self.default.exists())

    def test_a_hosted_version_folder_points_to_srv_mt_instead_of_init(self) -> None:
        (self.app / ".mt-hosted").write_text("hosted\n")

        result = self.mt("list")

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("/srv/mt", result.stderr)
        self.assertIn("-R", result.stderr)
        self.assertNotIn("init", result.stderr)
        self.assertFalse(self.default.exists())

    def test_mt_init_creates_the_workspace_from_the_templates_and_prints_its_path(self) -> None:
        first = self.init()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(f"Created workspace: {self.default}", first.stdout)
        self.assertEqual((self.default / "settings.yaml").read_bytes(),
                         (self.app / "templates" / "settings.yaml").read_bytes())
        self.assertEqual((self.default / ".gitignore").read_bytes(),
                         (self.app / "templates" / "workspace.gitignore").read_bytes())
        self.assertIn(".mt/", (self.default / ".gitignore").read_text())
        self.assertIn("settings.local.yaml", (self.default / ".gitignore").read_text())
        self.assertTrue((self.default / "cards").is_dir())
        self.assertTrue((self.default / "drafts").is_dir())
        self.assertFalse((self.default / "mt").exists())
        self.assertEqual(self.mt("validate").returncode, 0)

        second = self.init()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn(f"Workspace already exists: {self.default}", second.stdout)

    def test_mt_init_works_in_a_git_clone(self) -> None:
        (self.app / ".git").mkdir()

        created = self.init()

        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn(f"Created workspace: {self.default}", created.stdout)

    def test_mt_init_refuses_when_the_workspace_would_be_ignored_or_misplaced(self) -> None:
        elsewhere = self.data_workspace("elsewhere")
        with_env = self.init(env={**self.env, "MT_WORKSPACE": str(elsewhere)})
        self.assertEqual(with_env.returncode, 2)
        self.assertIn("MT_WORKSPACE is set", with_env.stderr)
        self.assertFalse(self.default.exists())

        for flag in ("--local", "--data"):
            self.assertEqual(self.init(flag).returncode, 2)
        self.assertFalse(self.default.exists())

        self.default.write_text("a file\n")
        in_the_way = self.init()
        self.assertEqual(in_the_way.returncode, 1)
        self.assertIn("not a folder", in_the_way.stderr)
        self.default.unlink()

        legacy_workspace(self.app)
        legacy = self.init()
        self.assertEqual(legacy.returncode, 1)
        self.assertIn("mt migrate", legacy.stderr)
        self.assertFalse(self.default.exists())

    def test_locks_and_local_state_live_in_the_workspace(self) -> None:
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual(self.mt("add", "First").returncode, 0)
        self.assertEqual(self.mt("add", "Second").returncode, 0)

        self.assertTrue((self.default / ".mt").is_dir())
        self.assertFalse((self.app / ".mt").exists())
        self.assertEqual(sorted(path.name for path in self.app.iterdir() if path.name.startswith(".mt")), [])

    def test_a_new_workspace_starts_empty_until_the_first_card(self) -> None:
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.mt("add", "Mine").returncode, 0)
        self.assertEqual(self.titles(), ["Mine"])

    def test_mt_workspace_wins_over_everything(self) -> None:
        legacy_workspace(self.app)
        workspace_module.create_workspace(self.default, self.app)
        elsewhere = self.data_workspace("elsewhere")
        self.assertEqual(self.mt("add", "Elsewhere", env={**self.env, "MT_WORKSPACE": str(elsewhere)}).returncode, 0)

        self.assertEqual(len(list((elsewhere / "cards").glob("*.md"))), 1)
        self.assertEqual(list((self.default / "cards").glob("*.md")), [])
        self.assertEqual(list((self.app / "cards").glob("*.md")), [])

    def test_an_invalid_mt_workspace_is_an_error_and_creates_nothing(self) -> None:
        plain = self.root / "plain folder"
        plain.mkdir()
        for value in ("relative/path", str(self.root / "missing"), str(plain)):
            with self.subTest(value=value):
                result = self.mt("add", "Lost", env={**self.env, "MT_WORKSPACE": value})
                self.assertEqual(result.returncode, 2)
                self.assertIn("MT_WORKSPACE", result.stderr)
        self.assertFalse(self.default.exists())
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(list(plain.iterdir()), [])

    def test_the_workspace_folder_comes_before_an_old_style_root(self) -> None:
        legacy_workspace(self.app)
        self.mt("add", "Old style")
        workspace_module.create_workspace(self.default, self.app)

        self.assertNotIn("Old style", self.titles())
        self.assertEqual(self.mt("add", "New style").returncode, 0)
        self.assertEqual(len(list((self.default / "cards").glob("*.md"))), 1)
        self.assertEqual(len(list((self.app / "cards").glob("*.md"))), 1)

    def test_an_old_style_root_keeps_working_and_gets_no_new_workspace(self) -> None:
        legacy_workspace(self.app)

        added = self.mt("add", "Old style")

        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(len(list((self.app / "cards").glob("*.md"))), 1)
        self.assertFalse(self.default.exists())

    def test_commands_that_do_not_need_a_workspace_do_not_create_one(self) -> None:
        for arguments in (("--help",), ("repo", "list"), ("init", str(self.root / "other"), "--data"),
                          ("update", "--check")):
            with self.subTest(arguments=arguments):
                self.mt(*arguments, env={**self.env, "MT_RELEASE_TAG": "v0.0.0",
                                         "MT_ARCHIVE_URL": (self.root / "missing.tar.gz").as_uri()})
                self.assertFalse(self.default.exists())

    def test_missing_workspace_error_in_process(self) -> None:
        env = {"MT_WORKSPACE": str(self.root / "elsewhere")}
        self.assertIsNone(workspace_module.missing_workspace_error(env, app_root=self.app))
        self.assertEqual(workspace_module.resolve_workspace_root(env, app_root=self.app), self.root / "elsewhere")

        self.assertEqual(workspace_module.resolve_workspace_root({}, app_root=self.app), self.default)
        self.assertIn("mt init", workspace_module.missing_workspace_error({}, app_root=self.app))
        self.assertFalse(self.default.exists())
        workspace_module.create_workspace(self.default, self.app)
        self.assertIsNone(workspace_module.missing_workspace_error({}, app_root=self.app))

    def test_create_workspace_completes_a_folder_without_overwriting_files(self) -> None:
        self.default.mkdir()
        (self.default / "settings.yaml").write_text("statuses: {}\n")
        workspace_module.create_workspace(self.default, self.app)
        self.assertEqual((self.default / "settings.yaml").read_text(), "statuses: {}\n")
        self.assertTrue((self.default / "cards").is_dir())


if __name__ == "__main__":
    unittest.main()
