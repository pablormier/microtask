from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import taskweb


class DraftHandlerValidationTests(unittest.TestCase):
    def test_draft_save_handler_rejects_unhashable_kind_and_mode_before_storage(self):
        cases = (
            ("kind", []),
            ("kind", {"note": "value"}),
            ("mode", []),
            ("mode", {"new": "value"}),
        )
        with patch.object(taskweb.draft_store, "save") as save:
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    payload = {"expected_revision": 0, "body": "Draft body",
                               "kind": "note", "mode": "new"}
                    payload[field] = value
                    status, response = taskweb.handle_draft_save(payload, "test-token")
                    self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                    self.assertIn(field, response["error"])
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
