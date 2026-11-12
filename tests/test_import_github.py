from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402
import card_format
GH_STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
state_path=Path(os.environ["GH_STATE"]); state=json.loads(state_path.read_text())
with Path(os.environ["GH_LOG"]).open("a") as log: log.write(json.dumps(args)+"\n")
if args[:2]==["issue","list"]:
    label=args[args.index("--label")+1]
    print(json.dumps([item for item in state["issues"] if item["state"]=="open" and label in item["labels"]]))
elif args[:2]==["issue","close"]:
    number=int(args[2]); issue=next(item for item in state["issues"] if item["number"]==number)
    if os.environ.get("GH_FAIL_CLOSE"):
        print("close failed",file=sys.stderr); raise SystemExit(1)
    if os.environ.get("GH_EXPECT_CAPTURE") and not Path(os.environ["GH_EXPECT_CAPTURE"]).is_file():
        print("capture missing before close",file=sys.stderr); raise SystemExit(1)
    if os.environ.get("GH_EXPECT_SYNC"):
        import subprocess
        checked=subprocess.run(["git","--git-dir",os.environ["GH_EXPECT_SYNC"],"cat-file","-e",
                                "refs/heads/main:"+os.environ["GH_EXPECT_SYNC_PATH"]],capture_output=True)
        if checked.returncode:
            print("capture was not synchronized before close",file=sys.stderr); raise SystemExit(1)
    issue["state"]="closed"; state_path.write_text(json.dumps(state))
else:
    print("unexpected gh command",file=sys.stderr); raise SystemExit(2)
