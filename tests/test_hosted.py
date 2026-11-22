"""One dashboard serving a shared workspace: identity, per-person state, limits, admin rules and history."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from tools import taskweb  # noqa: E402
from tools import web_actions  # noqa: E402
from tools import web_hosted  # noqa: E402
import card_format  # noqa: E402
import history  # noqa: E402
import transaction  # noqa: E402
import validation  # noqa: E402
from workspace import Workspace  # noqa: E402

SECRET = "test-proxy-secret-0123456789"
TOKEN = "test-token"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
    return {**env, **GIT_ENV}


class SharedWorkspaceCase(unittest.TestCase):
    admins = frozenset({"root"})
    user_map: dict[str, str] = {}
    scope = ""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "shared"
        init = subprocess.run([str(ROOT / "mt"), "init", "--data", str(self.root)], env=clean_env(),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(init.returncode, 0, init.stderr)
        if self.scope:
            settings = self.root / "settings.yaml"
            settings.write_text(settings.read_text().replace(
                "  max_doing_tasks: 5\n", f"  max_doing_tasks: 5\n  doing_limit_scope: {self.scope}\n"))
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ws = taskweb.Workspace.at(self.root)

    def mt(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(ROOT / "mt"), "-R", str(self.root), *args], env=clean_env(), capture_output=True,
                              text=True, input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                              check=False)

    def add_task(self, title: str, *, doing_by: str | None = None) -> dict:
        added = self.mt("add", title, "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        card = json.loads(added.stdout)
        if doing_by:
            moved = self.mt("move", title, "doing", "--by", doing_by)
            self.assertEqual(moved.returncode, 0, moved.stderr)
        return card

    def start(self, hosted: bool = True, *, sign_out_url: str = "", token: str = TOKEN) -> int:
        """Serve the workspace and send later requests to it; returns the port."""
        self.hosted_server = hosted
        config = web_hosted.HostedConfig(SECRET, self.admins, self.user_map, sign_out_url=sign_out_url) if hosted else None
        server = taskweb.TaskWebServer(("127.0.0.1", 0), token, self.ws, hosted=config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        return self.port

    def token_for(self, email: str | None) -> str:
        """The write token the page of this person receives; the process token in a single-user dashboard."""
        if email is None or not self.hosted_server:
            return TOKEN
        user = self.user_map.get(email) or email.split("@", 1)[0]
        return web_hosted.member_token(TOKEN, user)

    def request(self, method: str, path: str, body: dict | None = None, *, email: str | None = None,
                secret: str | None = SECRET, extra: list[tuple[str, str]] = (), token: bool | str = True) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest(method, path)
            if secret is not None:
                connection.putheader(web_hosted.SECRET_HEADER, secret)
            if email is not None:
                connection.putheader(web_hosted.EMAIL_HEADER, email)
            for name, value in extra:
                connection.putheader(name, value)
            data = json.dumps(body).encode() if body is not None else None
            if method == "POST":
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(data or b"")))
                if token is True:
                    connection.putheader("X-Microtask-Token", self.token_for(email))
                elif token:
                    connection.putheader("X-Microtask-Token", token)
            connection.endheaders(data)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
        finally:
            connection.close()

    def get(self, path: str, user: str, **kwargs) -> tuple[int, dict]:
        return self.request("GET", path, email=f"{user}@example.org", **kwargs)

    def post(self, path: str, user: str, body: dict, **kwargs) -> tuple[int, dict]:
        return self.request("POST", path, body, email=f"{user}@example.org", **kwargs)

    def upload(self, user: str, token: str | None = None) -> tuple[int, dict]:
        resolved_token = web_hosted.member_token(TOKEN, user) if token is None else token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("POST", "/api/attachments")
            connection.putheader(web_hosted.SECRET_HEADER, SECRET)
            connection.putheader(web_hosted.EMAIL_HEADER, f"{user}@example.org")
            connection.putheader("X-Microtask-Token", resolved_token)
            connection.putheader("X-Filename", "note.txt")
            connection.putheader("Content-Type", "text/plain")
            connection.putheader("Content-Length", "5")
            connection.endheaders(b"hello")
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
        finally:
            connection.close()

    def items(self, user: str) -> dict:
        status, payload = self.get("/api/items", user)
        self.assertEqual(status, 200, payload)
        return payload

    def card_path(self, card: dict) -> Path:
        return self.root / card["path"]

    def draft(self, user: str, title: str = "Idea") -> dict:
        status, payload = self.post("/api/draft-save", user, {"kind": "note", "title": title, "body": "text",
                                                              "capture_id": str(uuid.uuid4()), "created": "2026-01-01T10:00:00"})
        self.assertEqual(status, 200, payload)
        return payload["saved_draft"]

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True)
        return result.stdout


class IdentityTests(SharedWorkspaceCase):
    def test_requests_need_the_proxy_secret_and_one_identity(self) -> None:
        self.start()
        email = "alice@example.org"
        self.assertEqual(self.request("GET", "/api/items", email=email)[0], 200)
        for label, kwargs in {
            "no secret": {"email": email, "secret": None},
            "wrong secret": {"email": email, "secret": SECRET[:-1] + "x"},
            "no identity": {"email": None},
            "empty identity": {"email": ""},
            "two identity headers": {"email": email, "extra": [(web_hosted.EMAIL_HEADER, "bob@example.org")]},
            "two secret headers": {"email": email, "extra": [(web_hosted.SECRET_HEADER, SECRET)]},
            "list of emails": {"email": "alice@example.org, bob@example.org"},
            "not an email": {"email": "alice"},
        }.items():
            with self.subTest(label):
                status, payload = self.request("GET", "/api/items", **kwargs)
                self.assertIn(status, (401, 403), payload)
        # Writes need the same proof, not only the dashboard's cross-site token.
        status, _ = self.request("POST", "/api/done", {"ref": "@00000000"}, email=None)
        self.assertIn(status, (401, 403))
        status, _ = self.request("POST", "/api/done", {"ref": "@00000000"}, email=email, secret="wrong-secret-value")
        self.assertEqual(status, 403)
        # The page, vendored files and attachments are behind the same check.
        for path in ("/", "/vendor/alpine.min.js", "/attachments/x.png", "/api/drafts"):
            self.assertIn(self.request("GET", path, secret=None, email=email)[0], (401, 403), path)

    def test_email_becomes_a_safe_user_name(self) -> None:
        config = web_hosted.HostedConfig(SECRET, frozenset({"carol"}), {"c.arol+tag@example.org": "carol"})

        def identify(*emails: str):
            message = http.client.HTTPMessage()
            message[web_hosted.SECRET_HEADER] = SECRET
            for email in emails:
                message[web_hosted.EMAIL_HEADER] = email
            return config.identify(message)

        self.assertEqual(identify("Alice@Example.org").user, "alice")
        self.assertTrue(identify("c.arol+tag@example.org").admin)
        for bad in ("a+b@example.org", "../x@example.org", "Ünal@example.org", ".dot@example.org"):
            with self.subTest(bad), self.assertRaises(web_hosted.HostedAuthError):
                identify(bad)
        with self.assertRaises(web_hosted.HostedAuthError):
            identify("a@example.org", "b@example.org")

    def test_user_map_file_and_admin_list_are_validated(self) -> None:
        path = Path(self.temporary.name) / "users.txt"
        path.write_text("# members\nAlice.Smith@example.org alice\n\nbob@example.org bob  # comment\n")
        self.assertEqual(web_hosted.read_user_map(str(path)),
                         {"alice.smith@example.org": "alice", "bob@example.org": "bob"})
        for text in ("alice@example.org Alice\n", "alice@example.org ../x\n", "not-an-email alice\n",
                     "a@example.org a\na@example.org b\n", "a@example.org\n"):
            path.write_text(text)
            with self.subTest(text), self.assertRaises(ValueError):
                web_hosted.read_user_map(str(path))
        self.assertEqual(web_hosted.parse_admins("alice,bob", None, "carol"), {"alice", "bob", "carol"})
        with self.assertRaises(ValueError):
            web_hosted.parse_admins("../root")
        with self.assertRaises(ValueError):
            web_hosted.read_secret(value="short")
        self.assertEqual(web_hosted.read_sign_out_url(None), "")
        self.assertEqual(web_hosted.read_sign_out_url("  /oauth2/sign_out?rd=/  "), "/oauth2/sign_out?rd=/")
        self.assertEqual(web_hosted.read_sign_out_url("https://auth.example.org/logout"), "https://auth.example.org/logout")
        for bad in ("javascript:alert(1)", "//evil.example/out", "http://example.org/out", "oauth2/sign_out", "/out\nnext"):
            with self.subTest(bad), self.assertRaises(ValueError):
                web_hosted.read_sign_out_url(bad)

    def test_user_map_with_windows_line_endings_has_clean_names(self) -> None:
        path = Path(self.temporary.name) / "crlf.txt"
        path.write_bytes(b"# members\r\nalice@example.org alice\r\nbob@example.org bob # note\r\n\r\ncarol@example.org carol\r")
        self.assertEqual(web_hosted.read_user_map(str(path)),
                         {"alice@example.org": "alice", "bob@example.org": "bob", "carol@example.org": "carol"})

    def test_command_line_entry_prints_the_names_the_dashboard_uses(self) -> None:
        path = Path(self.temporary.name) / "users.txt"
        path.write_bytes(b"alice@example.org boss\r\n")

        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(ROOT / "tools" / "web_hosted.py"), *args],
                                  capture_output=True, text=True, check=False)

        result = run("user-names", "--map", str(path), "Alice@Example.org", "bob@example.org")
        self.assertEqual((result.returncode, result.stdout), (0, "alice@example.org boss\nbob@example.org bob\n"), result.stderr)
        self.assertEqual(run("user-names", "carol@example.org").stdout, "carol@example.org carol\n")
        self.assertNotEqual(run("user-names", "Bad Name@example.org").returncode, 0)
        self.assertNotEqual(run("user-names", "Carol.Smith+x@example.org").returncode, 0)
        self.assertIn("cannot read", run("user-names", "--map", str(path) + ".missing", "a@example.org").stderr)
        path.write_text("alice@example.org\n")
        self.assertIn("EMAIL USER", run("user-names", "--map", str(path), "a@example.org").stderr)
        for values in (("alice, bob",), ("alice bob",), (" alice ,, bob ", "bob"), ("alice,bob",)):
            with self.subTest(values):
                self.assertEqual(run("admins", *values).stdout.split(), ["alice", "bob"])
        self.assertNotEqual(run("admins", "../root").returncode, 0)
        self.assertEqual(run("admins", "").stdout, "")

    def test_single_user_dashboard_is_unchanged(self) -> None:
        self.start(hosted=False)
        status, payload = self.request("GET", "/api/items", secret=None)
        self.assertEqual(status, 200)
        self.assertNotIn("hosted", payload["meta"])
        card = self.add_task("Plain task")
        for path in ("/api/delete", "/api/takeover", "/api/topic-rename", "/api/focus"):
            status, _ = self.request("POST", path, {"ref": card["ref"]}, secret=None)
            self.assertEqual(status, 404, path)
        status, _ = self.request("POST", "/api/comment", {"ref": card["ref"], "message": "note"}, secret=None)
        self.assertEqual(status, 200)
        comments = card_format.parse_document(self.card_path(card).read_text())[0]["comments"]
        self.assertEqual(comments[0].keys(), {"at", "text"})
        self.assertFalse((self.root / ".mt" / "focus-users").exists())


class BrowserKeyTests(SharedWorkspaceCase):
    def page(self, user: str | None) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("GET", "/")
            if user is not None:
                connection.putheader(web_hosted.SECRET_HEADER, SECRET)
                connection.putheader(web_hosted.EMAIL_HEADER, f"{user}@example.org")
            connection.endheaders()
            return connection.getresponse().read().decode("utf-8")
        finally:
            connection.close()

    def test_each_person_gets_their_own_key_for_unsaved_edits(self) -> None:
        self.start()
        alice, bob = self.items("alice")["meta"]["workspace_key"], self.items("bob")["meta"]["workspace_key"]
        self.assertNotEqual(alice, bob)
        self.assertEqual(alice, self.items("alice")["meta"]["workspace_key"])
        # The page the browser loads first carries the same key as the payloads that follow.
        self.assertIn(alice, self.page("alice"))
        self.assertIn(bob, self.page("bob"))
        self.assertNotIn(bob, self.page("alice"))

    def test_hosted_identity_and_sign_out_url_reach_page_and_api(self) -> None:
        self.start(sign_out_url="/oauth2/sign_out")
        root_meta = self.items("root")["meta"]["hosted"]
        self.assertEqual(root_meta, {
            "user": "root",
            "email": "root@example.org",
            "admin": True,
            "sign_out_url": "/oauth2/sign_out",
        })
        alice_meta = self.items("alice")["meta"]["hosted"]
        self.assertEqual(alice_meta, {
            "user": "alice",
            "email": "alice@example.org",
            "admin": False,
            "sign_out_url": "/oauth2/sign_out",
        })
        page = self.page("root")
        self.assertIn('"email": "root@example.org"', page)
        self.assertIn('"sign_out_url": "/oauth2/sign_out"', page)
        self.assertIn('"workspace_name": "Shared workspace"', page)
        self.assertNotIn(str(self.root), page)
        self.assertEqual(self.items("root")["meta"]["workspace_name"], "Shared workspace")

    def test_the_single_user_key_is_unchanged(self) -> None:
        from tools import web_payloads
        old = hashlib.sha256(str(self.ws.root.resolve()).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(web_payloads.workspace_key(self.ws), old)
        self.start(hosted=False)
        self.assertEqual(self.request("GET", "/api/items")[1]["meta"]["workspace_key"], old)
        self.assertIn(old, self.page(None))


class MemberTokenTests(SharedWorkspaceCase):
    """A page keeps writing with the token it was loaded with, so the token names who it was loaded for."""

    def page_token(self, user: str | None) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("GET", "/")
            if user is not None:
                connection.putheader(web_hosted.SECRET_HEADER, SECRET)
                connection.putheader(web_hosted.EMAIL_HEADER, f"{user}@example.org")
            connection.endheaders()
            page = connection.getresponse().read().decode("utf-8")
        finally:
            connection.close()
        import re
        return re.search(r'"api_token":\s*"([^"]+)"', page).group(1)

    def stored_attachments(self) -> list[Path]:
        return [path for path in (self.root / "attachments").rglob("*") if path.is_file()]

    def test_the_page_and_the_payloads_carry_a_token_for_each_person(self) -> None:
        self.start()
        alice, bob = self.page_token("alice"), self.page_token("bob")
        self.assertNotEqual(alice, bob)
        self.assertNotEqual(alice, TOKEN)
        self.assertEqual(alice, self.page_token("alice"))
        self.assertEqual(alice, self.items("alice")["meta"]["api_token"])
        self.assertEqual(bob, self.items("bob")["meta"]["api_token"])

    def test_a_token_from_another_person_is_refused_before_anything_changes(self) -> None:
        card = self.add_task("Shared card")
        self.start()
        alice = self.page_token("alice")
        before = self.card_path(card).read_text()
        drafts_before = sorted(path.name for path in (self.root / "drafts").glob("*")) if (self.root / "drafts").exists() else []
        events_before = len(history.list_events(Workspace.at(self.root), limit=100))
        attempts = (
            ("/api/comment", {"ref": card["ref"], "message": "as bob?"}),
            ("/api/draft-save", {"kind": "note", "title": "Idea", "body": "text", "capture_id": str(uuid.uuid4()),
                                 "created": "2026-01-01T10:00:00"}),
        )
        for path, body in attempts:
            with self.subTest(path):
                status, payload = self.post(path, "bob", body, token=alice)
                self.assertEqual(status, 409, payload)
                self.assertTrue(payload["member_changed"])
                self.assertNotIn("conflict", payload)
        status, payload = self.upload("bob", alice)
        self.assertEqual((status, payload.get("member_changed")), (409, True))
        self.assertEqual(self.card_path(card).read_text(), before)
        after = sorted(path.name for path in (self.root / "drafts").glob("*")) if (self.root / "drafts").exists() else []
        self.assertEqual(after, drafts_before)
        self.assertEqual(self.stored_attachments(), [])
        self.assertEqual(len(history.list_events(Workspace.at(self.root), limit=100)), events_before)

    def test_a_person_writes_with_their_own_token(self) -> None:
        card = self.add_task("Shared card")
        self.start()
        alice = self.page_token("alice")
        status, payload = self.post("/api/comment", "alice", {"ref": card["ref"], "message": "mine"}, token=alice)
        self.assertEqual(status, 200, payload)
        status, payload = self.upload("alice", alice)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.stored_attachments()), 1)

    def test_the_process_token_is_not_accepted_in_a_shared_workspace(self) -> None:
        self.start()
        status, payload = self.post("/api/draft-save", "alice", {"kind": "note", "title": "x", "body": "y",
                                                                 "capture_id": str(uuid.uuid4()),
                                                                 "created": "2026-01-01T10:00:00"}, token=TOKEN)
        self.assertEqual((status, payload.get("member_changed")), (409, True))

    def test_the_single_user_token_is_the_process_token(self) -> None:
        self.start(hosted=False)
        self.assertEqual(self.items_single()["meta"]["api_token"], TOKEN)
        self.assertEqual(self.page_token(None), TOKEN)
        status, payload = self.request("POST", "/api/comment", {"ref": "@nothing", "message": "x"}, token=False)
        self.assertEqual((status, payload.get("error")), (403, "invalid token"))

    def items_single(self) -> dict:
        return self.request("GET", "/api/items")[1]

    def test_processes_with_the_same_secret_and_version_accept_each_others_pages(self) -> None:
        card = self.add_task("Shared card")
        shared = web_hosted.shared_process_token(SECRET, "version-1")
        self.start(token=shared)
        alice = self.page_token("alice")
        self.start(token=shared)
        self.assertEqual(self.page_token("alice"), alice)
        status, payload = self.post("/api/comment", "alice", {"ref": card["ref"], "message": "via another process"},
                                    token=alice)
        self.assertEqual(status, 200, payload)
        status, payload = self.upload("alice", alice)
        self.assertEqual(status, 200, payload)

    def test_a_page_from_another_version_or_secret_must_reload_before_writing(self) -> None:
        card = self.add_task("Shared card")
        self.start(token=web_hosted.shared_process_token(SECRET, "version-1"))
        alice = self.page_token("alice")
        for token in (web_hosted.shared_process_token(SECRET, "version-2"),
                      web_hosted.shared_process_token(SECRET + "-rotated", "version-1")):
            with self.subTest(token=token):
                self.start(token=token)
                before = self.card_path(card).read_text()
                status, payload = self.post("/api/comment", "alice", {"ref": card["ref"], "message": "stale"},
                                            token=alice)
                self.assertEqual((status, payload.get("member_changed")), (409, True), payload)
                self.assertEqual(self.card_path(card).read_text(), before)

    def test_serve_dashboard_derives_the_shared_token_and_keeps_a_random_one_for_one_person(self) -> None:
        tokens = []

        def refuse(address, token, *args, **kwargs):
            tokens.append(token)
            raise OSError("not started in this test")

        config = web_hosted.HostedConfig(SECRET)
        with mock.patch.object(taskweb, "TaskWebServer", side_effect=refuse), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            for hosted in (config, config, None, None):
                self.assertEqual(taskweb.serve_dashboard(self.ws, "127.0.0.1", 0, no_open=True, hosted=hosted), 1)
        expected = web_hosted.shared_process_token(SECRET, taskweb.application_version(taskweb.APP_ROOT))
        self.assertEqual(tokens[:2], [expected, expected])
        self.assertNotIn(expected, tokens[2:])
        self.assertNotEqual(tokens[2], tokens[3])


class PerPersonStateTests(SharedWorkspaceCase):
    def test_each_person_has_their_own_focus(self) -> None:
        first, second = self.add_task("First task"), self.add_task("Second task")
        self.start()
        self.assertEqual(self.post("/api/focus", "alice", {"ref": first["ref"]})[0], 200)
        self.assertEqual(self.post("/api/focus", "bob", {"ref": second["ref"]})[0], 200)
        self.assertEqual(self.items("alice")["meta"]["focus"]["selected"]["ref"], first["ref"])
        self.assertEqual(self.items("bob")["meta"]["focus"]["selected"]["ref"], second["ref"])
        self.assertEqual(self.items("alice")["meta"]["hosted"],
                         {"user": "alice", "email": "alice@example.org", "admin": False})
        self.assertEqual(sorted(path.name for path in (self.root / ".mt" / "focus-users").iterdir()), ["alice", "bob"])
        self.assertFalse((self.root / ".mt" / "focus").exists())
        self.assertEqual(self.post("/api/focus-clear", "alice", {})[0], 200)
        self.assertEqual(self.items("bob")["meta"]["focus"]["selected"]["ref"], second["ref"])

    def test_focus_only_offers_tasks_nobody_else_holds(self) -> None:
        self.add_task("Alice works on this", doing_by="alice")
        free = self.add_task("Free task")
        self.start()
        selected = self.items("bob")["meta"]["focus"]["candidates"]
        self.assertEqual([entry["ref"] for entry in selected], [free["ref"]])

    def test_drafts_belong_to_their_author(self) -> None:
        self.start()
        mine = self.draft("alice")
        self.assertEqual(mine["owner"], "alice")
        self.assertEqual([d["id"] for d in self.get("/api/drafts", "alice")[1]["drafts"]], [mine["id"]])
        self.assertEqual(self.get("/api/drafts", "bob")[1]["drafts"], [])
        self.assertEqual(self.items("bob")["meta"]["draft_count"], 0)
        self.assertEqual(self.items("alice")["meta"]["draft_count"], 1)
        # By ID, another person's draft is not found for any action.
        hashed = {"id": mine["id"], "expected_hash": mine["content_hash"]}
        for path, body in (
            ("/api/draft-save", {**hashed, "kind": "note", "title": "Taken", "body": "overwritten"}),
            ("/api/draft-discard", hashed),
            ("/api/draft-finish", hashed),
        ):
            with self.subTest(path):
                status, payload = self.post(path, "bob", body)
                self.assertEqual(status, 404, payload)
        status, payload = self.post("/api/draft-save", "bob", {"id": mine["id"], "kind": "note", "title": "x", "body": "y",
                                                            "capture_id": str(uuid.uuid4()), "created": "2026-01-01T10:00:00"})
        self.assertEqual(status, 404, payload)
        draft_file = self.root / "drafts" / f"{mine['id']}.md"
        self.assertIn("text", draft_file.read_text())
        self.assertNotIn("overwritten", draft_file.read_text())
        # The owner can still finish it, and the card is theirs to see.
        status, payload = self.post("/api/draft-finish", "alice", hashed)
        self.assertEqual(status, 200, payload)
        self.assertFalse(draft_file.exists())

    def test_drafts_without_an_owner_wait_for_an_administrator(self) -> None:
        created = self.mt("draft", "create", "note", "--title", "Old draft", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        draft = json.loads(created.stdout)
        self.assertNotIn("owner", draft)
        self.start()
        self.assertEqual(self.get("/api/drafts", "alice")[1]["drafts"], [])
        status, _ = self.post("/api/draft-discard", "alice", {"id": draft["id"], "expected_hash": draft["content_hash"]})
        self.assertEqual(status, 404)
        assigned = self.mt("draft", "assign", draft["id"], "alice")
        self.assertEqual(assigned.returncode, 0, assigned.stderr)
        self.assertEqual([d["id"] for d in self.get("/api/drafts", "alice")[1]["drafts"]], [draft["id"]])
        self.assertEqual(self.get("/api/drafts", "bob")[1]["drafts"], [])
        self.assertEqual(self.mt("validate").returncode, 0)
        self.assertNotEqual(self.mt("draft", "assign", draft["id"], "Not Safe").returncode, 0)
        self.assertIn("(owner alice)", self.mt("draft", "list").stdout)

    def test_a_card_made_from_a_draft_is_claimed_by_its_author_when_doing(self) -> None:
        self.start()
        status, payload = self.post("/api/draft-save", "alice", {"kind": "task", "status": "doing", "title": "Do it",
                                                                 "body": "text", "capture_id": str(uuid.uuid4()),
                                                                 "created": "2026-01-01T10:00:00"})
        self.assertEqual(status, 200, payload)
        saved = payload["saved_draft"]
        status, payload = self.post("/api/draft-finish", "alice", {"id": saved["id"], "expected_hash": saved["content_hash"]})
        self.assertEqual(status, 200, payload)
        ref = next(card["ref"] for card in payload["tasks"] if card["title"] == "Do it")
        card = next(card for card in payload["tasks"] if card["ref"] == ref)
        self.assertEqual(card["claimed_by"], "alice")


class CommentTests(SharedWorkspaceCase):
    def test_hosted_comments_name_their_author_and_old_ones_stay_valid(self) -> None:
        card = self.add_task("Discuss")
        self.assertEqual(self.mt("comment", "Discuss", "-m", "from the shell").returncode, 0)
        self.start()
        self.assertEqual(self.post("/api/comment", "alice", {"ref": card["ref"], "message": "hello"})[0], 200)
        status, payload = self.post("/api/done", "alice", {"ref": card["ref"], "comment": "finished"})
        self.assertEqual(status, 200, payload)
        comments = card_format.parse_document(self.card_path(card).read_text())[0]["comments"]
        by_text = {comment["text"]: comment for comment in comments}
        self.assertEqual(set(by_text["from the shell"]), {"at", "text"})
        self.assertEqual(by_text["hello"]["by"], "alice")
        self.assertEqual(by_text["finished"]["by"], "alice")
        self.assertEqual(self.mt("validate").returncode, 0)
        detail = self.get("/api/item?" + urlencode({"id": Path(card["path"]).stem}), "bob")[1]["item"]
        self.assertEqual([c.get("by") for c in detail["comments"] if c["text"] == "hello"], ["alice"])

    def test_comment_records_accept_only_at_text_and_by(self) -> None:
        good = {"comments": [{"at": "2026-01-01", "text": "a"}, {"at": "2026-01-01", "text": "b", "by": "alice"}]}
        self.assertEqual(card_format.validate_records(good), [])
        for bad in ({"at": "2026-01-01", "text": "a", "by": ""}, {"at": "2026-01-01", "text": "a", "who": "x"},
                    {"at": "2026-01-01", "by": "alice"}, {"at": "2026-01-01", "text": "a", "by": "x\ny"}):
            with self.subTest(bad):
                self.assertTrue(card_format.validate_records({"comments": [bad]}))

    def test_cli_comment_by_is_written_only_when_given(self) -> None:
        self.add_task("Plain")
        self.assertEqual(self.mt("comment", "Plain", "-m", "anonymous").returncode, 0)
        self.assertEqual(self.mt("comment", "Plain", "-m", "named", "--by", "dana").returncode, 0)
        text = next((self.root / "cards").glob("*.md")).read_text()
        comments = card_format.parse_document(text)[0]["comments"]
        self.assertEqual([set(comment) for comment in comments], [{"at", "text"}, {"at", "text", "by"}])


class DoingLimitTests(SharedWorkspaceCase):
    scope = "per_claimant"

    def test_each_person_may_work_up_to_the_limit(self) -> None:
        for number in range(6):
            self.add_task(f"Person {number} task", doing_by=f"user{number}")
        self.assertEqual(self.mt("validate").returncode, 0)
        for number in range(4):
            self.add_task(f"Extra {number}", doing_by="user0")
        self.assertEqual(self.mt("validate").returncode, 0)  # user0 holds five
        self.add_task("One too many")
        refused = self.mt("move", "One too many", "doing", "--by", "user0")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("doing task limit exceeded", refused.stderr)
        self.assertIn("user0", refused.stderr)

    def test_a_takeover_that_pushes_someone_over_the_limit_fails(self) -> None:
        for number in range(5):
            self.add_task(f"Own {number}", doing_by="user0")
        self.add_task("Other task", doing_by="user1")
        refused = self.mt("move", "Other task", "doing", "--by", "user0", "--takeover")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("doing task limit exceeded", refused.stderr)
        self.assertEqual(self.mt("validate").returncode, 0)
        self.mt("move", "Own 0", "waiting", "--by", "user0")
        allowed = self.mt("move", "Other task", "doing", "--by", "user0", "--takeover")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_the_audit_counts_the_same_way(self) -> None:
        for number in range(6):
            self.add_task(f"Task {number}", doing_by="user0" if number < 5 else "user1")
        policy = taskweb.store.policy(self.ws)
        self.assertEqual(validation.audit_workspace(self.root, policy).errors, [])
        # Reassign one task by editing the file, as a merge could: user0 now holds six.
        path = next(path for path in (self.root / "cards").glob("*task-5.md"))
        path.write_text(path.read_text().replace("claimed_by: user1", "claimed_by: user0"))
        errors = validation.audit_workspace(self.root, policy).errors
        self.assertEqual(len(errors), 1)
        self.assertIn("6 tasks claimed by user0", errors[0])
        self.assertNotEqual(self.mt("validate").returncode, 0)

    def test_unclaimed_doing_tasks_share_one_group(self) -> None:
        for number in range(6):
            card = self.add_task(f"Loose {number}")
            path = self.card_path(card)
            path.write_text(path.read_text().replace("status: inbox", "status: doing"))
        errors = validation.audit_workspace(self.root, taskweb.store.policy(self.ws)).errors
        self.assertEqual(len(errors), 1)
        self.assertIn("6 tasks with no claimant", errors[0])
        # One claimed task is a different group and is not affected.
        self.add_task("Claimed", doing_by="user0")
        self.assertEqual(len(validation.audit_workspace(self.root, taskweb.store.policy(self.ws)).errors), 1)

    def test_dashboard_counts_the_signed_in_persons_doing_tasks(self) -> None:
        self.add_task("Mine", doing_by="alice")
        self.add_task("Theirs", doing_by="bob")
        self.add_task("Theirs too", doing_by="bob")
        self.start()
        self.assertEqual(self.items("alice")["meta"]["doing_count"], 1)
        self.assertEqual(self.items("bob")["meta"]["doing_count"], 2)

    def test_setting_must_be_known(self) -> None:
        settings = self.root / "settings.yaml"
        settings.write_text(settings.read_text().replace("per_claimant", "per_team"))
        refused = self.mt("validate")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("doing_limit_scope", refused.stderr)


class WorkspaceScopeLimitTests(SharedWorkspaceCase):
    def test_the_default_scope_counts_everyone_together(self) -> None:
        for number in range(5):
            self.add_task(f"Task {number}", doing_by=f"user{number}")
        self.add_task("Sixth")
        refused = self.mt("move", "Sixth", "doing", "--by", "user5")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("doing task limit exceeded: 6 tasks, maximum 5", refused.stderr)


class AdminTests(SharedWorkspaceCase):
    def test_only_administrators_delete_take_over_and_rename_topics(self) -> None:
        card = self.add_task("Held by bob", doing_by="bob")
        topic = self.mt("add", "Topical", "--topic", "old/area", "--json")
        self.assertEqual(topic.returncode, 0, topic.stderr)
        self.start()
        detail = self.get("/api/items", "alice")[1]["tasks"]
        held = next(item for item in detail if item["ref"] == card["ref"])
        for path, body in (
            ("/api/delete", {"ref": card["ref"], "expected_hash": held["content_hash"]}),
            ("/api/takeover", {"ref": card["ref"]}),
            ("/api/topic-rename", {"old": "old/area", "new": "new/area"}),
        ):
            with self.subTest(path):
                status, payload = self.post(path, "alice", body)
                self.assertEqual(status, 403, payload)
                self.assertIn("administrator", payload["error"])
        self.assertEqual(self.card_path(card).exists(), True)
        # Anyone can still move a card of their own and be refused one that is held.
        status, payload = self.post("/api/move", "alice", {"ref": card["ref"], "status": "doing"})
        self.assertEqual(status, 409, payload)

        status, payload = self.post("/api/takeover", "root", {"ref": card["ref"]})
        self.assertEqual(status, 200, payload)
        self.assertIn("claimed_by: root", self.card_path(card).read_text())
        status, payload = self.post("/api/topic-rename", "root", {"old": "old/area", "new": "new/area"})
        self.assertEqual(status, 200, payload)
        self.assertIn("topic: new/area", next((self.root / "cards").glob("*topical*.md")).read_text())
        status, payload = self.post("/api/delete", "root", {"ref": card["ref"], "expected_hash": "0" * 64})
        self.assertEqual(status, 409, payload)
        fresh = next(item for item in self.items("root")["tasks"] if item["ref"] == card["ref"])
        status, payload = self.post("/api/delete", "root", {"ref": card["ref"], "expected_hash": fresh["content_hash"]})
        self.assertEqual(status, 200, payload)
        self.assertFalse(self.card_path(card).exists())

    def test_a_person_cannot_change_a_task_someone_else_holds(self) -> None:
        card = self.add_task("Bob's work", doing_by="bob")
        self.start()
        task = next(item for item in self.items("alice")["tasks"] if item["ref"] == card["ref"])
        for path, body in (
            ("/api/card-metadata", {"ref": card["ref"], "expected_hash": task["content_hash"], "patch": {"priority": "high"}}),
            ("/api/card-edit", {"ref": card["ref"], "expected_hash": task["content_hash"], "body": "mine now"}),
            ("/api/archive", {"ref": card["ref"], "archived": True}),
            ("/api/time-start", {"ref": card["ref"]}),
        ):
            with self.subTest(path):
                status, payload = self.post(path, "alice", body)
                self.assertEqual(status, 409, payload)
        self.assertNotIn("mine now", self.card_path(card).read_text())
        self.assertEqual(self.post("/api/card-metadata", "bob", {"ref": card["ref"], "expected_hash": task["content_hash"],
                                                                  "patch": {"priority": "high"}})[0], 200)


class HistoryAndConflictTests(SharedWorkspaceCase):
    def test_a_deleted_card_can_be_restored_from_local_history(self) -> None:
        # A card made from the shell is still saved in .mt/history.sqlite before it is deleted, even without Git.
        card = self.add_task("Keep a copy")
        original = self.card_path(card).read_text()
        shutil.rmtree(self.root / ".git")
        self.start()
        task = next(item for item in self.items("root")["tasks"] if item["ref"] == card["ref"])
        status, payload = self.post("/api/delete", "root", {"ref": card["ref"], "expected_hash": task["content_hash"]})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["sync_warning"], "")
        self.assertFalse(self.card_path(card).exists())
        listed = self.mt("history", "--deleted", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        events = json.loads(listed.stdout)
        delete_event = next(ev for ev in events if ev["action"] == "delete" and any(r["path"] == card["path"] for r in ev["revisions"]))
        restored = self.mt("restore", str(delete_event["id"]))
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.card_path(card).read_text(), original)

    def test_each_web_write_is_one_local_history_event(self) -> None:
        card = self.add_task("Watch history")
        self.start()
        ws = Workspace.at(self.root)
        before = len(history.list_events(ws, limit=100))
        self.assertEqual(self.post("/api/comment", "alice", {"ref": card["ref"], "message": "one"})[0], 200)
        self.assertEqual(self.post("/api/comment", "bob", {"ref": card["ref"], "message": "two"})[0], 200)
        events = history.list_events(ws, limit=100)
        self.assertEqual(len(events), before + 2)
        self.assertEqual([(events[0]["actor"], events[0]["source"], events[0]["action"]),
                          (events[1]["actor"], events[1]["source"], events[1]["action"])],
                         [("bob", "web", "comment"), ("alice", "web", "comment")])
        # Draft autosaves are not history events.
        self.draft("alice", "x")
        self.assertEqual(len(history.list_events(ws, limit=100)), before + 2)

    def test_a_write_and_its_history_record_happen_under_one_hold_of_the_lock(self) -> None:
        card = self.add_task("Shared lock")
        self.start()
        real_record = history.record_transaction
        held: list[bool] = []
        intruder: list[threading.Thread] = []
        results: list[subprocess.CompletedProcess] = []

        def record_with_rival(ws, ctx, original, changes, deletions):
            held.append(transaction._is_lock_held(ws))
            if not intruder:
                thread = threading.Thread(target=lambda: results.append(
                    self.mt("comment", "Shared lock", "-m", "from the rival", "--by", "bob")))
                thread.start()
                intruder.append(thread)
                thread.join(2)
                self.assertTrue(thread.is_alive(), "the rival write got in before history was recorded")
            return real_record(ws, ctx, original, changes, deletions)

        with mock.patch.object(history, "record_transaction", record_with_rival):
            self.assertEqual(self.post("/api/comment", "alice", {"ref": card["ref"], "message": "mine"})[0], 200)
        intruder[0].join(10)
        self.assertEqual(results[0].returncode, 0, results[0].stderr)
        self.assertEqual(held, [True])
        events = history.list_events(Workspace.at(self.root), limit=10)
        self.assertEqual([(ev["actor"], ev["action"]) for ev in events[:2]], [("bob", "comment"), ("alice", "comment")])
        self.assertIn("from the rival", self.card_path(card).read_text())

    def test_a_delete_and_its_snapshot_share_one_hold_of_the_lock(self) -> None:
        card = self.add_task("Snapshot lock")
        self.start()
        task = next(item for item in self.items("root")["tasks"] if item["ref"] == card["ref"])
        real = history.prepare_transaction
        held: list[bool] = []

        def prepare(ws, ctx, original, changes, deletions):
            held.append(transaction._is_lock_held(ws))
            return real(ws, ctx, original, changes, deletions)

        with mock.patch.object(history, "prepare_transaction", prepare):
            status, payload = self.post("/api/delete", "root", {"ref": card["ref"], "expected_hash": task["content_hash"]})
        self.assertEqual(status, 200, payload)
        self.assertEqual(held, [True])

    def test_a_history_warning_goes_only_to_the_person_whose_write_caused_it(self) -> None:
        card = self.add_task("Warn once")
        self.start()
        real = history.record_transaction
        calls: list[str] = []

        def fail_once(ws, ctx, original, changes, deletions):
            calls.append(ctx.actor)
            if len(calls) == 1:
                raise sqlite3.OperationalError("disk is full")
            return real(ws, ctx, original, changes, deletions)

        with mock.patch.object(history, "record_transaction", fail_once):
            status, payload = self.post("/api/comment", "alice", {"ref": card["ref"], "message": "one"})
            self.assertEqual(status, 200, payload)
            self.assertIn("disk is full", payload["sync_warning"])
            status, payload = self.post("/api/comment", "bob", {"ref": card["ref"], "message": "two"})
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["sync_warning"], "")
            self.assertEqual(self.items("bob").get("sync_warning", ""), "")

    def test_a_delete_is_refused_when_its_snapshot_fails(self) -> None:
        card = self.add_task("Unsaved copy")
        self.start()
        task = next(item for item in self.items("root")["tasks"] if item["ref"] == card["ref"])

        with mock.patch.object(history, "_open_db", side_effect=sqlite3.OperationalError("disk is full")):
            status, payload = self.post("/api/delete", "root", {"ref": card["ref"], "expected_hash": task["content_hash"]})
        self.assertEqual(status, 503, payload)
        self.assertIn("Nothing was changed", payload["error"])
        self.assertTrue(self.card_path(card).exists())

    def test_history_and_sync_warnings_are_both_reported(self) -> None:
        card = self.add_task("Two warnings")
        self.start()

        with mock.patch.object(history, "record_transaction", side_effect=sqlite3.OperationalError("disk is full")), \
                mock.patch.object(web_actions.BackgroundSync, "current_warning", return_value="Sync is behind"):
            status, payload = self.post("/api/comment", "alice", {"ref": card["ref"], "message": "one"})
        self.assertEqual(status, 200, payload)
        self.assertIn("Sync is behind", payload["sync_warning"])
        self.assertIn("disk is full", payload["sync_warning"])

    def test_shared_mode_works_without_git_installed_or_repo(self) -> None:
        card_a = self.add_task("Alpha")
        added_b = self.mt("add", "Beta", "--topic", "old-topic", "--json")
        self.assertEqual(added_b.returncode, 0, added_b.stderr)
        card_b = json.loads(added_b.stdout)
        shutil.rmtree(self.root / ".git")
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.start()
            status, payload = self.post("/api/comment", "alice", {"ref": card_a["ref"], "message": "hi"})
            self.assertEqual((status, payload["sync_warning"]), (200, ""), payload)
            status, payload = self.upload("alice")
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload.get("history_warning", ""), "")
            status, payload = self.post("/api/move", "alice", {"ref": card_a["ref"], "status": "doing"})
            self.assertEqual((status, payload["sync_warning"]), (200, ""), payload)
            status, payload = self.post("/api/takeover", "root", {"ref": card_a["ref"]})
            self.assertEqual((status, payload["sync_warning"]), (200, ""), payload)
            status, payload = self.post("/api/topic-rename", "root", {"old": "old-topic", "new": "new-topic"})
            self.assertEqual((status, payload["sync_warning"]), (200, ""), payload)
            task_b = next(item for item in self.items("root")["tasks"] if item["ref"] == card_b["ref"])
            status, payload = self.post("/api/delete", "root", {"ref": card_b["ref"], "expected_hash": task_b["content_hash"]})
            self.assertEqual((status, payload["sync_warning"]), (200, ""), payload)

    def test_stale_edits_return_409_and_change_nothing(self) -> None:
        card = self.add_task("Contested")
        self.start()
        task = next(item for item in self.items("alice")["tasks"] if item["ref"] == card["ref"])
        stale = task["content_hash"]
        self.assertEqual(self.post("/api/comment", "bob", {"ref": card["ref"], "message": "meanwhile"})[0], 200)
        status, payload = self.post("/api/card-edit", "alice", {"ref": card["ref"], "expected_hash": stale, "body": "late"})
        self.assertEqual((status, payload.get("conflict")), (409, True))
        status, payload = self.post("/api/card-metadata", "alice", {"ref": card["ref"], "expected_hash": stale,
                                                                    "patch": {"priority": "high"}})
        self.assertEqual((status, payload.get("conflict")), (409, True))
        text = self.card_path(card).read_text()
        self.assertNotIn("late", text)
        self.assertIn("priority: none", text)

    def test_incomplete_rollback_returns_500_for_delete_and_timer_start(self) -> None:
        card = self.add_task("Rollback 500")
        self.start()
        task = next(item for item in self.items("root")["tasks"] if item["ref"] == card["ref"])

        with mock.patch.object(transaction, "validate_data_root", side_effect=OSError("boom")), \
                mock.patch.object(transaction, "atomic_write", side_effect=OSError("rollback failed")):
            status, payload = self.post("/api/delete", "root", {"ref": card["ref"], "expected_hash": task["content_hash"]})
        self.assertEqual(status, 500, payload)
        self.assertIn("history event #", payload["error"])
        self.assertNotIn("conflict", payload)

        card2 = self.add_task("Timer Rollback 500")
        real_write = transaction.atomic_write
        calls = 0

        def fail_rollback_write(path: Path, content: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                real_write(path, content)
            else:
                raise OSError("rollback failed")

        with mock.patch.object(transaction, "validate_data_root", side_effect=OSError("boom")), \
                mock.patch.object(transaction, "atomic_write", side_effect=fail_rollback_write):
            status, payload = self.post("/api/time-start", "alice", {"ref": card2["ref"]})
        self.assertEqual(status, 500, payload)
        self.assertNotIn("conflict", payload)


class RequireProxySecretTests(SharedWorkspaceCase):
    """A single-user dashboard behind a proxy: --require-proxy-secret, with no shared-workspace behavior."""

    def start_required(self, secret: str | None = SECRET, sign_out_url: str = "") -> int:
        self.hosted_server = False
        server = taskweb.TaskWebServer(("127.0.0.1", 0), TOKEN, self.ws, require_secret=secret, sign_out_url=sign_out_url)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        return self.port

    def snapshot(self) -> dict[str, bytes]:
        """Every file in the workspace, so a refused request can be shown to have changed nothing."""
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in sorted(self.root.rglob("*")) if path.is_file() and ".git" not in path.parts
                and path.name != "history.sqlite-wal" and path.name != "history.sqlite-shm"}

    def history_events(self) -> int:
        database = self.root / ".mt" / "history.sqlite"
        if not database.exists():
            return 0
        connection = sqlite3.connect(database)
        try:
            return connection.execute("select count(*) from events").fetchone()[0]
        finally:
            connection.close()

    def upload_single(self, secret: str | None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("POST", "/api/attachments")
            if secret is not None:
                connection.putheader(web_hosted.SECRET_HEADER, secret)
            connection.putheader("X-Microtask-Token", TOKEN)
            connection.putheader("X-Filename", "note.txt")
            connection.putheader("Content-Type", "text/plain")
            connection.putheader("Content-Length", "5")
            connection.endheaders(b"hello")
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
        finally:
            connection.close()

    def raw_get(self, path: str, secret: str | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("GET", path)
            if secret is not None:
                connection.putheader(web_hosted.SECRET_HEADER, secret)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def bad_secrets(self) -> dict[str, dict]:
        return {
            "no secret": {"secret": None},
            "wrong secret": {"secret": SECRET[:-1] + "x"},
            "empty secret": {"secret": ""},
            "two secrets": {"extra": [(web_hosted.SECRET_HEADER, SECRET)]},
            "email header instead": {"secret": None, "email": "alice@example.org"},
        }

    def test_requests_without_the_secret_get_403_and_change_nothing(self) -> None:
        card = self.add_task("Plain task")
        self.start_required()
        before, events = self.snapshot(), self.history_events()
        for label, kwargs in self.bad_secrets().items():
            with self.subTest(label):
                for path in ("/", "/api/items", "/api/drafts", "/api/focus", "/vendor/alpinejs/dist/cdn.min.js",
                             "/attachments/x.png", "/nothing-here"):
                    self.assertEqual(self.request("GET", path, **kwargs)[0], 403, path)
                writes = {
                    "/api/comment": {"ref": card["ref"], "message": "note"},
                    "/api/done": {"ref": card["ref"]},
                    "/api/draft-save": {"kind": "note", "title": "Idea", "body": "text",
                                        "capture_id": str(uuid.uuid4()), "created": "2026-01-01T10:00:00"},
                }
                for path, body in writes.items():
                    # With the right dashboard token, so only the secret stands in the way.
                    status, payload = self.request("POST", path, body, **kwargs)
                    self.assertEqual((status, "error" in payload), (403, True), path)
                kwargs_upload = {"secret": kwargs.get("secret", SECRET)}
                if "extra" in kwargs:
                    continue
                self.assertEqual(self.upload_single(kwargs_upload["secret"])[0], 403)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.history_events(), events)
        self.assertFalse((self.root / "attachments").exists())
        self.assertFalse((self.root / ".mt" / "drafts").exists())

    def test_the_wrong_secret_is_refused_before_the_dashboard_token_is_looked_at(self) -> None:
        self.start_required()
        status, payload = self.request("POST", "/api/done", {"ref": "@00000000"}, secret="wrong", token="bad-token")
        self.assertEqual(status, 403)
        self.assertNotIn("member_changed", payload)

    def test_with_the_secret_it_is_the_ordinary_single_user_dashboard(self) -> None:
        card = self.add_task("Plain task")
        self.start_required()
        status, payload = self.request("GET", "/api/items")
        self.assertEqual(status, 200, payload)
        self.assertNotIn("hosted", payload["meta"])
        self.assertEqual(payload["meta"]["api_token"], TOKEN)
        status, page = self.raw_get("/", SECRET)
        self.assertEqual(status, 200)
        self.assertIn(TOKEN, page.decode("utf-8"))
        self.assertEqual(self.raw_get("/vendor/alpinejs/dist/cdn.min.js", SECRET)[0], 200)
        # The shared-workspace routes stay closed, as in any single-user dashboard.
        for path in ("/api/delete", "/api/takeover", "/api/topic-rename", "/api/focus"):
            self.assertEqual(self.request("POST", path, {"ref": card["ref"]})[0], 404, path)
        # An email header means nothing: the same single-user dashboard answers.
        self.assertEqual(self.request("GET", "/api/items", email="alice@example.org")[0], 200)
        status, payload = self.request("POST", "/api/comment", {"ref": card["ref"], "message": "note"})
        self.assertEqual(status, 200, payload)
        comments = card_format.parse_document(self.card_path(card).read_text())[0]["comments"]
        self.assertEqual(comments[0].keys(), {"at", "text"})
        status, payload = self.request("POST", "/api/draft-save", {
            "kind": "note", "title": "Idea", "body": "text", "capture_id": str(uuid.uuid4()),
            "created": "2026-01-01T10:00:00"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.request("GET", "/api/drafts")[0], 200)
        status, payload = self.upload_single(SECRET)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.request("POST", "/api/done", {"ref": card["ref"]}, token="wrong-token")[0], 403)
        self.assertFalse((self.root / ".mt" / "focus-users").exists())

    def test_the_secret_is_compared_with_the_stripped_value_only(self) -> None:
        self.start_required()
        self.assertEqual(self.request("GET", "/api/items", secret=f"  {SECRET}  ")[0], 200)
        self.assertEqual(self.request("GET", "/api/items", secret=SECRET + "x")[0], 403)
        self.assertEqual(self.request("GET", "/api/items", secret=SECRET[:-1])[0], 403)

    def test_without_the_flag_nothing_is_required(self) -> None:
        self.start(hosted=False)
        self.assertEqual(self.request("GET", "/api/items", secret=None)[0], 200)
        self.assertEqual(self.request("GET", "/api/items", secret="anything")[0], 200)
        self.assertEqual(self.raw_get("/")[0], 200)

    def parse(self, *argv: str, environ: dict[str, str] | None = None) -> str | None:
        return taskweb.required_secret(taskweb.parse_args(list(argv)), environ or {})

    def test_the_flag_reads_the_secret_like_trust_proxy_user_and_refuses_that_flag(self) -> None:
        path = Path(self.temporary.name) / "secret"
        path.write_text(SECRET + "\n")
        self.assertIsNone(self.parse())
        self.assertIsNone(self.parse("--proxy-secret-file", str(path)))
        self.assertEqual(self.parse("--require-proxy-secret", "--proxy-secret-file", str(path)), SECRET)
        self.assertEqual(self.parse("--require-proxy-secret", environ={"MT_PROXY_SECRET_FILE": str(path)}), SECRET)
        self.assertEqual(self.parse("--require-proxy-secret", environ={"MT_PROXY_SECRET": SECRET}), SECRET)
        for argv, environ in (
            (("--require-proxy-secret",), {}),
            (("--require-proxy-secret", "--proxy-secret-file", str(path) + ".missing"), {}),
            (("--require-proxy-secret",), {"MT_PROXY_SECRET": "short"}),
        ):
            with self.subTest(argv=argv, environ=environ):
                with self.assertRaises(ValueError) as caught:
                    self.parse(*argv, environ=environ)
                self.assertTrue(str(caught.exception))
        with self.assertRaises(ValueError) as missing:
            self.parse("--require-proxy-secret")
        self.assertIn("--require-proxy-secret needs the proxy secret", str(missing.exception))
        with self.assertRaises(ValueError) as combined:
            self.parse("--require-proxy-secret", "--trust-proxy-user", "--proxy-secret-file", str(path))
        self.assertIn("cannot be combined", str(combined.exception))

    def test_the_command_starts_only_with_a_usable_secret_and_not_with_trust_proxy_user(self) -> None:
        path = Path(self.temporary.name) / "secret"
        path.write_text(SECRET + "\n")
        for arguments, message in (
            (("--require-proxy-secret",), "needs the proxy secret"),
            (("--require-proxy-secret", "--trust-proxy-user", "--proxy-secret-file", str(path)), "cannot be combined"),
        ):
            with self.subTest(arguments=arguments):
                result = self.mt("web", "--no-open", "--port", "0", *arguments)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
        help_text = self.mt("web", "--help").stdout
        self.assertIn("--require-proxy-secret", help_text)
        self.assertIn("cannot be combined", " ".join(help_text.split()))

    def test_sign_out_url_reaches_the_single_user_page_and_api(self) -> None:
        self.start_required(sign_out_url="/oauth2/sign_out")
        meta = self.request("GET", "/api/items")[1]["meta"]
        self.assertEqual(meta["sign_out_url"], "/oauth2/sign_out")
        self.assertNotIn("hosted", meta)
        status, page = self.raw_get("/", SECRET)
        page = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('"sign_out_url": "/oauth2/sign_out"', page)
        self.assertIn('class="mt-account-signout" :href="(hosted && hosted.sign_out_url) || signOutUrl"', page)
        self.assertIn("'Account and display settings'", page)
        self.assertNotIn('"email"', page)
        card = self.add_task("Plain task")
        status, payload = self.request("POST", "/api/comment", {"ref": card["ref"], "message": "note"})
        self.assertEqual(payload["meta"]["sign_out_url"], "/oauth2/sign_out")
        self.assertEqual(self.request("GET", "/api/items", secret=None)[0], 403)

    def test_without_a_sign_out_url_the_single_user_page_has_no_link_setting(self) -> None:
        self.start_required()
        self.assertNotIn("sign_out_url", self.request("GET", "/api/items")[1]["meta"])
        self.assertNotIn('"sign_out_url"', self.raw_get("/", SECRET)[1].decode("utf-8"))

    def test_a_plain_dashboard_never_carries_a_sign_out_url(self) -> None:
        self.start(hosted=False)
        self.assertNotIn("sign_out_url", self.request("GET", "/api/items")[1]["meta"])
        self.assertNotIn('"sign_out_url"', self.raw_get("/")[1].decode("utf-8"))

    def sign_out(self, *argv: str, environ: dict[str, str] | None = None) -> str:
        return taskweb.single_user_sign_out_url(taskweb.parse_args(list(argv)), environ or {})

    def test_the_sign_out_url_comes_from_the_flag_or_the_variable_and_is_checked(self) -> None:
        self.assertEqual(self.sign_out("--require-proxy-secret", "--sign-out-url", "/oauth2/sign_out"), "/oauth2/sign_out")
        self.assertEqual(self.sign_out("--require-proxy-secret", environ={"MT_SIGN_OUT_URL": "https://auth.example.org/out"}),
                         "https://auth.example.org/out")
        self.assertEqual(self.sign_out("--require-proxy-secret", "--sign-out-url", "/a",
                                       environ={"MT_SIGN_OUT_URL": "/b"}), "/a")
        self.assertEqual(self.sign_out("--require-proxy-secret"), "")
        for bad in ("javascript:alert(1)", "//evil.example/out", "http://example.org/out", "out"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.sign_out("--require-proxy-secret", "--sign-out-url", bad)
                with self.assertRaises(ValueError):
                    self.sign_out("--require-proxy-secret", environ={"MT_SIGN_OUT_URL": bad})
        # Shared mode reads its own URL, and plain mode ignores the variable.
        self.assertEqual(self.sign_out("--trust-proxy-user", "--sign-out-url", "/x"), "")
        self.assertEqual(self.sign_out(environ={"MT_SIGN_OUT_URL": "/oauth2/sign_out"}), "")
        self.assertEqual(self.sign_out(environ={"MT_SIGN_OUT_URL": "javascript:bad"}), "")

    def test_the_command_refuses_a_bad_or_unused_sign_out_url_and_ignores_the_variable(self) -> None:
        path = Path(self.temporary.name) / "secret"
        path.write_text(SECRET + "\n")
        for arguments, message in (
            (("--sign-out-url", "/oauth2/sign_out"), "--sign-out-url needs --trust-proxy-user or --require-proxy-secret"),
            (("--require-proxy-secret", "--proxy-secret-file", str(path), "--sign-out-url", "javascript:alert(1)"),
             "sign-out URL must be"),
        ):
            with self.subTest(arguments=arguments):
                result = self.mt("web", "--no-open", "--port", "0", *arguments)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
        # The variable alone does not stop a plain dashboard from starting (it is only ignored).
        env = {**clean_env(), "MT_SIGN_OUT_URL": "/oauth2/sign_out"}
        process = subprocess.Popen([str(ROOT / "mt"), "-R", str(self.root), "web", "--no-open", "--port", "0"],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   stdin=subprocess.DEVNULL)
        try:
            self.assertTrue(process.stdout.readline().startswith("http://127.0.0.1:"))
        finally:
            process.terminate()
            process.communicate(timeout=30)
        help_text = " ".join(self.mt("web", "--help").stdout.split())
        self.assertIn("--require-proxy-secret", help_text[help_text.index("--sign-out-url"):])


class TailscaleTests(SharedWorkspaceCase):
    def start_tailscale(self, *, token: str = TOKEN) -> int:
        self.hosted_server = True
        config = web_hosted.HostedConfig(
            "",
            self.admins,
            self.user_map,
            email_header=web_hosted.TAILSCALE_EMAIL_HEADER,
            require_secret=False,
        )
        server = taskweb.TaskWebServer(("127.0.0.1", 0), token, self.ws, hosted=config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        return self.port

    def test_tailscale_identifies_users_without_proxy_secret(self) -> None:
        self.start_tailscale()
        # No proxy secret is needed, only Tailscale-User-Login.
        status, payload = self.request("GET", "/api/items", secret=None,
                                       extra=[(web_hosted.TAILSCALE_EMAIL_HEADER, "alice@example.org")])
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["hosted"]["user"], "alice")
        self.assertFalse(payload["meta"]["hosted"]["admin"])

        # Missing, duplicate, or invalid Tailscale-User-Login headers are rejected.
        self.assertEqual(self.request("GET", "/api/items", secret=None)[0], 401)
        self.assertEqual(
            self.request("GET", "/api/items", secret=None,
                         extra=[(web_hosted.TAILSCALE_EMAIL_HEADER, "alice@example.org"),
                                (web_hosted.TAILSCALE_EMAIL_HEADER, "bob@example.org")])[0],
            403,
        )
        self.assertEqual(
            self.request("GET", "/api/items", secret=None,
                         extra=[(web_hosted.TAILSCALE_EMAIL_HEADER, "not-an-email")])[0],
            403,
        )

        # Comments and admin actions follow the Tailscale user identity.
        card = self.add_task("Tailscale task")
        status, payload = self.request(
            "POST", "/api/comment", {"ref": card["ref"], "message": "from tailscale"},
            secret=None,
            extra=[(web_hosted.TAILSCALE_EMAIL_HEADER, "alice@example.org")],
            token=web_hosted.member_token(TOKEN, "alice"),
        )
        self.assertEqual(status, 200, payload)
        comments = card_format.parse_document(self.card_path(card).read_text())[0]["comments"]
        self.assertEqual(comments[-1]["by"], "alice")

        # Non-admin cannot delete; admin (root) can.
        fresh_hash = json.loads(self.mt("read", card["ref"], "--json").stdout)["content_hash"]
        self.assertEqual(
            self.request("POST", "/api/delete", {"ref": card["ref"], "expected_hash": fresh_hash}, secret=None,
                         extra=[(web_hosted.TAILSCALE_EMAIL_HEADER, "alice@example.org")],
                         token=web_hosted.member_token(TOKEN, "alice"))[0],
            403,
        )
        self.assertEqual(
            self.request("POST", "/api/delete", {"ref": card["ref"], "expected_hash": fresh_hash}, secret=None,
                         extra=[(web_hosted.TAILSCALE_EMAIL_HEADER, "root@example.org")],
                         token=web_hosted.member_token(TOKEN, "root"))[0],
            200,
        )

    def test_tailscale_cli_validation_and_startup(self) -> None:
        config = taskweb.hosted_config(taskweb.parse_args(["--trust-tailscale", "--admin", "alice"]), {})
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.email_header, web_hosted.TAILSCALE_EMAIL_HEADER)
        self.assertFalse(config.require_secret)
        self.assertEqual(config.admins, frozenset({"alice"}))

        path = Path(self.temporary.name) / "secret"
        path.write_text(SECRET + "\n")
        for arguments, message in (
            (("--trust-tailscale", "--host", "0.0.0.0"), "loopback --host"),
            (("--trust-tailscale", "--trust-proxy-user", "--proxy-secret-file", str(path)), "cannot be combined"),
            (("--trust-tailscale", "--require-proxy-secret", "--proxy-secret-file", str(path)), "cannot be combined"),
        ):
            with self.subTest(arguments=arguments):
                result = self.mt("web", "--no-open", "--port", "0", *arguments)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)

        process = subprocess.Popen(
            [str(ROOT / "mt"), "-R", str(self.root), "web", "--trust-tailscale", "--no-open", "--port", "0"],
            env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL,
        )
        try:
            url_line = process.stdout.readline()
            info_line = process.stdout.readline()
            self.assertTrue(url_line.startswith("http://127.0.0.1:"), url_line)
            self.assertIn(web_hosted.TAILSCALE_EMAIL_HEADER, info_line)
        finally:
            process.terminate()
            process.communicate(timeout=30)


if __name__ == "__main__":
    unittest.main()
