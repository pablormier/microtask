from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
MDVIZ = SOURCE_ROOT / "tools" / "mdviz.py"


class MarkdownViewerTests(unittest.TestCase):
    def test_renders_frontmatter_body_and_safe_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "task with spaces.md"
            output = root / "preview with spaces.html"
            source.write_text(
                """---
status: active
tags:
  - math
  - test
---

# Equation

| x | y |
| - | - |
| 1 | $x^2$ |

![local](image.png)

<img src=x onerror=alert(1)></script><script>alert(2)</script>
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(MDVIZ), str(source), "--out", str(output), "--no-open"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("<dt>status</dt><dd>active</dd>", rendered)
            self.assertIn("<dt>tags</dt><dd>math, test</dd>", rendered)
            self.assertNotIn("status: active", rendered)
            self.assertIn("DOMPurify.sanitize(restored", rendered)
            self.assertIn("marked@15.0.12", rendered)
            self.assertIn("dompurify@3.2.6", rendered)
            self.assertIn("katex@0.16.10", rendered)
            self.assertIn("Preview libraries could not be loaded", rendered)
            self.assertIn(source.resolve().parent.as_uri() + "/", rendered)
            self.assertIn("ul { list-style: disc; padding-left: 1.35rem; }", rendered)
            self.assertIn("ol { list-style: decimal; padding-left: 1.35rem; }", rendered)
            self.assertNotIn("</script><script>alert(2)", rendered)
            self.assertIn(r"\u003c/script>\u003cscript>alert(2)\u003c/script>", rendered)

    def test_default_output_is_stable_and_path_specific(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notes.md"
            source.write_text("# Notes\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(MDVIZ), str(source), "--no-open"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            digest = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:8]
            output = Path(result.stdout.strip())
            self.assertEqual(output.name, f"notes-{digest}.mdviz.html")
            output.unlink()

    def test_card_preview_uses_shared_renderer_and_preserves_literal_markdown(self) -> None:
        sys.path.insert(0, str(SOURCE_ROOT / "tools"))
        sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
        import card_format
        import mdviz
        source = Path(tempfile.gettempdir()) / "microtask-preview-fixture.md"
        content = "## Notes\n\nThis is document content.\n\n````markdown\n```microtask-capture\nLiteral example.\n```\n````"
        properties = {"id": "preview-fixture", "kind": "note", "title": "Preview fixture", "priority": "high",
                      "created":"2026-11-10", "updated":"2026-11-10",
                      "comments": [{"at": "2026-11-10", "text": "**Comment**"}]}
        raw = card_format.render_document(properties, content)
        rendered = mdviz.build_html(source, raw)
        self.assertIn("<h1>Preview fixture</h1>", rendered)
        self.assertIn('"body": "## Notes', rendered)
        self.assertIn("microtask-capture", rendered)
        self.assertIn("renderPreview(card, root)", rendered)
        self.assertIn((SOURCE_ROOT / "tools/web/js/markdown.js").read_text(), rendered)
        self.assertIn('"comments": [{"at": "2026-11-10", "text": "**Comment**"}]', rendered)



class TaskShowTests(unittest.TestCase):
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
            self.write_task("inbox", "2026-01-01-120000-first", "First task", "Shared token"),
            self.write_task("active", "2026-01-02-120000-second", "Second task", "Shared token"),
        ]
        self.output = self.root / "preview with spaces.html"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_task(self, status: str, task_id: str, title: str, note: str) -> Path:
        path = self.root / "cards" / f"{task_id}.md"
        sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
        import card_format
        properties = {"id": task_id, "kind": "task", "title": title, "status": status,
                      "priority": "medium", "type": "task", "created": "2026-01-01", "updated": "2026-01-01",
                      "next_action": "Open the preview."}
        path.write_text(card_format.render_document(properties, "Test viewing.\n\n## Notes\n\n" + note + "\n\n## Extra context\n"), encoding="utf-8")
        return path

    def run_mt(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), "show", *arguments, "--out", str(self.output), "--no-open"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_read(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), "read", *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_show_resolves_short_reference(self) -> None:
        task = self.tasks[0]
        short_ref = hashlib.sha256(task.stem.encode()).hexdigest()[:8]
        result = self.run_mt(f"@{short_ref}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<h1>First task</h1>", self.output.read_text(encoding="utf-8"))

    def test_read_resolves_abbreviated_short_reference(self) -> None:
        task = self.tasks[0]
        short_ref = hashlib.sha256(task.stem.encode()).hexdigest()[:8]

        result = self.run_read(f"@{short_ref[:3]}")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, task.read_text(encoding="utf-8"))

    def test_read_does_not_treat_bare_hex_text_as_a_short_reference(self) -> None:
        short_ref = hashlib.sha256(self.tasks[0].stem.encode()).hexdigest()[:8]

        result = self.run_read(short_ref[:3])

        self.assertEqual(result.returncode, 1)
        self.assertIn("No item matches", result.stderr)

    def test_read_reports_invalid_short_reference(self) -> None:
        result = self.run_read("@ab")

        self.assertEqual(result.returncode, 2)
        self.assertIn("short reference must be @ followed by 3 to 8 hexadecimal characters", result.stderr)

    def test_read_reports_ambiguous_short_reference_prefix(self) -> None:
        first = self.tasks[0]
        prefix = hashlib.sha256(first.stem.encode()).hexdigest()[:3]
        collision = "2026-01-03-120000-collision-4550"
        self.assertTrue(hashlib.sha256(collision.encode()).hexdigest().startswith(prefix))
        self.write_task("doing", collision, "Collision task", "Unrelated")

        result = self.run_read(f"@{prefix}")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Multiple items match", result.stderr)
        self.assertIn("First task", result.stderr)
        self.assertIn("Collision task", result.stderr)

    def test_show_reports_no_match(self) -> None:
        result = self.run_mt("query-that-cannot-match-any-task-8d693c")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No item matches", result.stderr)

    def test_show_resolves_unique_text_query(self) -> None:
        result = self.run_mt("Second task")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<h1>Second task</h1>", self.output.read_text(encoding="utf-8"))

    def test_show_previews_saved_plan_path(self) -> None:
        plan = self.root / ".mt" / "plans" / "2026-01-03-120000-week.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Weekly plan\n\nPlan the week.\n", encoding="utf-8")

        result = self.run_mt(str(plan.relative_to(self.root)))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(plan.name, self.output.read_text(encoding="utf-8"))

    def test_show_supports_fuzzy_matching(self) -> None:
        result = self.run_mt("--fuzzy", "secnd")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<h1>Second task</h1>", self.output.read_text(encoding="utf-8"))

    def test_show_reports_multiple_matches(self) -> None:
        result = self.run_mt("Shared token")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Multiple items match", result.stderr)
        self.assertIn("First task", result.stderr)
        self.assertIn("Second task", result.stderr)

    def test_read_prints_raw_markdown_for_short_reference(self) -> None:
        task = self.tasks[0]
        short_ref = hashlib.sha256(task.stem.encode()).hexdigest()[:8]

        result = self.run_read(f"@{short_ref}")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, task.read_text(encoding="utf-8"))

    def test_read_supports_shared_and_match_mode(self) -> None:
        result = self.run_read("--and", "second", "token")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.tasks[1].read_text(encoding="utf-8"))

    def test_read_reports_no_match(self) -> None:
        result = self.run_read("query-that-cannot-match-any-task-8d693c")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("No item matches", result.stderr)

    def test_read_reports_multiple_matches(self) -> None:
        result = self.run_read("Shared token")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Multiple items match", result.stderr)
        self.assertIn("First task", result.stderr)
        self.assertIn("Second task", result.stderr)


if __name__ == "__main__":
    unittest.main()
