from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402
import drafts as draft_store  # noqa: E402
import card_format  # noqa: E402
import support  # noqa: E402


EDITOR_STUB = '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

path = Path(sys.argv[-1])
path.write_text(os.environ.get("EDITOR_BODY", ""), encoding="utf-8")
'''


class DraftCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", ".DS_Store"),
        )
        self.env = os.environ.copy()
        self.editor = Path(self.temporary.name) / "editor"
        self.editor.write_text(EDITOR_STUB, encoding="utf-8")
        self.editor.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, input_text: str | None = None,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return support.run_cli(self.root, *args, input_text=input_text, env=env or self.env)

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.root, text=True, capture_output=True, check=False,
        )

    def json_output(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_note_draft_round_trip_and_revision_guard(self) -> None:
        body = "\n  rough note\r\n\r\n---\r\nnot frontmatter\r\n"
        created = self.json_output(self.run_mt("draft", "create", "note", "--file", "-", "--json", input_text=body))
        draft_id = created["id"]
        self.assertEqual(created["body"], body)
        self.assertTrue((self.root / "drafts" / f"{draft_id}.md").is_file())
        self.assertEqual(list(card_files(self.root, "note")), [])
        self.assertEqual(list(card_files(self.root, "task")), [])

        changed = "Revised rough note\n\n## Still just Markdown\n"
        edited = self.json_output(self.run_mt(
            "draft", "edit", draft_id, "--file", "-", "--expected-revision", "1", "--json",
            input_text=changed,
        ))
        self.assertEqual(edited["revision"], 2)
        stale = self.run_mt("draft", "edit", draft_id, "--file", "-", "--expected-revision", "1",
                            input_text="must not replace")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("changed since", stale.stderr)
        self.assertEqual(self.json_output(self.run_mt("draft", "read", draft_id, "--json"))["body"], changed)

        receipt = self.json_output(self.run_mt("draft", "save", draft_id, "--expected-revision", "2", "--json"))
        self.assertTrue(receipt["changed"])
        self.assertFalse((self.root / "drafts" / f"{draft_id}.md").exists())
        notes = list(card_files(self.root, "note"))
        self.assertEqual(len(notes), 1)
        self.assertIn("Revised rough note", notes[0].read_text(encoding="utf-8"))
        validate = self.run_mt("validate")
        self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)

    def test_invalid_processing_instructions_do_not_write_a_draft(self) -> None:
        draft_id = "550e8400-e29b-41d4-a716-446655440000"
        with self.assertRaisesRegex(ValueError, "draft instructions must be text or null"):
            draft_store.save(
                self.root, draft_id=draft_id,
                fields={"kind": "note", "mode": "new", "title": "Bad draft",
                        "capture_id": draft_id, "created": "2026-10-15T10:00:00",
                        "instructions": ["not", "text"]},
                body="Draft body", expected_revision=0, now="2026-10-15T10:00:00",
            )
        self.assertFalse((self.root / "drafts" / f"{draft_id}.md").exists())

    def test_draft_store_rejects_untyped_kind_mode_and_boolean_revisions_without_writes(self) -> None:
        now = "2026-10-15T10:00:00"
        cases = (
            ("11111111-1111-4111-8111-111111111111", "kind", []),
            ("22222222-2222-4222-8222-222222222222", "kind", {"note": "value"}),
            ("33333333-3333-4333-8333-333333333333", "mode", []),
            ("44444444-4444-4444-8444-444444444444", "mode", {"new": "value"}),
        )
        for draft_id, field, value in cases:
            fields = {"kind": "note", "mode": "new", "title": "Typed draft",
                      "capture_id": draft_id, "created": now}
            fields[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, f"draft {field}"):
                    draft_store.save(self.root, draft_id=draft_id, fields=fields, body="Draft body",
                                     expected_revision=0, now=now)
                self.assertFalse((self.root / "drafts" / f"{draft_id}.md").exists())

        with self.assertRaisesRegex(ValueError, "fields must be a mapping"):
            draft_store.save(self.root, draft_id="55555555-5555-4555-8555-555555555555",
                             fields=[], body="Draft body", expected_revision=0, now=now)
        with self.assertRaisesRegex(ValueError, "expected_revision"):
            draft_store.save(self.root, draft_id="66666666-6666-4666-8666-666666666666",
                             fields={"kind": "note", "mode": "new"}, body="Draft body",
                             expected_revision=False, now=now)

        draft_id = "77777777-7777-4777-8777-777777777777"
        valid_fields = {"kind": "note", "mode": "new", "title": "Imported",
                        "capture_id": draft_id, "created": now, "origin": "same-import"}
        saved = draft_store.save(self.root, draft_id=draft_id, fields=valid_fields, body="Same body",
                                 expected_revision=0, now=now)
        path = self.root / "drafts" / f"{draft_id}.md"
        original = path.read_bytes()
        malformed_replay = {**valid_fields, "kind": []}
        with self.assertRaisesRegex(ValueError, "draft kind"):
            draft_store.save(self.root, draft_id=draft_id, fields=malformed_replay, body="Same body",
                             expected_revision=0, now=now)
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "expected_revision"):
            draft_store.save(self.root, draft_id=draft_id, fields=valid_fields, body="Changed body",
                             expected_revision=True, now=now, expected_content_hash=saved["content_hash"])
        self.assertEqual(path.read_bytes(), original)

    def test_new_task_draft_can_be_saved_without_a_target(self) -> None:
        draft = self.json_output(self.run_mt(
            "draft", "create", "task", "--title", "Drafted task", "--file", "-", "--json",
            input_text="A task captured in a draft.",
        ))
        self.assertEqual(draft["mode"], "new")
        self.assertEqual(draft["kind"], "task")
        receipt = self.json_output(self.run_mt(
            "draft", "save", draft["id"], "--expected-revision", str(draft["revision"]), "--json",
        ))
        path = self.root / receipt["path"]
        self.assertTrue(path.is_file())
        metadata = card_format.parse_yaml(card_format.parse_frontmatter(path.read_text())[0] or "")
        self.assertEqual(metadata["type"], "task")
        self.assertEqual(metadata["status"], "inbox")
        self.assertFalse((self.root / "drafts" / f"{draft['id']}.md").exists())
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_task_and_note_edit_drafts_replace_exactly_the_main_content(self) -> None:
        task = self.json_output(self.run_mt(
            "add", "Draft task target", "--next-action", "Keep this action", "--json",
        ))
        note = self.json_output(self.run_mt(
            "note", "--title", "Draft note target", "--file", "-",
            "--json", input_text="Initial note body\n",
        ))
        changed = {
            task["ref"]: "Shared draft content",
            note["ref"]: "Shared draft content\n",
        }
        for reference, body in changed.items():
            with self.subTest(reference=reference):
                kind = "task" if reference == task["ref"] else "note"
                draft = self.json_output(self.run_mt(
                    "draft", "create", kind, "--target", reference, "--json",
                ))
                edited = self.json_output(self.run_mt(
                    "draft", "edit", draft["id"], "--file", "-", "--expected-revision", "1", "--json",
                    input_text=body,
                ))
                saved = self.json_output(self.run_mt(
                    "draft", "save", draft["id"], "--expected-revision", str(edited["revision"]), "--json",
                ))
                self.assertTrue(saved["changed"])
                self.assertEqual(self.run_mt("read", reference, "--body").stdout, body)
                card_path = self.root / saved["path"]
                properties, saved_body = card_format.parse_document(card_path.read_bytes().decode("utf-8"))
                if kind == "task":
                    self.assertEqual(properties["next_action"], "Keep this action")
                self.assertEqual(saved_body, body)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_target_draft_uses_supplied_file_body_and_keeps_stale_target_protection(self) -> None:
        note = self.json_output(self.run_mt(
            "note", "--title", "Seeded target", "--file", "-", "--json", input_text="Original body\n"))
        task = self.json_output(self.run_mt("add", "Seeded task", "--json"))
        for reference, kind in ((note["ref"], "note"), (task["ref"], "task")):
            with self.subTest(kind=kind):
                before = self.run_mt("read", reference, "--body").stdout
                seeded = self.json_output(self.run_mt("draft", "create", kind, "--target", reference, "--json"))
                self.assertEqual(seeded["body"], before)
                supplied = self.json_output(self.run_mt(
                    "draft", "create", kind, "--target", reference, "--file", "-", "--json",
                    input_text="New body\n"))
                self.assertEqual(supplied["body"], "New body\n")
                self.assertEqual(supplied["base_hash"], seeded["base_hash"])
                self.assertEqual(supplied["base_updated"], seeded["base_updated"])
                self.assertEqual(self.run_mt("read", reference, "--body").stdout, before)
                self.json_output(self.run_mt(
                    "draft", "save", supplied["id"], "--expected-revision", str(supplied["revision"]), "--json"))
                self.assertEqual(self.run_mt("read", reference, "--body").stdout, "New body\n")
                stale = self.json_output(self.run_mt(
                    "draft", "create", kind, "--target", reference, "--file", "-", "--json",
                    input_text="Stale body\n"))
                self.assertEqual(self.run_mt("comment", reference, "-m", "changes the card").returncode, 0)
                result = self.run_mt("draft", "save", stale["id"], "--expected-revision", str(stale["revision"]), "--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.run_mt("read", reference, "--body").stdout, "New body\n")

    def test_new_capture_draft_can_switch_to_task_and_promotes_with_metadata_once(self) -> None:
        capture_id = str(uuid.uuid4())
        created = "2026-10-15T09:30:00"
        body = "First line\n## Literal heading\n````\n**literal Markdown**\n````\n"
        fields = {
            "kind": "note", "mode": "new", "title": "Task from capture",
            "capture_id": capture_id, "created": created,
            "status": "active", "priority": "high", "type": "bug",
            "project": "custom project", "topic": "custom topic",
            "tags": ["code", "custom-tag"], "due": "2026-12-31T09:45:00",
            "due_kind": "target",
        }
        draft = draft_store.save(
            self.root, draft_id=capture_id, fields=fields, body=body,
            expected_revision=0, now=created,
        )
        switched_fields = {**fields, "kind": "task"}
        switched = draft_store.save(
            self.root, draft_id=capture_id, fields=switched_fields, body=body,
            expected_revision=int(draft["revision"]), expected_content_hash=str(draft["content_hash"]),
            now="2026-10-15T09:31:00",
        )
        self.assertEqual(switched["capture_id"], capture_id)
        self.assertEqual(switched["created"], created)
        self.assertEqual(switched["kind"], "task")
        self.assertEqual(switched["tags"], ["code", "custom-tag"])

        receipt = self.json_output(self.run_mt(
            "draft", "save", capture_id, "--expected-revision", str(switched["revision"]),
            "--expected-hash", str(switched["content_hash"]), "--json",
        ))
        task_path = self.root / receipt["path"]
        raw = task_path.read_text(encoding="utf-8")
        frontmatter, card_body = card_format.parse_frontmatter(raw)
        metadata = card_format.parse_yaml(frontmatter or "")
        self.assertEqual(metadata["status"], "active")
        self.assertEqual(metadata["priority"], "high")
        self.assertEqual(metadata["type"], "bug")
        self.assertEqual(metadata["project"], "custom project")
        self.assertEqual(metadata["topic"], "custom topic")
        self.assertEqual(metadata["tags"], ["code", "custom-tag"])
        self.assertEqual(metadata["due"], "2026-12-31T09:45:00")
        self.assertEqual(metadata["due_kind"], "target")
        self.assertEqual(metadata["capture_id"], capture_id)
        self.assertEqual(card_body, body)
        self.assertIn("## Literal heading", card_body)
        self.assertEqual(len(list(card_files(self.root, "task"))), 1)
        self.assertEqual(list(card_files(self.root, "note")), [])
        repeated = self.run_mt(
            "draft", "save", capture_id, "--expected-revision", str(switched["revision"]),
            "--expected-hash", str(switched["content_hash"]), "--json",
        )
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(len(list(card_files(self.root, "task"))), 1)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_task_capture_supports_derived_and_title_only_names_and_rejects_invalid_type(self) -> None:
        created = "2026-10-15T10:00:00"
        captures = [
            (str(uuid.uuid4()), "", "Rebuild the search index", "Rebuild the search index"),
            (str(uuid.uuid4()), "Title only task", "", "Title only task"),
        ]
        for capture_id, title, body, expected_title in captures:
            draft_store.save(
                self.root, draft_id=capture_id,
                fields={"kind": "task", "mode": "new", "title": title,
                        "capture_id": capture_id, "created": created,
                        "status": "inbox", "priority": "none", "type": "task",
                        "tags": [], "due": None, "due_kind": None},
                body=body, expected_revision=0, now=created,
            )
            receipt = self.json_output(self.run_mt(
                "draft", "save", capture_id, "--expected-revision", "1", "--json",
            ))
            raw = (self.root / receipt["path"]).read_text(encoding="utf-8")
            metadata, saved_body = card_format.parse_document(raw)
            self.assertEqual(metadata["title"], expected_title)
            self.assertEqual(saved_body, body)

        invalid_id = str(uuid.uuid4())
        invalid = draft_store.save(
            self.root, draft_id=invalid_id,
            fields={"kind": "task", "mode": "new", "title": "Invalid type",
                    "capture_id": invalid_id, "created": created,
                    "status": "inbox", "priority": "none", "type": "not-in-policy",
                    "tags": [], "due": None, "due_kind": None},
            body="Do not create this task", expected_revision=0, now=created,
        )
        failed = self.run_mt(
            "draft", "save", invalid_id, "--expected-revision", "1",
            "--expected-hash", str(invalid["content_hash"]), "--json",
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("task type must be an allowed", failed.stderr)
        self.assertTrue((self.root / "drafts" / f"{invalid_id}.md").is_file())
        self.assertFalse(any("Invalid type" in path.read_text(encoding="utf-8") for path in card_files(self.root, "task")))
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_task_capture_claims_explicit_and_default_doing_and_rolls_back_at_limit(self) -> None:
        policy_path = self.root / "mt-policy.yaml"
        policy_text = policy_path.read_text(encoding="utf-8")
        policy_text = policy_text.replace("default_status: inbox", "default_status: doing")
        policy_text = policy_text.replace("max_doing_tasks: 5", "max_doing_tasks: 2")
        policy_path.write_text(policy_text, encoding="utf-8")
        env = dict(self.env, MT_USERNAME="capture-test")
        created = "2026-10-15T10:00:00"

        def save_capture(capture_id: str, *, status: str | None, text: str) -> dict:
            fields = {
                "kind": "task", "mode": "new", "title": text,
                "capture_id": capture_id, "created": created,
                "priority": "none", "type": "task", "tags": [],
                "due": None, "due_kind": None,
            }
            if status is not None:
                fields["status"] = status
            draft_store.save(
                self.root, draft_id=capture_id, fields=fields, body=text,
                expected_revision=0, now=created,
            )
            receipt = self.json_output(self.run_mt(
                "draft", "save", capture_id, "--expected-revision", "1", "--json", env=env,
            ))
            return receipt

        explicit_id = str(uuid.uuid4())
        explicit = save_capture(explicit_id, status="doing", text="Explicit doing capture")
        self.assertTrue(explicit["changed"])
        explicit_card = (self.root / explicit["path"]).read_text(encoding="utf-8")
        explicit_properties, _ = card_format.parse_document(explicit_card, expected_kind="task")
        self.assertEqual(explicit_properties["claimed_by"], "capture-test")
        self.assertRegex(explicit_properties["claimed_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertEqual(explicit_properties["capture_id"], explicit_id)

        default_id = str(uuid.uuid4())
        defaulted = save_capture(default_id, status=None, text="Default doing capture")
        self.assertTrue(defaulted["changed"])
        default_card = (self.root / defaulted["path"]).read_text(encoding="utf-8")
        default_properties, _ = card_format.parse_document(default_card, expected_kind="task")
        self.assertEqual(default_properties["status"], "doing")
        self.assertEqual(default_properties["claimed_by"], "capture-test")
        self.assertEqual(default_properties["capture_id"], default_id)

        blocked_id = str(uuid.uuid4())
        blocked = draft_store.save(
            self.root, draft_id=blocked_id,
            fields={"kind": "task", "mode": "new", "title": "Over limit capture",
                    "capture_id": blocked_id, "created": created,
                    "status": "doing", "priority": "none", "type": "task",
                    "tags": [], "due": None, "due_kind": None},
            body="Over limit capture", expected_revision=0, now=created,
        )
        failed = self.run_mt(
            "draft", "save", blocked_id, "--expected-revision", "1",
            "--expected-hash", str(blocked["content_hash"]), "--json", env=env,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("doing task limit exceeded", failed.stderr)
        self.assertTrue((self.root / "drafts" / f"{blocked_id}.md").is_file())
        self.assertFalse(any("Over limit capture" in path.read_text(encoding="utf-8")
                             for path in card_files(self.root, "task")))
        validate = self.run_mt("validate", env=env)
        self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)

    def test_task_metadata_cli_applies_one_hash_guarded_patch_and_preserves_claim_and_body(self) -> None:
        actor = dict(self.env)
        actor["MT_USERNAME"] = "capture-test"
        created = self.run_mt("add", "Metadata target", "--status", "doing", "--by", "capture-test", env=actor)
        self.assertEqual(created.returncode, 0, created.stderr)
        path = card_files(self.root, "task")[0]
        before = path.read_bytes()
        before_hash = hashlib.sha256(before).hexdigest()
        frontmatter, body = card_format.parse_frontmatter(before.decode("utf-8"))
        original = card_format.parse_yaml(frontmatter or "")
        patch = {
            "priority": "high", "type": "decision", "project": "custom project",
            "topic": "custom topic", "tags": ["code", "custom-tag"],
            "due": "2026-12-31T09:45:00", "due_kind": "reminder",
        }
        metadata_args = (
            "card-metadata", f"@{hashlib.sha256(path.stem.encode()).hexdigest()[:8]}",
            "--expected-hash", before_hash, "--patch", json.dumps(patch), "--json",
        )
        unauthorized = dict(self.env)
        unauthorized["MT_USERNAME"] = "another-user"
        receipt = self.json_output(self.run_mt(*metadata_args, env=unauthorized))
        saved = path.read_bytes()
        frontmatter, saved_body = card_format.parse_frontmatter(saved.decode("utf-8"))
        metadata = card_format.parse_yaml(frontmatter or "")
        self.assertEqual(metadata["claimed_by"], "capture-test")
        self.assertEqual(metadata["claimed_at"], original["claimed_at"])
        self.assertEqual(saved_body, body)
        self.assertEqual(metadata["tags"], patch["tags"])
        self.assertEqual(metadata["due_kind"], "reminder")
        self.assertEqual(receipt["content_hash"], hashlib.sha256(saved).hexdigest())

        stale = self.run_mt(
            "card-metadata", receipt["ref"], "--expected-hash", before_hash,
            "--patch", json.dumps({"priority": "low"}), "--json",
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("conflict", stale.stderr)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_cli_editor_edits_canonical_draft_and_conflict_forks_changes(self) -> None:
        created = self.json_output(self.run_mt("draft", "create", "note", "--file", "-", "--json",
                                               input_text="original\n"))
        env = dict(self.env, VISUAL=str(self.editor), EDITOR_BODY="from editor\n")
        edited = self.json_output(self.run_mt("draft", "edit", created["id"], "--json", env=env))
        self.assertEqual(edited["body"], "from editor\n")
        self.assertEqual(edited["revision"], 2)

        next_created = self.json_output(self.run_mt("draft", "create", "note", "--file", "-", "--json",
                                                    input_text="before conflict\n"))
        conflict_env = dict(self.env, VISUAL=str(self.editor), EDITOR_BODY="edited copy\n")
        # A second process changes the draft while the editor is open.
        race_editor = Path(self.temporary.name) / "race-editor"
        race_editor.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
path = sys.argv[-1]
open(path, "w").write(os.environ["EDITOR_BODY"])
subprocess.run([os.environ["MT_PATH"], "draft", "edit", os.environ["DRAFT_ID"], "--file", "-",
                "--expected-revision", "1"], input="other window\\n", text=True, check=True)
''', encoding="utf-8")
        race_editor.chmod(0o755)
        conflict_env.update(VISUAL=str(race_editor), MT_PATH=str(self.root / "mt"), DRAFT_ID=next_created["id"])
        conflict = self.run_mt("draft", "edit", next_created["id"], env=conflict_env)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("edits were kept as draft", conflict.stderr)
        drafts = self.json_output(self.run_mt("draft", "list", "--json"))["drafts"]
        self.assertEqual(len([item for item in drafts if item["body"] in {"other window\n", "edited copy\n"}]), 2)

    def test_editor_recovery_is_archived_once_and_unmatched_files_are_retained(self) -> None:
        recovery = self.root / ".mt" / "recovery" / "edit"
        recovery.mkdir(parents=True)
        source = recovery / f"new-note-edit-{uuid.uuid4().hex}.md"
        source.write_bytes(b"\nrecovered\r\nbody\r\n")
        orphan = recovery / f"deleted-item-edit-{uuid.uuid4().hex}.md"
        orphan.write_text("unmatched task edit", encoding="utf-8")

        listed = self.json_output(self.run_mt("draft", "list", "--json"))
        self.assertEqual(listed["drafts"], [])
        self.assertTrue(source.is_file())

        result = self.json_output(self.run_mt("draft", "recover", "--json"))
        self.assertEqual(result["recovered"], [])
        self.assertEqual(result["unresolved"], sorted([source.name, orphan.name]))
        self.assertTrue(source.is_file())
        self.assertTrue(orphan.is_file())

    def test_note_body_recovery_preserves_yaml_frontmatter_and_original_target_hash(self) -> None:
        captured = self.run_mt("note", "Baseline content")
        self.assertEqual(captured.returncode, 0, captured.stderr)
        note = card_files(self.root, "note")[0]
        original_hash = hashlib.sha256(note.read_bytes()).hexdigest()
        note.write_text(note.read_text(encoding="utf-8").replace("Baseline content", "Newer content"),
                        encoding="utf-8")

        recovery = self.root / ".mt" / "recovery" / "edit"
        recovery.mkdir(parents=True, exist_ok=True)
        source = recovery / f"{note.stem}-edit-{uuid.uuid4().hex}.md"
        body = "---\ntitle: personal draft\n---\n\n# This is body text\n"
        source.write_text(body, encoding="utf-8")
        source.with_suffix(".meta.json").write_text(json.dumps({
            "version": 1, "kind": "note", "mode": "edit",
            "target": str(note.relative_to(self.root)), "base_hash": original_hash,
            "base_updated": "2026-10-15", "recovery_mode": "note-body",
        }), encoding="utf-8")

        recovered = self.json_output(self.run_mt("draft", "recover", "--json"))
        self.assertEqual(recovered["recovered"], [])
        self.assertEqual(recovered["unresolved"], [source.name])
        self.assertTrue(source.is_file())
        self.assertIn("Newer content", note.read_text(encoding="utf-8"))

    def test_legacy_edit_recovery_without_baseline_is_explicitly_unapplyable(self) -> None:
        captured = self.run_mt("note", "Existing content")
        self.assertEqual(captured.returncode, 0, captured.stderr)
        note = card_files(self.root, "note")[0]
        recovery = self.root / ".mt" / "recovery" / "edit"
        recovery.mkdir(parents=True, exist_ok=True)
        source = recovery / f"{note.stem}-edit-{uuid.uuid4().hex}.md"
        source.write_text("Recovered edit\n", encoding="utf-8")

        recovered = self.json_output(self.run_mt("draft", "recover", "--json"))
        self.assertEqual(recovered["recovered"], [])
        self.assertEqual(recovered["unresolved"], [source.name])
        self.assertTrue(source.is_file())
        self.assertIn("Existing content", note.read_text(encoding="utf-8"))

    def test_cli_draft_edit_detects_direct_markdown_change_with_same_revision(self) -> None:
        created = self.json_output(self.run_mt("draft", "create", "note", "--file", "-", "--json",
                                               input_text="Original\n"))
        path = self.root / "drafts" / f"{created['id']}.md"
        editor = Path(self.temporary.name) / "manual-draft-editor"
        editor.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
