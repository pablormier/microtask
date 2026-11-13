from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import date, timedelta
import urllib.error
import urllib.request
from urllib.parse import urlencode
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import taskweb
from tools.taskweb import load_js
from tools.web_queries import collection_results
sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_fixture import render_dashboard
from card_files import card_files  # noqa: E402


SOURCE_ROOT = Path(__file__).resolve().parent.parent
MDVIZ = SOURCE_ROOT / "tools" / "mdviz.py"


class WebDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        shutil.copy2(SOURCE_ROOT / "mt", self.root / "mt")
        shutil.copy2(SOURCE_ROOT / "mt-policy.yaml", self.root / "mt-policy.yaml")
        shutil.copytree(SOURCE_ROOT / "scripts", self.root / "scripts")
        shutil.copytree(SOURCE_ROOT / "tools", self.root / "tools")
        (self.root / "cards").mkdir()
        self.tasks = [
            self.write_task("active", "2026-01-01-120000-plan", "Plan the work", "active"),
            self.write_task("doing", "2026-01-02-120000-build", "Build the thing", "doing", due="2026-07-04", tags=["code", "urgent"]),
            self.write_task("inbox", "2026-01-03-120000-idea", "Vague idea", "inbox"),
            self.write_task("done", "2026-01-05-120000-finished", "Finished task", "done", completed="2026-01-05T17:30:00"),
        ]
        self.note = self.write_note("2026-01-04-120000-note", "Reference note")
        self.output = self.root / "dashboard.html"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_task(
        self,
        status: str,
        task_id: str,
        title: str,
        note: str,
        *,
        due: str | None = None,
        due_kind: str = "deadline",
        tags: list[str] | None = None,
        task_type: str = "task",
        next_action: str = "Do the next step",
        body: str = "",
        updated: str = "2026-01-01",
        completed: str | None = None,
        archived: bool = False,
    ) -> Path:
        tags_yaml = "[]" if not tags else "\n" + "\n".join(f"  - {tag}" for tag in tags)
        extra_body = body or f"Some **bold** notes for {note}.\n\n| a | b |\n| - | - |\n| 1 | $x^2$ |\n"
        completed_yaml = f"completed: {completed}\n" if completed is not None else ""
        path = self.root / "cards" / f"{task_id}.md"
        fields = {"id": task_id, "title": title, "kind": "task", "status": status,
                  "priority": "medium", "type": task_type, "created": "2026-01-01", "updated": updated,
                  "archived": archived, "tags": tags or [], "next_action": next_action, "project": None}
        if completed is not None:
            fields["completed"] = completed
        if due is not None:
            fields.update(due=due, due_kind=due_kind)
        path.write_text(taskweb.tasklib.card_format.render_document(fields, "Test content.\n\n## Notes\n\n" + extra_body + '\n## Extra context\n\n<svg onload=alert(1)></script><script>alert("xss")</script>'), encoding="utf-8")
        return path

    def write_note(
        self,
        note_id: str,
        title: str,
        *,
        archived: bool = False,
        needs_review: bool = False,
        priority: str = "none",
        content: str = "Some **bold** note content.",
    ) -> Path:
        path = self.root / "cards" / f"{note_id}.md"
        review_yaml = "needs_review: true\n" if needs_review else ""
        fields = {"id": note_id, "title": title, "kind": "note", "priority": priority,
                  "created": "2026-01-01", "updated": "2026-01-01", "archived": archived,
                  "needs_review": needs_review}
        path.write_text(taskweb.tasklib.card_format.render_document(fields, content), encoding="utf-8")
        return path

    def render_dashboard(self) -> subprocess.CompletedProcess[str]:
        return render_dashboard(self.root, self.output)

    def detail_json(self) -> str:
        return self.output.with_suffix('.details.json').read_text()

    def get_card(self, url: str, summary: dict) -> dict:
        return self.get_json(url + "api/item?" + urlencode({"id": summary["id"]}))["item"]

    def payload_json(self, key: str) -> str:
        return json.dumps(json.loads(self.output.with_suffix('.json').read_text())[key])

    def start_interactive_web(self, *arguments: str, cli: bool = False,
                              env: dict | None = None) -> tuple[subprocess.Popen[str], str]:
        command = ([str(self.root / "mt"), "web"] if cli else
                   [sys.executable, str(self.root / "tools" / "taskweb.py")])
        process = subprocess.Popen(
            [*command, *arguments, "--port", "0", *([] if env else ["--no-open"])],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        assert process.stdout is not None
        deadline = time.time() + 5
        url = ""
        while time.time() < deadline:
            line = process.stdout.readline().strip()
            if line:
                url = line
                break
            if process.poll() is not None:
                break
        if not url.startswith("http://127.0.0.1:"):
            stderr = process.stderr.read() if process.stderr else ""
            self.stop_interactive_web(process)
            self.assertTrue(url.startswith("http://127.0.0.1:"), stderr)
        return process, url

    def stop_interactive_web(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    def get_json(self, url: str) -> dict:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, url: str, token: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Microtask-Token": token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def test_shell_bootstrap_excludes_card_data(self) -> None:
        result = self.render_dashboard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.is_file())
        self.assertEqual(result.stdout.strip(), str(self.output))
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", rendered)
        self.assertIn("taskBoard()", rendered)
        config = json.loads(re.search(r'id="mt-config">(.*?)</script>', rendered).group(1))
        self.assertEqual(set(config), {"api_token", "workspace_key"})
        self.assertEqual(config["api_token"], "test-token")
        self.assertNotIn("Plan the work", rendered)
        self.assertNotIn('id="task-data"', rendered)
        self.assertNotIn('id="mt-meta"', rendered)

    def test_note_payload_uses_one_snapshot_for_text_and_concurrency_hash(self) -> None:
        # A concurrent writer may update the file after discovery. The payload
        # must still pair the loaded text with the hash of those same bytes.
        source = """
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, 'tools')
import taskweb
path = Path(sys.argv[1])
note = taskweb.tasklib.Card.read(path)
expected = hashlib.sha256(note.raw.encode('utf-8')).hexdigest()
path.write_bytes(note.raw.replace('Some **bold** note content.', 'Concurrent edit').encode('utf-8'))
payload = taskweb.task_to_dict(note)
print(json.dumps({'body': payload['body'], 'hash': payload['content_hash'], 'expected': expected}))
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.note)],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hash"], payload["expected"])
        self.assertIn("Some **bold** note content.", payload["body"])
        self.assertNotIn("Concurrent edit", payload["body"])

    def test_output_includes_all_cdn_libraries(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("@picocss/pico@2.1.1/css/pico.min.css", rendered)
        self.assertIn('layer(vendor)', rendered)
        self.assertIn("alpinejs@3.15.12/dist/cdn.min.js", rendered)
        self.assertIn("marked@15.0.12/lib/marked.umd.js", rendered)
        self.assertIn("dompurify@3.2.6/dist/purify.min.js", rendered)
        self.assertIn("katex@0.16.10/dist/katex.min.css", rendered)
        self.assertIn("katex@0.16.10/dist/katex.min.js", rendered)
        self.assertIn("katex@0.16.10/dist/contrib/auto-render.min.js", rendered)

    def test_shared_controls_and_keyboard_paths_are_present(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("button.mt-button:focus-visible", rendered)
        self.assertIn("button.mt-button:disabled", rendered)
        self.assertIn("button.mt-button[aria-busy=\"true\"]", rendered)
        self.assertIn("input.mt-field, select.mt-field, textarea.mt-field", rendered)
        self.assertIn(".mt-checkbox-filter", rendered)
        self.assertIn(".mt-segmented .mt-segment[aria-pressed=\"true\"]", rendered)
        self.assertRegex(rendered, r'<article class="mt-card[^\"]*" role="button" tabindex="0"')
        self.assertIn('@keydown.enter.stop.prevent="select(task, $event)"', rendered)
        self.assertIn('@keydown.space.stop.prevent="select(task, $event)"', rendered)
        self.assertIn('@keydown.tab="trapDetailFocus($event)"', rendered)
        self.assertIn("trapDialogFocus(event, dialog)", rendered)
        button_tags = re.findall(r"<button\b[^>]*>", rendered)
        self.assertTrue(button_tags)
        for tag in button_tags:
            self.assertRegex(tag, r'class="[^"]*\b(?:mt-button|mt-segment)\b')

    def test_pico_is_pinned_and_imported_in_the_vendor_layer(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('@import url("https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css") layer(vendor)', rendered)
        self.assertNotIn('<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico', rendered)

    def test_output_includes_titles_refs_next_actions_bodies(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        data = json.loads(
            self.payload_json("tasks")
        )
        titles = {item["title"] for item in data}
        self.assertEqual(titles, {"Plan the work", "Build the thing", "Vague idea", "Reference note", "Finished task"})
        finished = next(item for item in data if item["title"] == "Finished task")
        self.assertEqual(finished["completed"], "2026-01-05T17:30:00")
        self.assertFalse(finished["archived"])
        refs = {item["ref"] for item in data}
        expected_refs = {f"@{hashlib.sha256(p.stem.encode()).hexdigest()[:8]}" for p in [*self.tasks, self.note]}
        self.assertEqual(refs, expected_refs)
        self.assertTrue(all(item["next_action"] for item in data if item["kind"] == "task" and item["status"] != "done"))
        self.assertTrue(all("body" not in item for item in data))
        self.assertTrue(any(item["excerpt"] for item in data))

    def test_status_order_follows_policy(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        statuses_blob = self.payload_json("statuses")
        statuses = json.loads(statuses_blob)
        self.assertEqual(statuses, ["inbox", "active", "doing", "waiting", "someday", "done"])
        data = json.loads(self.payload_json("tasks"))
        data_statuses = [item["status"] for item in data if item["kind"] == "task"]
        self.assertEqual(sorted(data_statuses, key=statuses.index), data_statuses)

    def test_custom_status_and_labels_are_presentation_metadata(self) -> None:
        shared_policy = self.root / "mt-policy.yaml"
        shared_policy.write_text(
            shared_policy.read_text(encoding="utf-8").replace(
                '  done: "Done"\n', '  done: "Done"\n  review: "Review"\n', 1
            ),
            encoding="utf-8",
        )
        (self.root / "mt-policy.local.yaml").write_text(
            """statuses:
  inbox: Captured
  active: Ready
  doing: In progress
  waiting: Blocked
  done: Complete
  review: Needs review
view_labels:
  archive: History
""",
            encoding="utf-8",
        )
        custom = self.write_task("review", "2026-01-06-120000-review", "Review task", "review")

        result = self.render_dashboard()

        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = self.output.read_text(encoding="utf-8")
        statuses = json.loads(self.payload_json("statuses"))
        meta = json.loads(self.payload_json("meta"))
        data = json.loads(self.payload_json("tasks"))
        self.assertEqual(statuses, ["inbox", "active", "doing", "waiting", "someday", "done", "review"])
        self.assertEqual(meta["status_labels"]["active"], "Ready")
        self.assertEqual(meta["status_labels"]["review"], "Needs review")
        self.assertEqual(meta["view_labels"]["archive"], "History")
        self.assertEqual(next(item for item in data if item["id"] == custom.stem)["status"], "review")
        self.assertIn('x-text="statusText(status)"', rendered)
        self.assertIn('x-text="viewLabel(\'archive\')"', rendered)
        self.assertIn("mt-status-badge", rendered)
        self.assertIn("statusText(task.status)", rendered)

    def test_layout_uses_full_width_board_and_modal_preview(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn(".mt-groups", rendered)
        self.assertIn(".mt-detail-backdrop", rendered)
        self.assertIn('<section class="mt-detail mt-dialog" role="dialog"', rendered)
        self.assertNotIn('<aside class="mt-detail', rendered)
        self.assertIn(".mt-preview a { display: inline; }", rendered)
        self.assertIn('role="dialog"', rendered)
        self.assertIn("clearSelection()", rendered)
        self.assertNotIn("mt-detail-path", rendered)
        self.assertNotIn("Record information", rendered)
        self.assertIn('class="mt-detail-owner"', rendered)
        self.assertIn('class="mt-detail-layout"', rendered)
        self.assertIn('aria-label="Card content"', rendered)
        self.assertIn('aria-label="Card properties"', rendered)
        self.assertNotIn("<dt>Path</dt>", rendered)
        self.assertNotIn("mt-path", rendered)
        self.assertNotIn("grid-template-columns: 1fr 34rem", rendered)

    def test_toolbar_is_compact_and_accessible(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('<h1 class="mt-brand">', rendered)
        self.assertIn('<span class="mt-brand-mark" aria-hidden="true">MT</span>', rendered)
        self.assertIn('<span class="mt-brand-wordmark">MicroTask</span>', rendered)
        self.assertIn("<title>MicroTask</title>", rendered)
        self.assertIn('<search id="mt-filters" class="mt-filters"', rendered)
        self.assertEqual(rendered.count('<template x-for="facet in filterFacets()"'), 1)
        # The Filters toggle shows and hides the chip row and counts active filters.
        self.assertIn('x-show="filtersOpen && filtersApply()"', rendered)
        self.assertIn('aria-controls="mt-filters"', rendered)
        self.assertIn("activeFilterCount() + ' active'", rendered)
        self.assertEqual(rendered.count("@click=\"clearFilters()\""), 1)
        self.assertRegex(rendered, r'<button\b[^>]*class="mt-button mt-button-compact mt-filter-chip"')
        self.assertIn('aria-label="Search tasks"', rendered)
        self.assertIn(":aria-label=\"'Clear ' + facet.label + ' filter'\"", rendered)
        self.assertIn(":aria-multiselectable=\"String(facet.multi)\"", rendered)
        self.assertIn("activeFilterCount() > 0", rendered)
        self.assertIn("clearFilters()", rendered)
        self.assertIn('<label class="mt-checkbox-filter"><input type="checkbox" x-model="showHidden">', rendered)
        self.assertIn(".mt-title-row .mt-mode-toggle { margin: 0; }", rendered)
        self.assertIn('role="group" aria-label="Dashboard mode"', rendered)
        self.assertIn(':aria-pressed="mode === \'tasks\'"', rendered)
        self.assertIn("mt-sr-only", rendered)

    def test_focus_and_board_mode_controls_are_present(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('aria-label="Dashboard mode"', rendered)
        self.assertIn("mt-title-row", rendered)
        self.assertIn("mt-mode-toggle", rendered)
        self.assertNotIn("mt-mode-bar", rendered)
        self.assertIn("@click=\"mode = 'tasks'\"", rendered)
        self.assertIn("@click=\"mode = 'focus'\"", rendered)
        self.assertIn("@click=\"mode = 'notes'\"", rendered)
        self.assertIn("@click=\"mode = 'archive'\"", rendered)
        self.assertIn("x-text=\"viewLabel('archive')\"", rendered)
        self.assertIn("doneSelected()", rendered)
        self.assertIn("mt-cards-view", rendered)
        self.assertNotIn("mt-notes-grid", rendered)
        self.assertNotIn("mt-archive-grid", rendered)
        self.assertIn('.mt-segmented .mt-segment[aria-pressed="true"]', rendered)
        self.assertIn(':aria-pressed="mode === \'tasks\'"', rendered)
        self.assertIn('aria-label="Card kind"', rendered)
        self.assertIn("settings.archive.kind === 'task'", rendered)
        self.assertIn("settings.archive.kind === 'note'", rendered)
        self.assertIn("settings.archive.kind === 'all'", rendered)
        self.assertIn('<template x-if="mode === \'archive\'">', rendered)
        self.assertNotIn("KIND_ITEMS", rendered)
        self.assertIn('@change="settings[mode].group = $event.target.value"', rendered)
        self.assertIn('x-for="group in viewGroups()"', rendered)
        self.assertIn("collectionItems(name)", rendered)
        self.assertNotIn("filteredNotes", rendered)
        self.assertNotIn("sortedNotes", rendered)
        self.assertNotIn("archiveKind", rendered)
        self.assertNotIn("mode === 'board'", rendered)
        self.assertIn("Needs review only", rendered)
        self.assertEqual(rendered.count("Needs review only"), 1)
        self.assertIn("No open focus task.", rendered)
        self.assertIn("mt-focus-list", rendered)
        self.assertIn('x-if="isCardsView()"', rendered)
        self.assertIn('x-if="mode === \'focus\'"', rendered)
        self.assertIn('<section class="mt-overview">', rendered)

    def test_group_layout_selector_and_layout(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('aria-label="Group layout"', rendered)
        self.assertIn('title="Columns"', rendered)
        self.assertIn('title="Rows"', rendered)
        self.assertIn("setLayout('vertical')", rendered)
        self.assertIn("setLayout('horizontal')", rendered)
        self.assertIn(":aria-pressed=\"layout === 'vertical'\"", rendered)
        self.assertIn(":aria-pressed=\"layout === 'horizontal'\"", rendered)
        self.assertIn("'is-horizontal': settings[mode].group !== 'none' && layout === 'horizontal'", rendered)
        self.assertIn("mt-column-cards", rendered)
        self.assertIn(".mt-groups.is-horizontal", rendered)
        self.assertIn(".mt-groups.is-horizontal .mt-column-cards", rendered)
        self.assertIn(".mt-groups.is-flat", rendered)
        self.assertIn("align-items: stretch;", rendered)
        self.assertIn("mt-board-orientation", rendered)
        self.assertNotIn("mt-board-layout", rendered)
        self.assertNotIn("boardOrientation", rendered)

    def test_notes_data_includes_content_excerpt(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        data = json.loads(self.detail_json())
        note = next(item for item in data if item["kind"] == "note")
        self.assertEqual(note["excerpt"], "Some bold note content.")
        self.assertEqual(note["body"], "Some **bold** note content.")
        self.assertRegex(note["content_hash"], r"^[0-9a-f]{64}$")
        self.assertFalse(note["needs_review"])

    def test_note_detail_keeps_guidance_property_separate_from_body(self):
        fields, body = taskweb.tasklib.card_format.parse_document(self.note.read_text())
        fields["processing_instructions"] = "Review this note.\n\n## Content\n\n```python\nunfinished"
        self.note.write_text(taskweb.tasklib.card_format.render_document(fields, body))
        item = taskweb.tasklib.Card.read(self.note)
        payload = taskweb.task_to_dict(item, taskweb.tasklib.ReferenceIndex([item]))
        self.assertEqual(payload["body"], body)
        self.assertEqual(payload["processing_instructions"], fields["processing_instructions"])
        self.assertNotIn("sections", payload)

    def test_tasks_data_includes_intent_excerpt(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        data = json.loads(self.detail_json())
        task = next(item for item in data if item["kind"] == "task")
        self.assertTrue(task["excerpt"].startswith("Test content."))
        self.assertEqual(task["body"], taskweb.tasklib.card_format.parse_document((self.root / task["path"]).read_text())[1])

    def test_task_detail_exposes_one_body_and_independent_markdown_properties(self):
        item = taskweb.tasklib.Card.read(self.tasks[0])
        target = taskweb.tasklib.Card.read(self.note)
        fields = dict(item.frontmatter)
        body = f"Keep **Markdown** and link @{target.short_ref}.\n\n```python\n## Notes\nunfinished"
        fields["next_action"] = f"Read **details** in @{target.short_ref}"
        self.tasks[0].write_text(taskweb.tasklib.card_format.render_document(fields, body))
        item = taskweb.tasklib.Card.read(self.tasks[0])
        payload = taskweb.task_to_dict(item, taskweb.tasklib.ReferenceIndex([item, target]))
        self.assertEqual(payload["body"], body)
        self.assertEqual(payload["next_action"], fields["next_action"])
        self.assertNotIn("sections", payload)
        self.assertNotIn("content", payload)
        self.assertNotIn("edit_source", payload)
        self.assertEqual(payload["reference_spans"]["body"][0]["target"]["id"], target.frontmatter["id"])

    def test_excerpt_and_shared_card_regressions(self) -> None:
        import importlib.util

        self.assertEqual(self.render_dashboard().returncode, 0)
        self.assertNotIn(':class="[cardClass(', self.output.read_text())
        spec = importlib.util.spec_from_file_location("taskweb_excerpt_test", SOURCE_ROOT / "tools/taskweb.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = {
            "Compute x - y with snake_case_names": "Compute x - y with snake_case_names",
            '[Link](https://example.com/path_(v2) "Title")': "Link",
            "~~removed~~ and ![alt](image.png)": "removed and alt",
            "# Heading\n- First\n- Second": "Heading First Second",
            "Use **bold**, _emphasis_ and `code`": "Use bold, emphasis and code",
        }
        for source, expected in cases.items():
            self.assertEqual(module.extract_excerpt(source), expected)
        self.assertEqual(module.extract_excerpt('[A long label](https://example.com)', 10), "A long...")
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        probe = (
            'const fs=require("fs"), assert=require("assert/strict");\n'
            f'const cases={json.dumps(cases)};\n'
            'global.document={getElementById:id=>({textContent:JSON.stringify(id==="mt-meta"?{}:[])})};\n'
            f'eval({json.dumps(load_js(SOURCE_ROOT / "tools/web"))} + `\n'
            'const app=taskBoard();\n'
            'for(const [source,expected] of Object.entries(cases)) assert.equal(app.cleanExcerpt(source),expected);\n'
            'const done={kind:"task",status:"done",completed:"2026-01-05T17:30:00",updated:"2026-01-06"};\n'
            'assert.equal(app.cardClass(done)["is-done"],true); assert.equal(app.cardDate(done),done.completed);\n'
            'const archived={kind:"task",status:"active",archived:true,updated:"2026-01-06"};\n'
            'app.mode="archive"; assert.equal(app.cardDate(archived),archived.updated);\n'
            'app.mode="tasks"; assert.equal(app.cardDate(archived),"");\n'
            'assert.equal(app.dateLabel("2026-01-05"),app.deadlineDateLabel({due:"2026-01-05"}));\n'
            'assert.equal(app.dateLabel("invalid"),"invalid");\n'
            '`);'
        )
        result = subprocess.run([node], input=probe, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboard_includes_saved_time_totals(self) -> None:
        task = self.tasks[0]
        raw = task.read_text(encoding="utf-8")
        fields, body = taskweb.tasklib.card_format.parse_document(raw)
        fields["time_entries"] = [{"session_id":"a"*32, "start":"2026-01-01T10:00:00", "end":"2026-01-01T10:02:05", "elapsed_seconds":125}]
        raw = taskweb.tasklib.card_format.render_document(fields, body)
        task.write_text(raw, encoding="utf-8")

        self.render_dashboard()

        rendered = self.output.read_text(encoding="utf-8")
        data = json.loads(self.payload_json("tasks"))
        timed_task = next(item for item in data if item["id"] == task.stem)
        self.assertEqual(timed_task["time_seconds"], 125)
        self.assertIn("savedTimeLabel(task)", rendered)
        self.assertIn("mt-time-controls", rendered)

    def test_deadlines_view_renders_title_and_description(self) -> None:
        self.write_task(
            "active",
            "2026-01-06-120000-soft-target",
            "Soft target task",
            "soft target",
            due="2026-08-10",
            due_kind="target",
        )
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("mt-deadline-main", rendered)
        self.assertIn("mt-deadline-title", rendered)
        self.assertIn("mt-deadline-desc", rendered)
        self.assertIn('x-show="row.item.excerpt"', rendered)
        self.assertIn("mt-due-kind", rendered)
        self.assertIn("hasDeadlineDue(row.item) ? 'is-hard' : 'is-soft'", rendered)
        self.assertIn(".mt-due-kind.is-hard", rendered)
        self.assertIn(".mt-due-kind.is-soft", rendered)
        # The deadlines panel lists hard deadlines and soft targets/reminders.
        # The grouping select defaults to deadline type, whose group order comes
        # from the policy-defined due kinds; other fields group dynamically.
        self.assertIn('x-model="settings.deadlines.group"', rendered)
        self.assertIn('deadlines: { group: "due_kind" }', rendered)
        self.assertIn('x-for="row in deadlineRows()"', rendered)
        self.assertIn("mt-deadline-group-title", rendered)
        self.assertIn("mt-view-controls", rendered)
        self.assertIn('aria-label="Date"', rendered)
        self.assertNotIn("mt-deadline-controls", rendered)
        self.assertIn('x-show="settings.deadlines.group !== \'due_kind\'"', rendered)
        self.assertIn("this.groupCards(items, field)", rendered)
        self.assertIn('x-text="row.group"', rendered)

    def test_deadlines_view_renders_year_progress_bar_by_default(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("mt-year-progress", rendered)
        self.assertIn("x-text=\"yearProgressLabel()\"", rendered)
        self.assertIn("x-text=\"yearTimeLeft()\"", rendered)
        self.assertIn("yearTimeLeft() {", rendered)
        self.assertIn("return this.spanUntil(this.yearEnd());", rendered)
        self.assertIn("showYearProgress", rendered)
        self.assertIn("new Date(now.getFullYear() + 1, 0, 1)", rendered)
        self.assertIn("return this.elapsedPercent(start, end);", rendered)
        self.assertIn("pct.toFixed(2)", rendered)
        self.assertIn("width:' + yearProgressPercent() + '%'", rendered)
        self.assertNotIn("progressColor(", rendered)
        self.assertIn('role="progressbar" aria-label="Year progress"', rendered)

    def test_deadlines_view_renders_progress_bar_per_entry_by_default(self) -> None:
        self.write_task(
            "active",
            "2026-01-06-120000-progress-window",
            "Windowed task",
            "progress window",
            due="2026-08-10",
            due_kind="deadline",
        )
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('x-if="showDeadlineProgress"', rendered)
        self.assertIn("mt-deadline-progress", rendered)
        self.assertIn("mt-progress-track", rendered)
        self.assertIn("mt-progress-fill", rendered)
        self.assertIn("x-text=\"deadlineProgressLabel(row.item)\"", rendered)
        self.assertIn("width:' + deadlineProgressPercent(row.item) + '%'", rendered)
        self.assertNotIn("progressColor(", rendered)
        self.assertIn('role="progressbar" :aria-label="\'Time elapsed toward \' + row.item.title"', rendered)
        self.assertIn("this.showDeadlineProgress = meta.show_deadline_progress !== false", rendered)
        self.assertIn('meta.deadline_progress_start || "created"', rendered)
        self.assertIn("let start = item && item.created;", rendered)
        self.assertIn('if (months >= 1) parts.push(months + "mo");', rendered)
        self.assertIn('if (weeks >= 1) parts.push(weeks + "w");', rendered)

    def test_deadlines_view_hides_year_progress_bar_via_local_config(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            "rules:\n  show_year_progress: false\n",
            encoding="utf-8",
        )
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('x-if="showYearProgress"', rendered)
        self.assertIn("this.showYearProgress = meta.show_year_progress !== false", rendered)
        meta_blob = self.payload_json("meta")
        meta = json.loads(meta_blob)
        self.assertFalse(meta["show_year_progress"])
        self.assertTrue(meta["show_deadline_progress"])

    def test_deadlines_view_hides_entry_progress_bars_via_local_config(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            "rules:\n  show_deadline_progress: false\n",
            encoding="utf-8",
        )
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        meta_blob = self.payload_json("meta")
        meta = json.loads(meta_blob)
        self.assertFalse(meta["show_deadline_progress"])
        self.assertTrue(meta["show_year_progress"])
        self.assertIn("this.showDeadlineProgress = meta.show_deadline_progress !== false", rendered)

    def test_deadline_progress_anchor_from_local_config(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            "rules:\n  deadline_progress_start: created\n",
            encoding="utf-8",
        )
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        meta_blob = self.payload_json("meta")
        meta = json.loads(meta_blob)
        self.assertEqual(meta["deadline_progress_start"], "created")
        self.assertIn("let start = item && item.created;", rendered)

    def test_cards_collection_lists_tasks_by_rank_then_notes_latest_first(self) -> None:
        self.render_dashboard()
        cards = json.loads(self.payload_json("collections"))["cards"]
        self.assertEqual(cards[-1], self.note.stem)

    def test_embedded_policy_metadata_includes_priority_order_and_wip_limit(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        meta_blob = self.payload_json("meta")
        meta = json.loads(meta_blob)
        self.assertEqual(meta["priority_order"], ["urgent", "high", "medium", "low", "none"])
        self.assertEqual(meta["due_kinds"], ["deadline", "target", "reminder"])
        self.assertEqual(meta["max_doing_tasks"], 5)
        self.assertEqual(meta["focus"]["mode"], "auto")
        self.assertTrue(meta["focus"]["candidates"])
        self.assertIn("alerts", meta["focus"])
        self.assertTrue(meta["show_year_progress"])
        self.assertTrue(meta["show_deadline_progress"])
        self.assertEqual(meta["deadline_progress_start"], "created")
        self.assertIn("let start = item && item.created;", rendered)
        self.assertNotIn("interactive", meta)
        self.assertEqual(meta["api_token"], "test-token")
        self.assertIn("const MT_CONFIG = JSON.parse", rendered)
        self.assertIn("this.maxDoingTasks = meta.max_doing_tasks", rendered)

    def test_dashboard_renders_shared_detail_controls(self) -> None:
        self.assertEqual(self.render_dashboard().returncode, 0)
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('aria-label="Task status"', rendered)
        self.assertIn('@change="changeSelectedStatus()"', rendered)
        self.assertIn('aria-label="Task timer"', rendered)
        self.assertIn('aria-label="Card properties"', rendered)
        self.assertIn('aria-label="Card content"', rendered)
        self.assertIn('aria-label="Content Markdown"', rendered)
        self.assertIn('disabled = busy || draftBusy', rendered)
        self.assertIn('>Edit content</button>', rendered)
        self.assertIn('>Save content</button>', rendered)
        self.assertNotIn('>Edit properties</button>', rendered)
        self.assertNotIn('class="mt-extra-details"', rendered)
        self.assertIn('class="mt-content-footer"', rendered)
        self.assertIn('class="mt-detail-footer"', rendered)
        self.assertNotIn('Record information', rendered)
        self.assertIn('propertyEditor.errors, selected?.kind)', rendered)

    def test_capture_exposes_kind_and_primary_properties_with_shared_editor(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        body_index = rendered.index('id="mt-composer-body"')
        kind_index = rendered.index('aria-label="Create a note or task"')
        properties_index = rendered.index('class="mt-capture-properties"')
        self.assertLess(kind_index, properties_index)
        self.assertLess(properties_index, body_index)
        self.assertNotIn("Track as task", rendered)
        self.assertNotIn("<summary>Task options</summary>", rendered)
        self.assertIn('x-text="captureComposer.title()"', rendered)
        self.assertIn('x-text="captureComposer.saveLabel()"', rendered)
        self.assertIn('class="mt-metadata-primary"', rendered)
        self.assertIn("More properties", rendered)
        self.assertIn('<span>New</span>', rendered)
        self.assertNotIn("New note draft", rendered)
        self.assertIn(':selected="priority === value.priority"', rendered)
        self.assertIn(':selected="type === value.type"', rendered)
        self.assertIn(':selected="status === value.status"', rendered)
        self.assertIn(':selected="kind === value.due_kind"', rendered)
        self.assertIn(".mt-card-metadata-field small {", rendered)
        self.assertIn("margin: 0;", rendered)
        self.assertIn(".mt-metadata-create .mt-metadata-primary", rendered)
        self.assertIn(".mt-composer-content {", rendered)
        self.assertIn("overflow: auto;", rendered)

    def test_properties_reset_conflict_and_save_preserve_user_changes(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        self.assertEqual(self.render_dashboard().returncode, 0)
        rendered = self.output.read_text()
        data = json.loads(self.payload_json("tasks"))
        meta = json.loads(self.payload_json("meta"))
        probe = (
            'const fs = require("fs"), assert = require("assert/strict");\n'
            f'const data = {json.dumps(data)}, meta = {json.dumps(meta)};\n'
            'global.document = {getElementById: id => ({textContent: JSON.stringify('
            'id === "task-data" ? data : id === "mt-statuses" || id === "mt-drafts" ? [] : meta)})};\n'
            f'eval({json.dumps(load_js(self.root / "tools/web"))} + `\n'
            '(async () => {\n'
            'const app = taskBoard(); app.applyPayload({tasks: typeof data === "undefined" ? [] : data, meta: typeof meta === "undefined" ? {} : meta, statuses: typeof statuses === "undefined" ? [] : statuses}); app.ready = true; app.$nextTick = () => {}; \n'
            'app.selected = data.find(t => t.kind === "task"); app.selectedId = app.selected.id;\n'
            'app.propertyEditor.begin(app.selected, app.taskDefaults); const original = app.propertyEditor.form.project;\n'
            'const version = app.propertyEditor.version;\n'
            'app.propertyEditor.form.project = "discard me"; app.propertyEditor.begin(app.selected, app.taskDefaults);\n'
            'assert.ok(app.propertyEditor.version > version); assert.equal(app.propertyEditor.form.project, original);\n'
            'app.propertyEditor.form.project = "keep me";\n'
            'app.apiPost = async () => null; await app.propertyEditor.apply(app.propertyContext());\n'
            'assert.equal(app.propertyEditor.form.project, "keep me");\n'
            'app.renderSelectedPreview = () => {};\n'
            'app.applyPayload({tasks: data.map(t => t.id === app.selectedId ? {...t, content_hash: "new-hash"} : t), meta: {...meta, interactive: true}});\n'
            'assert.equal(app.propertyEditor.conflict, true); assert.equal(app.propertyEditor.canApply(app.propertyContext()), false);\n'
            'assert.equal(app.propertyEditor.form.project, "keep me");\n'
            'app.propertyEditor.begin(app.selected, app.taskDefaults); app.propertyEditor.form.project = "saved project";\n'
            'app.apiPost = async (path, body) => {\n'
            'assert.equal(path, "/api/card-metadata"); assert.equal(body.expected_hash, "new-hash");\n'
            'assert.deepEqual(body.patch, {project: "saved project"});\n'
            'app.selected = {...app.selected, project: body.patch.project}; return {saved: true}; };\n'
            'await app.propertyEditor.apply(app.propertyContext()); assert.equal(app.propertyEditor.changed(), false);\n'
            'app.selected = {...data.find(t => t.kind === "note"), due: "2026-12-20T09:30:45", due_kind: "target"};\n'
            'app.propertyEditor.begin(app.selected, app.taskDefaults); assert.equal(app.propertyEditor.changed(), false);\n'
            'app.propertyEditor.form.priority = "high"; app.propertyEditor.form.tags = ["a,b"];\n'
            'app.apiPost = async () => null; await app.propertyEditor.apply(app.propertyContext());\n'
            'assert.equal(app.propertyEditor.form.priority, "high");\n'
            'app.apiPost = async (path, body) => {assert.equal(path, "/api/card-metadata");\n'
            'assert.deepEqual(body.patch, {priority: "high", tags: ["a,b"]});\n'
            'app.selected = {...app.selected, ...body.patch}; return {saved: true};};\n'
            'await app.propertyEditor.apply(app.propertyContext()); assert.equal(app.propertyEditor.changed(), false);\n'
            '})().catch(error => {console.error(error); process.exitCode = 1;});\n'
            '`);'
        )
        result = subprocess.run([node], input=probe, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hidden_items_stay_in_deadlines_but_out_of_cards_and_focus(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        for path in (self.tasks[1], self.note):
            result = subprocess.run(
                [str(self.root / "mt"), "edit", path.stem, "--set", "hidden=true",
                 "--set", "due=2026-12-20", "--set", "due_kind=target"],
                cwd=self.root, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        archived = self.write_task("active", "2026-06-01-120000-archived", "Archived date", "archived",
                                   due="2026-12-01", archived=True)
        result = subprocess.run(
            [str(self.root / "mt"), "edit", self.tasks[3].stem, "--set", "due=2026-12-20",
             "--set", "due_kind=target"], cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.render_dashboard().returncode, 0)
        rendered = self.output.read_text()
        data = json.loads(self.payload_json("tasks"))
        meta = json.loads(self.payload_json("meta"))
        self.assertEqual({item["id"] for item in data if item["hidden"]}, {self.tasks[1].stem, self.note.stem})
        js_source = json.dumps(load_js(self.root / "tools/web"))
        probe = (
            'const fs = require("fs");\n'
            f'const data = {json.dumps(data)}; const meta = {json.dumps(meta)};\n'
            f'const normalCollections = {json.dumps(collection_results(data))}; const hiddenCollections = {json.dumps(collection_results(data, {"show_hidden": True}))};\n'
            'global.document = {getElementById: id => ({textContent: JSON.stringify('
            'id === "task-data" ? data : id === "mt-statuses" ? [] : meta)})};\n'
            f'eval({js_source} + `\n'
            'const app = taskBoard(); app.collections = normalCollections; app.applyPayload({tasks: typeof data === "undefined" ? [] : data, meta: typeof meta === "undefined" ? {} : meta, statuses: typeof statuses === "undefined" ? [] : statuses}); app.ready = true; const ids = items => items.map(t => t.id); const cards = kind => app.collectionItems("cards").filter(t => t.kind === kind);\n'
            'const normal = {doing: ids(cards("task").filter(t => t.status === "doing")), notes: ids(cards("note")), '
            'focus: app.focusTask().id, dates: ids(app.deadlineItems()), count: app.doingCount()};\n'
            'app.showHidden = true; app.collections = hiddenCollections;\n'
            'const included = {doing: ids(cards("task").filter(t => t.status === "doing")), notes: ids(cards("note")), '
            'focus: app.focusTask().id};\n'
            'app.tasks.push({...data.find(t => t.hidden), id: "hidden-followup", status: "waiting"});\n'
            'const followups = ids(app.followupTasks());\n'
            'app.selected = data.find(t => t.hidden); \n'
            'let request; app.apiPost = (path, payload) => { request = {path, payload}; };\n'
            'app.hideSelected(); app.clearFilters();\n'
            'console.log(JSON.stringify({normal, included, followups, request, reset: app.showHidden}));\n'
            '`);\n'
        )
        result = subprocess.run([node], input=probe, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["normal"]["doing"], [])
        self.assertEqual(payload["normal"]["notes"], [])
        self.assertNotEqual(payload["normal"]["focus"], self.tasks[1].stem)
        self.assertEqual(set(payload["normal"]["dates"]), {self.tasks[1].stem, self.note.stem})
        self.assertNotIn(archived.stem, payload["normal"]["dates"])
        self.assertNotIn(self.tasks[3].stem, payload["normal"]["dates"])
        self.assertEqual(payload["normal"]["count"], 1)
        self.assertEqual(payload["included"]["doing"], [self.tasks[1].stem])
        self.assertEqual(payload["included"]["notes"], [self.note.stem])
        self.assertNotEqual(payload["included"]["focus"], self.tasks[1].stem)
        self.assertEqual(payload["followups"], [])
        self.assertEqual(payload["request"]["path"], "/api/hidden")
        self.assertIs(payload["request"]["payload"]["hidden"], False)
        self.assertIs(payload["reset"], False)

    def test_timer_button_state_and_fixed_width_time_format(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        js_source = json.dumps(load_js(self.root / "tools/web"))
        probe = (
            'const fs = require("fs");\n'
            'global.document = {getElementById: id => ({textContent: JSON.stringify('
            'id === "task-data" ? [] : id === "mt-statuses" ? [] : {interactive: true})})};\n'
            f'eval({js_source} + `\n'
            'const app = taskBoard(); app.applyPayload({tasks: typeof data === "undefined" ? [] : data, meta: typeof meta === "undefined" ? {} : meta, statuses: typeof statuses === "undefined" ? [] : statuses}); app.ready = true;\n'
            'const task = {id: "a", kind: "task", status: "active", time_seconds: 3712};\n'
            'app.now = 12008;\n'
            'const idle = {start: app.showStartTimer(task), stop: app.showStopTimer(task)};\n'
            'app.runningTimers = {a: {start_epoch: 0, saved: false}};\n'
            'const running = {start: app.showStartTimer(task), stop: app.showStopTimer(task), '
            'session: app.timerSessionLabel(task), total: app.savedTimeLabel(task)};\n'
            'const otherIdle = {start: app.showStartTimer({...task, id: "b"}), stop: app.showStopTimer({...task, id: "b"})};\n'
            'app.runningTimers.b = {start_epoch: 0, saved: false};\n'
            'const otherRunning = {start: app.showStartTimer({...task, id: "b"}), stop: app.showStopTimer({...task, id: "b"})};\n'
            'app.busy = true; const busyStop = app.canStopTimer(task);\n'
            'const completed = app.showStartTimer({...task,status:"done"});\n'
            'app.timerWarnHours = 1 / 3600; const stale = app.timerStale(task);\n'
            'const staleLabel = app.timerSessionLabel(task);\n'
            'app.runningTimers.a.saved = true; const savedStale = app.timerStale(task);\n'
            'console.log(JSON.stringify({idle, running, otherIdle, otherRunning, busyStop, completed, '
            'stale, staleLabel, savedStale, '
            'formatted: app.formatTrackedTime(3723), zero: app.formatTrackedTime(0)}));\n'
            '`);\n'
        )
        result = subprocess.run([node], input=probe, text=True, capture_output=True, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["idle"], {"start": True, "stop": False})
        self.assertEqual(payload["running"]["start"], False)
        self.assertEqual(payload["running"]["stop"], True)
        self.assertEqual(payload["running"]["session"], "Session 00h 00m 12s")
        self.assertEqual(payload["running"]["total"], "Total 01h 01m 52s")
        self.assertEqual(payload["otherIdle"], {"start": True, "stop": False})
        self.assertEqual(payload["otherRunning"], {"start": False, "stop": True})
        self.assertFalse(payload["busyStop"])
        self.assertFalse(payload["completed"])
        self.assertTrue(payload["stale"])
        self.assertIn("Check timer", payload["staleLabel"])
        self.assertFalse(payload["savedStale"])
        self.assertEqual(payload["formatted"], "01h 02m 03s")
        self.assertEqual(payload["zero"], "00h 00m 00s")

    def test_interactive_dashboard_refreshes_on_focus_and_after_stale_timer_action(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        js_source = json.dumps(load_js(self.root / "tools/web"))
        probe = (
            'const fs = require("fs");\n'
            'const callbacks = {};\n'
            'let now = 10000; Date.now = () => now;\n'
            'const task = {id: "a", kind: "task", ref: "@aaa", status: "active", title: "Original"};\n'
            'const payload = (title, running) => ({tasks: [{...task, title}], statuses: ["active"], '
            'meta: {interactive: true, time_running: running}});\n'
            'let responses = [payload("Original", {}), payload("Changed by another agent", {}), payload("Changed by another agent", {}), '
            'payload("Changed by another agent", {a: {start_epoch: 1, saved: false}})];\n'
            'global.setInterval = () => 1;\n'
            'global.location = {hash: "", pathname: "/", search: ""};\n'
            'global.history = {state: null, replaceState(state) {this.state = state;}};\n'
            'global.window = {addEventListener: (name, callback) => {callbacks[name] = callback;}};\n'
            'global.document = {documentElement: {dataset: {}}, hidden: false, addEventListener: (name, callback) => {callbacks[name] = callback;}, '
            'getElementById: id => ({textContent: JSON.stringify(id === "task-data" ? [task] : '
            'id === "mt-statuses" ? ["active"] : id === "mt-drafts" ? [] : {interactive: true})})};\n'
            'global.fetch = async (path, options) => {\n'
            '  if (path.startsWith("/api/items?")) return {ok: true, json: async () => responses.shift()};\n'
            '  return {ok: false, json: async () => ({error: "timer state changed; refresh and retry"})};\n'
            '};\n'
            f'eval({js_source} + `\n'
            '(async () => {\n'
            '  const app = taskBoard(); app.applyPayload({tasks: typeof data === "undefined" ? [] : data, meta: typeof meta === "undefined" ? {} : meta, statuses: typeof statuses === "undefined" ? [] : statuses}); app.ready = true; app.$nextTick = () => {}; await app.init();\n'
            '  now += 5000;\n'
            '  await callbacks.focus();\n'
            '  const titleAfterFocus = app.tasks[0].title;\n'
            '  app.runningTimers = {a: {start_epoch: 1, saved: false}};\n'
            '  await app.apiPost("/api/time-stop", {ref: "@aaa"});\n'
            '  const afterStaleStop = {start: app.showStartTimer(app.tasks[0]), stop: app.showStopTimer(app.tasks[0])};\n'
            '  await app.apiPost("/api/time-start", {ref: "@aaa"});\n'
            '  console.log(JSON.stringify({titleAfterFocus, afterStaleStop, start: app.showStartTimer(app.tasks[0]), '
            'stop: app.showStopTimer(app.tasks[0]), actionError: app.actionError, message: app.actionMessage}));\n'
            '})();\n'
            '`);\n'
        )
        result = subprocess.run([node], input=probe, text=True, capture_output=True, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["titleAfterFocus"], "Changed by another agent")
        self.assertEqual(payload["afterStaleStop"], {"start": True, "stop": False})
        self.assertFalse(payload["start"])
        self.assertTrue(payload["stop"])
        self.assertTrue(payload["actionError"])
        self.assertEqual(payload["message"], "timer state changed; refresh and retry")

    def test_board_filters_round_trip_through_the_page_address(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        js_source = json.dumps(load_js(self.root / "tools/web"))
        probe = (
            'global.location = {hash: "#mt-card-a", pathname: "/", '
            'search: "?layout=horizontal&project=myproj&topic=Work//infra/&topic=home&tag=a&tag=b&q=login&review=1"};\n'
            'global.history = {state: {kept: true}, replaceState(state, _, url) {\n'
            '  this.state = state; const u = new URL(url, "http://x");\n'
            '  location.pathname = u.pathname; location.search = u.search; location.hash = u.hash;\n'
            '}};\n'
            'global.document = {documentElement: {dataset: {}}, '
            'getElementById: () => ({textContent: "{}"})};\n'
            f'eval({js_source} + `\n'
            'const app = taskBoard(); app.readFilterLocation();\n'
            'const read = {filters: JSON.parse(JSON.stringify(app.filters)), review: app.needsReviewOnly, hidden: app.showHidden};\n'
            'const unlisted = app.pickerRows("tag").map(row => row.kind + ":" + row.name);\n'
            'app.refreshDerivedLists({workspace_projects: ["MyProj"]});\n'
            'const canonical = app.filters.project;\n'
            'app.filters.search = ""; app.filters.status = ["doing", "todo"]; app.showHidden = true; app.needsReviewOnly = false;\n'
            'app.writeFilterLocation();\n'
            'const written = location.search + location.hash;\n'
            'const params = new URLSearchParams(); for (const [k, v] of Object.entries(app.collectionQuery())) appendParams(params, k, v);\n'
            'const request = params.toString();\n'
            'app.clearFilters(); app.writeFilterLocation();\n'
            'console.log(JSON.stringify({read, unlisted, canonical, written, request, cleared: location.search, state: history.state}));\n'
            '`);\n'
        )
        result = subprocess.run([node], input=probe, text=True, capture_output=True, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["read"]["filters"]["project"], ["myproj"])
        self.assertEqual(payload["read"]["filters"]["topic"], ["Work/infra", "home"])
        self.assertEqual(payload["read"]["filters"]["tag"], ["a", "b"])
        self.assertEqual(payload["read"]["filters"]["search"], "login")
        self.assertEqual(payload["read"]["filters"]["status"], [])
        self.assertTrue(payload["read"]["review"])
        self.assertFalse(payload["read"]["hidden"])
        # Selected tags that no card uses still get rows, so they can be unticked.
        self.assertEqual(payload["unlisted"], ["clear:", "option:a", "option:b"])
        self.assertEqual(payload["canonical"], ["MyProj"])
        self.assertEqual(payload["written"],
                         "?layout=horizontal&status=doing&status=todo&project=MyProj&topic=Work%2Finfra&topic=home"
                         "&tag=a&tag=b&hidden=1#mt-card-a")
        self.assertIn("status=doing&status=todo&", payload["request"])
        self.assertIn("tag=a&tag=b&", payload["request"])
        self.assertEqual(payload["cleared"], "?layout=horizontal")
        self.assertEqual(payload["state"], {"kept": True})

    def test_web_command_opens_the_board_on_the_given_filters(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("taskweb_url_test", SOURCE_ROOT / "tools/taskweb.py")
        taskweb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(taskweb)
        args = taskweb.parse_args(["--project", "Big project", "--project", "Other", "--topic", "work/infra",
                                   "--updated", "older-1y"])
        self.assertEqual(taskweb.dashboard_url("127.0.0.1", 8123, taskweb.board_filters(args)),
                         "http://127.0.0.1:8123/?project=Big+project&project=Other&topic=work%2Finfra&updated=older-1y")
        self.assertEqual(taskweb.dashboard_url("127.0.0.1", 8123), "http://127.0.0.1:8123/")

    def test_mt_web_passes_each_repeated_filter_to_the_dashboard(self) -> None:
        tasklib = taskweb.tasklib
        args = taskweb.parse_args(["--project", "a", "--project", "b", "--tag", "x", "--updated", "1m"])
        with mock.patch.object(tasklib.subprocess, "run") as run:
            tasklib.command_web(args)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--project"):], ["--project", "a", "--project", "b", "--tag", "x", "--updated", "1m"])
        args.project, args.tag, args.updated = "env-project", None, None  # MT_PROJECT supplies a string
        with mock.patch.object(tasklib.subprocess, "run") as run:
            tasklib.command_web(args)
        self.assertEqual(run.call_args.args[0][-2:], ["--project", "env-project"])

    def test_demo_timer_uses_the_card_id_shown_in_the_dashboard(self) -> None:
        for path in [*self.tasks, self.note]:
            path.unlink()
        shutil.copytree(SOURCE_ROOT / "examples", self.root / "examples")

        process, url = self.start_interactive_web()
        try:
            payload = self.get_json(url + "api/items")
            card = self.get_card(url, next(item for item in payload["tasks"] if item["title"] == "Fix login timeout bug"))
            demo_file = self.root / card["path"]
            declared_id = re.search(r"^id: (.+)$", demo_file.read_text(encoding="utf-8"), re.MULTILINE)
            self.assertIsNotNone(declared_id)
            self.assertEqual(card["id"], declared_id.group(1))

            status, started = self.post_json(
                url + "api/time-start", payload["meta"]["api_token"], {"ref": card["ref"]}
            )
            self.assertEqual(status, 200, started)
            self.assertIn(card["id"], started["meta"]["time_running"])
            self.assertIn("start_epoch", started["meta"]["time_running"][card["id"]])
        finally:
            self.stop_interactive_web(process)

    def test_empty_next_action_has_no_warning_and_can_move_from_web(self) -> None:
        path = self.write_task("inbox", "empty-next", "Read the document", "read", next_action="")
        process, url = self.start_interactive_web()
        try:
            payload = self.get_json(url + "api/items")
            task = self.get_card(url, next(item for item in payload["tasks"] if item["id"] == "empty-next"))
            self.assertFalse(task["needs_restart"])
            token = payload["meta"]["api_token"]
            for destination in ("active", "doing"):
                status, payload = self.post_json(url + "api/move", token,
                                                {"ref": task["ref"], "status": destination})
                self.assertEqual(status, 200, payload)
                task = self.get_card(url, next(item for item in payload["tasks"] if item["id"] == "empty-next"))
                self.assertEqual(task["status"], destination)
                self.assertEqual(task["next_action"], "")
                self.assertFalse(task["needs_restart"])
            self.assertTrue(task["claimed_by"])
            self.assertIn("status: doing", path.read_text())
        finally:
            self.stop_interactive_web(process)

    def test_collection_omits_full_content_but_search_and_detail_keep_it(self) -> None:
        secret = "TailOnlySearchNeedle"
        body = "A large body. " * 6000 + secret + " See @" + hashlib.sha256(self.note.stem.encode()).hexdigest()[:8]
        path = self.write_task("active", "2026-06-01-120000-large", "Large card", "large", body=body)
        process, url = self.start_interactive_web()
        try:
            response = self.get_json(url + "api/items?search=" + secret)
            self.assertEqual(response["collections"]["cards"], [path.stem])
            for card in response["tasks"]:
                self.assertFalse({"body", "content", "body", "references", "reference_spans"} & card.keys())
            summary = next(card for card in response["tasks"] if card["id"] == path.stem)
            detail = self.get_card(url, summary)
            self.assertIn(body, detail["body"])
            self.assertEqual(detail["body"], taskweb.tasklib.card_format.parse_document(path.read_text())[1])
            self.assertEqual(detail["content_hash"], summary["content_hash"])
            self.assertEqual(detail["references"][0]["target"]["id"], self.note.stem)
            self.assertTrue(detail["reference_spans"]["body"])
            self.assertLess(len(json.dumps(response)), len(json.dumps(detail)) / 4)
            status, moved = self.post_json(url + "api/move", response["meta"]["api_token"], {
                "ref": summary["ref"], "status": "waiting", "query": {"search": secret},
            })
            self.assertEqual(status, 200, moved)
            self.assertEqual(moved["collections"]["cards"], [path.stem])
            self.assertTrue(all("body" not in card and "body" not in card for card in moved["tasks"]))
            for endpoint, expected in [("api/item", 400), ("api/item?id=missing", 404),
                                       ("api/item?id=../../README.md", 404), ("api/item?id=a&id=b", 400)]:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.get_json(url + endpoint)
                self.assertEqual(raised.exception.code, expected)
                raised.exception.close()
        finally:
            self.stop_interactive_web(process)

    def test_mutation_builds_one_dashboard_and_failures_build_none(self) -> None:
        source = """
import sys
sys.path.insert(0, 'tools')
import taskweb
original = taskweb.dashboard_payload
builds = 0
def counted(**kwargs):
    global builds
    builds += 1
    payload = original(**kwargs)
    payload['response_builds'] = builds
    return payload
taskweb.dashboard_payload = counted
taskweb.serve_dashboard('127.0.0.1', 0, no_open=True)
"""
        process = subprocess.Popen([sys.executable, '-c', source], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            url = process.stdout.readline().strip()
            self.assertTrue(url.startswith('http://127.0.0.1:'), url)
            initial = self.get_json(url + 'api/items')
            self.assertEqual(initial['response_builds'], 1)
            task = next(item for item in initial['tasks'] if item['id'] == self.tasks[0].stem)
            body = {'ref':task['ref'], 'expected_hash':task['content_hash'],
                    'patch':{'project':'Optimized'}, 'query':{'search':'bold'}}
            status, saved = self.post_json(url + 'api/card-metadata', initial['meta']['api_token'], body)
            self.assertEqual(status, 200, saved)
            self.assertEqual(saved['response_builds'], 2)
            self.assertIn(task['id'], saved['collections']['cards'])
            status, rejected = self.post_json(url + 'api/card-metadata', initial['meta']['api_token'], body)
            self.assertEqual(status, 409, rejected)
            self.assertEqual(self.get_json(url + 'api/items')['response_builds'], 3)
        finally:
            self.stop_interactive_web(process)

    def test_checkbox_endpoint_toggles_one_item_and_guards_stale_state(self) -> None:
        subprocess.run([str(self.root / "mt"), "edit", self.tasks[0].stem, "--body", "--file", "-",
                        "--expected-hash", hashlib.sha256(self.tasks[0].read_text().encode()).hexdigest(),
                        "--json"],
                       input="Plan:\n\n- [ ] first\n- [x] second\n", text=True, cwd=self.root,
                       capture_output=True, check=True)
        process, url = self.start_interactive_web()
        try:
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            task = next(item for item in initial["tasks"] if item["id"] == self.tasks[0].stem)
            request = {"ref": task["ref"], "expected_hash": task["content_hash"], "index": 0, "count": 2, "checked": True}
            status, rejected = self.post_json(url + "api/checkbox", token, {**request, "count": 3})
            self.assertEqual(status, 409, rejected)
            self.assertIn("checklist changed", rejected["error"])
            status, saved = self.post_json(url + "api/checkbox", token, request)
            self.assertEqual(status, 200, saved)
            self.assertEqual(saved["message"], "Checklist updated")
            self.assertIn("- [x] first\n- [x] second\n", self.tasks[0].read_text())
            status, stale = self.post_json(url + "api/checkbox", token, {**request, "index": 1, "checked": False})
            self.assertEqual(status, 409, stale)
            self.assertIn("- [x] second", self.tasks[0].read_text())
            status, invalid = self.post_json(url + "api/checkbox", token, {**request, "checked": "yes"})
            self.assertEqual(status, 400, invalid)
        finally:
            self.stop_interactive_web(process)

    def test_server_queries_and_mutations_send_matching_cards_and_global_choices(self) -> None:
        self.tasks[0].write_text(self.tasks[0].read_text().replace("project: null", "project: Alpha"))
        self.tasks[1].write_text(self.tasks[1].read_text().replace("project: null", "project: Beta"))
        self.tasks[0].write_text(self.tasks[0].read_text().replace("project: Alpha", "project: Alpha\ntopic: learning/rust/async"))
        process, url = self.start_interactive_web()
        try:
            payload = self.get_json(url + "api/items?search=Plan&project=Alpha&show_hidden=false")
            tree = {node["path"]: (node["depth"], node["count"]) for node in payload["meta"]["topic_tree"]}
            self.assertEqual((tree["learning"], tree["learning/rust"], tree["learning/rust/async"]),
                             ((0, 1), (1, 1), (2, 1)))
            self.assertIn("work", tree)  # suggested_topics seeds empty top-level topics
            nested = self.get_json(url + "api/items?topic=learning")
            self.assertEqual(nested["collections"]["cards"], [self.tasks[0].stem])
            self.assertEqual(payload["collections"]["cards"], [self.tasks[0].stem])
            # Only matching cards are sent; filter choices and counts still cover the workspace.
            self.assertEqual([item["id"] for item in payload["tasks"]], [self.tasks[0].stem])
            self.assertEqual(payload["meta"]["workspace_projects"], ["Alpha", "Beta"])
            self.assertEqual(payload["meta"]["doing_count"], 1)
            self.assertEqual([entry["id"] for entry in payload["meta"]["focus"]["candidates"]], payload["collections"]["open"])
            everything = self.get_json(url + "api/items")
            self.assertEqual(len(everything["tasks"]), 5)
            self.assertEqual(everything["meta"]["reference_revision"], payload["meta"]["reference_revision"])
            # An open card and its trail stay available outside the filters; unknown IDs are ignored.
            extra = self.get_json(url + "api/items?" + urlencode(
                {"project": "Alpha", "include": [self.tasks[1].stem, "missing"]}, doseq=True))
            self.assertEqual({item["id"] for item in extra["tasks"]}, {self.tasks[0].stem, self.tasks[1].stem})
            self.assertEqual(extra["collections"], self.get_json(url + "api/items?project=Alpha")["collections"])
            self.assertEqual(extra["include"], [self.tasks[1].stem, "missing"])
            token = payload["meta"]["api_token"]
            task = self.get_card(url, next(item for item in payload["tasks"] if item["id"] == self.tasks[0].stem))
            code, result = self.post_json(url + "api/move", token, {
                "ref": task["ref"], "status": "waiting",
                "query": {"status": "active", "project": "Alpha"}, "include": [task["id"]],
            })
            self.assertEqual(code, 200, result)
            self.assertEqual(result["collections"]["cards"], [])
            self.assertEqual([item["id"] for item in result["tasks"]], [task["id"]])
            self.assertEqual(result["tasks"][0]["status"], "waiting")
            self.assertEqual(result["meta"]["reference_revision"], payload["meta"]["reference_revision"])
            refreshed = self.get_json(url + "api/items?status=waiting&project=Alpha")
            self.assertEqual(refreshed["collections"]["cards"], [self.tasks[0].stem])
        finally:
            self.stop_interactive_web(process)

    def test_interactive_server_serves_metadata_and_mutates_through_mt(self) -> None:
        process, url = self.start_interactive_web()
        try:
            payload = self.get_json(url + "api/items")
            token = payload["meta"]["api_token"]
            self.assertNotIn("interactive", payload["meta"])
            self.assertTrue(token)

            active = self.get_card(url, next(item for item in payload["tasks"] if item["title"] == "Plan the work"))
            doing = self.get_card(url, next(item for item in payload["tasks"] if item["title"] == "Build the thing"))
            note = self.get_card(url, next(item for item in payload["tasks"] if item["title"] == "Reference note"))
            inbox = self.get_card(url, next(item for item in payload["tasks"] if item["title"] == "Vague idea"))

            status, started_payload = self.post_json(
                url + "api/time-start", token, {"ref": active["ref"]}
            )
            self.assertEqual(status, 200, started_payload)
            self.assertIn(active["id"], started_payload["meta"]["time_running"])
            time.sleep(1.05)
            status, second_started = self.post_json(
                url + "api/time-start", token, {"ref": inbox["ref"]}
            )
            self.assertEqual(status, 200, second_started)
            self.assertEqual(
                set(second_started["meta"]["time_running"]), {inbox["id"]}
            )
            status, wrong_task_stop = self.post_json(
                url + "api/time-stop", token, {"ref": doing["ref"]}
            )
            self.assertEqual(status, 400)
            self.assertIn("no timer is running for", wrong_task_stop["error"])
            status, stopped_payload = self.post_json(
                url + "api/time-stop", token, {"ref": inbox["ref"]}
            )
            self.assertEqual(status, 200, stopped_payload)
            self.assertEqual(stopped_payload["meta"]["time_running"], {})
            timed_active = self.get_card(url, next(item for item in stopped_payload["tasks"] if item["id"] == active["id"]))
            self.assertGreaterEqual(timed_active["time_seconds"], 1)
            self.assertEqual(len(self.get_card(url, timed_active)["time_entries"]), 1)
            status, last_stopped_payload = self.post_json(
                url + "api/time-stop", token, {"ref": inbox["ref"]}
            )
            self.assertEqual(status, 400, last_stopped_payload)

            status, _ = self.post_json(url + "api/time-start", token, {"ref": doing["ref"]})
            self.assertEqual(status, 200)
            time.sleep(1.05)
            status, both_ends = self.post_json(
                url + "api/time-stop", token, {"ref": doing["ref"], "at": "10:00", "duration": "1s"}
            )
            self.assertEqual(status, 400, both_ends)
            status, trimmed = self.post_json(
                url + "api/time-stop", token, {"ref": doing["ref"], "duration": "1s"}
            )
            self.assertEqual(status, 200, trimmed)
            self.assertEqual(trimmed["meta"]["time_running"], {})
            trimmed_doing = self.get_card(url, doing)
            self.assertEqual(trimmed_doing["time_seconds"], 1)

            status, _ = self.post_json(url + "api/time-start", token, {"ref": doing["ref"]})
            self.assertEqual(status, 200)
            status, cancelled = self.post_json(url + "api/time-cancel", token, {"ref": doing["ref"]})
            self.assertEqual(status, 200, cancelled)
            self.assertEqual(cancelled["meta"]["time_running"], {})
            self.assertEqual(len(self.get_card(url, doing)["time_entries"]), 1)
            status, _ = self.post_json(url + "api/time-cancel", token, {"ref": doing["ref"]})
            self.assertEqual(status, 400)

            status, error_payload = self.post_json(url + "api/move", "bad-token", {"ref": active["ref"], "status": "waiting"})
            self.assertEqual(status, 403)
            self.assertEqual(error_payload["error"], "invalid token")

            status, moved_payload = self.post_json(url + "api/move", token, {"ref": active["ref"], "status": "waiting"})
            self.assertEqual(status, 200, moved_payload)
            moved = self.get_card(url, next(item for item in moved_payload["tasks"] if item["id"] == active["id"]))
            self.assertEqual(moved["status"], "waiting")
            moved_file = self.root / "cards" / f"{active['id']}.md"
            self.assertTrue(moved_file.is_file())
            self.assertIn("status: waiting", moved_file.read_text(encoding="utf-8"))

            status, commented_payload = self.post_json(
                url + "api/comment",
                token,
                {"ref": note["ref"], "message": "Reviewed from the web UI\nNeeds follow-up"},
            )
            self.assertEqual(status, 200, commented_payload)
            commented_note = self.get_card(url, next(item for item in commented_payload["tasks"] if item["id"] == note["id"]))
            self.assertEqual(commented_note["comments"][0]["text"], "Reviewed from the web UI\nNeeds follow-up")
            note_text = (self.root / "cards" / f"{note['id']}.md").read_text(encoding="utf-8")
            fields, body = taskweb.tasklib.card_format.parse_document(note_text)
            self.assertEqual(fields["comments"][0]["text"], "Reviewed from the web UI\nNeeds follow-up")
            self.assertNotIn("Reviewed from the web UI", body)

            for item in (active, note):
                for value in (None, "true", 1):
                    status, error = self.post_json(url + "api/hidden", token, {"ref": item["ref"], "hidden": value})
                    self.assertEqual(status, 400)
                    self.assertIn("hidden must be true or false", error["error"])
                for value in (True, False):
                    # The dashboard keeps the open card in the response even once it is hidden.
                    status, payload = self.post_json(url + "api/hidden", token, {
                        "ref": item["ref"], "hidden": value, "include": [item["id"]]})
                    self.assertEqual(status, 200, payload)
                    changed = self.get_card(url, next(card for card in payload["tasks"] if card["id"] == item["id"]))
                    self.assertIs(changed["hidden"], value)

            status, archived_payload = self.post_json(
                url + "api/archive",
                token,
                {"ref": note["ref"], "archived": True},
            )
            self.assertEqual(status, 200, archived_payload)
            archived_note = self.get_card(url, next(item for item in archived_payload["tasks"] if item["id"] == note["id"]))
            self.assertTrue(archived_note["archived"])

            status, done_payload = self.post_json(url + "api/done", token, {"ref": inbox["ref"]})
            self.assertEqual(status, 200, done_payload)
            completed = self.get_card(url, next(item for item in done_payload["tasks"] if item["id"] == inbox["id"]))
            self.assertEqual(completed["status"], "done")
            completed_file = self.root / "cards" / f"{inbox['id']}.md"
            self.assertTrue(completed_file.is_file())
            self.assertIn("status: done", completed_file.read_text(encoding="utf-8"))

            status, invalid_payload = self.post_json(url + "api/move", token, {"ref": note["ref"], "status": "active"})
            self.assertEqual(status, 400)
            self.assertEqual(invalid_payload["error"], "only open tasks can move")

            note_ref = next(item for item in done_payload["tasks"] if item["title"] == "Reference note")["ref"]
            status, priority_payload = self.post_json(
                url + "api/priority", token, {"ref": note_ref, "priority": "high"}
            )
            self.assertEqual(status, 200, priority_payload)
            prioritized_note = self.get_card(url, next(item for item in priority_payload["tasks"] if item["title"] == "Reference note"))
            self.assertEqual(prioritized_note["priority"], "high")

            status, invalid_priority = self.post_json(
                url + "api/priority", token, {"ref": note_ref, "priority": "nonsense"}
            )
            self.assertEqual(status, 400)
            self.assertEqual(invalid_priority["error"], "invalid priority")

            status, unchanged_priority = self.post_json(
                url + "api/priority", token, {"ref": note_ref, "priority": "high"}
            )
            self.assertEqual(status, 400)
            self.assertEqual(unchanged_priority["error"], "priority unchanged")

            status, due_set_payload = self.post_json(
                url + "api/due-set", token, {"ref": active["ref"], "due": "2026-10-31", "due_kind": "target"}
            )
            self.assertEqual(status, 200, due_set_payload)
            due_set = self.get_card(url, next(item for item in due_set_payload["tasks"] if item["id"] == active["id"]))
            self.assertEqual(due_set["due"], "2026-10-31")
            self.assertEqual(due_set["due_kind"], "target")

            status, due_time_payload = self.post_json(
                url + "api/due-set",
                token,
                {"ref": active["ref"], "due": "2026-10-31", "due_time": "14:30", "due_kind": "deadline"},
            )
            self.assertEqual(status, 200, due_time_payload)
            due_time_set = self.get_card(url, next(item for item in due_time_payload["tasks"] if item["id"] == active["id"]))
            self.assertEqual(due_time_set["due"], "2026-10-31T14:30:00")
            self.assertEqual(due_time_set["due_kind"], "deadline")

            status, invalid_time = self.post_json(
                url + "api/due-set",
                token,
                {"ref": active["ref"], "due": "2026-10-31", "due_time": "25:99", "due_kind": "deadline"},
            )
            self.assertEqual(status, 400)
            self.assertEqual(invalid_time["error"], "due_time must be an HH:MM time")

            status, invalid_due = self.post_json(
                url + "api/due-set", token, {"ref": active["ref"], "due": "not-a-date", "due_kind": "deadline"}
            )
            self.assertEqual(status, 400)
            self.assertEqual(invalid_due["error"], "due must be a YYYY-MM-DD date")

            status, invalid_kind = self.post_json(
                url + "api/due-set", token, {"ref": active["ref"], "due": "2026-10-31", "due_kind": "nonsense"}
            )
            self.assertEqual(status, 400)
            self.assertEqual(invalid_kind["error"], "invalid due kind")

            due_task = self.get_card(url, next(item for item in done_payload["tasks"] if item["title"] == "Build the thing"))
            status, cleared_payload = self.post_json(url + "api/due-clear", token, {"ref": due_task["ref"]})
            self.assertEqual(status, 200, cleared_payload)
            cleared = self.get_card(url, next(item for item in cleared_payload["tasks"] if item["id"] == due_task["id"]))
            self.assertIsNone(cleared["due"])
            self.assertIsNone(cleared["due_kind"])
            task_text = (self.root / "cards" / f"{due_task['id']}.md").read_text(encoding="utf-8")
            properties = taskweb.tasklib.card_format.parse_document(task_text)[0]
            self.assertIsNone(properties.get("due"))
            self.assertIsNone(properties.get("due_kind"))

            status, already_cleared = self.post_json(url + "api/due-clear", token, {"ref": due_task["ref"]})
            self.assertEqual(status, 400)
            self.assertEqual(already_cleared["error"], "item has no due date")
        finally:
            self.stop_interactive_web(process)

    def test_done_column_capped_and_archive_view_present(self) -> None:
        for i in range(1, 13):
            self.write_task(
                "done",
                f"2026-06-{i:02d}-090000-done-{i:02d}",
                f"Completed item {i:02d}",
                f"done {i:02d}",
                updated=f"2026-06-{i:02d}",
                completed=f"2026-06-{i:02d}T12:00:00",
            )
        self.write_task("active", "2026-06-20-090000-archived-active", "Archived active task", "active", archived=True)
        self.write_note("2026-06-21-090000-archived-note", "Archived reference note", archived=True)

        result = self.render_dashboard()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = self.output.read_text(encoding="utf-8")

        data = json.loads(self.payload_json("tasks"))
        done = [item for item in data if item["kind"] == "task" and item["status"] == "done"]
        archived = [item for item in data if item["archived"]]
        # All done tasks stay embedded so the archive view and search can find them.
        self.assertGreater(len(done), 10)
        self.assertEqual({item["title"] for item in archived}, {"Archived active task", "Archived reference note"})
        titles = {item["title"] for item in done}
        for i in range(1, 13):
            self.assertIn(f"Completed item {i:02d}", titles)

        # The Archive collection holds all done and archived cards.
        self.assertIn('this.mode === "archive" ? "archive" : "cards"', rendered)
        archive = json.loads(self.payload_json("collections"))["archive"]
        self.assertEqual(len([i for i in archive if i in {item["id"] for item in done}]), len(done))
        self.assertEqual(len(archive), len(done) + len(archived))

    def test_embedded_policy_metadata_includes_manual_focus_id(self) -> None:
        (self.root / ".mt").mkdir()
        (self.root / ".mt" / "focus").write_text(
            json.dumps({"task_id": self.tasks[0].stem, "mode": "cycle"}) + "\n",
            encoding="utf-8",
        )

        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        meta_blob = self.payload_json("meta")
        meta = json.loads(meta_blob)

        self.assertEqual(meta["focus"]["selected"]["id"], self.tasks[0].stem)
        self.assertEqual(meta["focus"]["mode"], "cycle")
        self.assertIn("this.focusContext = meta.focus", rendered)

    def test_dashboard_consumes_cli_focus_order_reasons_alerts_and_filters(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.write_task("active", "2026-01-06-120000-soon", "Tomorrow deadline", "soon", due=tomorrow)
        self.write_task("waiting", "2026-01-07-120000-blocked", "Blocked deadline", "blocked", due=date.today().isoformat())
        pin = subprocess.run([str(self.root / "mt"), "focus", self.tasks[0].stem],
                             cwd=self.root, text=True, capture_output=True)
        self.assertEqual(pin.returncode, 0, pin.stderr)
        cli = subprocess.run([str(self.root / "mt"), "focus", "--json"],
                             cwd=self.root, text=True, capture_output=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        expected = json.loads(cli.stdout)
        self.assertEqual(self.render_dashboard().returncode, 0)
        rendered = self.output.read_text()
        data = json.loads(self.payload_json("tasks"))
        meta = json.loads(self.payload_json("meta"))
        statuses = json.loads(self.payload_json("statuses"))
        self.assertEqual(meta["focus"]["selected"], expected["selected"])
        self.assertEqual(meta["focus"]["candidates"], expected["candidates"])
        self.assertEqual(meta["focus"]["alerts"], expected["alerts"])
        collections = {
            "normal": collection_results(data),
            "active": collection_results(data, {"status": "active"}),
            "search": collection_results(data, {"status": "active", "search": "Tomorrow deadline"}),
            "waiting": collection_results(data, {"status": "waiting"}),
        }
        probe = (
            f'const collections = {json.dumps(collections)};\n'
            f'const data = {json.dumps(data)}; const meta = {json.dumps(meta)}; '
            f'const statuses = {json.dumps(statuses)};\n'
            'global.document = {getElementById: id => ({textContent: JSON.stringify('
            'id === "task-data" ? data : id === "mt-statuses" ? statuses : meta)})};\n'
            f'eval({json.dumps(load_js(self.root / "tools/web"))} + `\n'
            'const app = taskBoard(); app.collections = collections.normal; app.applyPayload({tasks: typeof data === "undefined" ? [] : data, meta: typeof meta === "undefined" ? {} : meta, statuses: typeof statuses === "undefined" ? [] : statuses}); app.ready = true;\n'
            'const normal = {id: app.focusTask().id, reason: app.focusReason(app.focusTask()), '
            'alerts: app.focusAlerts().map(t => t.id), followups: app.followupTasks().map(t => t.id)};\n'
            'app.filters.status = ["active"]; app.collections = collections.active;\n'
            'const active = app.focusTask().id;\n'
            'app.filters.search = "Tomorrow deadline"; app.collections = collections.search;\n'
            'const searched = {id: app.focusTask().id, reason: app.focusReason(app.focusTask())};\n'
            'app.filters.status = ["waiting"]; app.filters.search = ""; app.collections = collections.waiting;\n'
            'console.log(JSON.stringify({normal, active, searched, blocked: app.focusTask()}));\n'
            '`);'
        )
        result = subprocess.run([node], input=probe, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual["normal"]["id"], expected["selected"]["id"])
        self.assertEqual(actual["normal"]["reason"], expected["selected"]["reason"])
        self.assertEqual(actual["normal"]["alerts"], [item["id"] for item in expected["alerts"]])
        self.assertEqual(actual["normal"]["followups"], expected["followup_ids"])
        self.assertEqual(actual["active"], self.tasks[0].stem)
        self.assertEqual(actual["searched"]["id"], "2026-01-06-120000-soon")
        self.assertEqual(actual["searched"]["reason"], "deadline in the next 7 days")
        self.assertIsNone(actual["blocked"])

    def test_embedded_policy_metadata_accepts_legacy_raw_focus_id(self) -> None:
        (self.root / ".mt").mkdir()
        (self.root / ".mt" / "focus").write_text(f"{self.tasks[0].stem}\n", encoding="utf-8")

        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        meta_blob = self.payload_json("meta")
        meta = json.loads(meta_blob)

        self.assertEqual(meta["focus"]["selected"]["id"], self.tasks[0].stem)
        self.assertEqual(meta["focus"]["mode"], "manual")

    def test_focus_helpers_use_shared_context(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        for helper in (
            "openTasks()",
            "dueState(task)",
            "hasDeadlineDue(task)",
            "focusTask()",
            "focusReason(task)",
            "followupTasks()",
            "needsRestartStep(task)",
            "groupCards(items, field",
        ):
            self.assertIn(helper, rendered)
        self.assertIn("this.focusContext.candidates", rendered)
        self.assertNotIn("isHighOverdue(task)", rendered)
        self.assertNotIn("needsDeadlineReview(task)", rendered)
        self.assertNotIn('"AI recommended"', rendered)

    def test_followups_strip_uses_waiting_tasks_without_command_hints(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("mt-followups", rendered)
        self.assertIn("No waiting follow-ups.", rendered)
        self.assertIn("this.focusContext.followup_ids", rendered)
        self.assertNotIn("mt-command-line", rendered)
        self.assertNotIn("restartCommand", rendered)
        self.assertNotIn("restartTargetLabel", rendered)
        self.assertNotIn("commandPrefix", rendered)
        self.assertNotIn("restart_commands", rendered)

    def test_due_cues_restart_badges_and_wip_warning_are_present(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn(".mt-card.is-overdue", rendered)
        self.assertIn(".mt-card.is-due-today", rendered)
        self.assertIn(".mt-due.is-overdue", rendered)
        self.assertIn(".mt-due.is-due-today", rendered)
        self.assertIn("dueClass(task)", rendered)
        self.assertIn("needs restart step", rendered)
        self.assertIn("task.needs_restart", rendered)
        self.assertIn("statusLabel(status)", rendered)
        self.assertIn('return label + " (" + this.doingCount() + "/" + this.maxDoingTasks + " max)";', rendered)
        self.assertIn("columnClass(status)", rendered)
        self.assertIn("is-over-limit", rendered)
        self.assertIn("width: fit-content", rendered)
        self.assertNotIn("mt-wip", rendered)
        self.assertNotIn(".mt-overview.is-compact", rendered)

    def test_priority_badges_have_distinct_classes(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("priorityClass(task.priority)", rendered)
        self.assertIn(".mt-priority.priority-low", rendered)
        self.assertIn(".mt-priority.priority-medium", rendered)
        self.assertIn(".mt-priority.priority-high", rendered)
        self.assertIn(".mt-priority.priority-urgent", rendered)

    def test_notes_without_priority_do_not_default_to_medium(self) -> None:
        self.write_note("2026-01-07-120000-unassigned", "Unassigned note", priority="none")
        self.render_dashboard()
        data = json.loads(
            self.payload_json("tasks")
        )
        unassigned = next(item for item in data if item["title"] == "Unassigned note")
        self.assertEqual(unassigned["priority"], "none")

    def test_notes_with_invalid_priority_are_rejected(self):
        fields, body = taskweb.tasklib.card_format.parse_document(self.note.read_text())
        fields["priority"] = None
        self.note.write_text("---\n" + taskweb.tasklib.card_format.dump_properties(fields) + "---\n" + body)
        result = self.render_dashboard()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("priority", result.stderr)

    def test_embedded_json_escapes_script_and_raw_html(self) -> None:
        path = self.write_task(
            "waiting",
            "2026-01-06-120000-evil",
            "Evil task",
            "xss",
            body="</script><script>alert('xss')</script>",
        )
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertNotIn("</script><script>alert('xss')</script>", rendered)
        self.assertIn("\\u003c/script>\\u003cscript>alert('xss')\\u003c/script>", self.detail_json())
        self.assertIn("\\u003csvg onload=alert(1)", self.detail_json())

    def test_note_bodies_with_brace_tokens_render_verbatim(self) -> None:
        self.write_note(
            "2026-01-07-120000-braces",
            "Gradient notes",
            content="Loss flows from {start} through {stopgrad} to {pred} for each {s}.",
        )
        result = self.render_dashboard()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = self.output.read_text(encoding="utf-8")
        blob = self.detail_json()
        data = json.loads(blob)
        note = next(item for item in data if item["title"] == "Gradient notes")
        self.assertIn("{start}", note["body"])
        self.assertIn("{stopgrad}", note["body"])
        self.assertIn("{pred}", note["body"])
        self.assertIn("{s}", note["body"])
        self.assertIn("{start}", note["excerpt"])

    def test_placeholder_tokens_in_bodies_are_not_substituted(self) -> None:
        self.write_task(
            "waiting",
            "2026-01-08-120000-placeholders",
            "Placeholder body task",
            "placeholders",
            body="Template docs: {app_script} {nonce} {task_json} {statuses_json} {meta_json}",
        )
        result = self.render_dashboard()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = self.output.read_text(encoding="utf-8")
        blob = self.detail_json()
        data = json.loads(blob)
        task = next(item for item in data if item["title"] == "Placeholder body task")
        self.assertIn("{app_script}", task["body"])
        self.assertNotIn("taskBoard()", blob)
        for nonce in re.findall(r'nonce="([A-Za-z0-9_-]+)"', rendered):
            self.assertNotIn(nonce, blob)

    def test_unreplaced_template_placeholder_still_fails(self) -> None:
        index_html = self.root / "tools" / "web" / "index.html"
        index_html.write_text(
            index_html.read_text(encoding="utf-8").replace("<title>", "<title>{bogus_marker}"),
            encoding="utf-8",
        )
        result = self.render_dashboard()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unreplaced placeholders", result.stderr)
        self.assertIn("{bogus_marker}", result.stderr)

    def test_js_sanitizes_marked_output_before_insertion(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn("DOMPurify.sanitize(restored", rendered)
        self.assertIn('const raw = marked.parse(protectedMath.protectedSource, { gfm: true, breaks: false, renderer });', rendered)
        self.assertIn("x-html=\"previewHtml\"", rendered)
        self.assertIn("el.innerHTML = renderMarkdown(task, source)", rendered)
        self.assertIn('const source = referenceSource(task, "body")', rendered)

    def test_references_share_cli_and_web_resolution_without_changing_compact_list(self) -> None:
        def cli(*args):
            result = subprocess.run([str(self.root / "mt"), *args], cwd=self.root,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        compact = cli("list", "--status", "inbox")
        source = self.tasks[2]
        targets = [self.tasks[0], self.tasks[1], self.tasks[3], self.note]
        refs = ["@" + hashlib.sha256(path.stem.encode()).hexdigest()[:8] for path in targets]
        # Hidden/archived targets still resolve, including across status filters.
        targets[0].write_text(targets[0].read_text().replace("archived: false", "archived: true\nhidden: true"))
        source.write_text(source.read_text().replace("Some **bold** notes for inbox.",
            "😀 See " + "; ".join(refs) + ". Repeat " + refs[0][:5].upper() +
            ". Missing @fff. Code `@ddd`. Escaped \\@eee."))
        self.assertEqual(cli("list", "--status", "inbox"), compact)
        for args in (("list", "--status", "inbox", "--json"), ("search", "Vague idea", "--json")):
            record = json.loads(cli(*args))[0]
            self.assertEqual([ref["state"] for ref in record["references"]],
                             ["resolved"] * 5 + ["missing"])
            self.assertEqual(record["references"][3]["target"]["kind"], "note")
        self.assertEqual(self.render_dashboard().returncode, 0)
        rendered = self.output.read_text()
        data = json.loads(self.detail_json())
        card = next(item for item in data if item["id"] == source.stem)
        self.assertEqual(card["references"], record["references"])
        for span in card["reference_spans"]["body"]:
            self.assertEqual(card["body"][span["start"]:span["end"]], span["ref"])

    def test_math_delimiters_and_katex_auto_render(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertIn('left: "$$", right: "$$", display: true', rendered)
        self.assertIn('left: "\\\\[", right: "\\\\]", display: true', rendered)
        self.assertIn('left: "\\\\(", right: "\\\\)", display: false', rendered)
        self.assertIn('left: "$", right: "$", display: false', rendered)
        self.assertIn("renderMathInElement", rendered)

    def test_base_uri_and_renderer_hooks_present(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")
        data = json.loads(self.payload_json("tasks"))
        for item in data:
            self.assertTrue(item["base_uri"].startswith("file://"))
            self.assertTrue(item["base_uri"].endswith("/"))
        self.assertIn("const renderer = new marked.Renderer()", rendered)
        self.assertIn("renderer.link = function", rendered)
        self.assertIn("renderer.image = function", rendered)
        self.assertIn('resolveUrl(task.base_uri || "", token.href)', rendered)
        self.assertIn('const raw = marked.parse(protectedMath.protectedSource, { gfm: true, breaks: false, renderer });', rendered)

    def test_overdue_filter_excludes_today(self) -> None:
        self.render_dashboard()
        rendered = self.output.read_text(encoding="utf-8")

    def test_empty_workspace_renders_valid_page(self) -> None:
        for task_path in list(card_files(self.root, "task")):
            task_path.unlink()
        for note_path in list(card_files(self.root, "note")):
            note_path.unlink()
        result = self.render_dashboard()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = self.output.read_text(encoding="utf-8")
        statuses_blob = self.payload_json("statuses")
        self.assertEqual(json.loads(statuses_blob), ["inbox", "active", "doing", "waiting", "someday", "done"])
        data_blob = self.payload_json("tasks")
        self.assertEqual(json.loads(data_blob), [])
        self.assertIn("mt-column", rendered)
        self.assertIn("empty", rendered)

    def test_server_shell_stays_available_when_card_loading_fails(self) -> None:
        (self.root / "cards" / "broken.md").write_text("# Broken card\n")
        process, url = self.start_interactive_web()
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                page = response.read().decode()
            self.assertIn('id="mt-config"', page)
            self.assertIn("Retry loading", page)
            self.assertNotIn("Plan the work", page)
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.get_json(url + "api/items")
            self.assertEqual(failure.exception.code, 409)
            self.assertIn("missing YAML frontmatter", json.loads(failure.exception.read())["error"])
            failure.exception.close()
        finally:
            self.stop_interactive_web(process)

    def test_web_defaults_to_server_and_opens_browser(self) -> None:
        marker = self.root / "browser-opened.txt"
        env = dict(os.environ)
        env["BROWSER"] = f"/bin/sh -c 'echo opened >> {marker}; true' %s"
        process, url = self.start_interactive_web(cli=True, env=env)
        try:
            self.assertIsNone(process.poll())
            payload = json.loads(urllib.request.urlopen(url + "api/items").read())
            self.assertNotIn("interactive", payload["meta"])
            self.assertFalse((self.root / ".mt/site/index.html").exists())
            deadline = time.time() + 5
            while time.time() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.is_file(), "browser open command did not run")
        finally:
            self.stop_interactive_web(process)

    def test_web_rejects_removed_export_options(self) -> None:
        for arguments in (("--static",), ("--interactive",), ("--out", str(self.output))):
            for entry in ([str(self.root / "mt"), "web"], [sys.executable, str(self.root / "tools/taskweb.py")]):
                result = subprocess.run([*entry, *arguments, "--no-open"],
                                        cwd=self.root, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unrecognized arguments", result.stderr)
                self.assertFalse(self.output.exists())

    def test_web_is_read_only_and_does_not_mutate_tasks(self) -> None:
        before = {p: p.read_bytes() for p in card_files(self.root, "task")}
        result = self.render_dashboard()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {p: p.read_bytes() for p in card_files(self.root, "task")}
        self.assertEqual(before, after)
        self.assertEqual(sorted(card_files(self.root, "task"), key=str), sorted(self.tasks, key=str))

    def test_interactive_live_reloads_modified_assets(self) -> None:
        process, url = self.start_interactive_web()
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                initial_html = response.read().decode("utf-8")
            self.assertNotIn("/* test-live-tuning-marker */", initial_html)

            theme_css = self.root / "tools" / "web" / "styles" / "theme.css"
            theme_css.write_text(theme_css.read_text(encoding="utf-8") + "\n/* test-live-tuning-marker */\n", encoding="utf-8")

            with urllib.request.urlopen(url, timeout=5) as response:
                updated_html = response.read().decode("utf-8")
            self.assertIn("/* test-live-tuning-marker */", updated_html)
        finally:
            self.stop_interactive_web(process)

    def test_workspace_drafts_are_saved_revision_guarded_promoted_without_embedding_drafts_in_shell(self) -> None:
        process, url = self.start_interactive_web()
        unique_text = "DRAFT_ONLY_STATIC_SENTINEL_6bc9b31f"
        draft_id = "9b12fef2-7cd2-4c0d-a71a-2642fb4258b3"
        capture_id = "fd993abe-d8e1-4dd2-8fe0-45df2441ff27"
        try:
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            created = {
                "id": draft_id, "expected_revision": 0, "kind": "note", "mode": "new",
                "capture_id": capture_id, "created": "2026-10-15T09:30:00",
                "title": "A workspace draft", "body": unique_text + "\n\n## Incomplete",
                "instructions": "Review the captured source before triage.",
            }
            status, saved = self.post_json(url + "api/draft-save", token, created)
            self.assertEqual(status, 200, saved)
            self.assertEqual(saved["meta"]["draft_count"], 1)
            self.assertNotIn("drafts", saved)
            self.assertFalse(any(item["kind"] == "note" and item["title"] == "A workspace draft" for item in saved["tasks"]))

            stale = dict(created, body="stale overwrite")
            status, conflict = self.post_json(url + "api/draft-save", token, stale)
            self.assertEqual(status, 409, conflict)
            self.assertTrue(conflict["conflict"])
            workspace_drafts = self.get_json(url + "api/drafts")["drafts"]
            self.assertEqual(workspace_drafts[0]["body"], created["body"])
            self.assertEqual(workspace_drafts[0]["instructions"], created["instructions"])

            static = self.render_dashboard()
            self.assertEqual(static.returncode, 0, static.stderr)
            static_html = self.output.read_text(encoding="utf-8")
            self.assertNotIn(unique_text, static_html)
            self.assertNotIn(draft_id, static_html)

            finished = self.post_json(url + "api/draft-finish", token, {
                "id": draft_id, "expected_revision": 1,
                "expected_hash": saved["saved_draft"]["content_hash"],
            })
            status, payload = finished
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["meta"]["draft_count"], 0)
            self.assertTrue(payload["saved_ref"])
            note = self.get_card(url, next(item for item in payload["tasks"] if item["kind"] == "note" and item["title"] == "A workspace draft"))
            self.assertEqual(note["ref"], payload["saved_ref"])
            self.assertEqual(note["body"], created["body"])
            saved_raw = (self.root / note["path"]).read_text(encoding="utf-8")
            self.assertEqual(taskweb.tasklib.card_format.parse_document(saved_raw)[0]["processing_instructions"],
                created["instructions"])
        finally:
            self.stop_interactive_web(process)

    def test_body_draft_cannot_replace_processing_instructions_property(self):
        fields, body = taskweb.tasklib.card_format.parse_document(self.note.read_text())
        fields["processing_instructions"] = "Keep source wording."
        self.note.write_text(taskweb.tasklib.card_format.render_document(fields, body))
        process, url = self.start_interactive_web()
        try:
            initial = self.get_json(url + "api/items")
            note = self.get_card(url, next(item for item in initial["tasks"] if item["kind"] == "note"))
            token = initial["meta"]["api_token"]
            import uuid
            request = {"id": str(uuid.uuid4()), "expected_revision": 0, "kind": "note", "mode": "edit",
                       "origin": "card-body", "title": "", "body": "## Processing instructions\n\nOrdinary heading.",
                       "target_ref": note["ref"], "base_hash": note["content_hash"]}
            status, response = self.post_json(url + "api/draft-save", token, request)
            self.assertEqual(status, 200, response)
            draft = response["saved_draft"]
            status, response = self.post_json(url + "api/draft-finish", token, {"id":draft["id"], "expected_revision":draft["revision"], "expected_hash":draft["content_hash"]})
            self.assertEqual(status, 200, response)
            actual, body = taskweb.tasklib.card_format.parse_document(self.note.read_text())
            self.assertEqual(actual["processing_instructions"], "Keep source wording.")
            self.assertEqual(body, request["body"])
        finally:
            self.stop_interactive_web(process)

    def test_saved_capture_drafts_can_be_cleared_without_losing_identity_or_guards(self) -> None:
        process, url = self.start_interactive_web()
        try:
            token = self.get_json(url + "api/items")["meta"]["api_token"]
            for index, kind in enumerate(("note", "task"), start=1):
                with self.subTest(kind=kind):
                    draft_id = f"00000000-0000-4000-8000-{index:012d}"
                    initial = {
                        "id": draft_id, "expected_revision": 0, "kind": kind, "mode": "new",
                        "capture_id": draft_id, "created": "2026-10-15T09:30:00",
                        "title": "Title to erase", "body": "Text to erase",
                        "instructions": "Guidance to erase",
                    }
                    status, response = self.post_json(url + "api/draft-save", token, initial)
                    self.assertEqual(status, 200, response)
                    saved = response["saved_draft"]
                    cleared = dict(initial, title="", body="", instructions="", expected_revision=saved["revision"],
                                   expected_hash=saved["content_hash"])
                    status, response = self.post_json(url + "api/draft-save", token, cleared)
                    self.assertEqual(status, 200, response)
                    updated = response["saved_draft"]
                    self.assertEqual(updated["revision"], 2)
                    for field in ("id", "kind", "capture_id", "created"):
                        self.assertEqual(updated[field], saved[field])
                    reopened = next(draft for draft in self.get_json(url + "api/drafts")["drafts"]
                                    if draft["id"] == draft_id)
                    self.assertEqual(reopened["body"], "")
                    self.assertEqual(reopened["title"], "")
                    self.assertEqual(reopened["instructions"], "")
                    status, conflict = self.post_json(url + "api/draft-save", token, cleared)
                    self.assertEqual(status, 409, conflict)
        finally:
            self.stop_interactive_web(process)

    def test_task_capture_draft_switches_kind_keeps_metadata_and_promotes_once(self) -> None:
        process, url = self.start_interactive_web()
        draft_id = "8ad0a2fc-985c-4d45-a696-99c67b800b5a"
        capture_id = "1f91547f-9e4f-471d-b4d3-36969257677e"
        body = "Keep this source\n## Literal heading\n````\nMarkdown stays literal\n````\n"
        try:
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            draft_body = {
                "id": draft_id, "expected_revision": 0, "kind": "note", "mode": "new",
                "capture_id": capture_id, "created": "2026-10-15T09:30:00",
                "title": "Task capture", "body": body,
                "status": "active", "priority": "high", "type": "bug",
                "project": "custom project", "topic": "custom topic",
                "tags": ["code", "custom-tag"], "due": "2026-12-31T09:45:00", "due_kind": "target",
            }
            status, note_mode = self.post_json(url + "api/draft-save", token, draft_body)
            self.assertEqual(status, 200, note_mode)
            self.assertEqual(note_mode["saved_draft"]["kind"], "note")
            switched = dict(draft_body, kind="task", expected_revision=1,
                            expected_hash=note_mode["saved_draft"]["content_hash"])
            status, task_mode = self.post_json(url + "api/draft-save", token, switched)
            self.assertEqual(status, 200, task_mode)
            self.assertEqual(task_mode["saved_draft"]["kind"], "task")
            self.assertEqual(task_mode["saved_draft"]["capture_id"], capture_id)
            self.assertEqual(task_mode["saved_draft"]["tags"], ["code", "custom-tag"])

            status, created = self.post_json(url + "api/draft-finish", token, {
                "id": draft_id, "expected_revision": 2,
                "expected_hash": task_mode["saved_draft"]["content_hash"],
            })
            self.assertEqual(status, 200, created)
            task = self.get_card(url, next(item for item in created["tasks"] if item["ref"] == created["saved_ref"]))
            self.assertEqual(task["kind"], "task")
            self.assertEqual(task["title"], "Task capture")
            self.assertEqual(task["status"], "active")
            self.assertEqual(task["priority"], "high")
            self.assertEqual(task["type"], "bug")
            self.assertEqual(task["project"], "custom project")
            self.assertEqual(task["topic"], "custom topic")
            self.assertEqual(task["tags"], ["code", "custom-tag"])
            self.assertEqual(task["due"], "2026-12-31T09:45:00")
            self.assertEqual(task["due_kind"], "target")
            stored = (self.root / task["path"]).read_text(encoding="utf-8")
            self.assertNotIn("microtask-capture", stored)
            self.assertIn("## Literal heading", stored)

            status, repeated = self.post_json(url + "api/draft-finish", token, {
                "id": draft_id, "expected_revision": 2,
                "expected_hash": task_mode["saved_draft"]["content_hash"],
            })
            self.assertEqual(status, 409, repeated)
            self.assertEqual(len(list(card_files(self.root, "task"))), 5)

            empty_id = "1ab9fd4d-c0f2-4446-adf5-c893949994fd"
            status, empty = self.post_json(url + "api/draft-save", token, {
                "id": empty_id, "expected_revision": 0, "kind": "note", "mode": "new",
                "capture_id": "75cc8138-74fd-4280-a323-1a98580653a5",
                "created": draft_body["created"], "title": "", "body": " \n\t",
            })
            self.assertEqual(status, 400, empty)
            self.assertEqual(empty["error"], "new drafts require a non-empty title or text")
        finally:
            self.stop_interactive_web(process)

    def test_task_metadata_api_is_atomic_hash_guarded_and_clears_due_fields_together(self) -> None:
        env = {**os.environ, "MT_USERNAME": "web-test"}
        process, url = self.start_interactive_web("--no-open", env=env)
        try:
            created = subprocess.run(
                [str(self.root / "mt"), "add", "Web metadata target", "--status", "doing", "--by", "web-test"],
                cwd=self.root, text=True, capture_output=True, check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            path = next(path for path in card_files(self.root, "task")
                        if "web-metadata-target" in path.stem)
            original = path.read_bytes()
            original_hash = hashlib.sha256(original).hexdigest()
            original_body = original.decode("utf-8").split("---\n", 2)[-1]
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            task = self.get_card(url, next(item for item in initial["tasks"] if item["path"] == str(path.relative_to(self.root))))
            self.assertEqual(task["content_hash"], original_hash)
            patch = {
                "priority": "high", "type": "decision", "project": "custom project",
                "topic": "custom topic", "tags": ["code", "custom-tag"],
                "due": "2026-12-31T09:45:00", "due_kind": "reminder",
            }
            status, updated = self.post_json(url + "api/card-metadata", token, {
                "ref": task["ref"], "expected_hash": original_hash, "patch": patch,
            })
            self.assertEqual(status, 200, updated)
            fresh = self.get_card(url, next(item for item in updated["tasks"] if item["path"] == task["path"]))
            self.assertEqual(fresh["priority"], "high")
            self.assertEqual(fresh["type"], "decision")
            self.assertEqual(fresh["tags"], ["code", "custom-tag"])
            saved = path.read_bytes()
            self.assertEqual(taskweb.tasklib.card_format.parse_document(saved.decode())[0]["claimed_by"], "web-test")
            self.assertEqual(saved.decode("utf-8").split("---\n", 2)[-1], original_body)

            status, stale = self.post_json(url + "api/card-metadata", token, {
                "ref": task["ref"], "expected_hash": original_hash,
                "patch": {"priority": "low"},
            })
            self.assertEqual(status, 409, stale)
            self.assertEqual(path.read_bytes(), saved)

            status, cleared = self.post_json(url + "api/card-metadata", token, {
                "ref": task["ref"], "expected_hash": fresh["content_hash"],
                "patch": {"due": None, "due_kind": None},
            })
            self.assertEqual(status, 200, cleared)
            cleared_task = self.get_card(url, next(item for item in cleared["tasks"] if item["path"] == task["path"]))
            self.assertIsNone(cleared_task["due"])
            self.assertIsNone(cleared_task["due_kind"])
            validated = subprocess.run(
                [str(self.root / "mt"), "validate"], cwd=self.root,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        finally:
            self.stop_interactive_web(process)

    def test_note_properties_save_together_and_reject_stale_or_invalid_changes(self) -> None:
        process, url = self.start_interactive_web()
        try:
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            note = self.get_card(url, next(item for item in initial["tasks"] if item["id"] == self.note.stem))
            body = {"ref": note["ref"], "expected_hash": note["content_hash"],
                    "patch": {"priority": "high", "project": "Research", "topic": "Work", "tags": ["a,b", "review"],
                              "due": "2026-12-20T09:30:00", "due_kind": "target"}}
            original_body = self.note.read_text().split("---\n", 2)[-1]
            status, saved = self.post_json(url + "api/card-metadata", token, body)
            self.assertEqual(status, 200, saved)
            fresh = self.get_card(url, next(item for item in saved["tasks"] if item["id"] == note["id"]))
            self.assertEqual((fresh["priority"], fresh["due"], fresh["due_kind"]),
                             ("high", "2026-12-20T09:30:00", "target"))
            self.assertEqual((fresh["project"], fresh["topic"], fresh["tags"]), ("Research", "Work", ["a,b", "review"]))
            self.assertEqual(self.note.read_text().split("---\n", 2)[-1], original_body)
            saved_bytes = self.note.read_bytes()
            status, stale = self.post_json(url + "api/card-metadata", token, body)
            self.assertEqual(status, 409, stale)
            self.assertEqual(self.note.read_bytes(), saved_bytes)
            body["expected_hash"] = fresh["content_hash"]
            for invalid_patch in ({"priority": "low", "type": "task"}, {"tags": [12]}, {"tags": ["bad\nvalue"]}):
                body["patch"] = invalid_patch
                status, invalid = self.post_json(url + "api/card-metadata", token, body)
                self.assertEqual(status, 400, invalid)
                self.assertEqual(self.note.read_bytes(), saved_bytes)
            body["patch"] = {"priority": "low", "due": "invalid date", "due_kind": "target"}
            status, invalid = self.post_json(url + "api/card-metadata", token, body)
            self.assertEqual(status, 400, invalid)
            self.assertEqual(self.note.read_bytes(), saved_bytes)
            body["patch"] = {"due": None, "due_kind": None}
            status, cleared = self.post_json(url + "api/card-metadata", token, body)
            self.assertEqual(status, 200, cleared)
            fresh = self.get_card(url, next(item for item in cleared["tasks"] if item["id"] == note["id"]))
            self.assertIsNone(fresh["due"])
            self.assertIsNone(fresh["due_kind"])
            self.assertEqual(fresh["priority"], "high")
        finally:
            self.stop_interactive_web(process)

    def test_inline_task_content_draft_preserves_metadata_and_rejects_stale_target(self) -> None:
        import uuid

        process, url = self.start_interactive_web()
        try:
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            task = self.get_card(url, next(item for item in initial["tasks"] if item["id"] == self.tasks[0].stem))
            source = task["body"].replace("Test content.", "Edited from the content panel.")
            request = {"id": str(uuid.uuid4()), "expected_revision": 0, "kind": "task", "mode": "edit",
                       "title": "", "body": source, "target_ref": task["ref"], "base_hash": task["content_hash"],
                       "base_updated": task["updated"], "origin": "card-body"}
            status, response = self.post_json(url + "api/draft-save", token, request)
            self.assertEqual(status, 200, response)
            draft = response["saved_draft"]
            status, result = self.post_json(url + "api/draft-finish", token, {
                "id": draft["id"], "expected_revision": draft["revision"], "expected_hash": draft["content_hash"]})
            self.assertEqual(status, 200, result)
            fresh = self.get_card(url, next(item for item in result["tasks"] if item["id"] == task["id"]))
            self.assertIn("Edited from the content panel.", fresh["body"])
            for key in ("status", "priority", "type", "tags", "due", "due_kind", "claimed_by"):
                self.assertEqual(fresh[key], task[key], key)
            unchanged = self.tasks[0].read_bytes()
            request["id"] = str(uuid.uuid4())
            status, response = self.post_json(url + "api/draft-save", token, request)
            self.assertEqual(status, 200, response)
            draft = response["saved_draft"]
            status, result = self.post_json(url + "api/draft-finish", token, {
                "id": draft["id"], "expected_revision": draft["revision"], "expected_hash": draft["content_hash"]})
            self.assertEqual(status, 409, result)
            self.assertEqual(self.tasks[0].read_bytes(), unchanged)
            self.assertTrue((self.root / "drafts" / (draft["id"] + ".md")).is_file())
        finally:
            self.stop_interactive_web(process)

    def test_note_edit_draft_keeps_text_when_target_changes(self) -> None:
        process, url = self.start_interactive_web()
        try:
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            note = self.get_card(url, next(item for item in initial["tasks"] if item["id"] == self.note.stem))
            draft_body = {"id": "c38f37dd-2eaa-4a80-a240-4a2b3b92c617", "expected_revision": 0,
                          "kind": "note", "mode": "edit", "origin": "card-body", "target_ref": note["ref"],
                          "base_hash": note["content_hash"], "base_updated": note["updated"],
                          "title": "", "body": "Preserve this stale edit."}
            status, created = self.post_json(url + "api/draft-save", token, draft_body)
            self.assertEqual(status, 200, created)
            changed = subprocess.run(
                [str(self.root / "mt"), "edit", note["ref"], "--body", "--file", "-",
                 "--expected-hash", note["content_hash"]],
                cwd=self.root, input="The target changed independently.", text=True, capture_output=True,
            )
            self.assertEqual(changed.returncode, 0, changed.stderr)
            status, conflict = self.post_json(url + "api/draft-finish", token, {
                "id": draft_body["id"], "expected_revision": 1,
                "expected_hash": created["saved_draft"]["content_hash"],
            })
            self.assertEqual(status, 409, conflict)
            self.assertIn("target changed", conflict["error"])
            kept = self.get_json(url + "api/drafts")["drafts"]
            self.assertEqual(kept[0]["body"], draft_body["body"])
        finally:
            self.stop_interactive_web(process)

    def test_draft_hash_guard_detects_manual_markdown_changes_for_edit_and_discard(self) -> None:
        process, url = self.start_interactive_web()
        draft_id = "0ae09c1c-6df1-48d0-8b7b-fc15e781a3bb"
        capture_id = "ef4dc16b-8a5e-47fb-a006-7261544a1621"
        try:
            token = self.get_json(url + "api/items")["meta"]["api_token"]
            status, saved = self.post_json(url + "api/draft-save", token, {
                "id": draft_id, "expected_revision": 0, "kind": "note", "mode": "new",
                "capture_id": capture_id, "created": "2026-10-15T09:30:00",
                "title": "Hash guard", "body": "Original text",
            })
            self.assertEqual(status, 200, saved)
            path = self.root / "drafts" / f"{draft_id}.md"
            original_hash = saved["saved_draft"]["content_hash"]
            raw = path.read_text(encoding="utf-8")
            header, separator, _body = raw.partition("\n---\n")
            self.assertTrue(separator)
            path.write_text(header + separator + "manual Git edit\n", encoding="utf-8")

            status, edit_conflict = self.post_json(url + "api/draft-save", token, {
                "id": draft_id, "expected_revision": 1, "expected_hash": original_hash,
                "kind": "note", "mode": "new", "capture_id": capture_id,
                "created": "2026-10-15T09:30:00", "title": "Hash guard", "body": "Overwrite",
            })
            self.assertEqual(status, 409, edit_conflict)
            self.assertIn("outside microtask", edit_conflict["error"])

            status, discard_conflict = self.post_json(url + "api/draft-discard", token, {
                "id": draft_id, "expected_revision": 1, "expected_hash": original_hash,
            })
            self.assertEqual(status, 409, discard_conflict)
            self.assertTrue(path.is_file())
            self.assertIn("manual Git edit", path.read_text(encoding="utf-8"))
        finally:
            self.stop_interactive_web(process)

    def test_edit_draft_without_original_baseline_cannot_apply(self) -> None:
        process, url = self.start_interactive_web()
        draft_id = "e1941a6e-739d-49e4-9391-d70770fb149b"
        try:
            initial = self.get_json(url + "api/items")
            token = initial["meta"]["api_token"]
            note = self.get_card(url, next(item for item in initial["tasks"] if item["id"] == self.note.stem))
            status, saved = self.post_json(url + "api/draft-save", token, {
                "id": draft_id, "expected_revision": 0, "kind": "note", "mode": "edit", "origin": "card-body",
                "target_ref": note["ref"], "title": "", "body": "Recovered unbased edit",
            })
            self.assertEqual(status, 200, saved)
            self.assertEqual(saved["saved_draft"]["base_hash"], "0" * 64)
            status, conflict = self.post_json(url + "api/draft-finish", token, {
                "id": draft_id, "expected_revision": 1,
                "expected_hash": saved["saved_draft"]["content_hash"],
            })
            self.assertEqual(status, 409, conflict)
            self.assertIn("no verified target baseline", conflict["error"])
            self.assertEqual(self.get_json(url + "api/drafts")["drafts"][0]["body"], "Recovered unbased edit")
        finally:
            self.stop_interactive_web(process)

    def test_interactive_read_endpoints_do_not_import_editor_recovery_copies(self) -> None:
        recovery = self.root / ".mt" / "recovery" / "edit"
        recovery.mkdir(parents=True)
        source = recovery / "new-note-edit-0123456789abcdef0123456789abcdef.md"
        source.write_text("RECOVERY_READ_SENTINEL\n", encoding="utf-8")
        process, url = self.start_interactive_web()
        try:
            items = self.get_json(url + "api/items")
            listed = self.get_json(url + "api/drafts")
            self.assertNotIn("drafts", items)
            self.assertNotIn("RECOVERY_READ_SENTINEL", json.dumps(items))
            self.assertEqual(listed["drafts"], [])
            self.assertTrue(source.is_file())
        finally:
            self.stop_interactive_web(process)

    def test_needs_review_is_exposed_and_cleared_through_edit(self) -> None:
        note_path = self.write_note(
            "2026-06-22-090000-review-note",
            "Needs review note",
            needs_review=True,
        )
        process, url = self.start_interactive_web()
        try:
            payload = self.get_json(url + "api/items")
            token = payload["meta"]["api_token"]
            note = self.get_card(url, next(item for item in payload["tasks"] if item["id"] == note_path.stem))
            self.assertTrue(note["needs_review"])

            status, reviewed = self.post_json(
                url + "api/review",
                token,
                {
                    "ref": note["ref"],
                    "expected_hash": note["content_hash"],
                },
            )
            self.assertEqual(status, 200, reviewed)
            result = self.get_card(url, next(item for item in reviewed["tasks"] if item["id"] == note_path.stem))
            self.assertFalse(result["needs_review"])
            stored = note_path.read_text(encoding="utf-8")
            self.assertIn("needs_review: false", stored)
        finally:
            self.stop_interactive_web(process)


class MdvizUnchangedTests(unittest.TestCase):
    def test_mdviz_behavior_remains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "task.md"
            output = root / "preview.html"
            source.write_text("# Title\n\n| x | y |\n| - | - |\n| 1 | $x^2$ |\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(MDVIZ), str(source), "--out", str(output), "--no-open"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("marked@15.0.12", rendered)
            self.assertIn("dompurify@3.2.6", rendered)
            self.assertIn("katex@0.16.10", rendered)
            self.assertNotIn("@picocss/pico", rendered)
            self.assertNotIn("taskBoard()", rendered)


if __name__ == "__main__":
    unittest.main()
