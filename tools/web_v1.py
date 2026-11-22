"""Versioned HTTP API handlers (`/api/v1/*`) returning CLI receipts and records directly."""
from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(APP_ROOT / "scripts"))

import card_query  # noqa: E402
from cli_cards import resolve_needs_review  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
from workspace import Workspace  # noqa: E402

try:
    from . import web_actions
except ImportError:
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    import web_actions

V1_MATCH_MODES = ("exact", "and", "or")
_VALID_SORTS = {"relevance", "updated", "created", "due", "completed", "priority", "title"}
_VALID_FORMATS = {"json", "compact", "outline", "full", "text"}


class _BadInput(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST, exit_code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.exit_code = exit_code


def _require_str(body: dict, key: str, *, non_empty: bool = False) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not web_actions.valid_utf8(value):
        raise _BadInput(f"{key} must be valid UTF-8 text")
    if non_empty and not value.strip():
        raise _BadInput(f"{key} must be a non-empty string")
    return value


def _optional_str(body: dict, key: str, default: str | None = None) -> str | None:
    if key not in body or body[key] is None:
        return default
    value = body[key]
    if not isinstance(value, str) or not web_actions.valid_utf8(value):
        raise _BadInput(f"{key} must be valid UTF-8 text")
    return value


def _optional_str_list(body: dict, key: str, *, default_empty: bool = True) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return [] if default_empty else None
    if not isinstance(value, list) or any(not isinstance(item, str) or not web_actions.valid_utf8(item) for item in value):
        raise _BadInput(f"{key} must be a list of strings")
    return list(value)


def _require_match_mode(body: dict) -> str:
    mode = body.get("match_mode", "exact")
    if mode not in V1_MATCH_MODES:
        raise _BadInput("match_mode must be exact, and, or or")
    return mode


def _error_response(error: store.TaskError) -> tuple[HTTPStatus, dict[str, object]]:
    if isinstance(error, store.HistoryRequiredError):
        raise error
    if isinstance(error, store.RollbackError):
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error), "exit_code": error.exit_code}
    if isinstance(error, store.ConflictError):
        payload: dict[str, object] = {"error": str(error), "conflict": True, "exit_code": error.exit_code}
        if error.sync_needed:
            payload["sync_needed"] = True
        return HTTPStatus.CONFLICT, payload
    return HTTPStatus.BAD_REQUEST, {"error": str(error), "exit_code": error.exit_code}


def _run_v1_operation(ws: Workspace, action) -> tuple[HTTPStatus, dict[str, object]]:
    try:
        result = action(ws)
    except store.TaskError as error:
        return _error_response(error)
    return HTTPStatus.OK, dict(result.receipt)


def _v1_item(ws: Workspace, params: dict[str, list[str]]) -> tuple[HTTPStatus, dict[str, object]]:
    refs = [value for value in params.get("ref", []) if value]
    match_mode = (params.get("match_mode") or ["exact"])[0]
    if match_mode not in V1_MATCH_MODES:
        raise _BadInput("match_mode must be exact, and, or or")
    if len(refs) > 1:
        items: list[dict[str, object]] = []
        for ref in refs:
            if not ref.startswith("@"):
                raise _BadInput("multi-item read requires @ref values")
            try:
                card = store._resolve_unique_card(ws, ref, match_mode="exact")
            except store.TaskError as error:
                status, payload = _error_response(error)
                if "no task or note matched" in str(error):
                    status = HTTPStatus.NOT_FOUND
                return status, payload
            items.append({"item": store.item_read_record(card, ws=ws), "raw": card.raw})
        return HTTPStatus.OK, {"items": items}
    target = (refs[0] if refs else None) or (params.get("query") or [None])[0] or (params.get("id") or [None])[0]
    if not isinstance(target, str) or not target.strip():
        raise _BadInput("a query, ref, or id is required")
    try:
        card = store._resolve_unique_card(ws, target.strip(), match_mode=match_mode)
    except store.TaskError as error:
        status, payload = _error_response(error)
        message = str(error)
        if "no task or note matched" in message:
            status = HTTPStatus.NOT_FOUND
        elif "matched multiple" in message:
            status = HTTPStatus.CONFLICT
        return status, payload
    return HTTPStatus.OK, {"item": store.item_read_record(card, ws=ws), "raw": card.raw}


def _parse_pos_int(raw: str | None, name: str) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise _BadInput(f"{name} must be an integer") from error
    if value < 1:
        raise _BadInput(f"--{name} must be a positive integer")
    return value


