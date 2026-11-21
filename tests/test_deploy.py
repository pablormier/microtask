from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = SOURCE_ROOT / "deploy" / "add-user.sh"
SHARED_SCRIPT = SOURCE_ROOT / "deploy" / "add-shared.sh"
UPGRADE = SOURCE_ROOT / "deploy" / "upgrade.sh"
BASH = shutil.which("bash")


def clone_working_tree(destination: Path) -> None:
    """Clone this checkout, then overlay its uncommitted files so the scripts run the code under test."""
    subprocess.run(["git", "clone", "--quiet", str(SOURCE_ROOT), str(destination)],
                   check=True, capture_output=True, text=True)
    listed = subprocess.run(["git", "-C", str(SOURCE_ROOT), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], check=True, capture_output=True).stdout
    wanted = {name for name in listed.decode().split("\0") if name and (SOURCE_ROOT / name).is_file()}
    for name in wanted:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE_ROOT / name, target)
    tracked = subprocess.run(["git", "-C", str(destination), "ls-files", "-z"],
                             check=True, capture_output=True).stdout.decode().split("\0")
    for name in tracked:
        if name and name not in wanted and (destination / name).exists():
            (destination / name).unlink()
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", "-C", str(destination), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(destination), *identity, "commit", "--quiet", "--allow-empty",
                    "-m", "working tree"], check=True, capture_output=True)


def tag_releases(source: Path) -> None:
    """Give a local clone two release tags, the second on a new commit."""
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    for command in (
        ["tag", "v1.0.0"],
        [*identity, "commit", "--quiet", "--allow-empty", "-m", "second release"],
        ["tag", "v1.1.0"],
        ["tag", "v1.3.0"],
        ["tag", "v3.0.0"],
    ):
        subprocess.run(["git", "-C", str(source), *command], check=True, capture_output=True, text=True)


