"""mt skills install/uninstall: microtask's agent skills, set up for another project."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns(".git", ".mt", ".DS_Store", "cards", "drafts")
TARGETS = (".agents/skills", ".claude/skills")


def installed_text(project: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for target in TARGETS
                     for path in sorted((project / target).rglob("*")) if path.is_file())


class SkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.app = self.home / "app"
        shutil.copytree(SOURCE_ROOT, self.app, ignore=IGNORED)
        (self.app / "cards").mkdir()
        # Skills are discovered, not listed: an extra one must be installed too.
        extra = self.app / ".agents" / "skills" / "extra"
        extra.mkdir()
        (extra / "SKILL.md").write_text("---\nname: extra\ndescription: Extra skill.\n---\n\nRun `./mt list`.\n",
                                        encoding="utf-8")
        self.names = sorted(path.parent.name for path in (self.app / ".agents" / "skills").glob("*/SKILL.md"))
        self.project = self.home / "my app"
        mine = self.project / ".claude" / "skills" / "mine"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("---\nname: mine\ndescription: The user's own skill.\n---\n", encoding="utf-8")
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {"MT_WORKSPACE", "MT_PROJECT", "MT_TOPIC"}}
        self.env.update({"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
                         "PATH": f"{self.app}{os.pathsep}{os.environ['PATH']}",
                         "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.invalid",
                         "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.invalid"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mt(self, *arguments: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["mt", *arguments], cwd=self.project, env=self.env,
                                text=True, capture_output=True, check=False)
        if ok:
            self.assertEqual(result.returncode, 0, f"mt {' '.join(arguments)}\n{result.stdout}{result.stderr}")
        return result

    def assert_installed(self, invocation: str) -> str:
        for target in TARGETS:
            installed = sorted(path.parent.name for path in (self.project / target).glob("*/SKILL.md")
                               if path.parent.name != "mine")
            self.assertEqual(installed, self.names, target)
            for name in self.names:
                skill = (self.project / target / name / "SKILL.md").read_text(encoding="utf-8")
                frontmatter = skill.split("---\n")[1]
                self.assertIn("installed-by: microtask", frontmatter, name)
        text = installed_text(self.project)
        self.assertNotRegex(text, r"(?<![.\w/])\./mt\b")
        # Every mt command was rewritten: none is left that would reach the mt on PATH unscoped.
        # (mt repo add is advice for the user, run with their own mt.)
        leftover = text.replace(invocation, "INVOCATION").replace("`mt repo add", "`MT-REPO-ADD")
        self.assertNotRegex(leftover, r"(?<![\w/.-])mt (?=[A-Za-z-])")
        self.assertIn(f"`{invocation} COMMAND`", text)
        self.assertIn(f"{invocation} list ", text)
        self.assertIn(f"$({invocation} scratch)", text)
        return text

    def test_install_for_the_personal_workspace_scoped_to_a_project(self) -> None:
        result = self.mt("skills", "install", "--project", "my app")
        text = self.assert_installed("MT_PROJECT='my app' mt")
        self.assertIn("`Scope:`", text)
        self.assertIn("mine", [path.name for path in (self.project / ".claude" / "skills").iterdir()])

        shutil.rmtree(self.app / ".agents" / "skills" / "extra")
        self.names.remove("extra")
        self.mt("skills", "install", "--project", "my app")
        self.assert_installed("MT_PROJECT='my app' mt")
        self.assertFalse((self.project / ".agents" / "skills" / "extra").exists())

        self.mt("skills", "uninstall")
        self.assertFalse((self.project / ".agents").exists())
        self.assertEqual([path.name for path in (self.project / ".claude" / "skills").iterdir()], ["mine"])
        self.assertIn("Installed", result.stdout)

    def test_unscoped_install_for_the_personal_workspace_warns(self) -> None:
        result = self.mt("skills", "install")
        self.assert_installed("mt")
        self.assertIn("--project", result.stderr)

    def test_install_for_a_data_workspace_uses_its_alias_or_path(self) -> None:
        data = self.home / "tasks"
        self.mt("init", str(data), "--data")
        unnamed = self.mt("-R", str(data), "skills", "install")
        self.assert_installed(f"mt -R {data}")
        self.assertIn("this machine", unnamed.stderr)

        self.mt("repo", "add", "proj", str(data))
        self.mt("-R", "proj", "skills", "install")
        text = self.assert_installed("mt -R proj")
        self.assertNotIn(str(self.home), text)

    def test_existing_skill_with_the_same_name_stops_the_install(self) -> None:
        clash = self.project / ".agents" / "skills" / "microtask"
        clash.mkdir(parents=True)
        (clash / "SKILL.md").write_text("---\nname: microtask\ndescription: Not ours.\n---\n", encoding="utf-8")
        result = self.mt("skills", "install", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(clash), result.stderr)
        self.assertEqual(sorted(path.name for path in (self.project / ".agents" / "skills").iterdir()), ["microtask"])
        self.assertFalse((self.project / ".claude" / "skills" / "extra").exists())


    def test_a_skill_the_marker_cannot_go_into_stops_the_install(self) -> None:
        extra = self.app / ".agents" / "skills" / "extra" / "SKILL.md"
        for text in ("No frontmatter here.\n", "---\nname: extra\nmetadata:\n  owner: me\n---\n"):
            with self.subTest(text=text):
                extra.write_text(text, encoding="utf-8")
                result = self.mt("skills", "install", ok=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(extra), result.stderr)
                self.assertFalse((self.project / ".agents").exists())

if __name__ == "__main__":
    unittest.main()
