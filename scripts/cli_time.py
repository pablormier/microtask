"""Time tracking CLI commands for microtask."""
from __future__ import annotations

import argparse
import re
import sys
import time as clock_time
import uuid

import card_format
from cli_helpers import _cli_run_write, resolve_unique, session_window
from operations import _time_end_locked, _time_start_locked, save_time_session
import store
from store import (
    _ws,
    active_root,
    command_query,
    discover,
    format_duration,
    local_stamp,
    now_stamp,
    parse_duration,
    parse_time_argument,
    saved_time_seconds,
    select_cards,
    session_entries,
    task_by_id,
    time_log_entries,
    timer_session_saved,
)
import transaction
from transaction import (
    auto_sync_after_mutation,
    read_time_timers,
    serialized_time_mutation,
)
from workspace import Workspace


def command_time_start(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_run_write(
        ws,
        lambda resolved: _time_start_locked(
            resolved,
            " ".join(args.query),
            parallel=getattr(args, "parallel", False),
            by=getattr(args, "by", None),
        ),
    )


def command_time_status(_args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        timers = read_time_timers(resolved)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not timers:
        print("No timer running.")
        return 0
    for task_id, timer in sorted(timers.items()):
        task = task_by_id(task_id, ws=resolved)
        label = f"[@{task.short_ref}] {task.title}" if task else f"missing task {task_id}"
        already_saved = timer_session_saved(task_id, timer, ws=resolved)
        elapsed = 0 if already_saved else max(0, int(clock_time.time()) - int(timer["start_epoch"]))
        print(f"Timer: {label}")
        print(f"  owner:   {timer['by']}")
        print(f"  started: {timer['start']}")
        if not already_saved and elapsed > float(store.policy(resolved).get("rules", {}).get("timer_warn_hours", 8)) * 3600:
            print("  warning: long-running timer; use time stop --at TIME or --duration DURATION to correct it")
        print(f"  elapsed: {format_duration(elapsed)}" + (" (session already saved)" if already_saved else ""))
        if task:
            print(f"  saved:   {format_duration(saved_time_seconds(task))}")
    return 0


def command_time_end(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_run_write(
        ws,
        lambda resolved: _time_end_locked(
            resolved,
            query=" ".join(getattr(args, "query", [])),
            action=args.time_command,
            at=getattr(args, "at", None),
            duration=getattr(args, "duration", None),
            by=getattr(args, "by", None),
        ),
    )


def command_time_list(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    selection = command_query(args)
    window = session_window(args)
    if selection is None or window is None:
        return 2
    query = " ".join(args.query).strip()
    if query:
        task, result = resolve_unique(query, kind="task", ws=resolved)
        if task is None:
            return result
        tasks = [task]
    else:
        tasks = discover("task", ws=resolved)
    sessions = [(task, entry) for task in select_cards(tasks, selection)
                for entry in session_entries(task, window)]
    if not sessions:
        print("No recorded sessions.")
        return 0
    for task, entry in sorted(sessions, key=lambda pair: (pair[1]["start"], pair[1]["session_id"]), reverse=True):
        print(f"{entry['session_id'][:8]}  {entry['start']} → {entry['end']}  "
              f"{format_duration(int(entry['elapsed_seconds']))}  [@{task.short_ref}] {task.title}")
    return 0


@serialized_time_mutation
def command_time_remove(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    session_ref = args.session_id.lower()
    if not re.fullmatch(r"[0-9a-f]{8,32}", session_ref):
        print("error: use a session ID from 'mt time list' (at least 8 hexadecimal characters)", file=sys.stderr)
        return 2
    matches = [
        (task, entry)
        for task in discover("task", ws=resolved)
        for entry in time_log_entries(task.raw)
        if str(entry["session_id"]).startswith(session_ref)
    ]
    if not matches:
        print(f"error: no recorded session matches '{session_ref}'", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print("error: multiple sessions match; use a longer session ID:", file=sys.stderr)
        for task, entry in matches:
            print(f"  {entry['session_id']}  [@{task.short_ref}] {task.title}", file=sys.stderr)
        return 2
    task, entry = matches[0]
    session_id = str(entry["session_id"])
    properties, body = card_format.parse_document(task.raw, expected_kind="task")
    entries = properties.get("time_entries", [])
    if not isinstance(entries, list):
        print("error: task time_entries property is malformed", file=sys.stderr)
        return 1
    properties["time_entries"] = [entry for entry in entries
                                  if not isinstance(entry, dict) or entry.get("session_id") != session_id]
    if not properties["time_entries"]:
        properties.pop("time_entries")
    updated = card_format.render_document(properties, body, expected_kind="task")
    updated = card_format.replace_property(updated, "updated", now_stamp())
    original = task.path.read_bytes()
    base = active_root(resolved)
    relative = str(task.path.relative_to(base))
    try:
        transaction.apply_item_transaction({relative: updated.encode("utf-8")}, expected={relative: original}, source_root=base, ws=resolved)
    except (OSError, ValueError) as error:
        print(f"error: session removal failed validation; task was restored: {error}", file=sys.stderr)
        return 1
    print(f"Removed session {session_id} ({format_duration(int(entry['elapsed_seconds']))}) "
          f"from [@{task.short_ref}] {task.title}.")
    return auto_sync_after_mutation(ws=resolved)


@serialized_time_mutation
def command_time_add(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        end_epoch = parse_time_argument(args.end) if getattr(args, "end", None) else int(clock_time.time())
        if getattr(args, "start", None):
            if args.duration:
                raise ValueError("use either DURATION or --start, not both")
            start_epoch = parse_time_argument(args.start)
            seconds = end_epoch - start_epoch
        else:
            if not args.duration:
                raise ValueError("provide DURATION or --start TIME")
            seconds = parse_duration(args.duration)
            start_epoch = end_epoch - seconds
        if seconds <= 0 or end_epoch > int(clock_time.time()):
            raise ValueError("session start must precede end, and end cannot be in the future")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    task, result = resolve_unique(args.query, kind="task", ws=resolved)
    if task is None:
        return result
    timer = {
        "session_id": uuid.uuid4().hex,
        "task_id": str(task.frontmatter.get("id") or task.path.stem),
        "start": local_stamp(start_epoch),
        "start_epoch": start_epoch,
    }
    saved, message = save_time_session(task, timer, end_epoch, ws=resolved)
    if not saved:
        print(f"error: {message}", file=sys.stderr)
        return 1
    print(f"Added {format_duration(seconds)} to [@{task.short_ref}] {task.title}.")
    return auto_sync_after_mutation(ws=resolved)


def command_time_report(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    selection = command_query(args)
    window = session_window(args)
    if selection is None or window is None:
        return 2
    grouped: dict[str, int] = {}
    labels: dict[str, list[str]] = {}
    for task in select_cards(discover("task", ws=resolved), selection):
        seconds = sum(int(entry["elapsed_seconds"]) for entry in session_entries(task, window))
        if not seconds:
            continue
        if args.by == "task":
            key = f"[@{task.short_ref}] {task.title}"
        else:
            value = task.frontmatter.get(args.by)
            key = str(value) if value else f"(no {args.by})"
            labels.setdefault(key, []).append(task.title)
        grouped[key] = grouped.get(key, 0) + seconds
    if not grouped:
        print("No tracked time yet.")
        return 0
    for key in sorted(grouped, key=str.casefold):
        suffix = f" ({len(labels.get(key, []))} task(s))" if args.by != "task" else ""
        print(f"{key}: {format_duration(grouped[key])}{suffix}")
    print(f"Total: {format_duration(sum(grouped.values()))}")
    return 0
