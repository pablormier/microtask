#!/usr/bin/env python3
"""Serve the local web dashboard for the task workspace."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "scripts"))

import card_format  # noqa: E402
import card_query  # noqa: E402
import cli  # noqa: E402
import drafts as draft_store  # noqa: E402
import git_ops  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace  # noqa: E402

try:
    from .web_queries import collection_results
except ImportError:  # Direct script entry point or file-based test loader.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from web_queries import collection_results


PICO_CDN = "https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css"
ALPINE_CDN = "https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js"
MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js"
DOMPURIFY_CDN = "https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js"
KATEX_CSS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css"
KATEX_JS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.js"
KATEX_AUTO_RENDER_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/contrib/auto-render.min.js"

# Parsed cards reused across requests; every read re-checks each file's stat signature.
CARD_CACHE = store.CardCache()

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_INCLUDED_IDS = 8


def current_workspace() -> Workspace:
    return Workspace.resolve()


def workspace_root() -> Path:
    return current_workspace().root


def application_version(app_root: Path = APP_ROOT) -> str:
    """Return a fingerprint of the dashboard and core application files on disk."""
    digest = hashlib.sha256()
    paths: list[Path] = []
    for folder in (app_root / "scripts", app_root / "tools"):
        if folder.is_dir():
            paths.extend(sorted(folder.rglob("*.py")))
    web_dir = app_root / "tools" / "web"
    if web_dir.is_dir():
        paths.extend(sorted(path for path in web_dir.rglob("*") if path.is_file()))
    for path in sorted(set(paths)):
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = str(path.relative_to(app_root))
        digest.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}\n".encode("utf-8"))
    return digest.hexdigest()


def load_css() -> str:
    css_files = [
        "theme.css",
        "components.css",
        "layout.css",
        "board.css",
        "views.css",
        "detail.css",
        "composer.css",
        "popover.css",
    ]
    return "\n".join((WEB_DIR / "styles" / f).read_text(encoding="utf-8") for f in css_files)


def load_js(web_dir: Path = WEB_DIR) -> str:
    js_files = [
        "markdown.js",
        "card_helpers.js",
        "picker.js",
        "metadata_editor.js",
        "draft_store.js",
        "capture_composer.js",
        "property_editor.js",
        "content_editor.js",
        "filters_query.js",
        "draft_coordinator.js",
        "card_detail.js",
        "board.js",
    ]
    return "\n".join((web_dir / "js" / f).read_text(encoding="utf-8") for f in js_files)


def load_template() -> str:
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    card = (WEB_DIR / "templates" / "card.html").read_text(encoding="utf-8")
    toolbar = (WEB_DIR / "templates" / "toolbar.html").read_text(encoding="utf-8")
    timer_menu = (WEB_DIR / "templates" / "timer_menu.html").read_text(encoding="utf-8")
    view_focus = (
        (WEB_DIR / "templates" / "views" / "focus.html").read_text(encoding="utf-8")
        .replace("{card}", card)
        .replace("{timer_menu}", timer_menu.replace("{timer_task}", "focusTask()").replace("{timer_blocked}", ""))
    )
    view_cards = (WEB_DIR / "templates" / "views" / "cards.html").read_text(encoding="utf-8").replace("{card}", card)
    view_deadlines = (WEB_DIR / "templates" / "views" / "deadlines.html").read_text(encoding="utf-8")
    view_drafts = (WEB_DIR / "templates" / "views" / "drafts.html").read_text(encoding="utf-8")
    drawer = (
        (WEB_DIR / "templates" / "drawer.html").read_text(encoding="utf-8")
        .replace("{timer_menu}", timer_menu.replace("{timer_task}", "selected").replace("{timer_blocked}", " || !!contentEditor.draft"))
    )
    capture_composer = (WEB_DIR / "templates" / "capture_composer.html").read_text(encoding="utf-8")
    card_metadata_editor = (WEB_DIR / "templates" / "card_metadata_editor.html").read_text(encoding="utf-8")

    return (
        index_html
        .replace("{toolbar}", toolbar)
        .replace("{view_focus}", view_focus)
        .replace("{view_cards}", view_cards)
        .replace("{view_deadlines}", view_deadlines)
        .replace("{view_drafts}", view_drafts)
        .replace("{drawer}", drawer)
        .replace("{capture_composer}", capture_composer)
        .replace("{card_metadata_editor}", card_metadata_editor)
    )


def safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def extract_excerpt(text: str, max_len: int = 180) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if re.fullmatch(r"\s*(?:`{3,}|~{3,})[^`~]*", line):
            continue
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        line = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", line)
        line = re.sub(r"^\s*>\s*", "", line)
        if line.strip():
            cleaned_lines.append(line.strip())
    clean_content = " ".join(cleaned_lines)
    clean_content = re.sub(r'''!?\[([^\]]*)\]\((?:[^\s()]|\([^()]*\))*(?:\s+["'][^"']*["'])?\s*\)''', r"\1", clean_content)
    clean_content = re.sub(r"`([^`]+)`", r"\1", clean_content).replace("`", "")
    clean_content = re.sub(r"\*\*([^*]+)\*\*|~~([^~]+)~~", lambda match: match[1] or match[2], clean_content)
    clean_content = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"\1", clean_content)
    clean_content = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", clean_content)
    clean_content = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", clean_content)
    excerpt = " ".join(clean_content.split())
    if len(excerpt) > max_len:
        excerpt = excerpt[:max_len - 3].rstrip() + "..."
    return excerpt


def task_to_dict(task: store.Card, reference_index: store.ReferenceIndex | None = None, *, detail: bool = True, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else store.policy()
    fm = task.frontmatter
    tags = fm.get("tags", [])
    body = task.body
    base_uri = task.path.resolve().parent.as_uri() + "/"
    rank = {name: index for index, name in enumerate(cfg.get("priorities", {}))}
    processing_instructions = fm.get("processing_instructions", "")
    comments = fm.get("comments", [])
    time_entries = fm.get("time_entries", [])
    result = {
        "id": fm["id"],
        "kind": task.kind,
        "ref": f"@{task.short_ref}",
        "status": fm.get("status", ""),
        "priority": fm["priority"],
        "type": fm.get("type", ""),
        "project": fm.get("project"),
        "topic": fm.get("topic"),
        "due": fm.get("due"),
        "due_kind": store.due_kind_value(fm),
        "created": fm["created"],
        "updated": fm["updated"],
        "completed": fm.get("completed", ""),
        "claimed_by": fm.get("claimed_by"),
        "claimed_at": fm.get("claimed_at"),
        "archived": store.is_archived(task),
        "hidden": store.is_hidden(task),
        "tags": list(tags),
        "title": task.title,
        "next_action": task.next_action,
        "needs_restart": operations.next_action_needs_restart_step(task),
        "display_rank": operations.task_display_sort_key(task, rank),
        "excerpt": extract_excerpt(body),
        "content_hash": hashlib.sha256(task.raw.encode("utf-8")).hexdigest(),
        "needs_review": fm.get("needs_review") is True,
        "time_seconds": store.saved_time_seconds(task) if task.kind == "task" else 0,
        "comment_count": len(comments) if isinstance(comments, list) else 0,
        "path": str(task.path.relative_to(workspace_root())) if task.path.is_relative_to(workspace_root()) else str(task.path),
        "base_uri": base_uri,
    }
    if detail:
        references = store.item_references(task, reference_index) if reference_index else []
        result.update({
            "body": body,
            "processing_instructions": processing_instructions,
            "comments": comments,
            "time_entries": time_entries,
            "references": references,
            "reference_spans": {
                "body": reference_index.occurrences(body),
                "next_action": reference_index.occurrences(task.next_action),
                "processing_instructions": reference_index.occurrences(processing_instructions),
                "comments": [reference_index.occurrences(comment["text"]) for comment in comments],
            } if reference_index else {},
        })
    return result

def dashboard_detail(identity: str) -> dict | None:
    store.pin_demo_state()
    store.maybe_seed_demo()
    items = store.discover(cache=CARD_CACHE)
    item = next((item for item in items if str(item.frontmatter.get("id") or item.path.stem) == identity), None)
    return task_to_dict(item, store.ReferenceIndex(items)) if item else None



def dashboard_meta(*, api_token: str = "", items: list[store.Card] | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else store.policy()
    default_status, default_priority, default_type = operations.task_defaults(cfg)
    statuses = list(cfg.get("statuses", {}))
    configured_status_labels = cfg.get("statuses", {})
    if not isinstance(configured_status_labels, dict):
        configured_status_labels = {}
    status_labels = {
        status: str(configured_status_labels.get(status) or status)
        for status in statuses
    }
    configured_view_labels = cfg.get("view_labels", {})
    if not isinstance(configured_view_labels, dict):
        configured_view_labels = {}
    view_labels = {"archive": "Archive"}
    view_labels.update({
        str(name): str(label)
        for name, label in configured_view_labels.items()
        if label is not None and str(label)
    })
    rules = cfg.get("rules", {})
    raw_limit = rules.get("max_doing_tasks") if isinstance(rules, dict) else None
    max_doing = int(str(raw_limit)) if raw_limit is not None and str(raw_limit).isdigit() else 1
    timers = transaction.read_time_timers()
    timers = {
        task_id: {**timer, "saved": store.timer_session_saved(task_id, timer)}
        for task_id, timer in timers.items()
    }
    item_values = items or []
    workspace_tags: set[str] = set()
    usage: dict[str, dict[str, int]] = {"project": {}, "topic": {}, "tag": {}}
    for item in item_values:
        raw_tags = item.frontmatter.get("tags") or []
        tag_values = raw_tags if isinstance(raw_tags, list) else [raw_tags]
        workspace_tags.update(str(tag) for tag in tag_values if tag)
        for tag in {str(tag) for tag in tag_values if tag}:
            usage["tag"][tag] = usage["tag"].get(tag, 0) + 1
        for field in ("project", "topic"):
            if item.frontmatter.get(field):
                name = str(item.frontmatter[field])
                usage[field][name] = usage[field].get(name, 0) + 1
    marker = store.view_marker()
    return {
        "workspace_key": hashlib.sha256(str(workspace_root().resolve()).encode("utf-8")).hexdigest()[:16],
        "status_labels": status_labels,
        "view_labels": view_labels,
        "priority_order": list(cfg.get("priorities", {}).keys()),
        "due_kinds": list(cfg.get("due_kinds", {}).keys()),
        "task_defaults": {"status": default_status, "priority": default_priority, "type": default_type},
        "task_statuses": [status for status in statuses if status != "done"],
        "task_priorities": cfg.get("priorities", {}),
        "task_types": cfg.get("types", {}),
        "task_due_kinds": cfg.get("due_kinds", {}),
        "suggested_tags": cfg.get("suggested_tags", []),
        "workspace_projects": sorted({str(item.frontmatter["project"]) for item in item_values if item.frontmatter.get("project")}),
        "topic_tree": card_format.topic_tree(usage["topic"], operations.suggested_topics(cfg)),
        "workspace_tags": sorted(workspace_tags),
        "workspace_usage": usage,
        "max_doing_tasks": max_doing,
        "show_year_progress": bool(rules.get("show_year_progress", True)),
        "show_deadline_progress": bool(rules.get("show_deadline_progress", True)),
        "deadline_progress_start": str(rules.get("deadline_progress_start") or "created"),
        "api_token": api_token,
        "demo": store.demo_active(),
        "view": ({"name": marker["name"], "source": marker["source"]} if marker else None),
        "time_running": timers,
        "timer_warn_hours": float(rules.get("timer_warn_hours", 8)),
    }


def included_ids(value: object) -> list[str]:
    """Card IDs a client asked to receive whatever the filters say; a short list of text only."""
    values = value if isinstance(value, list) else []
    return [item for item in values if isinstance(item, str) and item][:MAX_INCLUDED_IDS]


def request_query(values: object) -> card_query.Query:
    """The validated query a request's parameters describe; raises QueryError with a message for the client."""
    if values is not None and not isinstance(values, dict):
        raise card_query.QueryError("query must be an object")
    query = card_query.query_from_mapping(values or {})
    card_query.validate(query, statuses=store.statuses(), priorities=list(store.policy().get("priorities", {})))
    return query