editor_path = Path(sys.argv[-1])
editor_path.write_text("CLI overwrite\\n", encoding="utf-8")
canonical = Path(os.environ["DRAFT_PATH"])
header, separator, _body = canonical.read_text(encoding="utf-8").partition("\\n---\\n")
canonical.write_text(header + separator + "Manual edit\\n", encoding="utf-8")
''', encoding="utf-8")
        editor.chmod(0o755)
        env = dict(self.env, VISUAL=str(editor), EDITOR="", DRAFT_PATH=str(path))
        edit = self.run_mt("draft", "edit", created["id"], env=env)
        self.assertNotEqual(edit.returncode, 0, edit.stdout + edit.stderr)
        self.assertIn("edits were kept as draft", edit.stderr)
        self.assertIn("Manual edit", path.read_text(encoding="utf-8"))
        copies = self.json_output(self.run_mt("draft", "list", "--json"))["drafts"]
        self.assertIn("CLI overwrite\n", [draft["body"] for draft in copies])

    def test_demo_note_edit_draft_uses_demo_note_format_and_stays_out_of_real_cards(self) -> None:
        created = self.json_output(self.run_mt(
            "draft", "create", "note", "--target", "microtask workflow cheatsheet", "--json",
        ))
        self.assertEqual(created["target"], ".mt/demo/cards/2026-01-04-120000-microtask-workflow-cheatsheet.md")
        self.assertEqual(created["mode"], "edit")
        body = "Edited demo note\n"
        edited = self.json_output(self.run_mt("draft", "edit", created["id"], "--file", "-",
                                              "--expected-revision", "1", "--json", input_text=body))
        saved = self.json_output(self.run_mt("draft", "save", created["id"],
                                             "--expected-revision", str(edited["revision"]), "--json"))
        self.assertTrue(saved["changed"])
        target = self.root / str(created["target"])
        self.assertIn("Edited demo note", target.read_text(encoding="utf-8"))
        self.assertEqual(list(card_files(self.root, "note")), [])

    def test_draft_directory_symlink_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "x.md").write_text("---\nunsafe\n---\n", encoding="utf-8")
        drafts_directory = self.root / "drafts"
        if drafts_directory.is_dir() and not drafts_directory.is_symlink():
            drafts_directory.rmdir()
        (self.root / "drafts").symlink_to(outside, target_is_directory=True)
        result = self.run_mt("draft", "list", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be a symbolic link", result.stderr)

    def test_sync_includes_draft_creation_and_tracked_deletion(self) -> None:
        remote = Path(self.temporary.name) / "remote.git"
        init = self.git("init", "-b", "main")
        self.assertEqual(init.returncode, 0, init.stderr)
        self.git("config", "user.name", "Draft Test")
        self.git("config", "user.email", "draft@example.invalid")
        self.git("add", ".")
        self.assertEqual(self.git("commit", "-m", "Initial").returncode, 0)
        self.assertEqual(self.git("init", "--bare", "-b", "main", str(remote), cwd=Path(self.temporary.name)).returncode, 0)
        self.git("remote", "add", "origin", str(remote))
        pushed = self.git("push", "-u", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

        draft = self.json_output(self.run_mt("draft", "create", "note", "--file", "-", "--json",
                                             input_text="sync this unfinished note\n"))
        draft_id = draft["id"]
        first_sync = self.run_mt("sync")
        self.assertEqual(first_sync.returncode, 0, first_sync.stderr)
        remote_draft = self.git("--git-dir", str(remote), "cat-file", "-e", f"refs/heads/main:drafts/{draft_id}.md",
                                cwd=Path(self.temporary.name))
        self.assertEqual(remote_draft.returncode, 0, remote_draft.stderr)

        discarded = self.run_mt("draft", "discard", draft_id, "--expected-revision", "1")
        self.assertEqual(discarded.returncode, 0, discarded.stderr)
        deletion_sync = self.run_mt("sync")
        self.assertEqual(deletion_sync.returncode, 0, deletion_sync.stderr)
        self.assertIn("Update 1 workspace draft", deletion_sync.stdout)
        remote_deleted = self.git("--git-dir", str(remote), "cat-file", "-e", f"refs/heads/main:drafts/{draft_id}.md",
                                  cwd=Path(self.temporary.name))
        self.assertNotEqual(remote_deleted.returncode, 0)


if __name__ == "__main__":
    unittest.main()
