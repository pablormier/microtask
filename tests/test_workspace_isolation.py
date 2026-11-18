"""An operation on the selected workspace never reads or writes the one MT_WORKSPACE names."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
import urllib.request
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tools"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))

import card_format  # noqa: E402
import card_query  # noqa: E402
import cli_cards  # noqa: E402
import drafts as draft_store  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import support  # noqa: E402
import transaction  # noqa: E402
import taskweb  # noqa: E402
from workspace import Workspace  # noqa: E402


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class WorkspaceIsolationTests(unittest.TestCase):
    """Workspace A is selected explicitly while the environment points at B."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        shared = (SOURCE_ROOT / "mt-policy.yaml").read_text(encoding="utf-8").replace(
            "demo_workspace: true", "demo_workspace: false")
        # A keeps the shipped policy. B defaults to waiting and has no `urgent` priority or `someday` status.
        policies = {
            "a": shared,
            "b": shared.replace("default_status: inbox", "default_status: waiting")
                       .replace('  someday: "Someday" # optional; remove if unused\n', "")
                       .replace('  urgent: "Time-sensitive or blocking important work"\n', ""),
        }
        self.roots = {}
        for name, policy in policies.items():
            root = base / name
            (root / "cards").mkdir(parents=True)
            (root / "mt-policy.yaml").write_text(policy, encoding="utf-8")
            self.roots[name] = root
        self.assertNotIn("urgent:", policies["b"])
        self.a = Workspace.at(self.roots["a"], app_root=SOURCE_ROOT)
        self.b = Workspace.at(self.roots["b"], app_root=SOURCE_ROOT)
        operations.create_card("task", "Alpha task body", title="Alpha task", priority="urgent", ws=self.a)
        operations.create_card("note", "Alpha note body", title="Alpha note", ws=self.a)
        operations.create_card("task", "Beta task body", title="Beta task", ws=self.b)
        self.b_before = snapshot(self.roots["b"])
        environment = mock.patch.dict(os.environ, {"MT_WORKSPACE": str(self.roots["b"])})
        environment.start()
        self.addCleanup(environment.stop)

    def card_text(self, title: str) -> str:
        for card in store.discover(ws=self.a):
            if card.title == title:
                return card.raw
        self.fail(f"no card titled {title}")

    def start_server(self) -> str:
        server = taskweb.TaskWebServer(("127.0.0.1", 0), "test-token", self.a)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        return f"http://{host}:{port}"

    def call(self, base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            base + path, data=data, headers={"X-Microtask-Token": "test-token", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_query_scope_comes_from_the_workspace_environment(self) -> None:
        # MT_PROJECT selects a subset of the cards; it must come from the selected workspace's
        # environment, not the process's.
        selected = Workspace.at(self.roots["a"], app_root=SOURCE_ROOT, environ={"MT_PROJECT": "alpha"})
        parser = argparse.ArgumentParser()
        card_query.add_query_arguments(parser)
        args = parser.parse_args([])
        with mock.patch.dict(os.environ, {"MT_PROJECT": "beta"}):
            self.assertEqual(store.command_query(args, selected).project, ("alpha",))
            self.assertEqual(taskweb.board_filters(selected, args).get("project"), ["alpha"])
            self.assertEqual(card_query.env_scope(argparse.Namespace(query_args=True, project=None, topic=None),
                                                  selected.environ), ["project alpha (MT_PROJECT)"])

    def test_operations_on_the_selected_workspace_ignore_the_environment(self) -> None:
        # Converting a note uses A's default status, not B's. The workspace is passed by position,
        # which the mutation decorator must honour as well as the keyword form.
        args = argparse.Namespace(
            query=["Alpha note"], destination="task", status=None, task_type=None, next_action=None,
            match_mode="exact", as_json=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_cards.command_convert(args, self.a), 0)
        self.assertEqual(card_format.parse_document(self.card_text("Alpha note"))[0]["status"], "inbox")

        base = self.start_server()
        # A dashboard read returns A's cards and statuses.
        status, payload = self.call(base, "/api/items")
        self.assertEqual(status, 200)
        self.assertEqual({task["title"] for task in payload["tasks"]}, {"Alpha task", "Alpha note"})
        self.assertIn("someday", payload["statuses"])
        self.assertEqual(payload["meta"]["task_defaults"]["status"], "inbox")
        ref = next(task["ref"] for task in payload["tasks"] if task["title"] == "Alpha task")

        # A dashboard write lands in A and is checked against A's policy (B has no `someday`).
        status, moved = self.call(base, "/api/move", {"ref": ref, "status": "someday"})
        self.assertEqual(status, 200, moved)
        self.assertEqual(card_format.parse_document(self.card_text("Alpha task"))[0]["status"], "someday")
        status, saved = self.call(base, "/api/draft-save", {"kind": "note", "title": "Alpha draft",
                                                   "body": "Alpha draft body",
                                                   "capture_id": str(uuid.uuid4()), "created": "2026-06-15T09:00:00"})
        self.assertEqual(status, 200, saved)
        draft = draft_store.read(self.roots["a"], saved["saved_draft"]["id"])
        self.assertEqual((draft["title"], draft["body"].strip()), ("Alpha draft", "Alpha draft body"))

        # Stopping a timer validates the card against A (B has no `urgent` priority).
        clock = support.FakeClock()
        with clock.patch(store, operations, transaction):
            operations.op_time_start(self.a, ref, sync=False)
            clock.advance(seconds=300)
            operations.op_time_end(self.a, query=ref, action="stop", sync=False)
        self.assertIn("time_entries", card_format.parse_document(self.card_text("Alpha task"))[0])

        self.assertEqual(snapshot(self.roots["b"]), self.b_before)
        # The CLI, dashboard and timer operations above all read A's shared policy cache; none modified it.
        self.assertEqual(self.a.policy(), Workspace.at(self.roots["a"], app_root=SOURCE_ROOT).policy())


if __name__ == "__main__":
    unittest.main()
