from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_fixture import render_dashboard
from card_files import card_files  # noqa: E402


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
import tasklib

class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root,
                        ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"))
        self.env = {**os.environ, "MT_USERNAME": "test-user"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root,
                              env=self.env if env is None else env, text=True, capture_output=True)

    def ready(self, title: str = "Claim this task") -> Path:
        result = self.run_mt("add", title, "--next-action", "Open the draft.")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = max(card_files(self.root, "task"), key=lambda p: p.stat().st_mtime_ns)
        return path

    def claim(self, path: Path, username: str = "test-user") -> Path:
        result = self.run_mt("move", path.stem, "doing", "--by", username)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    def test_legacy_doing_card_is_unclaimed_until_explicitly_claimed(self) -> None:
        path = self.ready()
        raw = path.read_text().replace("status: inbox", "status: doing")
        doing = path
        doing.write_text(raw)
        validated = self.run_mt("validate")
        listed = self.run_mt("list", "--status", "doing", "--json")
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIsNone(json.loads(listed.stdout)[0]["claimed_by"])
        self.assertEqual(doing.read_text(), raw)
        self.claim(doing)
        claimed = tasklib.Card.read(doing)
        self.assertEqual(claimed.frontmatter["claimed_by"], "test-user")
        self.assertTrue(tasklib.is_timestamp(claimed.frontmatter["claimed_at"]))
        self.assertEqual(tasklib.card_format.parse_document(claimed.raw)[1], tasklib.card_format.parse_document(raw)[1])

    def test_identity_precedence_and_same_username_claim_is_a_noop(self) -> None:
        default = self.ready("Default username")
        env = self.env.copy()
        env.pop("MT_USERNAME")
        result = self.run_mt("move", default.stem, "doing", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tasklib.Card.read(default).frontmatter["claimed_by"], getpass.getuser())
        (self.root / "mt-policy.local.yaml").write_text('default_username: "personal-user"\n')
        configured = self.ready("Configured username")
        result = self.run_mt("move", configured.stem, "doing", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tasklib.Card.read(configured).frontmatter["claimed_by"], "personal-user")
        explicit = self.claim(self.ready("Explicit username"), 'agent:"quoted"\\run')
        original = explicit.read_bytes()
        result = self.run_mt("move", explicit.stem, "doing", "--by", 'agent:"quoted"\\run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(explicit.read_bytes(), original)
        inherited = self.ready("Environment username")
        result = self.run_mt("move", inherited.stem, "doing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tasklib.Card.read(inherited).frontmatter["claimed_by"], "test-user")

    def test_configured_username_applies_to_timer_and_release_without_changing_old_claims(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text('default_username: "personal-user"\n')
        env = self.env.copy()
        env.pop("MT_USERNAME")
        path = self.ready()
        self.assertEqual(self.run_mt("move", path.stem, "doing", env=env).returncode, 0)
        (self.root / "mt-policy.local.yaml").write_text('default_username: "different-user"\n')
        before = path.read_bytes()
        rejected = self.run_mt("time", "start", path.stem, env=env)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("claimed by 'personal-user'", rejected.stderr)
        self.assertEqual(path.read_bytes(), before)
        started = self.run_mt("time", "start", path.stem, "--by", "personal-user", env=env)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(self.run_mt("time", "cancel", path.stem, "--by", "personal-user", env=env).returncode, 0)
        (self.root / "mt-policy.local.yaml").write_text('default_username: "personal-user"\n')
        released = self.run_mt("move", path.stem, "active", env=env)
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertNotIn("claimed_by", tasklib.Card.read(path).frontmatter)

    def test_other_username_cannot_change_lifecycle_or_start_timer_but_can_collaborate(self) -> None:
        path = self.claim(self.ready(), "agent:one")
        before = path.read_bytes()
        digest = hashlib.sha256(before).hexdigest()
        for arguments in (("move", path.stem, "doing"), ("move", path.stem, "waiting"),
                          ("done", path.stem), ("delete", path.stem),
                          ("time", "start", path.stem),
                          ("edit", path.stem, "--set", "status=waiting"),
                          ("edit", path.stem, "--archived"),
                          ("edit", path.stem, "--old", "Claim", "--new", "Seize"),
                          ("edit", path.stem, "--body", "--file", str(path), "--expected-hash", digest)):
            with self.subTest(arguments=arguments):
                rejected = self.run_mt(*arguments)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("claimed by", rejected.stderr)
                self.assertEqual(path.read_bytes(), before)
        converted = self.run_mt("convert", path.stem, "note")
        self.assertNotEqual(converted.returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.run_mt("comment", path.stem, "-m", "Added context").returncode, 0)
        self.assertEqual(self.run_mt("edit", path.stem, "--set", "priority=high", "--by", "agent:one").returncode, 0)
        self.assertEqual(tasklib.Card.read(path).frontmatter["claimed_by"], "agent:one")

    def test_takeover_and_release_preserve_history_and_time_log(self) -> None:
        path = self.claim(self.ready(), "agent:one")
        self.assertEqual(self.run_mt("time", "add", path.stem, "5m").returncode, 0)
        original = path.read_text()
        original_properties, _ = tasklib.card_format.parse_document(original)
        result = self.run_mt("move", path.stem, "doing", "--takeover", "--by", "agent:two")
        self.assertEqual(result.returncode, 0, result.stderr)
        transferred = tasklib.Card.read(path)
        self.assertEqual(transferred.frontmatter["claimed_by"], "agent:two")
        transferred_comments = "\n".join(comment["text"] for comment in transferred.frontmatter["comments"])
        original_comments = "\n".join(comment["text"] for comment in original_properties.get("comments", []))
        self.assertIn(original_comments, transferred_comments)
        self.assertIn("agent:one", transferred_comments)
        released = self.run_mt("move", path.stem, "active", "--by", "agent:two")
        self.assertEqual(released.returncode, 0, released.stderr)
        raw = path.read_text()
        fields = tasklib.parse_yaml(tasklib.parse_frontmatter(raw)[0])
        self.assertTrue(all(field not in fields for field in tasklib.CLAIM_FIELDS))
        self.assertEqual(fields.get("time_entries"), original_properties.get("time_entries"))
        self.assertIn("agent:two", "\n".join(comment["text"] for comment in fields["comments"]))

    def test_completion_releases_claim_and_stops_only_its_timer(self) -> None:
        path = self.claim(self.ready(), "agent:one")
        started = self.run_mt("time", "start", path.stem, "--by", "agent:one")
        self.assertEqual(started.returncode, 0, started.stderr)
        result = self.run_mt("done", path.stem, "--by", "agent:one", "--note", "Finished draft")
        self.assertEqual(result.returncode, 0, result.stderr)
        done = tasklib.Card.read(path)
        self.assertTrue(all(field not in done.frontmatter for field in tasklib.CLAIM_FIELDS))
        self.assertEqual(len(tasklib.time_log_entries(done.raw)), 1)
        self.assertIn("agent:one", "\n".join(comment["text"] for comment in done.frontmatter["comments"]))
        self.assertFalse((self.root / ".mt/time-running").exists())

    def test_two_concurrent_users_cannot_both_claim(self) -> None:
        path = self.ready()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda username: self.run_mt("move", path.stem, "doing", "--by", username),
                                        ("agent:one", "agent:two")))
        self.assertEqual(sum(result.returncode == 0 for result in results), 1)
        winner = "agent:one" if results[0].returncode == 0 else "agent:two"
        doing = tasklib.Card.read(path)
        self.assertEqual(doing.frontmatter["claimed_by"], winner)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_invalid_username_takeover_and_failed_claim_do_not_change_task(self) -> None:
        path = self.ready()
        before = path.read_bytes()
        for arguments in (("move", path.stem, "doing", "--by", " "),
                          ("move", path.stem, "doing", "--by", "a\nb"),
                          ("move", path.stem, "active", "--takeover")):
            self.assertNotEqual(self.run_mt(*arguments).returncode, 0)
            self.assertEqual(path.read_bytes(), before)
        path.write_text(path.read_text().replace("type: task", "type: invalid"))
        before = path.read_bytes()
        self.assertNotEqual(self.run_mt("move", path.stem, "doing").returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(tasklib.Card.read(path).frontmatter["status"], "inbox")

    def test_validator_accepts_missing_or_null_claims_and_rejects_invalid_pairs(self) -> None:
        path = self.ready()
        original = path.read_bytes().decode("utf-8")
        original_properties, body = tasklib.card_format.parse_document(original)
        cases = (({"claimed_by": None, "claimed_at": None}, True),
                 ({"claimed_by": "username"}, False),
                 ({"claimed_at": "2026-10-11T10:00:00"}, False),
                 ({"claimed_by": "username", "claimed_at": "2026-10-11T10:00:00"}, False),
                 ({"claimed_by": 42, "claimed_at": "2026-10-11T10:00:00"}, False))
        for claims, valid in cases:
            with self.subTest(claims=claims):
                candidate = {**original_properties, **claims}
                raw = "---\n" + tasklib.card_format.dump_properties(candidate) + "---\n" + body
                path.write_bytes(raw.encode("utf-8"))
                result = self.run_mt("validate")
                self.assertEqual(result.returncode == 0, valid, result.stderr)
        path.write_bytes(original.encode("utf-8"))
        self.assertEqual(self.run_mt("note", "Reference note").returncode, 0)
        note = card_files(self.root, "note")[0]
        note_properties, note_body = tasklib.card_format.parse_document(note.read_bytes().decode("utf-8"))
        note_properties["claimed_by"] = None
        note.write_text("---\n" + tasklib.card_format.dump_properties(note_properties) + "---\n" + note_body)
        self.assertNotEqual(self.run_mt("validate").returncode, 0)

    def test_claim_is_visible_in_cli_and_dashboard_data(self) -> None:
        path = self.claim(self.ready(), "agent:one")
        listed = self.run_mt("list", "--status", "doing", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)[0]["claimed_by"], "agent:one")
        self.assertIn("agent:one", self.run_mt("list", "--status", "doing").stdout)
        page = self.root / "dashboard.html"
        generated = render_dashboard(self.root, page)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        html = page.with_suffix(".json").read_text()
        self.assertIn('"claimed_by": "agent:one"', html)
        self.assertIn(tasklib.Card.read(path).frontmatter["claimed_at"], html)

    def test_claim_blocks_conflicting_work_but_not_additions(self) -> None:
        path = self.claim(self.ready(), "agent:one")
        before = path.read_bytes()
        rejected = self.run_mt("edit", path.stem, "--old", "Claim", "--new", "Seize")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("If you are agent:one, add --by agent:one", rejected.stderr)
        self.assertIn("--takeover", rejected.stderr)
        self.assertEqual(path.read_bytes(), before)
        addition = self.root / "addition.md"
        addition.write_text("Extra context")
        for arguments in (("edit", path.stem, "--body", "--append", "--file", str(addition)),
                          ("edit", path.stem, "--set", "priority=high", "--tag", "extra", "--set", "topic=work"),
                          ("edit", path.stem, "--title", "Renamed"),
                          ("comment", path.stem, "-m", "Context")):
            with self.subTest(arguments=arguments):
                result = self.run_mt(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
        card = tasklib.Card.read(path)
        self.assertEqual(card.frontmatter["claimed_by"], "agent:one")
        self.assertEqual(card.frontmatter["priority"], "high")
        self.assertIn("Extra context", card.body)
        self.assertEqual(self.run_mt("edit", path.stem, "--old", "Claim", "--new", "Seize", "--by", "agent:one").returncode, 0)

    def test_completion_cannot_release_another_users_claim(self) -> None:
        path = self.claim(self.ready(), "agent:owner")
        before = path.read_bytes()
        result = self.run_mt("done", path.stem, "--by", "agent:other")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("claimed by 'agent:owner'", result.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_editor_cannot_remove_claim(self) -> None:
        path = self.claim(self.ready())
        before = path.read_bytes()
        result = self.run_mt("edit", path.stem, "--set", "claimed_by=null")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        claimed = tasklib.Card.read(path).frontmatter
        self.assertEqual(claimed["claimed_by"], "test-user")
        self.assertTrue(tasklib.is_timestamp(claimed["claimed_at"]))


if __name__ == "__main__":
    unittest.main()