def _v1_items(ws: Workspace, params: dict[str, list[str]]) -> tuple[HTTPStatus, dict[str, object]]:
    raw_params = dict(params)
    search = (raw_params.pop("search", ["0"])[0] or "0").lower() in {"1", "true", "yes"}
    sort_field = raw_params.pop("sort", ["relevance" if search else "updated"])[0]
    order = raw_params.pop("order", ["desc"])[0]
    fmt = raw_params.pop("format", ["json"])[0]
    limit = _parse_pos_int(raw_params.pop("limit", [None])[0], "limit")
    tail = _parse_pos_int(raw_params.pop("tail", [None])[0], "tail")
    if sort_field not in _VALID_SORTS:
        raise _BadInput(f"invalid sort field: {sort_field}")
    if order not in {"asc", "desc"}:
        raise _BadInput(f"invalid sort order: {order}")
    if fmt not in _VALID_FORMATS:
        raise _BadInput(f"invalid format: {fmt}")
    if limit is not None and tail is not None:
        raise _BadInput("limit and tail cannot be combined")
    normalized = {
        key: (vals[0] if len(vals) == 1 and key not in card_query.LIST_FIELDS else vals)
        for key, vals in raw_params.items()
    }
    try:
        query = card_query.query_from_mapping(normalized)
        card_query.validate(query, statuses=store.statuses(ws), priorities=list(store.policy(ws).get("priorities", {})))
    except card_query.QueryError as error:
        raise _BadInput(str(error)) from error
    if search and not query.text.strip():
        raise _BadInput("search requires a non-empty query")
    return HTTPStatus.OK, store.query_card_records(
        ws, query, search=search, sort=sort_field, order=order, limit=limit, tail=tail, fmt=fmt,
    )


def handle_v1_get(ws: Workspace, path: str, params: dict[str, list[str]], actor=None) -> tuple[HTTPStatus, dict[str, object]]:
    try:
        if path == "/api/v1/item":
            return _v1_item(ws, params)
        if path == "/api/v1/items":
            return _v1_items(ws, params)
    except _BadInput as error:
        return error.status, {"error": error.message, "exit_code": error.exit_code}
    return HTTPStatus.NOT_FOUND, {"error": "not found", "exit_code": 1}


def _v1_add(ws: Workspace, body: dict, by: str | None) -> tuple[HTTPStatus, dict[str, object]]:
    kind = body.get("kind", "task")
    if kind not in {"task", "note"}:
        raise _BadInput("kind must be task or note")
    text = _optional_str(body, "text", "") or ""
    title = _optional_str(body, "title")
    tags = _optional_str_list(body, "tags")
    is_capture = bool(body.get("capture", False))
    needs_review = resolve_needs_review(kind, is_capture, body.get("needs_review"))
    resolved_title = title if is_capture else (title or operations.capture_title(text))
    next_action = _optional_str(body, "next_action", "") or ""
    instructions = _optional_str(body, "instructions", "") or ""
    status = _optional_str(body, "status") if kind == "task" else None
    task_type = _optional_str(body, "task_type") if kind == "task" else None
    priority = _optional_str(body, "priority")
    project = _optional_str(body, "project")
    topic = _optional_str(body, "topic")
    due = _optional_str(body, "due")
    due_kind = _optional_str(body, "due_kind")
    capture_id = _optional_str(body, "capture_id") if is_capture else None
    created_at = _optional_str(body, "created_at") if is_capture else None
    effective_status = status or (operations.task_defaults(store.policy(ws))[0] if kind == "task" else None)
    return _run_v1_operation(
        ws,
        lambda resolved: operations.op_create(
            resolved,
            kind,
            text,
            title=resolved_title,
            capture_id=capture_id,
            created_at=created_at,
            status=status,
            task_type=task_type,
            next_action=next_action if kind == "task" else "",
            instructions=instructions,
            priority=priority,
            project=project,
            topic=topic,
            due=due,
            due_kind=due_kind,
            tags=tags,
            needs_review=needs_review,
            archived=bool(body.get("archived", False)),
            hidden=bool(body.get("hidden", False)),
            by=by if (kind == "task" and effective_status == "doing") else None,
            sync=False,
        ),
    )


def _v1_move(ws: Workspace, body: dict, by: str | None, actor=None) -> tuple[HTTPStatus, dict[str, object]]:
    query = _require_str(body, "query", non_empty=True)
    destination = _require_str(body, "destination", non_empty=True)
    comments = _optional_str_list(body, "comments") or []
    takeover = bool(body.get("takeover", False))
    if takeover and actor is not None and not actor.admin:
        return HTTPStatus.FORBIDDEN, {"error": web_actions.ADMIN_ONLY_MESSAGE, "exit_code": 1}
    match_mode = _require_match_mode(body)
    return _run_v1_operation(
        ws,
        lambda resolved: operations.op_move(
            resolved, query, destination, comments=comments, takeover=takeover,
            by=by, match_mode=match_mode, sync=False,
        ),
    )


