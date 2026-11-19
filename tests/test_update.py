"""mt update: replace the application from a release tarball and leave the workspace alone."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from support import make_release


SOURCE_ROOT = Path(__file__).resolve().parent.parent
OLD, NEW = "v0.1.0", "v0.2.0"


def snapshot(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        old = make_release(self.root / "old.tar.gz", OLD)
        shutil.unpack_archive(old, self.root / "unpacked")
        self.app = self.root / "microtask"
        shutil.move(str(next((self.root / "unpacked").iterdir())), self.app)
        (self.app / "VERSION").write_text(OLD + "\n")
        (self.app / ".release-files").write_text(
            "".join(f"{item.name}\n" for item in sorted(self.app.iterdir())
                    if item.name not in {"VERSION", "deploy"}) + "old-only.txt\nold-folder\n")
        (self.app / "old-only.txt").write_text("gone in the new release\n")
        (self.app / "old-folder").mkdir()
        (self.app / "old-folder" / "file.txt").write_text("gone too\n")
        (self.app / "README.md").write_text("# Old readme\n")
        (self.app / "deploy" / "add-user.sh").write_text("#!/bin/sh\nmine\n")
        self.workspace = self.app / "workspace"
        subprocess.run(["python3", str(self.app / "scripts" / "workspace.py"), "create", str(self.workspace)],
                       check=True, capture_output=True)
        subprocess.run([str(self.app / "mt"), "add", "Keep me"], check=True, capture_output=True,
                       stdin=subprocess.DEVNULL, env={**os.environ, "MT_WORKSPACE": str(self.workspace)})
        self.release = make_release(
            self.root / "new.tar.gz", NEW, drop=("README.md", "deploy"),
            extra={"README.md": b"# New readme\n", "NEW-FILE.md": b"added\n",
                   "deploy/add-user.sh": b"#!/bin/sh\nchanged upstream\n", "deploy/new-file.txt": b"new\n"})
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        self.env.update({"HOME": str(self.root), "MT_RELEASE_TAG": NEW, "MT_ARCHIVE_URL": self.release.as_uri()})

    def update(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.app / "mt"), "update", *arguments], cwd=self.root, env=env or self.env,
                              text=True, capture_output=True, check=False, stdin=subprocess.DEVNULL)

    def test_update_replaces_adds_and_removes_application_entries(self) -> None:
        workspace_before = snapshot(self.workspace)

        result = self.update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Updated microtask: {OLD} -> {NEW}", result.stdout)
        self.assertEqual((self.app / "README.md").read_text(), "# New readme\n")
        self.assertTrue((self.app / "NEW-FILE.md").is_file())
        self.assertFalse((self.app / "old-only.txt").exists())
        self.assertFalse((self.app / "old-folder").exists())
        self.assertEqual((self.app / "VERSION").read_text(), NEW + "\n")
        self.assertFalse((self.app / ".update-tmp").exists())
        self.assertTrue(os.access(self.app / "mt", os.X_OK))
        self.assertEqual((self.app / "deploy" / "add-user.sh").read_text(), "#!/bin/sh\nmine\n")
        self.assertFalse((self.app / "deploy" / "new-file.txt").exists())
        self.assertEqual(snapshot(self.workspace), workspace_before)
        self.assertIn("Workspace checked", result.stdout)

        repeat = self.update()
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertIn(f"Already up to date ({NEW})", repeat.stdout)

    def test_check_reports_versions_and_entries_without_changing_anything(self) -> None:
        before = snapshot(self.app)

        text = self.update("--check")
        as_json = self.update("--check", "--json")
        forced = self.update("--check", "--force")

        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn(f"Current version: {OLD}", text.stdout)
        self.assertIn(f"Latest version:  {NEW}", text.stdout)
        self.assertIn("NEW-FILE.md", text.stdout)
        self.assertIn("old-only.txt", text.stdout)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        report = json.loads(as_json.stdout)
        self.assertEqual(report["current_version"], OLD)
        self.assertEqual(report["latest_version"], NEW)
        self.assertFalse(report["up_to_date"])
        self.assertEqual(report["added"], ["NEW-FILE.md"])
        self.assertEqual(sorted(report["removed"]), ["old-folder", "old-only.txt"])
        self.assertIn("README.md", report["replaced"])
        self.assertNotIn("deploy", report["added"] + report["removed"] + report["replaced"])
        self.assertEqual(snapshot(self.app), before)
        self.assertFalse((self.app / ".update-tmp").exists())

        self.assertNotEqual(self.update("--json").returncode, 0)

    def test_files_added_by_hand_are_never_removed(self) -> None:
        (self.app / ".env").write_text("SECRET=1\n")
        (self.app / "my-notes").mkdir()
        (self.app / "my-notes" / "idea.txt").write_text("mine\n")

        report = json.loads(self.update("--check", "--json").stdout)
        self.assertEqual(report["kept"], [".env", "my-notes"])
        self.assertNotIn(".env", report["removed"])

        result = self.update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / ".env").read_text(), "SECRET=1\n")
        self.assertEqual((self.app / "my-notes" / "idea.txt").read_text(), "mine\n")
        self.assertFalse((self.app / "old-only.txt").exists())
        recorded = (self.app / ".release-files").read_text().split()
        self.assertIn("NEW-FILE.md", recorded)
        self.assertNotIn("old-only.txt", recorded)
        self.assertNotIn(".env", recorded)

    def test_without_a_record_nothing_is_removed(self) -> None:
        (self.app / ".release-files").unlink()

        result = self.update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / "old-only.txt").is_file())
        self.assertEqual((self.app / "README.md").read_text(), "# New readme\n")
        self.assertTrue((self.app / ".release-files").is_file())

    def test_missing_version_file_reports_unknown(self) -> None:
        (self.app / "VERSION").unlink()
        report = json.loads(self.update("--check", "--json").stdout)
        self.assertEqual(report["current_version"], "unknown")

    def test_a_git_clone_is_refused(self) -> None:
        (self.app / ".git").mkdir()
        before = snapshot(self.app)

        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git pull", result.stderr)
        self.assertEqual(snapshot(self.app), before)

    def test_a_hosted_version_folder_is_pointed_to_upgrade_sh(self) -> None:
        (self.app / ".git").mkdir()
        (self.app / ".mt-hosted").write_text("hosted\n")
        before = snapshot(self.app)

        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deploy/upgrade.sh", result.stderr)
        self.assertNotIn("git pull", result.stderr)
        self.assertEqual(snapshot(self.app), before)

    def test_an_old_style_workspace_is_refused_until_migrated(self) -> None:
        shutil.rmtree(self.workspace)
        (self.app / "cards").mkdir()
        shutil.copy2(self.app / "templates" / "settings.yaml", self.app / "settings.yaml")

        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mt migrate", result.stderr)
        self.assertTrue((self.app / "cards").is_dir())
        self.assertEqual((self.app / "VERSION").read_text(), OLD + "\n")

    def test_a_workspace_elsewhere_is_untouched(self) -> None:
        elsewhere = self.root / "elsewhere"
        subprocess.run(["python3", str(self.app / "scripts" / "workspace.py"), "create", str(elsewhere)],
                       check=True, capture_output=True)
        (elsewhere / "cards" / "note.md").write_text("keep\n")
        before, inside = snapshot(elsewhere), snapshot(self.workspace)

        result = self.update(env={**self.env, "MT_WORKSPACE": str(elsewhere)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(elsewhere), before)
        self.assertEqual(snapshot(self.workspace), inside)

    def test_the_workspace_skill_copies_are_refreshed_and_cards_are_untouched(self) -> None:
        env = {**self.env, "MT_WORKSPACE": str(self.workspace)}
        subprocess.run([str(self.app / "mt"), "skills", "install", "--workspace"], check=True,
                       capture_output=True, env=env, stdin=subprocess.DEVNULL)
        skill = self.workspace / ".agents" / "skills" / "microtask" / "SKILL.md"
        skill.write_text(skill.read_text() + "stale\n")
        cards = snapshot(self.workspace / "cards")

        result = self.update(env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Refreshed the workspace's agent skills.", result.stdout)
        self.assertNotIn("stale", skill.read_text())
        self.assertEqual(snapshot(self.workspace / "cards"), cards)

    def test_problems_in_the_workspace_are_reported_but_do_not_undo_the_update(self) -> None:
        (self.workspace / "cards" / "bad.md").write_text("not a card\n")

        result = self.update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("warning", result.stderr)
        self.assertEqual((self.app / "VERSION").read_text(), NEW + "\n")

    def test_a_release_that_does_not_start_is_rolled_back(self) -> None:
        broken = make_release(self.root / "broken.tar.gz", NEW, drop=("scripts",),
                              extra={"scripts/cli.py": b"raise SystemExit(7)\n"})
        before = snapshot(self.app)

        result = self.update(env={**self.env, "MT_ARCHIVE_URL": broken.as_uri()})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not start", result.stderr)
        self.assertEqual(snapshot(self.app), before)
        self.assertFalse((self.app / ".update-tmp").exists())

    def test_unsafe_or_foreign_archives_change_nothing(self) -> None:
        unsafe = make_release(self.root / "unsafe.tar.gz", NEW,
                              raw_members=[("microtask-0.2.0/../../escape.txt", b"x\n", 0o644)])
        foreign = self.root / "foreign.tar.gz"
        shutil.make_archive(str(self.root / "foreign"), "gztar", root_dir=self.root, base_dir="old.tar.gz")
        before = snapshot(self.app)
        for archive in (unsafe, foreign, self.root / "missing.tar.gz"):
            with self.subTest(archive=archive.name):
                result = self.update(env={**self.env, "MT_ARCHIVE_URL": archive.as_uri()})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(snapshot(self.app), before)
        self.assertFalse((self.root.parent / "escape.txt").exists())

    # Stepped updates. The archive is the same for every tag; only the tag names and VERSION differ.

    def releases_file(self, *entries: object) -> str:
        """A fake releases list. A string is a published tag; a dict is used as given."""
        listed = [entry if isinstance(entry, dict) else {"tag_name": entry, "draft": False, "prerelease": False}
                  for entry in entries]
        path = self.root / "releases.json"
        path.write_text(json.dumps(listed))
        return path.as_uri()

    def stepped_env(self, url: str) -> dict[str, str]:
        env = {key: value for key, value in self.env.items() if key != "MT_RELEASE_TAG"}
        env["MT_RELEASES_URL"] = url
        return env

    def stepped(self, current: str | None, entries: tuple[object, ...], *arguments: str):
        if current is None:
            (self.app / "VERSION").unlink()
        else:
            (self.app / "VERSION").write_text(current + "\n")
        return self.update(*arguments, env=self.stepped_env(self.releases_file(*entries)))

    def installed(self) -> str:
        return (self.app / "VERSION").read_text().strip()

    def test_a_patch_in_the_current_minor_comes_first(self) -> None:
        result = self.stepped("v0.1.0", ("v0.1.0", "v0.1.1", "v0.1.2", "v0.2.0"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed(), "v0.1.2")
        self.assertIn("Run 'mt update' again to continue to v0.2.0", result.stdout)

    def test_a_jump_stops_at_the_newest_patch_of_the_next_minor(self) -> None:
        entries = ("v0.1.5", "v0.2.0", "v0.2.1", "v0.3.0")

        result = self.stepped("v0.1.5", entries)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed(), "v0.2.1")
        self.assertIn("Run 'mt update' again to continue to v0.3.0", result.stdout)

        last = self.stepped("v0.2.1", entries)
        self.assertEqual(last.returncode, 0, last.stderr)
        self.assertEqual(self.installed(), "v0.3.0")
        self.assertNotIn("again", last.stdout)

    def test_the_next_minor_is_the_smallest_later_one_even_across_a_major(self) -> None:
        result = self.stepped("v0.9.0", ("v0.9.0", "v1.2.0", "v1.0.0", "v1.0.3", "v2.0.0"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed(), "v1.0.3")
        self.assertIn("continue to v1.2.0", result.stdout)

    def test_the_newest_patch_with_nothing_newer_is_up_to_date(self) -> None:
        (self.app / "VERSION").write_text("v0.2.1\n")
        before = snapshot(self.app)

        result = self.stepped("v0.2.1", ("v0.1.0", "v0.2.0", "v0.2.1"))
        report = json.loads(self.stepped("v0.2.1", ("v0.2.1",), "--check", "--json").stdout)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already up to date (v0.2.1)", result.stdout)
        self.assertEqual(snapshot(self.app), before)
        self.assertTrue(report["up_to_date"])
        self.assertIsNone(report["target_version"])
        self.assertFalse(report["steps_remaining"])

    def test_an_unknown_version_goes_to_the_latest_release_with_a_note(self) -> None:
        for current in (None, "dev-build"):
            with self.subTest(current=current):
                result = self.stepped(current, ("v0.1.0", "v0.2.0", "v0.3.0"))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.installed(), "v0.3.0")
                self.assertIn("unknown", result.stdout)
                self.assertIn("no stepping", result.stdout)
                self.assertNotIn("again", result.stdout)

    def test_drafts_prereleases_and_unparsable_tags_are_ignored(self) -> None:
        entries = (
            "v0.1.0",
            {"tag_name": "v0.1.1", "draft": False, "prerelease": False},
            {"tag_name": "v0.1.9", "draft": True, "prerelease": False},
            {"tag_name": "v0.2.0", "draft": False, "prerelease": True},
            "nightly", "v0.3", "release-0.4.0", {"name": "no tag"}, "v0.1.1-rc1",
        )

        report = json.loads(self.stepped("v0.1.0", entries, "--check", "--json").stdout)

        self.assertEqual(report["target_version"], "v0.1.1")
        self.assertEqual(report["latest_version"], "v0.1.1")
        self.assertFalse(report["steps_remaining"])

    def test_check_reports_the_step_the_latest_and_the_remaining_steps(self) -> None:
        (self.app / "VERSION").write_text("v0.1.5\n")
        before = snapshot(self.app)

        text = self.stepped("v0.1.5", ("v0.1.5", "v0.2.0", "v0.2.1", "v0.3.0"), "--check")
        report = json.loads(self.stepped("v0.1.5", ("v0.1.5", "v0.2.0", "v0.2.1", "v0.3.0"),
                                         "--check", "--json").stdout)

        self.assertIn("Target version:  v0.2.1", text.stdout)
        self.assertIn("Latest version:  v0.3.0", text.stdout)
        self.assertEqual(report["current_version"], "v0.1.5")
        self.assertEqual(report["target_version"], "v0.2.1")
        self.assertEqual(report["latest_version"], "v0.3.0")
        self.assertTrue(report["steps_remaining"])
        self.assertFalse(report["up_to_date"])
        self.assertIn("NEW-FILE.md", report["added"])
        self.assertEqual(snapshot(self.app), before)

        final = json.loads(self.stepped("v0.1.5", ("v0.1.5", "v0.2.0"), "--check", "--json").stdout)
        self.assertEqual((final["target_version"], final["latest_version"], final["steps_remaining"]),
                         ("v0.2.0", "v0.2.0", False))

    def migrate_workspace_back(self) -> None:
        (self.workspace / "settings.yaml").rename(self.workspace / "mt-policy.yaml")

    def test_a_new_minor_needs_a_migrated_workspace_but_a_patch_does_not(self) -> None:
        self.migrate_workspace_back()
        before = snapshot(self.app)

        refused = self.stepped("v0.1.0", ("v0.1.0", "v0.2.0"))
        refused_check = self.stepped("v0.1.0", ("v0.1.0", "v0.2.0"), "--check")
        patch = self.stepped("v0.1.0", ("v0.1.0", "v0.1.1", "v0.2.0"))

        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("mt migrate", refused.stderr)
        self.assertNotEqual(refused_check.returncode, 0)
        self.assertIn("mt migrate", refused_check.stderr)
        self.assertEqual(patch.returncode, 0, patch.stderr)
        self.assertEqual(self.installed(), "v0.1.1")
        self.assertTrue((self.workspace / "mt-policy.yaml").is_file())
        self.assertNotEqual(snapshot(self.app), before)

    def test_the_migration_check_looks_at_the_workspace_that_mt_workspace_names(self) -> None:
        elsewhere = self.root / "elsewhere"
        shutil.copytree(self.workspace, elsewhere)
        (elsewhere / "settings.yaml").rename(elsewhere / "mt-policy.yaml")
        env = {**self.stepped_env(self.releases_file("v0.1.0", "v0.2.0")), "MT_WORKSPACE": str(elsewhere)}
        (self.app / "VERSION").write_text("v0.1.0\n")

        result = self.update(env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(elsewhere), result.stderr)
        self.assertIn("mt migrate", result.stderr)

    def test_a_forced_tag_is_installed_as_given(self) -> None:
        (self.app / "VERSION").write_text("v0.1.0\n")
        env = {**self.stepped_env((self.root / "missing.json").as_uri()), "MT_RELEASE_TAG": "v0.5.0"}

        result = self.update(env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed(), "v0.5.0")
        self.assertNotIn("again", result.stdout)

    def serve(self, handler: type[BaseHTTPRequestHandler]) -> str:
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_releases_are_read_from_every_page_with_the_expected_headers(self) -> None:
        seen: list[dict[str, str]] = []
        base: list[str] = []

        class Pages(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append({"agent": self.headers.get("User-Agent", ""), "accept": self.headers.get("Accept", "")})
                second = self.path.startswith("/second")
                body = json.dumps([{"tag_name": "v0.3.0" if second else "v0.1.1", "draft": False,
                                    "prerelease": False}]).encode()
                self.send_response(200)
                if not second:
                    self.send_header("Link", f'<{base[0]}/second>; rel="next", <{base[0]}/last>; rel="last"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments: object) -> None:
                pass

        base.append(self.serve(Pages))
        (self.app / "VERSION").write_text("v0.1.0\n")

        report = json.loads(self.update("--check", "--json", env=self.stepped_env(base[0] + "/first")).stdout)

        self.assertEqual((report["target_version"], report["latest_version"]), ("v0.1.1", "v0.3.0"))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["agent"], "microtask-update")
        self.assertEqual(seen[0]["accept"], "application/vnd.github+json")

    def test_a_rate_limit_suggests_trying_later_or_choosing_a_tag(self) -> None:
        class Limited(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *arguments: object) -> None:
                pass

        before = snapshot(self.app)

        result = self.update(env=self.stepped_env(self.serve(Limited) + "/releases"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("limiting requests", result.stderr)
        self.assertIn("later", result.stderr)
        self.assertIn("MT_RELEASE_TAG", result.stderr)
        self.assertEqual(snapshot(self.app), before)


if __name__ == "__main__":
    unittest.main()
