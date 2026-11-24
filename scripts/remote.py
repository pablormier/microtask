"""Fail-closed HTTP client for operating on a hosted microtask workspace."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import card_query
from cli_admin import emit_settings
from cli_cards import (
    _edit_body_mode_from_args,
    _edit_metadata_from_args,
    create_params_from_args,
    emit_focus_context,
    emit_topic_tree,
    prepare_command_inputs,
)
from cli_helpers import emit_json, emit_query_output, emit_receipt, query_output_format

SUPPORTED_REMOTE_COMMANDS = {
    "add", "note", "list", "search", "read", "move", "edit", "comment", "done",
    "convert", "settings", "topic", "focus",
}


class RemoteError(Exception):
    """Remote HTTP or protocol error mapped to a CLI exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def _request_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
    payload: dict[str, object] | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    url = base_url + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raw_error = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw_error)
        except json.JSONDecodeError:
            detail = {}
        message = str(detail.get("error") or error.reason or f"HTTP {error.code}")
        raw_code = detail.get("exit_code")
        raise RemoteError(message, exit_code=raw_code if isinstance(raw_code, int) and raw_code > 0 else 1) from error
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as error:
        raise RemoteError(f"cannot reach remote workspace {base_url}: {getattr(error, 'reason', error)}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RemoteError(f"remote server returned invalid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise RemoteError("remote server returned an unexpected JSON payload")
    return parsed


def _post_receipt(base_url: str, token: str, path: str, payload: dict[str, object], *, as_json: bool) -> int:
    receipt = _request_json(base_url, token, "POST", path, payload=payload)
    return emit_receipt(receipt, as_json=as_json)


def _remote_post(
    base_url: str,
    token: str,
    args: argparse.Namespace,
    path: str,
    extra: dict[str, object],
) -> int:
    payload: dict[str, object] = {"query": _require_query(args), "match_mode": args.match_mode, **extra}
    return _post_receipt(base_url, token, path, payload, as_json=bool(getattr(args, "as_json", False)))


def _require_query(args: argparse.Namespace) -> str:
    query = " ".join(args.query).strip()
    if not query:
        raise RemoteError(f"{args.command} requires a non-empty query", exit_code=2)
    return query


def _remote_add(base_url: str, token: str, args: argparse.Namespace) -> int:
    for field, variable in (("project", "MT_PROJECT"), ("topic", "MT_TOPIC")):
        value = os.environ.get(variable, "").strip()
        if value and getattr(args, field, None) is None:
            setattr(args, field, value)
    return _post_receipt(
        base_url, token, "/api/v1/add", create_params_from_args(args),
        as_json=bool(getattr(args, "as_json", False)),
    )


def _query_params(query: card_query.Query) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for key, value in card_query.query_to_mapping(query).items():
        if isinstance(value, list):
            params.extend((key, str(entry)) for entry in value)
        elif isinstance(value, bool):
            if value:
                params.append((key, "true"))
        elif value is not None:
            params.append((key, str(value)))
    return params


def _remote_list_or_search(base_url: str, token: str, args: argparse.Namespace, *, search: bool) -> int:
    if args.limit is not None and args.limit < 1:
        raise RemoteError("--limit must be a positive integer", exit_code=2)
    if args.tail is not None and args.tail < 1:
        raise RemoteError("--tail must be a positive integer", exit_code=2)
    try:
        query = card_query.query_from_args(args, env=os.environ)
    except card_query.QueryError as error:
        raise RemoteError(str(error), exit_code=2) from error
    if search and not query.text.strip():
        raise RemoteError("search requires a non-empty query", exit_code=2)

    fmt = query_output_format(args)
    params: list[tuple[str, str]] = [
        ("search", "1" if search else "0"),
        ("sort", args.sort),
        ("order", args.order),
        ("format", fmt),
    ]
    for opt in ("limit", "tail"):
        if getattr(args, opt) is not None:
            params.append((opt, str(getattr(args, opt))))
    params.extend(_query_params(query))

    return emit_query_output(_request_json(base_url, token, "GET", "/api/v1/items", params=params), fmt)


def _remote_settings(base_url: str, token: str, args: argparse.Namespace) -> int:
    response = _request_json(base_url, token, "GET", "/api/v1/settings")
    return emit_settings(response["settings"], as_json=bool(getattr(args, "as_json", False)))


def _remote_topic(base_url: str, token: str, args: argparse.Namespace) -> int:
    if getattr(args, "topic_command", None) != "list":
        raise RemoteError(f"'mt topic {getattr(args, 'topic_command', '')}' is not available in remote mode", exit_code=2)
    params = [("topic", args.topic)] if getattr(args, "topic", None) else []
    response = _request_json(base_url, token, "GET", "/api/v1/topics", params=params)
    return emit_topic_tree(
        list(response.get("topics") or []),
        depth=int(response.get("depth", 0)),
        topic=getattr(args, "topic", None),
        as_json=bool(getattr(args, "as_json", False)),
        paths=bool(getattr(args, "paths", False)),
        tree_view=bool(getattr(args, "tree", False)),
        has_topics=bool(response.get("has_topics", False)),
    )


def _remote_focus(base_url: str, token: str, args: argparse.Namespace) -> int:
    if (
        getattr(args, "next", False)
        or getattr(args, "clear", False)
        or getattr(args, "set_query", None)
        or " ".join(getattr(args, "query", []) or []).strip()
    ):
        raise RemoteError("remote 'mt focus' is read-only; --set, --clear, and --next are not available in remote mode", exit_code=2)
    try:
        query = card_query.query_from_args(args, env=os.environ)
    except card_query.QueryError as error:
        raise RemoteError(str(error), exit_code=2) from error
    response = _request_json(base_url, token, "GET", "/api/v1/focus", params=_query_params(query))
    return emit_focus_context(response, as_json=bool(getattr(args, "as_json", False) or getattr(args, "json", False)))


def _remote_read(base_url: str, token: str, args: argparse.Namespace) -> int:
    is_multi_ref = len(args.query) > 1 and all(q.startswith("@") for q in args.query)
    if is_multi_ref:
        response = _request_json(base_url, token, "GET", "/api/v1/item", params=[("ref", ref) for ref in args.query])
        entries = response.get("items") or []
        if args.as_json:
            emit_json([entry["item"] for entry in entries])
            return 0
        for entry in entries:
            text = entry["item"]["body"] if getattr(args, "body", False) else entry["raw"]
            sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))
        return 0
    response = _request_json(
        base_url, token, "GET", "/api/v1/item",
        params=[("query", _require_query(args)), ("match_mode", args.match_mode)],
    )
    if args.as_json:
        emit_json(response["item"])
        return 0
    sys.stdout.write(response["item"]["body"] if getattr(args, "body", False) else response["raw"])
    return 0


