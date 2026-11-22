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
from cli_cards import (
    _edit_body_mode_from_args,
    _edit_metadata_from_args,
    card_text_option,
    prepare_command_inputs,
    resolve_needs_review,
)
from cli_helpers import emit_json, emit_query_output, query_output_format

SUPPORTED_REMOTE_COMMANDS = {"add", "note", "list", "search", "read", "move", "edit", "comment", "done"}


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
    if as_json:
        emit_json(receipt)
        return 0
    ref, card_path = receipt.get("ref") or "", receipt.get("path") or ""
    if receipt.get("changed", True):
        print(f"{ref}: {card_path}" if ref and card_path else str(ref or card_path or "Saved."))
    else:
        print(f"No changes: {ref}" if ref else "No changes.")
    if receipt.get("history_warning"):
        print(f"warning: {receipt['history_warning']}", file=sys.stderr)
    return 0


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
    kind = getattr(args, "kind", "task")
    is_capture = bool(getattr(args, "capture", False))
    payload: dict[str, object] = {
        "kind": kind,
        "text": " ".join(args.text),
        "title": getattr(args, "title", None),
        "capture": is_capture,
        "capture_id": getattr(args, "capture_id", None) if is_capture else None,
        "created_at": getattr(args, "created_at", None) if is_capture else None,
        "status": getattr(args, "status", None) if kind == "task" else None,
        "task_type": getattr(args, "task_type", None) if kind == "task" else None,
        "next_action": card_text_option(args, "next_action") if kind == "task" else "",
        "instructions": card_text_option(args, "instructions"),
        "priority": getattr(args, "priority", None),
        "project": getattr(args, "project", None),
        "topic": getattr(args, "topic", None),
        "due": getattr(args, "due", None),
        "due_kind": getattr(args, "due_kind", None),
        "tags": list(getattr(args, "tags", None) or []),
        "needs_review": resolve_needs_review(kind, is_capture, getattr(args, "needs_review", None)),
        "archived": bool(getattr(args, "archived", False)),
        "hidden": bool(getattr(args, "hidden", False)),
    }
    return _post_receipt(base_url, token, "/api/v1/add", payload, as_json=bool(getattr(args, "as_json", False)))


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
    for key, value in card_query.query_to_mapping(query).items():
        if isinstance(value, list):
            params.extend((key, str(entry)) for entry in value)
        elif isinstance(value, bool):
            if value:
                params.append((key, "true"))
        elif value is not None:
            params.append((key, str(value)))

    return emit_query_output(_request_json(base_url, token, "GET", "/api/v1/items", params=params), fmt)


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
    as_json = bool(getattr(args, "as_json", False))
    try:
        if args.command in {"add", "note"}:
            return _remote_add(remote_url, token, args)
        if args.command in {"list", "search"}:
            return _remote_list_or_search(remote_url, token, args, search=args.command == "search")
        if args.command == "read":
            return _remote_read(remote_url, token, args)
        if args.command == "move":
            return _post_receipt(remote_url, token, "/api/v1/move", {
                "query": _require_query(args), "destination": args.destination,
                "comments": list(args.comments or []), "takeover": bool(getattr(args, "takeover", False)),
                "match_mode": args.match_mode,
            }, as_json=as_json)
        if args.command == "edit":
            return _post_receipt(remote_url, token, "/api/v1/edit", {
                "query": _require_query(args), "match_mode": args.match_mode,
                "expected_hash": getattr(args, "expected_hash", None),
                "body_mode": _edit_body_mode_from_args(args),
                "body_text": getattr(args, "file_text", ""),
                "body_replacements": [list(pair) for pair in zip(args.old, args.new)] if args.old is not None else None,
                "exact_replacements_text": getattr(args, "replacements_text", None),
                "metadata_fields": _edit_metadata_from_args(args),
                "assignments": list(args.assignments or []),
                "tag_add": list(args.tag_add) if getattr(args, "tag_add", None) is not None else None,
                "tag_remove": list(args.tag_remove) if getattr(args, "tag_remove", None) is not None else None,
            }, as_json=as_json)
        if args.command == "comment":
            return _post_receipt(remote_url, token, "/api/v1/comment", {
                "query": _require_query(args), "comments": list(args.comments or []), "match_mode": args.match_mode,
            }, as_json=as_json)
        if args.command == "done":
            return _post_receipt(remote_url, token, "/api/v1/done", {
                "query": _require_query(args), "notes": list(args.notes or []),
                "comments": list(args.comments or []), "match_mode": args.match_mode,
            }, as_json=as_json)
    except RemoteError as error:
        print(f"error: {error.message}", file=sys.stderr)
        return error.exit_code
    return 2
