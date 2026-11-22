"""Tests for sparse LexoRank card ordering across CLI, validation, and web API."""
from __future__ import annotations

from http import HTTPStatus
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent.parent
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
if str(SOURCE / "scripts") not in sys.path:
    sys.path.insert(0, str(SOURCE / "scripts"))
if str(SOURCE / "tests") not in sys.path:
    sys.path.insert(0, str(SOURCE / "tests"))

from test_validate import task_file

import card_format
import cli_parser
import lexorank
import operations
import store
from tools import web_actions, web_payloads
from workspace import Workspace


def make_task_file(stem: str, title: str, *, status: str = "active", priority: str = "medium") -> str:
    return (
        f"---\nid: {stem}\ntitle: {title}\nkind: task\nstatus: {status}\npriority: {priority}\n"
        f"type: task\nproject: null\ntopic: null\ndue: null\ndue_kind: null\n"
        f"created: 2026-06-01T09:00:00\nupdated: 2026-06-01T09:00:00\ntags: []\n---\n# {title}\n"
    )


def note_file(stem: str, title: str, *, updated: str = "2026-06-01T10:00:00") -> str:
    return (
        f"---\nid: {stem}\ntitle: {title}\nkind: note\npriority: none\n"
        f"project: null\ntopic: null\ndue: null\ndue_kind: null\n"
        f"created: {updated}\nupdated: {updated}\ntags: []\n---\n# {title}\n"
    )


class LexorankTests(unittest.TestCase):
    def test_rank_between_invariants_and_splits(self) -> None:
        first = lexorank.rank_between(None, None)
        self.assertTrue(lexorank.is_rank(first))
        before = lexorank.rank_between(None, first)
        after = lexorank.rank_between(first, None)
        self.assertTrue(before < first < after)

        # Adjacent single characters and prefix bounds
        mid = lexorank.rank_between("a", "b")
        self.assertTrue("a" < mid < "b")
        self.assertTrue(lexorank.is_rank(mid))

        mid2 = lexorank.rank_between("a", "a1")
        self.assertTrue("a" < mid2 < "a1")
        self.assertTrue(lexorank.is_rank(mid2))

        # Repeatedly inserting at the front never runs out of room or emits trailing '0'
        cur = first
        for _ in range(40):
            nxt = lexorank.rank_between(None, cur)
            self.assertTrue(lexorank.is_rank(nxt))
            self.assertLess(nxt, cur)
            cur = nxt

        # Initial ranks produce strictly increasing valid ranks
        ranks = lexorank.initial_ranks(10)
        self.assertEqual(len(ranks), 10)
        self.assertEqual(ranks, sorted(set(ranks)))
        self.assertTrue(all(lexorank.is_rank(r) for r in ranks))

    def test_card_format_rejects_invalid_order(self) -> None:
        raw_bad = make_task_file("t1", "Body").replace("priority: medium\n", "priority: medium\norder: a0\n")
        fm_bad, _ = card_format.parse_frontmatter(raw_bad)
        props = card_format.parse_yaml(fm_bad)
        errors = card_format.validate_properties(props, kind="task")
        self.assertTrue(any("order" in err for err in errors))

        raw_ok = make_task_file("t1", "Body").replace("priority: medium\n", "priority: medium\norder: i\n")
        fm_ok, _ = card_format.parse_frontmatter(raw_ok)
        props_ok = card_format.parse_yaml(fm_ok)
        self.assertEqual(card_format.validate_properties(props_ok, kind="task"), [])


class OrderOperationsAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "cards").mkdir()
        (self.root / "settings.yaml").write_text(
            (SOURCE / "templates" / "settings.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        # Three tasks in status 'active' with distinct priorities so default order is t1, t2, t3.
        (self.root / "cards" / "2026-06-01-090001-t1.md").write_text(
            make_task_file("2026-06-01-090001-t1", "First", status="active", priority="high"),
            encoding="utf-8",
        )
        (self.root / "cards" / "2026-06-01-090002-t2.md").write_text(
            make_task_file("2026-06-01-090002-t2", "Second", status="active", priority="medium"),
            encoding="utf-8",
        )
        (self.root / "cards" / "2026-06-01-090003-t3.md").write_text(
            make_task_file("2026-06-01-090003-t3", "Third", status="active", priority="low"),
            encoding="utf-8",
        )
        (self.root / "cards" / "2026-06-01-090004-n1.md").write_text(
            note_file("2026-06-01-090004-n1", "Note one", updated="2026-06-01T10:00:00"),
            encoding="utf-8",
        )
        (self.root / "cards" / "2026-06-01-090005-n2.md").write_text(
            note_file("2026-06-01-090005-n2", "Note two", updated="2026-06-01T11:00:00"),
            encoding="utf-8",
        )
        self.ws = Workspace.at(self.root, app_root=SOURCE)

    def _board_task_ids(self) -> list[str]:
        payload = web_payloads.dashboard_payload(self.ws, store.CardCache())
        return payload["collections"]["tasks"]

    def _board_note_ids(self) -> list[str]:
        payload = web_payloads.dashboard_payload(self.ws, store.CardCache())
        return payload["collections"]["notes"]

    def test_top_touches_only_one_card_and_clear_restores_default(self) -> None:
        self.assertEqual(
            self._board_task_ids(),
            ["2026-06-01-090001-t1", "2026-06-01-090002-t2", "2026-06-01-090003-t3"],
        )
        operations.op_order(self.ws, "2026-06-01-090003-t3", position="top", sync=False)

        # Only t3 got an order property; t1 and t2 remain untouched
        t1 = store.Card.read(self.root / "cards" / "2026-06-01-090001-t1.md")
        t2 = store.Card.read(self.root / "cards" / "2026-06-01-090002-t2.md")
        t3 = store.Card.read(self.root / "cards" / "2026-06-01-090003-t3.md")
        self.assertNotIn("order", t1.frontmatter)
        self.assertNotIn("order", t2.frontmatter)
        self.assertTrue(lexorank.is_rank(t3.frontmatter.get("order")))
        self.assertEqual(
            self._board_task_ids(),
            ["2026-06-01-090003-t3", "2026-06-01-090001-t1", "2026-06-01-090002-t2"],
        )

        # Clear restores original priority-ranked order
        operations.op_order(self.ws, "2026-06-01-090003-t3", position="clear", sync=False)
        t3_cleared = store.Card.read(self.root / "cards" / "2026-06-01-090003-t3.md")
        self.assertNotIn("order", t3_cleared.frontmatter)
        self.assertEqual(
            self._board_task_ids(),
            ["2026-06-01-090001-t1", "2026-06-01-090002-t2", "2026-06-01-090003-t3"],
        )

    def test_relative_before_after_and_bottom_and_notes(self) -> None:
        # Move t3 immediately before t2 -> [t1, t3, t2]
        operations.op_order(self.ws, "2026-06-01-090003-t3", before="2026-06-01-090002-t2", sync=False)
        self.assertEqual(
            self._board_task_ids(),
            ["2026-06-01-090001-t1", "2026-06-01-090003-t3", "2026-06-01-090002-t2"],
        )

        # Move t1 to bottom -> [t3, t2, t1]
        operations.op_order(self.ws, "2026-06-01-090001-t1", position="bottom", sync=False)
        self.assertEqual(
            self._board_task_ids(),
            ["2026-06-01-090003-t3", "2026-06-01-090002-t2", "2026-06-01-090001-t1"],
        )

        # Notes default newest first: [n2, n1]; pin n1 to top -> [n1, n2]
        self.assertEqual(self._board_note_ids(), ["2026-06-01-090005-n2", "2026-06-01-090004-n1"])
        operations.op_order(self.ws, "2026-06-01-090004-n1", position="top", sync=False)
        self.assertEqual(self._board_note_ids(), ["2026-06-01-090004-n1", "2026-06-01-090005-n2"])

    def test_cli_order_and_api_order_endpoint(self) -> None:
        from contextlib import redirect_stdout
        out = io.StringIO()
        args = cli_parser.parser().parse_args(["order", "2026-06-01-090003-t3", "--top", "--json"])
        with redirect_stdout(out):
            code = args.func(args, ws=self.ws)
        self.assertEqual(code, 0)
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt["properties"]["id"], "2026-06-01-090003-t3")
        self.assertTrue(lexorank.is_rank(receipt["properties"]["order"]))
        self.assertEqual(
            self._board_task_ids()[0],
            "2026-06-01-090003-t3",
        )

        t2 = store.Card.read(self.root / "cards" / "2026-06-01-090002-t2.md")
        status, body = web_actions.handle_api_action(
            self.ws,
            "/api/order",
            {"ref": f"@{t2.short_ref}", "position": "top"},
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(
            self._board_task_ids(),
            ["2026-06-01-090002-t2", "2026-06-01-090003-t3", "2026-06-01-090001-t1"],
        )

        # Resetting panel order clears all custom order in that status and restores priority order
        status, body = web_actions.handle_api_action(
            self.ws,
            "/api/order",
            {"ref": f"@{t2.short_ref}", "position": "clear_panel"},
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(
            self._board_task_ids(),
            ["2026-06-01-090001-t1", "2026-06-01-090002-t2", "2026-06-01-090003-t3"],
        )


if __name__ == "__main__":
    unittest.main()
