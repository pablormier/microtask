from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ROOT / "tools/web/js/card_helpers.js",
    ROOT / "tools/web/js/editor_surface.js",
    ROOT / "tools/web/js/mention_autocomplete.js",
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

    def test_editor_surface_delegates_to_custom_engine_when_present(self) -> None:
        self.run_node(r"""
let value='initial';
const surfaceDefault=createEditorSurface({getValue:()=>value});
assert.equal(surfaceDefault.hasCustomEngine(),false);
assert.equal(surfaceDefault.mount({}),false);

const events=[];
global.window={
  MTZenEditorEngine:{
    mount(hostEl,opts){
      events.push(['mount',opts.value,opts.placeholder,opts.disabled]);
      return {
        setValue(next){ events.push(['setValue',next]); },
        setDisabled(flag){ events.push(['setDisabled',flag]); },
        focus(){ events.push(['focus']); },
        destroy(){ events.push(['destroy']); },
      };
    }
  }
};
try {
  const surface=createEditorSurface({
    getValue:()=>value,
    getPlaceholder:()=> 'Write...',
    isDisabled:()=>false,
  });
  assert.equal(surface.hasCustomEngine(),true);
  assert.equal(surface.mount({id:'host'}),true);
  surface.setValue('updated');
  surface.setDisabled(true);
  assert.equal(surface.focus(),true);
  surface.destroy();
  assert.deepEqual(events,[
    ['mount','initial','Write...',false],
    ['setValue','updated'],
    ['setDisabled',true],
    ['focus'],
    ['destroy'],
  ]);
} finally {
  delete global.window;
}
""")

    def test_zen_mode_lifecycle_and_escape_hierarchy(self) -> None:
        self.run_node(r"""
const store={
  isEmpty:d=>d.mode==='new'&&!String(d.title||'').trim()&&!String(d.text||'').trim()&&!Number(d.revision||0),
  forgetEmpty:()=>{}, saveLocal:()=>true, upsert:()=>{},
};
const captureCtx={interactive:true,workspaceKey:'ws',busy:false,store,
  taskDefaults:{status:'inbox',priority:'none',type:'task'},taskStatuses:['inbox','active'],
  taskPriorityPolicy:{none:'None'},taskTypePolicy:{task:'Task'},
  taskOptionsFromDraft:d=>d.taskOptions||{},metadataSnapshot:()=>'',captureDraftFields:()=>({}),
  getDrafts:()=>[],scheduleDraftSave:()=>{},focusInput:()=>{},nextTick:fn=>fn(),setMode:()=>{},
};
const composer=createCaptureComposer();
composer.beginNew(captureCtx,null,'note');
assert.equal(composer.zen,false);
composer.toggleZen(captureCtx);
assert.equal(composer.zen,true);
composer.setZenView(captureCtx,'split');
assert.equal(composer.zenView,'split');
composer.draft.taskOptions={project:'research',tags:['math','notes']};
composer.draft.instructions='Summarize equations';
assert.equal(composer.zenMetaSummary(),'research · 2 tags · instructions');
composer.zenMetaOpen=true;
composer.handleEscape(captureCtx);
assert.equal(composer.zenMetaOpen,false);
assert.equal(composer.zen,true);
composer.handleEscape(captureCtx);
assert.equal(composer.zen,false);
assert.equal(composer.open,true);
composer.handleEscape(captureCtx);
assert.equal(composer.open,false);

const task={id:'t1',ref:'@t1',path:'cards/t1.md',kind:'note',content_hash:'h1',body:'Hello'};
const content=createContentEditor();
const contentCtx={selected:task,busy:false,draftBusy:false,getDrafts:()=>[],makeId:()=> 'c1',
  store,scheduleDraftSave:()=>{},needsReconciliation:()=>false,clearStatus:()=>{},focusInput:()=>{},nextTick:fn=>fn()};
content.begin(contentCtx);
assert.equal(content.handleEscape(contentCtx),false);
content.toggleZen(contentCtx);
assert.equal(content.zen,true);
content.setZenView(contentCtx,'split');
assert.equal(content.zenView,'split');
content.syncSurface(contentCtx,{id:'content-host'});
assert.equal(content.handleEscape(contentCtx),true);
assert.equal(content.zen,false);
content.close(contentCtx);
assert.equal(content.zen,false);
assert.equal(content.draft,null);
""")

    def test_custom_engine_mounts_only_in_zen_and_falls_back_on_null(self) -> None:
        self.run_node(r"""
const store={isEmpty:()=>false,forgetEmpty:()=>{},saveLocal:()=>true,upsert:()=>{}};
const ctx={interactive:true,workspaceKey:'ws',busy:false,store,
  taskDefaults:{status:'inbox',priority:'none',type:'task'},taskStatuses:['inbox'],
  taskPriorityPolicy:{none:'None'},taskTypePolicy:{task:'Task'},
  taskOptionsFromDraft:d=>d.taskOptions||{},metadataSnapshot:()=>'',captureDraftFields:()=>({}),
  getDrafts:()=>[],scheduleDraftSave:()=>{},focusInput:()=>{},nextTick:fn=>fn(),setMode:()=>{},
};
let mountResult={setValue:()=>{},setDisabled:()=>{},focus:()=>{},destroy:()=>{}};
let mounts=0,destroys=0;
global.window={
  MTZenEditorEngine:{
    mount(){
      mounts++;
      return mountResult && {...mountResult, destroy(){ destroys++; }};
    }
  }
};
try {
  const composer=createCaptureComposer();
  composer.beginNew(ctx,null,'note');
  const host={id:'host'};
  composer.syncSurface(ctx,host);
  assert.equal(mounts,0);
  assert.equal(composer.isCustomMounted(),false);
  composer.toggleZen(ctx,true);
  composer.syncSurface(ctx,host);
  assert.equal(mounts,1);
  assert.equal(composer.isCustomMounted(),true);
  composer.toggleZen(ctx,false);
  composer.syncSurface(ctx,host);
  assert.equal(destroys,1);
  assert.equal(composer.isCustomMounted(),false);

  mountResult=null;
  composer.toggleZen(ctx,true);
  composer.syncSurface(ctx,host);
  assert.equal(composer.isCustomMounted(),false);
} finally {
  delete global.window;
}
""")

    def test_mention_autocomplete_matches_refs_and_keywords_and_inserts(self) -> None:
        self.run_node(r"""
const cards=[
  {ref:'@4e91a7c2',title:'Fix login timeout bug',kind:'task',status:'active',project:'auth',topic:'work',tags:['bug']},
  {ref:'@8a12bc34',title:'Quarterly roadmap notes',kind:'note',status:'',project:'planning',topic:'work',tags:['strategy']},
  {ref:'@11223344',title:'Dentist appointment',kind:'task',status:'inbox',project:'',topic:'personal',tags:['errand']},
];
assert.deepEqual(findMentionTrigger('See @log', 8), {query:'log', start:4, end:8});
assert.deepEqual(findMentionTrigger('See @fix login', 14), {query:'fix login', start:4, end:14});
assert.equal(findMentionTrigger('email@example.com', 17), null);

assert.equal(matchMentionCards(cards, '4e9')[0].ref, '@4e91a7c2');
assert.equal(matchMentionCards(cards, 'login bug')[0].ref, '@4e91a7c2');
assert.equal(matchMentionCards(cards, 'strategy')[0].ref, '@8a12bc34');

const mention=createMentionAutocomplete();
const textarea={
  value:'Check @road',
  selectionStart:11,
  focused:false,
  selectionRange:null,
  focus(){ this.focused=true; },
  setSelectionRange(a,b){ this.selectionRange=[a,b]; },
};
mention.update(textarea, cards);
assert.equal(mention.open,true);
assert.equal(mention.items.length,1);
assert.equal(mention.items[0].ref,'@8a12bc34');

let updated='';
const handled=mention.handleKeydown(
  {key:'Enter',metaKey:false,ctrlKey:false,shiftKey:false,preventDefault(){},stopPropagation(){}},
  textarea,
  next=>{ updated=next; },
);
assert.equal(handled,true);
assert.equal(updated,'Check @8a12bc34 ');
assert.equal(textarea.value,'Check @8a12bc34 ');
assert.deepEqual(textarea.selectionRange,[16,16]);
assert.equal(mention.open,false);

// Escape closes mention menu before closing Zen or composer
textarea.value='See @';
textarea.selectionStart=5;
mention.update(textarea, cards);
assert.equal(mention.open,true);
assert.equal(mention.items.length,3);
// IME composition events are ignored so candidate confirmation does not insert a mention
assert.equal(mention.handleKeydown({key:'Enter',isComposing:true,preventDefault(){},stopPropagation(){}}, textarea, ()=>{}), false);
assert.equal(mention.handleKeydown({key:'Enter',keyCode:229,preventDefault(){},stopPropagation(){}}, textarea, ()=>{}), false);
assert.equal(mention.open,true);
mention.handleKeydown({key:'ArrowDown',preventDefault(){}}, textarea, ()=>{});
assert.equal(mention.activeIndex,1);
mention.handleKeydown({key:'Escape',preventDefault(){},stopPropagation(){}}, textarea, ()=>{});
assert.equal(mention.open,false);

// Moving caret away from @trigger closes menu instead of applying at stale range
textarea.value='See @road and more';
textarea.selectionStart=9;
mention.update(textarea, cards);
assert.equal(mention.open,true);
textarea.selectionStart=18;
assert.equal(mention.handleKeydown({key:'Enter',preventDefault(){},stopPropagation(){}}, textarea, ()=>{}), false);
assert.equal(mention.open,false);

// Caret positioning anchors the floating menu near the @ token and flips above near bottom edge
global.window={
  innerWidth:800,
  innerHeight:300,
  getComputedStyle:()=>({fontSize:'14px',lineHeight:'20px'}),
};
global.document={
  body:{appendChild(){},removeChild(){}},
  createElement:(tag)=>tag==='span'
    ? {offsetLeft:42,offsetTop:16,offsetHeight:20,textContent:''}
    : {style:{},textContent:'',appendChild(){}},
};
try {
  textarea.value='Hello @road';
  textarea.selectionStart=11;
  textarea.scrollTop=0;
  textarea.scrollLeft=0;
  textarea.getBoundingClientRect=()=>({left:100,top:50,bottom:220,width:400,height:170});
  mention.update(textarea, cards);
  assert.deepEqual(mention.pos, {top:90, left:142, placeAbove:false});

  // Near bottom of viewport flips above caret
  textarea.getBoundingClientRect=()=>({left:600,top:240,bottom:290,width:400,height:50});
  mention.reposition(textarea);
  assert.deepEqual(mention.pos, {top:252, left:404, placeAbove:true});
} finally {
  delete global.window;
  delete global.document;
}
""")


    def test_unified_zen_composer_supports_existing_card_edits_and_combined_save(self) -> None:
        self.run_node(r"""
const note={
  id:'note-1',ref:'@note-1',path:'cards/note-1.md',kind:'note',
  title:'Original',content_hash:'h1',updated:'2026-11-15T10:00:00Z',
  body:'Initial body',priority:'none',project:'',topic:'',tags:[],due:null,due_kind:null,
  processing_instructions:'',
};
const composer=createCaptureComposer();
const calls=[];
const drafts=[];
const ctx={
  workspaceKey:'ws-1',
  busy:false,
  taskDefaults:{status:'inbox',priority:'none',type:'task'},
  taskPriorityPolicy:{none:'None',high:'High'},
  taskTypePolicy:{task:'Task'},
  getDrafts:()=>drafts,
  getSelected:()=>note,
  store:{
    isEmpty:d=>d.mode==='new'&&!String(d.title||'').trim()&&!String(d.text||'').trim()&&!Number(d.revision||0),
    forgetEmpty:()=>{},
    saveLocal:()=>true,
    upsert:()=>{},
  },
  scheduleDraftSave:()=>{},
  focusInput:()=>{},
  nextTick:fn=>fn(),
  finishWorkspaceDraft:async(draft)=>{
    calls.push(['body',draft.body]);
    note.content_hash='h2';
    return {saved_ref:'@note-1'};
  },
  saveCardProperties:async(ref,expectedHash,patch)=>{
    calls.push(['props',ref,expectedHash,patch]);
    return {saved_ref:ref};
  },
};

// beginEdit opens unified Zen editor configured for existing card
composer.beginEdit(ctx,note,{zen:true});
assert.equal(composer.open,true);
assert.equal(composer.zen,true);
assert.equal(composer.isCardEdit(),true);
assert.equal(composer.title(),'Edit note');
assert.equal(composer.saveLabel(),'Save changes');
assert.equal(composer.canSave(ctx),false);

// Property-only change in Zen is saveable without touching body draft
composer.draft.title='Updated title';
composer.draft.taskOptions.tags=['zen'];
assert.equal(composer.canSave(ctx),true);
await composer.save(ctx);
assert.deepEqual(calls,[['props','@note-1','h1',{tags:['zen'],title:'Updated title'}]]);
assert.equal(composer.open,false);

// Combined body + property change saves body first (updating hash), then properties
calls.length=0;
note.content_hash='h1';
composer.beginEdit(ctx,note,{zen:true});
composer.draft.text='Updated body';
composer.draft.instructions='Keep concise';
assert.equal(composer.canSave(ctx),true);
await composer.save(ctx);
assert.deepEqual(calls,[
  ['body','Updated body'],
  ['props','@note-1','h2',{processing_instructions:'Keep concise'}],
]);
assert.equal(composer.open,false);
""")

    def test_zen_edit_schedules_and_finishes_the_live_draft_object(self) -> None:
        self.run_node(r"""
const scheduled=[];
const finished=[];
const note={ref:'@note-1',path:'cards/note.md',kind:'note',title:'Note',content_hash:'h1',updated:'2026-10-01',body:'Original'};
const composer=createCaptureComposer();
const ctx={
  interactive:true,workspaceKey:'workspace-a',busy:false,
  store:{saveLocal:()=>true,upsert:()=>{}},
  taskDefaults:{status:'inbox',priority:'none',type:'task'},
  taskStatuses:['inbox','active'],taskPriorityPolicy:{none:'None'},taskTypePolicy:{task:'Task'},
  metadataSnapshot:()=>'',captureDraftFields:()=>({}),getDrafts:()=>[],
  scheduleDraftSave:draft=>{scheduled.push(draft);draft.revision=1;draft.content_hash='d1';},
  finishWorkspaceDraft:async draft=>{finished.push({draft,body:draft.body,revision:draft.revision});return {saved_ref:'@note-1'};},
  focusInput:()=>{},nextTick:fn=>fn(),setMode:()=>{},
};
composer.beginEdit(ctx,note,{zen:true});
composer.draft.text='First edit';
assert.equal(composer.persist(ctx),true);
assert.equal(scheduled.length,1);
assert.equal(scheduled[0],composer.draft);
assert.equal(composer.draft.revision,1);
assert.equal(composer.draft.content_hash,'d1');
composer.draft.text='Second edit';
await composer.save(ctx);
assert.equal(finished.length,1);
assert.equal(finished[0].body,'Second edit');
assert.equal(finished[0].revision,1);

// Partial Zen save (body succeeds, properties fail) resets draft to a fresh edit baseline
let failProps=true;
const card2={ref:'@note-2',path:'cards/note2.md',kind:'note',title:'Note 2',content_hash:'h-old',updated:'2026-10-01',body:'Before'};
const ctx2={
  ...ctx,
  getSelected:()=>card2,
  finishWorkspaceDraft:async draft=>{card2.content_hash='h-new';card2.updated='2026-10-02';return {saved_ref:'@note-2'};},
  saveCardProperties:async()=>failProps?null:{saved_ref:'@note-2'},
  actionMessage:()=>'Property conflict',
};
composer.beginEdit(ctx2,card2,{zen:true});
const oldId=composer.draft.id;
composer.draft.revision=2;
composer.draft.text='After';
composer.draft.title='New title';
assert.equal(await composer.save(ctx2),null);
assert.notEqual(composer.draft.id,oldId);
assert.equal(composer.draft.revision,0);
assert.equal(composer.draft.base_hash,'h-new');
assert.equal(composer.draft.originalText,'After');
assert.equal(composer.hasContent(ctx2),false);
failProps=false;
assert.deepEqual(await composer.save(ctx2),{saved_ref:'@note-2'});
""")

    def test_zen_edit_discards_the_live_draft_object(self) -> None:
        self.run_node(r"""
const discarded=[];
const note={ref:'@note-1',path:'cards/note.md',kind:'note',title:'Note',content_hash:'h1',updated:'2026-10-01',body:'Original'};
const composer=createCaptureComposer();
const ctx={
  interactive:true,workspaceKey:'workspace-a',busy:false,
  store:{saveLocal:()=>true,upsert:()=>{}},
  taskDefaults:{status:'inbox',priority:'none',type:'task'},
  taskStatuses:['inbox','active'],taskPriorityPolicy:{none:'None'},taskTypePolicy:{task:'Task'},
  metadataSnapshot:()=>'',captureDraftFields:()=>({}),getDrafts:()=>[],
  scheduleDraftSave:draft=>{draft.revision=1;draft.content_hash='d1';},
  discardWorkspaceDraft:async (draft,confirmed)=>{discarded.push({draft,confirmed,revision:draft.revision});},
  confirm:()=>true,focusInput:()=>{},nextTick:fn=>fn(),setMode:()=>{},
};
composer.beginEdit(ctx,note,{zen:true});
composer.draft.text='Edited';
assert.equal(composer.persist(ctx),true);
const live=composer.draft;
composer.draft.text='Edited again';
await composer.discard(ctx);
assert.equal(discarded.length,1);
assert.equal(discarded[0].draft,live);
assert.equal(discarded[0].confirmed,true);
assert.equal(discarded[0].draft.body,'Edited again');
assert.equal(discarded[0].revision,1);
""")


if __name__ == "__main__":
    unittest.main()
