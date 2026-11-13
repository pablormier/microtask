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


class CardNavigationTests(unittest.TestCase):
    def run_navigation(self, scenario: str) -> dict:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        script = r"""
const fs = require('fs');
const data = ['task-a', 'note-b', 'task-c'].map((id, i) => ({
  id, title: id, kind: i === 1 ? 'note' : 'task', status: i === 1 ? '' : 'inbox',
}));
const meta = { workspace_key: 'test-workspace', interactive: false };
const handlers = {};
const rootFocus = { focus() {}, isConnected: true };
global.document = {
  activeElement: rootFocus, documentElement: { dataset: {} }, addEventListener() {},
  getElementById(id) { return {textContent: JSON.stringify(
    id === 'task-data' ? data : id === 'mt-config' ? meta : [])}; },
};
global.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
global.setInterval = () => 0;
global.window = { addEventListener(type, fn) { (handlers[type] ||= []).push(fn); } };
global.location = { hash: '', pathname: '/', search: '?view=test' };
function changeUrl(url) {
  if (url !== undefined) location.hash = new URL(url, 'http://localhost/?view=test').hash;
}
const entries = [{ state: null, hash: '' }];
let position = 0;
global.history = {
  get state() { return entries[position].state; },
  pushState(state, _, url) {
    changeUrl(url);
    entries.splice(position + 1);
    entries.push({ state, hash: location.hash });
    position++;
  },
  replaceState(state, _, url) {
    changeUrl(url);
    entries[position] = { state, hash: location.hash };
  },
  go(delta) {
    const next = position + delta;
    if (next < 0 || next >= entries.length) throw new Error('Left app history');
    position = next;
    location.hash = entries[position].hash;
    for (const type of ['popstate', 'hashchange']) {
      for (const callback of handlers[type] || []) callback();
    }
  },
  back() { this.go(-1); }, forward() { this.go(1); },
};
function makeApp() {
  for (const key of Object.keys(handlers)) delete handlers[key];
  const app = taskBoard();
  app.$nextTick = callback => callback();
  app.$refs = {};
  app.renderSelectedPreview = () => {};
  app.loadWorkspace = () => { app.applyPayload({tasks:data,meta}); app.ready=true; app.readCardLocation(); };
  app.init();
  return app;
}
"""
        source = load_js()
        result = subprocess.run([node], input=script + "\n" + source + "\n" + scenario,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_card_trail_back_forward_reload_and_close(self):
        result = self.run_navigation(r"""
let app = makeApp();
app.mode = 'notes'; app.filters.search = 'unchanged';
app.select(data[0]);
const first = {id:app.selectedId, hash:location.hash, back:app.canGoBackReference()};
app.select(data[1]); app.select(data[2]);
app.select(data[2]); // Following a self-reference does not add a history entry.
app.backReference();
const back = {id:app.selectedId, hash:location.hash, canBack:app.canGoBackReference()};
history.back();
const firstAgain = {id:app.selectedId, canBack:app.canGoBackReference()};
history.forward();
const forward = {id:app.selectedId, mode:app.mode, search:app.filters.search};
app = makeApp(); // Reload retains the browser history marker.
const reloaded = {id:app.selectedId, canBack:app.canGoBackReference()};
app.clearSelection();
const closed = {id:app.selectedId, hash:location.hash, search:location.search};
console.log(JSON.stringify({first, back, firstAgain, forward, reloaded, closed, entries:entries.length}));
""")
        self.assertEqual(result, {
            "first": {"id": "task-a", "hash": "#mt-card-task-a", "back": False},
            "back": {"id": "note-b", "hash": "#mt-card-note-b", "canBack": True},
            "firstAgain": {"id": "task-a", "canBack": False},
            "forward": {"id": "note-b", "mode": "notes", "search": "unchanged"},
            "reloaded": {"id": "note-b", "canBack": True},
            "closed": {"id": None, "hash": "", "search": "?view=test"},
            "entries": 4,
        })

    def test_direct_missing_malformed_and_deleted_targets(self):
        result = self.run_navigation(r"""
location.hash = '#mt-card-note-b';
let app = makeApp();
const direct = {id:app.selectedId, back:app.canGoBackReference()};
app.backReference(); // Must not leave the page on a direct link.
app.select(data[0]);
app.tasks = app.tasks.filter(task => task.id !== 'note-b');
const deletedPrevious = app.canGoBackReference();
location.hash = '#mt-card-missing'; app.readCardLocation();
const missing = {id:app.selectedId, back:app.canGoBackReference(), hash:location.hash};
location.hash = '#mt-card-%E0%A4%A'; app.readCardLocation();
console.log(JSON.stringify({direct, deletedPrevious, missing, malformed:app.selectedId}));
""")
        self.assertEqual(result, {
            "direct": {"id": "note-b", "back": False}, "deletedPrevious": False,
            "missing": {"id": None, "back": False, "hash": "#mt-card-missing"}, "malformed": None,
        })

    def test_cards_outside_the_filtered_list_open_and_stay_requested(self):
        result = self.run_navigation(r"""
(async () => {
  const requests = [];
  global.fetch = async url => {
    const params = new URLSearchParams(url.split('?')[1]);
    if (url.startsWith('/api/item?')) {
      return { ok: true, json: async () => ({ item: { ...data.find(card => card.id === params.get('id')), body: '' } }) };
    }
    requests.push(params.getAll('include'));
    return { ok: true, json: async () => ({ tasks: data, meta }) };
  };
  const app = makeApp();
  app.tasks = [data[0]]; // The filters leave the other cards out.
  app.select(data[0]);
  // A reference target opens from the summary the open card carries.
  app.selected = { ...data[0], body: '', references: [{ target: data[1] }] };
  app.openReference({ target: { closest() { return { target: '', getAttribute() { return referenceHref(data[1]); } }; } },
    currentTarget: { contains() { return true; } }, button: 0, preventDefault() {} });
  const referenced = { id: app.selectedId, back: app.canGoBackReference(), requests: requests.length };
  // A linked card is requested once, then opened when the server has it.
  app.tasks = [data[0]];
  location.hash = '#mt-card-task-c'; app.readCardLocation(); app.readCardLocation();
  await new Promise(resolve => setTimeout(resolve, 0));
  // A response requested before the card was opened keeps it open and asks again;
  // one that did ask for it and came back without it closes the card.
  const sent = requests.length;
  app.applyPayload({ tasks: [data[0]], meta, include: [] });
  const stale = { id: app.selectedId, kept: app.tasks.some(card => card.id === 'task-c') };
  await new Promise(resolve => setTimeout(resolve, 0));
  const asked = requests.length - sent;
  app.applyPayload({ tasks: [data[0]], meta, include: ['task-c'] });
  console.log(JSON.stringify({ referenced, linked: app.selectedId === null ? 'closed' : 'open', requests: requests.slice(0, sent),
    stale, asked }));
})();
""")
        self.assertEqual(result["stale"], {"id": "task-c", "kept": True})
        self.assertEqual(result["asked"], 1)
        self.assertEqual(result["linked"], "closed")
        self.assertEqual(result["referenced"], {"id": "note-b", "back": True, "requests": 0})
        self.assertEqual(len(result["requests"]), 1)
        self.assertIn("task-c", result["requests"][0])

    def test_reference_modifier_clicks_keep_native_link_behavior(self):
        result = self.run_navigation(r"""
const app = makeApp(); app.select(data[0]);
const href = referenceHref(data[1]);
const link = { target:'', getAttribute() { return href; } };
function click(options) {
  let prevented = false;
  app.openReference({ target:{closest() {return link;}}, currentTarget:{contains() {return true;}},
    button:0, preventDefault() {prevented=true;}, ...options });
  return prevented;
}
const modified = [click({ctrlKey:true}), click({metaKey:true}), click({shiftKey:true}), click({button:1})];
link.target = '_blank'; const blank = click({}); link.target = '';
const idBefore = app.selectedId;
const normal = click({});
console.log(JSON.stringify({modified, blank, idBefore, normal, opened:app.selectedId}));
""")
        self.assertEqual(result, {
            "modified": [False] * 4, "blank": False, "idBefore": "task-a",
            "normal": True, "opened": "note-b",
        })

    def test_reference_hover_activates_and_clears_hovercard(self):
        result = self.run_navigation(r"""
(async () => {
  const app = makeApp(); app.select(data[0]);
  const href = referenceHref(data[1]);
  const link = {
    target: '',
    getAttribute() { return href; },
    getBoundingClientRect() { return { top: 100, bottom: 120, left: 50, right: 100 }; },
    contains(el) { return el === link; },
    closest(selector) { return selector.includes('mt-card-') ? link : null; }
  };
  app.onReferenceHover({ target: link });
  await new Promise(r => setTimeout(r, 200));
  const hovered = {
    hasCard: Boolean(app.hoverCard),
    id: app.hoverCard ? app.hoverCard.id : null,
    top: app.hoverPos.top,
    left: app.hoverPos.left,
    placeAbove: app.hoverPos.placeAbove
  };
  app.onReferenceLeave({ target: link, relatedTarget: null });
  await new Promise(r => setTimeout(r, 160));
  const cleared = Boolean(app.hoverCard);
  console.log(JSON.stringify({ hovered, cleared }));
})();
""")
        self.assertEqual(result, {
            "hovered": {"hasCard": True, "id": "note-b", "top": 126, "left": 50, "placeAbove": False},
            "cleared": False,
        })