def _v1_edit(ws: Workspace, body: dict, by: str | None) -> tuple[HTTPStatus, dict[str, object]]:
    query = _require_str(body, "query", non_empty=True)
    match_mode = _require_match_mode(body)
    expected_hash = body.get("expected_hash")
    if expected_hash is not None and not web_actions._valid_sha256(expected_hash):
        raise _BadInput("expected_hash must be a 64-character SHA-256 hex digest")
    body_mode = body.get("body_mode")
    if body_mode is not None and body_mode not in {"replace", "append", "replace_pairs", "replace_file"}:
        raise _BadInput("invalid body_mode")
    body_text = _optional_str(body, "body_text", "") or ""
    raw_pairs = body.get("body_replacements")
    body_replacements = None
    if raw_pairs is not None:
        if (
            not isinstance(raw_pairs, list)
            or any(
                not isinstance(pair, list) or len(pair) != 2
                or not isinstance(pair[0], str) or not isinstance(pair[1], str)
                or not web_actions.valid_utf8(pair[0]) or not web_actions.valid_utf8(pair[1])
                for pair in raw_pairs
            )
        ):
            raise _BadInput("body_replacements must be a list of [old, new] UTF-8 string pairs")
        body_replacements = [(pair[0], pair[1]) for pair in raw_pairs]
    exact_replacements_text = _optional_str(body, "exact_replacements_text")
    metadata_fields = body.get("metadata_fields")
    if metadata_fields is not None and not isinstance(metadata_fields, dict):
        raise _BadInput("metadata_fields must be an object")
    assignments = _optional_str_list(body, "assignments") or []
    tag_add = _optional_str_list(body, "tag_add", default_empty=False)
    tag_remove = _optional_str_list(body, "tag_remove", default_empty=False)
    return _run_v1_operation(
        ws,
        lambda resolved: operations.op_edit(
            resolved, query, match_mode=match_mode, expected_hash=expected_hash,
            body_mode=body_mode, body_text=body_text, body_replacements=body_replacements,
            exact_replacements_text=exact_replacements_text, metadata_fields=metadata_fields,
            assignments=assignments, tag_add=tag_add, tag_remove=tag_remove, by=by, sync=False,
        ),
    )


def _v1_comment(ws: Workspace, body: dict, by: str | None) -> tuple[HTTPStatus, dict[str, object]]:
    query = _require_str(body, "query", non_empty=True)
    comments = _optional_str_list(body, "comments")
    if not comments:
        raise _BadInput("comments must be a non-empty list of strings")
    match_mode = _require_match_mode(body)
    return _run_v1_operation(
        ws,
        lambda resolved: operations.op_comment(resolved, query, comments, by=by, match_mode=match_mode, sync=False),
    )


def _v1_done(ws: Workspace, body: dict, by: str | None) -> tuple[HTTPStatus, dict[str, object]]:
    query = _require_str(body, "query", non_empty=True)
    notes = _optional_str_list(body, "notes") or []
    comments = _optional_str_list(body, "comments") or []
    match_mode = _require_match_mode(body)
    return _run_v1_operation(
        ws,
        lambda resolved: operations.op_complete(
            resolved, query, notes=notes, comments=comments, by=by, match_mode=match_mode, sync=False,
        ),
    )


def v1_requires_snapshot(path: str, body: dict) -> bool:
    return path == "/api/v1/move" and bool(body.get("takeover", False))


def handle_v1_post(ws: Workspace, path: str, body: dict, actor=None) -> tuple[HTTPStatus, dict[str, object]]:
    by = actor.user if actor is not None else None
    try:
        if path == "/api/v1/add":
            return _v1_add(ws, body, by)
        if path == "/api/v1/move":
            return _v1_move(ws, body, by, actor=actor)
        if path == "/api/v1/edit":
            return _v1_edit(ws, body, by)
        if path == "/api/v1/comment":
            return _v1_comment(ws, body, by)
        if path == "/api/v1/done":
            return _v1_done(ws, body, by)
    except _BadInput as error:
        return error.status, {"error": error.message, "exit_code": error.exit_code}
    return HTTPStatus.NOT_FOUND, {"error": "not found", "exit_code": 1}
