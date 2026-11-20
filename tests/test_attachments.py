"""Tests for attachments storage, validation, CLI, sync, update, and web server."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import attachments  # noqa: E402
import operations  # noqa: E402
import policy_parser  # noqa: E402
import store  # noqa: E402
import taskweb  # noqa: E402
import transaction  # noqa: E402
import validation  # noqa: E402
from workspace import Workspace  # noqa: E402
from support import run_cli  # noqa: E402


# Minimal valid 1x1 PNG bytes.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def make_data_workspace(tmp_dir: Path) -> Workspace:
    (tmp_dir / "cards").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "templates" / "settings.yaml", tmp_dir / "settings.yaml")
    (tmp_dir / ".gitignore").write_text(".mt/\n", encoding="utf-8")
    return Workspace.at(tmp_dir, app_root=ROOT)


class AttachmentCoreTests(unittest.TestCase):
    def test_naming_and_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_data_workspace(Path(tmp))
            first = operations.op_store_attachment(
                ws, PNG_1X1, filename="Screenshot 2026.PNG", content_type="image/png", sync=False
            )
            self.assertTrue(first.changed)
            self.assertFalse(first.receipt["deduplicated"])
            self.assertTrue(first.receipt["is_image"])
            self.assertTrue(str(first.receipt["filename"]).endswith("-screenshot-2026.png"))
            self.assertEqual(first.receipt["markdown"], f"![]({first.receipt['relative_path']})")

            # Pasting the exact same bytes with the same filename deduplicates.
            second = operations.op_store_attachment(
                ws, PNG_1X1, filename="Screenshot 2026.PNG", content_type="image/png", sync=False
            )
            self.assertFalse(second.changed)
            self.assertTrue(second.receipt["deduplicated"])
            self.assertEqual(second.receipt["filename"], first.receipt["filename"])

            # Generic clipboard paste name collapses to <hash>.<ext>.
            unnamed = operations.op_store_attachment(
                ws, PNG_1X1 + b"extra", filename="image.png", content_type="image/png", sync=False
            )
            self.assertRegex(str(unnamed.receipt["filename"]), r"^[0-9a-f]{12}\.png$")

            # Non-image attachment generates a regular Markdown link with filename label.
            pdf = operations.op_store_attachment(
                ws, b"%PDF-1.4\n", filename="Quarterly Report.pdf", content_type="application/pdf", sync=False
            )
            self.assertFalse(pdf.receipt["is_image"])
            self.assertEqual(
                pdf.receipt["markdown"],
                f"[Quarterly Report.pdf]({pdf.receipt['relative_path']})",
            )

    def test_size_limit_and_empty_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = make_data_workspace(root)
            policy_text = (root / "settings.yaml").read_text(encoding="utf-8")
            (root / "settings.yaml").write_text(
                policy_text.replace("max_attachment_mb: 10", "max_attachment_mb: 0.001"),
                encoding="utf-8",
            )
            ws = Workspace.at(root, app_root=ROOT)
            with self.assertRaises(store.InputError):
                operations.op_store_attachment(ws, b"", filename="empty.txt", sync=False)
            with self.assertRaises(store.InputError):
                operations.op_store_attachment(ws, b"x" * 2048, filename="big.bin", sync=False)

    def test_validation_safety_subfolders_and_broken_link_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = make_data_workspace(root)

            # Manual subfolder attachment works and does not produce orphan warnings.
            manual_dir = root / "attachments" / "papers"
            manual_dir.mkdir(parents=True)
            (manual_dir / "notes.pdf").write_bytes(b"%PDF-1.4\n")
            (manual_dir / "unreferenced.txt").write_text("manual file\n", encoding="utf-8")

            # Create a card referencing one valid and one missing attachment.
            with transaction.time_mutation_lock(ws):
                operations.create_card(
                    "task",
                    (
                        "See [paper](../attachments/papers/notes.pdf) and "
                        "![missing](../attachments/does-not-exist.png).\n\n"
                        "Code span `![](../attachments/ignored.png)` is ignored."
                    ),
                    title="Review attachment links",
                    ws=ws,
                )
            policy = policy_parser.load_policy(root)
            report = validation.audit_workspace(root, policy)
            self.assertEqual(report.errors, [])
            self.assertEqual(len(report.warnings), 1)
            self.assertIn("does-not-exist.png", report.warnings[0])

            # Symlink under attachments/ is rejected as a hard validation error.
            outside = root / "secret.txt"
            outside.write_text("nope", encoding="utf-8")
            symlink_path = root / "attachments" / "escape.txt"
            symlink_path.symlink_to(outside)
            bad_report = validation.audit_workspace(root, policy)
            self.assertTrue(any("symbolic link under attachments/" in err for err in bad_report.errors))
            symlink_path.unlink()

            # Oversized attachment is rejected by validation.
            (root / "settings.yaml").write_text(
                (root / "settings.yaml").read_text(encoding="utf-8").replace(
                    "max_attachment_mb: 10", "max_attachment_mb: 0.001"
                ),
                encoding="utf-8",
            )
            (root / "attachments" / "huge.bin").write_bytes(b"0" * 4096)
            size_report = validation.audit_workspace(root, policy_parser.load_policy(root))
            self.assertTrue(any("exceeds max_attachment_mb" in err for err in size_report.errors))

    def test_cli_attach_and_card_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = make_data_workspace(root)
            sample = root / "diagram.png"
            sample.write_bytes(PNG_1X1)
            with transaction.time_mutation_lock(ws):
                card, _ = operations.create_card("task", "Initial body.", title="Attach target", ws=ws)
            ref = f"@{card.short_ref}"

            env = {"MT_WORKSPACE": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
            res = run_cli(ROOT, "attach", str(sample), "--card", ref, "--json", env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertTrue(payload["is_image"])
            self.assertTrue((root / payload["path"]).is_file())
            self.assertEqual(payload["card"]["ref"], ref)

            read_res = run_cli(ROOT, "read", ref, "--json", env=env)
            self.assertEqual(read_res.returncode, 0, read_res.stderr)
            card_data = json.loads(read_res.stdout)
            self.assertIn(payload["markdown"], card_data["body"])

    def test_cli_attach_missing_card_leaves_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data_workspace(root)
            sample = root / "diagram.png"
            sample.write_bytes(PNG_1X1)
            env = {"MT_WORKSPACE": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
            res = run_cli(ROOT, "attach", str(sample), "--card", "@nonexistent-card", env=env)
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(list((root / "attachments").glob("*")) if (root / "attachments").exists() else [], [])

    def test_cli_attach_rejects_file_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data_workspace(root)
            policy_path = root / "settings.yaml"
            text = policy_path.read_text(encoding="utf-8")
            self.assertIn("max_attachment_mb: 10", text)
            policy_path.write_text(text.replace("max_attachment_mb: 10", "max_attachment_mb: 0.000001"), encoding="utf-8")
            sample = root / "diagram.png"
            sample.write_bytes(PNG_1X1)
            env = {"MT_WORKSPACE": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
            res = run_cli(ROOT, "attach", str(sample), env=env)
            self.assertEqual(res.returncode, 2, res.stderr)
            self.assertIn("max_attachment_mb", res.stderr)
            self.assertEqual(list((root / "attachments").glob("*")) if (root / "attachments").exists() else [], [])

    def test_upload_js_replacement_and_missing_placeholder(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        source = (ROOT / "tools" / "web" / "js" / "attachments.js").read_text(encoding="utf-8")
        scenario = r"""
