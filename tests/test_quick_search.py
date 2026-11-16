import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import operations
import store
import taskweb
import web_assets
import web_payloads
import workspace


class QuickSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "cards").mkdir()
        (self.root / "mt-policy.yaml").write_text((ROOT / "mt-policy.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        self._old_cwd = Path.cwd()
        web_payloads.CARD_CACHE.entries.clear()
        store.workspace_reset() if hasattr(store, "workspace_reset") else None

    def tearDown(self):
        web_payloads.CARD_CACHE.entries.clear()
        self.tmp.cleanup()

    def _with_workspace(self, fn):
        ws = workspace.Workspace(root=self.root)
        with patch.object(workspace.Workspace, "resolve", return_value=ws), \
             patch.object(web_payloads, "workspace_root", return_value=self.root):
            return fn()

    def _make_card(self, title, *, kind="task", body="", tags=(), archived=False):
        card, _ = operations.create_card(
            kind,
            body,
            title=title,
            tags=list(tags),
        )
        if archived:
            operations.op_edit(
                workspace.Workspace(root=self.root),
                f"@{card.short_ref}",
                assignments=["archived=true"],
                match_mode="exact",
                sync=False,
            )
            card = next(c for c in store.discover(cache=web_payloads.CARD_CACHE) if c.frontmatter["id"] == card.frontmatter["id"])
        return card

    def test_search_cards_ranks_title_above_body_and_supports_ref_and_archived(self):
        def run():
            body_match = self._make_card("Weekly review", body="Contains needle in body")
            title_match = self._make_card("Needle in title", kind="note")
            archived_match = self._make_card("Archived needle", archived=True)

            res = web_payloads.search_cards("needle", limit=10)
            ids = [item["id"] for item in res["items"]]
            self.assertEqual(ids[0], title_match.frontmatter["id"])
            self.assertIn(body_match.frontmatter["id"], ids)
            self.assertIn(archived_match.frontmatter["id"], ids)
            self.assertEqual(ids[-1], archived_match.frontmatter["id"])

            ref_res = web_payloads.search_cards(f"@{body_match.short_ref}", limit=10)
            self.assertEqual([item["id"] for item in ref_res["items"]], [body_match.frontmatter["id"]])

            empty_res = web_payloads.search_cards("", limit=2)
            self.assertEqual(len(empty_res["items"]), 2)

        self._with_workspace(run)

    def test_http_search_endpoint_and_bad_limit(self):
        class FakeHandler(taskweb.TaskWebHandler):
            def __init__(self, path):
                self.path = path
                self.headers = {}
                self.rfile = io.BytesIO()
                self.wfile = io.BytesIO()
                self.status = None
                self.response_headers = {}

            def send_response(self, code, message=None):
                self.status = code

            def send_header(self, keyword, value):
                self.response_headers[keyword] = value

            def end_headers(self):
                pass

        def run():
            card = self._make_card("Spotlight search target")
            handler = FakeHandler("/api/search?q=Spotlight&limit=5")
            handler.do_GET()
            self.assertEqual(handler.status, 200)
            payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
            self.assertEqual([item["id"] for item in payload["items"]], [card.frontmatter["id"]])

            bad = FakeHandler("/api/search?q=test&limit=abc")
            bad.do_GET()
            self.assertEqual(bad.status, 400)

        self._with_workspace(run)

    def test_template_and_assets_include_quick_search_dialog(self):
        html = web_assets.load_template()
        js = web_assets.load_js()
        css = web_assets.load_css()
        self.assertIn('id="mt-quick-search"', html)
        self.assertIn('closedby="any"', html)
        self.assertIn('role="combobox"', html)
        self.assertIn('role="listbox"', html)
        self.assertIn("createQuickSearchController", js)
        self.assertIn("dialog.mt-quick-search:modal", css)


if __name__ == "__main__":
    unittest.main()