def dashboard_payload(*, api_token: str = "", query: card_query.Query | None = None, include: object = None) -> dict:
    """Collections for ``query`` plus only the card summaries a client needs to show them.

    ``tasks`` holds the cards in any collection, the targets of saved drafts, and the
    ``include`` IDs (the open card and its navigation trail), which are echoed back.
    Workspace-wide figures are in ``meta``.
    """
    store.pin_demo_state()
    store.maybe_seed_demo()
    cfg = store.policy()
    query = query or card_query.DEFAULT
    statuses = list(cfg.get("statuses", {}))
    tasks = store.discover(cache=CARD_CACHE)
    ordered = sorted(
        tasks,
        key=lambda t: statuses.index(t.frontmatter.get("status") or "")
        if (t.frontmatter.get("status") or "") in statuses
        else len(statuses),
    )
    meta = dashboard_meta(api_token=api_token, items=tasks, cfg=cfg)
    now = datetime.now()
    meta["focus"] = operations.focus_context(card_query.select(tasks, card_query.Query(kind=("task",)), now=now))
    summaries = [task_to_dict(task, detail=False, cfg=cfg) for task in ordered]
    rank = {name: index for index, name in enumerate(cfg.get("priorities", {}))}
    collections = collection_results(
        ordered, query, now=now, open_statuses=store.open_statuses(),
        done_cutoff=card_query.default_done_cutoff(query, store.done_window(), date.today()),
        display_rank=lambda card: operations.task_display_sort_key(card, rank))
    drafts = draft_store.list_drafts(workspace_root())
    included = included_ids(include)
    wanted = {identity for ids in collections.values() for identity in ids} | set(included)
    draft_targets = {draft.get("target") for draft in drafts}
    meta["draft_count"] = len(drafts)
    # The Focus view shows only open cards that pass the filters, so send the focus entries for those.
    shown = set(collections["open"])
    for key in ("candidates", "alerts"):
        meta["focus"][key] = [entry for entry in meta["focus"][key] if entry["id"] in shown]
    meta["focus"]["followup_ids"] = [identity for identity in meta["focus"]["followup_ids"] if identity in shown]
    meta["doing_count"] = len(card_query.select(
        tasks, card_query.Query(status=("doing",), hidden="include"), now=now))
    # Changes when any card is added, removed, renamed or converted, so an open card can refresh its references.
    meta["reference_revision"] = hashlib.sha256(json.dumps(sorted(
        [str(item["id"]), item["ref"], item["title"], item["kind"]] for item in summaries
    )).encode("utf-8")).hexdigest()[:16]
    return {
        "tasks": [item for item in summaries if item["id"] in wanted or item["path"] in draft_targets],
        "statuses": statuses,
        "meta": meta,
        "query": card_query.query_to_mapping(query),
        # Echoed so a client can tell a card that is gone from one this request never asked for.
        "include": included,
        "collections": collections,
    }


