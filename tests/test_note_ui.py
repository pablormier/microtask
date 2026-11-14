from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.taskweb import load_js


ROOT = Path(__file__).resolve().parents[1]


class NoteUiTests(unittest.TestCase):
    def test_unpersisted_capture_survives_every_editor_replacement(self) -> None:
        result = self.run_node(r"""
(() => {
  const app=taskBoard(); app.$nextTick=()=>{}; app.$refs={}; app.scheduleDraftSave=()=>{};
  app.captureComposer.beginNew(app.captureContext());
  const original=app.captureComposer.draft;
  original.text='Only copy'; localStorage.failWrite=true;
  app.captureComposer.persist(app.captureContext());
  app.captureComposer.close(app.captureContext());
  app.beginNewCapture();
  const newBlocked=app.captureComposer.draft===original && app.captureComposer.open && app.hasUnsavedChanges();
  app.beginNewCapture();
  const editBlocked=app.captureComposer.draft===original;
  app.openWorkspaceDraft({id:'other',kind:'note',mode:'new',title:'Other draft',body:'Other text'});
  const draftBlocked=app.captureComposer.draft===original;
  localStorage.failWrite=false;
  app.beginNewCapture();
  console.log(JSON.stringify({newBlocked,editBlocked,draftBlocked,
    resumed:app.captureComposer.draft!==original,
    retained:app.drafts.some(d=>d.id===original.draftId && d.body==='Only copy')}));
})()
""")
        self.assertEqual(result, dict.fromkeys(["newBlocked", "editBlocked", "draftBlocked", "resumed", "retained"], True))

    def test_inline_content_recovery_and_dismissal_keep_target_baseline(self) -> None:
        result = self.run_node(r"""
(async () => {
  location.protocol='http:';
  const results=[];
  for(const kind of ['task','note']) {
    const app=taskBoard(); app.$nextTick=()=>{}; app.$refs={}; app.scheduleDraftSave=()=>{};
    const target={id:kind,ref:'@'+kind,path:kind+'s/item.md',kind,content_hash:'target-hash',body:'# Before'};
    app.tasks=[target]; app.select=item=>{app.selected=item;app.selectedId=item.id;};
    const orphan={id:'old-'+kind+':local-recovery',sourceId:'old-'+kind,storageId:'old-'+kind,
      kind,mode:'edit',origin:'card-body',target:target.path,body:'# My edit',base_hash:'original-baseline',
      base_updated:'2026-10-01',revision:2,content_hash:'stale-hash',_orphanedLocal:true,_localRecovery:true};
    app.drafts=[orphan]; await app.openWorkspaceDraft(orphan);
    const editor=app.contentEditor.draft; let sent;
    global.fetch=async (_path,options)=>{sent=JSON.parse(options.body);
      return {ok:true,json:async()=>({saved_draft:{...sent,id:sent.id,revision:1,content_hash:'fresh-hash'}})};};
    await app.recoverLocalWorkspaceDraft(editor);
    const recovered=app.contentEditor.draft;
    const recovery={kind,fresh:recovered.id!==editor.id,origin:recovered.origin,
      baseline:sent.base_hash,body:app.contentEditor.text,canSave:app.contentEditor.canSave(app.contentContext()),
      oldClosed:app.getDraftStore().wasDismissed(orphan.sourceId)};
    app.contentEditor.close(app.contentContext());
    await app.openWorkspaceDraft({...orphan,id:'dismiss-'+kind,sourceId:'dismiss-'+kind,storageId:'dismiss-'+kind});
    const dismissed=app.contentEditor.draft;
    app.drafts.push({id:'dismiss-'+kind,kind,mode:'edit',body:'# My edit',revision:0});
    app.dismissLocalWorkspaceDraft(dismissed);
    recovery.dismissed=app.contentEditor.draft===null && app.getDraftStore().wasDismissed('dismiss-'+kind)
      && !app.drafts.some(d=>d.id==='dismiss-'+kind);
    results.push(recovery);
  }
  console.log(JSON.stringify({results}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        for row in result["results"]:
            self.assertEqual(row, {"kind": row["kind"], "fresh": True, "origin": "card-body",
                                   "baseline": "original-baseline", "body": "# My edit", "canSave": True,
                                   "oldClosed": True, "dismissed": True})

    def test_content_save_does_not_rebase_properties_changed_by_another_writer(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard(); app.$nextTick = () => {}; app.$refs = {};
  app.scheduleDraftSave = () => {}; app.renderSelectedPreview = () => {};
  app.selected = {id:'task',ref:'@task',path:'cards/task.md',kind:'task',status:'active',
    priority:'none',type:'task',tags:[],project:'Original',content_hash:'base',body:'# Content'};
  app.tasks = [app.selected]; app.selectedId = app.selected.id;
  app.propertyEditor.begin(app.selected, app.taskDefaults);
  app.propertyEditor.form.project = 'My pending project';
  app.contentEditor.begin(app.contentContext());
  app.contentEditor.text = '# Changed content';
  app.contentEditor.persist(app.contentContext());
  app.finishWorkspaceDraft = async () => {
    app.applyPayload({tasks:[{...app.selected,project:'Concurrent project',content_hash:'server-hash'}],
      meta:{interactive:true}});
    app.contentEditor.draft = null;
    return {saved:true};
  };
  await app.contentEditor.save(app.contentContext());
  console.log(JSON.stringify({project:app.propertyEditor.form.project,
    baseline:app.propertyEditor.baselineHash,conflict:app.propertyEditor.conflict,
    canSave:app.propertyEditor.canApply(app.propertyContext())}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        self.assertEqual(result, {"project": "My pending project", "baseline": "base",
                                  "conflict": True, "canSave": False})

    def test_recovered_capture_uses_new_identity_for_storage_and_promotion(self) -> None:
        result = self.run_node(r"""
(async () => {
  location.protocol = 'http:';
  const app = taskBoard(); app.$nextTick = () => {};
  app.scheduleDraftSave = () => {};
  app.captureComposer.beginNew(app.captureContext()); app.captureComposer.draft.text = 'Local divergent text';
  const originalId = app.captureComposer.draft.draftId;
  app.captureComposer.draft.sourceId = originalId; app.captureComposer.draft.storageId = originalId;
  app.captureComposer.draft._orphanedLocal = true;
  const calls = [];
  global.fetch = async (path, options) => {
    const payload = JSON.parse(options.body); calls.push({path,payload});
    return {ok:true,json:async()=>path === '/api/draft-finish'
      ? {saved_ref:'@recovered',tasks:[],meta:{interactive:true}}
      : {saved_draft:{...payload,revision:1,content_hash:'a'.repeat(64)}}};
  };
  await app.recoverLocalWorkspaceDraft(app.captureComposer.draft);
  const recovered = app.captureComposer.draft;
  const newId = recovered.draftId;
  const aligned = ['id','sourceId','storageId','captureId','capture_id'].every(field=>recovered[field]===newId);
  app.captureComposer.persist(app.captureContext());
  const local = store.has(app.noteDraftStorageKey(app.workspaceKey,newId));
  const oldRemoved = !store.has(app.noteDraftStorageKey(app.workspaceKey,originalId));
  const saved = await app.captureComposer.save(app.captureContext());
  console.log(JSON.stringify({aligned,local,oldRemoved,fresh:newId!==originalId,
    saved:!!saved,closed:app.captureComposer.draft===null,finishId:calls[calls.length-1].payload.id,newId}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        for field in ("aligned", "local", "oldRemoved", "fresh", "saved", "closed"):
            self.assertTrue(result[field], field)
        self.assertEqual(result["finishId"], result["newId"])

    def test_property_edits_are_kept_when_navigation_is_cancelled(self) -> None:
        result = self.run_node(r"""
(() => {
  const app = taskBoard(); app.$nextTick = () => {}; app.$refs = {};
  app.selected = {id:"note",kind:"note",priority:"none",tags:[],content_hash:"hash"};
  app.propertyEditor.begin(app.selected, app.taskDefaults); app.propertyEditor.form.project = "Keep this";
  window.confirm = () => false;
  const blocked = !app.canLeaveCard();
  const retained = app.propertyEditor.form.project;
  const unloadWarning = app.hasUnsavedChanges();
  window.confirm = () => true;
  const allowed = app.canLeaveCard();
  console.log(JSON.stringify({blocked,retained,unloadWarning,allowed,reset:!app.propertyEditor.changed()}));
})()
""")
        self.assertEqual(result, {"blocked": True, "retained": "Keep this", "unloadWarning": True, "allowed": True, "reset": True})

    def test_inline_content_keeps_frontmatter_out_of_editor_and_preserves_pending_properties(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard();
  app.interactive = true; app.workspaceKey = "workspace-a";
  app.$nextTick = callback => callback(); app.$refs = {};
  app.scheduleDraftSave = () => {}; app.renderSelectedPreview = () => {};
  app.taskPriorityPolicy = {none: "None", high: "High"};
  app.taskTypePolicy = {task: "Task"};
  const source = '## Review\n\nBefore\n';
  app.selected = {id:"original",title:"Original",ref:"@original",path:"cards/original.md",kind:"task",status:"active",
    priority:"none",type:"task",tags:[],content_hash:"base-hash",body:source};
  app.tasks = [app.selected]; app.selectedId = app.selected.id;
  app.propertyEditor.begin(app.selected, app.taskDefaults);
  app.propertyEditor.form.project = "Pending project";
  app.contentEditor.begin(app.contentContext());
  const hiddenFrontmatter = !app.contentEditor.text.startsWith("---");
  app.contentEditor.text = app.contentEditor.text.replace("Before", "After");
  app.contentEditor.persist(app.contentContext());
  const preservedPrefix = app.contentEditor.draft.body.startsWith('## Review\n\n');
  const propertiesBlocked = !app.propertyEditor.canApply(app.propertyContext());
  const draftId = app.contentEditor.draft.id;
  app.contentEditor.close(app.contentContext()); app.contentEditor.begin(app.contentContext());
  const resumed = app.contentEditor.draft.id === draftId && app.contentEditor.text.includes("After");
  app.finishWorkspaceDraft = async draft => {
    app.applyPayload({tasks:[{...app.selected,content_hash:"saved-hash",body:draft.body}],meta:{interactive:true}});
    app.contentEditor.draft = null;
    return {saved:true};
  };
  await app.contentEditor.save(app.contentContext());
  const saved = {project:app.propertyEditor.form.project,hash:app.propertyEditor.baselineHash,
    conflict:app.propertyEditor.conflict,canSave:app.propertyEditor.canApply(app.propertyContext())};
  app.selected = {id:"note",ref:"@note",path:"cards/note.md",kind:"note",body:"Note before",content_hash:"note-hash",tags:[]};
  app.contentEditor.begin(app.contentContext());
  console.log(JSON.stringify({hiddenFrontmatter,preservedPrefix,propertiesBlocked,resumed,saved,noteText:app.contentEditor.text}));
})().catch(error => { console.error(error); process.exitCode=1; });
""")
        for key in ("hiddenFrontmatter", "preservedPrefix", "propertiesBlocked", "resumed"):
            self.assertTrue(result[key], key)
        self.assertEqual(result["saved"], {"project": "Pending project", "hash": "saved-hash", "conflict": False, "canSave": True})
        self.assertEqual(result["noteText"], "Note before")

    def test_contextual_capture_kind_switching_preserves_input_and_metadata(self) -> None:
        result = self.run_node(r"""
(() => {
  const app = taskBoard();
  app.interactive = true;
  app.workspaceKey = "workspace-a";
  app.$nextTick = () => {};
  app.captureComposer.persist = () => true;
  app.mode = "tasks";
  app.beginNewCapture();
  const tasksTab = {kind: app.captureComposer.draft.kind, title: app.captureComposer.title(), save: app.captureComposer.saveLabel()};
  app.captureComposer.draft.title = "Keep my title";
  app.captureComposer.draft.text = "Keep my text";
  app.captureComposer.draft.instructions = "Keep my instructions";
  app.captureComposer.draft.taskOptions.project = "Keep my project";
  const id = app.captureComposer.draft.captureId;
  app.captureComposer.setKind(app.captureContext(), "note");
  const note = {title: app.captureComposer.title(), save: app.captureComposer.saveLabel()};
  app.captureComposer.setKind(app.captureContext(), "task");
  const preserved = {id: id === app.captureComposer.draft.captureId, title: app.captureComposer.draft.title,
    text: app.captureComposer.draft.text, instructions: app.captureComposer.draft.instructions,
    project: app.captureComposer.draft.taskOptions.project};
  app.captureComposer.saving = true;
  app.captureComposer.setKind(app.captureContext(), "note");
  const saving = {kind: app.captureComposer.draft.kind, label: app.captureComposer.saveLabel()};
  app.captureComposer.saving = false;
  app.mode = "notes";
  app.beginNewCapture();
  const notesDefault = app.captureComposer.draft.kind;
  app.mode = "focus";
  app.beginNewCapture();
  console.log(JSON.stringify({tasksTab, note, preserved, saving, notesDefault, focusDefault: app.captureComposer.draft.kind}));
})()
""")
        self.assertEqual(result["tasksTab"], {"kind": "task", "title": "New task", "save": "Create task"})
        self.assertEqual(result["note"], {"title": "New note", "save": "Create note"})
        self.assertEqual(result["preserved"], {"id": True, "title": "Keep my title", "text": "Keep my text",
                                                "instructions": "Keep my instructions", "project": "Keep my project"})
        self.assertEqual(result["saving"], {"kind": "task", "label": "Saving…"})
        self.assertEqual(result["notesDefault"], "note")
        self.assertEqual(result["focusDefault"], "task")

    def run_node(self, source: str) -> dict:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        script = f"""
const fs = require("fs");
const boardSource = {json.dumps(load_js())};
global.setInterval = () => 0;
const data = [];
const meta = {{ interactive: true, workspace_key: "workspace-a", api_token: "token" }};
const unloadHandlers = [];
const store = new Map();
global.localStorage = {{
  failRead: false, failWrite: false,
  get length() {{ return store.size; }},
  key(index) {{ return Array.from(store.keys())[index] || null; }},
  getItem(key) {{ if (this.failRead) throw new Error("storage blocked"); return store.has(key) ? store.get(key) : null; }},
  setItem(key, value) {{ if (this.failWrite) throw new Error("storage blocked"); store.set(key, value); }},
  removeItem(key) {{ if (this.failWrite) throw new Error("storage blocked"); store.delete(key); }}
}};
global.document = {{
  hidden: false,
  documentElement: {{ dataset: {{}} }},
  getElementById(id) {{
    if (id === "task-data") return {{ textContent: JSON.stringify(data) }};
    if (id === "mt-statuses") return {{ textContent: JSON.stringify([]) }};
    if (id === "mt-config") return {{ textContent: JSON.stringify(meta) }};
    return {{ focus() {{}} }};
  }},
  addEventListener() {{}}
}};
global.window = {{ addEventListener(type, handler) {{ if (type === "beforeunload") unloadHandlers.push(handler); }} }};
global.location = {{ hash: "", pathname: "/", search: "" }};
global.history = {{ state: null, replaceState(state) {{ this.state = state; }}, pushState(state) {{ this.state = state; }} }};
eval(boardSource + "\\n" + 'function makeTestBoard() {{ const app=taskBoard(); app.applyPayload({{tasks:data,meta}}); app.ready=true; app.loading=false; return app; }}' + "\\n" + {json.dumps(source.replace('taskBoard()', 'makeTestBoard()'))});
"""
        result = subprocess.run([node], input=script, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    GROUP_SETUP = r"""
const setup = app => {
  app.statuses = ['inbox', 'active', 'doing', 'done'];
  app.priorityOrder = ['urgent', 'high', 'none'];
  app.dueKinds = ['deadline', 'target'];
  app.maxDoingTasks = 1;
  return app;
};
const card = (id, extra = {}) => ({id, kind: 'task', status: 'active', priority: 'none', project: null, tags: [], title: id, ...extra});
const summary = groups => groups.map(g => [g.key, g.items.map(i => i.id)]);
"""

    def test_status_groups_keep_policy_order_empty_groups_wip_label(self) -> None:
        result = self.run_node(self.GROUP_SETUP + r"""
const app = setup(taskBoard());
app.tasks = [card('a'), card('d1', {status: 'doing'}), card('x1', {status: 'done'}), card('x2', {status: 'done'}),
  card('x3', {status: 'done'}), card('n', {kind: 'note', status: ''})];
app.doingTotal = 1;
const groups = app.groupCards(app.tasks, 'status', {workflow: true});
const plain = app.groupCards(app.tasks, 'status');
console.log(JSON.stringify({keys: groups.map(g => g.key), labels: groups.map(g => g.label), counts: groups.map(g => g.countLabel),
  done: groups[3].items.map(i => i.id), empty: groups[0].empty, over: groups[2].className['is-over-limit'],
  quiet: groups[3].className['is-quiet'], plainKeys: plain.map(g => g.key), plainLabel: plain[1].label,
  plainDone: plain.find(g => g.key === 'done').items.length}));
""")
        self.assertEqual(result["keys"], ["inbox", "active", "doing", "done", ""])
        self.assertEqual(result["labels"][2], "doing (1/1 max)")
        self.assertEqual(result["labels"][4], "Ungrouped")
        self.assertEqual(result["counts"], ["0", "1", "1", "3", "1"])
        self.assertEqual(result["done"], ["x1", "x2", "x3"])
        self.assertEqual(result["empty"], "No tasks.")
        self.assertTrue(result["over"])
        self.assertTrue(result["quiet"])
        self.assertEqual(result["plainKeys"], ["active", "doing", "done", ""])
        self.assertEqual(result["plainDone"], 3)

    def test_other_groups_show_only_used_groups_in_policy_or_alphabetical_order(self) -> None:
        result = self.run_node(self.GROUP_SETUP + r"""
const app = setup(taskBoard());
const items = [card('p1', {priority: 'none', project: 'beta'}), card('p2', {priority: 'high', project: 'Alpha'}),
  card('p3', {priority: 'zzz', project: 'alpha2'}), card('p4', {priority: 'urgent'}), card('p5', {priority: 'aaa', project: 'Alpha'}),
  card('p6', {priority: 'none', due_kind: 'target'}), card('p7', {due_kind: 'deadline'})];
console.log(JSON.stringify({priority: summary(app.groupCards(items, 'priority')),
  priorityLabels: app.groupCards(items, 'priority').map(g => g.label),
  project: summary(app.groupCards(items, 'project')), due: app.groupCards(items, 'due_kind').map(g => [g.key, g.label])}));
""")
        self.assertEqual([key for key, _ in result["priority"]], ["urgent", "high", "none", "aaa", "zzz"])
        self.assertEqual(result["priorityLabels"][2], "Unassigned")
        self.assertEqual(result["project"], [["Alpha", ["p2", "p5"]], ["alpha2", ["p3"]], ["beta", ["p1"]],
                                             ["", ["p4", "p6", "p7"]]])
        self.assertEqual(result["due"], [["deadline", "Deadline"], ["target", "Target"], ["", "Ungrouped"]])

    def test_tag_groups_use_every_tag_and_ungrouped_has_untagged_cards(self) -> None:
        result = self.run_node(self.GROUP_SETUP + r"""
const app = setup(taskBoard());
const items = [card('a', {tags: ['Work', 'home']}), card('b', {tags: ['work']}), card('c'), card('d', {tags: ['home', 'home']})];
console.log(JSON.stringify(summary(app.groupCards(items, 'tag'))));
""")
        self.assertEqual(result, [["home", ["a", "d"]], ["Work", ["a"]], ["work", ["b"]], ["", ["c"]]])

    def test_tasks_and_notes_tabs_have_fixed_kinds_and_archive_has_kind(self) -> None:
        result = self.run_node(self.GROUP_SETUP + r"""
const app = setup(taskBoard());
app.tasks = [card('t', {status: 'active'}), card('n', {kind: 'note', status: ''})];
app.collections = {tasks: ['t'], notes: ['n'], archive: ['t', 'n'], archive_tasks: ['t'], archive_notes: ['n']};
const options = kind => app.groupOptions(kind).map(o => o.value);
const ids = () => summary(app.viewGroups()).filter(([, items]) => items.length);
app.mode = 'tasks'; app.settings.tasks.group = 'none';
const tasks = {kind: app.viewKind(), items: ids()};
app.mode = 'notes'; app.settings.notes.group = 'none';
const notes = {kind: app.viewKind(), items: ids(), options: options(app.viewKind())};
app.mode = 'archive';
const archive = {kind: app.viewKind(), items: ids()};
app.settings.archive.group = 'status'; app.setKind('all');
const all = ids();
app.setKind('note');
console.log(JSON.stringify({tasks, notes, archive, all, noteGroup: app.settings.archive.group,
  taskOptions: options('task'), keys: Object.keys(app.settings.tasks).concat(Object.keys(app.settings.notes))}));
""")
        self.assertEqual(result["tasks"], {"kind": "task", "items": [["", ["t"]]]})
        self.assertEqual(result["notes"]["kind"], "note")
        self.assertEqual(result["notes"]["items"], [["", ["n"]]])
        self.assertNotIn("status", result["notes"]["options"])
        self.assertNotIn("type", result["notes"]["options"])
        self.assertEqual(result["archive"], {"kind": "task", "items": [["", ["t"]]]})
        self.assertEqual(result["all"], [["active", ["t"]], ["", ["n"]]])
        self.assertEqual(result["noteGroup"], "none")
        self.assertIn("status", result["taskOptions"])
        self.assertIn("type", result["taskOptions"])
        self.assertEqual(result["keys"], ["group", "group"])

    def test_due_kind_labels_are_plain_and_unassigned_is_priority_only(self) -> None:
        result = self.run_node(self.GROUP_SETUP + r"""
const app = setup(taskBoard());
console.log(JSON.stringify({due: app.groupLabel('due_kind', 'none', false), kind: app.groupLabel('due_kind', 'target', false),
  priority: app.groupLabel('priority', 'none', false)}));
""")
        self.assertEqual(result, {"due": "None", "kind": "Target", "priority": "Unassigned"})

    def test_view_settings_default_per_tab_persist_and_survive_blocked_storage(self) -> None:
        result = self.run_node(self.GROUP_SETUP + r"""
const defaults = JSON.parse(JSON.stringify(taskBoard().settings));
const app = taskBoard();
app.mode = 'archive'; app.setKind('all'); app.settings.archive.group = 'project';
app.settings.notes.group = 'tag';
app.settings.deadlines.group = 'priority';
app.saveSettings();
const reloaded = taskBoard(); reloaded.loadSettings();
localStorage.setItem('mt-view-settings', JSON.stringify({tasks: {kind: 'note', group: 'status'}, notes: {kind: 'task', group: 'status'}, archive: {kind: 'tasks', group: 'nope'}}));
const sanitized = taskBoard(); sanitized.loadSettings();
localStorage.setItem('mt-view-settings', '{broken');
const broken = taskBoard(); broken.loadSettings();
localStorage.failRead = true; localStorage.failWrite = true;
const blocked = taskBoard(); blocked.loadSettings(); blocked.saveSettings();
console.log(JSON.stringify({defaults, reloaded: reloaded.settings, sanitized: sanitized.settings, broken: broken.settings, blocked: blocked.settings}));
""")
        self.assertEqual(result["defaults"], {
            "tasks": {"group": "status"}, "notes": {"group": "none"},
            "archive": {"kind": "task", "group": "none"}, "deadlines": {"group": "due_kind"}})
        self.assertEqual(result["reloaded"]["archive"], {"kind": "all", "group": "project"})
        self.assertEqual(result["reloaded"]["notes"], {"group": "tag"})
        self.assertEqual(result["reloaded"]["tasks"], result["defaults"]["tasks"])
        self.assertEqual(result["reloaded"]["deadlines"], {"group": "priority"})
        # A stale kind on Tasks or Notes is ignored; an invalid group falls back to the tab default.
        self.assertEqual(result["sanitized"]["tasks"], {"group": "status"})
        self.assertEqual(result["sanitized"]["notes"], {"group": "none"})
        self.assertEqual(result["sanitized"]["archive"], result["defaults"]["archive"])
        self.assertEqual(result["broken"], result["defaults"])
        self.assertEqual(result["blocked"], result["defaults"])

    def test_automatic_refreshes_coalesce_events_without_blocking_search_or_retry(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard();
  let now = 10000;
  Date.now = () => now;
  const events = {};
  window.addEventListener = (name, handler) => { events[name] = handler; };
  document.addEventListener = (name, handler) => { events[name] = handler; };
  app.loadWorkspace = async () => {};
  app.init();
  const requests = [];
  global.fetch = url => new Promise((resolve, reject) => requests.push({url, resolve, reject}));
  const reply = index => requests[index].resolve({ok:true,
    json:async()=>({tasks:[],meta:{},query:app.collectionQuery()})});
  const first = events.focus();
  await events.visibilitychange();
  const coalesced = requests.length === 1;
  reply(0); await first;
  now += 4999;
  await events.focus();
  const recentSkipped = requests.length === 1;
  // Explicit searches bypass the grace period.
  app.filters.text = 'fresh';
  const search = app.refreshItems();
  reply(1); await search;
  now += 5000;
  document.hidden = true; await events.visibilitychange();
  document.hidden = false; app.ready = false; await events.focus();
  const unavailableSkipped = requests.length === 2;
  app.ready = true;
  const automatic = events.focus();
  // A search can supersede an automatic refresh without waiting for it.
  app.filters.text = 'newer';
  const newer = app.refreshItems();
  reply(3); await newer;
  now += 6000;
  await events.focus();
  const staleInFlightSkipped = requests.length === 4 && !app.queryLoading;
  reply(2); await automatic;
  const failed = events.focus();
  requests[4].reject(new Error('offline')); await failed;
  const retry = events.visibilitychange();
  reply(5); await retry;
  const failureRetried = requests.length === 6 && app.itemRequestsInFlight === 0;
  now += 5000;
  app.scheduleQuery(60000);
  await events.focus();
  const debounceSkipped = requests.length === 6;
  clearTimeout(app.queryTimer);
  console.log(JSON.stringify({coalesced,recentSkipped,unavailableSkipped,staleInFlightSkipped,
    failureRetried,debounceSkipped,searchUrl:requests[1].url,newerUrl:requests[3].url}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        for key in ["coalesced", "recentSkipped", "unavailableSkipped", "staleInFlightSkipped",
                    "failureRetried", "debounceSkipped"]:
            self.assertTrue(result[key], key)
        self.assertIn("text=fresh", result["searchUrl"])
        self.assertIn("text=newer", result["newerUrl"])

    def test_collection_requests_reject_stale_successes_and_failures(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard();
  app.$nextTick = () => {};
  const requests = [];
  global.fetch = (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
  const payload = (ids, query) => ({tasks: ids.map(id => ({id,kind:'task',status:'active',title:id})),
    collections:{tasks:ids},query,meta:{}});
  const reply = (request, value) => request.resolve({ok:true,json:async()=>value});
  const old = app.refreshItems();
  app.filters.text = 'new & café';
  const currentQuery = app.collectionQuery();
  const current = app.refreshItems();
  reply(requests[1],payload(['second','first'],currentQuery)); await current;
  reply(requests[0],payload(['old'],{})); await old;
  const ordered = app.collectionItems("tasks").map(item=>item.id);
  const staleError = app.refreshItems();
  app.filters.priority = 'high';
  const newest = app.refreshItems();
  reply(requests[3],payload(['latest'],app.collectionQuery())); await newest;
  requests[2].reject(new Error('stale network failure')); await staleError;
  const post = app.apiPost('/api/move',{ref:'@latest',status:'waiting'});
  const postBody = JSON.parse(requests[4].options.body);
  reply(requests[4],payload([],postBody.query)); await post;
  console.log(JSON.stringify({ordered, url:requests[1].url, latest:app.tasks.map(item=>item.id),
    loading:app.queryLoading, error:app.queryError, query:postBody.query}));
})().catch(error => {console.error(error);process.exitCode=1;});
""")
        self.assertEqual(result["ordered"], ["second", "first"])
        self.assertIn("text=new+%26+caf%C3%A9", result["url"])
        self.assertEqual(result["latest"], [])
        self.assertFalse(result["loading"])
        self.assertEqual(result["error"], "")
        self.assertEqual(result["query"]["priority"], ["high"])

    def test_default_draft_payload_refreshes_active_query_without_unfiltered_flash(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard();
  app.filters.text = 'needle';
  app.collections = {tasks:['keep']};
  const cards = [{id:'keep',kind:'task',status:'active',title:'needle'},
                 {id:'other',kind:'task',status:'active',title:'other'}];
  const query = app.collectionQuery();
  let requested = '';
  global.fetch = async url => {
    requested = url;
    return {ok:true,json:async()=>({tasks:cards,collections:{tasks:['keep']},query,meta:{}})};
  };
  app.applyPayload({tasks:cards,collections:{tasks:['other','keep']},query:{},meta:{}});
  const immediate = app.collectionItems("tasks").map(item=>item.id);
  await new Promise(resolve=>setTimeout(resolve,20));
  console.log(JSON.stringify({immediate,final:app.collectionItems("tasks").map(item=>item.id),
    requested,cards:app.tasks.length,loading:app.queryLoading}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        self.assertEqual(result["immediate"], ["keep"])
        self.assertEqual(result["final"], ["keep"])
        self.assertIn("text=needle", result["requested"])
        self.assertEqual(result["cards"], 2)
        self.assertFalse(result["loading"])

    def test_card_details_load_on_open_ignore_stale_responses_and_retry_safely(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard(); app.$nextTick=()=>{}; app.$refs={}; app.renderSelectedPreview=()=>{};
  const cards=['a','b'].map(id=>({id,ref:'@'+id,path:'cards/'+id+'.md',kind:'task',title:id,
    content_hash:'base-'+id,status:'active',priority:'none',type:'task',tags:[]}));
  app.tasks=cards;
  const requests=[];
  global.fetch=(url)=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}));
  const detail=(card,text)=>({...card,body:text,reference_spans:{},references:[]});
  const respond=(index,item,ok=true)=>requests[index].resolve({ok,json:async()=>ok?{item}:{error:'Card unavailable'}});
  const first=app.showCard(cards[0]);
  app.contentEditor.begin(app.contentContext());
  const blocked=app.contentEditor.draft===null;
  const second=app.showCard(cards[1]);
  respond(1,detail(cards[1],'B content')); await second;
  requests[0].reject(new Error('stale failure')); await first;
  const opened={id:app.selected.id,text:app.selected.body};
  await app.applyPayload({tasks:cards,meta:{}});
  const unchangedRequests=requests.length;
  app.propertyEditor.form.project='My pending edit';
  const changed={...cards[1],content_hash:'new-b',status:'waiting'};
  const refreshing=app.applyPayload({tasks:[cards[0],changed],meta:{}});
  respond(2,detail(changed,'B changed'));await refreshing;
  const conflict={value:app.propertyEditor.form.project,conflict:app.propertyEditor.conflict,body:app.selected.body,destination:app.moveDestination};
  app.propertyEditor.begin(app.selected,app.taskDefaults);
  const closing=app.showCard(cards[0]);app.clearSelection();respond(3,detail(cards[0],'Too late'));await closing;
  const stayedClosed=app.selected===null;
  const failure=app.showCard(cards[1]);respond(4,null,false);await failure;
  app.contentEditor.begin(app.contentContext());
  const failed={error:app.detailError,blocked:app.contentEditor.draft===null};
  const retry=app.loadSelectedDetails(true);respond(5,detail(cards[1],'Recovered'));await retry;
  app.contentEditor.begin(app.contentContext());
  console.log(JSON.stringify({blocked,opened,unchangedRequests,conflict,stayedClosed,failed,
    recovered:app.contentEditor.text,loading:app.detailLoading,error:app.detailError,
    summaries:app.tasks.every(card=>!('body' in card)),url:requests[0].url}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["opened"], {"id": "b", "text": "B content"})
        self.assertEqual(result["unchangedRequests"], 2)
        self.assertEqual(result["conflict"], {"value": "My pending edit", "conflict": True, "body": "B changed", "destination": "waiting"})
        self.assertTrue(result["stayedClosed"])
        self.assertEqual(result["failed"], {"error": "Card unavailable", "blocked": True})
        self.assertEqual(result["recovered"], "Recovered")
        self.assertFalse(result["loading"])
        self.assertEqual(result["error"], "")
        self.assertTrue(result["summaries"])
        self.assertEqual(result["url"], "/api/item?id=a")

    def test_reference_target_changes_refresh_details_without_losing_pending_properties(self) -> None:
        result = self.run_node(r"""
(async()=>{
  const app=taskBoard();app.$nextTick=()=>{};app.$refs={};app.renderSelectedPreview=()=>{};
  const a={id:'a',ref:'@a',kind:'task',title:'A',content_hash:'same',status:'active',priority:'none',type:'task'};
  const b={id:'b',ref:'@b',kind:'note',title:'B'};
  app.tasks=[a,b];
  let calls=0;
  global.fetch=async()=>({ok:true,json:async()=>({item:{...a,body:'See @b',
    references:[{ref:'@b',state:++calls===1?'resolved':'missing'}]}})});
  await app.showCard(a);
  app.propertyEditor.form.project='Pending';
  await app.applyPayload({tasks:[a],meta:{reference_revision:'b-removed'}});
  console.log(JSON.stringify({calls,state:app.selected.references[0].state,
    project:app.propertyEditor.form.project,hash:app.propertyEditor.baselineHash}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        self.assertEqual(result,{"calls":2,"state":"missing","project":"Pending","hash":"same"})

    def test_workspace_startup_retries_and_resolves_card_after_mount(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard();
  app.ready = false;
  app.tasks = [];
  const events = [];
  app.$nextTick = callback => { events.push("mount"); callback(); };
  app.readCardLocation = () => events.push("navigation:" + app.tasks.length);
  let fail = true;
  global.fetch = async path => {
    events.push(path.split("?")[0]);
    if (path.startsWith("/api/items?")) return {
      ok: !fail, json: async () => fail ? { error: "Workspace unavailable" }
        : { tasks: [{ id: "loaded-card", kind: "note", title: "Loaded" }], statuses: [],
            meta: { ...meta, demo: true, show_year_progress: false, show_deadline_progress: false, deadline_progress_start: "created" } },
    };
    return { ok: true, json: async () => ({ drafts: [] }) };
  };
  await app.loadWorkspace();
  const failed = { ready: app.ready, loading: app.loading, error: app.loadError, cards: app.tasks.length };
  fail = false;
  await app.loadWorkspace();
  console.log(JSON.stringify({ failed, ready: app.ready, loading: app.loading, error: app.loadError, demo: app.demo, yearProgress: app.showYearProgress, deadlineProgress: app.showDeadlineProgress, events }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertEqual(result["failed"], {"ready": False, "loading": False, "error": "Workspace unavailable", "cards": 0})
        self.assertTrue(result["ready"])
        self.assertFalse(result["loading"])
        self.assertEqual(result["error"], "")
        self.assertTrue(result["demo"])
        self.assertFalse(result["yearProgress"])
        self.assertFalse(result["deadlineProgress"])
        self.assertEqual(result["events"], ["/api/items", "/api/items", "/api/drafts", "mount", "navigation:1"])

    def test_workspace_draft_saves_are_per_id_and_inflight_text_is_queued(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard();
  app.$nextTick = callback => callback();
  app.interactive = true;
  app.workspaceKey = "workspace-a";
  app.apiToken = "token";
  location.protocol = "http:";
  const pending = [];
  global.fetch = (path, options) => new Promise(resolve => {
    pending.push({ payload: JSON.parse(options.body), resolve });
  });
  const makeDraft = (id, body) => ({
    id, kind: "note", mode: "new", capture_id: id, created: "2026-10-15T10:00:00",
    revision: 1, content_hash: "1".repeat(64), body, title: "",
    _savedBody: "before", _savedTitle: "",
  });
  const a = makeDraft("draft-a", "A first");
  const b = makeDraft("draft-b", "B text");
  const reply = (request, revision, hash) => request.resolve({
    ok: true,
    json: async () => ({ saved_draft: { ...request.payload, revision, updated: "2026-10-15T10:01:00", content_hash: hash } }),
  });
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  app.scheduleDraftSave(a, 0);
  await wait(10);
  a.body = "A newest";
  app.scheduleDraftSave(a, 0);
  app.scheduleDraftSave(b, 0);
  await wait(10);
  const firstRequests = pending.map(request => ({ id: request.payload.id, body: request.payload.body }));
  reply(pending[1], 2, "2".repeat(64));
  reply(pending[0], 2, "2".repeat(64));
  await wait(10);
  const queuedRequest = pending[2] && { id: pending[2].payload.id, body: pending[2].payload.body, hash: pending[2].payload.expected_hash };
  if (pending[2]) reply(pending[2], 3, "3".repeat(64));
  await wait(10);
  console.log(JSON.stringify({ firstRequests, queuedRequest, finalBody: a._savedBody, finalRevision: a.revision }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertEqual([request["id"] for request in result["firstRequests"]], ["draft-a", "draft-b"])
        self.assertEqual(result["firstRequests"][0]["body"], "A first")
        self.assertEqual(result["queuedRequest"]["id"], "draft-a")
        self.assertEqual(result["queuedRequest"]["body"], "A newest")
        self.assertEqual(result["queuedRequest"]["hash"], "2" * 64)
        self.assertEqual(result["finalBody"], "A newest")
        self.assertEqual(result["finalRevision"], 3)

    def test_empty_capture_stays_ephemeral_and_task_options_survive_kind_switches(self) -> None:
        result = self.run_node(r"""
(async () => {
  function makeApp() {
    const app = taskBoard();
    app.$nextTick = callback => callback();
    app.interactive = true;
    app.workspaceKey = "workspace-a";
    app.apiToken = "token";
    app.taskDefaults = { status: "inbox", priority: "none", type: "task" };
    app.taskStatuses = ["inbox", "active", "doing"];
    app.taskPriorityPolicy = { urgent: "Urgent", high: "High", medium: "Medium", low: "Low", none: "None" };
    app.taskTypePolicy = { task: "Action", bug: "Broken" };
    app.taskDueKindPolicy = { deadline: "Hard", reminder: "Resurface", target: "Soft" };
    app.scheduleDraftSave = () => {};
    return app;
  }
  location.protocol = "http:";
  const startCount = store.size;
  let requests = [];
  global.fetch = async (_path, options) => {
    const payload = JSON.parse(options.body);
    requests.push(payload);
    const revision = payload.expected_revision + 1;
    return { ok: true, json: async () => ({ saved_draft: {
      ...payload, id: payload.id, revision, updated: "2026-10-15T10:00:00",
      content_hash: String(revision).repeat(64),
    } }) };
  };

  const empty = makeApp();
  const originalMode = empty.mode;
  empty.captureComposer.beginNew(empty.captureContext());
  const emptyId = empty.captureComposer.draft.draftId;
  empty.captureComposer.setKind(empty.captureContext(), "task");
  empty.captureComposer.close(empty.captureContext());
  const emptyStayedEphemeral = !store.has("mt-note-draft-v2:workspace-a:" + emptyId)
    && !empty.drafts.some(draft => draft.id === emptyId) && empty.captureComposer.draft === null
    && empty.mode === originalMode && requests.length === 0;

  const app = makeApp();
  app.captureComposer.beginNew(app.captureContext());
  app.captureComposer.setKind(app.captureContext(), "task");
  const captureId = app.captureComposer.draft.captureId;
  const created = app.captureComposer.draft.createdAt;
  app.captureComposer.draft.title = "A task capture";
  app.captureComposer.draft.text = "Raw task text\n## Literal heading";
  app.captureComposer.draft.taskOptions = {
    status: "active", priority: "high", type: "bug", project: "custom project",
    topic: "custom topic", tags: ["code", "custom-tag"], due: "2026-12-31T09:45:00", due_kind: "target",
  };
  app.captureComposer.persist(app.captureContext());
  await app.saveWorkspaceDraft(app.captureComposer.draft);
  const first = requests[0];
  app.captureComposer.setKind(app.captureContext(), "note");
  await app.saveWorkspaceDraft(app.captureComposer.draft);
  app.captureComposer.setKind(app.captureContext(), "task");
  await app.saveWorkspaceDraft(app.captureComposer.draft);
  const last = requests[2];
  console.log(JSON.stringify({
    emptyStayedEphemeral,
    startCount,
    firstKind: first.kind,
    switchedKinds: requests.map(request => request.kind),
    identityRetained: requests.every(request => request.capture_id === captureId && request.created === created),
    lastPayload: last,
    metadataRetained: last.status === "active" && last.priority === "high" && last.type === "bug"
      && last.project === "custom project" && last.topic === "custom topic"
      && JSON.stringify(last.tags) === JSON.stringify(["code", "custom-tag"])
      && last.due === "2026-12-31T09:45:00" && last.due_kind === "target",
    bodyRetained: last.body === "Raw task text\n## Literal heading",
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertTrue(result["emptyStayedEphemeral"])
        self.assertEqual(result["firstKind"], "task")
        self.assertEqual(result["switchedKinds"], ["task", "note", "task"])
        self.assertTrue(result["identityRetained"])
        self.assertTrue(result["metadataRetained"], result.get("lastPayload"))
        self.assertTrue(result["bodyRetained"])

    def test_cleared_capture_survives_close_autosave_and_offline_recovery(self) -> None:
        result = self.run_node(r"""
(async () => {
  location.protocol = "http:";
  Object.defineProperty(localStorage, "length", { get: () => store.size });
  localStorage.key = index => Array.from(store.keys())[index];
  const app = taskBoard();
  app.$nextTick = callback => callback();
  app.scheduleDraftSave = () => {};
  const requests = [];
  global.fetch = async (_path, options) => {
    const payload = JSON.parse(options.body);
    requests.push(payload);
    return { ok: true, json: async () => ({ saved_draft: {
      ...payload, revision: payload.expected_revision + 1,
      content_hash: String(payload.expected_revision + 1).repeat(64),
    } }) };
  };
  app.captureComposer.beginNew(app.captureContext());
  const draft = app.captureComposer.draft;
  draft.text = "Text to erase";
  app.captureComposer.persist(app.captureContext());
  await app.saveWorkspaceDraft(draft);
  const previous = { ...app.drafts.find(item => item.id === draft.draftId) };
  draft.text = "";
  app.captureComposer.persist(app.captureContext());
  app.captureComposer.close(app.captureContext());
  const key = app.noteDraftStorageKey(app.workspaceKey, draft.draftId);
  const local = JSON.parse(localStorage.getItem(key));
  const retained = app.captureComposer.draft === draft && local.text === "";

  global.fetch = async () => { throw new Error("offline"); };
  await app.saveWorkspaceDraft(draft);
  app.drafts = [previous];
  app.restoreLocalWorkspaceDrafts();
  const recovery = app.drafts.find(item => item._localRecovery);
  const recoveredEmpty = !!recovery && recovery.body === "";

  global.fetch = async (_path, options) => {
    const payload = JSON.parse(options.body);
    requests.push(payload);
    return { ok: true, json: async () => ({ saved_draft: {
      ...payload, revision: payload.expected_revision + 1, content_hash: "2".repeat(64),
    } }) };
  };
  await app.flushWorkspaceDraft(draft);
  const saved = app.drafts.find(item => item.id === draft.draftId);
  app.openWorkspaceDraft(saved);
  console.log(JSON.stringify({ retained, recoveredEmpty, reopened: app.captureComposer.draft.text,
    requests: requests.map(p => ({ body: p.body, revision: p.expected_revision, hash: p.expected_hash })) }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertTrue(result["retained"])
        self.assertTrue(result["recoveredEmpty"])
        self.assertEqual(result["reopened"], "")
        self.assertEqual(result["requests"][-1], {"body": "", "revision": 1, "hash": "1" * 64})

    def test_clearing_capture_during_first_save_keeps_the_cleared_revision(self) -> None:
        result = self.run_node(r"""
(async () => {
  location.protocol = "http:";
  const app = taskBoard();
  app.$nextTick = callback => callback();
  app.scheduleDraftSave = () => {};
  const requests = [];
  let finishFirst;
  global.fetch = (_path, options) => {
    const payload = JSON.parse(options.body);
    requests.push(payload);
    const response = { ok: true, json: async () => ({ saved_draft: {
      ...payload, revision: payload.expected_revision + 1,
      content_hash: String(payload.expected_revision + 1).repeat(64),
    } }) };
    return requests.length === 1 ? new Promise(resolve => { finishFirst = () => resolve(response); })
      : Promise.resolve(response);
  };
  app.captureComposer.beginNew(app.captureContext());
  const draft = app.captureComposer.draft;
  draft.text = "Text sent before clearing";
  app.captureComposer.persist(app.captureContext());
  const firstSave = app.saveWorkspaceDraft(draft);
  draft.text = "";
  app.captureComposer.persist(app.captureContext());
  app.captureComposer.close(app.captureContext());
  const retained = app.captureComposer.draft === draft;
  const flushed = app.flushWorkspaceDraft(draft);
  finishFirst();
  await firstSave;
  await flushed;

  app.captureComposer.beginNew(app.captureContext());
  app.captureComposer.draft.text = "Never sent";
  app.captureComposer.persist(app.captureContext());
  const unsentId = app.captureComposer.draft.draftId;
  app.captureComposer.draft.text = "";
  app.captureComposer.persist(app.captureContext());
  app.captureComposer.close(app.captureContext());
  console.log(JSON.stringify({ retained, bodies: requests.map(p => p.body), revision: draft.revision,
    unsentRemoved: !store.has(app.noteDraftStorageKey(app.workspaceKey, unsentId))
      && !app.drafts.some(item => item.id === unsentId) && app.captureComposer.draft === null }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertTrue(result["retained"])
        self.assertEqual(result["bodies"], ["Text sent before clearing", ""])
        self.assertEqual(result["revision"], 2)
        self.assertTrue(result["unsentRemoved"])

    def test_metadata_editor_defaults_and_unrelated_edits_preserve_exact_tags_and_due(self) -> None:
        result = self.run_node(r"""
(() => {
  const config = {
    task_defaults: { status: "active", priority: "none", type: "decision" },
    task_statuses: ["inbox", "active", "doing"],
    task_priorities: { urgent: "Urgent", none: "Unassigned", low: "Low" },
    task_types: { task: "Action", decision: "Decision" },
    task_due_kinds: { deadline: "Hard", target: "Soft" },
    workspace_tags: ["workspace-tag"], suggested_tags: ["suggested-tag"],
  };
  const fresh = cardMetadataEditor({}, true, "fresh", config, {});
  const existing = cardMetadataEditor({
    status: "doing", priority: "low", type: "task", tags: ["a,b", "quoted\"tag"],
    due: "2026-10-20T12:34:56", due_kind: "target",
  }, false, "existing", config, {});
  existing.syncIdentity("first:hash", existing.copy(), {});
  existing.value.project = "changed project";
  const copied = existing.copy();
  existing.value.tags = ["a,b", "quoted\"tag", "added"];
  existing.tagsText = '"a,b", "quoted""tag", added';
  existing.syncTags();
  const parsedEditedTags = existing.copy().tags;
  existing.dateValue = "";
  existing.syncDue();
  console.log(JSON.stringify({
    freshDefault: { status: fresh.value.status, priority: fresh.value.priority, type: fresh.value.type },
    copied: { tags: copied.tags, due: copied.due, due_kind: copied.due_kind, project: copied.project },
    parsedEditedTags, clearedDate: { due: existing.value.due, due_kind: existing.value.due_kind, time: existing.timeValue },
    replacement: (() => {
      existing.value.priority = "urgent";
      existing.syncIdentity("second:hash", { priority: "none", type: "decision", status: "active", tags: [], due: null }, {});
      return existing.value;
    })(),
  }));
})()
""")
        self.assertEqual(result["freshDefault"], {"status": "active", "priority": "none", "type": "decision"})
        self.assertEqual(result["copied"], {
            "tags": ["a,b", 'quoted"tag'], "due": "2026-10-20T12:34:56",
            "due_kind": "target", "project": "changed project",
        })
        self.assertEqual(result["parsedEditedTags"], ["a,b", 'quoted"tag', "added"])
        self.assertEqual(result["clearedDate"], {"due": None, "due_kind": None, "time": ""})
        self.assertEqual(result["replacement"]["priority"], "none")
        self.assertEqual(result["replacement"]["type"], "decision")

    def test_metadata_pickers_list_all_choices_create_clear_and_tag_chips(self) -> None:
        result = self.run_node(r"""
(() => {
  const config = {
    topic_tree: [
      { path: "garden", name: "garden", depth: 0, count: 1 }, { path: "home", name: "home", depth: 0, count: 0 },
      { path: "learning", name: "learning", depth: 0, count: 3 }, { path: "learning/rust", name: "rust", depth: 1, count: 2 },
      { path: "learning/rust/async", name: "async", depth: 2, count: 1 }, { path: "work", name: "work", depth: 0, count: 4 },
    ],
    workspace_projects: ["alpha", "beta"], suggested_tags: ["code"], workspace_tags: ["bug", "code"],
    workspace_usage: { project: { alpha: 1, beta: 3 }, topic: { work: 4, garden: 1 }, tag: { bug: 2 } },
  };
  const editor = cardMetadataEditor({ topic: "work", tags: ["bug"] }, false, "probe", config, {});
  const changes = [];
  editor.$dispatch = (name, detail) => changes.push(detail);
  const names = rows => rows.map(row => row.kind + ":" + row.name);
  editor.openPicker("topic");
  const topicRows = names(editor.pickerRows("topic"));
  editor.setPickerQuery("topic", "WOR");
  const filtered = names(editor.pickerRows("topic"));
  editor.setPickerQuery("topic", "learning/");
  const descendants = names(editor.pickerRows("topic"));
  editor.setPickerQuery("topic", "ASYNC");
  const segment = names(editor.pickerRows("topic"));
  editor.setPickerQuery("topic", "Learning/Rust/Web");
  const reused = names(editor.pickerRows("topic"));
  editor.setPickerQuery("topic", "learning//x");
  const invalid = names(editor.pickerRows("topic"));
  editor.setPickerQuery("topic", "");
  const indented = editor.pickerRows("topic").map(row => row.depth + ":" + row.label);
  editor.setPickerQuery("topic", "Shed");
  const created = names(editor.pickerRows("topic"));
  editor.leavePicker("topic");
  const afterLeave = editor.value.topic;
  editor.openPicker("topic");
  editor.choosePickerRow("topic", editor.pickerRows("topic")[0]);
  const cleared = editor.value.topic;
  editor.openPicker("project");
  const projectRows = names(editor.pickerRows("project"));
  editor.openPicker("tags");
  const tagRows = names(editor.pickerRows("tags"));
  editor.setPickerQuery("tags", "Code, new-tag, partial");
  return console.log(JSON.stringify({
    topicRows, filtered, created, descendants, segment, reused, invalid, indented, afterLeave, cleared, projectRows, tagRows,
    tags: editor.value.tags, query: editor.picker.query, tagsText: editor.tagsText,
    duplicate: (editor.addTag("BUG"), editor.value.tags.length),
    emitted: changes.length > 0,
  }));
})()
""")
        self.assertEqual(result["topicRows"], [
            "clear:", "option:garden", "option:home", "option:learning", "option:learning/rust",
            "option:learning/rust/async", "option:work"])
        self.assertEqual(result["filtered"], ["option:work", "create:WOR"])
        self.assertEqual(result["descendants"], ["option:learning/rust", "option:learning/rust/async"])
        self.assertEqual(result["segment"], ["option:learning/rust/async", "create:ASYNC"])
        self.assertEqual(result["reused"], ["create:learning/rust/Web"])
        self.assertEqual(result["invalid"], [])
        self.assertEqual(result["indented"][3:6], ["0:learning", "1:rust", "2:async"])
        self.assertEqual(result["created"], ["create:Shed"])
        self.assertEqual(result["afterLeave"], "Shed")
        self.assertEqual(result["cleared"], "")
        self.assertEqual(result["projectRows"], ["option:beta", "option:alpha"])
        self.assertEqual(result["tagRows"], ["option:code"])
        self.assertEqual(result["tags"], ["bug", "code", "new-tag"])
        self.assertEqual(result["query"], "partial")
        self.assertEqual(result["tagsText"], "bug, code, new-tag")
        self.assertEqual(result["duplicate"], 3)
        self.assertTrue(result["emitted"])

    def test_metadata_apply_patch_contains_only_deliberately_changed_fields(self) -> None:
        result = self.run_node(r"""
(async () => {
  const app = taskBoard();
  app.interactive = true;
  app.workspaceKey = "workspace-a";
  app.apiToken = "token";
  app.taskPriorityPolicy = { urgent: "Urgent", none: "Unassigned" };
  app.taskTypePolicy = { task: "Action" };
  app.taskDueKindPolicy = { target: "Soft" };
  app.taskDefaults = { status: "inbox", priority: "none", type: "task" };
  app.selected = {
    id: "first", title: "First", ref: "@first", kind: "task", status: "active", priority: "none", type: "task",
    project: "before", topic: "topic", tags: ["a,b", "other"], due: "2026-10-20T12:34:56",
    due_kind: "target", content_hash: "hash-one",
  };
  app.propertyEditor.begin(app.selected, app.taskDefaults);
  app.propertyEditor.form.project = "after";
  let request;
  global.fetch = async (_path, options) => {
    request = JSON.parse(options.body);
    const next = { ...app.selected, project: "after", content_hash: "hash-two" };
    return { ok: true, json: async () => ({ tasks: [next], statuses: ["inbox", "active", "doing"], meta: {
      interactive: true, task_defaults: app.taskDefaults, task_statuses: ["inbox", "active", "doing"],
      task_priorities: app.taskPriorityPolicy, task_types: app.taskTypePolicy, task_due_kinds: app.taskDueKindPolicy,
    } }) };
  };
  await app.propertyEditor.apply(app.propertyContext());
  console.log(JSON.stringify({ request, selected: app.selected }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertEqual(result["request"]["patch"], {"project": "after"})
        self.assertEqual(result["request"]["expected_hash"], "hash-one")
        self.assertEqual(result["selected"]["tags"], ["a,b", "other"])
        self.assertEqual(result["selected"]["due"], "2026-10-20T12:34:56")

    def test_task_metadata_refresh_preserves_edits_but_blocks_stale_hash_apply(self) -> None:
        result = self.run_node(r"""
(() => {
  const app = taskBoard();
  app.$nextTick = callback => callback();
  app.$refs = { preview: null };
  const first = { id: "first", title: "First", ref: "@first", kind: "task", status: "active", priority: "none", type: "task",
    project: "before", topic: "topic", tags: ["one"], due: null, due_kind: null, content_hash: "hash-one" };
  const refreshed = { ...first, project: "remote change", content_hash: "hash-two" };
  app.interactive = true; app.selectedId = "first"; app.selected = first; app.tasks = [first];
  app.taskDefaults = { status: "inbox", priority: "none", type: "task" };
  app.taskPriorityPolicy = { none: "Unassigned", high: "Important" };
  app.taskTypePolicy = { task: "Action" };
  app.taskDueKindPolicy = { target: "Soft" };
  app.propertyEditor.begin(first, app.taskDefaults);
  app.propertyEditor.form.project = "my deliberate edit";
  app.applyPayload({ tasks: [refreshed], statuses: ["inbox", "active", "doing"], meta: {
    interactive: true, task_defaults: app.taskDefaults, task_statuses: ["inbox", "active", "doing"],
    task_priorities: app.taskPriorityPolicy, task_types: app.taskTypePolicy, task_due_kinds: app.taskDueKindPolicy,
  } });
  const preserved = app.propertyEditor.form.project;
  const conflicted = app.propertyEditor.conflict;
  const enabled = app.propertyEditor.canApply(app.propertyContext());
  let requests = 0;
  app.apiPost = () => { requests += 1; return Promise.resolve({}); };
  Promise.resolve(app.propertyEditor.apply(app.propertyContext())).then(result => {
    app.showCard(refreshed);
    console.log(JSON.stringify({ preserved, conflicted, enabled, requests, result,
      reopened: app.propertyEditor.form.project, reopenedConflict: app.propertyEditor.conflict,
      baselineHash: app.propertyEditor.baselineHash }));
  });
})()
""")
        self.assertEqual(result["preserved"], "my deliberate edit")
        self.assertTrue(result["conflicted"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["requests"], 0)
        self.assertIsNone(result["result"])
        self.assertEqual(result["reopened"], "remote change")
        self.assertFalse(result["reopenedConflict"])
        self.assertEqual(result["baselineHash"], "hash-two")

    def test_local_draft_recovery_preserves_kind_only_switches_in_both_directions(self) -> None:
        result = self.run_node(r"""
(async () => {
  async function scenario(canonicalKind, localKind) {
    const entries = new Map();
    localStorage.setItem = (key, value) => entries.set(key, value);
    localStorage.getItem = key => entries.has(key) ? entries.get(key) : null;
    localStorage.removeItem = key => entries.delete(key);
    Object.defineProperty(localStorage, "length", { configurable: true, get() { return entries.size; } });
    localStorage.key = index => Array.from(entries.keys())[index] || null;
    const id = "capture-kind-switch";
    const key = "mt-note-draft-v2:workspace-a:" + id;
    const metadata = { status: "active", priority: "none", type: "task", project: "", topic: "", tags: [], due: null, due_kind: null };
    entries.set(key, JSON.stringify({
      id, kind: localKind, mode: "new", capture_id: id, created: "2026-10-15T10:00:00",
      revision: 1, content_hash: "a".repeat(64), title: "same title", body: "same body",
      ...metadata, _serverSaved: true, _savedKind: canonicalKind,
    }));
    const app = taskBoard();
    app.$nextTick = callback => callback();
    app.interactive = true; app.workspaceKey = "workspace-a"; app.apiToken = "token";
    app.drafts = [{
      id, kind: canonicalKind, mode: "new", capture_id: id, created: "2026-10-15T10:00:00",
      revision: 1, content_hash: "a".repeat(64), title: "same title", body: "same body", ...metadata,
    }];
    location.protocol = "http:";
    let sentKind = null;
    global.fetch = async (_path, options) => {
      sentKind = JSON.parse(options.body).kind;
      throw new Error("offline");
    };
    await app.importLocalWorkspaceDrafts();
    app.restoreLocalWorkspaceDrafts();
    const recovery = app.drafts.find(draft => draft._orphanedLocal);
    return { sentKind, recoveryKind: recovery && recovery.kind, canonicalKind };
  }
  console.log(JSON.stringify({
    noteToTask: await scenario("note", "task"),
    taskToNote: await scenario("task", "note"),
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertEqual(result["noteToTask"], {"sentKind": "task", "recoveryKind": "task", "canonicalKind": "note"})
        self.assertEqual(result["taskToNote"], {"sentKind": "note", "recoveryKind": "note", "canonicalKind": "task"})

    def test_local_body_edit_recovery_preserves_text_and_origin(self) -> None:
        result = self.run_node(r"""
(async () => {
  const entries = new Map();
  localStorage.setItem = (key, value) => entries.set(key, value);
  localStorage.getItem = key => entries.has(key) ? entries.get(key) : null;
  localStorage.removeItem = key => entries.delete(key);
  Object.defineProperty(localStorage, "length", { configurable: true, get() { return entries.size; } });
  localStorage.key = index => Array.from(entries.keys())[index] || null;
  const app = taskBoard(); app.$nextTick = callback => callback(); app.$refs = {};
  app.interactive = true; app.workspaceKey = "workspace-a"; app.apiToken = "token";
  const original = "## Capture\n\nSource text.\n\nOld instructions.\n\n## Notes\n\nHistory.\n";
  const edited = original.replace("Old instructions.", "Triage this source before action.");
  const target = {id:"note",ref:"@note",path:"cards/note.md",kind:"note",content_hash:"a".repeat(64),
    updated:"2026-10-01",body:original};
  app.tasks = [target]; app.selected = target;
  const id = "81c67d7e-12f7-43cc-8279-08e970e94aa0";
  entries.set("mt-note-draft-v2:workspace-a:" + id, JSON.stringify({id,kind:"note",mode:"edit",
    origin:"card-body",target:target.path,ref:target.ref,base_hash:target.content_hash,
    base_updated:target.updated,revision:0,title:"",body:edited}));
  let sent;
  global.fetch = async (_path, options) => {
    sent = JSON.parse(options.body);
    return {ok:true,json:async()=>({saved_draft:{...sent,revision:1,content_hash:"b".repeat(64)}})};
  };
  await app.importLocalWorkspaceDrafts();
  const saved = app.drafts.find(draft => draft.id === id);
  app.contentEditor.begin(app.contentContext(), saved);
  console.log(JSON.stringify({sentOrigin:sent.origin,savedOrigin:saved.origin,
    editorOrigin:app.contentEditor.draft.origin,
    text:app.contentEditor.text,canSave:app.contentEditor.canSave(app.contentContext())}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        self.assertEqual(result["sentOrigin"], "card-body")
        self.assertEqual(result["savedOrigin"], "card-body")
        self.assertEqual(result["editorOrigin"], "card-body")
        self.assertIn("## Capture", result["text"])
        self.assertIn("Triage this source before action.", result["text"])
        self.assertTrue(result["canSave"])

    def test_removed_workspace_draft_becomes_explicit_local_recovery(self) -> None:
        result = self.run_node(r"""
(async () => {
  const entries = new Map();
  localStorage.setItem = (key, value) => entries.set(key, value);
  localStorage.getItem = key => entries.has(key) ? entries.get(key) : null;
  localStorage.removeItem = key => entries.delete(key);
  Object.defineProperty(localStorage, "length", { get() { return entries.size; } });
  localStorage.key = index => Array.from(entries.keys())[index] || null;
  const app = taskBoard();
  app.$nextTick = callback => callback();
  app.interactive = true;
  app.workspaceKey = "workspace-a";
  app.apiToken = "token";
  app.tasks = [{ id:"note", title:"Note", body:"Before", ref: "@note", path: "cards/example.md", kind: "note" }];
  location.protocol = "http:";
  const id = "81c67d7e-12f7-43cc-8279-08e970e94aa0";
  const key = "mt-note-draft-v2:workspace-a:" + id;
  const local = {
    id, kind: "note", mode: "edit", origin: "card-body", target: "cards/example.md", ref: "@note",
    base_hash: "a".repeat(64), base_updated: "2026-10-01", revision: 2,
    content_hash: "b".repeat(64), _serverSaved: true, title: "", body: "saved locally",
  };
  entries.set(key, JSON.stringify(local));
  let calls = [];
  global.fetch = async (_path, options) => {
    const payload = JSON.parse(options.body); calls.push(payload);
    return { ok: true, json: async () => ({ saved_draft: {
      ...payload, revision: 1, updated: "2026-10-15T10:00:00", content_hash: "c".repeat(64),
    } }) };
  };
  await app.importLocalWorkspaceDrafts();
  app.restoreLocalWorkspaceDrafts();
  const recovery = app.drafts.find(draft => draft._orphanedLocal);
  const firstState = { count: calls.length, orphaned: !!recovery, text: recovery && recovery.body };
  await app.recoverLocalWorkspaceDraft(recovery);
  const fork = calls[0];
  entries.set(key, JSON.stringify(local)); // simulate an old tab writing after recovery
  const next = taskBoard(); next.workspaceKey = "workspace-a"; next.drafts = [];
  next.restoreLocalWorkspaceDrafts();
  await next.importLocalWorkspaceDrafts();
  const stale = next.drafts.find(draft => draft._orphanedLocal);
  next.dismissLocalWorkspaceDraft(stale);
  console.log(JSON.stringify({
    firstState, forkIdDiffers: fork.id !== id, baseline: fork.base_hash,
    staleStillRecoverable: !!stale, dismissed: !next.drafts.some(draft => draft._orphanedLocal),
    draftKeyRemoved: !entries.has(key), tombstone: !!entries.get("mt-draft-closed-v1:workspace-a:" + id),
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertEqual(result["firstState"], {"count": 0, "orphaned": True, "text": "saved locally"})
        self.assertTrue(result["forkIdDiffers"])
        self.assertEqual(result["baseline"], "a" * 64)
        self.assertFalse(result["staleStillRecoverable"])
        self.assertTrue(result["dismissed"])
        self.assertTrue(result["draftKeyRemoved"])
        self.assertTrue(result["tombstone"])

    def test_recovered_new_note_uses_fresh_capture_id_and_keeps_canonical_sibling(self) -> None:
        result = self.run_node(r"""
(async () => {
  const entries = new Map();
  localStorage.setItem = (key, value) => entries.set(key, value);
  localStorage.getItem = key => entries.has(key) ? entries.get(key) : null;
  localStorage.removeItem = key => entries.delete(key);
  Object.defineProperty(localStorage, "length", { get() { return entries.size; } });
  localStorage.key = index => Array.from(entries.keys())[index] || null;
  const app = taskBoard();
  app.$nextTick = callback => callback();
  app.interactive = true; app.workspaceKey = "workspace-a"; app.apiToken = "token";
  app.captureComposer.makeId = () => "recovered-capture-id";
  const id = "already-applied-capture";
  const key = "mt-note-draft-v2:workspace-a:" + id;
  const local = { id, kind: "note", mode: "new", capture_id: id, _serverSaved: true,
    revision: 2, content_hash: "a".repeat(64), title: "Local divergence", body: "divergent body" };
  const canonical = { id, kind: "note", mode: "new", capture_id: id, revision: 2,
    content_hash: "b".repeat(64), title: "Canonical", body: "original body" };
  entries.set(key, JSON.stringify(local));
  app.drafts = [canonical];
  app.restoreLocalWorkspaceDrafts();
  const recovery = app.drafts.find(draft => draft._orphanedLocal);
  let sent = null;
  global.fetch = async (_path, options) => {
    sent = JSON.parse(options.body);
    return { ok: true, json: async () => ({ saved_draft: { ...sent, revision: 1,
      content_hash: "c".repeat(64), updated: "2026-10-15T10:00:00" } }) };
  };
  await app.recoverLocalWorkspaceDraft(recovery);
  const canonicalAfterFork = app.drafts.find(draft => draft.id === id);

  const dismissedId = "canonical-to-retain";
  const dismissedKey = "mt-note-draft-v2:workspace-a:" + dismissedId;
  const dismissedCanonical = { id: dismissedId, kind: "note", mode: "new", revision: 1,
    body: "server version", content_hash: "d".repeat(64) };
  entries.set(dismissedKey, JSON.stringify({ ...local, id: dismissedId, body: "stale version" }));
  const other = taskBoard();
  other.workspaceKey = "workspace-a"; other.drafts = [dismissedCanonical];
  other.restoreLocalWorkspaceDrafts();
  const stale = other.drafts.find(draft => draft._orphanedLocal);
  other.dismissLocalWorkspaceDraft(stale);
  entries.set(dismissedKey, JSON.stringify({ ...local, id: dismissedId, body: "old tab write" }));
  const afterDismiss = taskBoard();
  afterDismiss.workspaceKey = "workspace-a"; afterDismiss.drafts = [dismissedCanonical];
  afterDismiss.restoreLocalWorkspaceDrafts();
  console.log(JSON.stringify({
    freshCaptureId: sent.capture_id, forkId: sent.id,
    originalKeptAfterFork: canonicalAfterFork && canonicalAfterFork.body === "original body",
    originalKeptAfterDismiss: other.drafts.some(draft => draft.id === dismissedId && !draft._orphanedLocal),
    staleNotResurrected: !afterDismiss.drafts.some(draft => draft._orphanedLocal),
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertEqual(result["freshCaptureId"], result["forkId"])
        self.assertEqual(result["freshCaptureId"], "recovered-capture-id")
        self.assertTrue(result["originalKeptAfterFork"])
        self.assertTrue(result["originalKeptAfterDismiss"])
        self.assertTrue(result["staleNotResurrected"])

    def test_missing_canonical_orphan_is_removed_after_editor_clone_recovery_or_dismissal(self) -> None:
        result = self.run_node(r"""
(async () => {
  const entries = new Map();
  localStorage.setItem = (key, value) => entries.set(key, value);
  localStorage.getItem = key => entries.has(key) ? entries.get(key) : null;
  localStorage.removeItem = key => entries.delete(key);
  Object.defineProperty(localStorage, "length", { get() { return entries.size; } });
  localStorage.key = index => Array.from(entries.keys())[index] || null;
  const makeApp = id => {
    const app = taskBoard();
    app.$nextTick = callback => callback();
    app.$refs = {};
    app.select = item => { app.selected=item; app.selectedId=item.id; };
    app.interactive = true; app.workspaceKey = "workspace-" + id; app.apiToken = "token";
    app.tasks = [{ id:"task", title:"Task", body:"Before", ref: "@task", path: "cards/example.md", kind: "task" }];
    entries.set("mt-note-draft-v2:" + app.workspaceKey + ":" + id, JSON.stringify({
      id, kind: "task", mode: "edit", origin: "card-body", target: "cards/example.md", ref: "@task",
      base_hash: "a".repeat(64), base_updated: "2026-10-01", revision: 2,
      content_hash: "b".repeat(64), _serverSaved: true, body: "saved locally", title: "",
    }));
    app.restoreLocalWorkspaceDrafts();
    return app;
  };
  const recoverApp = makeApp("recover-orphan");
  const recoverRow = recoverApp.drafts.find(draft => draft._orphanedLocal);
  await recoverApp.openWorkspaceDraft(recoverRow);
  const recoverEditor = recoverApp.contentEditor.draft;
  global.fetch = async (_path, options) => {
    const payload = JSON.parse(options.body);
    return { ok: true, json: async () => ({ saved_draft: { ...payload, revision: 1,
      content_hash: "c".repeat(64), updated: "2026-10-15T10:00:00" } }) };
  };
  await recoverApp.recoverLocalWorkspaceDraft(recoverEditor);

  const dismissApp = makeApp("dismiss-orphan");
  const dismissRow = dismissApp.drafts.find(draft => draft._orphanedLocal);
  await dismissApp.openWorkspaceDraft(dismissRow);
  const dismissEditor = dismissApp.contentEditor.draft;
  dismissApp.dismissLocalWorkspaceDraft(dismissEditor);
  console.log(JSON.stringify({
    clonedRecoverEditor: recoverEditor !== recoverRow,
    recoveredOrphanRemoved: !recoverApp.drafts.some(draft => draft._orphanedLocal),
    recoveredDraftKept: recoverApp.drafts.some(draft => draft.id !== "recover-orphan" && !draft._orphanedLocal),
    clonedDismissEditor: dismissEditor !== dismissRow,
    dismissedOrphanRemoved: !dismissApp.drafts.some(draft => draft._orphanedLocal),
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        self.assertTrue(result["clonedRecoverEditor"])
        self.assertTrue(result["recoveredOrphanRemoved"])
        self.assertTrue(result["recoveredDraftKept"])
        self.assertTrue(result["clonedDismissEditor"])
        self.assertTrue(result["dismissedOrphanRemoved"])

    def test_capture_recovery_preserves_whitespace_and_meeting_template_guard(self) -> None:
        result = self.run_node(r"""
const makeApp=()=>{const app=taskBoard();app.$nextTick=()=>{};app.$refs={};app.scheduleDraftSave=()=>{};return app;};
const app=makeApp();app.captureComposer.beginNew(app.captureContext());
app.captureComposer.setKind(app.captureContext(),'note');
app.captureComposer.draft.title='Standup';
app.captureComposer.draft.text='  Source text.\r\n\r\n';
app.captureComposer.persist(app.captureContext());
const id=app.captureComposer.draft.draftId;
app.captureComposer.close(app.captureContext());
const reopened=makeApp();reopened.restoreLocalWorkspaceDrafts();
const recovered=reopened.drafts.find(draft=>draft.id===id);
const other=makeApp();other.workspaceKey='another-workspace';other.restoreLocalWorkspaceDrafts();
const template=makeApp();template.captureComposer.beginNew(template.captureContext());
template.captureComposer.setKind(template.captureContext(),'note');
template.captureComposer.applyMeetingTemplate(template.captureContext());
const blocked=!template.captureComposer.canSave(template.captureContext());
template.captureComposer.draft.text+='\n\nAn actual decision.';
console.log(JSON.stringify({title:recovered.title,body:recovered.body,kind:recovered.kind,
isolated:other.drafts.length===0,blocked,ready:template.captureComposer.canSave(template.captureContext())}));
""")
        self.assertEqual(result, {'title':'Standup','body':'  Source text.\r\n\r\n','kind':'note',
                                  'isolated':True,'blocked':True,'ready':True})

    def test_only_body_preview_checkboxes_are_enabled_with_indexes(self) -> None:
        result = self.run_node(r"""
const mk=()=>{const el={attrs:{},classes:[],removeAttribute(n){delete this.attrs[n];},
  setAttribute(n,v){this.attrs[n]=v;},classList:{add:c=>el.classes.push(c)}};el.attrs.disabled='';return el;};
const boxes=[mk(),mk()];
const body={querySelectorAll:sel=>sel.includes('checkbox')?boxes:[]};
const count=enableTaskCheckboxes(body);
const panel=mk();const panelRoot={innerHTML:''};
global.marked={Renderer:function(){this.link=t=>String(t);this.image=t=>String(t);},parse:()=>'<ul></ul>'};
global.DOMPurify={sanitize:s=>s};window.marked=marked;window.DOMPurify=DOMPurify;
renderPropertyPanels({kind:'task',body:'',next_action:'- [ ] a',comments:[]},panelRoot);
console.log(JSON.stringify({count,attrs:boxes.map(b=>b.attrs),classes:boxes.map(b=>b.classes),
  panelDisabled:'disabled' in panel.attrs,noop:enableTaskCheckboxes({innerHTML:''})}));
""")
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['attrs'], [{'data-mt-task-index': '0'}, {'data-mt-task-index': '1'}])
        self.assertEqual(result['classes'], [['mt-task-checkbox']] * 2)
        self.assertTrue(result['panelDisabled'])
        self.assertEqual(result['noop'], 0)

    def test_body_and_activity_render_independently_for_both_kinds(self) -> None:
        result = self.run_node(r"""
const parsed=[];
function Renderer() { this.link=token=>String(token); this.image=token=>String(token); }
global.marked={Renderer,parse:source=>{parsed.push(source);return '<p>rendered</p>';}};
global.DOMPurify={sanitize:source=>source}; window.marked=marked;window.DOMPurify=DOMPurify;
const results=[];
for(const kind of ['task','note']) {
  parsed.length=0;
  const body='## Next action\n\nOrdinary body.\n\n```python\nunfinished';
  const card={kind,body,processing_instructions:'Check source.\n```python\nunfinished',
    next_action:kind==='task'?'Compare methods.':'',
    comments:[{at:'2026-11-10',text:'History line one.\nLine two.'}]};
  const document={innerHTML:''},activity={innerHTML:''};
  renderPreview(card,document);
  const bodyCalls=parsed.slice();renderPropertyPanels(card,activity);
  results.push({kind,body,bodyCalls,activityCalls:parsed.slice(1),document:document.innerHTML,activity:activity.innerHTML});
}
console.log(JSON.stringify({results}));
""")
        for row in result['results']:
            self.assertEqual(row['bodyCalls'], [row['body']])
            self.assertEqual(len(row['activityCalls']), 3 if row['kind']=='task' else 2)
            self.assertIn('History line one.\nLine two.', row['activityCalls'])
            self.assertIn('Comments (1)', row['activity'])
            self.assertEqual(row['document'], '<p>rendered</p>')
            self.assertEqual('Next action</h3>' in row['activity'], row['kind']=='task')

    def test_template_exposes_capture_and_review_controls(self) -> None:
        cards = (ROOT / "tools/web/templates/views/cards.html").read_text(encoding="utf-8")
        toolbar = (ROOT / "tools/web/templates/toolbar.html").read_text(encoding="utf-8")
        composer = (ROOT / "tools/web/templates/capture_composer.html").read_text(encoding="utf-8")
        drawer = (ROOT / "tools/web/templates/drawer.html").read_text(encoding="utf-8")
        self.assertNotIn("interactive", toolbar)
        self.assertIn('x-show="captureComposer.draft && !captureComposer.open"', toolbar)
        self.assertIn("{card}", cards)
        card = (ROOT / "tools/web/templates/card.html").read_text(encoding="utf-8")
        self.assertIn('x-show="task.needs_review"', card)
        self.assertIn('aria-modal="true"', composer)
        self.assertIn("@keydown.meta.enter", composer)
        self.assertIn("@keydown.ctrl.enter", composer)
        self.assertIn("What do you want to accomplish?", composer)
        self.assertIn('x-show="!contentEditor.draft"', drawer)
        self.assertIn("Mark reviewed", drawer)

    def test_markdown_math_protection_and_restoration(self) -> None:
        source = r"""
const fs = require("fs");
const markdownSource = fs.readFileSync("" + __MD_PATH__, "utf8");
eval(markdownSource);

const testInputs = {
  subscripts: "Outcomes ($\\overleftarrow{\\mathbf{Y}}_i$, $t < 0$) unaffected ($Y_{i,t} = Y_{i,t}^{(0)}$).",
  braces: "Set $\\alpha \\in \\{1/10, 4\\}$ and $\\{(a_i, \\dots)\\}_{i=1}^n$.",
  table: "| Method | Metric | Source |\n| --- | --- | --- |\n| Synthetic | $\\text{MRE} = \\frac{|A - B|}{C}$ | §6 |",
  code: "Inline `$foo$` and fence:\n```python\n$bar = 1$\n```",
  currency: "The cost is $50 and the tax is $10.",
};

const protectedResults = {};
for (const [key, text] of Object.entries(testInputs)) {
  const p = protectMathSpans(text);
  protectedResults[key] = {
    protected: p.protectedSource,
    mathList: p.mathList,
    restored: restoreMathSpans(p.protectedSource, p.mathList)
  };
}

let mathRendered = false;
global.marked = {
  parse(src, opts) {
    return "<p>" + src + "</p>";
  },
  Renderer: function() { this.link = () => ""; this.image = () => ""; }
};
global.DOMPurify = { sanitize: s => s };
global.window = {
  marked: global.marked,
  DOMPurify: global.DOMPurify,
  renderMathInElement(el) {
    mathRendered = true;
    el.innerHTML = el.innerHTML.replace(/\$([^\$]+)\$/g, '<span class="katex">$1</span>');
  }
};
global.document = {
  createElement() {
    let _html = "";
    return {
      get innerHTML() { return _html; },
      set innerHTML(val) { _html = val; }
    };
  }
};

const renderedHtml = renderMarkdown({}, "Formula: $x + y$");

console.log(JSON.stringify({ protectedResults, mathRendered, renderedHtml }));
"""
        script = source.replace("__MD_PATH__", json.dumps(str(ROOT / "tools/web/js/markdown.js")))
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        result = subprocess.run([node], input=script, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        subscripts = payload["protectedResults"]["subscripts"]
        self.assertIn("@@MTMATH0@@", subscripts["protected"])
        self.assertIn("@@MTMATH1@@", subscripts["protected"])
        self.assertIn("@@MTMATH2@@", subscripts["protected"])
        self.assertNotIn("_i", subscripts["protected"])
        self.assertEqual(len(subscripts["mathList"]), 3)
        self.assertIn("$t &lt; 0$", subscripts["restored"])

        braces = payload["protectedResults"]["braces"]
        self.assertIn("@@MTMATH0@@", braces["protected"])
        self.assertIn(r"\{1/10, 4\}", braces["mathList"][0])

        table = payload["protectedResults"]["table"]
        self.assertIn("@@MTMATH0@@", table["protected"])
        lines = table["protected"].splitlines()
        self.assertEqual(lines[2].count("|"), 4)

        code = payload["protectedResults"]["code"]
        self.assertEqual(len(code["mathList"]), 0)
        self.assertEqual(code["protected"], "Inline `$foo$` and fence:\n```python\n$bar = 1$\n```")

        currency = payload["protectedResults"]["currency"]
        self.assertEqual(len(currency["mathList"]), 0)

        self.assertTrue(payload["mathRendered"])
        self.assertIn('<span class="katex">x + y</span>', payload["renderedHtml"])

    def test_reference_spans_cover_ordinary_body_and_ignore_fenced_examples(self) -> None:
        import tools.taskweb as taskweb
        def note(item_id, title, body):
            raw=taskweb.operations.render_new_card('note',item_id,title,body_text=body)
            frontmatter, markdown=taskweb.store.parse_frontmatter(raw)
            return taskweb.store.Card(Path('/tmp/'+item_id+'.md'),raw,frontmatter,markdown,taskweb.store.parse_yaml(frontmatter))
        target=note('target','Target','Source.')
        ref='@'+target.short_ref
        body=f'## Notes\n\nLiteral reference {ref}\n\n```microtask-capture\nExample {ref}\n```'
        item=note('testnote1','Review',body)
        detail=taskweb.task_to_dict(item,taskweb.store.ReferenceIndex([item,target]))
        self.assertEqual(detail['body'],body)
        spans=detail['reference_spans']['body']
        self.assertEqual(len(spans),1)
        self.assertEqual(body[spans[0]['start']:spans[0]['end']],ref)


if __name__ == "__main__":
    unittest.main()
