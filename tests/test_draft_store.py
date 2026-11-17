from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ROOT / "tools/web/js/card_helpers.js",
    ROOT / "tools/web/js/draft_store.js",
]
HARNESS = r"""
const assert = require('assert/strict');
const workspace = {workspaceKey:'workspace-a',apiToken:'token',available:true};
const notifications = [];
function makeStore(options = {}) {
  return createDraftStore({
    context: () => workspace,
    captureFields: () => ({}),
    onStatus: status => notifications.push(status),
    onWarning: warning => notifications.push(warning),
    ...options,
  });
}
function makeDraft(id = 'draft-a') {
  return {
    id, kind:'note', state:'dirty', body:'new text', title:'',
    capture_id:'22222222-2222-4222-8222-222222222222', created:'2026-10-01T10:00:00',
    content_hash:'a'.repeat(64), _saved:'before this test',
  };
}
let hashSeq = 1;
function savedResponse(payload) {
  hashSeq += 1;
  return {ok:true,json:async () => ({saved_draft:{...payload,
    content_hash:String(hashSeq % 10).repeat(64)}})};
}
"""


class DraftStoreTests(unittest.TestCase):
    def run_store(self, scenario: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        script = "\n".join(path.read_text() for path in SOURCES) + HARNESS + "\n(async () => {\n" + scenario + "\n})().catch(error => {console.error(error);process.exitCode=1;});"
        result = subprocess.run([node], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pending_edits_flush_before_finish_with_latest_guards(self) -> None:
        self.run_store(r"""
const store = makeStore(); const draft = makeDraft(); const calls = [];
let release;
global.fetch = (path, options) => {
  const payload = JSON.parse(options.body); calls.push({path,payload});
  if (calls.length === 1) return new Promise(resolve => {release=()=>resolve(savedResponse(payload));});
  if (path === '/api/draft-finish') return Promise.resolve({ok:true,json:async()=>({saved_ref:'@target'})});
  return Promise.resolve(savedResponse(payload));
};
const first = store.save(draft);
assert.equal(draft.state, 'saving');
draft.body = 'latest text';
const finished = store.finish(draft);
release(); await first; const result = await finished;
assert.equal(result.saved_ref,'@target');
assert.equal(draft.state, 'closed');
assert.deepEqual(calls.map(call=>call.path),['/api/draft-save','/api/draft-save','/api/draft-finish']);
assert.equal(calls[1].payload.body,'latest text');
assert.equal(calls[1].payload.expected_hash,'2'.repeat(64));
assert.equal(calls[2].payload.expected_hash,'3'.repeat(64));
assert.equal(store.drafts.length,0);
""")

    def test_store_instances_do_not_share_queues_or_initial_rows(self) -> None:
        self.run_store(r"""
const initial = [];
const first = makeStore({initialDrafts:initial}), second = makeStore({initialDrafts:initial});
const a=makeDraft(), b=makeDraft(); b.body='other store text';
const requests=[];
global.fetch = (_path,options)=>new Promise(resolve=>requests.push({payload:JSON.parse(options.body),resolve}));
const savingA=first.save(a), savingB=second.save(b);
assert.equal(requests.length,2);
for(const request of requests) request.resolve(savedResponse(request.payload));
await Promise.all([savingA,savingB]);
assert.equal(first.drafts[0].body,'new text');
assert.equal(second.drafts[0].body,'other store text');
assert.equal(initial.length,0);
""")

    def test_network_failure_keeps_dirty_state_and_blocks_finish(self) -> None:
        self.run_store(r"""
const store=makeStore(), draft=makeDraft(); const paths=[];
global.fetch=async path=>{paths.push(path);throw new Error('offline');};
assert.equal(await store.save(draft),false);
assert.equal(draft.state,'dirty');
assert.equal(await store.finish(draft),null);
assert.equal(draft.content_hash,'a'.repeat(64));
assert.equal(paths.includes('/api/draft-finish'),false);
assert.ok(notifications.some(value=>value.includes('Workspace save failed')));
""")

    def test_conflict_sets_conflict_state_and_save_as_new_creates_fresh_draft(self) -> None:
        self.run_store(r"""
for(const status of [409,404]) {
  const draft=makeDraft('draft-'+status);
  const store=makeStore({initialDrafts:[draft]});
  global.fetch=async()=>({ok:false,status,json:async()=>({error:'Draft changed elsewhere'})});
  assert.equal(await store.save(draft),false);
  assert.equal(draft.state,'conflict');
  assert.equal(store.drafts.length,1);
  assert.equal(store.drafts[0],draft);
  assert.equal(await store.finish(draft),null);
  assert.ok(notifications.some(value=>value.includes('This draft changed since you opened it')));
  const posted=[];
  global.fetch=async(_path,options)=>{const payload=JSON.parse(options.body);posted.push(payload);return savedResponse(payload);};
  const saved = await store.saveAsNew(draft,'new-'+status,'2026-10-16T12:00:00');
  assert.equal(posted[0].expected_hash,undefined);
  assert.equal(posted[0].id,'new-'+status);
  assert.equal(posted[0].capture_id,'new-'+status);
  assert.equal(saved.state,'clean');
  assert.equal(draft.state,'closed');
  assert.equal(store.drafts.length,1);
  assert.equal(store.drafts[0].id,'new-'+status);
}
""")

    def test_unavailable_or_other_workspace_drafts_are_not_sent(self) -> None:
        self.run_store(r"""
const calls=[];
global.fetch=async path=>{calls.push(path);return savedResponse({});};
const store=makeStore();
const other=makeDraft('other-ws'); other.workspaceKey='workspace-b';
assert.equal(await store.save(other),false);
workspace.available=false;
assert.equal(await store.save(makeDraft('offline-ws')),false);
assert.equal(calls.length,0);
""")

    def test_save_during_inflight_save_resends_latest_text_and_waits_for_it(self) -> None:
        self.run_store(r"""
const store = makeStore();
const draft = makeDraft('draft-q');
const releases = [];
const payloads = [];
global.fetch = (_path, options) => new Promise(resolve => {
  const payload = JSON.parse(options.body);
  payloads.push(payload);
  releases.push(() => resolve(savedResponse(payload)));
});
const first = store.save(draft);
draft.body = 'queued edit';
const second = store.save(draft);
let secondDone = false;
second.then(() => { secondDone = true; });
assert.equal(payloads.length, 1);
releases[0]();
await new Promise(resolve => setTimeout(resolve, 0));
// The second save waits for a second request carrying the latest text and the new hash.
assert.equal(secondDone, false);
assert.equal(payloads.length, 2);
assert.equal(payloads[1].body, 'queued edit');
assert.equal(payloads[1].expected_hash, draft.content_hash);
releases[1]();
assert.equal(await second, true);
assert.equal(await first, true);
assert.equal(store.hasChanges(draft), false);
""")

    def test_discard_cancels_pending_save_without_flushing_new_edits_to_server(self) -> None:
        self.run_store(r"""
const store = makeStore();
const draft = makeDraft('draft-d');
const calls = [];
let releaseSave;
global.fetch = (path, options) => {
  const payload = JSON.parse(options.body);
  calls.push({ path, payload });
  if (calls.length === 1) return new Promise(resolve => { releaseSave = () => resolve(savedResponse(payload)); });
  return Promise.resolve({ ok: true, json: async () => ({ message: 'Draft discarded' }) });
};
const first = store.save(draft);
draft.body = 'edited while saving';
const discarded = store.discard(draft);
releaseSave();
await first;
await discarded;
assert.deepEqual(calls.map(c => c.path), ['/api/draft-save', '/api/draft-discard']);
assert.equal(calls[1].payload.expected_hash, '2'.repeat(64));
assert.equal(draft.state, 'closed');
assert.equal(store.drafts.length, 0);
""")


if __name__ == "__main__":
    unittest.main()