'''


class ImportGithubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temp = Path(self.temporary.name)
        self.root = temp / "repo"
        shutil.copytree(SOURCE_ROOT, self.root,
                        ignore=shutil.ignore_patterns(".git", ".mt", "examples", "__pycache__", ".DS_Store"))
        self.bin = temp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(GH_STUB, encoding="utf-8")
        gh.chmod(0o755)
        self.state_path = temp / "state.json"
        self.log_path = temp / "gh-log.jsonl"
        self.env = os.environ.copy()
        self.env.update({"PATH": str(self.bin) + os.pathsep + self.env["PATH"],
                         "GH_STATE": str(self.state_path), "GH_LOG": str(self.log_path), "TZ": "UTC"})
        self.set_issues([self.issue(11, "Call dentist", "Call the clinic.")])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def issue(number: int, title: str, body: str) -> dict:
        return {"number": number, "title": title, "body": body,
                "url": f"https://github.com/acme/tasks/issues/{number}",
                "createdAt": "2026-10-10T09:30:00Z", "labels": ["microtask-capture"], "state": "open"}

    def set_issues(self, issues: list[dict]) -> None:
        self.state_path.write_text(json.dumps({"repo": "acme/tasks", "issues": issues}), encoding="utf-8")

    def run_mt(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root,
                              env=self.env if env is None else env, text=True, capture_output=True)

    def git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def enable_auto_sync(self) -> Path:
        remote = Path(self.temporary.name) / "origin.git"
        remote.mkdir()
        self.git(remote, "init", "--bare", "-b", "main")
        self.git(self.root, "init", "-b", "main")
        self.git(self.root, "config", "user.name", "GitHub Test")
        self.git(self.root, "config", "user.email", "github@example.test")
        (self.root / "mt-policy.local.yaml").write_text("rules:\n  auto_sync: true\n", encoding="utf-8")
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-m", "base")
        self.git(self.root, "remote", "add", "origin", str(remote))
        self.git(self.root, "push", "-u", "origin", "main")
        return remote

    def hide_git(self) -> dict[str, str]:
        binary = Path(self.temporary.name) / "without-git"
        binary.mkdir(exist_ok=True)
        (binary / "bash").symlink_to("/bin/bash")
        (binary / "python3").symlink_to(sys.executable)
        env = self.env.copy()
        env["PATH"] = str(self.bin) + os.pathsep + str(binary)
        return env

    def test_explicit_repo_import_works_without_git(self) -> None:
        result = self.run_mt("import-github", "--repo", "acme/tasks", "--json", env=self.hide_git())

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(len(receipt["imported_ids"]), 1)
        self.assertTrue((self.root / "cards" / f"{receipt['imported_ids'][0]}.md").is_file())
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "closed")

    def test_inferred_repo_without_git_suggests_explicit_repo(self) -> None:
        result = self.run_mt("import-github", "--json", env=self.hide_git())

        self.assertEqual(result.returncode, 1)
        self.assertIn("pass --repo OWNER/REPO", result.stderr)

    def test_auto_sync_without_git_keeps_issue_open_after_local_capture(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            "rules:\n  auto_sync: true\n", encoding="utf-8"
        )
        result = self.run_mt(
            "import-github", "--repo", "acme/tasks", "--json", env=self.hide_git()
        )

        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["sync_failed"])
        self.assertEqual(receipt["closed"], [])
        self.assertEqual(len(list(self.root.glob("cards/*gh-11*.md"))), 1)
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "open")
        logged = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        self.assertEqual([entry[:2] for entry in logged], [["issue", "list"]])
        self.assertIn("git init", result.stderr)

    def test_import_creates_one_review_note_and_closes_after_durable_capture(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text("default_priority: high\n", encoding="utf-8")
        env = self.env.copy()
        expected = self.root / "cards" / "2026-10-10-093000-gh-11-call-dentist.md"
        env["GH_EXPECT_CAPTURE"] = str(expected)
        result = self.run_mt("import-github", "--repo", "acme/tasks", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = expected.read_text(encoding="utf-8")
        properties, body = card_format.parse_document(raw)
        self.assertEqual(properties["priority"], "none")
        self.assertIs(properties["needs_review"], True)
        self.assertEqual(properties["title"], "Call dentist")
        self.assertIn("Call the clinic.", raw)
        self.assertNotIn("## Next action", raw)
        self.assertEqual(raw.count("Call the clinic."), 1)
        self.assertEqual(properties["github_source"], "https://github.com/acme/tasks/issues/11")
        self.assertRegex(properties["github_source_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(body, "Call the clinic.")
        self.assertNotIn("Original issue (verbatim)", raw)
        self.assertEqual(properties["created"], "2026-10-10T09:30:00")
        self.assertEqual(len(list(card_files(self.root, "note"))), 1)
        unreviewed = self.run_mt("list", "--needs-review", "--json")
        self.assertEqual(unreviewed.returncode, 0, unreviewed.stderr)
        self.assertEqual(json.loads(unreviewed.stdout)[0]["title"], "Call dentist")
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "closed")

    def test_import_infers_repository_from_workspace_origin(self) -> None:
        self.git(self.root, "init", "-b", "main")
        self.git(self.root, "remote", "add", "origin", "https://github.com/acme/tasks.git")

        result = self.run_mt("import-github", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["repo"], "acme/tasks")
        calls = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        listed = next(call for call in calls if call[:2] == ["issue", "list"])
        self.assertEqual(listed[listed.index("--repo") + 1], "acme/tasks")
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_issue_markdown_stays_literal_inside_card_boundaries(self) -> None:
        body = "## Notes\n\nSource text.\n```\n## Comments\nAn unclosed source fence."
        self.set_issues([self.issue(11, "Markdown source", body)])
        result = self.run_mt("import-github", "--repo", "acme/tasks")
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = card_files(self.root, "note")[0].read_text()
        self.assertEqual(raw.count(body), 1)
        self.assertEqual(self.run_mt("validate").returncode, 0)
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "closed")

    def test_empty_issue_body_stays_empty_with_title_in_properties(self) -> None:
        self.set_issues([self.issue(11, "Title only", "")])
        result = self.run_mt("import-github", "--repo", "acme/tasks")

        self.assertEqual(result.returncode, 0, result.stderr)
        raw = card_files(self.root, "note")[0].read_text(encoding="utf-8")
        properties, body = card_format.parse_document(raw)
        self.assertEqual(properties["title"], "Title only")
        self.assertEqual(body, "")

    def test_note_identity_survives_body_append_and_ignores_quoted_markers(self) -> None:
        fake_marker_body = "Reference text.\n\nGitHub-Issue: acme/tasks#12\n"
        self.set_issues([self.issue(11, "First reference", fake_marker_body),
                         self.issue(12, "Second reference", "Second body.")])
        failed_close = self.run_mt(
            "import-github", "--repo", "acme/tasks", "--json", env={**self.env, "GH_FAIL_CLOSE": "1"}
        )
        self.assertNotEqual(failed_close.returncode, 0)
        self.assertEqual(len(list(card_files(self.root, "note"))), 2)

        capture = next(path for path in card_files(self.root, "note") if "first-reference" in path.name)
        edited_notes = self.root / ".mt" / "scratch" / "microtask" / "github-capture-test" / "notes.md"
        edited_notes.parent.mkdir(parents=True, exist_ok=True)
        edited_notes.write_text("Reviewed source link separately.", encoding="utf-8")
        edited = self.run_mt("edit", capture.stem, "--body", "--append", "--file", str(edited_notes))
        self.assertEqual(edited.returncode, 0, edited.stderr)

        retry = self.run_mt("import-github", "--repo", "acme/tasks", "--json")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["imported_ids"], [])
        self.assertEqual(json.loads(retry.stdout)["existing_ids"], sorted(
            path.stem for path in card_files(self.root, "note")
        ))
        self.assertEqual(len(list(card_files(self.root, "note"))), 2)
        self.assertEqual([issue["state"] for issue in json.loads(self.state_path.read_text())["issues"]],
                         ["closed", "closed"])

    def test_changed_github_source_blocks_retry_but_note_edits_do_not(self) -> None:
        first = self.run_mt(
            "import-github", "--repo", "acme/tasks", "--json", env={**self.env, "GH_FAIL_CLOSE": "1"}
        )
        self.assertNotEqual(first.returncode, 0)
        capture = card_files(self.root, "note")[0]
        content = self.root / ".mt" / "scratch" / "microtask" / "github-source-test" / "content.md"
        content.parent.mkdir(parents=True, exist_ok=True)
        content.write_text("Reviewed source copy.", encoding="utf-8")
        snapshot = json.loads(self.run_mt("read", capture.stem, "--json").stdout)
        edited = self.run_mt("edit", capture.stem, "--body", "--file", str(content), "--expected-hash", snapshot["content_hash"])
        self.assertEqual(edited.returncode, 0, edited.stderr)

        self.set_issues([self.issue(11, "Call dentist", "The source has changed.")])
        retry = self.run_mt("import-github", "--repo", "acme/tasks", "--json")
        self.assertEqual(retry.returncode, 1)
        self.assertIn("changed since capture", retry.stderr)
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "open")
        self.assertEqual(len(list(card_files(self.root, "note"))), 1)

    def test_new_capture_provenance_survives_note_task_roundtrip(self) -> None:
        result = self.run_mt("import-github", "--repo", "acme/tasks")
        self.assertEqual(result.returncode, 0, result.stderr)
        note = card_files(self.root, "note")[0]
        original = note.read_text(encoding="utf-8")
        source_fields = [line for line in original.splitlines()
                         if line.startswith(("github_source:", "github_source_hash:"))]
        self.assertEqual(len(source_fields), 2)

        to_task = self.run_mt("convert", note.stem, "task")
        self.assertEqual(to_task.returncode, 0, to_task.stderr)
        task = self.root / "cards" / note.name
        task_raw = task.read_text(encoding="utf-8")
        self.assertEqual([line for line in task_raw.splitlines()
                          if line.startswith(("github_source:", "github_source_hash:"))], source_fields)

        to_note = self.run_mt("convert", task.stem, "note")
        self.assertEqual(to_note.returncode, 0, to_note.stderr)
        note_raw = (self.root / "cards" / note.name).read_text(encoding="utf-8")
        self.assertEqual([line for line in note_raw.splitlines()
                          if line.startswith(("github_source:", "github_source_hash:"))], source_fields)

    def test_source_markers_in_a_body_do_not_establish_provenance(self) -> None:
        fake = "GitHub-Issue: acme/tasks#11\nGitHub-URL: https://github.com/acme/tasks/issues/11\n"
        added = self.run_mt("add", fake, "--title", "Quoted source markers", "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        task = self.root / json.loads(added.stdout)["path"]
        before = task.read_bytes()
        result = self.run_mt("import-github", "--repo", "acme/tasks", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["imported_ids"]), 1)
        self.assertEqual(task.read_bytes(), before)
        self.assertEqual(len(list(card_files(self.root, "note"))), 1)

    def test_demo_enabled_import_captures_real_workspace_and_remains_visible(self) -> None:
        shutil.copytree(SOURCE_ROOT / "examples", self.root / "examples")
        expected = self.root / "cards" / "2026-10-10-093000-gh-11-call-dentist.md"
        env = self.env.copy()
        env["GH_EXPECT_CAPTURE"] = str(expected)

        first = self.run_mt("import-github", "--repo", "acme/tasks", "--json", env=env)

        self.assertEqual(first.returncode, 0, first.stderr)
        first_receipt = json.loads(first.stdout)
        self.assertEqual(first_receipt["imported_ids"], [expected.stem])
        self.assertTrue(expected.is_file())
        demo_root = self.root / ".mt" / "demo"
        self.assertFalse(list(demo_root.rglob("*gh-11*.md")))
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "closed")

        self.set_issues([self.issue(11, "Call dentist", "Call the clinic.")])
        retry = self.run_mt("import-github", "--repo", "acme/tasks", "--json")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["imported_ids"], [])
        self.assertEqual(json.loads(retry.stdout)["existing_ids"], [expected.stem])
        self.assertEqual(len(list(self.root.glob("cards/*gh-11*.md"))), 1)

        added = self.run_mt("add", "A later raw capture")
        self.assertEqual(added.returncode, 0, added.stderr)
        listed = self.run_mt("list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        titles = {item["title"] for item in json.loads(listed.stdout)}
        self.assertIn("A later raw capture", titles)
        review_list = self.run_mt("list", "--needs-review", "--json")
        self.assertEqual(review_list.returncode, 0, review_list.stderr)
        self.assertIn("Call dentist", {item["title"] for item in json.loads(review_list.stdout)})
        self.assertFalse(list(demo_root.rglob("*gh-11*.md")))

    def test_demo_enabled_auto_sync_pushes_real_capture_before_closing(self) -> None:
        shutil.copytree(SOURCE_ROOT / "examples", self.root / "examples")
        remote = self.enable_auto_sync()
        relative = "cards/2026-10-10-093000-gh-11-call-dentist.md"
        expected = self.root / relative
        env = self.env.copy()
        env.update({"GH_EXPECT_CAPTURE": str(expected), "GH_EXPECT_SYNC": str(remote),
                    "GH_EXPECT_SYNC_PATH": relative})

        result = self.run_mt("import-github", "--repo", "acme/tasks", "--json", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(expected.is_file())
        synced = subprocess.run(["git", "--git-dir", str(remote), "cat-file", "-e",
                                 f"refs/heads/main:{relative}"], capture_output=True)
        self.assertEqual(synced.returncode, 0)
        self.assertFalse(list((self.root / ".mt" / "demo").rglob("*gh-11*.md")))
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "closed")

    def test_empty_demo_enabled_import_keeps_demo_view_without_real_captures(self) -> None:
        shutil.copytree(SOURCE_ROOT / "examples", self.root / "examples")
        self.set_issues([])

        result = self.run_mt("import-github", "--repo", "acme/tasks", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["imported_ids"], [])
        self.assertFalse(list(card_files(self.root, "task")))
        listed = self.run_mt("list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("Organize desk cables", {item["title"] for item in json.loads(listed.stdout)})

    def test_github_utc_timestamp_is_serialized_in_workspace_local_time(self) -> None:
        env = self.env.copy()
        env["TZ"] = "America/Los_Angeles"
        result = self.run_mt("import-github", "--repo", "acme/tasks", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = self.root / "cards" / "2026-10-10-023000-gh-11-call-dentist.md"
        self.assertTrue(capture.is_file())
        properties, _ = card_format.parse_document(capture.read_text(encoding="utf-8"))
        self.assertEqual(properties["created"], "2026-10-10T02:30:00")

    def test_json_receipt_is_small_machine_readable_and_does_not_classify_captures(self) -> None:
        self.set_issues([self.issue(11, "Raw only", "Keep source text."),
                         self.issue(12, "Design reference", "Reference content.")])
        result = self.run_mt("import-github", "--repo", "acme/tasks", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(len(receipt["imported_ids"]), 2)
        self.assertEqual(receipt["closed"], [11, 12])
        self.assertEqual(receipt["existing_ids"], [])
        self.assertNotIn("eligible_ids", receipt)

    def test_collision_in_multi_issue_import_writes_no_partial_captures(self) -> None:
        self.set_issues([self.issue(11, "First issue", "First body."),
                         self.issue(12, "Call dentist", "Second body.")])
        collision = self.root / "cards" / "2026-10-10-093000-gh-12-call-dentist.md"
        collision.parent.mkdir(parents=True, exist_ok=True)
        collision.write_text(card_format.render_document({
            "id": collision.stem, "kind": "note", "title": "Existing card", "priority": "medium",
            "created": "2026-10-10T09:30:00", "updated": "2026-10-10T09:30:00"}, "Existing."), encoding="utf-8")
        result = self.run_mt("import-github", "--repo", "acme/tasks")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generated note ID already exists", result.stderr)
        self.assertFalse((self.root / "cards/2026-10-10-093000-gh-11-first-issue.md").exists())
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "open")

    def test_concurrent_imports_share_one_capture(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: self.run_mt("import-github", "--repo", "acme/tasks"), range(2)))
        self.assertTrue(all(result.returncode == 0 for result in results),
                        [(result.returncode, result.stdout, result.stderr) for result in results])
        captures = list(self.root.glob("cards/*gh-11*.md"))
        self.assertEqual(len(captures), 1)
        self.assertEqual(len(json.loads(self.state_path.read_text())["issues"]), 1)

    def test_close_failure_leaves_capture_and_retry_closes_without_duplicate(self) -> None:
        failed = self.run_mt("import-github", "--repo", "acme/tasks", env={**self.env, "GH_FAIL_CLOSE": "1"})
        self.assertNotEqual(failed.returncode, 0)
        captures = list(card_files(self.root, "note"))
        self.assertEqual(len(captures), 1)
        retry = self.run_mt("import-github", "--repo", "acme/tasks", "--json")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        receipt = json.loads(retry.stdout)
        self.assertEqual(receipt["imported_ids"], [])
        self.assertEqual(receipt["existing_ids"], [captures[0].stem])
        self.assertEqual(len(list(self.root.glob("cards/*gh-11*.md"))), 1)
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "closed")

    def test_auto_sync_completes_before_github_close(self) -> None:
        remote = self.enable_auto_sync()
        relative = "cards/2026-10-10-093000-gh-11-call-dentist.md"
        env = self.env.copy()
        env.update({"GH_EXPECT_SYNC": str(remote), "GH_EXPECT_SYNC_PATH": relative})
        result = self.run_mt("import-github", "--repo", "acme/tasks", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        synced = subprocess.run(["git", "--git-dir", str(remote), "cat-file", "-e",
                                 f"refs/heads/main:{relative}"], capture_output=True)
        self.assertEqual(synced.returncode, 0)

    def test_auto_sync_failure_leaves_issue_open_and_retry_syncs_existing_capture(self) -> None:
        remote = self.enable_auto_sync()
        hook = remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        first = self.run_mt("import-github", "--repo", "acme/tasks", "--json")
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(json.loads(first.stdout)["sync_failed"])
        relative = "cards/2026-10-10-093000-gh-11-call-dentist.md"
        capture = self.root / relative
        self.assertTrue(capture.is_file())
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "open")
        hook.unlink()
        env = self.env.copy()
        env.update({"GH_EXPECT_SYNC": str(remote), "GH_EXPECT_SYNC_PATH": relative})
        retry = self.run_mt("import-github", "--repo", "acme/tasks", "--json", env=env)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["existing_ids"], [capture.stem])
        self.assertEqual(len(list(self.root.glob("cards/*gh-11*.md"))), 1)
        self.assertEqual(json.loads(self.state_path.read_text())["issues"][0]["state"], "closed")

    def test_obsolete_import_handoff_flags_are_rejected(self) -> None:
        for flag in (("--run-dir", str(Path(self.temporary.name) / "outside")), ("--no-ai",)):
            result = self.run_mt("import-github", "--repo", "acme/tasks", *flag)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(list((self.root / "cards").glob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
