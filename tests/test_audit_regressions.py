"""Regression tests for 0.2.0 audit fixes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TOOLS = ROOT / "tools"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import operations
import policy_parser
import store
import taskweb
import transaction
import validation
import web_actions
from workspace import Workspace, create_workspace


class AuditRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws_dir = Path(self.tmp.name) / "ws"
        create_workspace(self.ws_dir)
        self.ws = Workspace.at(self.ws_dir, demo=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_apply_item_transaction_prewrite_cas_does_not_clobber_external_edit(self) -> None:
        card_path = self.ws_dir / "cards" / "2026-11-19-120000-cas.md"
        before = (
            "---\n"
            "id: 2026-11-19-120000-cas\n"
            "title: Original\n"
            "kind: note\n"
            "priority: medium\n"
            "created: '2026-11-19T12:00:00'\n"
            "updated: '2026-11-19T12:00:00'\n"
            "---\n"
            "Original body\n"
        )
        card_path.write_text(before, encoding="utf-8")
        external = before.replace("Original body", "External edit")
        with mock.patch.object(
            transaction,
            "validate_item_changes",
            side_effect=lambda *a, **kw: card_path.write_text(external, encoding="utf-8") or [],
        ):
            with self.assertRaises(store.StaleCardError):
                transaction.apply_item_transaction(
                    {"cards/2026-11-19-120000-cas.md": external.encode("utf-8")},
                    source_root=self.ws_dir,
                    ws=self.ws,
                )
        self.assertEqual(card_path.read_text(encoding="utf-8"), external)

    def test_time_mutation_lock_is_reentrant_and_workspace_scoped(self) -> None:
        ws_b_dir = Path(self.tmp.name) / "ws_b"
        create_workspace(ws_b_dir)
        ws_b = Workspace.at(ws_b_dir, demo=False)
        with transaction.time_mutation_lock(self.ws):
            self.assertTrue(transaction._is_lock_held(self.ws))
            self.assertFalse(transaction._is_lock_held(ws_b))
            with transaction.time_mutation_lock(self.ws):
                self.assertTrue(transaction._is_lock_held(self.ws))
            self.assertTrue(transaction._is_lock_held(self.ws))
        self.assertFalse(transaction._is_lock_held(self.ws))

    def test_op_draft_save_respects_auto_sync_false(self) -> None:
        draft = operations.op_draft_create(self.ws, "note", title="Draft note", body="Body")
        with mock.patch.object(transaction, "run_auto_sync_for_receipt") as sync_mock:
            operations.op_draft_save(self.ws, str(draft["id"]), sync=True)
        sync_mock.assert_not_called()

    def test_audit_workspace_handles_invalid_utf8_and_card_symlinks(self) -> None:
        bad = self.ws_dir / "cards" / "2026-11-19-120000-bad.md"
        bad.write_bytes(b"\xff\xfe\xfd")
        link = self.ws_dir / "cards" / "2026-11-19-120000-link.md"
        link.symlink_to(bad)
        report = validation.audit_workspace(self.ws_dir, policy_parser.load_policy(self.ws_dir))
        self.assertTrue(any("cannot read card" in e for e in report.errors), report.errors)
        self.assertTrue(any("symbolic link under cards/" in e for e in report.errors), report.errors)

    def test_glued_repo_selector_targets_selected_workspace(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "MT_WORKSPACE"}
        result = subprocess.run(
            [str(ROOT / "mt"), f"-R{self.ws_dir}", "list", "--json"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

        dup = subprocess.run(
            [str(ROOT / "mt"), f"-R{self.ws_dir}", "-R", str(self.ws_dir), "list"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(dup.returncode, 2)

    def test_split_host_header_and_checkbox_missing_card(self) -> None:
        self.assertEqual(taskweb.split_host_header("localhost"), ("localhost", None))
        self.assertEqual(taskweb.split_host_header("localhost:abc"), ("localhost", -1))
        status, payload = web_actions.handle_checkbox(
            self.ws,
            {"ref": "@deadbeef", "content_hash": "0" * 64},
            {"expected_hash": "0" * 64, "index": 0, "count": 1, "checked": True},
        )
        self.assertEqual(status.value, 404)
        self.assertIn("no longer exists", payload["error"])

    def test_topic_picker_blur_and_property_hydrate_in_node(self) -> None:
        if not shutil.which("node"):
            self.skipTest("node is required")
        sources = "\n".join(
            (TOOLS / "web" / "js" / name).read_text(encoding="utf-8")
            for name in ("card_helpers.js", "picker.js", "metadata_editor.js", "property_editor.js")
        )
        script = sources + r"""
const assert = require("assert/strict");
const editor = cardMetadataEditor(
  { topic: "" },
  false,
  "t",
  {
    task_statuses: ["inbox"],
    topic_tree: [
      { path: "learning", name: "learning", depth: 0, count: 1 },
      { path: "learning/rust", name: "rust", depth: 1, count: 1 },
    ],
  },
  {},
  "task"
);
editor.$dispatch = () => {};
editor.openPicker("topic");
editor.setPickerQuery("topic", "learning/rust");
editor.leavePicker("topic");
assert.equal(editor.value.topic, "learning/rust");

const prop = createPropertyEditor();
prop.begin({ id: "1", title: "T", status: "inbox", priority: "medium", type: "action", content_hash: "a".repeat(64) }, {});
prop.hydrate({ id: "1", title: "T", status: "inbox", priority: "medium", type: "action", project: "ServerProj", content_hash: "b".repeat(64) }, {});
assert.equal(prop.form.project, "ServerProj");
"""
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