(async () => {
  const assert = require("assert");
  let attachment = {markdown: "[cost$&$$.txt](../attachments/x.txt)"};
  global.uploadAttachmentFile = async () => attachment;
  let text = "";
  const notes = [];
  const run = (mid) => uploadAndInsertAttachments({
    files: [{name: "cost.txt", type: "text/plain"}], textarea: null,
    getText: () => text, setText: (t) => { text = t; },
    notify: (m, e) => notes.push([m, e]),
  });
  await run();
  assert.strictEqual(text, attachment.markdown);
  assert.strictEqual(notes.length, 0);
  // Placeholder removed mid-upload: text untouched, link surfaced via notify.
  global.uploadAttachmentFile = async () => { text = "other card"; return attachment; };
  const uploaded = await run();
  assert.strictEqual(text, "other card");
  assert.strictEqual(uploaded.length, 1);
  assert.strictEqual(notes.length, 1);
  assert.strictEqual(notes[0][1], false);
  assert.ok(notes[0][0].includes(attachment.markdown));
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([node], input=source + "\n" + scenario, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_upload_js_surfaces_the_history_warning(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        source = (ROOT / "tools" / "web" / "js" / "attachments.js").read_text(encoding="utf-8")
        scenario = r"""
(async () => {
  const assert = require("assert");
  global.readJsonResponse = async (response) => response.body;
  global.fetch = async () => ({ok: true, status: 200,
    body: {attachment: {markdown: "[a](../attachments/a.txt)"}, history_warning: "Local history is unavailable: disk full"}});
  let text = "";
  const notes = [];
  const uploaded = await uploadAndInsertAttachments({
    files: [{name: "a.txt", type: "text/plain"}], textarea: null,
    getText: () => text, setText: (t) => { text = t; },
    notify: (m, e) => notes.push([m, e]),
  });
  assert.strictEqual(uploaded.length, 1);
  assert.strictEqual(text, "[a](../attachments/a.txt)");
  assert.deepStrictEqual(notes, [["Local history is unavailable: disk full", true]]);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([node], input=source + "\n" + scenario, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_commits_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = make_data_workspace(root)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

            # Add an attachment including a .md file inside attachments/ to verify it is not treated as a card.
            operations.op_store_attachment(ws, PNG_1X1, filename="shot.png", sync=False)
            sub = root / "attachments" / "docs"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "readme.md").write_text("# Attached markdown\n", encoding="utf-8")

            rc, committed = transaction._sync_stage_and_commit_local(ws, None, out=io.StringIO(), err=io.StringIO())
            self.assertIsNone(rc)
            self.assertTrue(committed)
            msg = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(msg, "Update 2 attachments")

    def test_content_disposition_ascii_and_unicode(self) -> None:
        ascii_header = attachments.content_disposition_attachment("report.zip")
        self.assertEqual(ascii_header, 'attachment; filename="report.zip"')
        ascii_header.encode("latin-1")

        unicode_header = attachments.content_disposition_attachment("日本語-résumé.zip")
        unicode_header.encode("latin-1")
        self.assertIn('filename="', unicode_header)
        self.assertIn("filename*=UTF-8''", unicode_header)
        self.assertIn("%E6%97%A5%E6%9C%AC%E8%AA%9E", unicode_header)

        control_header = attachments.content_disposition_attachment('bad"\r\nname.txt')
        control_header.encode("latin-1")
        self.assertNotIn("\r", control_header)
        self.assertNotIn("\n", control_header)

    def test_attachment_csp_allows_pdf_viewer_and_sandboxes_others(self) -> None:
        self.assertEqual(attachments.attachment_csp(Path("paper.pdf")), "default-src 'none'; object-src 'self'")
        self.assertEqual(attachments.attachment_csp(Path("PAPER.PDF")), "default-src 'none'; object-src 'self'")
        self.assertFalse(attachments.should_force_download(Path("paper.pdf")))
        for name in ("diagram.svg", "shot.png", "evil.html", "archive.zip"):
            self.assertEqual(attachments.attachment_csp(Path(name)), "sandbox")


class AttachmentWebServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = make_data_workspace(self.root)
        self.token = "test-attach-token"
        self.server = taskweb.TaskWebServer(("127.0.0.1", 0), self.token, self.ws)
        self.host, self.port = self.server.server_address
        self.base_url = f"http://{self.host}:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_upload_serve_security_headers_and_traversal_block(self) -> None:
        # Reject upload without valid token.
        req_no_token = urllib.request.Request(
            f"{self.base_url}/api/attachments",
            data=PNG_1X1,
            method="POST",
            headers={"Content-Type": "image/png", "X-Filename": "paste.png"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_no_token, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

        # Upload PNG with token.
        req = urllib.request.Request(
            f"{self.base_url}/api/attachments",
            data=PNG_1X1,
            method="POST",
            headers={
                "X-Microtask-Token": self.token,
                "Content-Type": "image/png",
                "X-Filename": "paste.png",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        att = data["attachment"]
        self.assertTrue(att["is_image"])
        self.assertTrue(att["url"].startswith("/attachments/"))

        # Fetch uploaded image and verify security headers + ETag.
        with urllib.request.urlopen(f"{self.base_url}{att['url']}", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/png")
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(resp.headers.get("Content-Security-Policy"), "sandbox")
            self.assertIsNone(resp.headers.get("Content-Disposition"))
            self.assertEqual(resp.read(), PNG_1X1)
            etag = resp.headers.get("ETag")
            self.assertTrue(etag)

        # Conditional GET returns 304 Not Modified.
        req_304 = urllib.request.Request(
            f"{self.base_url}{att['url']}", headers={"If-None-Match": etag}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx304:
            urllib.request.urlopen(req_304, timeout=5)
        self.assertEqual(ctx304.exception.code, 304)

        # Upload HTML file and verify Content-Disposition: attachment + sandbox CSP.
        req_html = urllib.request.Request(
            f"{self.base_url}/api/attachments",
            data=b"<script>alert(1)</script>",
            method="POST",
            headers={
                "X-Microtask-Token": self.token,
                "Content-Type": "text/html",
                "X-Filename": "evil.html",
            },
        )
        with urllib.request.urlopen(req_html, timeout=5) as resp:
            html_att = json.loads(resp.read().decode("utf-8"))["attachment"]
        with urllib.request.urlopen(f"{self.base_url}{html_att['url']}", timeout=5) as resp:
            self.assertEqual(resp.headers.get("Content-Security-Policy"), "sandbox")
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

        # Manual subfolder file is served cleanly.
        sub = self.root / "attachments" / "papers"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "spec.pdf").write_bytes(b"%PDF-1.4\n")
        with urllib.request.urlopen(f"{self.base_url}/attachments/papers/spec.pdf", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "application/pdf")
            self.assertEqual(resp.headers.get("Content-Security-Policy"), "default-src 'none'; object-src 'self'")
            self.assertIsNone(resp.headers.get("Content-Disposition"))

        # Traversal attempts return 404.
        for bad_path in ("/attachments/../settings.yaml", "/attachments/%2e%2e/settings.yaml"):
            with self.assertRaises(urllib.error.HTTPError) as bad_ctx:
                urllib.request.urlopen(f"{self.base_url}{bad_path}", timeout=5)
            self.assertEqual(bad_ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