def fill_placeholders(page: str, values: dict[str, str]) -> str:
    pattern = re.compile("|".join(re.escape(key) for key in values))
    return pattern.sub(lambda match: values[match.group(0)], page)


def build_html(config: dict) -> str:
    nonce = secrets.token_urlsafe(18)
    template = load_template()
    css = load_css()
    js = load_js()

    page = fill_placeholders(template, {
        "{pico_css}": PICO_CDN,
        "{katex_css}": KATEX_CSS_CDN,
        "{alpine_js}": ALPINE_CDN,
        "{marked_js}": MARKED_CDN,
        "{dompurify_js}": DOMPURIFY_CDN,
        "{katex_js}": KATEX_JS_CDN,
        "{katex_auto_render_js}": KATEX_AUTO_RENDER_CDN,
        "{dashboard_css}": css,
        "{nonce}": nonce,
        "{app_script}": js,
    })
    pending = ("{config_json}",)
    leftover = [m for m in re.findall(r"\{[a-z_]+\}", page) if m not in pending]
    if leftover:
        raise ValueError(f"unreplaced placeholders in dashboard HTML: {leftover}")
    return fill_placeholders(page, {"{config_json}": safe_json(config)})


def bootstrap_config(token: str) -> dict:
    return {
        "api_token": token,
        "workspace_key": hashlib.sha256(str(workspace_root().resolve()).encode("utf-8")).hexdigest()[:16],
    }



