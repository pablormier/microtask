from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cli as tasklib
import store
import transaction  # noqa: E402
from workspace import Workspace  # noqa: E402

SOURCE_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = SOURCE_ROOT / "examples" / "cards"
TASK = "2026-01-02-090100-aggregation-bug.md"
OTHER_TASK = "2026-01-02-090300-lab-readout.md"
NOTE = "2026-01-02-091300-alphafold-notes.md"


class CardCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "cards"
        self.ws = Workspace.at(SOURCE_ROOT / "templates")
        self.root.mkdir()
        for name in (TASK, OTHER_TASK, NOTE):
            shutil.copy2(EXAMPLES / name, self.root / name)
        self.cache = store.CardCache()

    def assert_equivalent(self) -> list[store.Card]:
        cached = store.read_cards(self.root, cache=self.cache, ws=self.ws)
        self.assertEqual(cached, store.read_cards(self.root, ws=self.ws))
        return cached

    def count_parses(self, action) -> int:
        with mock.patch.object(store.Card, "read", wraps=store.Card.read) as read:
            action()
        return read.call_count

    def test_unchanged_files_are_not_parsed_again(self) -> None:
        first = self.assert_equivalent()
        second = []
        parses = self.count_parses(lambda: second.extend(store.read_cards(self.root, cache=self.cache, ws=self.ws)))
        self.assertEqual(parses, 0)
        self.assertEqual(len(second), 3)
        self.assertTrue(all(a is b for a, b in zip(first, second)))

    def test_atomic_rewrite_reparses_only_that_card(self) -> None:
        self.assert_equivalent()
        path = self.root / TASK
        replacement = path.with_name("replacement.tmp")
        replacement.write_text(path.read_text(encoding="utf-8").replace("Fix per-target metric aggregation", "Fix the metric aggregation"),
                               encoding="utf-8")
        os.replace(replacement, path)
        self.assertEqual(self.count_parses(lambda: store.read_cards(self.root, cache=self.cache, ws=self.ws)), 1)
        self.assert_equivalent()
        titles = [card.title for card in store.read_cards(self.root, cache=self.cache, ws=self.ws)]
        self.assertIn("Fix the metric aggregation", titles)

    def test_in_place_rewrite_with_restored_mtime_is_detected(self) -> None:
        self.assert_equivalent()
        path = self.root / TASK
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        changed = text.replace("Fix per-target metric aggregation", "Fix per-target metric calculation")
        self.assertEqual(len(changed), len(text))
        path.write_text(changed, encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        if (after.st_mtime_ns, after.st_size, after.st_ino) != (before.st_mtime_ns, before.st_size, before.st_ino):
            self.fail("test setup did not restore mtime, size and inode")
        if after.st_ctime_ns == before.st_ctime_ns:
            self.skipTest("this filesystem's ctime did not change on an in-place write")
        self.assertEqual(self.count_parses(lambda: store.read_cards(self.root, cache=self.cache, ws=self.ws)), 1)
        self.assert_equivalent()
        self.assertIn("Fix per-target metric calculation", [card.title for card in store.read_cards(self.root, cache=self.cache, ws=self.ws)])

    def test_deleted_cards_disappear_and_new_cards_appear(self) -> None:
        self.assert_equivalent()
        (self.root / NOTE).unlink()
        shutil.copy2(EXAMPLES / "2026-01-02-090200-baseline.md", self.root / "2026-01-02-090200-baseline.md")
        cards = self.assert_equivalent()
        self.assertNotIn(NOTE, [card.path.name for card in cards])
        self.assertIn("2026-01-02-090200-baseline.md", [card.path.name for card in cards])
        self.assertNotIn(self.root / NOTE, self.cache.entries)

    def test_pruning_leaves_other_roots_alone(self) -> None:
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        shutil.copy2(EXAMPLES / TASK, other / TASK)
        store.read_cards(other, cache=self.cache, ws=self.ws)
        self.assert_equivalent()
        self.assertIn(other / TASK, self.cache.entries)

    def test_parse_errors_are_not_cached(self) -> None:
        self.assert_equivalent()
        path = self.root / NOTE
        good = path.read_text(encoding="utf-8")
        path.write_text("no frontmatter here\n", encoding="utf-8")
        with self.assertRaises(ValueError) as uncached:
            store.read_cards(self.root, ws=self.ws)
        with self.assertRaises(ValueError) as cached:
            store.read_cards(self.root, cache=self.cache, ws=self.ws)
        self.assertEqual(str(cached.exception), str(uncached.exception))
        self.assertNotIn(path, self.cache.entries)
        path.write_text(good, encoding="utf-8")
        self.assert_equivalent()

    def test_status_policy_is_checked_on_every_read(self) -> None:
        self.assert_equivalent()
        allowed = [name for name in store.statuses(self.ws) if name != "active"]
        with mock.patch.object(store, "statuses", return_value=allowed):
            with self.assertRaises(ValueError) as uncached:
                store.read_cards(self.root, ws=self.ws)
            with self.assertRaises(ValueError) as cached:
                store.read_cards(self.root, cache=self.cache, ws=self.ws)
        self.assertEqual(str(cached.exception), str(uncached.exception))

    def test_kind_filter_does_not_evict_other_cards(self) -> None:
        notes = store.read_cards(self.root, "note", cache=self.cache, ws=self.ws)
        self.assertEqual([card.path.name for card in notes], [NOTE])
        read_all = lambda: store.read_cards(self.root, cache=self.cache, ws=self.ws)
        self.assertEqual(self.count_parses(read_all), 2)
        self.assert_equivalent()
        self.assertEqual(self.count_parses(read_all), 0)


class DashboardCacheTests(unittest.TestCase):
    def test_dashboard_payload_sees_a_card_changed_between_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            shutil.copy2(SOURCE_ROOT / "templates" / "settings.yaml", root / "settings.yaml")
            shutil.copy2(SOURCE_ROOT / "mt", root / "mt")
            shutil.copytree(SOURCE_ROOT / "scripts", root / "scripts")
            shutil.copytree(SOURCE_ROOT / "tools", root / "tools")
            (root / "cards").mkdir()
            shutil.copy2(EXAMPLES / TASK, root / "cards" / TASK)
            source = f'''
import sys
sys.path.insert(0, 'tools')
import taskweb
from pathlib import Path
ws, cache = taskweb.Workspace.resolve(), taskweb.store.CardCache()
titles = lambda: [task['title'] for task in taskweb.dashboard_payload(ws, cache)['tasks']]
print(titles()[0])
path = Path('cards') / {TASK!r}
path.write_text(path.read_text(encoding='utf-8').replace('Fix per-target metric aggregation', 'Changed title'), encoding='utf-8')
print(titles()[0])
'''
            result = subprocess.run([sys.executable, "-c", source], cwd=root, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\n")[:2], ["Fix per-target metric aggregation", "Changed title"])


if __name__ == "__main__":
    unittest.main()
