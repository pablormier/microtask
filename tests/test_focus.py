from __future__ import annotations

import re
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402
import card_format
import support  # noqa: E402


class FocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", "examples", ".DS_Store"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str) -> subprocess.CompletedProcess[str]:
        return support.run_cli(self.root, *args)

    def add_ready_task(
        self,
        title: str,
        *,
        priority: str = "medium",
        due: str | None = None,
        due_kind: str | None = None,
        project: str | None = None,
    ) -> Path:
        result = self.run_mt("add", title)
        self.assertEqual(result.returncode, 0, result.stderr)
        task = max(card_files(self.root, "task"), key=lambda path: path.stat().st_mtime_ns)
        fields, body = card_format.parse_document(task.read_text())
        fields.update(priority=priority, next_action=f"Start {title}.", updated="1999-01-01")
        if due is not None:
            fields.update(due=due, due_kind=due_kind or "deadline")
        if project is not None:
            fields["project"] = project
        task.write_text(card_format.render_document(fields, body))
        return task

    def short_ref(self, task: Path) -> str:
        return hashlib.sha256(task.stem.encode()).hexdigest()[:8]

    def read_focus_state(self) -> object:
        return json.loads((self.root / ".mt" / "focus").read_text(encoding="utf-8"))

    def test_focus_prefers_due_today_before_started_work(self) -> None:
        today = date.today().isoformat()
        active = self.add_ready_task("Due today active task", due=today)
        doing = self.add_ready_task("Doing task")
        self.assertEqual(self.run_mt("move", "Due today active task", "active").returncode, 0)
        self.assertEqual(self.run_mt("move", "Doing task", "doing").returncode, 0)

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Focus:", result.stdout)
        self.assertIn("Due today active task", result.stdout)
        self.assertIn("why:     due today", result.stdout)
        self.assertIn("restart: mt move @", result.stdout)
        self.assertEqual(re.search(r"(?m)^status: (.+)$", active.read_text()).group(1), "active")
        self.assertEqual(re.search(r"(?m)^status: (.+)$", doing.read_text()).group(1), "doing")

    def test_focus_prefers_high_overdue_before_started_work(self) -> None:
        overdue = (date.today() - timedelta(days=1)).isoformat()
        active = self.add_ready_task("High overdue active task", priority="high", due=overdue)
        self.add_ready_task("Doing task")
        self.assertEqual(self.run_mt("move", "High overdue active task", "active").returncode, 0)
        self.assertEqual(self.run_mt("move", "Doing task", "doing").returncode, 0)

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("High overdue active task", result.stdout)
        self.assertIn("why:     overdue and high priority", result.stdout)
        self.assertEqual(re.search(r"(?m)^status: (.+)$", active.read_text()).group(1), "active")

    def test_focus_treats_low_overdue_as_deadline_review_after_started_work(self) -> None:
        overdue = (date.today() - timedelta(days=1)).isoformat()
        active = self.add_ready_task("Low overdue active task", priority="low", due=overdue)
        self.add_ready_task("Doing task")
        self.assertEqual(self.run_mt("move", "Low overdue active task", "active").returncode, 0)
        self.assertEqual(self.run_mt("move", "Doing task", "doing").returncode, 0)

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Doing task", result.stdout)
        self.assertIn("why:     already started", result.stdout)
        self.assertEqual(re.search(r"(?m)^status: (.+)$", active.read_text()).group(1), "active")

    def test_focus_does_not_escalate_overdue_target_before_started_work(self) -> None:
        overdue = (date.today() - timedelta(days=1)).isoformat()
        active = self.add_ready_task("Soft target active task", priority="urgent", due=overdue, due_kind="target")
        self.add_ready_task("Doing task")
        self.assertEqual(self.run_mt("move", "Soft target active task", "active").returncode, 0)
        self.assertEqual(self.run_mt("move", "Doing task", "doing").returncode, 0)

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Doing task", result.stdout)
        self.assertIn("why:     already started", result.stdout)
        self.assertNotIn("why:     overdue and high priority", result.stdout)
        self.assertEqual(re.search(r"(?m)^status: (.+)$", active.read_text()).group(1), "active")

    def test_focus_does_not_treat_due_today_reminder_as_due_today(self) -> None:
        today = date.today().isoformat()
        self.add_ready_task("Reminder active task", priority="urgent", due=today, due_kind="reminder")
        self.assertEqual(self.run_mt("move", "Reminder active task", "active").returncode, 0)

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reminder active task", result.stdout)
        self.assertNotIn("why:     due today", result.stdout)

    def test_focus_reviews_low_overdue_before_normal_active(self) -> None:
        overdue = (date.today() - timedelta(days=1)).isoformat()
        active = self.add_ready_task("Normal active task")
        overdue_task = self.add_ready_task("Low overdue task", priority="low", due=overdue)
        self.assertEqual(self.run_mt("move", "Normal active task", "active").returncode, 0)
        self.assertEqual(self.run_mt("move", "Low overdue task", "active").returncode, 0)

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Low overdue task", result.stdout)
        self.assertIn("why:     needs deadline review", result.stdout)
        self.assertEqual(re.search(r"(?m)^status: (.+)$", active.read_text()).group(1), "active")
        self.assertEqual(re.search(r"(?m)^status: (.+)$", overdue_task.read_text()).group(1), "active")

    def test_focus_prefers_started_work_when_nothing_is_due_now(self) -> None:
        self.add_ready_task("Inbox task", priority="urgent")
        active = self.add_ready_task("Active task")
        self.add_ready_task("Doing task")
        self.assertEqual(self.run_mt("move", "Active task", "active").returncode, 0)
        self.assertEqual(self.run_mt("move", "Doing task", "doing").returncode, 0)

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Focus:", result.stdout)
        self.assertIn("Doing task", result.stdout)
        self.assertIn("why:     already started", result.stdout)
        self.assertIn("restart: mt read @", result.stdout)
        self.assertEqual(re.search(r"(?m)^status: (.+)$", active.read_text()).group(1), "active")

    def test_focus_prefers_nearby_deadline_before_later_higher_priority(self) -> None:
        self.add_ready_task(
            "Later urgent", priority="urgent", due=(date.today() + timedelta(days=20)).isoformat()
        )
        self.add_ready_task(
            "Soon urgent", priority="urgent", due=(date.today() + timedelta(days=10)).isoformat()
        )
        self.add_ready_task(
            "High task", priority="high", due=(date.today() + timedelta(days=1)).isoformat()
        )

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Focus:", result.stdout)
        self.assertIn("High task", result.stdout)
        self.assertIn("why:     deadline in the next 7 days", result.stdout)
        self.assertIn("restart: mt read @", result.stdout)

    def test_focus_respects_metadata_filters(self) -> None:
        self.add_ready_task("Wrong project task", priority="urgent", project="other")
        self.add_ready_task("Right project task", priority="medium", project="target")

        result = self.run_mt("focus", "--project", "target")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Right project task", result.stdout)
        self.assertNotIn("Wrong project task", result.stdout)

    def test_focus_json_is_read_only_and_keeps_deadlines_visible_behind_pin(self) -> None:
        today = date.today().isoformat()
        urgent = self.add_ready_task("New deadline", due=today)
        pinned = self.add_ready_task("Keep my choice", priority="low")
        self.assertEqual(self.run_mt("focus", f"@{self.short_ref(pinned)}").returncode, 0)
        before = (self.root / ".mt" / "focus").read_bytes()
        cards = {path: path.read_bytes() for path in card_files(self.root, "task")}

        result = self.run_mt("focus", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["selected"]["id"], pinned.stem)
        self.assertEqual(data["mode"], "manual")
        self.assertEqual(data["candidates"][0]["id"], urgent.stem)
        self.assertEqual(data["alerts"][0]["id"], urgent.stem)
        self.assertEqual((self.root / ".mt" / "focus").read_bytes(), before)
        self.assertEqual({path: path.read_bytes() for path in cards}, cards)

    def test_blocked_deferred_and_other_claims_are_alerts_not_candidates(self) -> None:
        today = date.today().isoformat()
        waiting = self.add_ready_task("Waiting deadline", due=today)
        someday = self.add_ready_task("Deferred deadline", due=today)
        owned = self.add_ready_task("Other actor deadline", due=today)
        ready = self.add_ready_task("Available work")
        for task, status in ((waiting, "waiting"), (someday, "someday")):
            result = self.run_mt("move", f"@{self.short_ref(task)}", status)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_mt("move", f"@{self.short_ref(owned)}", "doing", "--by", "teammate")
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self.run_mt("focus", "--json", "--by", "focus-reader")

        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([item["id"] for item in data["candidates"]], [ready.stem])
        self.assertEqual({item["id"]: item["action"] for item in data["alerts"]},
                         {waiting.stem: "followup", someday.stem: "review", owned.stem: "coordinate"})
        self.assertEqual(data["followup_ids"], [waiting.stem])
        own = self.run_mt("focus", "--json", "--by", "teammate")
        self.assertEqual(json.loads(own.stdout)["selected"]["id"], owned.stem)
        for task in (waiting, someday, owned):
            pin = self.run_mt("focus", f"@{self.short_ref(task)}", "--by", "focus-reader")
            self.assertNotEqual(pin.returncode, 0)
        self.assertFalse((self.root / ".mt" / "focus").exists())

    def test_upcoming_deadlines_use_earliest_time_before_priority(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        early = self.add_ready_task("Morning deadline", priority="low", due=tomorrow + "T09:00:00")
        self.add_ready_task("Evening deadline", priority="urgent", due=tomorrow + "T18:00:00")
        self.add_ready_task("Soft target", priority="urgent", due=date.today().isoformat(), due_kind="target")

        result = self.run_mt("focus", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["selected"]["id"], early.stem)
        self.assertEqual(data["selected"]["reason_key"], "due_soon")
        self.assertEqual(len(data["alerts"]), 2)
        self.assertFalse((self.root / ".mt" / "focus").exists())

    def test_upcoming_deadline_keeps_started_work_and_stays_in_alerts(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        deadline = self.add_ready_task("Prepare tomorrow", due=tomorrow)
        doing = self.add_ready_task("Finish current work")
        self.assertEqual(self.run_mt("move", f"@{self.short_ref(doing)}", "doing").returncode, 0)
        data = json.loads(self.run_mt("focus", "--json").stdout)
        self.assertEqual(data["selected"]["id"], doing.stem)
        self.assertEqual(data["candidates"][1]["id"], deadline.stem)
        self.assertEqual(data["alerts"][0]["id"], deadline.stem)

    def test_cycle_with_only_blocked_deadline_does_not_save_focus(self) -> None:
        waiting = self.add_ready_task("Only blocked work", due=date.today().isoformat())
        self.assertEqual(self.run_mt("move", f"@{self.short_ref(waiting)}", "waiting").returncode, 0)
        result = self.run_mt("focus", "--next", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIsNone(data["selected"])
        self.assertEqual(data["candidates"], [])
        self.assertEqual(data["alerts"][0]["action"], "followup")
        self.assertFalse((self.root / ".mt" / "focus").exists())

    def test_focus_query_pins_lower_ranked_open_task(self) -> None:
        today = date.today().isoformat()
        self.add_ready_task("Due today task", priority="urgent", due=today)
        pinned = self.add_ready_task("Manual focus task", priority="low")

        set_result = self.run_mt("focus", f"@{self.short_ref(pinned)}")
        result = self.run_mt("focus")

        self.assertEqual(set_result.returncode, 0, set_result.stderr)
        self.assertIn("Manual focus task", set_result.stdout)
        self.assertIn("why:     manually focused", set_result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Manual focus task", result.stdout)
        self.assertIn("why:     manually focused", result.stdout)
        self.assertEqual(self.read_focus_state(), {"task_id": pinned.stem, "mode": "manual"})

    def test_focus_set_flag_pins_task(self) -> None:
        self.add_ready_task("Urgent task", priority="urgent")
        pinned = self.add_ready_task("Explicitly focused task", priority="low")

        result = self.run_mt("focus", "--set", f"@{self.short_ref(pinned)}")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Explicitly focused task", result.stdout)
        self.assertIn("why:     manually focused", result.stdout)
        self.assertEqual(self.read_focus_state(), {"task_id": pinned.stem, "mode": "manual"})

    def test_focus_next_advances_from_ranked_focus_and_persists_cycle_state(self) -> None:
        first = self.add_ready_task("First urgent", priority="urgent")
        second = self.add_ready_task("Second high", priority="high")

        result = self.run_mt("focus", "--next")
        followup = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Second high", result.stdout)
        self.assertIn("why:     cycled focus", result.stdout)
        self.assertEqual(self.read_focus_state(), {"task_id": second.stem, "mode": "cycle"})
        self.assertEqual(followup.returncode, 0, followup.stderr)
        self.assertIn("Second high", followup.stdout)
        self.assertIn("why:     cycled focus", followup.stdout)
        self.assertTrue(first.exists())

    def test_focus_next_wraps_across_ranked_candidates(self) -> None:
        first = self.add_ready_task("First urgent", priority="urgent")
        second = self.add_ready_task("Second high", priority="high")

        first_cycle = self.run_mt("focus", "--next")
        second_cycle = self.run_mt("focus", "--next")

        self.assertEqual(first_cycle.returncode, 0, first_cycle.stderr)
        self.assertIn("Second high", first_cycle.stdout)
        self.assertEqual(second_cycle.returncode, 0, second_cycle.stderr)
        self.assertIn("First urgent", second_cycle.stdout)
        self.assertEqual(self.read_focus_state(), {"task_id": first.stem, "mode": "cycle"})

    def test_focus_next_advances_from_manual_focus_and_switches_mode(self) -> None:
        ranked = self.add_ready_task("Ranked urgent task", priority="urgent")
        pinned = self.add_ready_task("Pinned low task", priority="low")
        self.assertEqual(self.run_mt("focus", f"@{self.short_ref(pinned)}").returncode, 0)

        result = self.run_mt("focus", "--next")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ranked urgent task", result.stdout)
        self.assertIn("why:     cycled focus", result.stdout)
        self.assertEqual(self.read_focus_state(), {"task_id": ranked.stem, "mode": "cycle"})

    def test_focus_next_respects_metadata_filters(self) -> None:
        self.add_ready_task("Other project task", priority="urgent", project="other")
        first = self.add_ready_task("Target urgent", priority="urgent", project="target")
        second = self.add_ready_task("Target medium", priority="medium", project="target")

        result = self.run_mt("focus", "--next", "--project", "target")
        followup = self.run_mt("focus", "--project", "target")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Target medium", result.stdout)
        self.assertEqual(self.read_focus_state(), {"task_id": second.stem, "mode": "cycle"})
        self.assertEqual(followup.returncode, 0, followup.stderr)
        self.assertIn("Target medium", followup.stdout)
        self.assertNotIn("Other project task", result.stdout)
        self.assertTrue(first.exists())

    def test_unfocus_and_focus_clear_restore_ranked_focus(self) -> None:
        ranked = self.add_ready_task("Ranked urgent task", priority="urgent")
        pinned = self.add_ready_task("Pinned low task", priority="low")
        self.assertEqual(self.run_mt("focus", f"@{self.short_ref(pinned)}").returncode, 0)

        unfocus = self.run_mt("unfocus")
        self.assertEqual(unfocus.returncode, 0, unfocus.stderr)
        self.assertFalse((self.root / ".mt" / "focus").exists())
        self.assertIn("Ranked urgent task", unfocus.stdout)
        self.assertNotIn("Pinned low task", unfocus.stdout)

        self.assertEqual(self.run_mt("focus", f"@{self.short_ref(pinned)}").returncode, 0)
        clear = self.run_mt("focus", "--clear")
        self.assertEqual(clear.returncode, 0, clear.stderr)
        self.assertFalse((self.root / ".mt" / "focus").exists())
        self.assertIn("Ranked urgent task", clear.stdout)
        self.assertTrue(ranked.exists())

    def test_stale_focus_pin_is_ignored(self) -> None:
        ranked = self.add_ready_task("Ranked urgent task", priority="urgent")
        stale = self.add_ready_task("Deleted pinned task", priority="low")
        (self.root / ".mt").mkdir(exist_ok=True)
        (self.root / ".mt" / "focus").write_text(
            json.dumps({"task_id": stale.stem, "mode": "cycle"}) + "\n",
            encoding="utf-8",
        )
        stale.unlink()

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ranked urgent task", result.stdout)
        self.assertNotIn("Deleted pinned task", result.stdout)
        self.assertTrue(ranked.exists())

    def test_done_and_note_focus_pins_are_ignored(self) -> None:
        ranked = self.add_ready_task("Ranked urgent task", priority="urgent")
        done_task = self.add_ready_task("Done pinned task", priority="low")
        self.assertEqual(self.run_mt("done", f"@{self.short_ref(done_task)}").returncode, 0)
        (self.root / ".mt").mkdir(exist_ok=True)
        (self.root / ".mt" / "focus").write_text(
            json.dumps({"task_id": done_task.stem, "mode": "manual"}) + "\n",
            encoding="utf-8",
        )

        done_result = self.run_mt("focus")
        self.assertEqual(done_result.returncode, 0, done_result.stderr)
        self.assertIn("Ranked urgent task", done_result.stdout)
        self.assertNotIn("Done pinned task", done_result.stdout)

        notes = self.root / "cards"
        notes.mkdir(exist_ok=True)
        note = notes / "2026-01-01-120000-note.md"
        note.write_text(
            """---
id: 2026-01-01-120000-note
title: Pinned note
kind: note
priority: medium
project: null
topic: null
due: null
due_kind: null
created: 2026-01-01
updated: 2026-01-01
tags: []
---

# Pinned note

## Content

Note only.

## Notes

Note.

""",
            encoding="utf-8",
        )
        (self.root / ".mt" / "focus").write_text(
            json.dumps({"task_id": note.stem, "mode": "manual"}) + "\n",
            encoding="utf-8",
        )

        note_result = self.run_mt("focus")
        self.assertEqual(note_result.returncode, 0, note_result.stderr)
        self.assertIn("Ranked urgent task", note_result.stdout)
        self.assertNotIn("Pinned note", note_result.stdout)
        self.assertTrue(ranked.exists())

    def test_focus_filters_hide_manual_pin(self) -> None:
        pinned = self.add_ready_task("Pinned other project", priority="urgent", project="other")
        self.add_ready_task("Filtered project task", priority="medium", project="target")
        self.assertEqual(self.run_mt("focus", f"@{self.short_ref(pinned)}").returncode, 0)

        result = self.run_mt("focus", "--project", "target")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Filtered project task", result.stdout)
        self.assertNotIn("Pinned other project", result.stdout)

    def test_legacy_raw_focus_id_still_resolves_as_manual_focus(self) -> None:
        ranked = self.add_ready_task("Ranked urgent task", priority="urgent")
        pinned = self.add_ready_task("Legacy pinned task", priority="low")
        (self.root / ".mt").mkdir(exist_ok=True)
        (self.root / ".mt" / "focus").write_text(f"{pinned.stem}\n", encoding="utf-8")

        result = self.run_mt("focus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Legacy pinned task", result.stdout)
        self.assertIn("why:     manually focused", result.stdout)
        self.assertTrue(ranked.exists())

    def test_focus_rejects_invalid_set_clear_combinations(self) -> None:
        task = self.add_ready_task("Task to pin")

        cases = (
            ("focus", f"@{self.short_ref(task)}", "--set", "Task to pin"),
            ("focus", "--clear", "--set", "Task to pin"),
            ("focus", "--set", "Task to pin", "--project", "x"),
            ("focus", "--clear", "--project", "x"),
            ("focus", "--next", "--set", "Task to pin"),
            ("focus", "--next", "Task to pin"),
            ("focus", "--next", "--clear"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_mt(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)

    def test_followup_lists_waiting_tasks_by_due_then_priority(self) -> None:
        overdue = (date.today() - timedelta(days=1)).isoformat()
        future = (date.today() + timedelta(days=5)).isoformat()
        undated = self.add_ready_task("Undated waiting", priority="urgent")
        future_task = self.add_ready_task("Future waiting", priority="urgent", due=future)
        overdue_task = self.add_ready_task("Overdue waiting", priority="low", due=overdue)
        for title in ("Undated waiting", "Future waiting", "Overdue waiting"):
            self.assertEqual(self.run_mt("move", title, "waiting").returncode, 0)

        result = self.run_mt("followup")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Follow-ups (3 waiting):", result.stdout)
        self.assertLess(result.stdout.index("Overdue waiting"), result.stdout.index("Future waiting"))
        self.assertLess(result.stdout.index("Future waiting"), result.stdout.index("Undated waiting"))
        self.assertIn("cmds: mt read @", result.stdout)
        for task in (overdue_task, future_task, undated):
            self.assertEqual(re.search(r"(?m)^status: (.+)$", task.read_text()).group(1), "waiting")

    def test_followup_handles_empty_waiting_folder(self) -> None:
        result = self.run_mt("followup")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No waiting follow-ups.", result.stdout)


if __name__ == "__main__":
    unittest.main()
