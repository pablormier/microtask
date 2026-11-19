"""Time tracking CLI commands for microtask."""
from __future__ import annotations

import argparse
import sys
import time as clock_time

from cli_helpers import _cli_call_op, command_query, resolve_unique, session_window
from operations import op_time_add, op_time_end, op_time_remove, op_time_start
import store
from store import (
    _ws,
    discover,
    format_duration,
    saved_time_seconds,
    select_cards,
    session_entries,
    task_by_id,
    timer_session_saved,
)
from transaction import read_time_timers
from workspace import Workspace


def command_time_start(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_call_op(
        lambda: op_time_start(
            ws,
            " ".join(args.query),
            parallel=getattr(args, "parallel", False),
            by=getattr(args, "by", None),
            quiet_sync=False,
        )
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
        already_saved = timer_session_saved(task, timer)
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
    return _cli_call_op(
        lambda: op_time_end(
            ws,
            query=" ".join(getattr(args, "query", [])),
            action=args.time_command,
            at=getattr(args, "at", None),
            duration=getattr(args, "duration", None),
            by=getattr(args, "by", None),
            quiet_sync=False,
        )
    )


def command_time_list(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    selection = command_query(args, ws=resolved)
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


def command_time_remove(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_call_op(lambda: op_time_remove(ws, args.session_id, quiet_sync=False))


def command_time_add(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    query = " ".join(args.query).strip() if isinstance(args.query, list) else str(args.query or "").strip()
    return _cli_call_op(
        lambda: op_time_add(
            ws,
            query,
            duration=getattr(args, "duration", None),
            start=getattr(args, "start", None),
            end=getattr(args, "end", None),
            quiet_sync=False,
        )
    )


def command_time_report(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    selection = command_query(args, ws=resolved)
    window = session_window(args)
    if selection is None or window is None:
        return 2
    grouped: dict[str, int] = {}
    display_keys: dict[str, str] = {}
    labels: dict[str, list[str]] = {}
    for task in select_cards(discover("task", ws=resolved), selection):
        seconds = sum(int(entry["elapsed_seconds"]) for entry in session_entries(task, window))
        if not seconds:
            continue
        if args.by == "task":
            group_key = f"[@{task.short_ref}] {task.title}"
            display_key = group_key
        else:
            value = task.frontmatter.get(args.by)
            display_key = str(value) if value else f"(no {args.by})"
            group_key = str(value).casefold() if value else display_key
            labels.setdefault(group_key, []).append(task.title)
        display_keys.setdefault(group_key, display_key)
        grouped[group_key] = grouped.get(group_key, 0) + seconds
    if not grouped:
        print("No tracked time yet.")
        return 0
    for group_key in sorted(grouped, key=lambda k: display_keys[k].casefold()):
        suffix = f" ({len(labels.get(group_key, []))} task(s))" if args.by != "task" else ""
        print(f"{display_keys[group_key]}: {format_duration(grouped[group_key])}{suffix}")
    print(f"Total: {format_duration(sum(grouped.values()))}")
    return 0
