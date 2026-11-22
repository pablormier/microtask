#!/usr/bin/env python3
"""Serve the local web dashboard for the task workspace."""
from __future__ import annotations

import argparse
import contextlib
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

import attachments  # noqa: E402
import card_format  # noqa: E402, F401
import card_query  # noqa: E402
import cli_helpers  # noqa: E402
import cli_parser  # noqa: E402
import drafts as draft_store  # noqa: E402
import history  # noqa: E402
import operations  # noqa: E402, F401
import store  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace, missing_workspace_error  # noqa: E402

try:
    from . import web_hosted
    from .web_actions import (
        BackgroundSync,
        SNAPSHOT_BEFORE,
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
    import web_hosted  # noqa: F401
    from web_actions import (  # noqa: F401
        BackgroundSync,
        SNAPSHOT_BEFORE,
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
    def __init__(self, address: tuple[str, int], token: str, workspace: Workspace, allow_hosts: Iterable[str] = (),
                 *, demo: bool = False, hosted: web_hosted.HostedConfig | None = None):
        super().__init__(address, TaskWebHandler)
        # Set when one dashboard serves a shared workspace: every request must then name a signed-in person.
        self.hosted = hosted
        self.allow_hosts = {name.lower() for name in allow_hosts}  # NAME or NAME:PORT, from --allow-host.
        self.requested_host = address[0]  # As given on the command line, which may be a name.
        self.token = token
        self.workspace = workspace
        self.demo = bool(demo)
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

    def authenticate(self) -> tuple[bool, web_hosted.Identity | None]:
        """Who is asking: (True, None) for a single-user dashboard, (True, person) in a shared one.

        A shared dashboard answers 401 or 403 and returns (False, None) when the request carries
        no valid proxy secret or no single signed-in identity, for reads as well as writes.
        """
        hosted = getattr(self.server, "hosted", None)
        if hosted is None:
            return True, None
        try:
            return True, hosted.identify(self.headers)
        except web_hosted.HostedAuthError as error:
            self.send_json(HTTPStatus(error.status), {"error": error.message})
            return False, None

    def api_token(self, actor: web_hosted.Identity | None) -> str:
        """The write token for this request: the process token, or in a shared workspace the signed-in person's."""
        if actor is None:
            return self.server.token
        return web_hosted.member_token(self.server.token, actor.user)

    def hosted_meta(self, actor: web_hosted.Identity | None) -> dict | None:
        if actor is None:
            return None
        meta: dict = {"user": actor.user, "email": actor.email, "admin": actor.admin}
        hosted = getattr(self.server, "hosted", None)
        if hosted is not None and hosted.sign_out_url:
            meta["sign_out_url"] = hosted.sign_out_url
        return meta

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

    def send_attachment_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        status = HTTPStatus.NOT_MODIFIED if self.headers.get("If-None-Match") == etag else HTTPStatus.OK
        self.send_response(status.value)
        self.send_header("Content-Type", attachments.guess_content_type(path))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", attachments.attachment_csp(path))
        if attachments.should_force_download(path):
            self.send_header("Content-Disposition", attachments.content_disposition_attachment(path.name))
        if status is HTTPStatus.NOT_MODIFIED:
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_scope(self, ws: Workspace, actor: web_hosted.Identity | None):
        """One hold of the mutation lock around a write and its history commit.

        In a shared workspace no other dashboard write can land between the action and its commit.
        The commit stages every pending workspace path, so it may also hold changes that were
        already waiting, such as shell edits or draft autosaves. Nothing inside waits on the
        network: the commit is local, and the background sync runs after the lock is released.
        """
        return transaction.time_mutation_lock(ws) if actor is not None else contextlib.nullcontext()

    def handle_attachment_upload(self, actor: web_hosted.Identity | None = None) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        if length <= 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "attachment must not be empty"})
            return
        ws = self.server.workspace.for_operation()
        policy = store.policy(ws)
        max_bytes = attachments.max_attachment_bytes(policy)
        if length > max_bytes:
            limit_mb = policy.get("rules", {}).get("max_attachment_mb", attachments.DEFAULT_MAX_ATTACHMENT_MB)
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"attachment exceeds max_attachment_mb ({limit_mb} MB)"},
            )
            return
        raw_bytes = self.rfile.read(length)
        if len(raw_bytes) != length:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete attachment upload"})
            return
        raw_filename = self.headers.get("X-Filename", "")
        filename = unquote(raw_filename) if raw_filename else ""
        content_type = self.headers.get("Content-Type")
        history_warning = ""
        try:
            with self.write_scope(ws, actor):
                req_actor = actor.user if actor is not None else store.current_username(ws=ws)
                with history.capture_context(action="upload", actor=req_actor, source="web") as ctx:
                    result = operations.op_store_attachment(
                        ws,
                        raw_bytes,
                        filename=filename,
                        content_type=content_type,
                        by=actor.user if actor else None,
                        sync=False,
                    )
                history_warning = result.history_warning or ctx.warning
        except store.InputError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except store.TaskError as error:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        except OSError as error:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"could not store attachment: {error}"})
            return
        if result.changed:
            self.server.sync.request(ws)
        reply = {"attachment": result.receipt}
        if history_warning:
            reply["history_warning"] = history_warning
        self.send_json(HTTPStatus.OK, reply)

    def do_GET(self) -> None:
        if self.reject_host():
            return
        allowed, actor = self.authenticate()
        if not allowed:
            return
        url = urlsplit(self.path)
        if url.path.startswith("/vendor/"):
            found = vendor_file(unquote(url.path))
            if found is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            else:
                self.send_vendor_file(*found)
            return
        if url.path.startswith("/attachments/"):
            rel = unquote(url.path[len("/attachments/"):])
            ws = self.server.workspace.for_operation()
            found = attachments.resolve_attachment_path(ws.attachments_root(), rel)
            if found is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            else:
                self.send_attachment_file(found)
            return
        if url.path in ("/", "/index.html"):
            page, nonce = build_html_with_nonce(bootstrap_config(self.server.workspace, self.api_token(actor),
                                                                         actor.user if actor else None,
                                                                         hosted=self.hosted_meta(actor),
                                                                         demo=self.server.demo))
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
                payload = dashboard_payload(ws, self.server.cache, api_token=self.api_token(actor), include=include,
                                            query=request_query(ws, params), demo=self.server.demo,
                                            actor=actor.user if actor else None, hosted=self.hosted_meta(actor))
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
                self.send_json(HTTPStatus.OK, {"drafts": draft_list_payload(self.server.workspace.root,
                                                                             actor.user if actor else None)})
            except (OSError, ValueError) as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.reject_host():
            return
        allowed, actor = self.authenticate()
        if not allowed:
            return
        sent_token = (self.headers.get("X-Microtask-Token") or "").encode("utf-8")
        if not secrets.compare_digest(sent_token, self.api_token(actor).encode("utf-8")):
            if actor is not None:
                # Before anything is read or changed: the page was loaded for another account, or by an
                # older process. The client keeps its unsaved work locally and reloads.
                self.send_json(HTTPStatus.CONFLICT, {"error": "The signed-in account changed. Reload the page.",
                                                     "member_changed": True})
            else:
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
            return
        if urlsplit(self.path).path == "/api/attachments":
            self.handle_attachment_upload(actor)
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
        history_warning = ""
        with self.write_scope(ws, actor):
            req_actor = actor.user if actor is not None else store.current_username(ws=ws)
            require_snapshot = actor is not None and actor.admin and path in SNAPSHOT_BEFORE
            try:
                with history.capture_context(
                    action=path[len("/api/"):],
                    actor=req_actor,
                    source="web",
                    require=require_snapshot,
                ) as ctx:
                    status, payload = handle_api_action(ws, path, body, actor)
                    history_warning = ctx.warning
            except store.HistoryRequiredError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": f"Nothing was changed: the card could not be saved to local history first. {error}",
                })
                return
            wrote = path not in UNSYNCED_ACTIONS and (status == HTTPStatus.OK or payload.get("sync_needed"))
        if wrote:
            self.server.sync.request(ws)
        if status == HTTPStatus.OK:
            # Actions return receipts; build the queried collection once, after success.
            payload = {**dashboard_payload(self.server.workspace.for_operation(), self.server.cache,
                                           api_token=self.api_token(actor), query=query, include=body.get("include"),
                                           demo=self.server.demo, actor=actor.user if actor else None,
                                           hosted=self.hosted_meta(actor)),
                       "sync_warning": " ".join(part for part in (self.server.sync.current_warning(), history_warning) if part),
                       "restart_notice": self.server.restart_notice(),
                       **payload}
        elif history_warning:
            payload = {**payload, "sync_warning": history_warning}
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
                    allow_hosts: Iterable[str] = (), demo: bool = False,
                    hosted: web_hosted.HostedConfig | None = None) -> int:
    # A shared workspace may run several processes behind one proxy, so they all derive the same token.
    token = (web_hosted.shared_process_token(hosted.secret, application_version(APP_ROOT)) if hosted is not None
             else secrets.token_urlsafe(24))
    try:
        server = TaskWebServer((host, port), token, workspace, allow_hosts, demo=demo, hosted=hosted)
    except OSError as error:
        print(f"error: failed to start web server: {error}", file=sys.stderr)
        return 1
    actual_host, actual_port = server.server_address
    url = dashboard_url(actual_host, actual_port, filters)
    if not no_open:
        cli_helpers.open_in_browser(url)
    print(url, flush=True)
    if hosted is not None:
        print(f"Shared workspace: signed-in users come from the {web_hosted.EMAIL_HEADER} header; "
              f"{len(hosted.admins)} administrator(s).", flush=True)
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
    parser.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def hosted_config(args: argparse.Namespace, environ) -> web_hosted.HostedConfig | None:
    """The shared-workspace settings given by flags or MT_* variables, or None for a single-user dashboard."""
    if not args.trust_proxy_user:
        return None
    secret = web_hosted.read_secret(args.proxy_secret_file or environ.get("MT_PROXY_SECRET_FILE"),
                                    environ.get("MT_PROXY_SECRET"))
    admins = web_hosted.parse_admins(*args.admin, environ.get("MT_ADMINS"))
    sign_out_url = web_hosted.read_sign_out_url(args.sign_out_url or environ.get("MT_SIGN_OUT_URL"))
    return web_hosted.HostedConfig(secret, admins, web_hosted.read_user_map(args.user_map or environ.get("MT_USER_MAP")),
                                   sign_out_url=sign_out_url)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    # The process edge: `mt web` starts this script with the selected workspace in its environment.
    workspace = Workspace.resolve()
    missing = missing_workspace_error(workspace.environ, app_root=workspace.app_root)
    if missing:
        print(f"error: {missing}", file=sys.stderr)
        return 2
    try:
        hosted = hosted_config(args, workspace.environ)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return serve_dashboard(workspace, args.host, args.port, no_open=args.no_open,
                           filters=board_filters(workspace, args), allow_hosts=args.allow_host,
                           demo=bool(args.demo), hosted=hosted)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
