"""Focus writes, the kept sync error, the Host allowlist and timers through body edits."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tools"))

import card_format  # noqa: E402
import cli_admin  # noqa: E402
import cli_helpers  # noqa: E402
import cli_cards  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import taskweb  # noqa: E402
import transaction  # noqa: E402
import web_actions  # noqa: E402
from workspace import Workspace  # noqa: E402


class RobustnessTests(unittest.TestCase):
    """Run against a data-only workspace, like a hosted one."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "data"
        (self.root / "cards").mkdir(parents=True)
        policy = (SOURCE_ROOT / "mt-policy.yaml").read_text(encoding="utf-8")
        (self.root / "mt-policy.yaml").write_text(
            policy.replace("demo_workspace: true", "demo_workspace: false").replace("auto_sync: true", "auto_sync: false"),
            encoding="utf-8")
        (self.root / ".gitignore").write_text(".mt/\n", encoding="utf-8")  # As `mt` creates for data workspaces.
        self.ws = Workspace.at(self.root, app_root=SOURCE_ROOT, environ={**os.environ, "MT_USERNAME": "tester"})

    def git(self, *arguments: str, cwd: Path | None = None) -> None:
        result = subprocess.run(["git", *arguments], cwd=cwd or self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def init_repository(self) -> None:
        for arguments in (("init", "-q", "-b", "main"), ("config", "user.name", "Test"),
                          ("config", "user.email", "test@example.invalid"), ("add", "."), ("commit", "-qm", "Initial")):
            self.git(*arguments)

    def add_origin(self) -> None:
        remote = self.base / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(remote), cwd=self.base)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "-u", "origin", "main")

    def sync(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return transaction.command_sync(argparse.Namespace(message=None), ws=self.ws)

    def info(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli_admin.command_info(argparse.Namespace(), ws=self.ws), 0)
        return out.getvalue()

    # Atomic focus write

    def test_focus_write_replaces_the_file_atomically(self) -> None:
        store.write_focus_state("old-task", "pinned", self.ws)
        path = store.focus_state_path(self.ws)
        before = path.read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.write_focus_state("new-task", "pinned", self.ws)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([entry.name for entry in path.parent.iterdir()], ["focus"])
        store.write_focus_state("new-task", "pinned", self.ws)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["task_id"], "new-task")

    # Kept sync error

    def test_a_failed_sync_is_kept_shown_by_info_and_cleared_by_a_later_success(self) -> None:
        self.init_repository()  # No origin yet, so sync fails.
        self.assertNotIn("LAST SYNC FAILED", self.info())
        self.assertEqual(self.sync(), 1)
        self.assertIn("origin", transaction.read_sync_error(self.ws)[1])
        self.assertTrue((self.root / ".mt" / "last-sync-error").exists())
        info = self.info()
        self.assertIn("LAST SYNC FAILED", info)
        self.assertIn("origin", info)

        self.add_origin()
        code = self.sync()
        self.assertEqual(code, 0, transaction.read_sync_error(self.ws))
        self.assertFalse((self.root / ".mt" / "last-sync-error").exists())
        self.assertNotIn("LAST SYNC FAILED", self.info())

    def test_a_restarted_dashboard_reports_the_stored_sync_warning(self) -> None:
        self.init_repository()
        self.assertEqual(self.sync(), 1)
        stored = transaction.read_sync_error(self.ws)[1]
        self.assertTrue(stored)
        server = taskweb.TaskWebServer(("127.0.0.1", 0), "test-token", self.ws)
        self.addCleanup(server.server_close)
        self.assertEqual(server.sync.warning, stored)
        self.assertEqual(web_actions.sync_once(self.ws), stored)

    def test_a_workspace_without_git_keeps_the_unavailable_message_for_the_dashboard(self) -> None:
        message = web_actions.sync_once(self.ws)
        self.assertIn("Git sync is unavailable", message)
        self.assertEqual(transaction.read_sync_error(self.ws)[1], message)

    # Host allowlist

    def serve(self, host: str, allow_hosts: tuple = ()) -> tuple[taskweb.TaskWebServer, int]:
        server = taskweb.TaskWebServer((host, 0), "test-token", self.ws, allow_hosts)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, server.server_address[1]

    def request(self, port: int, host_header: str, method: str = "GET", path: str = "/api/items") -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host_header)
            connection.putheader("X-Microtask-Token", "test-token")
            connection.putheader("Content-Length", "2")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body.startswith(b"{") else {}
        finally:
            connection.close()

    def test_unknown_hosts_are_refused_on_every_route_and_method(self) -> None:
        _, port = self.serve("127.0.0.1")
        for header in (f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", ""):
            for method, path in (("GET", "/"), ("GET", "/api/items"), ("GET", "/vendor/alpine.js"), ("POST", "/api/move")):
                with self.subTest(header=header, method=method, path=path):
                    status, payload = self.request(port, header, method, path)
                    self.assertEqual(status, 421)
                    self.assertIn("error", payload)

    def test_loopback_names_are_accepted_with_the_port(self) -> None:
        _, port = self.serve("127.0.0.1")
        for header in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}", f"[::1]:{port}"):
            with self.subTest(header=header):
                self.assertEqual(self.request(port, header)[0], 200)

    def test_a_server_bound_to_another_address_accepts_that_address(self) -> None:
        # Any 127.x.y.z address is local on Linux, but macOS only has 127.0.0.1, so use that as "the bound host".
        server, port = self.serve("127.0.0.1")
        server.requested_host = "100.64.0.9"
        server.server_address = ("100.64.0.9", port)
        self.assertEqual(self.request(port, f"100.64.0.9:{port}")[0], 200)
        self.assertEqual(self.request(port, f"100.64.0.10:{port}")[0], 421)
        self.assertEqual(self.request(port, f"tailnet.example:{port}")[0], 421)

    def test_a_wildcard_server_accepts_ip_addresses_but_not_names(self) -> None:
        _, port = self.serve("0.0.0.0")
        self.assertEqual(self.request(port, f"192.168.1.20:{port}")[0], 200)
        self.assertEqual(self.request(port, f"[fd7a::1]:{port}")[0], 200)
        self.assertEqual(self.request(port, f"evil.example:{port}")[0], 421)
        self.assertEqual(self.request(port, "192.168.1.20:1")[0], 421)

    def test_allowed_host_names_match_any_port_and_case(self) -> None:
        _, port = self.serve("127.0.0.1", {"tasks.example.com"})
        for header in ("tasks.example.com", "tasks.example.com:443", "TASKS.example.com:8443", f"tasks.example.com:{port}"):
            with self.subTest(header=header):
                self.assertEqual(self.request(port, header)[0], 200)
        for header in (f"evil.example:{port}", "evil.example", "sub.tasks.example.com", f"tasks.example.com.evil.example:{port}"):
            with self.subTest(header=header):
                self.assertEqual(self.request(port, header)[0], 421)
        self.assertEqual(self.request(port, f"127.0.0.1:{port}")[0], 200)

    def test_an_allowed_host_with_a_port_accepts_only_that_port(self) -> None:
        _, port = self.serve("127.0.0.1", {"tasks.example.com:8443"})
        self.assertEqual(self.request(port, "Tasks.example.com:8443")[0], 200)
        for header in ("tasks.example.com", "tasks.example.com:443", f"tasks.example.com:{port}"):
            with self.subTest(header=header):
                self.assertEqual(self.request(port, header)[0], 421)

    def test_allow_host_values_are_validated_and_forwarded_by_mt_web(self) -> None:
        parser = argparse.ArgumentParser()
        cli_helpers.add_web_arguments(parser)
        args = parser.parse_args(["--allow-host", "Tasks.Example.com", "--allow-host", "other.example:8443"])
        self.assertEqual(args.allow_host, ["tasks.example.com", "other.example:8443"])
        self.assertEqual(parser.parse_args([]).allow_host, [])
        for value in ("", "a/b", "a b", "*", "*.example.com", "example.com:", "example.com:x", "example.com:0"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["--allow-host", value])
        launched = []
        with mock.patch.object(cli_admin.os, "execve", lambda path, command, env: launched.append(command)), \
                mock.patch.object(cli_admin.os, "chdir"):
            self.assertEqual(cli_admin.command_web(args, ws=self.ws), 0)
        command = launched[0]
        self.assertEqual([command[i + 1] for i, item in enumerate(command) if item == "--allow-host"],
                         ["tasks.example.com", "other.example:8443"])

    # Shutdown

    @staticmethod
    def kill_group(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def start_dashboard_process(self, *command: str) -> tuple[subprocess.Popen, str]:
        environment = {**os.environ, "MT_WORKSPACE": str(self.root), "MT_USERNAME": "tester"}
        # Its own process group, so cleanup also stops any child it left behind.
        process = subprocess.Popen([sys.executable, *command], cwd=self.root, env=environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.kill_group, process)
        self.addCleanup(process.wait)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        url = process.stdout.readline().strip()
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)
        return process, url

    def test_sigterm_runs_the_same_cleanup_as_ctrl_c(self) -> None:
        marker = self.base / "waited"
        source = f"""
import sys
sys.path.insert(0, {str(SOURCE_ROOT / "tools")!r})
sys.path.insert(0, {str(SOURCE_ROOT / "scripts")!r})
import taskweb, web_actions
from pathlib import Path
def wait(self, timeout=None):
    Path({str(marker)!r}).write_text("waited")
web_actions.BackgroundSync.wait = wait
raise SystemExit(taskweb.serve_dashboard(taskweb.Workspace.resolve(), "127.0.0.1", 0, no_open=True))
"""
        process, _ = self.start_dashboard_process("-c", source)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 0, process.stderr.read())
        self.assertTrue(marker.exists())

    def test_the_dashboard_script_exits_cleanly_on_sigterm(self) -> None:
        process, _ = self.start_dashboard_process(str(SOURCE_ROOT / "tools" / "taskweb.py"), "--no-open", "--port", "0")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 0, process.stderr.read())

    def test_mt_web_is_the_dashboard_process_so_sigterm_reaches_it(self) -> None:
        # `mt web` replaces itself with the dashboard, so a supervisor's SIGTERM (sent to the one
        # process it started) reaches the server instead of an `mt` wrapper that would leave it running.
        process, _ = self.start_dashboard_process(str(SOURCE_ROOT / "scripts" / "mt_main.py"), "web", "--no-open", "--port", "0")
        process.send_signal(signal.SIGTERM)
        # No stderr read here: a dashboard orphaned by the old wrapper would keep the pipe open.
        self.assertEqual(process.wait(timeout=10), 0)

    # Timers through body edits

    def test_replacing_a_body_keeps_the_running_timer_and_time_entries(self) -> None:
        card, _ = operations.create_card("task", "Timed body", title="Timed", ws=self.ws)
        ref = f"@{card.short_ref}"
        operations.op_time_start(self.ws, ref, sync=False)
        operations.op_time_end(self.ws, query=ref, sync=False)
        operations.op_time_start(self.ws, ref, sync=False)
        before = card_format.parse_document(card.path.read_text(encoding="utf-8"))[0]
        self.assertIn("timer", before)
        self.assertEqual(len(before["time_entries"]), 1)

        # The body may even look like a whole document with no timer.
        replacement = "---\nid: other\ntitle: Other\n---\nNew body\n"
        digest = hashlib.sha256(card.path.read_bytes()).hexdigest()
        args = argparse.Namespace(query=[ref], match_mode="exact", expected_hash=digest, body=True, old=None, new=None,
                                  replace_file=None, append=False, file_text=replacement, as_json=False,
                                  tags=None, assignments=[], tag_add=None, tag_remove=None, by=None, title=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_cards.command_edit(args, ws=self.ws), 0)
        after = card_format.parse_document(card.path.read_text(encoding="utf-8"))[0]
        self.assertEqual(after["timer"], before["timer"])
        self.assertEqual(after["time_entries"], before["time_entries"])
        self.assertEqual(after["id"], before["id"])

        status, _ = web_actions.handle_card_edit(
            self.ws, {"ref": ref}, {"expected_hash": hashlib.sha256(card.path.read_bytes()).hexdigest(), "body": "Again\n"}, "t")
        self.assertEqual(status, 200)
        after = card_format.parse_document(card.path.read_text(encoding="utf-8"))[0]
        self.assertEqual(after["timer"], before["timer"])


if __name__ == "__main__":
    unittest.main()
