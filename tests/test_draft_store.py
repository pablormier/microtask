from pathlib import Path
import shutil
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tools/web/js/draft_store.js"
HARNESS = r"""
const assert = require('assert/strict');
const entries = new Map();
global.localStorage = {
  get length() { return entries.size; },
  key: index => Array.from(entries.keys())[index] || null,
  getItem: key => entries.get(key) || null,
  setItem: (key, value) => entries.set(key, value),
  removeItem: key => entries.delete(key),
};
const workspace = {workspaceKey:'workspace-a',apiToken:'token',available:true};
const notifications = [];
function makeStore(options = {}) {
  return createDraftStore({
    context: () => workspace,
    findTarget: () => ({ref:'@target'}),
    captureFields: () => ({}),
    onStatus: status => notifications.push(status),
    onWarning: warning => notifications.push(warning),
    ...options,
  });
}
function makeDraft(id = 'draft-a') {
  return {id,kind:'note',mode:'edit',target:'cards/a.md',ref:'@target',body:'new text',title:'',
    revision:1,content_hash:'a'.repeat(64),base_hash:'b'.repeat(64),base_updated:'2026-10-01',
    origin:'card-body',_savedBody:'old text',_savedTitle:''};
}
function savedResponse(payload) {
  return {ok:true,json:async () => ({saved_draft:{...payload,revision:payload.expected_revision+1,
    content_hash:String(payload.expected_revision+1).repeat(64)}})};
}
"""


