#!/usr/bin/env python3
"""Serve the local web dashboard for the task workspace."""
from __future__ import annotations

import argparse
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from collections.abc import Iterable
from pathlib import Path
import secrets
import signal
import sys
import threading
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "scripts"))

import card_format  # noqa: E402, F401
import card_query  # noqa: E402
import cli_helpers  # noqa: E402
import cli_parser  # noqa: E402
import drafts as draft_store  # noqa: E402
import operations  # noqa: E402, F401
import store  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace, missing_workspace_error  # noqa: E402

try:
    from .web_actions import (
        BackgroundSync,
        UNSYNCED_ACTIONS,
        handle_api_action,
        handle_draft_save,
    )
    from .web_assets import (
        WEB_DIR,
        application_version,
        build_html,
        build_html_with_nonce,
        safe_json,
        vendor_file,
    )
    from .web_payloads import (
        bootstrap_config,
        dashboard_detail,
        dashboard_payload,
        draft_list_payload,
        request_query,
        search_cards,
        task_to_dict,
    )
except ImportError:  # Direct script entry point or file-based test loader.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from web_actions import (  # noqa: F401
        BackgroundSync,
        UNSYNCED_ACTIONS,
        handle_api_action,
        handle_draft_save,
    )
    from web_assets import (  # noqa: F401
        WEB_DIR,
        application_version,
        build_html,
        build_html_with_nonce,
        safe_json,
        vendor_file,
    )
    from web_payloads import (  # noqa: F401
        bootstrap_config,
        dashboard_detail,
        dashboard_payload,
        draft_list_payload,
        request_query,
        search_cards,
        task_to_dict,
    )


class TaskWebServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], token: str, workspace: Workspace, allow_hosts: Iterable[str] = ()):
        super().__init__(address, TaskWebHandler)
        self.allow_hosts = {name.lower() for name in allow_hosts}  # NAME or NAME:PORT, from --allow-host.
        self.requested_host = address[0]  # As given on the command line, which may be a name.
        self.token = token
        self.workspace = workspace
        # Parsed cards reused across requests; every read re-checks each file's stat signature.
        self.cache = store.CardCache()
        self.sync = BackgroundSync()
        # A failure from before this process started stays visible until a sync succeeds.
        self.sync.warning = transaction.read_sync_error(workspace)[1]
        self.app_version = application_version(APP_ROOT)
        self._cached_restart_notice = ""

    def restart_notice(self) -> str:
        if self._cached_restart_notice:
            return self._cached_restart_notice
        if application_version(APP_ROOT) != self.app_version:
            self._cached_restart_notice = "Application updated on disk; restart the dashboard to use the new version."
        return self._cached_restart_notice


MAX_BODY_BYTES = 10 * 1024 * 1024


def split_host_header(value: str) -> tuple[str, int | None]:
    """Split a Host header into a lowercase host (IPv6 keeps its brackets) and a port."""
    value = value.strip().lower()
    if value.startswith("["):
        host, _, rest = value.partition("]")
        host, port = host + "]", rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = value.partition(":")
    if not port:
        return host, None
    return host, int(port) if port.isdigit() else -1


