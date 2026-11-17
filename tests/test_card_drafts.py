"""Drafts use logical bodies and retain canonical card ownership/CAS guarantees."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
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
        metadata = dict(id='11111111-1111-4111-8111-111111111111', kind='note', mode='new',
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

    def test_new_note_draft_promotion_preserves_shared_properties(self):
        draft = drafts.save(self.root, draft_id=None, fields={"kind":"note", "mode":"new", "title":"Research draft",
            "capture_id":"22222222-2222-4222-8222-222222222222", "created":"2026-11-10T10:00:00",
            "priority":"high", "project":"research", "topic":"work", "tags":["reading"],
            "due":"2026-12-01", "due_kind":"reminder", "instructions":"Keep sources."},
            body="## Review\n\nSaved prose.", now="2026-11-10T10:00:00")
        receipt = self.json('draft','save',draft['id'],
                            '--expected-hash',draft['content_hash'],'--json')
        saved = self.json('read',receipt['ref'],'--json')
        for field in ('priority','project','topic','tags','due','due_kind'):
            self.assertEqual(saved['properties'][field], draft[field])
        self.assertEqual(saved['properties']['processing_instructions'], 'Keep sources.')
        self.assertEqual(saved['body'], draft['body'])


if __name__ == '__main__':
    unittest.main()