class BackgroundSync:
    """Run auto-sync after dashboard writes, one sync at a time.

    Writes that arrive during a sync are folded into one more sync. The last
    failure is kept so the board can show it on its next refresh.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.again = False
        self.warning = ""

    def request(self) -> None:
        if not store.policy().get("rules", {}).get("auto_sync", False):
            return
        with self.lock:
            if self.thread is not None:
                self.again = True
                return
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def run(self) -> None:
        while True:
            warning = sync_once()
            with self.lock:
                self.warning = warning
                if not self.again:
                    self.thread = None
                    return
                self.again = False

    def wait(self, timeout: float | None = None) -> None:
        with self.lock:
            thread = self.thread
        if thread is not None:
            thread.join(timeout)


def sync_once() -> str:
    """Sync the workspace and return a warning, or an empty string on success."""
    ws = current_workspace()
    status = git_ops.workspace_status(ws.root)
    if status.state in {"missing", "uninitialized"}:
        return transaction.auto_sync_unavailable_message(status, ws)
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        returncode = transaction.command_sync(argparse.Namespace(message=None), ws=ws)
    if returncode == 0:
        return ""
    error_lines = [line for line in errors.getvalue().splitlines()
                   if not line.startswith(("Workspace: ", "Application: "))]
    return "\n".join([*error_lines, output.getvalue()]).strip() or "automatic sync failed"


# Draft autosaves change only drafts/, which sync does not need to publish on every keystroke.
UNSYNCED_ACTIONS = {"/api/draft-save", "/api/draft-discard"}


class TaskWebServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], token: str):
        super().__init__(address, TaskWebHandler)
        self.token = token
        self.sync = BackgroundSync()
        self.app_version = application_version(APP_ROOT)

    def restart_notice(self) -> str:
        if application_version(APP_ROOT) != self.app_version:
            return "Application updated on disk; restart the dashboard to use the new version."
        return ""


class TaskWebHandler(BaseHTTPRequestHandler):
    server: TaskWebServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = safe_json(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, page: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = page.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            page = build_html(bootstrap_config(self.server.token))
            self.send_html(page)
            return
        if url.path == "/api/item":
            values = parse_qs(url.query).get("id", [])
            if len(values) != 1 or not values[0]:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "one card id is required"})
                return
            try:
                item = dashboard_detail(values[0])
            except ValueError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            if item is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Card no longer exists in this workspace."})
            else:
                self.send_json(HTTPStatus.OK, {"item": item})
            return
        if url.path == "/api/items":
            try:
                params = parse_qs(url.query)
                include = params.pop("include", None)
                payload = dashboard_payload(api_token=self.server.token, include=include, query=request_query(params))
                payload["sync_warning"] = self.server.sync.warning
                payload["restart_notice"] = self.server.restart_notice()
                # An address with older spellings: the client adopts the canonical `query`.
                payload["query_aliased"] = card_query.uses_aliases(params)
            except card_query.QueryError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except ValueError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            self.send_json(HTTPStatus.OK, payload)
            return
        if self.path == "/api/drafts":
            try:
                self.send_json(HTTPStatus.OK, {"drafts": draft_store.list_drafts(workspace_root())})
            except (OSError, ValueError) as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.headers.get("X-Microtask-Token") != self.server.token:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        if not isinstance(body, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "request body must be an object"})
            return
        try:
            query = request_query(body.get("query"))
        except card_query.QueryError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        status, payload = handle_api_action(self.path, body, self.server.token)
        if status == HTTPStatus.OK:
            if self.path not in UNSYNCED_ACTIONS:
                self.server.sync.request()
            # Actions return receipts; build the queried collection once, after success.
            payload = {**dashboard_payload(api_token=self.server.token, query=query, include=body.get("include")),
                       "sync_warning": self.server.sync.warning,
                       "restart_notice": self.server.restart_notice(),
                       **payload}
        self.send_json(status, payload)


def item_by_ref(ref: object) -> dict | None:
    if not isinstance(ref, str) or not store.SHORT_REF_RE.fullmatch(ref):
        return None
    store.pin_demo_state()
    store.maybe_seed_demo()
    items = store.items_by_ref(ref)
    item = items[0] if len(items) == 1 and f"@{items[0].short_ref}" == ref else None
    return task_to_dict(item, detail=False) if item else None


def _run_operation(action, message: str, *, include_saved_ref: bool = False) -> tuple[HTTPStatus, dict]:
    try:
        result = action(current_workspace())
    except store.ConflictError as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except store.TaskError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    payload: dict[str, object] = {"message": message}
    if include_saved_ref:
        payload["saved_ref"] = result.receipt.get("ref")
    return HTTPStatus.OK, payload


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def valid_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def handle_review(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    """Clear needs_review on any card kind, guarded by the loaded content hash."""
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    if not item.get("needs_review"):
        return HTTPStatus.OK, {"message": "Already reviewed", "saved_ref": item["ref"]}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            expected_hash=expected_hash,
            assignments=["needs_review=false"],
            match_mode="exact",
            sync=False,
        ),
        "Marked reviewed",
        include_saved_ref=True,
    )


def handle_checkbox(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    """Toggle one task-list checkbox in the card body, guarded by the loaded content hash."""
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    index, count, checked = body.get("index"), body.get("count"), body.get("checked")
    for name, value in (("index", index), ("count", count)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return HTTPStatus.BAD_REQUEST, {"error": f"{name} must be a non-negative integer"}
    if not isinstance(checked, bool):
        return HTTPStatus.BAD_REQUEST, {"error": "checked must be true or false"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    card = store.items_by_ref(item["ref"])[0]
    try:
        new_body = card_format.toggle_task_checkbox(card.body, index, count, checked)
    except ValueError as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            expected_hash=expected_hash,
            body_mode="replace",
            body_text=new_body,
            match_mode="exact",
            sync=False,
        ),
        "Checklist updated",
        include_saved_ref=True,
    )


def handle_draft_save(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    expected_revision = body.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be a non-negative integer"}
    expected_hash = body.get("expected_hash")
    if expected_revision > 0 and not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash when updating a draft"}
    if not isinstance(body.get("body"), str):
        return HTTPStatus.BAD_REQUEST, {"error": "body must be valid UTF-8 text"}
    kind = body.get("kind")
    mode = body.get("mode")
    if not isinstance(kind, str) or kind not in {"note", "task"}:
        return HTTPStatus.BAD_REQUEST, {"error": "kind must be note or task"}
    if not isinstance(mode, str) or mode not in {"new", "edit"}:
        return HTTPStatus.BAD_REQUEST, {"error": "mode must be new or edit"}
    title = body.get("title", "")
    if not isinstance(title, str) or not valid_utf8(title):
        return HTTPStatus.BAD_REQUEST, {"error": "title must be valid UTF-8 text"}
    fields: dict[str, object] = {"kind": kind, "mode": mode, "title": title}
    for field in ("status", "priority", "type", "project", "topic", "tags", "due", "due_kind"):
        if field not in body:
            continue
        value = body[field]
        if field == "tags":
            if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
                return HTTPStatus.BAD_REQUEST, {"error": "tags must be an array of strings"}
        elif value is not None and not isinstance(value, str):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be text or null"}
        if isinstance(value, str) and not valid_utf8(value):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be valid UTF-8 text"}
        fields[field] = value
    if "instructions" in body:
        fields["instructions"] = body["instructions"]
    if mode == "new":
        fields.update(capture_id=body.get("capture_id"), created=body.get("created"))
    elif mode == "edit" and expected_revision == 0:
        item = item_by_ref(body.get("target_ref"))
        if item is None or item["kind"] != kind:
            return HTTPStatus.BAD_REQUEST, {"error": "edit draft target is unavailable"}
        item_path = workspace_root() / str(item["path"])
        if item_path.is_symlink() or not item_path.is_file():
            return HTTPStatus.BAD_REQUEST, {"error": "edit draft target is unavailable"}
        supplied_hash = body.get("base_hash")
        if supplied_hash is None:
            supplied_hash = draft_store.UNVERIFIED_BASE_HASH
        if not _valid_sha256(supplied_hash):
            return HTTPStatus.BAD_REQUEST, {"error": "base_hash must be a SHA-256 hash"}
        supplied_updated = body.get("base_updated", "")
        if not isinstance(supplied_updated, str):
            return HTTPStatus.BAD_REQUEST, {"error": "base_updated must be text"}
        fields.update(target=str(item["path"]), base_hash=supplied_hash, base_updated=supplied_updated)
    origin = body.get("origin")
    if origin is not None:
        if not isinstance(origin, str) or len(origin) > 512:
            return HTTPStatus.BAD_REQUEST, {"error": "origin must be a short string"}
        fields["origin"] = origin
    if (mode == "new" and expected_revision == 0 and not title.strip()
            and not body["body"].strip() and not str(body.get("instructions") or "").strip()):
        return HTTPStatus.BAD_REQUEST, {"error": "new drafts require a non-empty title or text"}
    try:
        draft = draft_store.save(
            workspace_root(),
            draft_id=draft_id if isinstance(draft_id, str) else None,
            fields=fields,
            body=body["body"],
            expected_revision=expected_revision,
            now=store.now_stamp(),
            expected_content_hash=expected_hash if expected_revision > 0 else None,
        )
    except draft_store.DraftConflict as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (OSError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    return HTTPStatus.OK, {"message": "Draft saved", "saved_draft": draft}


def handle_draft_discard(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    revision = body.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be an integer"}
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    try:
        removed = draft_store.discard(workspace_root(), str(draft_id), revision, expected_hash)
    except draft_store.DraftConflict as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (OSError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    if not removed:
        return HTTPStatus.NOT_FOUND, {"error": "draft not found"}
    payload = {}
    payload["message"] = "Draft discarded"
    return HTTPStatus.OK, payload


def handle_draft_finish(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    revision = body.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be an integer"}
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    return _run_operation(
        lambda ws: operations.op_draft_save(
            ws,
            str(draft_id),
            expected_revision=revision,
            expected_hash=expected_hash,
            sync=False,
        ),
        "Draft applied",
        include_saved_ref=True,
    )


def handle_card_metadata(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    expected_hash = body.get("expected_hash")
    patch = body.get("patch")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if not isinstance(patch, dict) or not patch:
        return HTTPStatus.BAD_REQUEST, {"error": "metadata patch must be a non-empty object"}
    return _run_operation(
        lambda ws: operations.op_card_metadata(
            ws,
            item["ref"],
            patch,
            expected_hash=expected_hash,
            kind=item["kind"],
            match_mode="exact",
            sync=False,
        ),
        "Properties saved",
        include_saved_ref=True,
    )


def handle_move(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    destination = body.get("status")
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can move"}
    open_statuses = [status for status in store.statuses() if status != "done"]
    if destination not in open_statuses:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid destination status"}
    label = store.policy().get("statuses", {}).get(str(destination)) or destination
    return _run_operation(
        lambda ws: operations.op_move(
            ws, item["ref"], str(destination), match_mode="exact", sync=False
        ),
        f"Moved to {label}",
    )


def handle_comment(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return HTTPStatus.BAD_REQUEST, {"error": "comment requires text"}
    return _run_operation(
        lambda ws: operations.op_comment(
            ws, item["ref"], [message], match_mode="exact", sync=False
        ),
        "Comment added",
    )


def handle_time_start(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can start a timer"}
    return _run_operation(
        lambda ws: operations.op_time_start(ws, item["ref"], sync=False),
        "Timer started",
    )


def handle_time_stop(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task":
        return HTTPStatus.BAD_REQUEST, {"error": "this task has no running timer"}
    ends = {key: body.get(key) for key in ("at", "duration") if body.get(key) not in (None, "")}
    if len(ends) > 1:
        return HTTPStatus.BAD_REQUEST, {"error": "give either at or duration, not both"}
    cleaned_ends: dict[str, str] = {}
    for key, value in ends.items():
        if not isinstance(value, str) or not value.strip():
            return HTTPStatus.BAD_REQUEST, {"error": f"{key} must be text"}
        cleaned_ends[key] = value.strip()
    return _run_operation(
        lambda ws: operations.op_time_end(
            ws,
            query=item["ref"],
            action="stop",
            at=cleaned_ends.get("at"),
            duration=cleaned_ends.get("duration"),
            sync=False,
        ),
        "Timer stopped and saved",
    )


def handle_time_cancel(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task":
        return HTTPStatus.BAD_REQUEST, {"error": "this task has no running timer"}
    return _run_operation(
        lambda ws: operations.op_time_end(ws, query=item["ref"], action="cancel", sync=False),
        "Timer discarded; no time recorded",
    )


def handle_done(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can be marked done"}
    comment = body.get("comment")
    comments = [comment] if isinstance(comment, str) and comment.strip() else ()
    return _run_operation(
        lambda ws: operations.op_complete(
            ws, item["ref"], comments=comments, match_mode="exact", sync=False
        ),
        "Marked done",
    )


def handle_flag_update(field: str, message: str):
    def _handler(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
        flag = body.get(field)
        if not isinstance(flag, bool):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be true or false"}
        value = "true" if flag else "false"
        return _run_operation(
            lambda ws: operations.op_edit(
                ws, item["ref"], assignments=[f"{field}={value}"], match_mode="exact", sync=False
            ),
            message,
        )

    return _handler


def handle_priority(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    priority = body.get("priority")
    allowed_priorities = list(store.policy().get("priorities", {}).keys())
    if not isinstance(priority, str) or priority not in allowed_priorities:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid priority"}
    if priority == item.get("priority"):
        return HTTPStatus.BAD_REQUEST, {"error": "priority unchanged"}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws, item["ref"], assignments=[f"priority={priority}"], match_mode="exact", sync=False
        ),
        f"Priority set to {priority}",
    )


def handle_due_set(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    due = body.get("due")
    due_time = body.get("due_time")
    due_kind = body.get("due_kind")
    try:
        parsed = date.fromisoformat(due) if isinstance(due, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != due:
        return HTTPStatus.BAD_REQUEST, {"error": "due must be a YYYY-MM-DD date"}
    if due_time:
        try:
            parsed_time = time.fromisoformat(due_time) if isinstance(due_time, str) else None
        except ValueError:
            parsed_time = None
        if parsed_time is None:
            return HTTPStatus.BAD_REQUEST, {"error": "due_time must be an HH:MM time"}
        value = f"{due}T{parsed_time.strftime('%H:%M:%S')}"
        message = f"Deadline set to {due} {parsed_time.strftime('%H:%M')}"
    else:
        value = due
        message = f"Deadline set to {due}"
    allowed_kinds = list(store.policy().get("due_kinds", {}).keys())
    if due_kind not in allowed_kinds:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid due kind"}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            assignments=[f"due={value}", f"due_kind={due_kind}"],
            match_mode="exact",
            sync=False,
        ),
        message,
    )


def handle_due_clear(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if not item.get("due"):
        return HTTPStatus.BAD_REQUEST, {"error": "item has no due date"}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            assignments=["due=null", "due_kind=null"],
            match_mode="exact",
            sync=False,
        ),
        "Deadline removed",
    )


DRAFT_ACTION_HANDLERS = {
    "/api/draft-save": handle_draft_save,
    "/api/draft-discard": handle_draft_discard,
    "/api/draft-finish": handle_draft_finish,
}

ITEM_ACTION_HANDLERS = {
    "/api/card-metadata": handle_card_metadata,
    "/api/checkbox": handle_checkbox,
    "/api/review": handle_review,
    "/api/move": handle_move,
    "/api/comment": handle_comment,
    "/api/time-start": handle_time_start,
    "/api/time-stop": handle_time_stop,
    "/api/time-cancel": handle_time_cancel,
    "/api/done": handle_done,
    "/api/archive": handle_flag_update("archived", "Archive updated"),
    "/api/hidden": handle_flag_update("hidden", "Visibility updated"),
    "/api/priority": handle_priority,
    "/api/due-set": handle_due_set,
    "/api/due-clear": handle_due_clear,
}


def handle_api_action(path: str, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_handler = DRAFT_ACTION_HANDLERS.get(path)
    if draft_handler is not None:
        return draft_handler(body, token)

    item_handler = ITEM_ACTION_HANDLERS.get(path)
    if item_handler is None:
        return HTTPStatus.NOT_FOUND, {"error": "not found"}

    item = item_by_ref(body.get("ref"))
    if item is None:
        return HTTPStatus.BAD_REQUEST, {"error": "unknown item ref"}
    return item_handler(item, body, token)


def board_filters(args: argparse.Namespace) -> dict:
    """The board filters given on the command line (--project, --topic, ...), in canonical form."""
    query = card_query.query_from_args(args)
    card_query.validate(query, statuses=store.statuses(), priorities=list(store.policy().get("priorities", {})))
    return card_query.query_to_mapping(query)


def dashboard_url(host: str, port: int, filters: dict | None = None) -> str:
    """Return the dashboard address, opening on the given board filters."""
    query = urlencode({key: "true" if value is True else value for key, value in (filters or {}).items()},
                      doseq=True)
    return f"http://{host}:{port}/" + (f"?{query}" if query else "")


def serve_dashboard(host: str, port: int, *, no_open: bool, filters: dict | None = None) -> int:
    token = secrets.token_urlsafe(24)
    try:
        server = TaskWebServer((host, port), token)
    except OSError as error:
        print(f"error: failed to start web server: {error}", file=sys.stderr)
        return 1
    actual_host, actual_port = server.server_address
    url = dashboard_url(actual_host, actual_port, filters)
    if not no_open:
        cli.open_in_browser(url)
    print(url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        # Let a sync that already started finish publishing the last change.
        server.sync.wait(timeout=60)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open the local web dashboard.")
    cli.add_web_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return serve_dashboard(args.host, args.port, no_open=args.no_open, filters=board_filters(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
