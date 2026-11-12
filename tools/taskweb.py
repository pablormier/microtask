#!/usr/bin/env python3
"""Serve the local web dashboard for the task workspace."""
from __future__ import annotations

import argparse
from datetime import date, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import uuid
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tasklib  # noqa: E402
import drafts as draft_store  # noqa: E402

try:
    from .web_queries import collection_results, normalize_query
except ImportError:  # Direct script entry point or file-based test loader.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from web_queries import collection_results, normalize_query


PICO_CDN = "https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css"
ALPINE_CDN = "https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js"
MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js"
DOMPURIFY_CDN = "https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js"
KATEX_CSS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css"
KATEX_JS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.js"
KATEX_AUTO_RENDER_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/contrib/auto-render.min.js"

WEB_DIR = Path(__file__).resolve().parent / "web"


def load_css() -> str:
    css_files = [
        "theme.css",
        "components.css",
        "layout.css",
        "board.css",
        "views.css",
        "detail.css",
    ]
    return "\n".join((WEB_DIR / "styles" / f).read_text(encoding="utf-8") for f in css_files)


def load_js(web_dir: Path = WEB_DIR) -> str:
    js_files = [
        "markdown.js",
        "card_helpers.js",
        "draft_store.js",
        "capture_composer.js",
        "property_editor.js",
        "content_editor.js",
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


def task_to_dict(task: tasklib.Card, reference_index: tasklib.ReferenceIndex | None = None, *, detail: bool = True, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else tasklib.policy()
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
        "due_kind": tasklib.due_kind_value(fm),
        "created": fm["created"],
        "updated": fm["updated"],
        "completed": fm.get("completed", ""),
        "claimed_by": fm.get("claimed_by"),
        "claimed_at": fm.get("claimed_at"),
        "archived": tasklib.is_archived(task),
        "hidden": tasklib.is_hidden(task),
        "tags": list(tags),
        "title": task.title,
        "next_action": task.next_action,
        "needs_restart": tasklib.next_action_needs_restart_step(task),
        "display_rank": tasklib.task_display_sort_key(task, rank),
        "excerpt": extract_excerpt(body),
        "content_hash": hashlib.sha256(task.raw.encode("utf-8")).hexdigest(),
        "needs_review": fm.get("needs_review") is True,
        "time_seconds": tasklib.saved_time_seconds(task) if task.kind == "task" else 0,
        "comment_count": len(comments) if isinstance(comments, list) else 0,
        "path": str(task.path.relative_to(tasklib.REPO_ROOT)) if task.path.is_relative_to(tasklib.REPO_ROOT) else str(task.path),
        "base_uri": base_uri,
    }
    if detail:
        references = tasklib.item_references(task, reference_index) if reference_index else []
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
    tasklib.pin_demo_state()
    tasklib.maybe_seed_demo()
    items = tasklib.discover()
    item = next((item for item in items if str(item.frontmatter.get("id") or item.path.stem) == identity), None)
    return task_to_dict(item, tasklib.ReferenceIndex(items)) if item else None



def dashboard_meta(*, api_token: str = "", items: list[tasklib.Card] | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else tasklib.policy()
    default_status, default_priority, default_type = tasklib.task_defaults(cfg)
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
    raw_done_limit = rules.get("list_done_limit") if isinstance(rules, dict) else None
    done_limit = int(str(raw_done_limit)) if raw_done_limit is not None and str(raw_done_limit).isdigit() and int(str(raw_done_limit)) > 0 else 10
    timers = tasklib.read_time_timers()
    timers = {
        task_id: {**timer, "saved": tasklib.timer_session_saved(task_id, timer)}
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
    marker = tasklib.view_marker()
    return {
        "workspace_key": hashlib.sha256(str(tasklib.REPO_ROOT.resolve()).encode("utf-8")).hexdigest()[:16],
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
        "topic_tree": tasklib.card_format.topic_tree(usage["topic"], tasklib.suggested_topics(cfg)),
        "workspace_tags": sorted(workspace_tags),
        "workspace_usage": usage,
        "max_doing_tasks": max_doing,
        "done_limit": done_limit,
        "show_year_progress": bool(rules.get("show_year_progress", True)),
        "show_deadline_progress": bool(rules.get("show_deadline_progress", True)),
        "deadline_progress_start": str(rules.get("deadline_progress_start") or "created"),
        "api_token": api_token,
        "demo": tasklib.demo_active(),
        "view": ({"name": marker["name"], "source": marker["source"]} if marker else None),
        "time_running": timers,
        "timer_warn_hours": float(rules.get("timer_warn_hours", 8)),
    }


def dashboard_payload(*, api_token: str = "", query: dict | None = None) -> dict:
    tasklib.pin_demo_state()
    tasklib.maybe_seed_demo()
    cfg = tasklib.policy()
    statuses = list(cfg.get("statuses", {}))
    tasks = tasklib.discover()
    ordered = sorted(
        tasks,
        key=lambda t: statuses.index(t.frontmatter.get("status") or "")
        if (t.frontmatter.get("status") or "") in statuses
        else len(statuses),
    )
    meta = dashboard_meta(api_token=api_token, items=tasks, cfg=cfg)
    meta["focus"] = tasklib.focus_context([
        task for task in tasks if task.kind == "task"
        and not tasklib.is_archived(task) and not tasklib.is_hidden(task)
    ])
    payload = {
        "tasks": [task_to_dict(task, detail=False, cfg=cfg) for task in ordered],
        "statuses": statuses,
        "meta": meta,
    }
    payload["query"] = normalize_query(query)
    # Search full text on the server without including it in collection responses.
    query_items = [{**summary, "body": item.body} for summary, item in zip(payload["tasks"], ordered)]
    payload["collections"] = collection_results(query_items, query)
    payload["meta"]["draft_count"] = len(draft_store.list_drafts(tasklib.REPO_ROOT))
    return payload


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
    pending = ("{ref}", "{config_json}")
    leftover = [m for m in re.findall(r"\{[a-z_]+\}", page) if m not in pending]
    if leftover:
        raise ValueError(f"unreplaced placeholders in dashboard HTML: {leftover}")
    return fill_placeholders(page, {"{config_json}": safe_json(config)})


def bootstrap_config(token: str) -> dict:
    return {
        "api_token": token,
        "workspace_key": hashlib.sha256(str(tasklib.REPO_ROOT.resolve()).encode("utf-8")).hexdigest()[:16],
    }



class TaskWebServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], token: str):
        super().__init__(address, TaskWebHandler)
        self.token = token


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
                payload = dashboard_payload(api_token=self.server.token, query={
                    key: values[-1] for key, values in parse_qs(url.query).items()
                })
            except ValueError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            self.send_json(HTTPStatus.OK, payload)
            return
        if self.path == "/api/drafts":
            try:
                self.send_json(HTTPStatus.OK, {"drafts": draft_store.list_drafts(tasklib.REPO_ROOT)})
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
        status, payload = handle_api_action(self.path, body, self.server.token)
        if status == HTTPStatus.OK:
            query = body.get("query") if isinstance(body.get("query"), dict) else None
            # Actions return receipts; build the queried collection once, after success.
            payload = {**dashboard_payload(api_token=self.server.token, query=query), **payload}
        self.send_json(status, payload)


def item_by_ref(ref: object) -> dict | None:
    if not isinstance(ref, str) or not tasklib.SHORT_REF_RE.fullmatch(ref):
        return None
    tasklib.pin_demo_state()
    tasklib.maybe_seed_demo()
    items = tasklib.items_by_ref(ref)
    item = items[0] if len(items) == 1 and f"@{items[0].short_ref}" == ref else None
    return task_to_dict(item, detail=False) if item else None


def run_mt_command(command: list[str], token: str, message: str) -> tuple[HTTPStatus, dict]:
    result = subprocess.run(
        [str(REPO_ROOT / "mt"), *command],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "command failed").strip()
        return HTTPStatus.BAD_REQUEST, {"error": output}
    payload = {}
    payload["message"] = message
    payload["command_output"] = (result.stdout or "").strip()
    return HTTPStatus.OK, payload


def valid_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def cli_conflict(output: str) -> bool:
    return bool(re.search(
        r"\b(conflict|changed since|content changed|expected updated|stale|baseline|draft not found|draft was removed)\b",
        output, re.IGNORECASE,
    ))


def run_mt_receipt_command(
    command: list[str],
    token: str,
    message: str,
    *,
    input_text: str | None = None,
) -> tuple[HTTPStatus, dict]:
    result = subprocess.run(
        [str(REPO_ROOT / "mt"), *command],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        input=input_text.encode("utf-8") if input_text is not None else None,
    )
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    output = stdout.strip()
    receipt: dict = {}
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            receipt = parsed
    except json.JSONDecodeError:
        pass

    if (result.returncode == 0 and isinstance(receipt.get("ref"), str)
            and isinstance(receipt.get("path"), str)
            and isinstance(receipt.get("content_hash"), str)):
        payload = {}
        payload["message"] = message
        payload["saved_ref"] = receipt.get("ref")
        sync_error = receipt.get("sync_error")
        if isinstance(sync_error, str) and sync_error.strip():
            payload["sync_warning"] = sync_error.strip()
        return HTTPStatus.OK, payload

    error = (stderr or stdout or "command failed").strip()
    if cli_conflict(error):
        return HTTPStatus.CONFLICT, {"error": error, "conflict": True}
    return HTTPStatus.BAD_REQUEST, {"error": error}


def handle_review(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    """Clear needs_review on any card kind, guarded by the loaded content hash."""
    expected_hash = body.get("expected_hash")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    if not item.get("needs_review"):
        return HTTPStatus.OK, {"message": "Already reviewed", "saved_ref": item["ref"]}
    return run_mt_receipt_command(
        ["edit", item["ref"], "--set", "needs_review=false", "--expected-hash", expected_hash, "--json"],
        token, "Marked reviewed",
    )


def handle_draft_save(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    expected_revision = body.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be a non-negative integer"}
    expected_hash = body.get("expected_hash")
    if expected_revision > 0 and (
        not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
    ):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash when updating a draft"}
    if not isinstance(body.get("body"), str) or not valid_utf8(body["body"]):
        return HTTPStatus.BAD_REQUEST, {"error": "body must be valid UTF-8 text"}
    kind = body.get("kind")
    mode = body.get("mode")
    if not isinstance(kind, str) or kind not in {"note", "task"}:
        return HTTPStatus.BAD_REQUEST, {"error": "kind must be note or task"}
    if not isinstance(mode, str) or mode not in {"new", "edit"}:
        return HTTPStatus.BAD_REQUEST, {"error": "mode must be new or edit"}
    fields: dict[str, object] = {"kind": kind, "mode": mode, "title": body.get("title", "")}
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
        if not isinstance(body["instructions"], str) or not valid_utf8(body["instructions"]):
            return HTTPStatus.BAD_REQUEST, {"error": "instructions must be valid UTF-8 text"}
        fields["instructions"] = body["instructions"]
    if mode == "new":
        capture_id = body.get("capture_id")
        created = body.get("created")
        try:
            parsed = uuid.UUID(capture_id) if isinstance(capture_id, str) else None
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != capture_id.lower():
            return HTTPStatus.BAD_REQUEST, {"error": "capture_id must be a canonical UUID"}
        if not isinstance(created, str) or not tasklib.is_timestamp(created):
            return HTTPStatus.BAD_REQUEST, {"error": "created must be a local ISO timestamp with seconds"}
        fields.update(capture_id=capture_id, created=created)
    else:
        if expected_revision == 0:
            target_ref = body.get("target_ref")
            item = item_by_ref(target_ref)
            if item is None or item["kind"] != kind:
                return HTTPStatus.BAD_REQUEST, {"error": "edit draft target is unavailable"}
            item_path = tasklib.REPO_ROOT / str(item["path"])
            if item_path.is_symlink() or not item_path.is_file():
                return HTTPStatus.BAD_REQUEST, {"error": "edit draft target is unavailable"}
            supplied_hash = body.get("base_hash")
            if supplied_hash is None:
                supplied_hash = draft_store.UNVERIFIED_BASE_HASH
            if not isinstance(supplied_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
                return HTTPStatus.BAD_REQUEST, {"error": "base_hash must be a SHA-256 hash"}
            supplied_updated = body.get("base_updated", "")
            if not isinstance(supplied_updated, str):
                return HTTPStatus.BAD_REQUEST, {"error": "base_updated must be text"}
            fields.update(
                target=str(item["path"]),
                base_hash=supplied_hash,
                base_updated=supplied_updated,
            )
    origin = body.get("origin")
    if origin is not None:
        if not isinstance(origin, str) or len(origin) > 512:
            return HTTPStatus.BAD_REQUEST, {"error": "origin must be a short string"}
        fields["origin"] = origin
    if not isinstance(body.get("title", ""), str) or not valid_utf8(body.get("title", "")):
        return HTTPStatus.BAD_REQUEST, {"error": "title must be valid UTF-8 text"}
    if "title" in body and any(character in body["title"] for character in "\r\n"):
        return HTTPStatus.BAD_REQUEST, {"error": "title must be single-line"}
    if (mode == "new" and expected_revision == 0 and not body.get("title", "").strip()
            and not body["body"].strip() and not str(body.get("instructions") or "").strip()):
        return HTTPStatus.BAD_REQUEST, {"error": "new drafts require a non-empty title or text"}
    try:
        draft = draft_store.save(
            tasklib.REPO_ROOT,
            draft_id=draft_id if isinstance(draft_id, str) else None,
            fields=fields,
            body=body["body"],
            expected_revision=expected_revision,
            now=tasklib.now_stamp(),
            expected_content_hash=expected_hash if expected_revision > 0 else None,
        )
    except draft_store.DraftConflict as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (OSError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    payload = {}
    payload["message"] = "Draft saved"
    payload["saved_draft"] = draft
    return HTTPStatus.OK, payload


def handle_draft_discard(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    revision = body.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be an integer"}
    expected_hash = body.get("expected_hash")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    try:
        removed = draft_store.discard(tasklib.REPO_ROOT, str(draft_id), revision, expected_hash)
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
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    result = subprocess.run(
        [str(REPO_ROOT / "mt"), "draft", "save", str(draft_id),
         "--expected-revision", str(revision), "--expected-hash", expected_hash, "--json"],
        cwd=REPO_ROOT, check=False, capture_output=True, text=True,
    )
    try:
        receipt = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        receipt = {}
    if (result.returncode != 0 or not isinstance(receipt.get("ref"), str)
            or not isinstance(receipt.get("path"), str)
            or not isinstance(receipt.get("content_hash"), str)):
        output = (result.stderr or result.stdout or "could not apply draft").strip()
        status = HTTPStatus.CONFLICT if cli_conflict(output) else HTTPStatus.BAD_REQUEST
        return status, {"error": output, **({"conflict": True} if status == HTTPStatus.CONFLICT else {})}
    payload = {}
    payload["message"] = "Draft applied"
    payload["saved_ref"] = receipt.get("ref")
    if isinstance(receipt.get("sync_error"), str):
        payload["sync_warning"] = receipt["sync_error"]
    return HTTPStatus.OK, payload


def handle_api_action(path: str, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if path == "/api/draft-save":
        return handle_draft_save(body, token)
    if path == "/api/draft-discard":
        return handle_draft_discard(body, token)
    if path == "/api/draft-finish":
        return handle_draft_finish(body, token)

    item = item_by_ref(body.get("ref"))
    if item is None:
        return HTTPStatus.BAD_REQUEST, {"error": "unknown item ref"}

    if path == "/api/card-metadata":
        expected_hash = body.get("expected_hash")
        patch = body.get("patch")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
        if not isinstance(patch, dict) or not patch:
            return HTTPStatus.BAD_REQUEST, {"error": "metadata patch must be a non-empty object"}
        return run_mt_receipt_command(
            ["card-metadata", item["ref"], "--kind", item["kind"], "--expected-hash", expected_hash,
             "--patch", json.dumps(patch, ensure_ascii=False), "--json"],
            token, "Properties saved",
        )

    if path == "/api/review":
        return handle_review(item, body, token)

    if path == "/api/move":
        destination = body.get("status")
        if item["kind"] != "task" or item["status"] == "done":
            return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can move"}
        open_statuses = [status for status in tasklib.statuses() if status != "done"]
        if destination not in open_statuses:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid destination status"}
        label = tasklib.policy().get("statuses", {}).get(str(destination)) or destination
        return run_mt_command(["move", item["ref"], str(destination)], token, f"Moved to {label}")

    if path == "/api/comment":
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return HTTPStatus.BAD_REQUEST, {"error": "comment requires text"}
        return run_mt_command(["comment", item["ref"], "-m", message], token, "Comment added")

    if path == "/api/time-start":
        if item["kind"] != "task" or item["status"] == "done":
            return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can start a timer"}
        return run_mt_command(["time", "start", item["ref"]], token, "Timer started")

    if path == "/api/time-stop":
        if item["kind"] != "task":
            return HTTPStatus.BAD_REQUEST, {"error": "this task has no running timer"}
        command = ["time", "stop", item["ref"]]
        ends = {key: body.get(key) for key in ("at", "duration") if body.get(key) not in (None, "")}
        if len(ends) > 1:
            return HTTPStatus.BAD_REQUEST, {"error": "give either at or duration, not both"}
        for key, value in ends.items():
            if not isinstance(value, str) or not value.strip():
                return HTTPStatus.BAD_REQUEST, {"error": f"{key} must be text"}
            command.append(f"--{key}={value.strip()}")
        return run_mt_command(command, token, "Timer stopped and saved")

    if path == "/api/time-cancel":
        if item["kind"] != "task":
            return HTTPStatus.BAD_REQUEST, {"error": "this task has no running timer"}
        return run_mt_command(["time", "cancel", item["ref"]], token, "Timer discarded; no time recorded")

    if path == "/api/done":
        if item["kind"] != "task" or item["status"] == "done":
            return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can be marked done"}
        command = ["done", item["ref"]]
        comment = body.get("comment")
        if isinstance(comment, str) and comment.strip():
            command.extend(["--comment", comment])
        return run_mt_command(command, token, "Marked done")

    if path in {"/api/archive", "/api/hidden"}:
        field = "archived" if path == "/api/archive" else "hidden"
        flag = body.get(field)
        if not isinstance(flag, bool):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be true or false"}
        value = "true" if flag else "false"
        message = "Archive updated" if field == "archived" else "Visibility updated"
        return run_mt_command(["edit", item["ref"], "--set", f"{field}={value}"], token, message)

    if path == "/api/priority":
        priority = body.get("priority")
        allowed_priorities = list(tasklib.policy().get("priorities", {}).keys())
        if not isinstance(priority, str) or priority not in allowed_priorities:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid priority"}
        if priority == item.get("priority"):
            return HTTPStatus.BAD_REQUEST, {"error": "priority unchanged"}
        return run_mt_command(["edit", item["ref"], "--set", f"priority={priority}"], token, f"Priority set to {priority}")

    if path == "/api/due-set":
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
        allowed_kinds = list(tasklib.policy().get("due_kinds", {}).keys())
        if due_kind not in allowed_kinds:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid due kind"}
        return run_mt_command(
            ["edit", item["ref"], "--set", f"due={value}", "--set", f"due_kind={due_kind}"],
            token,
            message,
        )

    if path == "/api/due-clear":
        if not item.get("due"):
            return HTTPStatus.BAD_REQUEST, {"error": "item has no due date"}
        return run_mt_command(["edit", item["ref"], "--set", "due=null", "--set", "due_kind=null"], token, "Deadline removed")

    return HTTPStatus.NOT_FOUND, {"error": "not found"}


def serve_dashboard(host: str, port: int, *, no_open: bool) -> int:
    token = secrets.token_urlsafe(24)
    try:
        server = TaskWebServer((host, port), token)
    except OSError as error:
        print(f"error: failed to start web server: {error}", file=sys.stderr)
        return 1
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    if not no_open:
        tasklib.open_in_browser(url)
    print(url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open the local web dashboard.")
    tasklib.add_web_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return serve_dashboard(args.host, args.port, no_open=args.no_open)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