class TaskWebHandler(BaseHTTPRequestHandler):
    server: TaskWebServer

    def host_allowed(self) -> bool:
        """Accept only the addresses this server is meant to be reached at, with its port.

        A page on another site can resolve its own name to this machine (DNS rebinding) and
        then read the dashboard, token included. Browsers always send the name they used, so
        a Host that is not ours is refused. Loopback names, the bound address and the names given
        with --allow-host (any port, unless the option names one) are allowed.
        A server bound to all interfaces (0.0.0.0 or ::) has no single address to compare, so
        it accepts any IP literal, such as a LAN or Tailscale address, but never a DNS name.
        """
        host, port = split_host_header(self.headers.get("Host", ""))
        for entry in self.server.allow_hosts:
            name, allowed_port = split_host_header(entry)
            if host == name and allowed_port in (None, port):
                return True
        if (80 if port is None else port) != self.server.server_address[1]:
            return False
        bound = {name.lower() for name in (self.server.requested_host, self.server.server_address[0])}
        bound_hosts = {f"[{name}]" if ":" in name else name for name in bound}
        if host in {"localhost", "127.0.0.1", "[::1]"} or host in bound_hosts:
            return True
        if bound & {"", "0.0.0.0", "::"}:
            try:
                ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                return False
            return True
        return False

    def reject_host(self) -> bool:
        if self.host_allowed():
            return False
        self.send_json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "unrecognized Host header"})
        return True

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

    def send_html(self, page: str, status: HTTPStatus = HTTPStatus.OK, *, nonce: str = "") -> None:
        body = page.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if nonce:
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'self'; script-src 'self' 'nonce-{nonce}' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
            )
        self.end_headers()
        self.wfile.write(body)

    def send_vendor_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        # Cached copies are revalidated on each load, so a changed file is never served stale.
        status = HTTPStatus.NOT_MODIFIED if self.headers.get("If-None-Match") == etag else HTTPStatus.OK
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status is HTTPStatus.NOT_MODIFIED:
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.reject_host():
            return
        url = urlsplit(self.path)
        if url.path.startswith("/vendor/"):
            found = vendor_file(unquote(url.path))
            if found is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            else:
                self.send_vendor_file(*found)
            return
        if url.path in ("/", "/index.html"):
            page, nonce = build_html_with_nonce(bootstrap_config(self.server.workspace, self.server.token))
            self.send_html(page, nonce=nonce)
            return
        if url.path == "/api/item":
            values = parse_qs(url.query).get("id", [])
            if len(values) != 1 or not values[0]:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "one card id is required"})
                return
            try:
                item = dashboard_detail(self.server.workspace.for_operation(), self.server.cache, values[0])
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
                ws = self.server.workspace.for_operation()
                payload = dashboard_payload(ws, self.server.cache, api_token=self.server.token, include=include,
                                            query=request_query(ws, params))
                payload["sync_warning"] = self.server.sync.current_warning()
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
        if url.path == "/api/search":
            params = parse_qs(url.query)
            q = (params.get("q") or [""])[0]
            raw_limit = (params.get("limit") or ["12"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
                return
            try:
                self.send_json(HTTPStatus.OK, search_cards(self.server.workspace.for_operation(), self.server.cache, q, limit=limit))
            except ValueError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        if url.path == "/api/drafts":
            try:
                self.send_json(HTTPStatus.OK, {"drafts": draft_list_payload(self.server.workspace.root)})
            except (OSError, ValueError) as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.reject_host():
            return
        if not secrets.compare_digest(self.headers.get("X-Microtask-Token") or "", self.server.token):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        if length < 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        if length > MAX_BODY_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body too large"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        if not isinstance(body, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "request body must be an object"})
            return
        path = urlsplit(self.path).path
        ws = self.server.workspace.for_operation()
        try:
            query = request_query(ws, body.get("query"))
        except card_query.QueryError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        status, payload = handle_api_action(ws, path, body)
        if path not in UNSYNCED_ACTIONS and (status == HTTPStatus.OK or payload.get("sync_needed")):
            self.server.sync.request(ws)
        if status == HTTPStatus.OK:
            # Actions return receipts; build the queried collection once, after success.
            # A fresh handle: the write may have ended demo mode.
            payload = {**dashboard_payload(self.server.workspace.for_operation(), self.server.cache,
                                           api_token=self.server.token, query=query, include=body.get("include")),
                       "sync_warning": self.server.sync.current_warning(),
                       "restart_notice": self.server.restart_notice(),
                       **payload}
        self.send_json(status, payload)


def board_filters(ws: Workspace, args: argparse.Namespace) -> dict:
    """The board filters given on the command line (--project, --topic, ...), in canonical form."""
    query = card_query.query_from_args(args, env=ws.environ)
    card_query.validate(query, statuses=store.statuses(ws), priorities=list(store.policy(ws).get("priorities", {})))
    return card_query.query_to_mapping(query)


def dashboard_url(host: str, port: int, filters: dict | None = None) -> str:
    """Return the dashboard address, opening on the given board filters."""
    query = urlencode({key: "true" if value is True else value for key, value in (filters or {}).items()},
                      doseq=True)
    return f"http://{host}:{port}/" + (f"?{query}" if query else "")


def serve_dashboard(workspace: Workspace, host: str, port: int, *, no_open: bool, filters: dict | None = None,
                    allow_hosts: Iterable[str] = ()) -> int:
    token = secrets.token_urlsafe(24)
    try:
        server = TaskWebServer((host, port), token, workspace, allow_hosts)
    except OSError as error:
        print(f"error: failed to start web server: {error}", file=sys.stderr)
        return 1
    actual_host, actual_port = server.server_address
    url = dashboard_url(actual_host, actual_port, filters)
    if not no_open:
        cli_helpers.open_in_browser(url)
    print(url, flush=True)
    # Process supervisors stop us with SIGTERM; treat it like Ctrl-C so the cleanup below runs.
    previous = None
    if threading.current_thread() is threading.main_thread():
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        server.server_close()
        # Let a sync that already started finish publishing the last change.
        server.sync.wait(timeout=60)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open the local web dashboard.")
    cli_parser.add_web_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    # The process edge: `mt web` starts this script with the selected workspace in its environment.
    workspace = Workspace.resolve()
    missing = missing_workspace_error(workspace.environ, app_root=workspace.app_root)
    if missing:
        print(f"error: {missing}", file=sys.stderr)
        return 2
    return serve_dashboard(workspace, args.host, args.port, no_open=args.no_open,
                           filters=board_filters(workspace, args), allow_hosts=args.allow_host)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
