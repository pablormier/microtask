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
captureA.draft={body:'capture A'};
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
  isEmpty:d=>d.mode==='new'&&!String(d.title||'').trim()&&!String(d.text||'').trim()&&!d.content_hash,
  forgetEmpty:()=>{}, upsert:d=>{ if(!rows.includes(d)) rows.push(d); },
};
const ctx={interactive:true,workspaceKey:'workspace-a',busy:false,store,
  taskDefaults:{status:'inbox',priority:'none',type:'task'},taskStatuses:['inbox','active'],
  taskPriorityPolicy:{none:'None',high:'High'},taskTypePolicy:{task:'Task'},
  taskOptionsFromDraft:d=>d.taskOptions||{},metadataSnapshot:()=>'',captureDraftFields:()=>({}),
  getDrafts:()=>rows,scheduleDraftSave:()=>{},focusInput:()=>{},nextTick:fn=>fn(),setMode:()=>{},
};
const editor=createCaptureComposer();
editor.beginNew(ctx,null,'note');
editor.draft.body='A recoverable capture';
assert.equal(editor.persist(ctx),true);
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

    def test_content_conflict_stays_open_and_save_returns_result(self) -> None:
        self.run_node(r"""
const task={id:'task',ref:'@task',path:'cards/task.md',kind:'task',content_hash:'base',body:'# Existing'};
const editor=createContentEditor();
let resolveSave;
const ctx={workspaceKey:'ws-1',selected:task,interactive:true,busy:false,draftBusy:false,
  clearStatus:()=>{},focusInput:()=>{},
  saveCardBody:()=>new Promise(resolve=>{resolveSave=resolve;})};
editor.begin(ctx);
editor.text='# Edited';
editor.persist();
const saving=editor.save(ctx);
assert.equal(editor.saving,true);
assert.equal(editor.canSave(ctx),false);
assert.equal(await editor.save(ctx),null);
resolveSave({ok:false,conflict:true,error:'stale'});
assert.equal(await saving,null);
assert.equal(editor.open,true);
assert.equal(editor.conflict,true);
assert.equal(editor.error,'This card changed since you opened it. Copy your text or reload.');
assert.equal(editor.saving,false);

const savingOk=editor.save(ctx);
resolveSave({ok:true,payload:{saved_ref:'@task'}});
assert.deepEqual(await savingOk,{saved_ref:'@task'});
assert.equal(editor.open,false);
assert.equal(editor.saving,false);
""")

    def test_zen_mode_lifecycle_and_escape_hierarchy(self) -> None:
        self.run_node(r"""
const store={
  isEmpty:d=>d.mode==='new'&&!String(d.title||'').trim()&&!String(d.text||'').trim()&&!d.content_hash,
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
assert.equal(content.handleEscape(contentCtx),true);
assert.equal(content.zen,false);
content.close(contentCtx);
assert.equal(content.zen,false);
assert.equal(content.open,false);
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

    def test_comment_mention_controller_and_template_wiring(self) -> None:
        self.run_node(r"""
let commentText = 'See @log';
const cards = [{ref:'@4e91a7c2', title:'Fix login timeout bug', kind:'task', status:'active'}];
const controller = mentionMethods({
  setText: (_self, ctx, nextText) => ctx.setText(nextText),
  mentionIdPrefix: 'mt-comment-mention-',
});
const ctx = { getCards: () => cards, setText: next => { commentText = next; }, nextTick: fn => fn() };
const textarea = {
  value: commentText,
  selectionStart: commentText.length,
  focus(){},
  setSelectionRange(){},
};
controller.updateMention(ctx, textarea);
assert.equal(controller.mention.open, true);
assert.equal(controller.mention.items[0].ref, '@4e91a7c2');
controller.pickMention(ctx, controller.mention.items[0], textarea);
assert.equal(commentText, 'See @4e91a7c2 ');
assert.equal(controller.mention.open, false);
""")
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        import web_assets
        html = web_assets.load_template()
        self.assertIn('id="mt-comment-mention-list"', html)
        self.assertIn('commentMention.pickMention(commentMentionContext(), item, $refs.commentInput)', html)
        self.assertNotIn("{comment_mention_menu}", html)

if __name__ == "__main__":
    unittest.main()