class DraftStoreTests(unittest.TestCase):
    def run_store(self, scenario: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        script = SOURCE.read_text() + HARNESS + "\n(async () => {\n" + scenario + "\n})().catch(error => {console.error(error);process.exitCode=1;});"
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
store.saveLocal(draft);
const first = store.save(draft);
draft.body = 'latest text'; store.saveLocal(draft);
const finished = store.finish(draft);
release(); await first; const result = await finished;
assert.equal(result.saved_ref,'@target');
assert.deepEqual(calls.map(call=>call.path),['/api/draft-save','/api/draft-save','/api/draft-finish']);
assert.equal(calls[1].payload.body,'latest text');
assert.equal(calls[1].payload.expected_hash,'2'.repeat(64));
assert.equal(calls[2].payload.expected_revision,3);
assert.equal(calls[2].payload.expected_hash,'3'.repeat(64));
assert.equal(entries.has(store.storageKey(null,draft.id)),false);
assert.equal(store.wasDismissed(draft.id),true);
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

    def test_network_failure_preserves_local_text_and_blocks_promotion(self) -> None:
        self.run_store(r"""
const store=makeStore(), draft=makeDraft(); const paths=[];
store.saveLocal(draft);
global.fetch=async path=>{paths.push(path);throw new Error('offline');};
assert.equal(await store.save(draft),false);
assert.equal(await store.finish(draft),null);
assert.equal(draft.revision,1);
assert.equal(draft._orphanedLocal,undefined);
assert.equal(JSON.parse(entries.get(store.storageKey(null,draft.id))).body,'new text');
assert.equal(paths.includes('/api/draft-finish'),false);
assert.ok(notifications.some(value=>value.includes('local recovery copy')));
""")

    def test_conflicting_draft_is_recovered_without_overwriting_canonical(self) -> None:
        self.run_store(r"""
for(const status of [409,404]) {
  const draft=makeDraft('draft-'+status);
  const canonical={...draft,body:'other device text',revision:2,content_hash:'c'.repeat(64)};
  const store=makeStore({initialDrafts:[canonical]});
  global.fetch=async()=>({ok:false,status,json:async()=>({error:'Draft changed elsewhere'})});
  assert.equal(await store.save(draft),false);
  assert.equal(draft._orphanedLocal,true);
  assert.equal(store.drafts.find(row=>row.id===draft.id).body,'other device text');
  assert.equal(store.drafts.find(row=>row._orphanedLocal).body,'new text');
  assert.equal(await store.finish(draft),null);
  const posted=[];
  global.fetch=async(_path,options)=>{const payload=JSON.parse(options.body);posted.push(payload);return savedResponse(payload);};
  await store.recover(draft,'recovered-'+status,'2026-10-16T12:00:00');
  assert.equal(posted[0].base_hash,'b'.repeat(64));
  assert.equal(posted[0].expected_revision,0);
  assert.equal(store.drafts.find(row=>row.id===draft.id).body,'other device text');
  assert.equal(store.drafts.some(row=>row._orphanedLocal),false);
  assert.equal(store.wasDismissed(draft.id),true);
}
""")

    def test_target_conflict_keeps_workspace_draft_editable(self) -> None:
        self.run_store(r"""
const draft=makeDraft(); draft._savedBody=draft.body;
const store=makeStore({initialDrafts:[draft]});
global.fetch=async()=>({ok:false,status:409,json:async()=>({error:'target changed since the draft was created'})});
await assert.rejects(store.finish(draft),error=>error.conflict&&error.targetConflict);
assert.equal(draft._orphanedLocal,undefined);
assert.equal(store.drafts.length,1);
assert.equal(store.wasDismissed(draft.id),false);
""")

    def test_failed_draft_does_not_block_another_saved_draft(self) -> None:
        self.run_store(r"""
const store=makeStore(), failing=makeDraft('failing'), ready=makeDraft('ready');
ready._savedBody=ready.body;
global.fetch=async()=>{throw new Error('offline');};
assert.equal(await store.save(failing),false);
global.fetch=async(path,options)=>{
  assert.equal(path,'/api/draft-finish');
  assert.equal(JSON.parse(options.body).id,'ready');
  return {ok:true,json:async()=>({saved_ref:'@ready'})};
};
assert.equal((await store.finish(ready)).saved_ref,'@ready');
""")

    def test_reload_restores_divergent_text_and_closed_ids_stay_closed(self) -> None:
        self.run_store(r"""
const draft=makeDraft(), store=makeStore(); store.saveLocal(draft);
const canonical={...draft,body:'other text',revision:2,content_hash:'c'.repeat(64)};
const reloaded=makeStore({initialDrafts:[canonical]});
reloaded.restoreLocal();
const recovery=reloaded.drafts.find(row=>row._orphanedLocal);
assert.equal(recovery.body,'new text');
reloaded.dismiss(recovery);
store.saveLocal(draft); // stale tab writes after dismissal
const next=makeStore({initialDrafts:[canonical]}); next.restoreLocal();
assert.equal(next.drafts.length,1);
assert.equal(next.drafts[0].body,'other text');
assert.equal(entries.has(next.storageKey(null,draft.id)),false);
""")

    def test_matching_edit_recovery_ignores_empty_server_metadata_defaults(self) -> None:
        self.run_store(r"""
const local=makeDraft(); local._savedBody=local.body;
const store=makeStore(); store.saveLocal(local);
const canonical={...local,status:null,priority:null,type:null,project:null,topic:null,tags:[],due:null,due_kind:null};
const reloaded=makeStore({initialDrafts:[canonical]}); reloaded.restoreLocal();
assert.equal(reloaded.drafts.length,1);
assert.equal(reloaded.drafts[0].id,local.id);
local.body='Different local text'; store.saveLocal(local);
const divergent=makeStore({initialDrafts:[canonical]}); divergent.restoreLocal();
assert.equal(divergent.drafts.length,2);
assert.equal(divergent.drafts[1]._orphanedLocal,true);
assert.equal(divergent.drafts[1].body,'Different local text');
""")

    def test_legacy_browser_drafts_are_not_imported_after_cutover(self) -> None:
        self.run_store(r"""
const oldKey='mt-composer-draft-v1:workspace-a';
entries.set(oldKey,JSON.stringify({version:1,workspaceKey:'workspace-a',mode:'edit',ref:'@target',
  text:'legacy whole-card text',expectedHash:'d'.repeat(64)}));
const store=makeStore(); store.restoreLocal();
assert.equal(store.drafts.length,0);
assert.equal(entries.has(oldKey),true);
""")

    def test_unavailable_or_other_workspace_drafts_are_not_sent(self) -> None:
        self.run_store(r"""
const store=makeStore(), draft=makeDraft();
global.fetch=async()=>{throw new Error('must not send');};
workspace.available=false;
assert.equal(await store.save(draft),false);
workspace.available=true; draft.workspaceKey='workspace-b';
assert.equal(await store.save(draft),false);
store.saveLocal(draft);
assert.equal(JSON.parse(entries.get(store.storageKey('workspace-b',draft.id))).body,'new text');
""")
