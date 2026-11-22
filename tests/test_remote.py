"""End-to-end tests for remote `mt` mode against a hosted shared workspace."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from tools import taskweb  # noqa: E402
from tools import web_hosted  # noqa: E402

SECRET = "test-proxy-secret-0123456789"
TOKEN = "test-token"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
    return {**env, **GIT_ENV}


class RemoteModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.workspace_dir = self.base / "shared"
        init = subprocess.run(
            [str(ROOT / "mt"), "init", "--data", str(self.workspace_dir)],
            env=clean_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        settings = self.workspace_dir / "settings.yaml"
        settings.write_text(
            settings.read_text(encoding="utf-8").replace(
                "  max_doing_tasks: 5\n",
                "  max_doing_tasks: 5\n  doing_limit_scope: per_claimant\n",
            ),
            encoding="utf-8",
        )
        self.members_file = self.base / "team.members"
        self.members_file.write_text("alice@example.com\nbob@example.com\n", encoding="utf-8")
        self.tokens_file = self.base / "team.tokens.json"
        self.alice_token, _ = web_hosted.issue_token(
            self.tokens_file, "alice@example.com", label="alice-laptop", members_path=self.members_file,
        )
        self.bob_token, _ = web_hosted.issue_token(
            self.tokens_file, "bob@example.com", label="bob-laptop", members_path=self.members_file,
        )

        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ws = taskweb.Workspace.at(self.workspace_dir)
        config = web_hosted.HostedConfig(
            SECRET,
            frozenset({"alice"}),
            tokens_file=str(self.tokens_file),
            members_file=str(self.members_file),
        )
        self.server = taskweb.TaskWebServer(("127.0.0.1", 0), TOKEN, self.ws, hosted=config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.remote_url = f"http://127.0.0.1:{self.port}"
        self.server_stopped = False

    def stop_server(self) -> None:
        if not self.server_stopped:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.server_stopped = True

    def run_remote_mt(
        self,
        *args: str,
        token: str | None,
        remote_url: str | None = None,
        use_repo_flag: bool = False,
        cwd: Path | None = None,
        input_text: str | None = None,
        mt_bin: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = clean_env()
        url = remote_url or self.remote_url
        if not use_repo_flag:
            env["MT_REMOTE"] = url
        if token is not None:
            env["MT_REMOTE_TOKEN"] = token
        cmd = [str(mt_bin or (ROOT / "mt"))]
        if use_repo_flag:
            cmd.extend(["-R", url])
        cmd.extend(args)
        return subprocess.run(
            cmd,
            cwd=cwd or self.base,
            env=env,
            capture_output=True,
            text=True,
            input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            check=False,
        )

    def dashboard_get(self, path: str, *, email: str = "bob@example.com") -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                "GET",
                path,
                headers={
                    web_hosted.SECRET_HEADER: SECRET,
                    web_hosted.EMAIL_HEADER: email,
                },
            )
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def test_add_move_read_with_two_credentials_and_dashboard_visibility(self) -> None:
        body_file = self.base / "spec.md"
        body_file.write_text("Run the calibration sweep locally.\n", encoding="utf-8")

        added = self.run_remote_mt(
            "add",
            "--title",
            "Calibration sweep",
            "--file",
            str(body_file),
            "--json",
            token=self.alice_token,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        add_receipt = json.loads(added.stdout)
        ref = add_receipt["ref"]
        self.assertEqual(add_receipt["properties"]["title"], "Calibration sweep")

        # Claim as Alice using -R URL mode too.
        moved = self.run_remote_mt(
            "move",
            ref,
            "doing",
            "--by",
            "bob",  # Server must ignore client-supplied --by and attribute to Alice.
            "--json",
            token=self.alice_token,
            use_repo_flag=True,
        )
        self.assertEqual(moved.returncode, 0, moved.stderr)
        move_receipt = json.loads(moved.stdout)
        self.assertEqual(move_receipt["properties"]["status"], "doing")
        self.assertEqual(move_receipt["properties"]["claimed_by"], "alice")

        # Bob reads the card via remote `mt read --json`, `mt list --json`, `mt search --json`, and dashboard API.
        read_res = self.run_remote_mt("read", ref, "--json", token=self.bob_token)
        self.assertEqual(read_res.returncode, 0, read_res.stderr)
        read_card = json.loads(read_res.stdout)
        self.assertEqual(read_card["ref"], ref)
        self.assertEqual(read_card["properties"]["claimed_by"], "alice")
        self.assertEqual(read_card["content_hash"], move_receipt["content_hash"])

        list_res = self.run_remote_mt("list", "--status", "doing", "--json", token=self.bob_token)
        self.assertEqual(list_res.returncode, 0, list_res.stderr)
        listed = json.loads(list_res.stdout)
        self.assertEqual([item["ref"] for item in listed], [ref])
        self.assertEqual(listed[0]["claimed_by"], "alice")

        search_res = self.run_remote_mt("search", "calibration", "--json", token=self.bob_token)
        self.assertEqual(search_res.returncode, 0, search_res.stderr)
        self.assertEqual([item["ref"] for item in json.loads(search_res.stdout)], [ref])

        dash_status, dash_payload = self.dashboard_get("/api/items", email="bob@example.com")
        self.assertEqual(dash_status, 200)
        dash_item = next(item for item in dash_payload["tasks"] if item["ref"] == ref)
        self.assertEqual(dash_item["status"], "doing")
        self.assertEqual(dash_item["claimed_by"], "alice")

        # Bob attempts full body replacement with the current hash -> isolated 409 claim conflict.
        bob_body = self.base / "bob-edit.md"
        bob_body.write_text("Bob overwriting Alice's work.\n", encoding="utf-8")
        bob_edit = self.run_remote_mt(
            "edit",
            ref,
            "--body",
            "--file",
            str(bob_body),
            "--expected-hash",
            read_card["content_hash"],
            token=self.bob_token,
        )
        self.assertNotEqual(bob_edit.returncode, 0)
        self.assertIn("claimed by 'alice'", bob_edit.stderr)

        # Bob cannot claim Alice's task or use --takeover (non-admin -> 403).
        bob_claim = self.run_remote_mt("move", ref, "doing", token=self.bob_token)
        self.assertNotEqual(bob_claim.returncode, 0)
        self.assertIn("claimed by 'alice'", bob_claim.stderr)

        bob_takeover = self.run_remote_mt("move", ref, "doing", "--takeover", token=self.bob_token)
        self.assertNotEqual(bob_takeover.returncode, 0)
        self.assertIn("administrator", bob_takeover.stderr)

        # Bob's comment succeeds and is attributed to Bob.
        bob_comment = self.run_remote_mt("comment", ref, "-m", "Checked dataset split.", "--json", token=self.bob_token)
        self.assertEqual(bob_comment.returncode, 0, bob_comment.stderr)
        comment_receipt = json.loads(bob_comment.stdout)
        self.assertEqual(comment_receipt["properties"]["comments"][-1]["by"], "bob")
        self.assertEqual(comment_receipt["properties"]["comments"][-1]["text"], "Checked dataset split.")

        # Alice attempts body replacement using her OLD hash (before Bob's comment) -> stale-write 409 conflict.
        alice_body = self.base / "alice-result.md"
        alice_body.write_text("Calibration sweep finished: ECE=0.018.\n", encoding="utf-8")
        stale_edit = self.run_remote_mt(
            "edit",
            ref,
            "--body",
            "--file",
            str(alice_body),
            "--expected-hash",
            move_receipt["content_hash"],
            token=self.alice_token,
        )
        self.assertNotEqual(stale_edit.returncode, 0)
        self.assertIn("content hash mismatch", stale_edit.stderr)

        # Alice re-reads current hash, replaces body, posts a note from stdin, and completes the task.
        reread = json.loads(self.run_remote_mt("read", ref, "--json", token=self.alice_token).stdout)
        alice_edit = self.run_remote_mt(
            "edit",
            ref,
            "--body",
            "--file",
            str(alice_body),
            "--expected-hash",
            reread["content_hash"],
            "--json",
            token=self.alice_token,
        )
        self.assertEqual(alice_edit.returncode, 0, alice_edit.stderr)

        note_res = self.run_remote_mt(
            "note",
            "--title",
            "Lab sweep log",
            "--file",
            "-",
            "--json",
            token=self.alice_token,
            input_text="Logged calibration parameters for v2.\n",
        )
        self.assertEqual(note_res.returncode, 0, note_res.stderr)
        self.assertEqual(json.loads(note_res.stdout)["properties"]["kind"], "note")

        done_res = self.run_remote_mt("done", ref, "--comment", "Ready for review", "--json", token=self.alice_token)
        self.assertEqual(done_res.returncode, 0, done_res.stderr)
        done_receipt = json.loads(done_res.stdout)
        self.assertEqual(done_receipt["properties"]["status"], "done")
        self.assertEqual(done_receipt["properties"]["comments"][-1]["by"], "alice")

        # Bob sees the completed card and Alice's attribution in the dashboard.
        dash_status, dash_after = self.dashboard_get("/api/items?archived=exclude", email="bob@example.com")
        self.assertEqual(dash_status, 200)
        dash_done = next(item for item in dash_after["tasks"] if item["ref"] == ref)
        self.assertEqual(dash_done["status"], "done")
        detail_status, detail_payload = self.dashboard_get(f"/api/item?id={done_receipt['properties']['id']}", email="bob@example.com")
        self.assertEqual(detail_status, 200)
        self.assertIn("ECE=0.018", detail_payload["item"]["body"])
        self.assertEqual([c["by"] for c in detail_payload["item"]["comments"] if "by" in c], ["bob", "alice"])

        # Removing Bob from the members file immediately cuts off Bob's already-accepted token.
        self.members_file.write_text("alice@example.com\n", encoding="utf-8")
        denied = self.run_remote_mt("read", ref, "--json", token=self.bob_token)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("not a member", denied.stderr)

        # Revoking Alice's token also denies Alice immediately.
        web_hosted.revoke_tokens(self.tokens_file, email="alice@example.com")
        revoked = self.run_remote_mt("read", ref, "--json", token=self.alice_token)
        self.assertNotEqual(revoked.returncode, 0)
        self.assertIn("revoked", revoked.stderr)

        # Missing or unreadable members file also denies bearer access.
        self.members_file.unlink()
        missing_members = self.run_remote_mt("read", ref, "--json", token=self.alice_token)
        self.assertNotEqual(missing_members.returncode, 0)

    def test_fail_closed_when_server_stopped_or_update_invoked(self) -> None:
        # Build an isolated app copy with a local workspace/ to prove remote mode never falls back.
        local_app = self.base / "local-app"
        shutil.copytree(
            ROOT,
            local_app,
            ignore=shutil.ignore_patterns(".git", ".mt", ".DS_Store", "workspace", "__pycache__", ".playwright-mcp"),
        )
        local_init = subprocess.run(
            [str(local_app / "mt"), "init"],
            cwd=local_app,
            env=clean_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(local_init.returncode, 0, local_init.stderr)
        (local_app / "VERSION").write_text("v0.4.3\n", encoding="utf-8")

        def snapshot(folder: Path) -> dict[str, bytes]:
            return {
                str(p.relative_to(folder)): p.read_bytes()
                for p in sorted(folder.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts
            }

        before = snapshot(local_app)
        self.stop_server()

        # Both `mt update` and an ordinary write with server stopped must fail visibly.
        upd = self.run_remote_mt("update", token=self.alice_token, cwd=local_app, mt_bin=local_app / "mt")
        self.assertNotEqual(upd.returncode, 0)
        self.assertIn("remote mode", upd.stderr)

        write_res = self.run_remote_mt(
            "add",
            "Should not land locally",
            token=self.alice_token,
            cwd=local_app,
            mt_bin=local_app / "mt",
        )
        self.assertNotEqual(write_res.returncode, 0)
        self.assertIn("cannot reach remote workspace", write_res.stderr)

        # Neither local installation nor local workspace was touched.
        self.assertEqual(snapshot(local_app), before)

        # Local -R PATH still works even when MT_REMOTE is set in the environment.
        local_list = self.run_remote_mt(
            "-R",
            str(local_app),
            "list",
            "--json",
            token=self.alice_token,
            cwd=self.base,
            mt_bin=local_app / "mt",
        )
        self.assertEqual(local_list.returncode, 0, local_list.stderr)
        self.assertEqual(json.loads(local_list.stdout), [])

    def test_token_mode_preservation_input_validation_and_default_doing_claim(self) -> None:
        # Token store preserves group-readable 0640 mode across issue and revoke.
        self.tokens_file.chmod(0o640)
        extra_token, meta = web_hosted.issue_token(
            self.tokens_file, "alice@example.com", label="ci", members_path=self.members_file,
        )
        self.assertEqual(stat.S_IMODE(self.tokens_file.stat().st_mode), 0o640)
        web_hosted.revoke_tokens(self.tokens_file, token_id=str(meta["id"]))
        self.assertEqual(stat.S_IMODE(self.tokens_file.stat().st_mode), 0o640)
        self.assertTrue(extra_token.startswith("mt_"))

        # Malformed non-string JSON fields return 400 instead of 500.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = json.dumps({"kind": "task", "text": "Bad priority", "priority": 123}).encode("utf-8")
            conn.request(
                "POST",
                "/api/v1/add",
                body=body,
                headers={
                    "Authorization": f"Bearer {self.alice_token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

        # When default_status is doing and --status is omitted, op_create claims for Alice.
        settings = self.workspace_dir / "settings.yaml"
        settings.write_text(
            settings.read_text(encoding="utf-8").replace("default_status: inbox", "default_status: doing"),
            encoding="utf-8",
        )
        added = self.run_remote_mt("add", "Auto-claimed task", "--json", token=self.alice_token)
        self.assertEqual(added.returncode, 0, added.stderr)
        receipt = json.loads(added.stdout)
        self.assertEqual(receipt["properties"]["status"], "doing")
        self.assertEqual(receipt["properties"]["claimed_by"], "alice")

    def test_metadata_discovery_settings_topics_and_focus(self) -> None:
        settings_res = self.run_remote_mt("settings", "--json", token=self.alice_token)
        self.assertEqual(settings_res.returncode, 0, settings_res.stderr)
        settings_payload = json.loads(settings_res.stdout)
        self.assertIn("statuses", settings_payload)
        self.assertIn("priorities", settings_payload)

        added = self.run_remote_mt(
            "add",
            "Read async cancellation notes",
            "--topic",
            "learning/rust",
            "--status",
            "active",
            "--json",
            token=self.alice_token,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        ref = json.loads(added.stdout)["ref"]

        topics_res = self.run_remote_mt("topic", "list", "--json", token=self.bob_token)
        self.assertEqual(topics_res.returncode, 0, topics_res.stderr)
        topic_paths = [node["path"] for node in json.loads(topics_res.stdout)["topics"]]
        self.assertIn("learning", topic_paths)
        self.assertIn("learning/rust", topic_paths)

        focus_res = self.run_remote_mt("focus", "--json", token=self.bob_token)
        self.assertEqual(focus_res.returncode, 0, focus_res.stderr)
        focus_payload = json.loads(focus_res.stdout)
        self.assertEqual(focus_payload["selected"]["ref"], ref)

        bad_focus = self.run_remote_mt("focus", "--clear", token=self.bob_token)
        self.assertEqual(bad_focus.returncode, 2)
        self.assertIn("read-only", bad_focus.stderr)


if __name__ == "__main__":
    unittest.main()
