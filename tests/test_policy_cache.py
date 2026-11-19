"""Operation handles of one workspace share its parsed policy until a policy file changes."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))

import policy_parser  # noqa: E402
from workspace import Workspace  # noqa: E402


class PolicyCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "cards").mkdir()
        self.policy_path = self.root / "settings.yaml"
        self.policy_path.write_text((SOURCE_ROOT / "templates" / "settings.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        self.ws = Workspace.at(self.root, app_root=SOURCE_ROOT)
        loads = mock.patch.object(policy_parser, "load_policy", wraps=policy_parser.load_policy)
        self.load_policy = loads.start()
        self.addCleanup(loads.stop)

    def test_operation_handles_parse_the_policy_once(self) -> None:
        for _ in range(5):
            self.assertEqual(self.ws.for_operation().policy()["default_status"], "inbox")
        self.assertEqual(self.load_policy.call_count, 1)

    def test_a_changed_policy_file_is_reloaded(self) -> None:
        self.assertEqual(self.ws.for_operation().policy()["default_status"], "inbox")
        # Same length as before, so only the content and timestamps differ.
        text = self.policy_path.read_text(encoding="utf-8").replace("default_status: inbox", "default_status: doing")
        self.policy_path.write_text(text, encoding="utf-8")
        self.assertEqual(self.ws.for_operation().policy()["default_status"], "doing")

    def test_a_new_local_policy_file_is_picked_up(self) -> None:
        self.assertEqual(self.ws.for_operation().policy()["default_status"], "inbox")
        (self.root / "settings.local.yaml").write_text("default_status: waiting\n", encoding="utf-8")
        self.assertEqual(self.ws.for_operation().policy()["default_status"], "waiting")

    def test_separate_workspaces_do_not_share_a_cache(self) -> None:
        self.ws.policy()
        Workspace.at(self.root, app_root=SOURCE_ROOT).policy()
        self.assertEqual(self.load_policy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
