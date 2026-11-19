"""Running timers live on their cards: switching, interruption, pulls, upgrade and concurrency."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tools"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))

import card_format  # noqa: E402
import cli_admin  # noqa: E402
import cli_time  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import support  # noqa: E402
import taskweb  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace  # noqa: E402

SESSION = "0123456789abcdef0123456789abcdef"


class TimerCardTests(unittest.TestCase):
    """Run against a data-only workspace, like a hosted one."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "data"
        (self.root / "cards").mkdir(parents=True)
        policy = (SOURCE_ROOT / "templates" / "settings.yaml").read_text(encoding="utf-8")
        (self.root / "settings.yaml").write_text(
            policy.replace("demo_workspace: true", "demo_workspace: false"), encoding="utf-8")
        self.ws = Workspace.at(self.root, app_root=SOURCE_ROOT,
                               environ={**os.environ, "MT_USERNAME": "tester"})

    # Fixtures

    def add_task(self, title: str, **fields: object) -> Path:
        card, _created = operations.create_card("task", title + " body", title=title, ws=self.ws)
        if fields:
            properties, body = card_format.parse_document(card.raw)
            properties.update(fields)
            card.path.write_text(card_format.render_document(properties, body), encoding="utf-8")
        return card.path

    def ref(self, path: Path) -> str:
        return f"@{store.Card.read(path).short_ref}"

    def timers(self) -> dict[str, dict]:
        return store.running_timers(self.ws)

    def put_timer(self, path: Path, by: str = "tester", session_id: str = SESSION,
                  start_epoch: int | None = None) -> dict:
        """Write a timer onto a card directly, as a pull or an editor could."""
        start_epoch = int(datetime(2026, 6, 15, 9, 0, 0).timestamp()) if start_epoch is None else start_epoch
        timer = {"session_id": session_id, "start": store.local_stamp(start_epoch),
                 "start_epoch": start_epoch, "by": by}
        properties, body = card_format.parse_document(path.read_text(encoding="utf-8"))
        properties["timer"] = timer
        properties["status"] = "doing"
        properties["claimed_by"], properties["claimed_at"] = by, "2026-06-15T09:00:00"
        path.write_text(card_format.render_document(properties, body), encoding="utf-8")
        return timer

    def cli(self, function, **namespace: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = function(argparse.Namespace(**namespace), ws=self.ws)
        return code, out.getvalue(), err.getvalue()

    def cli_start(self, path: Path, parallel: bool = False) -> int:
        return self.cli(cli_time.command_time_start, query=[path.stem], parallel=parallel, by=None)[0]

    def status(self) -> str:
        code, out, err = self.cli(cli_time.command_time_status)
        self.assertEqual(code, 0, err)
        return out

    def entries(self, path: Path) -> list[dict]:
        return card_format.time_log_entries(path.read_text(encoding="utf-8"))

    def start_server(self) -> str:
        server = taskweb.TaskWebServer(("127.0.0.1", 0), "test-token", self.ws)
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

    # Switching between the CLI and the dashboard

    def test_start_from_the_cli_and_switch_from_the_dashboard_and_back(self) -> None:
        first, second = self.add_task("First timed task"), self.add_task("Second timed task")
        base = self.start_server()
        self.assertEqual(self.cli_start(first), 0)
        self.assertEqual(set(self.timers()), {first.stem})

        status, switched = self.call(base, "/api/time-start", {"ref": self.ref(second)})
        self.assertEqual(status, 200, switched)
        self.assertEqual(set(switched["meta"]["time_running"]), {second.stem})
        self.assertEqual(len(self.entries(first)), 1)
        self.assertEqual(self.entries(second), [])
        self.assertEqual(store.Card.read(first).frontmatter.get("timer"), None)

        # The reverse: the dashboard's timer is replaced by one started from the CLI.
        self.assertEqual(self.cli_start(first), 0)
        self.assertEqual(set(self.timers()), {first.stem})
        self.assertEqual(len(self.entries(second)), 1)
        status, board = self.call(base, "/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(set(board["meta"]["time_running"]), {first.stem})
        status, stopped = self.call(base, "/api/time-stop", {"ref": self.ref(first)})
        self.assertEqual(status, 200, stopped)
        self.assertEqual(stopped["meta"]["time_running"], {})
        self.assertEqual(len(self.entries(first)), 2)

    # An interrupted switch

    def test_a_switch_interrupted_between_its_two_writes_leaves_both_timers_running(self) -> None:
        old, new = self.add_task("Old timed task"), self.add_task("New timed task")
        clock = support.FakeClock(datetime(2026, 6, 15, 10, 0, 0))
        base = self.start_server()
        with clock.patch(store, operations, transaction):
            operations.op_time_start(self.ws, self.ref(old), sync=False)
            clock.advance(seconds=300)
            with mock.patch.object(operations, "_stop_timer_card", side_effect=OSError("interrupted")):
                with self.assertRaises(store.ConflictError) as raised:
                    operations.op_time_start(self.ws, self.ref(new), sync=False)
            self.assertIn("both timers are running", str(raised.exception))

            self.assertEqual(set(self.timers()), {old.stem, new.stem})
            status_text = self.status()
            self.assertIn("Old timed task", status_text)
            self.assertIn("New timed task", status_text)
            status, board = self.call(base, "/api/items")
            self.assertEqual(status, 200)
            self.assertEqual(set(board["meta"]["time_running"]), {old.stem, new.stem})
            # Each timer names its card, so the board can list both even when a filter hides one.
            self.assertEqual({timer["title"] for timer in board["meta"]["time_running"].values()},
                             {"Old timed task", "New timed task"})
            self.assertEqual(board["meta"]["time_running"][old.stem]["ref"], self.ref(old))
            new_start = self.timers()[new.stem]["start"]
            self.assertEqual(self.entries(old), [])

            clock.advance(seconds=60)
            result = operations.op_time_end(self.ws, query=self.ref(old), at=new_start, sync=False)
            self.assertTrue(result.changed)
        sessions = self.entries(old)
        self.assertEqual(len(sessions), 1)
        self.assertEqual((sessions[0]["end"], sessions[0]["elapsed_seconds"]), (new_start, 300))
        self.assertEqual(set(self.timers()), {new.stem})
        self.assertEqual(self.entries(new), [])

    # A pull that brings a second timer

    def test_a_pulled_second_timer_shows_validates_and_leaves_edits_working(self) -> None:
        mine, pulled, other = (self.add_task("My timer"), self.add_task("Pulled timer"),
                               self.add_task("Unrelated task"))
        self.assertEqual(self.cli_start(mine), 0)
        self.put_timer(pulled, by="tester", session_id="b" * 32)

        text = self.status()
        self.assertIn("My timer", text)
        self.assertIn("Pulled timer", text)
        base = self.start_server()
        status, board = self.call(base, "/api/items")
        self.assertEqual(set(board["meta"]["time_running"]), {mine.stem, pulled.stem})
        code, out, err = self.cli(cli_admin.command_validate)
        self.assertEqual(code, 0, err)
        operations.op_comment(self.ws, self.ref(other), ["Still editable"], sync=False)
        operations.op_comment(self.ws, self.ref(pulled), ["Edit beside a timer"], sync=False)
        self.assertEqual(set(self.timers()), {mine.stem, pulled.stem})
        # With several timers, stop asks which one.
        with self.assertRaises(store.ConflictError) as raised:
            operations.op_time_end(self.ws, sync=False)
        self.assertIn("multiple timers are running", str(raised.exception))
        operations.op_time_end(self.ws, query=self.ref(pulled), sync=False)
        self.assertEqual(set(self.timers()), {mine.stem})

    def test_validate_rejects_a_malformed_timer_and_a_timer_on_a_note(self) -> None:
        task = self.add_task("Broken timer")
        self.put_timer(task)
        properties, body = card_format.parse_document(task.read_text(encoding="utf-8"))
        for broken in ({**properties["timer"], "session_id": "short"}, {**properties["timer"], "by": "a\nb"},
                       {**properties["timer"], "start": "yesterday"}, {**properties["timer"], "start_epoch": "1"},
                       {key: value for key, value in properties["timer"].items() if key != "by"}, "running"):
            with self.subTest(timer=broken):
                task.write_text(card_format.dump_properties({**properties, "timer": broken}).join(["---\n", "---\n"]) + body,
                                encoding="utf-8")
                code, _out, err = self.cli(cli_admin.command_validate)
                self.assertEqual(code, 1)
                self.assertIn("timer", err)
        note, _created = operations.create_card("note", "Note body", title="A note", ws=self.ws)
        task.write_text(card_format.render_document(properties, body), encoding="utf-8")
        text = note.path.read_text(encoding="utf-8").replace("kind: note\n", "kind: note\ntimer:\n  session_id: " + SESSION + "\n")
        note.path.write_text(text, encoding="utf-8")
        self.assertEqual(self.cli(cli_admin.command_validate)[0], 1)

    # Upgrade

    def test_upgrade_imports_the_timer_file_onto_cards_and_keeps_unmatched_entries(self) -> None:
        with_owner = self.add_task("Timer with owner")
        legacy_claimed = self.add_task("Timer without owner", status="doing",
                                       claimed_by="alice", claimed_at="2026-06-15T09:00:00")
        unowned = self.add_task("Timer without owner or claim")
        epoch = int(datetime(2026, 6, 15, 9, 0, 0).timestamp())

        def entry(session: str, **extra: object) -> dict:
            return {"session_id": session * 32, "start": store.local_stamp(epoch), "start_epoch": epoch, **extra}

        legacy = {
            with_owner.stem: entry("a", by="bob"),
            legacy_claimed.stem: entry("b"),
            unowned.stem: entry("d"),
            "2026-01-01-000000-deleted-card": entry("c", by="tester"),
        }
        state = self.root / ".mt" / "time-running"
        state.parent.mkdir(parents=True, exist_ok=True)
        original = (json.dumps(legacy, indent=2) + "\n").encode("utf-8")
        state.write_bytes(original)
        before = {path: path.read_bytes() for path in (with_owner, legacy_claimed)}

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            timers = transaction.read_time_timers(self.ws)

        self.assertEqual(set(timers), {with_owner.stem, legacy_claimed.stem, unowned.stem})
        self.assertEqual(timers[with_owner.stem], legacy[with_owner.stem])
        self.assertEqual(timers[legacy_claimed.stem], {**legacy[legacy_claimed.stem], "by": "alice"})
        self.assertEqual(timers[unowned.stem]["by"], "tester")
        self.assertEqual(self.timers(), timers)
        self.assertFalse(state.exists())
        self.assertEqual((self.root / ".mt" / "time-running.migrated").read_bytes(), original)
        self.assertIn("2026-01-01-000000-deleted-card", err.getvalue())
        self.assertIn("time-running.migrated", err.getvalue())
        self.assertNotEqual(with_owner.read_bytes(), before[with_owner])

        # Idempotent: a second read changes nothing and says nothing.
        snapshot = {path: path.read_bytes() for path in (self.root / "cards").glob("*.md")}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(transaction.read_time_timers(self.ws), timers)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual({path: path.read_bytes() for path in (self.root / "cards").glob("*.md")}, snapshot)

    def test_upgrade_is_safe_when_the_card_already_holds_the_timer(self) -> None:
        task = self.add_task("Already migrated")
        timer = self.put_timer(task, by="tester")
        state = self.root / ".mt" / "time-running"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({task.stem: timer}), encoding="utf-8")
        before = task.read_bytes()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(set(transaction.read_time_timers(self.ws)), {task.stem})
        self.assertEqual(task.read_bytes(), before)
        self.assertEqual(err.getvalue(), "")
        self.assertFalse(state.exists())

    def test_a_pending_timer_file_is_imported_before_the_task_completes(self) -> None:
        task = self.add_task("Complete after upgrade")
        epoch = int(datetime(2026, 6, 15, 9, 0, 0).timestamp())
        state = self.root / ".mt" / "time-running"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({task.stem: {
            "session_id": SESSION, "start": store.local_stamp(epoch), "start_epoch": epoch, "by": "tester"}}),
            encoding="utf-8")
        operations.op_complete(self.ws, self.ref(task), sync=False)
        self.assertEqual([entry["session_id"] for entry in self.entries(task)], [SESSION])
        self.assertNotIn("timer", store.Card.read(task).frontmatter)

    def test_a_workspace_with_a_pending_timer_file_stays_out_of_demo_mode(self) -> None:
        app = self.root.parent / "app"
        (app / "examples").mkdir(parents=True)
        (app / "settings.yaml").write_text((SOURCE_ROOT / "templates" / "settings.yaml").read_text(encoding="utf-8"),
                                            encoding="utf-8")
        ws = Workspace.at(app, app_root=app)
        self.assertTrue(ws.pin_demo())
        (app / ".mt").mkdir()
        (app / ".mt" / "time-running").write_text("{}", encoding="utf-8")
        self.assertFalse(Workspace.at(app, app_root=app).pin_demo())

    # Concurrency and atomicity

    def test_simultaneous_starts_by_one_person_leave_one_running_timer(self) -> None:
        tasks = [self.add_task(f"Racing start {index}") for index in range(4)]
        barrier = threading.Barrier(len(tasks))

        def start(path: Path) -> None:
            barrier.wait(timeout=10)
            operations.op_time_start(self.ws, self.ref(path), sync=False)

        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            for future in [executor.submit(start, path) for path in tasks]:
                future.result(timeout=30)
        self.assertEqual(len(self.timers()), 1)
        recorded = sum(len(self.entries(path)) for path in tasks)
        self.assertEqual(recorded, len(tasks) - 1)

    def test_stop_is_one_card_write_that_removes_the_timer_and_records_the_session(self) -> None:
        task = self.add_task("Single write stop")
        operations.op_time_start(self.ws, self.ref(task), sync=False)
        with mock.patch.object(transaction, "apply_item_transaction",
                               wraps=transaction.apply_item_transaction) as write:
            operations.op_time_end(self.ws, query=self.ref(task), sync=False)
        self.assertEqual(write.call_count, 1)
        changes = write.call_args.args[0]
        self.assertEqual(list(changes), [f"cards/{task.name}"])
        written = card_format.parse_document(changes[f"cards/{task.name}"].decode("utf-8"))[0]
        self.assertNotIn("timer", written)
        self.assertEqual(len(written["time_entries"]), 1)
        previous = card_format.parse_document(write.call_args.kwargs["expected"][f"cards/{task.name}"].decode("utf-8"))[0]
        self.assertEqual(previous["timer"]["session_id"], written["time_entries"][0]["session_id"])

    def test_cancel_removes_the_timer_and_records_nothing(self) -> None:
        task = self.add_task("Cancelled timer")
        operations.op_time_start(self.ws, self.ref(task), sync=False)
        operations.op_time_end(self.ws, query=self.ref(task), action="cancel", sync=False)
        card = store.Card.read(task)
        self.assertNotIn("timer", card.frontmatter)
        self.assertNotIn("time_entries", card.frontmatter)


    # One snapshot decides and writes

    def replace_timer_after_snapshot(self, path: Path, session_id: str = "b" * 32) -> mock._patch:
        """Patch the timer snapshot so an external edit lands right after it, before any write."""
        real = transaction.read_time_timer_cards

        def snapshot(ws=None):
            found = real(ws)
            self.put_timer(path, by="bob", session_id=session_id)
            return found

        return mock.patch.object(transaction, "read_time_timer_cards", side_effect=snapshot)

    def test_stop_and_cancel_conflict_when_the_timer_is_replaced_after_the_snapshot(self) -> None:
        for action in ("stop", "cancel"):
            with self.subTest(action=action):
                task = self.add_task(f"Replaced under {action}")
                operations.op_time_start(self.ws, self.ref(task), sync=False)
                with self.replace_timer_after_snapshot(task):
                    with self.assertRaises(store.ConflictError) as raised:
                        operations.op_time_end(self.ws, query=self.ref(task), action=action, sync=False)
                self.assertIn("changed before transaction", str(raised.exception))
                card = store.Card.read(task)
                self.assertEqual(card.frontmatter["timer"]["by"], "bob")
                self.assertEqual(card.frontmatter["timer"]["session_id"], "b" * 32)
                self.assertEqual(self.entries(task), [])
                operations.op_time_end(self.ws, query=self.ref(task), action="cancel", by="bob", sync=False)

    def test_switch_conflicts_when_the_old_timer_is_replaced_after_the_snapshot(self) -> None:
        old, new = self.add_task("Old replaced timer"), self.add_task("New switched timer")
        operations.op_time_start(self.ws, self.ref(old), sync=False)
        with self.replace_timer_after_snapshot(old):
            with self.assertRaises(store.ConflictError) as raised:
                operations.op_time_start(self.ws, self.ref(new), sync=False)
        self.assertTrue(raised.exception.sync_needed)
        self.assertEqual(store.Card.read(old).frontmatter["timer"]["by"], "bob")
        self.assertEqual(self.entries(old), [])
        self.assertIn(new.stem, self.timers())

    def test_migration_does_not_overwrite_an_edit_made_after_it_loaded_the_card(self) -> None:
        task = self.add_task("Edited during upgrade")
        epoch = int(datetime(2026, 6, 15, 9, 0, 0).timestamp())
        state = self.root / ".mt" / "time-running"
        state.parent.mkdir(parents=True, exist_ok=True)
        legacy = {task.stem: {"session_id": SESSION, "start": store.local_stamp(epoch),
                              "start_epoch": epoch, "by": "tester"}}
        state.write_text(json.dumps(legacy), encoding="utf-8")
        real = transaction.task_by_id

        def loaded(task_id, ws=None):
            card = real(task_id, ws=ws)
            task.write_text(task.read_text(encoding="utf-8") + "\nEdited meanwhile.\n", encoding="utf-8")
            return card

        err = io.StringIO()
        with mock.patch.object(transaction, "task_by_id", side_effect=loaded), contextlib.redirect_stderr(err):
            self.assertEqual(transaction.read_time_timers(self.ws), {})
        self.assertIn("Edited meanwhile.", task.read_text(encoding="utf-8"))
        self.assertNotIn("timer", store.Card.read(task).frontmatter)
        self.assertIn("was not imported", err.getvalue())
        self.assertEqual(json.loads((self.root / ".mt" / "time-running.migrated").read_text(encoding="utf-8")), legacy)

    def test_a_demo_workspaces_old_timer_file_is_imported_onto_the_demo_card(self) -> None:
        app = self.root.parent / "app"
        shutil.copytree(SOURCE_ROOT / "examples", app / "examples")
        (app / "settings.yaml").write_text((SOURCE_ROOT / "templates" / "settings.yaml").read_text(encoding="utf-8"),
                                            encoding="utf-8")
        ws = Workspace.at(app, app_root=app, environ={**os.environ, "MT_USERNAME": "tester"})
        self.assertTrue(ws.pin_demo())
        ws.maybe_seed_demo()
        card = next(card for card in store.discover("task", ws=ws) if card.frontmatter.get("status") != "done")
        epoch = int(datetime(2026, 6, 15, 9, 0, 0).timestamp())
        state = app / ".mt" / "demo" / "time-running"
        state.write_text(json.dumps({store.card_task_id(card): {
            "session_id": SESSION, "start": store.local_stamp(epoch), "start_epoch": epoch, "by": "tester"}}),
            encoding="utf-8")
        self.assertEqual(set(transaction.read_time_timers(ws)), {store.card_task_id(card)})
        self.assertEqual(store.Card.read(card.path).frontmatter["timer"]["session_id"], SESSION)
        self.assertFalse(state.exists())
        self.assertTrue(state.with_name("time-running.migrated").exists())

    # The dashboard and timers

    def test_a_partly_applied_switch_from_the_dashboard_still_requests_a_sync(self) -> None:
        (self.root / "settings.local.yaml").write_text("rules:\n  auto_sync: true\n", encoding="utf-8")
        old, new = self.add_task("Old synced timer"), self.add_task("New synced timer")
        operations.op_time_start(self.ws, self.ref(old), sync=False)
        base = self.start_server()
        with mock.patch.object(operations, "_stop_timer_card", side_effect=OSError("interrupted")), \
                mock.patch.object(taskweb.BackgroundSync, "request", autospec=True) as request:
            status, payload = self.call(base, "/api/time-start", {"ref": self.ref(new)})
        self.assertEqual(status, 409)
        self.assertTrue(payload["conflict"])
        self.assertTrue(payload["sync_needed"])
        self.assertEqual(request.call_count, 1)

    def test_a_refresh_with_a_warm_cache_reads_no_cards_and_takes_no_lock(self) -> None:
        task = self.add_task("Timed on the board")
        operations.op_time_start(self.ws, self.ref(task), sync=False)
        cache = store.CardCache()
        first = taskweb.dashboard_payload(self.ws, cache)
        self.assertEqual(set(first["meta"]["time_running"]), {task.stem})
        with mock.patch.object(store.Card, "read", wraps=store.Card.read) as read, \
                mock.patch.object(transaction, "time_mutation_lock", wraps=transaction.time_mutation_lock) as lock:
            second = taskweb.dashboard_payload(self.ws, cache)
        self.assertEqual(read.call_count, 0)
        self.assertEqual(lock.call_count, 0)
        self.assertEqual(second["meta"]["time_running"], first["meta"]["time_running"])

    def test_a_pending_timer_file_is_imported_before_the_board_shows_timers(self) -> None:
        task = self.add_task("Timed before upgrade")
        epoch = int(datetime(2026, 6, 15, 9, 0, 0).timestamp())
        state = self.root / ".mt" / "time-running"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({task.stem: {
            "session_id": SESSION, "start": store.local_stamp(epoch), "start_epoch": epoch, "by": "tester"}}),
            encoding="utf-8")
        payload = taskweb.dashboard_payload(self.ws, store.CardCache())
        self.assertEqual(set(payload["meta"]["time_running"]), {task.stem})
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
