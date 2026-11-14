"""Canonical properties and an opaque Markdown document share one file."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import card_format as cards
import cli as tasklib
import operations
import store
import transaction
import validators


class CardFormatTests(unittest.TestCase):
    def test_relative_dates_subtract_calendar_spans(self) -> None:
        from datetime import date
        today = date(2026, 3, 31)
        for value, expected in (("0d", today), ("10D", date(2026, 3, 21)), ("2w", date(2026, 3, 17)),
                                ("1m", date(2026, 2, 28)), ("3M", date(2025, 12, 31)), ("1y", date(2025, 3, 31))):
            self.assertEqual(cards.relative_date(value, today), expected, value)
        self.assertEqual(cards.relative_date("1y", date(2024, 2, 29)), date(2023, 2, 28))
        self.assertEqual(cards.relative_date("1m", date(2024, 3, 31)), date(2024, 2, 29))
        for bad in ("", "3", "d", "3x", "-1d", "1.5w", "2026-03-01", "1 w"):
            self.assertIsNone(cards.relative_date(bad, today), bad)

    def test_task_checkbox_toggle_touches_only_the_marker(self):
        body = ("- [ ] one\r\n  * [X] two\r\n1. [x] three\n> - [ ] quoted\n"
                "- plain [ ] text\n- [ ]no space\n```\n- [ ] fenced\n```\n+ [ ] last")
        self.assertEqual([s[2] for s in cards.task_checkbox_spans(body)], [" ", "X", "x", " ", " "])
        toggled = cards.toggle_task_checkbox(body, 1, 5, False)
        self.assertEqual(toggled, body.replace("[X] two", "[ ] two"))
        self.assertEqual(cards.toggle_task_checkbox(body, 0, 5, True)[:12], "- [x] one\r\n ")
        self.assertIn("> - [x] quoted", cards.toggle_task_checkbox(body, 3, 5, True))
        self.assertIn("+ [x] last", cards.toggle_task_checkbox(body, 4, 5, True))
        self.assertIn("- [ ] fenced", cards.toggle_task_checkbox(body, 0, 5, True))
        with self.assertRaises(ValueError):
            cards.toggle_task_checkbox(body, 0, 4, True)
        with self.assertRaises(ValueError):
            cards.toggle_task_checkbox(body, 5, 5, True)
        with self.assertRaises(ValueError):
            cards.toggle_task_checkbox(body, -1, 5, True)
        # A box already in the requested state means the caller saw a different box.
        with self.assertRaises(ValueError):
            cards.toggle_task_checkbox(body, 1, 5, True)
        with self.assertRaises(ValueError):
            cards.toggle_task_checkbox(body, 0, 5, False)

    def properties(self, kind="task"):
        properties, _ = cards.parse_document(operations.render_new_card(kind, "example", "Example", body_text=""))
        return properties

    def test_arbitrary_markdown_round_trips_without_encoding(self):
        bodies = ("", "  \n## Notes\n\nNeed follow-up.\n\n", "```python\nunfinished = True",
                  "````microtask-capture\n## Notes\n~~~~\n`````",
                  "---\nstatus: done\n---\n\r\nPasted text\r\n",
                  "| a | b |\n| --- | --- |\n| unfinished |")
        for kind in ("task", "note"):
            for body in bodies:
                with self.subTest(kind=kind, body=body):
                    properties = self.properties(kind)
                    raw = cards.render_document(properties, body, expected_kind=kind)
                    actual, document = cards.parse_document(raw, expected_kind=kind)
                    self.assertEqual(actual, properties)
                    self.assertEqual(document, body)
                    context = validators.CheckContext(kind, f"{kind}s/example.md", actual, document, store.policy())
                    self.assertEqual(validators.run_item_validators(context), [])

    def test_nested_activity_multiline_properties_and_unknown_data_round_trip(self):
        properties = self.properties()
        properties.update(next_action="Compare baselines.\n\nResume at Figure 2.",
                          processing_instructions="## Guidance\nKeep user wording.",
                          comments=[{"at": "2026-11-10", "text": "Line one.\n\nLine two."}],
                          time_entries=[{"session_id": "a" * 32, "start": "2026-11-10T09:00:00",
                                         "end": "2026-11-10T09:30:00", "elapsed_seconds": 1800}],
                          extension={"values": ["a:b", "# literal", None]})
        body = "## Processing instructions\n\nThis is content, not guidance."
        raw = cards.render_document(properties, body)
        parsed, document = cards.parse_document(raw)
        self.assertEqual(parsed, properties)
        self.assertEqual(document, body)
        self.assertIn("processing_instructions: |-", raw)

    def test_yaml_dates_stay_strings_and_duplicate_keys_are_rejected(self):
        metadata = cards.parse_yaml("due: 2026-12-01\ncreated: 2026-11-10T09:00:00\n")
        self.assertEqual(metadata, {"due": "2026-12-01", "created": "2026-11-10T09:00:00"})
        for duplicate in ("id: first\nid: second", "comments:\n- at: x\n  text: a\n  text: b"):
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(ValueError, "duplicate"):
                cards.parse_yaml(duplicate)
        with self.assertRaises(ValueError):
            cards.parse_yaml("!!python/object/apply:os.system ['false']")

    def test_yaml_values_must_remain_json_compatible(self):
        for source in ("extension: !!binary SGVsbG8=", "extension: !!set {a: null}",
                       "extension: !!timestamp 2026-11-10", "extension: &loop [*loop]"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                cards.parse_yaml(source)

    def test_first_frontmatter_boundary_preserves_crlf_body(self):
        properties = self.properties("note")
        body = "\r\n## Content\r\n---\r\nstatus: done\r\n---\r\n"
        raw = cards.render_document(properties, body)
        header, actual = cards.parse_frontmatter(raw)
        self.assertEqual(actual, body)
        self.assertEqual(cards.parse_yaml(header)["kind"], "note")
        properties["priority"] = "high"
        changed = cards.render_document(properties, actual)
        self.assertEqual(cards.parse_document(changed)[1].encode(), body.encode())

    def test_structure_validates_properties_not_document_headings(self):
        properties = self.properties()
        for change in ({"title": "Two\nlines"}, {"title": "Null\x00title"}, {"kind": "idea"}, {"next_action": True},
                       {"processing_instructions": []}, {"id": {"nested": "value"}},
                       {"project": {"nested": "value"}}, {"topic": ["nested"]},
                       {"tags": ["valid", 4]}, {"status": None}, {"type": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                cards.render_document({**properties, **change}, "Any body")
        malformed_kind = cards.parse_yaml("kind: [task]\n")
        self.assertTrue(any("kind must be task or note" in error
                            for error in cards.validate_properties(malformed_kind)))
        with self.assertRaises(ValueError):
            cards.render_document(properties, "", expected_kind="note")
        with self.assertRaises(ValueError):
            cards.render_document({**self.properties("note"), "next_action": "A step"}, "")

    def test_activity_records_require_typed_fields(self):
        properties = self.properties()
        invalid_records = (
            {"comments": [{"at": "yesterday", "text": "Hello"}]},
            {"comments": [{"at": "2026-11-10", "text": ""}]},
            {"comments": "not a list"},
            {"time_entries": [{"session_id": "a" * 32, "start": "2026-11-10T09:00:00",
                               "end": "2026-11-10T09:30:00", "elapsed_seconds": True}]},
        )
        for records in invalid_records:
            with self.subTest(records=records), self.assertRaises(ValueError):
                cards.render_document({**properties, **records}, "")

    def test_capture_identity_provenance_and_boolean_validation(self):
        for kind in ("task", "note"):
            properties = self.properties(kind)
            properties.update(capture_id="550e8400-e29b-41d4-a716-446655440000",
                              github_source="https://github.com/acme/tasks/issues/11",
                              github_source_hash="a" * 64, needs_review=True)
            context = validators.CheckContext(kind, f"{kind}s/example.md", properties, "", store.policy())
            self.assertEqual(validators.run_item_validators(context), [])
            properties["needs_review"] = "true"
            self.assertTrue(any("needs_review" in error for error in validators.run_item_validators(context)))
            properties["needs_review"] = True
            properties["capture_id"] = "not-a-uuid"
            self.assertTrue(any("capture_id" in error for error in validators.run_item_validators(context)))
            properties["capture_id"] = "550e8400-e29b-41d4-a716-446655440000"
            properties["github_source_hash"] = None
            self.assertTrue(any("must be set together" in error for error in validators.run_item_validators(context)))

    def test_optional_review_visibility_and_organization_values_need_not_be_stored(self):
        for kind in ("task", "note"):
            properties = self.properties(kind)
            for field in ("needs_review", "archived", "hidden", "project", "topic", "due", "due_kind"):
                properties.pop(field, None)
            context = validators.CheckContext(kind, f"{kind}s/example.md", properties, "", store.policy())
            self.assertEqual(validators.run_item_validators(context), [])


if __name__ == "__main__":
    unittest.main()
