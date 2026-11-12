from pathlib import Path
import os
import shutil
import subprocess
import unittest


HELPERS = Path(__file__).resolve().parents[1] / "tools/web/js/card_helpers.js"


class CardHelperTests(unittest.TestCase):
    def run_helpers(self, scenario: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        result = subprocess.run(
            [node], input='const assert = require("assert/strict");\n' + HELPERS.read_text() + scenario,
            text=True, capture_output=True, env={**os.environ, "TZ": "Europe/London"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_date_boundaries_and_progress_without_browser_state(self) -> None:
        self.run_helpers(r"""
const now = new Date('2026-03-28T12:00:00');
assert.equal(parseCardDueDate('2026-03-28').getHours(), 23);
assert.equal(cardDueDayDifference(parseCardDueDate('2026-03-30'), now), 2);
assert.equal(formatCardDate('invalid'), 'invalid');
assert.equal(formatCardDate(null), '—');
assert.equal(formatTimeUntil(new Date(now.getTime() + 90 * 60000), now.getTime()), '01h 30m left');
assert.equal(formatTimeUntil(new Date(now.getTime() - 60000), now.getTime()), 'overdue by 00h 01m');
assert.equal(elapsedPercentage(100, 200, 150), 50);
assert.equal(elapsedPercentage(100, 200, 250), 100);
assert.equal(elapsedPercentage(100, 100, 99), 0);
assert.equal(elapsedPercentage('invalid', 200, 150), 0);
assert.equal(formatTrackedDuration(-1), '00h 00m 00s');
assert.equal(formatTrackedDuration(3723.9), '01h 02m 03s');
""")

    def test_metadata_defaults_and_tags_are_independent_copies(self) -> None:
        self.run_helpers(r"""
const config = {task_defaults:{status:'active',priority:'high',type:'idea'}};
const source = {tags:['one, two','say "hello"','line\nbreak'],due:'2026-12-01T09:30:00',due_kind:'target'};
const first = normalizeMetadataValue(source,config);
const second = normalizeMetadataValue(source,config);
assert.equal(first.status,'active'); assert.equal(first.priority,'high'); assert.equal(first.type,'idea');
assert.deepEqual(parseMetadataTags(formatMetadataTags(first.tags)), source.tags);
first.tags.push('changed');
assert.deepEqual(second.tags,source.tags);
assert.deepEqual(parseMetadataTags('a, a, "b, c"'),['a','b, c']);
assert.equal(normalizeMetadataValue(null,{}).status,'inbox');
assert.equal(normalizeMetadataValue(null,{task_statuses:['waiting']}).status,'waiting');
assert.equal(second.due,source.due); assert.equal(second.due_kind,'target');
""")
