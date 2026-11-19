"""Read-only focus ranking and context helpers."""
from __future__ import annotations

from datetime import datetime
import re

import card_query
from store import (
    FOCUS_REASONS,
    VAGUE_RESTART_PATTERN,
    Card,
    _ws,
    due_kind_value,
    due_sort_value,
    item_json_record,
    now_stamp,
    read_focus_state,
)
from workspace import Workspace


def focus_sort_key(task: Card, rank: dict[str, int]) -> tuple[int, str, str]:
    priority = str(task.frontmatter.get("priority") or "medium")
    due = due_sort_value(task.frontmatter.get("due")) or "9999-12-31T23:59:59"
    return rank.get(priority, len(rank)), due, task.path.name


def is_high_priority_task(task: Card) -> bool:
    priority = str(task.frontmatter.get("priority") or "medium")
    return priority in {"urgent", "high"}


def has_deadline_due(task: Card) -> bool:
    return due_kind_value(task.frontmatter) == "deadline"


def ranked_focus_candidates(tasks: list[Card], rank: dict[str, int]) -> list[tuple[Card, str]]:
    """Rank actionable tasks; deadline alerts cover blocked and deferred work."""
    now = datetime.now()

    def due(task: Card, mode: str) -> bool:
        return has_deadline_due(task) and card_query.due_matches(task.frontmatter.get("due"), mode, now)

    overdue, today, week = card_query.DUE_OVERDUE, card_query.DUE_TODAY, card_query.DUE_WEEK

    def due_value(task: Card) -> str | None:
        return due_sort_value(task.frontmatter.get("due")) or None

    groups = (
        ((task for task in tasks if due(task, today) and not due(task, overdue)), "due_today"),
        ((task for task in tasks if due(task, overdue) and is_high_priority_task(task)), "high_overdue"),
        ((task for task in tasks if task.frontmatter.get("status") == "doing"), "doing"),
        ((task for task in tasks if due(task, week) and not due(task, today)), "due_soon"),
        ((task for task in tasks if due(task, overdue) and not is_high_priority_task(task)), "deadline_review"),
        ((task for task in tasks if task.frontmatter.get("status") == "active"), "active"),
        ((task for task in tasks if task.frontmatter.get("status") == "inbox"), "inbox"),
    )
    ordered: list[tuple[Card, str]] = []
    seen: set[str] = set()
    for candidates, reason in groups:
        def key(item: Card) -> tuple:
            order = focus_sort_key(item, rank)
            return (due_value(item) or "", *order) if reason in {"due_today", "due_soon"} else order

        for task in sorted(candidates, key=key):
            task_id = str(task.frontmatter.get("id") or task.path.stem)
            if task_id in seen:
                continue
            seen.add(task_id)
            ordered.append((task, reason))
    return ordered


def task_display_sort_key(task: Card, rank: dict[str, int]) -> tuple[int, int, str, str]:
    value, now = task.frontmatter.get("due"), datetime.now()
    due_group = 0 if any(card_query.due_matches(value, mode, now) for mode in (
        card_query.DUE_OVERDUE, card_query.DUE_TODAY)) else 1 if value else 2
    priority, normalized_due, _ = focus_sort_key(task, rank)
    return due_group, priority, normalized_due, str(task.path)


def followup_sort_key(task: Card, rank: dict[str, int]) -> tuple[int, str, int, str]:
    due = due_sort_value(task.frontmatter.get("due"))
    priority = str(task.frontmatter.get("priority") or "medium")
    due_group = 0 if card_query.due_matches(task.frontmatter.get("due"), card_query.DUE_OVERDUE, datetime.now()) else 1 if due else 2
    return due_group, due or "9999-12-31T23:59:59", rank.get(priority, len(rank)), task.path.name


def focus_context(tasks: list[Card], *, actor: str | None = None, ws: Workspace | None = None) -> dict:
    """One read-only focus snapshot for the CLI, dashboard, and skills.

    Callers select visibility and workspace filters before calling this function.
    Saved selection never suppresses alerts or changes the candidate order.
    """
    resolved = _ws(ws)
    import store

    cfg = store.policy(resolved)
    actor = store.current_username(actor, ws=resolved)
    rank = {name: index for index, name in enumerate(cfg.get("priorities", {}))}
    tasks = [task for task in tasks if task.frontmatter.get("status") != "done"]
    available = [task for task in tasks
                 if task.frontmatter.get("status") in {"active", "doing", "inbox"}
                 and task.frontmatter.get("claimed_by") in {None, "", actor}]

    def record(task: Card, reason_key: str) -> dict:
        return {**item_json_record(task, ws=resolved), "reason_key": reason_key,
                "reason": FOCUS_REASONS[reason_key]}

    candidates = [record(task, reason) for task, reason in ranked_focus_candidates(available, rank)]
    state = read_focus_state(resolved)
    selected = next((item for item in candidates if state and item["id"] == state.task_id), None)
    mode = state.mode if selected and state else "auto"
    if selected:
        selected = {**selected, "reason_key": mode, "reason": FOCUS_REASONS[mode]}
    elif candidates:
        selected = candidates[0]

    now = datetime.now()
    alerts = []
    deadline_order = lambda item: (due_sort_value(item.frontmatter.get("due")) or "9999",
                                   *focus_sort_key(item, rank))
    for task in sorted(tasks, key=deadline_order):
        value = task.frontmatter.get("due")
        # Overdue or due within the week: the same cards as `--due next`.
        if not has_deadline_due(task) or not any(card_query.due_matches(value, mode, now) for mode in (
                card_query.DUE_OVERDUE, card_query.DUE_WEEK)):
            continue
        owner = task.frontmatter.get("claimed_by")
        status = task.frontmatter.get("status")
        if owner and owner != actor:
            action = "coordinate"
        elif status == "waiting":
            action = "followup"
        elif status not in {"active", "doing", "inbox"}:
            action = "review"
        else:
            action = "work"
        action_label = {"coordinate": "Check with the owner", "followup": "Follow up",
                        "review": "Review the commitment", "work": "Allow time for this"}[action]
        alerts.append({**item_json_record(task, ws=resolved), "action": action, "action_label": action_label})
    return {"selected": selected, "mode": mode, "actor": actor,
            "generated_at": now_stamp(), "candidates": candidates, "alerts": alerts,
            "followup_ids": [str(task.frontmatter.get("id") or task.path.stem)
                             for task in sorted(tasks, key=lambda item: followup_sort_key(item, rank))
                             if task.frontmatter.get("status") == "waiting"]}


def next_action_needs_restart_step(item: Card) -> bool:
    value = str(item.next_action or "").strip().casefold()
    return bool(value) and (value == "tbd" or re.search(VAGUE_RESTART_PATTERN, value) is not None)
