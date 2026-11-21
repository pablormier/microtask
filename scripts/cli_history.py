"""CLI commands for inspecting and restoring local workspace history."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zlib

import history
import operations
import store
from store import _ws
from workspace import Workspace


def _format_event_line(event: dict) -> str:
    revs = event.get("revisions") or []
    if not revs:
        detail = event.get("note") or "(no file changes)"
    elif len(revs) == 1:
        rev = revs[0]
        title = f" — {rev['title']}" if rev.get("title") else ""
        detail = f"{rev['change_type']} {rev['path']}{title}"
    else:
        detail = f"{len(revs)} files ({revs[0]['path']}, ...)"
    status_tag = f" ({event['status']})" if event.get("status", "committed") != "committed" else ""
    return f"#{event['id']}  {event['ts']}  {event['actor']}  [{event['source']}:{event['action']}]{status_tag}  {detail}"


def _show_event(resolved: Workspace, event_id: int, *, diff: bool, as_json: bool) -> int:
    try:
        event = history.get_event(resolved, event_id, include_diffs=bool(diff or as_json))
    except (sqlite3.Error, OSError, zlib.error) as error:
        print(f"error: could not read local history: {error}", file=sys.stderr)
        return 1
    if event is None:
        print(f"error: history event #{event_id} not found", file=sys.stderr)
        return 1
    if as_json:
        serializable = {
            "id": event["id"],
            "ts": event["ts"],
            "actor": event["actor"],
            "action": event["action"],
            "source": event["source"],
            "note": event["note"],
            "status": event["status"],
            "revisions": [
                {
                    "id": rev["id"],
                    "path": rev["path"],
                    "card_id": rev["card_id"],
                    "title": rev["title"],
                    "change_type": rev["change_type"],
                    "before_sha256": rev["before_sha256"],
                    "after_sha256": rev["after_sha256"],
                    "before_text": rev["before_text"],
                    "after_text": rev["after_text"],
                    "diff": rev["diff"],
                }
                for rev in event["revisions"]
            ],
        }
        print(json.dumps(serializable, indent=2))
        return 0
    print(f"Event #{event['id']}")
    print(f"  Time:   {event['ts']}")
    print(f"  Actor:  {event['actor']}")
    print(f"  Action: {event['action']} ({event['source']})")
    if event["note"]:
        print(f"  Note:   {event['note']}")
    for rev in event["revisions"]:
        title = f" ({rev['title']})" if rev["title"] else ""
        print(f"  - {rev['change_type']}: {rev['path']}{title}")
        if diff and rev["diff"]:
            print(rev["diff"], end="" if rev["diff"].endswith("\n") else "\n")
    return 0


def command_history(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    target = getattr(args, "target", None)
    event_id_raw = getattr(args, "event_id", None)
    if target == "show":
        if event_id_raw is None:
            print("error: usage: mt history show EVENT_ID [--diff] [--json]", file=sys.stderr)
            return 2
        try:
            event_id = int(event_id_raw)
        except ValueError:
            print(f"error: invalid event id: {event_id_raw}", file=sys.stderr)
            return 2
        return _show_event(resolved, event_id, diff=bool(args.diff), as_json=bool(args.as_json))
    if event_id_raw is not None:
        print(f"error: unexpected argument: {event_id_raw}", file=sys.stderr)
        return 2
    try:
        events = history.list_events(
            resolved,
            card_query=target,
            deleted_only=bool(args.deleted),
            actor=args.actor,
            limit=args.limit,
        )
    except (sqlite3.Error, OSError, zlib.error) as error:
        print(f"error: could not read local history: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(events, indent=2))
        return 0
    if not events:
        print("No history events found.")
        return 0
    for event in events:
        print(_format_event_line(event))
    return 0


def command_restore(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        result = operations.op_restore(
            resolved,
            args.event_id,
            path=args.path,
            force=bool(args.force),
            quiet_sync=bool(args.as_json),
        )
    except (sqlite3.Error, OSError, zlib.error) as error:
        print(f"error: could not read local history: {error}", file=sys.stderr)
        return 1
    except store.TaskError as error:
        prefix = "error: " if error.prefixed else ""
        print(f"{prefix}{error}", file=sys.stderr)
        return error.exit_code
    if args.as_json:
        payload = dict(result.receipt)
        if result.sync_error:
            payload["sync_error"] = result.sync_error
        if result.history_warning:
            payload["history_warning"] = result.history_warning
        print(json.dumps(payload, indent=2))
    elif result.message:
        print(result.message)
    if not args.as_json and result.history_warning:
        print(f"warning: {result.history_warning}", file=sys.stderr)
    return result.sync_exit_code
