#!/usr/bin/env python3
"""Serve the local web dashboard for the task workspace."""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "scripts"))

import card_format  # noqa: E402, F401
import card_query  # noqa: E402
import cli  # noqa: E402
import drafts as draft_store  # noqa: E402
import operations  # noqa: E402, F401
import store  # noqa: E402

try:
    from .web_actions import (
        BackgroundSync,
        DRAFT_ACTION_HANDLERS,
        ITEM_ACTION_HANDLERS,
        UNSYNCED_ACTIONS,
        handle_api_action,
        handle_card_metadata,
        handle_checkbox,
        handle_comment,
        handle_done,
        handle_draft_discard,
        handle_draft_finish,
        handle_draft_save,
        handle_due_clear,
        handle_due_set,
        handle_flag_update,
        handle_move,
        handle_priority,
        handle_review,
        handle_time_cancel,
        handle_time_start,
        handle_time_stop,
        item_by_ref,
        sync_once,
        valid_utf8,
    )
    from .web_assets import (
        ALPINE_CDN,
        DOMPURIFY_CDN,
        KATEX_AUTO_RENDER_CDN,
        KATEX_CSS_CDN,
        KATEX_JS_CDN,
        MARKED_CDN,
        PICO_CDN,
        WEB_DIR,
        application_version,
        build_html,
        fill_placeholders,
        load_css,
        load_js,
        load_template,
        safe_json,
    )
    from .web_payloads import (
        CARD_CACHE,
        MAX_INCLUDED_IDS,
        bootstrap_config,
        current_workspace,
        dashboard_detail,
        dashboard_meta,
        dashboard_payload,
        extract_excerpt,
        included_ids,
        request_query,
        task_to_dict,
        workspace_root,
    )
    from .web_queries import collection_results  # noqa: F401
except ImportError:  # Direct script entry point or file-based test loader.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from web_actions import (  # noqa: F401
        BackgroundSync,
        DRAFT_ACTION_HANDLERS,
        ITEM_ACTION_HANDLERS,
        UNSYNCED_ACTIONS,
        handle_api_action,
        handle_card_metadata,
        handle_checkbox,
        handle_comment,
        handle_done,
        handle_draft_discard,
        handle_draft_finish,
        handle_draft_save,
        handle_due_clear,
        handle_due_set,
        handle_flag_update,
        handle_move,
        handle_priority,
        handle_review,
        handle_time_cancel,
        handle_time_start,
        handle_time_stop,
        item_by_ref,
        sync_once,
        valid_utf8,
    )
    from web_assets import (  # noqa: F401
        ALPINE_CDN,
        DOMPURIFY_CDN,
        KATEX_AUTO_RENDER_CDN,
        KATEX_CSS_CDN,
        KATEX_JS_CDN,
        MARKED_CDN,
        PICO_CDN,
        WEB_DIR,
        application_version,
        build_html,
        fill_placeholders,
        load_css,
        load_js,
        load_template,
        safe_json,
    )
    from web_payloads import (  # noqa: F401
        CARD_CACHE,
        MAX_INCLUDED_IDS,
        bootstrap_config,
        current_workspace,
        dashboard_detail,
        dashboard_meta,
        dashboard_payload,
        extract_excerpt,
        included_ids,
        request_query,
        task_to_dict,
        workspace_root,
    )
    from web_queries import collection_results  # noqa: F401


class TaskWebServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], token: str):
        super().__init__(address, TaskWebHandler)
        self.token = token
        self.sync = BackgroundSync()
        self.app_version = application_version(APP_ROOT)
        self._cached_restart_notice = ""

    def restart_notice(self) -> str:
        if self._cached_restart_notice:
            return self._cached_restart_notice
        if application_version(APP_ROOT) != self.app_version:
            self._cached_restart_notice = "Application updated on disk; restart the dashboard to use the new version."
        return self._cached_restart_notice


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
