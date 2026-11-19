from __future__ import annotations

from datetime import date, timedelta
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))

import cli  # noqa: E402
import cli_admin  # noqa: E402
import store  # noqa: E402
import support  # noqa: E402
import workspace  # noqa: E402


class DemoWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "app"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", ".DS_Store", "workspace"),
        )
        self.env = os.environ.copy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return support.run_cli(self.root, *args, env=env or self.env)

    def test_seed_demo_workspace_rebases_dates_copies_attachments_and_disables_auto_sync(self) -> None:
        target = Path(self.temporary.name) / "seeded"
        ws = workspace.seed_demo_workspace(target, app_root=self.root)

        self.assertFalse(store.policy(ws).get("rules", {}).get("auto_sync", True))
        cards = store.discover(ws=ws)
        by_title = {card.title: card for card in cards}
        expected = {
            "Re-run the benchmark baseline": (-1, "deadline"),
            "Audit the benchmark split": (0, "deadline"),
            "Check back on ambiguous benchmark cases": (1, "reminder"),
            "Prepare the lab meeting readout": (3, "deadline"),
            "Work through the causal inference DAG exercises": (5, "target"),
        }
        today = date.today()
        for title, (delta, kind) in expected.items():
            card = by_title[title]
            self.assertEqual(card.frontmatter["due"], (today + timedelta(days=delta)).isoformat())
            self.assertEqual(card.frontmatter["due_kind"], kind)

        for attachment in (self.root / "examples" / "attachments").iterdir():
            if attachment.is_file() and not attachment.name.startswith("."):
                copied = target / "attachments" / attachment.name
                self.assertTrue(copied.is_file())
                self.assertEqual(copied.read_bytes(), attachment.read_bytes())

    def test_empty_initialized_workspace_stays_genuinely_empty(self) -> None:
        init = self.run_mt("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertIn("mt demo", init.stdout)

        listed = self.run_mt("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.strip(), "")
        self.assertNotIn("Demo mode:", listed.stderr)

        validated = self.run_mt("validate")
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertIn("0 task file(s), 0 note file(s)", validated.stdout)

    def test_demo_command_ignores_mt_workspace_and_cleans_up_temp_dir(self) -> None:
        captured: dict[str, object] = {}

        def fake_serve(ws, host, port, *, no_open, filters=None, allow_hosts=(), demo=False):
            captured["root"] = ws.root
            captured["demo"] = demo
            captured["no_open"] = no_open
            captured["cards"] = len(list(ws.cards_root().glob("*.md")))
            self.assertTrue(ws.root.is_dir())
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp_parent:
            env = {**self.env, "MT_WORKSPACE": "/nonexistent/should-be-ignored"}
            with mock.patch("tools.taskweb.serve_dashboard", side_effect=fake_serve), \
                 mock.patch.object(sys, "argv", ["mt", "demo", "--no-open"]), \
                 mock.patch.object(tempfile, "gettempdir", return_value=tmp_parent), \
                 mock.patch.dict(os.environ, env, clear=True):
                rc = cli.main()
                self.assertEqual(rc, 0)

        demo_root = captured["root"]
        assert isinstance(demo_root, Path)
        self.assertTrue(captured["demo"])
        self.assertTrue(captured["no_open"])
        self.assertGreater(captured["cards"], 0)
        self.assertFalse(demo_root.exists())

    def test_sweep_stale_unlocked_demo_dirs_preserves_locked_sibling(self) -> None:
        parent = Path(self.temporary.name) / "microtask-demo"
        parent.mkdir()
        stale = parent / "demo-stale"
        stale.mkdir()
        (stale / ".demo.lock").touch()
        (stale / "marker.txt").write_text("old", encoding="utf-8")

        live = parent / "demo-live"
        live.mkdir()
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys, time; f=open(sys.argv[1],'a+'); fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB); print('locked', flush=True); time.sleep(10)",
                str(live / ".demo.lock"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            cli_admin._sweep_stale_demo_dirs(parent)
            self.assertFalse(stale.exists())
            self.assertTrue(live.is_dir())
        finally:
            holder.terminate()
            holder.wait(timeout=5)

    def run_demo_with_tmpdir(self, tmp_parent: str, argv: list[str], env: dict[str, str] | None = None,
                             serve=None) -> int:
        serve = serve or mock.Mock(side_effect=KeyboardInterrupt)
        with mock.patch("tools.taskweb.serve_dashboard", side_effect=serve), \
             mock.patch.object(sys, "argv", ["mt", "demo", "--no-open", *argv]), \
             mock.patch.object(tempfile, "gettempdir", return_value=tmp_parent), \
             mock.patch.dict(os.environ, env if env is not None else self.env, clear=True):
            return cli.main()

    def test_demo_ignores_scope_variables_in_the_environment(self) -> None:
        seen: dict[str, object] = {}

        def fake_serve(ws, host, port, *, no_open, filters=None, allow_hosts=(), demo=False):
            seen["filters"] = filters
            seen["environ"] = dict(ws.environ)
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp_parent:
            env = {**self.env, "MT_PROJECT": "no-such-project", "MT_TOPIC": "no/such/topic"}
            self.assertEqual(self.run_demo_with_tmpdir(tmp_parent, [], env, fake_serve), 0)
        self.assertEqual(seen["filters"], {})
        for name in ("MT_WORKSPACE", "MT_PROJECT", "MT_TOPIC"):
            self.assertNotIn(name, seen["environ"])

    def test_demo_with_an_invalid_filter_exits_2_and_creates_no_folder(self) -> None:
        serve = mock.Mock(side_effect=KeyboardInterrupt)
        with tempfile.TemporaryDirectory() as tmp_parent:
            self.assertEqual(self.run_demo_with_tmpdir(tmp_parent, ["--status", "bogus"], serve=serve), 2)
            self.assertEqual(list(Path(tmp_parent).iterdir()), [])
        serve.assert_not_called()

    def test_demo_refuses_a_parent_folder_open_to_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_parent:
            parent = Path(tmp_parent) / f"microtask-demo-{os.getuid()}"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            serve = mock.Mock(side_effect=KeyboardInterrupt)
            self.assertEqual(self.run_demo_with_tmpdir(tmp_parent, [], serve=serve), 1)
            serve.assert_not_called()
            self.assertEqual(list(parent.iterdir()), [])

    def test_demo_refuses_a_symlinked_parent_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_parent:
            elsewhere = Path(tmp_parent) / "elsewhere"
            elsewhere.mkdir(mode=0o700)
            (Path(tmp_parent) / f"microtask-demo-{os.getuid()}").symlink_to(elsewhere)
            serve = mock.Mock(side_effect=KeyboardInterrupt)
            self.assertEqual(self.run_demo_with_tmpdir(tmp_parent, [], serve=serve), 1)
            serve.assert_not_called()
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_sweep_removes_old_demo_folders_without_a_lock_file_and_keeps_fresh_ones(self) -> None:
        parent = Path(self.temporary.name) / "microtask-demo-sweep"
        parent.mkdir()
        old = parent / "demo-old"
        fresh = parent / "demo-fresh"
        other = parent / "not-a-demo"
        for folder in (old, fresh, other):
            folder.mkdir()
            (folder / "marker.txt").write_text("x", encoding="utf-8")
        long_ago = time.time() - 3600
        os.utime(old, (long_ago, long_ago))
        os.utime(other, (long_ago, long_ago))
        cli_admin._sweep_stale_demo_dirs(parent)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(other.is_dir())

    def test_existing_settings_with_demo_workspace_false_still_validates(self) -> None:
        ws_dir = Path(self.temporary.name) / "legacy-settings"
        workspace.create_workspace(ws_dir, app_root=self.root)
        settings = ws_dir / "settings.yaml"
        base_text = (SOURCE_ROOT / "templates" / "settings.yaml").read_text(encoding="utf-8")
        settings.write_text(base_text.replace("  show_year_progress:", "  demo_workspace: false\n  show_year_progress:"),
                            encoding="utf-8")
        validated = self.run_mt("-R", str(ws_dir), "validate")
        self.assertEqual(validated.returncode, 0, validated.stderr)


if __name__ == "__main__":
    unittest.main()
