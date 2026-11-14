"""Drafts use logical bodies and retain canonical card ownership/CAS guarantees."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'tests'))
sys.path.insert(0, str(SOURCE / 'scripts'))
import drafts
import support


class CardDraftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'workspace'
        self.root.mkdir()
        shutil.copytree(SOURCE / 'scripts', self.root / 'scripts', ignore=shutil.ignore_patterns())
        shutil.copy2(SOURCE / 'mt', self.root / 'mt')
        (self.root / 'mt-policy.yaml').write_text((SOURCE / 'mt-policy.yaml').read_text().replace('demo_workspace: true', 'demo_workspace: false'))
        (self.root / 'cards').mkdir()

    def run_mt(self, *args, data=None, actor='test-run'):
        return support.run_cli(self.root, *args, input_bytes=data, env={**os.environ, 'MT_USERNAME': actor}, text=False)

    def json(self, *args, data=None, actor='test-run'):
        result = self.run_mt(*args, data=data, actor=actor)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def test_draft_serializer_round_trips_nested_fields_and_multiline_strings(self):
        metadata = dict(id='11111111-1111-4111-8111-111111111111', kind='note', mode='new', revision=1,
                        created='2026-11-10T10:00:00', updated='2026-11-10T10:00:00', title='Draft title',
                        capture_id='22222222-2222-4222-8222-222222222222',
                        tags=['research', 'quote: # source'], instructions='Keep original text.\n\n## Guidance')
        path = self.root / 'drafts' / (metadata['id'] + '.md')
        path.parent.mkdir()
        body = '## Content\r\n\r\n```python\r\nunfinished'
        path.write_bytes(drafts.serialize(metadata, body))
        actual = drafts.read_path(path)
        for field, value in metadata.items():
            self.assertEqual(actual[field], value)
        self.assertEqual(actual['body'], body)

    def test_body_edit_draft_applies_without_changing_properties_or_activity(self):
        for kind in ('task', 'note'):
            with self.subTest(kind=kind):
                created = self.json('add', '--kind', kind, '--title', 'Draft target '+kind, '--file', '-', '--json', data=b'Original body')
                self.json('comment', created['ref'], '-m', 'Keep history', '--json')
                before = self.json('read', created['ref'], '--json')
                draft = self.json('draft', 'create', kind, '--target', created['ref'], '--json')
                self.assertEqual(draft['body'], 'Original body')
                self.assertEqual(draft['origin'], 'card-body')
                proposed = '## Review\n\n| A | B |\n| - | - |\n| 1 |\n\n```python\nunclosed'
                edited = self.json('draft', 'edit', draft['id'], '--file', '-', '--expected-revision', str(draft['revision']), '--json', data=proposed.encode())
                self.json('draft', 'save', draft['id'], '--expected-revision', str(edited['revision']), '--expected-hash', edited['content_hash'], '--json')
                after = self.json('read', created['ref'], '--json')
                self.assertEqual(after['body'], proposed)
                self.assertEqual({k:v for k,v in before['properties'].items() if k!='updated'}, {k:v for k,v in after['properties'].items() if k!='updated'})
                self.assertFalse(Path(edited['path']).exists())

    def test_new_note_draft_promotion_preserves_shared_properties(self):
        draft = drafts.save(self.root, draft_id=None, fields={"kind":"note", "mode":"new", "title":"Research draft",
            "capture_id":"22222222-2222-4222-8222-222222222222", "created":"2026-11-10T10:00:00",
            "priority":"high", "project":"research", "topic":"work", "tags":["reading"],
            "due":"2026-12-01", "due_kind":"reminder", "instructions":"Keep sources."},
            body="## Review\n\nSaved prose.", expected_revision=0, now="2026-11-10T10:00:00")
        receipt = self.json('draft','save',draft['id'],'--expected-revision',str(draft['revision']),
                            '--expected-hash',draft['content_hash'],'--json')
        saved = self.json('read',receipt['ref'],'--json')
        for field in ('priority','project','topic','tags','due','due_kind'):
            self.assertEqual(saved['properties'][field], draft[field])
        self.assertEqual(saved['properties']['processing_instructions'], 'Keep sources.')
        self.assertEqual(saved['body'], draft['body'])

    def test_claimed_task_draft_requires_current_owner(self):
        created = self.json('add', 'Original', '--title', 'Owned task', '--status', 'doing', '--by', 'owner', '--json', actor='owner')
        draft = self.json('draft', 'create', 'task', '--target', created['ref'], '--json', actor='other')
        edited = self.json('draft', 'edit', draft['id'], '--file', '-', '--expected-revision', str(draft['revision']), '--json', data=b'Unauthorized body', actor='other')
        before = (self.root / created['path']).read_bytes()
        rejected = self.run_mt('draft', 'save', draft['id'], '--expected-revision', str(edited['revision']), '--expected-hash', edited['content_hash'], actor='other')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b'claimed by', rejected.stderr)
        self.assertEqual((self.root / created['path']).read_bytes(), before)
        self.assertTrue(Path(edited['path']).exists())

    def test_stale_target_preserves_draft_and_external_edit(self):
        created = self.json('note', 'Original', '--title', 'Stale target', '--json')
        draft = self.json('draft', 'create', 'note', '--target', created['ref'], '--json')
        self.json('edit', created['ref'], '--body', '--append', '--file', '-', '--json', data=b'External addition')
        before = (self.root / created['path']).read_bytes()
        rejected = self.run_mt('draft', 'save', draft['id'], '--expected-revision', str(draft['revision']), '--expected-hash', draft['content_hash'])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b'target changed', rejected.stderr)
        self.assertEqual((self.root / created['path']).read_bytes(), before)
        self.assertTrue(Path(draft['path']).exists())


if __name__ == '__main__':
    unittest.main()