@unittest.skipUnless(BASH, "bash is not available")
class AddUserScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name).resolve()
        # A local clone source keeps everything off the network. It gets two
        # release tags, the second on a new commit.
        cls.source = base / "source"
        clone_working_tree(cls.source)
        tag_releases(cls.source)
        # A checkout of the app, as add-user.sh finds it in /opt/microtask/current.
        cls.app = base / "app"
        subprocess.run(["git", "clone", "--quiet", str(cls.source), str(cls.app)],
                       check=True, capture_output=True, text=True)
        cls.base = base

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(dir=self.base))

    def add_user(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return subprocess.run(
            [BASH, str(SCRIPT), "--root", str(self.root), "--no-systemctl",
             "--app", str(self.app), *arguments],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

    def test_creates_workspace_env_site_and_email(self) -> None:
        result = self.add_user("alice", "Alice@Example.com", "alice.mt.example.com")

        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.root / "srv" / "mt" / "alice"
        self.assertTrue((workspace / "cards").is_dir())
        self.assertTrue((workspace / ".git").is_dir())
        self.assertFalse((workspace / "mt").exists())
        self.assertFalse((workspace / "scripts").exists())
        remotes = subprocess.run(["git", "-C", str(workspace), "remote"],
                                 check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(remotes, [])
        log = subprocess.run(["git", "-C", str(workspace), "log", "--oneline"],
                             check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(log), 1)
        policy = (workspace / "settings.yaml").read_text()
        self.assertIn("auto_sync: false", policy)

        self.assertEqual((self.root / "etc/mt/alice.env").read_text(),
                         "PORT=8101\nHOST_NAME=alice.mt.example.com\n")
        site = (self.root / "etc/caddy/mt-users/alice.caddy").read_text()
        self.assertIn("alice.mt.example.com {", site)
        self.assertIn("not header X-Auth-Request-Email alice@example.com", site)
        self.assertIn("reverse_proxy 127.0.0.1:8101", site)
        self.assertIn("request_header -X-Auth-Request-Email", site)
        self.assertIn("forward_auth 127.0.0.1:4180", site)
        self.assertNotIn("@USER@", site)
        self.assertNotIn("@PORT@", site)
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(), "alice@example.com\n")

    def test_second_user_gets_next_port_and_email_is_appended(self) -> None:
        self.assertEqual(self.add_user("alice", "alice@example.com", "alice.mt.example.com").returncode, 0)
        result = self.add_user("bob", "bob@example.com", "bob.mt.example.com")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/mt/bob.env").read_text(),
                         "PORT=8102\nHOST_NAME=bob.mt.example.com\n")
        self.assertIn("reverse_proxy 127.0.0.1:8102",
                      (self.root / "etc/caddy/mt-users/bob.caddy").read_text())
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(),
                         "alice@example.com\nbob@example.com\n")

    def test_duplicate_user_is_refused_and_changes_nothing(self) -> None:
        self.assertEqual(self.add_user("alice", "alice@example.com", "alice.mt.example.com").returncode, 0)
        env_before = (self.root / "etc/mt/alice.env").read_text()

        result = self.add_user("alice", "other@example.com", "other.mt.example.com")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual((self.root / "etc/mt/alice.env").read_text(), env_before)
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(), "alice@example.com\n")

    def test_invalid_names_hosts_and_emails_are_refused(self) -> None:
        for arguments in (
            ("Alice", "alice@example.com", "alice.mt.example.com"),
            ("-alice", "alice@example.com", "alice.mt.example.com"),
            ("al_ice", "alice@example.com", "alice.mt.example.com"),
            ("../alice", "alice@example.com", "alice.mt.example.com"),
            ("alice", "not-an-email", "alice.mt.example.com"),
            ("alice", "alice@example.com", "localhost"),
            ("alice", "alice@example.com", "bad host.example.com"),
        ):
            with self.subTest(arguments=arguments):
                result = self.add_user(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("error:", result.stderr)
        self.assertFalse((self.root / "srv").exists())
        self.assertFalse((self.root / "etc").exists())

    def test_dry_run_changes_nothing(self) -> None:
        env = dict(os.environ, HOME=str(self.root))
        result = subprocess.run(
            [BASH, str(SCRIPT), "--root", str(self.root), "--no-systemctl", "--dry-run",
             "--app", str(self.app),
             "alice", "alice@example.com", "alice.mt.example.com"],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORT=8101", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def add_shared(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return subprocess.run(
            [BASH, str(SHARED_SCRIPT), "--root", str(self.root), "--no-systemctl",
             "--app", str(self.app), *arguments],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

    def test_shared_workspace_counts_doing_tasks_per_person_and_trusts_only_the_proxy(self) -> None:
        result = self.add_shared("--admin", "alice", "team", "team.mt.example.com",
                              "Alice@Example.com", "bob.smith+x@example.com")
        self.assertNotEqual(result.returncode, 0)  # a plus sign cannot be part of a user name
        self.assertFalse((self.root / "srv" / "mt" / "team").exists())

        result = self.add_shared("--admin", "alice", "team", "team.mt.example.com",
                              "Alice@Example.com", "bob@example.com", "carol@example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.root / "srv" / "mt" / "team"
        self.assertIn("doing_limit_scope: per_claimant", (workspace / "settings.yaml").read_text())
        log = subprocess.run(["git", "-C", str(workspace), "log", "--oneline"],
                             check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(log), 2)
        status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain"],
                                check=True, capture_output=True, text=True).stdout
        self.assertEqual(status, "")
        validate = subprocess.run([str(self.app / "mt"), "-R", str(workspace), "validate"], check=False,
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  env={key: value for key, value in os.environ.items() if not key.startswith("MT_")})
        self.assertEqual(validate.returncode, 0, validate.stderr)

        secret_file = self.root / "etc/mt/team.secret"
        secret = secret_file.read_text().strip()
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        self.assertEqual((self.root / "etc/mt/team.env").read_text(),
                         "PORT=8101\nHOST_NAME=team.mt.example.com\nMT_ADMINS=alice\n"
                         "MT_PROXY_SECRET_FILE=/etc/mt/team.secret\n"
                         "MT_SIGN_OUT_URL=/oauth2/sign_out\n")
        site = (self.root / "etc/caddy/mt-users/team.caddy").read_text()
        self.assertIn("team.mt.example.com {", site)
        self.assertIn("request_header -X-Auth-Request-Email", site)
        self.assertIn("request_header -X-Microtask-Proxy-Secret", site)
        self.assertIn(f'request_header X-Microtask-Proxy-Secret "{secret}"', site)
        self.assertIn(r"`(?i)^(alice@example\.com|bob@example\.com|carol@example\.com)$`", site)
        self.assertIn("reverse_proxy 127.0.0.1:8101", site)
        self.assertNotRegex(site, r"@[A-Z_]+@")
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(),
                         "alice@example.com\nbob@example.com\ncarol@example.com\n")
        # The same port numbers are shared with the one-person sites.
        self.assertEqual(self.add_user("dave", "dave@example.com", "dave.mt.example.com").returncode, 0)
        self.assertIn("PORT=8102", (self.root / "etc/mt/dave.env").read_text())

    def test_shared_refusals_change_nothing(self) -> None:
        for arguments in (
            ("--admin", "zed", "team", "team.mt.example.com", "alice@example.com"),
            ("team", "team.mt.example.com", "alice@example.com", "alice@other.example.com"),
            ("team", "team.mt.example.com", "not-an-email"),
            ("Bad Name", "team.mt.example.com", "alice@example.com"),
            ("team", "nohost", "alice@example.com"),
            ("team", "team.mt.example.com"),
        ):
            with self.subTest(arguments):
                self.assertNotEqual(self.add_shared(*arguments).returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])
        dry = self.add_shared("--dry-run", "team", "team.mt.example.com", "alice@example.com")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.add_shared("team", "team.mt.example.com", "alice@example.com").returncode, 0)
        again = self.add_shared("team", "team2.mt.example.com", "bob@example.com")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stderr)

    def create_team(self) -> None:
        created = self.add_shared("--admin", "alice", "team", "team.mt.example.com",
                                  "alice@example.com", "bob@example.com")
        self.assertEqual(created.returncode, 0, created.stderr)

    def test_shared_members_are_kept_in_a_private_list(self) -> None:
        self.create_team()
        members = self.root / "etc/mt/team.members"
        self.assertEqual(members.read_text(), "alice@example.com\nbob@example.com\n")
        self.assertEqual(stat.S_IMODE(members.stat().st_mode), 0o600)

    def test_shared_members_can_be_added_and_removed(self) -> None:
        self.create_team()
        caddy = self.root / "etc/caddy/mt-users/team.caddy"
        emails = self.root / "etc/oauth2-proxy/emails.txt"
        before = caddy.read_text()
        secret = (self.root / "etc/mt/team.secret").read_text().strip()

        added = self.add_shared("--add", "team", "Carol@Example.com", "bob@example.com")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual((self.root / "etc/mt/team.members").read_text(),
                         "alice@example.com\nbob@example.com\ncarol@example.com\n")
        site = caddy.read_text()
        self.assertIn(r"`(?i)^(alice@example\.com|bob@example\.com|carol@example\.com)$`", site)
        self.assertIn(f'request_header X-Microtask-Proxy-Secret "{secret}"', site)
        self.assertIn("reverse_proxy 127.0.0.1:8101", site)
        self.assertEqual(site.replace("|carol@example\\.com", ""), before)
        self.assertEqual(emails.read_text(), "alice@example.com\nbob@example.com\ncarol@example.com\n")
        self.assertIn("Nothing to add", self.add_shared("--add", "team", "carol@example.com").stdout)

        # Another site keeps an address that this one gives up.
        self.assertEqual(self.add_user("dave", "carol@example.com", "dave.mt.example.com").returncode, 0)
        removed = self.add_shared("--remove", "team", "carol@example.com", "bob@example.com")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual((self.root / "etc/mt/team.members").read_text(), "alice@example.com\n")
        self.assertIn(r"`(?i)^(alice@example\.com)$`", caddy.read_text())
        self.assertEqual(emails.read_text(), "alice@example.com\ncarol@example.com\n")

    def test_shared_member_changes_are_checked_and_can_be_previewed(self) -> None:
        self.create_team()
        snapshot = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for arguments in (
            ("--add", "team", "alice@other.example.com"),     # same user name as alice
            ("--add", "team", "not-an-email"),
            ("--add", "nosuch", "carol@example.com"),
            ("--add", "--admin", "carol", "team", "carol@example.com"),
            ("--remove", "team", "carol@example.com"),        # not a member
            ("--remove", "team", "alice@example.com"),        # an administrator
            ("--remove", "team", "alice@example.com", "bob@example.com"),  # nobody left
        ):
            with self.subTest(arguments):
                self.assertNotEqual(self.add_shared(*arguments).returncode, 0)
        self.assertIn("MT_ADMINS", self.add_shared("--remove", "team", "alice@example.com").stderr)
        for arguments in (("--add", "team", "carol@example.com"), ("--remove", "team", "bob@example.com")):
            dry = self.add_shared("--dry-run", *arguments)
            self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, snapshot)
        # Once the administrator list no longer names the person, removal works.
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text().replace("MT_ADMINS=alice", "MT_ADMINS="))
        self.assertEqual(self.add_shared("--remove", "team", "alice@example.com").returncode, 0)

    def add_shared_with_umask(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return subprocess.run(
            [BASH, "-c", 'umask 022; exec "$@"', "bash", BASH, str(SHARED_SCRIPT), "--root", str(self.root),
             "--no-systemctl", "--app", str(self.app), *arguments],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

    def assert_private_files(self) -> None:
        for name, limit in (("caddy/mt-users/team.caddy", 0o640), ("mt/team.secret", 0o600),
                            ("mt/team.members", 0o600)):
            path = self.root / "etc" / name
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & ~limit, 0, f"{name} is {oct(mode)}, more open than {oct(limit)}")
            self.assertEqual(mode & 0o007, 0, f"{name} is readable by others")
        retired = self.root / "etc/mt/team.retired"
        if retired.exists():
            self.assertEqual(stat.S_IMODE(retired.stat().st_mode) & 0o077, 0)

    def test_shared_files_with_the_secret_are_never_open_to_others_under_a_permissive_umask(self) -> None:
        created = self.add_shared_with_umask("--admin", "alice", "team", "team.mt.example.com",
                                             "alice@example.com", "bob@example.com")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assert_private_files()
        for arguments in (("--add", "team", "carol@example.com"), ("--remove", "team", "bob@example.com")):
            result = self.add_shared_with_umask(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_private_files()
        # A site file left open by an older version is closed again by the next update.
        caddy = self.root / "etc/caddy/mt-users/team.caddy"
        caddy.chmod(0o644)
        self.assertEqual(self.add_shared_with_umask("--add", "team", "dave@example.com").returncode, 0)
        self.assert_private_files()

    def start_waiting(self, script: Path, *arguments: str) -> tuple[Path, subprocess.Popen]:
        """Start a script that waits for the shared lock, which this test holds until it releases it."""
        lock = self.root / "etc/mt/.add-user.lock"
        lock.mkdir(parents=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        process = subprocess.Popen(
            [BASH, str(script), "--root", str(self.root), "--no-systemctl", "--app", str(self.app), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL)
        time.sleep(0.5)
        return lock, process

    def make_shared_style_files(self) -> None:
        (self.root / "srv/mt/team").mkdir(parents=True)
        (self.root / "srv/mt/team/keep.txt").write_text("first run")
        for name, text in (("team.env", "PORT=8101\n"), ("team.secret", "s\n"), ("team.members", "a@example.com\n")):
            (self.root / "etc/mt" / name).write_text(text)
        (self.root / "etc/caddy/mt-users").mkdir(parents=True)
        (self.root / "etc/caddy/mt-users/team.caddy").write_text("site\n")

    def test_add_user_that_loses_the_race_to_add_shared_leaves_the_shared_workspace_alone(self) -> None:
        lock, process = self.start_waiting(SCRIPT, "team", "carol@example.com", "carol.mt.example.com")
        self.make_shared_style_files()
        lock.rmdir()
        _, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("already exists", stderr)
        self.assertEqual((self.root / "srv/mt/team/keep.txt").read_text(), "first run")
        self.assertEqual((self.root / "etc/mt/team.env").read_text(), "PORT=8101\n")
        self.assertEqual((self.root / "etc/mt/team.secret").read_text(), "s\n")
        self.assertEqual((self.root / "etc/caddy/mt-users/team.caddy").read_text(), "site\n")
        self.assertFalse(lock.exists())

    def test_add_shared_that_loses_the_race_to_add_user_leaves_the_user_alone(self) -> None:
        lock, process = self.start_waiting(SHARED_SCRIPT, "team", "team.mt.example.com", "bob@example.com")
        (self.root / "srv/mt/team").mkdir(parents=True)
        (self.root / "srv/mt/team/keep.txt").write_text("first run")
        (self.root / "etc/mt/team.env").write_text("PORT=8101\nHOST_NAME=x.example.com\n")
        (self.root / "etc/caddy/mt-users").mkdir(parents=True)
        (self.root / "etc/caddy/mt-users/team.caddy").write_text("site\n")
        lock.rmdir()
        _, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("already exists", stderr)
        self.assertEqual((self.root / "srv/mt/team/keep.txt").read_text(), "first run")
        self.assertEqual((self.root / "etc/mt/team.env").read_text(), "PORT=8101\nHOST_NAME=x.example.com\n")
        self.assertEqual((self.root / "etc/caddy/mt-users/team.caddy").read_text(), "site\n")
        self.assertFalse((self.root / "etc/mt/team.secret").exists())

    def test_add_user_refuses_a_name_that_only_has_shared_files(self) -> None:
        for leftover in ("team.secret", "team.members", "team.retired"):
            with self.subTest(leftover):
                (self.root / "etc/mt").mkdir(parents=True, exist_ok=True)
                path = self.root / "etc/mt" / leftover
                path.write_text("x\n")
                result = self.add_user("team", "carol@example.com", "carol.mt.example.com")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already exists", result.stderr)
                self.assertEqual(path.read_text(), "x\n")
                self.assertFalse((self.root / "srv/mt/team").exists())
                path.unlink()

    def set_user_map(self, text: str, admins: str | None = None) -> None:
        user_map = self.root / "etc/mt/team.users"
        user_map.write_text(text)
        env_file = self.root / "etc/mt/team.env"
        content = env_file.read_text()
        if admins is not None:
            content = content.replace("MT_ADMINS=alice", f"MT_ADMINS={admins}")
        env_file.write_text(content + f"MT_USER_MAP={user_map}\n")

    def test_member_user_names_are_the_ones_the_dashboard_uses(self) -> None:
        self.create_team()
        # Different local parts, same mapped name.
        self.set_user_map("carol@example.com bob\n")
        refused = self.add_shared("--add", "team", "carol@example.com")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("user name bob", refused.stderr)
        self.assertNotIn("carol@example.com", (self.root / "etc/mt/team.members").read_text())
        # The same local part is fine when the map gives the newcomer another name.
        self.set_user_map("bob@other.example.org bob2\n")
        self.assertEqual(self.add_shared("--add", "team", "bob@other.example.org").returncode, 0)
        self.assertIn("bob@other.example.org", (self.root / "etc/mt/team.members").read_text())
        # A mapped name must be a valid user name.
        self.set_user_map("dave@example.com Bad_Name\n")
        self.assertNotEqual(self.add_shared("--add", "team", "dave@example.com").returncode, 0)

    def test_the_administrator_check_uses_the_mapped_name(self) -> None:
        self.create_team()
        # alice is mapped to boss, and boss administers: removing her is refused.
        self.set_user_map("alice@example.com boss\n", admins="boss")
        refused = self.add_shared("--remove", "team", "alice@example.com")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("MT_ADMINS", refused.stderr)
        # Her local part does not make her an administrator: she is boss to the dashboard.
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text().replace("MT_ADMINS=boss", "MT_ADMINS=bob"))
        self.assertEqual(self.add_shared("--remove", "team", "alice@example.com").returncode, 0)

    def test_member_changes_stop_when_the_configured_user_map_cannot_be_used(self) -> None:
        self.create_team()
        user_map = self.root / "etc/mt/team.users"
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text() + f"MT_USER_MAP={user_map}\n")
        snapshot = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        cases = [("missing", None, "does not exist"),
                 ("one field", "carol@example.com\n", "EMAIL USER"),
                 ("three fields", "carol@example.com carol extra\n", "EMAIL USER"),
                 ("not an email", "carol carol2\n", "not an email"),
                 ("bad name", "carol@example.com Carol\n", "user name must be"),
                 ("duplicate", "carol@example.com c1\ncarol@example.com c2\n", "twice")]
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            cases.append(("unreadable", "carol@example.com c1\n", "cannot read"))
        for label, content, message in cases:
            if content is not None:
                user_map.write_text(content)
                if label == "unreadable":
                    user_map.chmod(0)
            for arguments in (("--add", "team", "carol@example.com"), ("--remove", "team", "bob@example.com")):
                for dry in ((), ("--dry-run",)):
                    with self.subTest(label=label, arguments=arguments, dry=dry):
                        result = self.add_shared(*dry, *arguments)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(message, result.stderr)
            if label == "unreadable":
                user_map.chmod(0o600)
            if user_map.exists() and label != "missing":
                snapshot[user_map] = user_map.read_bytes()
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, {**snapshot, **{p: b for p, b in after.items() if p == user_map}})
        user_map.write_text("# comment\ncarol@example.com carol2 # trailing\n")
        self.assertEqual(self.add_shared("--dry-run", "--add", "team", "carol@example.com").returncode, 0)

    def test_admin_names_are_read_like_the_dashboard(self) -> None:
        self.create_team()
        env_file = self.root / "etc/mt/team.env"
        for admins in ("alice, bob", "alice bob", "alice,bob", " alice ,, bob "):
            env_file.write_text(env_file.read_text().replace(
                next(line for line in env_file.read_text().splitlines() if line.startswith("MT_ADMINS=")),
                f"MT_ADMINS={admins}"))
            with self.subTest(admins=admins):
                refused = self.add_shared("--dry-run", "--remove", "team", "bob@example.com")
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("MT_ADMINS", refused.stderr)
                self.assertEqual(self.add_shared("--add", "team", "carol@example.com").returncode, 0)
                allowed = self.add_shared("--remove", "team", "carol@example.com")
                self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_admin_flag_values_are_written_as_a_plain_comma_list(self) -> None:
        result = self.add_shared("--admin", "alice", "--admin", "bob, carol", "team", "team.mt.example.com",
                                 "alice@example.com", "bob@example.com", "carol@example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MT_ADMINS=alice,bob,carol\n", (self.root / "etc/mt/team.env").read_text())

    def create_with_settings(self, rules_text: str, edit: str = "", intercept: str = "") -> subprocess.CompletedProcess[str]:
        """Create the team with an app whose `mt init` leaves the given line in place of max_doing_tasks.

        `edit` is Python run on the settings text `t` afterwards; `intercept` is Bash run before the real app."""
        fake = self.root.parent / f"fake-app-{self.root.name}"
        shutil.rmtree(fake, ignore_errors=True)
        fake.mkdir()
        wrapper = fake / "mt"
        wrapper.write_text(
            "#!/usr/bin/env bash\nset -e\n"
            f"{intercept}\n"
            f'"{self.app}/mt" "$@"\n'
            'dir=""; while [[ $# -gt 0 ]]; do [[ "$1" == --data ]] && dir="$2"; shift; done\n'
            '[[ -n "$dir" ]] || exit 0\n'
            f'python3 - "$dir/settings.yaml" <<\'PY\'\nimport sys, pathlib\n'
            f'p = pathlib.Path(sys.argv[1]); t = p.read_text()\n'
            f't = t.replace("  max_doing_tasks: 5\\n", {rules_text!r}, 1)\n{edit}\np.write_text(t)\nPY\n'
            'git -C "$dir" commit -qam "Adjust settings" || true\n')
        wrapper.chmod(0o755)
        (fake / "tools").symlink_to(self.app / "tools")
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env.update(HOME=str(self.root), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@localhost",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@localhost")
        return subprocess.run(
            [BASH, str(SHARED_SCRIPT), "--root", str(self.root), "--no-systemctl", "--app", str(fake),
             "team", "team.mt.example.com", "alice@example.com"],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)

    def test_settings_that_already_name_the_doing_limit_scope_end_up_per_claimant(self) -> None:
        for label, rules_text in (
            ("missing", "  max_doing_tasks: 5\n"),
            ("workspace", "  max_doing_tasks: 5\n  doing_limit_scope: workspace\n"),
            ("already", "  max_doing_tasks: 5\n  doing_limit_scope: per_claimant\n"),
            ("commented", "  max_doing_tasks: 5\n  # doing_limit_scope: workspace\n"),
        ):
            with self.subTest(label):
                shutil.rmtree(self.root)
                self.root.mkdir()
                result = self.create_with_settings(rules_text)
                self.assertEqual(result.returncode, 0, result.stderr)
                workspace = self.root / "srv/mt/team"
                text = (workspace / "settings.yaml").read_text()
                active = [line for line in text.splitlines() if line.startswith("  doing_limit_scope:")]
                self.assertEqual(active, ["  doing_limit_scope: per_claimant"])
                self.assertEqual(text.count("per_claimant"), 1)
                status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain"],
                                        check=True, capture_output=True, text=True).stdout
                self.assertEqual(status, "")
                log = subprocess.run(["git", "-C", str(workspace), "log", "--format=%s"],
                                     check=True, capture_output=True, text=True).stdout.splitlines()
                self.assertEqual(log.count("Count tasks in progress per person"), 0 if label == "already" else 1)

    def test_settings_with_four_space_indentation_end_up_valid_and_per_claimant(self) -> None:
        edit = ("import re\n"
                "head, rest = t.split('\\nrules:\\n', 1)\n"
                "body, tail = rest.split('\\n\\nlint:', 1)\n"
                "t = head + '\\nrules:\\n' + re.sub(r'(?m)^  ', '    ', body) + '\\n\\nlint:' + tail")
        for label, rules_text in (("missing", "  max_doing_tasks: 5\n"),
                                  ("workspace", "  max_doing_tasks: 5\n  doing_limit_scope: workspace\n")):
            with self.subTest(label):
                shutil.rmtree(self.root)
                self.root.mkdir()
                result = self.create_with_settings(rules_text, edit=edit)
                self.assertEqual(result.returncode, 0, result.stderr)
                workspace = self.root / "srv/mt/team"
                text = (workspace / "settings.yaml").read_text()
                self.assertIn("\n    doing_limit_scope: per_claimant\n", text)
                self.assertEqual(text.count("doing_limit_scope"), 1)
                settings = subprocess.run([str(self.app / "mt"), "-R", str(workspace), "settings", "--json"],
                                          check=True, capture_output=True, text=True, env={**os.environ, "MT_WORKSPACE": ""})
                self.assertEqual(json.loads(settings.stdout)["rules"]["doing_limit_scope"], "per_claimant")
                valid = subprocess.run([str(self.app / "mt"), "-R", str(workspace), "validate"],
                                       check=False, capture_output=True, text=True)
                self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)

    def test_a_workspace_that_does_not_validate_after_the_settings_change_is_not_kept(self) -> None:
        intercepts = {
            "invalid": '[[ " $* " == *" validate "* ]] && { echo "settings.yaml is broken" >&2; exit 1; }',
            "not read back": '[[ " $* " == *" settings "* ]] && { echo \'{"rules": {}}\'; exit 0; }',
        }
        # Without a rules block the script adds one, and the app then refuses it: max_doing_tasks is required.
        no_rules = "import re\nt = re.sub(r'(?ms)^rules:.*?\\n\\n(?=lint:)', '', t)"
        for label, intercept, edit in (("invalid", intercepts["invalid"], ""),
                                       ("not read back", intercepts["not read back"], ""),
                                       ("no rules block", "", no_rules)):
            with self.subTest(label):
                shutil.rmtree(self.root)
                self.root.mkdir()
                result = self.create_with_settings("  max_doing_tasks: 5\n", edit=edit, intercept=intercept)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("doing_limit_scope", result.stderr)
                self.assertIn("removing what this run created", result.stderr)
                for path in ("srv/mt/team", "etc/mt/team.env", "etc/mt/team.secret", "etc/mt/team.members",
                             "etc/caddy/mt-users/team.caddy"):
                    self.assertFalse((self.root / path).exists(), path)

    def test_a_user_map_with_windows_line_endings_does_not_block_other_changes(self) -> None:
        self.create_team()
        user_map = self.root / "etc/mt/team.users"
        user_map.write_bytes(b"# map\r\nalice@example.com alice\r\nbob@example.com bob\r\ncarol@example.com carol2\r\n")
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text() + f"MT_USER_MAP={user_map}\n")
        added = self.add_shared("--add", "team", "carol@example.com")
        self.assertEqual(added.returncode, 0, added.stderr)
        removed = self.add_shared("--remove", "team", "carol@example.com")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        retired = (self.root / "etc/mt/team.retired").read_bytes()
        self.assertEqual(retired, b"carol2 carol@example.com\n")
        # An unrelated member leaves while the map still has CRLF lines.
        removed = self.add_shared("--remove", "team", "bob@example.com")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertNotIn(b"\r", (self.root / "etc/mt/team.retired").read_bytes())
        self.assertNotIn(b"\r", (self.root / "etc/mt/team.members").read_bytes())

    def test_a_second_create_that_loses_the_race_leaves_the_first_workspace_alone(self) -> None:
        lock = self.root / "etc/mt/.add-user.lock"
        lock.mkdir(parents=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        process = subprocess.Popen(
            [BASH, str(SHARED_SCRIPT), "--root", str(self.root), "--no-systemctl", "--app", str(self.app),
             "team", "team.mt.example.com", "bob@example.com"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL)
        # The second run passed its early check and now waits for the lock. The first run finishes.
        time.sleep(0.5)
        workspace = self.root / "srv/mt/team"
        workspace.mkdir(parents=True)
        (workspace / "keep.txt").write_text("first run")
        (self.root / "etc/mt/team.env").write_text("PORT=8101\n")
        lock.rmdir()
        _, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("already exists", stderr)
        self.assertEqual((workspace / "keep.txt").read_text(), "first run")
        self.assertEqual((self.root / "etc/mt/team.env").read_text(), "PORT=8101\n")
        self.assertFalse((self.root / "etc/mt/team.secret").exists())
        self.assertFalse(lock.exists())

    def test_a_removed_members_user_name_is_not_handed_to_someone_else(self) -> None:
        self.create_team()
        retired = self.root / "etc/mt/team.retired"
        self.assertEqual(self.add_shared("--remove", "team", "bob@example.com").returncode, 0)
        self.assertEqual(retired.read_text(), "bob bob@example.com\n")
        self.assertEqual(stat.S_IMODE(retired.stat().st_mode), 0o600)
        snapshot = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        refused = self.add_shared("--add", "team", "bob@other.example.org")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--reuse-name", refused.stderr)
        self.assertIn("MT_USER_MAP", refused.stderr)
        dry = self.add_shared("--dry-run", "--add", "team", "bob@other.example.org")
        self.assertNotEqual(dry.returncode, 0)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, snapshot)
        self.assertNotEqual(self.add_shared("--remove", "--reuse-name", "team", "alice@example.com").returncode, 0)

        # A different name through the user map is fine.
        user_map = self.root / "etc/mt/team.users"
        user_map.write_text("bob@other.example.org bob2\n")
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text() + f"MT_USER_MAP={user_map}\n")
        self.assertEqual(self.add_shared("--add", "team", "bob@other.example.org").returncode, 0)
        self.assertEqual(retired.read_text(), "bob bob@example.com\n")

    def test_a_retired_user_name_can_come_back_with_the_same_address_or_the_reuse_flag(self) -> None:
        self.create_team()
        retired = self.root / "etc/mt/team.retired"
        members = self.root / "etc/mt/team.members"
        self.assertEqual(self.add_shared("--remove", "team", "bob@example.com").returncode, 0)
        # The same address is the same person, so the name comes back without a flag.
        self.assertEqual(self.add_shared("--add", "team", "Bob@Example.com").returncode, 0)
        self.assertEqual(retired.read_text(), "")
        self.assertIn("bob@example.com", members.read_text())
        # After another removal a different address needs the explicit flag.
        self.assertEqual(self.add_shared("--remove", "team", "bob@example.com").returncode, 0)
        self.assertEqual(self.add_shared("--add", "--reuse-name", "team", "bob@other.example.org").returncode, 0)
        self.assertEqual(retired.read_text(), "")
        self.assertIn("bob@other.example.org", members.read_text())
        # Now bob@example.com is blocked from taking the name back from the new person.
        self.assertEqual(self.add_shared("--remove", "team", "bob@other.example.org").returncode, 0)
        self.assertEqual(retired.read_text(), "bob bob@other.example.org\n")

    def test_shared_script_syntax_and_shellcheck(self) -> None:
        syntax = subprocess.run([BASH, "-n", str(SHARED_SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        lint = subprocess.run([shellcheck, str(SHARED_SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)

    def test_script_syntax_and_shellcheck(self) -> None:
        syntax = subprocess.run([BASH, "-n", str(SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        lint = subprocess.run([shellcheck, str(SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)


@unittest.skipUnless(BASH, "bash is not available")
class UpgradeScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name).resolve()
        cls.source = base / "source"
        clone_working_tree(cls.source)
        tag_releases(cls.source)
        cls.base = base

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(dir=self.base))
        self.apps = self.root / "opt" / "microtask"

    def environment(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return env

    def upgrade(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(UPGRADE), "--root", str(self.root), "--no-systemctl",
             "--source", str(self.source), *arguments],
            check=False, capture_output=True, text=True, env=self.environment(), stdin=subprocess.DEVNULL,
        )

    def add_user(self, name: str) -> None:
        result = subprocess.run(
            [BASH, str(SCRIPT), "--root", str(self.root), "--no-systemctl",
             "--app", str(self.apps / "current"), name, f"{name}@example.com", f"{name}.mt.example.com"],
            check=False, capture_output=True, text=True, env=self.environment(), stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def link(self, name: str) -> str | None:
        path = self.apps / name
        return os.readlink(path) if path.is_symlink() else None

    def test_first_install_creates_version_and_current_only(self) -> None:
        result = self.upgrade("v1.0.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.apps / "v1.0.0" / "mt").is_file())
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertIsNone(self.link("previous"))
        self.assertIn("v1.0.0", result.stdout)
        self.assertTrue((self.apps / "v1.0.0" / ".mt-hosted").is_file())
        self.assertEqual([path.name for path in self.apps.iterdir() if path.name.startswith(".")], [])
        mode = (self.apps / "v1.0.0" / "mt").stat().st_mode
        self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH))

    def test_upgrade_switches_links_and_validates_every_workspace(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        self.add_user("bob")

        result = self.upgrade("v1.1.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link("current"), "v1.1.0")
        self.assertEqual(self.link("previous"), "v1.0.0")
        self.assertIn("ok: alice", result.stdout)
        self.assertIn("ok: bob", result.stdout)
        self.assertTrue((self.apps / "v1.0.0").is_dir())

    def test_failing_workspace_stops_the_switch(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        self.add_user("bob")
        (self.root / "srv/mt/bob/cards/broken.md").write_text("no front matter here\n")

        result = self.upgrade("v1.1.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bob", result.stderr)
        self.assertNotIn("FAILED: alice", result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertIsNone(self.link("previous"))
        self.assertTrue((self.apps / "v1.1.0").is_dir())
        self.assertIn("stays in", result.stderr)

    def test_workspace_still_using_the_old_settings_name_stops_the_switch(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        self.add_user("bob")
        (self.root / "srv/mt/bob/settings.yaml").rename(self.root / "srv/mt/bob/mt-policy.yaml")

        result = self.upgrade("v1.1.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED: bob", result.stderr)
        self.assertIn("mt-policy.yaml", result.stderr)
        self.assertIn("migrate", result.stderr)
        self.assertNotIn("FAILED: alice", result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertFalse((self.root / "srv/mt/bob/settings.yaml").exists())

    def test_skipping_a_minor_version_is_refused_unless_allowed(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)

        for tag in ("v1.3.0", "v3.0.0"):
            with self.subTest(tag=tag):
                result = self.upgrade(tag)
                dry = self.upgrade("--dry-run", tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(dry.returncode, 0)
                self.assertIn("one minor version at a time", result.stderr)
                self.assertIn("--allow-skip", result.stderr)
                self.assertFalse((self.apps / tag).exists())
                self.assertEqual(self.link("current"), "v1.0.0")

        self.assertIn("the next one is v1.1.x", self.upgrade("v1.3.0").stderr)

        allowed = self.upgrade("--allow-skip", "v1.3.0")

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(self.link("current"), "v1.3.0")

    def test_the_next_minor_comes_from_the_tags_even_across_a_major(self) -> None:
        # Tags are v1.0.0, v1.1.0, v1.3.0 and v3.0.0: there is no v1.2.x or v2.x.
        self.assertEqual(self.upgrade("v1.1.0").returncode, 0)
        self.assertEqual(self.upgrade("v1.3.0").returncode, 0, "v1.2.x does not exist, so v1.3.0 is next")
        result = self.upgrade("v3.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link("current"), "v3.0.0")

    def test_failed_clone_leaves_nothing_behind(self) -> None:
        result = self.upgrade("v9.9.9")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not clone", result.stderr)
        self.assertEqual(list(self.apps.iterdir()), [])

    def test_rollback_swaps_back_and_forth(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.assertEqual(self.upgrade("v1.1.0").returncode, 0)

        result = self.upgrade("--rollback")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertEqual(self.link("previous"), "v1.1.0")
        self.assertEqual(self.upgrade("--rollback").returncode, 0)
        self.assertEqual(self.link("current"), "v1.1.0")

    def test_rollback_without_previous_is_refused(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)

        result = self.upgrade("--rollback")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no previous", result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")

    def test_current_tag_is_a_no_op(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)

        result = self.upgrade("v1.0.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already the current", result.stdout)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertIsNone(self.link("previous"))

    def test_existing_non_current_folder_is_refused(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.assertEqual(self.upgrade("v1.1.0").returncode, 0)

        result = self.upgrade("v1.0.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.link("current"), "v1.1.0")

    def test_dry_run_and_bad_tags_change_nothing(self) -> None:
        self.assertEqual(self.upgrade("--dry-run", "v1.0.0").returncode, 0)
        for tag in ("../x", "current", "bad tag"):
            with self.subTest(tag=tag):
                self.assertNotEqual(self.upgrade(tag).returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def snapshot(self, folder: Path) -> dict[str, tuple[int, int]]:
        found = {}
        for directory, names, files in os.walk(folder):
            for name in [*names, *files]:
                path = Path(directory) / name
                info = path.lstat()
                found[str(path)] = (info.st_mtime_ns, info.st_size)
        return found

    def test_read_only_app_runs_a_data_only_workspace(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        app = self.apps / "v1.0.0"
        workspace = self.root / "srv/mt/alice"
        for path in [app, *app.rglob("*")]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

        def restore() -> None:
            for path in [app, *app.rglob("*")]:
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | stat.S_IWUSR)
        self.addCleanup(restore)

        env = self.environment()
        env.update(HOME=str(workspace), PYTHONDONTWRITEBYTECODE="1")
        before = self.snapshot(app)

        def mt(*arguments: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run([str(app / "mt"), "-R", str(workspace), *arguments], cwd=workspace,
                                    check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 0, f"{arguments}: {result.stderr}")
            return result

        mt("add", "Read only app task")
        self.assertIn("Read only app task", mt("list").stdout)
        mt("time", "start", "Read only")
        mt("time", "stop")
        mt("validate")

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen(
            [str(app / "mt"), "-R", str(workspace), "web", "--no-open", "--host", "127.0.0.1",
             "--port", str(port), "--allow-host", "alice.mt.example.com"],
            cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Host": "alice.mt.example.com"})
            for _ in range(50):
                try:
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("the dashboard did not start")
        finally:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

        self.assertEqual(self.snapshot(app), before)

    def test_script_syntax_and_shellcheck(self) -> None:
        syntax = subprocess.run([BASH, "-n", str(UPGRADE)], check=False, capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        lint = subprocess.run([shellcheck, str(UPGRADE)], check=False, capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)


class DeployFilesTests(unittest.TestCase):
    def test_kit_has_no_real_hosts_or_secrets(self) -> None:
        text = "\n".join(path.read_text() for path in (SOURCE_ROOT / "deploy").rglob("*") if path.is_file())
        self.assertIn("example.com", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertNotRegex(text, r"[A-Za-z0-9._-]+@(?!example\.com)[a-z0-9-]+\.(com|org|net|io)\b")


if __name__ == "__main__":
    unittest.main()
