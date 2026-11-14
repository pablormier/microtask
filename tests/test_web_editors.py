from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ROOT / "tools/web/js/card_helpers.js",
    ROOT / "tools/web/js/capture_composer.js",
    ROOT / "tools/web/js/property_editor.js",
    ROOT / "tools/web/js/content_editor.js",
]


class WebEditorComponentTests(unittest.TestCase):
    def run_node(self, scenario: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        source = "\n".join(path.read_text(encoding="utf-8") for path in SOURCES)
        script = "const assert = require('assert/strict');\n" + source + "\n(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        result = subprocess.run([node], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_editor_instances_keep_their_state_separate(self) -> None:
        self.run_node(r"""
const captureA=createCaptureComposer(), captureB=createCaptureComposer();
const propertiesA=createPropertyEditor(), propertiesB=createPropertyEditor();
const contentA=createContentEditor(), contentB=createContentEditor();
captureA.draft={text:'capture A'};
propertiesA.form={project:'property A'};
contentA.text='content A';
assert.equal(captureB.draft,null);
assert.equal(propertiesB.form,null);
assert.equal(contentB.text,'');
""")

    def test_capture_uses_a_minimal_context_for_input_persistence_and_validation(self) -> None:
        self.run_node(r"""
const rows=[];
const store={
  isEmpty:d=>d.mode==='new'&&!String(d.title||'').trim()&&!String(d.text||'').trim()&&!Number(d.revision||0),
  forgetEmpty:()=>{}, saveLocal:()=>true,
};
const ctx={interactive:true,workspaceKey:'workspace-a',busy:false,store,
  taskDefaults:{status:'inbox',priority:'none',type:'task'},taskStatuses:['inbox','active'],
  taskPriorityPolicy:{none:'None',high:'High'},taskTypePolicy:{task:'Task'},
  taskOptionsFromDraft:d=>d.taskOptions||{},metadataSnapshot:()=>'',captureDraftFields:()=>({}),
  getDrafts:()=>rows,scheduleDraftSave:()=>{},focusInput:()=>{},nextTick:fn=>fn(),setMode:()=>{},
};
const editor=createCaptureComposer();
editor.beginNew(ctx,null,'note');
editor.draft.text='A recoverable capture';
assert.equal(editor.persist(ctx),true);
assert.equal(editor.persisted,true);
assert.equal(rows.length,1);
assert.equal(editor.canSave(ctx),true);
assert.equal(editor.draft.taskOptions.project,'');
const scoped=createCaptureComposer();
scoped.beginNew({...ctx,scopeDefaults:{project:'launch',topic:'work/web'}},null,'task');
assert.equal(scoped.draft.taskOptions.project,'launch');
assert.equal(scoped.draft.taskOptions.topic,'work/web');
""")

    def test_failed_property_save_keeps_values_and_reports_component_error(self) -> None:
        self.run_node(r"""
const task={ref:'@task',kind:'task',title:'Task title',content_hash:'base',priority:'none',type:'task',tags:[],due:null,due_kind:null};
const editor=createPropertyEditor();
editor.begin(task,{status:'active',priority:'none',type:'task'});
editor.form.project='Pending project';
const ctx={selected:task,interactive:true,busy:false,draftBusy:false,contentOpen:false,
  defaults:{status:'active',priority:'none',type:'task'},priorityPolicy:{none:'None'},
  typePolicy:{task:'Task'},dueKindPolicy:{},apiPost:async()=>null,
  actionMessage:()=> 'offline',getSelected:()=>task};
assert.equal(editor.canApply(ctx),true);
assert.equal(await editor.apply(ctx),null);
assert.equal(editor.form.project,'Pending project');
assert.equal(editor.error,'offline');
assert.equal(editor.baselineHash,'base');
""")

    def test_content_local_save_failure_stays_open_and_save_returns_result(self) -> None:
        self.run_node(r"""
const task={id:'task',ref:'@task',path:'cards/task.md',kind:'task',content_hash:'base',body:'# Existing'};
const editor=createContentEditor();
let finish;
const ctx={selected:task,interactive:true,busy:false,draftBusy:false,getDrafts:()=>[],makeId:()=> 'content-id',
  store:{saveLocal:()=>false,upsert:()=>{}},scheduleDraftSave:()=>{},needsReconciliation:()=>false,
  clearStatus:()=>{},focusInput:()=>{},
  finishWorkspaceDraft:()=>new Promise(resolve=>{finish=resolve;})};
editor.begin(ctx);
editor.text='# Edited';
editor.persist(ctx);
assert.equal(editor.localSaved,false);
assert.equal(editor.close(ctx),false);
assert.match(editor.error,/could not be saved locally/);
const saving=editor.save(ctx);
assert.equal(editor.saving,true);
assert.equal(editor.canSave(ctx),false);
assert.equal(await editor.save(ctx),null);
task.content_hash='content-saved-hash';
finish({saved_ref:'@task'});
assert.deepEqual(await saving,{saved_ref:'@task'});
assert.equal(editor.saving,false);
""")


if __name__ == "__main__":
    unittest.main()