def dispatch_remote(remote_url: str, root_parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Execute a CLI command against a hosted remote workspace; fail closed on any error."""
    if args.command not in SUPPORTED_REMOTE_COMMANDS:
        print(f"error: 'mt {args.command}' is not available in remote mode", file=sys.stderr)
        return 2
    token = os.environ.get("MT_REMOTE_TOKEN", "").strip()
    if not token:
        print("error: MT_REMOTE_TOKEN is required in remote mode", file=sys.stderr)
        return 2
    if args.command == "note" and (getattr(args, "template", None) or (args.file is None and not args.text and sys.stdin.isatty())):
        print("error: 'mt note' in remote mode requires text, --file, or piped stdin", file=sys.stderr)
        return 2
    prepare_command_inputs(root_parser, args)
    try:
        if args.command in {"add", "note"}:
            return _remote_add(remote_url, token, args)
        if args.command in {"list", "search"}:
            return _remote_list_or_search(remote_url, token, args, search=args.command == "search")
        if args.command == "read":
            return _remote_read(remote_url, token, args)
        if args.command == "settings":
            return _remote_settings(remote_url, token, args)
        if args.command == "topic":
            return _remote_topic(remote_url, token, args)
        if args.command == "focus":
            return _remote_focus(remote_url, token, args)
        if args.command == "move":
            return _remote_post(remote_url, token, args, "/api/v1/move", {
                "destination": args.destination,
                "comments": list(args.comments or []),
                "takeover": bool(getattr(args, "takeover", False)),
            })
        if args.command == "edit":
            return _remote_post(remote_url, token, args, "/api/v1/edit", {
                "expected_hash": getattr(args, "expected_hash", None),
                "body_mode": _edit_body_mode_from_args(args),
                "body_text": getattr(args, "file_text", ""),
                "body_replacements": [list(pair) for pair in zip(args.old, args.new)] if args.old is not None else None,
                "exact_replacements_text": getattr(args, "replacements_text", None),
                "metadata_fields": _edit_metadata_from_args(args),
                "assignments": list(args.assignments or []),
                "tag_add": list(args.tag_add) if getattr(args, "tag_add", None) is not None else None,
                "tag_remove": list(args.tag_remove) if getattr(args, "tag_remove", None) is not None else None,
            })
        if args.command == "comment":
            payload: dict[str, object] = {
                "comments": list(args.comments or []),
            }
            if getattr(args, "edit", None) is not None:
                payload["edit"] = args.edit
            if getattr(args, "delete", None) is not None:
                payload["delete"] = args.delete
            if getattr(args, "expected_hash", None) is not None:
                payload["expected_hash"] = args.expected_hash
            return _remote_post(remote_url, token, args, "/api/v1/comment", payload)
        if args.command == "done":
            return _remote_post(remote_url, token, args, "/api/v1/done", {
                "notes": list(args.notes or []),
                "comments": list(args.comments or []),
            })
        if args.command == "convert":
            return _remote_post(remote_url, token, args, "/api/v1/convert", {
                "destination": args.destination,
                "expected_hash": getattr(args, "expected_hash", None),
                "status": getattr(args, "status", None),
                "task_type": getattr(args, "task_type", None),
                "next_action": getattr(args, "next_action", None),
            })
    except RemoteError as error:
        print(f"error: {error.message}", file=sys.stderr)
        return error.exit_code
    return 2
