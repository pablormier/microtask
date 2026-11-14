"""Core workspace write and read operations for microtask."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
import time as clock_time
import uuid

import card_format
import card_query
import drafts as draft_store
import store
from store import (
    CLAIM_FIELDS,
    FOCUS_REASONS,
    VAGUE_RESTART_PATTERN,
    Card,
    CardInputError,
    ConflictError,
    InputError,
    NotFoundError,
    TaskError,
    ValidationError,
    _resolve_unique_card,
    _ws,
    abbreviate,
    active_root,
    card_by_capture_id,
    current_username,
    demo_active,
    demo_root,
    discover,
    due_kind_error,
    due_kind_value,
    due_sort_value,
    format_duration,
    is_archived,
    is_due_value,
    is_hidden,
    is_timestamp,
    item_json_record,
    item_receipt,
    local_stamp,
    now_stamp,
    open_statuses,
    parse_duration,
    parse_frontmatter,
    parse_time_argument,
    parse_yaml,
    pin_demo_state,
    read_focus_state,
    real_cards_root,
    saved_receipt,
    select_cards,
    slugify,
    task_by_id,
    time_session_for,
)
import transaction
from transaction import (
    MutationReceipt,
    _require_mutation_lock,
    auto_sync_after_mutation,
    read_time_timers,
    time_mutation_lock,
)
from workspace import Workspace


@dataclass(frozen=True)
class WriteResult:
    """Result of a workspace write operation."""

    card: "Card | None"
    receipt: dict[str, object]
    message: str = ""
    changed: bool = True
    sync_error: str | None = None



def _run_write_operation(
    ws: Workspace | None,
    operation,
    *,
    sync: bool = True,
) -> WriteResult:
    """Execute one write operation under time_mutation_lock, then run auto-sync if enabled."""
    resolved = _ws(ws)
    try:
        with time_mutation_lock(resolved):
            result = operation(resolved)
    except TaskError as error:
        if sync and error.sync_needed and store.policy(resolved).get("rules", {}).get("auto_sync", False):
            transaction.run_auto_sync_for_receipt(ws=resolved)
        raise
    if sync and result.changed and store.policy(resolved).get("rules", {}).get("auto_sync", False):
        sync_error = transaction.run_auto_sync_for_receipt(ws=resolved)
        if sync_error:
            return replace(result, sync_error=sync_error)
    return result



def unique_item_timestamp(*, real: bool = False, ws: Workspace | None = None) -> str:
    resolved = _ws(ws)
    current = datetime.now().replace(microsecond=0)
    while True:
        timestamp = current.strftime("%Y-%m-%d-%H%M%S")
        root = resolved.real_cards_root() if real else resolved.cards_root()
        if not any(path.name.startswith(f"{timestamp}-") for path in root.glob("*.md")):
            return timestamp
        current += timedelta(seconds=1)



def clean_text_values(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]



def append_comments(raw: str, comments: list[str], timestamp: str) -> str:
    cleaned = clean_text_values(comments)
    if not cleaned:
        return raw
    properties, body = card_format.parse_document(raw)
    records = properties.setdefault("comments", [])
    if not isinstance(records, list):
        raise ValueError("comments must be a list")
    records.extend({"at": timestamp, "text": comment} for comment in cleaned)
    return card_format.render_document(properties, body)



def append_time_entry(raw: str, entry: dict[str, object]) -> str:
    properties, body = card_format.parse_document(raw)
    if not isinstance(entry, dict) or set(entry) != {"session_id", "start", "end", "elapsed_seconds"}:
        raise ValueError("time entry must contain session_id, start, end, and elapsed_seconds")
    records = properties.setdefault("time_entries", [])
    if not isinstance(records, list):
        raise ValueError("time_entries must be a list")
    records.append(dict(entry))
    return card_format.render_document(properties, body)



def blank_line_separator(body: str) -> str:
    if not body:
        return ""
    if re.search(r"(?:\r?\n){2}$", body):
        return ""
    newline = "\r\n" if body.endswith("\r\n") else "\n" if body.endswith("\n") else (
        "\r\n" if "\r\n" in body and "\n" not in body.replace("\r\n", "") else "\n"
    )
    return newline if body.endswith(("\n", "\r")) else newline * 2



def append_completion_note(raw: str, notes: list[str], today: str) -> str:
    cleaned = clean_text_values(notes)
    if not cleaned:
        return raw
    properties, body = card_format.parse_document(raw)
    comments = properties.setdefault("comments", [])
    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    comments.extend({"at": today, "text": note} for note in cleaned)
    return card_format.render_document(properties, body)



def finish_lifecycle_timer(
    task: Card, raw: str, end_epoch: int, ws: Workspace | None = None,
) -> tuple[str, dict | None]:
    """Stage a session in the same card transaction as its lifecycle change."""
    task_id = str(task.frontmatter.get("id") or task.path.stem)
    timer = read_time_timers(ws).get(task_id)
    if timer:
        entry, already_saved = time_session_for(task_id, timer, end_epoch, ws=ws)
        if not already_saved:
            raw = append_time_entry(raw, entry)
    return raw, timer



def cleanup_lifecycle_timer(task: Card, timer: dict | None, ws: Workspace | None = None) -> None:
    if timer:
        try:
            transaction.clear_time_timer(
                str(task.frontmatter.get("id") or task.path.stem),
                str(timer["session_id"]),
                ws=ws,
            )
        except OSError as error:
            print(f"warning: session saved, but timer cleanup failed: {error}; retry time stop safely", file=sys.stderr)



def save_time_session(
    task: Card, timer: dict[str, object], end_epoch: int, ws: Workspace | None = None,
) -> tuple[bool, str]:
    """Append one session; restore the card and retain timer state on validation failure."""
    resolved = _ws(ws)
    task_id = str(task.frontmatter.get("id") or task.path.stem)
    entry, already_saved = time_session_for(task_id, timer, end_epoch, ws=resolved)
    if already_saved:
        return True, "already saved"
    original = task.path.read_bytes()
    updated = append_time_entry(task.raw, entry)
    updated = card_format.replace_property(updated, "updated", now_stamp())
    base = active_root(resolved)
    relative = str(task.path.relative_to(base))
    try:
        transaction.apply_item_transaction({relative: updated.encode("utf-8")}, expected={relative: original}, source_root=base)
    except (OSError, ValueError):
        return False, "time log failed validation; running timer was retained"
    return True, "saved"



def focus_sort_key(task: Card, rank: dict[str, int]) -> tuple[int, str, str]:
    priority = str(task.frontmatter.get("priority") or "medium")
    due = due_sort_value(task.frontmatter.get("due")) or "9999-12-31T23:59:59"
    return rank.get(priority, len(rank)), due, task.path.name



def is_high_priority_task(task: Card) -> bool:
    priority = str(task.frontmatter.get("priority") or "medium")
    return priority in {"urgent", "high"}



def has_deadline_due(task: Card) -> bool:
    return due_kind_value(task.frontmatter) == "deadline"



def ranked_focus_candidates(tasks: list["Task"], rank: dict[str, int]) -> list[tuple["Task", str]]:
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



def task_display_sort_key(task: "Card", rank: dict[str, int]) -> tuple[int, int, str, str]:
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



def focus_context(tasks: list["Task"], *, actor: str | None = None, ws: Workspace | None = None) -> dict:
    """One read-only focus snapshot for the CLI, dashboard, and skills.

    Callers select visibility and workspace filters before calling this function.
    Saved selection never suppresses alerts or changes the candidate order.
    """
    resolved = _ws(ws)
    actor = current_username(actor)
    rank = {name: index for index, name in enumerate(store.policy(resolved).get("priorities", {}))}
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
        alerts.append({**item_json_record(task), "action": action, "action_label": action_label})
    return {"selected": selected, "mode": mode, "actor": actor,
            "generated_at": now_stamp(), "candidates": candidates, "alerts": alerts,
            "followup_ids": [str(task.frontmatter.get("id") or task.path.stem)
                             for task in sorted(tasks, key=lambda item: followup_sort_key(item, rank))
                             if task.frontmatter.get("status") == "waiting"]}



def next_action_needs_restart_step(item: Card) -> bool:
    value = str(item.next_action or "").strip().casefold()
    return bool(value) and (value == "tbd" or re.search(VAGUE_RESTART_PATTERN, value) is not None)



def render_item_document(
    *,
    item_mode: str,
    item_id: str,
    title: str,
    priority: object,
    project: object,
    topic: object,
    due: object,
    due_kind: object,
    created: object,
    updated: object,
    tags: object,
    body_text: str,
    archived: object = False,
    hidden: object = False,
    needs_review: object = False,
    github_source: object = None,
    github_source_hash: object = None,
    capture_id: str | None = None,
    status: object | None = None,
    task_type: object | None = None,
    next_action: str | None = None,
    processing_instructions: str = "",
) -> str:
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    properties: dict[str, object] = {
        "id": item_id, "title": title.strip(), "kind": item_mode,
        "priority": priority, "created": str(created), "updated": str(updated),
    }
    if capture_id:
        properties["capture_id"] = capture_id
    if github_source is not None:
        properties["github_source"] = github_source
    if github_source_hash is not None:
        properties["github_source_hash"] = github_source_hash
    for field, value in (("project", project), ("topic", topic), ("due", due), ("due_kind", due_kind)):
        if value is not None:
            properties[field] = value
    for field, value in (("archived", archived), ("hidden", hidden), ("needs_review", needs_review)):
        if value:
            properties[field] = True
    if tags:
        properties["tags"] = tags
    if item_mode == "task":
        if status is None or task_type is None:
            raise ValueError("task status and type are required")
        properties.update(status=status, type=task_type)
        if next_action:
            properties["next_action"] = next_action
    if processing_instructions:
        properties["processing_instructions"] = processing_instructions
    if item_mode == "note" and (status is not None or task_type is not None or next_action):
        raise ValueError("task-only properties cannot be set on a note")
    if github_source is not None or github_source_hash is not None:
        if github_source is None or github_source_hash is None:
            raise ValueError("github_source and github_source_hash must be set together")
        properties["github_source"] = github_source
        properties["github_source_hash"] = github_source_hash
    if capture_id is not None:
        properties["capture_id"] = capture_id
    return card_format.render_document(properties, body_text, expected_kind=item_mode)



def parse_tag_json(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("tags must be a JSON string array") from error
    if not isinstance(parsed, list) or any(
        not isinstance(tag, str) or not tag.strip() or any(c in tag for c in "\r\n\x00")
        for tag in parsed
    ):
        raise ValueError("tags must be a JSON array of non-empty single-line strings")
    return list(dict.fromkeys(tag.strip() for tag in parsed))



def check_claim(task: Card, username: str, *, takeover: bool = False) -> None:
    owner = task.frontmatter.get("claimed_by")
    if owner and owner != username and not takeover:
        raise ValueError(
            f"task is claimed by '{owner}'. If you are {owner}, add --by {shlex.quote(owner)} "
            f"(or set MT_USERNAME={shlex.quote(owner)}). Taking over someone else's claim needs the user's "
            f"permission: mt move @{task.short_ref} doing --by {shlex.quote(username)} --takeover."
        )



def transition_claim(task: Card, raw: str, destination: str, username: str, timestamp: str) -> str:
    owner = task.frontmatter.get("claimed_by")
    properties, body = card_format.parse_document(raw, expected_kind="task")
    if destination == "doing":
        if owner == username:
            return raw
        properties["claimed_by"] = username
        properties["claimed_at"] = timestamp
        comment = f"{username} took over the claim from {owner}." if owner else f"{username} claimed this task."
    elif owner:
        for field in CLAIM_FIELDS:
            properties.pop(field, None)
        comment = f"{username} moved this task to {destination}; released the claim held by {owner}."
    else:
        return raw
    raw = card_format.render_document(properties, body, expected_kind="task")
    return append_comments(raw, [comment], timestamp)



def task_defaults(cfg: dict) -> tuple[str, str, str]:
    values: list[str] = []
    for field, allowed_field, singular in (
        ("default_status", "statuses", "status"),
        ("default_priority", "priorities", "priority"),
        ("default_type", "types", "type"),
    ):
        value = cfg.get(field)
        allowed = cfg.get(allowed_field)
        if not isinstance(value, str) or not isinstance(allowed, dict) or value not in allowed:
            raise ValueError(f"policy {field} must name a configured {singular}")
        values.append(value)
    return values[0], values[1], values[2]



def unassigned_priority(cfg: dict) -> str:
    """Use the unassigned class when supported, for explicit unassigned flows."""
    return "none" if "none" in cfg.get("priorities", {}) else task_defaults(cfg)[1]



def default_priority(cfg: dict) -> str:
    """Return the shared policy default used when creating either card kind."""
    return task_defaults(cfg)[1]



def validate_shared_metadata(
    cfg: dict,
    *,
    priority: object = None,
    default_priority: bool = True,
    project: object = None,
    topic: object = None,
    due: object = None,
    due_kind: object = None,
    tags: object = None,
) -> dict[str, object]:
    """Normalize and validate shared metadata at every card-write boundary."""
    selected_priority = task_defaults(cfg)[1] if priority is None and default_priority else priority
    if selected_priority is not None and (
        not isinstance(selected_priority, str) or selected_priority not in cfg.get("priorities", {})
    ):
        raise ValueError("priority must be an allowed effective policy value")
    normalized: dict[str, object] = {"priority": selected_priority}
    if project is not None and (not isinstance(project, str) or not project.strip()
                                or any(char in project for char in "\r\n\x00")):
        raise ValueError("project must be non-empty single-line text")
    normalized["project"] = project.strip() if isinstance(project, str) else None
    if topic is not None:
        topic_problem = "must be text" if not isinstance(topic, str) else card_format.topic_path_error(topic)
        if topic_problem:
            raise ValueError(f"topic {topic_problem}")
        normalized["topic"] = card_format.normalize_topic(topic)
    else:
        normalized["topic"] = None
    if due is not None and (not isinstance(due, str) or not is_due_value(due)):
        raise ValueError("due must be a valid date or local timestamp")
    if due is not None and due_kind_error(due_kind, due, cfg):
        raise ValueError(due_kind_error(due_kind, due, cfg) or "invalid due kind")
    if due is None and due_kind is not None:
        raise ValueError("due_kind requires due")
    normalized.update(due=due, due_kind=due_kind)
    if tags is None:
        tags = []
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or not tag.strip() or any(char in tag for char in "\r\n\x00")
        for tag in tags
    ):
        raise ValueError("tags must be non-empty single-line strings")
    normalized["tags"] = list(dict.fromkeys(tag.strip() for tag in tags))
    return normalized



def render_new_card(
    kind: str,
    card_id: str,
    title: str,
    *,
    body_text: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    next_action: str = "",
    processing_instructions_text: str = "",
    priority: str | None = None,
    project: str | None = None,
    topic: str | None = None,
    due: str | None = None,
    due_kind: str | None = None,
    tags: list[str] | None = None,
    created_at: str | None = None,
    capture_id: str | None = None,
    archived: bool = False,
    hidden: bool = False,
    needs_review: bool = False,
) -> str:
    """Render a new card; tasks fill status and type from policy defaults."""
    cfg = store.policy()
    default_status, default_priority, default_type = task_defaults(cfg)
    if kind == "task":
        status, task_type = status or default_status, task_type or default_type
    stamp = now_stamp()
    return render_item_document(
        item_mode=kind, item_id=card_id, title=title, priority=priority or default_priority,
        project=project, topic=topic, due=due, due_kind=due_kind, created=created_at or stamp, updated=stamp,
        tags=tags or [], body_text=title if body_text is None else body_text, status=status,
        task_type=task_type, next_action=next_action,
        processing_instructions=processing_instructions_text,
        archived=archived, hidden=hidden, needs_review=needs_review,
        capture_id=capture_id,
    )



def render_converted_note(task: Card, today: str) -> str:
    properties = {key: value for key, value in task.frontmatter.items()
                  if key != "kind" and key not in card_format.TASK_ONLY_FIELDS}
    properties.update(kind="note", title=task.title, updated=today)
    comments = list(properties.get("comments", []))
    properties["comments"] = comments
    action = task.frontmatter.get("next_action")
    history = (f"Converted from task on {today}.\n\nStatus: {task.frontmatter.get('status')}\n"
               f"Type: {task.frontmatter.get('type')}\nNext action:\n{action if isinstance(action, str) and action else '(none)'}")
    comments.append({"at": today, "text": history})
    return card_format.render_document(properties, task.body, expected_kind="note")



def render_converted_task(note: Card, today: str, *, status_override: str | None = None,
                          type_override: str | None = None,
                          next_action_override: str | None = None) -> str:
    cfg = store.policy()
    default_status, _priority, default_type = task_defaults(cfg)
    status = status_override or default_status
    task_type = type_override or default_type
    if status not in open_statuses():
        raise ValueError(f"task status must be one of: {', '.join(open_statuses())}")
    if task_type not in cfg.get("types", {}):
        raise ValueError("task type must be an allowed effective policy value")
    properties = {key: value for key, value in note.frontmatter.items()
                  if key not in {"kind", "status", "type", "next_action"}}
    properties.update(kind="task", title=note.title, status=status, type=task_type, updated=today)
    if next_action_override:
        properties["next_action"] = next_action_override
    else:
        properties.pop("next_action", None)
    return card_format.render_document(properties, note.body, expected_kind="task")



def card_capture_fields(card: Card) -> dict[str, object]:
    """Properties a capture retry must match to be treated as the same card."""
    fm = card.frontmatter
    return {
        "kind": card.kind, "title": card.title, "created": str(fm.get("created") or ""),
        "status": fm.get("status") if card.is_task else None,
        "type": fm.get("type") if card.is_task else None,
        "next_action": card.next_action or None,
        "processing_instructions": fm.get("processing_instructions") or None,
        "priority": fm.get("priority"), "project": fm.get("project"), "topic": fm.get("topic"),
        "due": fm.get("due"), "due_kind": due_kind_value(fm), "tags": fm.get("tags") or [],
        "needs_review": fm.get("needs_review") is True,
        "archived": is_archived(card), "hidden": is_hidden(card),
    }



def create_card(
    kind: str,
    text: str,
    *,
    title: str,
    created: str | None = None,
    capture_id: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    next_action: str = "",
    instructions: str = "",
    priority: str | None = None,
    project: str | None = None,
    topic: str | None = None,
    due: str | None = None,
    due_kind: str | None = None,
    tags: list[str] | None = None,
    needs_review: bool = False,
    archived: bool = False,
    hidden: bool = False,
    by: str | None = None,
    ws: Workspace | None = None,
) -> tuple[Card, bool]:
    """Validate, deduplicate by capture ID, and write one new task or note.

    Every creation path (add, note, capture, drafts, dashboard) uses this.
    Returns the saved card and whether a new file was written.
    """
    resolved = _ws(ws)
    try:
        cfg = store.policy(resolved)
        default_status, _priority, default_type = task_defaults(cfg)
        shared = validate_shared_metadata(
            cfg, priority=priority, project=project, topic=topic, tags=tags, due=due, due_kind=due_kind,
        )
    except ValueError as error:
        raise CardInputError(str(error)) from error
    title = title.strip()
    if not title or any(character in title for character in "\r\n\x00"):
        raise CardInputError("title must be non-empty single-line text")
    if kind == "task":
        status = status or default_status
        allowed_open = open_statuses(resolved)
        if status not in allowed_open:
            raise CardInputError(f"task status must be one of: {', '.join(allowed_open)}")
        task_type = task_type or default_type
        if task_type not in cfg.get("types", {}):
            raise CardInputError("task type must be an allowed effective policy value")
        if by is not None and status != "doing":
            raise CardInputError("--by requires status doing")
    elif kind == "note":
        if status is not None or task_type is not None or next_action or by is not None:
            raise CardInputError("status, type, next action, and claims apply only to tasks")
    else:
        raise CardInputError("kind must be task or note")
    created = created or now_stamp()
    desired = {
        "kind": kind, "title": title, "created": created, "status": status, "type": task_type,
        "next_action": next_action or None, "processing_instructions": instructions or None,
        **shared, "needs_review": bool(needs_review), "archived": bool(archived), "hidden": bool(hidden),
    }
    if capture_id is not None:
        existing = card_by_capture_id(capture_id, ws=resolved)
        if existing is not None:
            if card_capture_fields(existing) == desired and existing.body == text:
                return existing, False
            raise CardInputError(
                "conflict: capture ID already exists with different kind, title, body, created-at, or metadata"
            )
    card_id = f"{unique_item_timestamp(real=True, ws=resolved)}-{slugify(title)}"
    destination = real_cards_root(resolved) / f"{card_id}.md"
    if destination.exists():
        raise CardInputError(f"{kind} file already exists: {destination}")
    try:
        rendered = render_new_card(
            kind, card_id, title, body_text=text, status=status, task_type=task_type,
            next_action=next_action, processing_instructions_text=instructions,
            priority=str(shared["priority"]), project=shared["project"], topic=shared["topic"],
            due=shared["due"], due_kind=shared["due_kind"], tags=shared["tags"],
            created_at=created, capture_id=capture_id,
            archived=bool(archived), hidden=bool(hidden), needs_review=bool(needs_review),
        )
        if kind == "task" and status == "doing":
            frontmatter, body = parse_frontmatter(rendered)
            pending = Card(destination, rendered, frontmatter, body, parse_yaml(frontmatter))
            rendered = transition_claim(pending, rendered, "doing", current_username(by, ws=resolved), now_stamp())
    except ValueError as error:
        raise CardInputError(str(error)) from error
    # New user captures always enter the real workspace. Demo cards are a
    # read-only sample and the first real capture switches the view over.
    relative = str(destination.relative_to(resolved.root))
    transaction.apply_item_transaction({relative: rendered.encode("utf-8")}, expected={relative: None}, source_root=resolved.root, ws=resolved)
    pin_demo_state(resolved)
    return Card.read(destination), True



def card_text_option(args: argparse.Namespace, name: str) -> str:
    """Read an optional text property supplied inline or from a --*-file option."""
    value = str(getattr(args, f"{name}_text", None) or getattr(args, name, "") or "")
    return value if value.strip() else ""



def capture_card(
    args: argparse.Namespace, text: str, kind: str, ws: Workspace | None = None,
) -> tuple[Card, bool]:
    """Save one quick capture with a retry-safe capture ID when one is supplied.

    Captured notes default to needing review; captured tasks are triaged by status.
    """
    raw_title = getattr(args, "title", None)
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    if not title and text.strip():
        title = title_from_note_body(text) if kind == "note" else capture_title(text)
    if not title:
        raise CardInputError(f"{kind} requires non-empty text or a title")
    supplied_id = getattr(args, "capture_id", None) or None
    created_at = getattr(args, "created_at", None) or None
    capture_id = None
    if supplied_id is not None:
        try:
            capture_id = str(uuid.UUID(supplied_id))
        except (AttributeError, ValueError) as error:
            raise CardInputError("capture ID must be a canonical UUID") from error
        if capture_id != supplied_id.lower():
            raise CardInputError("capture ID must be a canonical UUID")
        if created_at is None or not is_timestamp(created_at):
            raise CardInputError("a capture ID requires a created-at time in YYYY-MM-DDTHH:MM:SS format")
    elif created_at is not None:
        raise CardInputError("a created-at time requires a capture ID")
    needs_review = getattr(args, "needs_review", None)
    return create_card(
        kind, text, title=title, capture_id=capture_id,
        created=created_at or getattr(args, "capture_timestamp", None),
        status=getattr(args, "status", None) if kind == "task" else None,
        task_type=(getattr(args, "task_type", None) or getattr(args, "type", None)) if kind == "task" else None,
        next_action=card_text_option(args, "next_action") if kind == "task" else "",
        instructions=card_text_option(args, "instructions"),
        priority=getattr(args, "priority", None), project=getattr(args, "project", None),
        topic=getattr(args, "topic", None), due=getattr(args, "due", None),
        due_kind=getattr(args, "due_kind", None), tags=getattr(args, "tags", None),
        needs_review=(kind == "note") if needs_review is None else bool(needs_review),
        archived=bool(getattr(args, "archived", False)), hidden=bool(getattr(args, "hidden", False)),
        by=getattr(args, "by", None) if kind == "task" else None,
        ws=ws,
    )



def capture_title(text: str) -> str:
    """Keep generated titles short and mark omitted text without summarizing it."""
    title = re.sub(r"\s+", " ", text).strip()
    if len(title) <= 80:
        return title
    prefix = title[:79]
    if title[79] != " " and " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip() + "…"



def title_from_note_body(text: str) -> str:
    meeting_headings = {"### Discussion", "### Decisions", "### Actions"}
    is_meeting = meeting_headings <= {line.strip() for line in text.splitlines()}
    for line in text.splitlines():
        value = line.strip()
        if not value or (is_meeting and value in meeting_headings):
            continue
        value = re.sub(r"^#{1,6}\s+", "", value)
        if value:
            return capture_title(value)
    return "Meeting notes" if is_meeting else "Untitled note"



def _finish_mutation(
    item: Card,
    *,
    updated: bool,
    summary: str,
    as_json: bool = False,
    receipt_fn=None,
    ws: Workspace | None = None,
) -> int | MutationReceipt:
    if as_json:
        auto_sync_after_mutation(changed=updated, ws=ws)
        receipt = receipt_fn(item, updated=updated) if receipt_fn is not None else saved_receipt(item, updated=updated, ws=ws)
        return MutationReceipt(receipt)
    print(summary)
    return auto_sync_after_mutation(changed=updated, ws=ws)



def _commit_item_write(
    resolved: Workspace,
    item: Card,
    updated: str,
    *,
    expected: bytes | None = None,
    failure_prefix: str,
    summary: str,
    receipt_fn=None,
    timer: dict | None = None,
) -> WriteResult:
    _require_mutation_lock()
    base = active_root(resolved)
    relative = str(item.path.relative_to(base))
    try:
        transaction.apply_item_transaction(
            {relative: updated.encode("utf-8")},
            expected={relative: expected if expected is not None else item.raw.encode("utf-8")},
            source_root=base,
            ws=resolved,
        )
    except (OSError, ValueError) as error:
        if "conflict:" in str(error).lower() or "changed before transaction" in str(error):
            raise ConflictError(f"{failure_prefix}: {error}") from error
        raise ValidationError(f"{failure_prefix}: {error}") from error
    if timer:
        cleanup_lifecycle_timer(item, timer, ws=resolved)
    saved = Card.read(item.path)
    receipt = receipt_fn(saved, updated=True) if receipt_fn is not None else saved_receipt(saved, updated=True, ws=resolved)
    return WriteResult(
        card=saved,
        receipt=receipt,
        message=summary,
        changed=True,
    )



def _commit_item_change(
    item: Card,
    updated: str,
    *,
    expected: bytes | None = None,
    failure_prefix: str,
    summary: str,
    as_json: bool = False,
    receipt_fn=None,
    timer: dict | None = None,
    ws: Workspace | None = None,
) -> int | MutationReceipt:
    resolved = _ws(ws)
    base = active_root(resolved)
    relative = str(item.path.relative_to(base))
    try:
        transaction.apply_item_transaction(
            {relative: updated.encode("utf-8")},
            expected={relative: expected if expected is not None else item.raw.encode("utf-8")},
            source_root=base,
            ws=resolved,
        )
    except (OSError, ValueError) as error:
        print(f"error: {failure_prefix}: {error}", file=sys.stderr)
        return 1
    if timer:
        cleanup_lifecycle_timer(item, timer, ws=resolved)
    return _finish_mutation(
        Card.read(item.path),
        updated=True,
        summary=summary,
        as_json=as_json,
        receipt_fn=receipt_fn,
        ws=resolved,
    )



def _complete_locked(
    resolved: Workspace,
    query: str,
    *,
    notes: list[str] | tuple[str, ...] = (),
    comments: list[str] | tuple[str, ...] = (),
    by: str | None = None,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock()
    query = query.strip()
    if not query:
        raise InputError("done requires a non-empty query")
    task = _resolve_unique_card(resolved, query, statuses=open_statuses(resolved), match_mode=match_mode)
    try:
        username = current_username(by, ws=resolved)
        check_claim(task, username)
    except ValueError as error:
        raise ConflictError(str(error), exit_code=1) from error
    completed_epoch = int(clock_time.time())
    completed_at = local_stamp(completed_epoch)
    completion_day = completed_at[:10]
    updated = card_format.replace_property(task.raw, "status", "done")
    updated = card_format.replace_property(updated, "updated", completed_at)
    updated = card_format.replace_property(updated, "completed", completed_at)
    updated = transition_claim(task, updated, "done", username, completed_at)
    try:
        updated, timer = finish_lifecycle_timer(task, updated, completed_epoch, ws=resolved)
        updated = append_completion_note(updated, list(notes), completion_day)
        updated = append_comments(updated, list(comments), completed_at)
    except ValueError as error:
        raise InputError(f"completion update failed: {error}", exit_code=1) from error
    return _commit_item_write(
        resolved,
        task,
        updated,
        failure_prefix="completion failed validation; change rolled back",
        summary=f"Completed and validated: {task.path.name}",
        timer=timer,
    )



def op_complete(
    ws: Workspace | None,
    query: str,
    *,
    notes: list[str] | tuple[str, ...] = (),
    comments: list[str] | tuple[str, ...] = (),
    by: str | None = None,
    match_mode: str = "exact",
    sync: bool = True,
) -> WriteResult:
    """Complete an open task and record an optional completion note or comment."""
    return _run_write_operation(
        ws,
        lambda resolved: _complete_locked(
            resolved, query, notes=notes, comments=comments, by=by, match_mode=match_mode
        ),
        sync=sync,
    )



def _move_locked(
    resolved: Workspace,
    query: str,
    destination: str,
    *,
    comments: list[str] | tuple[str, ...] = (),
    takeover: bool = False,
    by: str | None = None,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock()
    query = query.strip()
    if not query:
        raise InputError("move requires a non-empty query")
    allowed_statuses = open_statuses(resolved)
    if destination not in allowed_statuses:
        raise InputError(f"move destination must be one of: {', '.join(allowed_statuses)}")
    if takeover and destination != "doing":
        raise InputError("--takeover requires destination doing")
    task = _resolve_unique_card(resolved, query, statuses=allowed_statuses, match_mode=match_mode)
    try:
        username = current_username(by, ws=resolved)
        check_claim(task, username, takeover=takeover)
    except ValueError as error:
        raise ConflictError(str(error), exit_code=1) from error
    source_status = task.frontmatter["status"]
    if source_status == destination and (
        destination != "doing" or task.frontmatter.get("claimed_by") == username
    ):
        return WriteResult(
            card=task,
            receipt=saved_receipt(task, ws=resolved),
            message=f"Already {destination}: {task.path.name}",
            changed=False,
        )
    moved_epoch = int(clock_time.time())
    moved_at = local_stamp(moved_epoch)
    updated = card_format.replace_property(task.raw, "status", destination)
    updated = card_format.replace_property(updated, "updated", moved_at)
    updated = transition_claim(task, updated, destination, username, moved_at)
    try:
        updated = append_comments(updated, list(comments), moved_at)
        timer = None
        if destination != "doing" or task.frontmatter.get("claimed_by") != username:
            updated, timer = finish_lifecycle_timer(task, updated, moved_epoch, ws=resolved)
    except ValueError as error:
        raise InputError(f"move update failed: {error}", exit_code=1) from error
    return _commit_item_write(
        resolved,
        task,
        updated,
        failure_prefix="move failed validation; change rolled back",
        summary=f"Moved to {destination} and validated: {task.path.name}",
        timer=timer,
    )



def op_move(
    ws: Workspace | None,
    query: str,
    destination: str,
    *,
    comments: list[str] | tuple[str, ...] = (),
    takeover: bool = False,
    by: str | None = None,
    match_mode: str = "exact",
    sync: bool = True,
) -> WriteResult:
    """Move an open task to another open status."""
    return _run_write_operation(
        ws,
        lambda resolved: _move_locked(
            resolved,
            query,
            destination,
            comments=comments,
            takeover=takeover,
            by=by,
            match_mode=match_mode,
        ),
        sync=sync,
    )



def _comment_locked(
    resolved: Workspace,
    query: str,
    comments: list[str] | tuple[str, ...],
    *,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock()
    query = query.strip()
    if not query:
        raise InputError("comment requires a non-empty query")
    item = _resolve_unique_card(resolved, query, match_mode=match_mode)
    if not clean_text_values(list(comments)):
        raise InputError("comment requires at least one non-empty message")
    commented_at = now_stamp()
    try:
        updated = card_format.replace_property(item.raw, "updated", commented_at)
        updated = append_comments(updated, list(comments), commented_at)
    except ValueError as error:
        raise InputError(f"comment failed: {error}", exit_code=1) from error
    return _commit_item_write(
        resolved,
        item,
        updated,
        expected=item.path.read_bytes(),
        failure_prefix="comment failed validation; change rolled back",
        summary=f"Commented and validated: {item.path.relative_to(resolved.root)}",
    )



def op_comment(
    ws: Workspace | None,
    query: str,
    comments: list[str] | tuple[str, ...],
    *,
    match_mode: str = "exact",
    sync: bool = True,
) -> WriteResult:
    """Append one or more timestamped comments to a task or note."""
    return _run_write_operation(
        ws,
        lambda resolved: _comment_locked(resolved, query, comments, match_mode=match_mode),
        sync=sync,
    )



def apply_exact_body_replacements(body: str, raw_json: str) -> str:
    try:
        replacements = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"--replace-file is not valid JSON ({error.msg}; line {error.lineno}, column {error.colno}). "
            "Pass the JSON through a quoted heredoc (<<'EOF') so the shell does not alter backslashes."
        ) from error
    expected = 'a JSON array of {"old": ..., "new": ...} objects'
    if not isinstance(replacements, list):
        raise ValueError(f"--replace-file must contain {expected}; got {type(replacements).__name__}")
    for index, pair in enumerate(replacements, 1):
        if not isinstance(pair, dict) or set(pair) != {"old", "new"}:
            raise ValueError(f"--replace-file entry {index} must be an object with exactly old and new; expected {expected}")
        if not isinstance(pair["old"], str) or not pair["old"]:
            raise ValueError(f"--replace-file entry {index}: old must be non-empty text")
        if not isinstance(pair["new"], str):
            raise ValueError(f"--replace-file entry {index}: new must be text")
    return apply_body_replacements(body, [(pair["old"], pair["new"]) for pair in replacements])



def apply_body_replacements(body: str, pairs: list[tuple[str, str]]) -> str:
    """Replace each old passage that occurs exactly once in body; all or nothing."""
    matches: list[tuple[int, int, str]] = []
    for index, (old, new) in enumerate(pairs, 1):
        if not old:
            raise ValueError(f"replacement {index} old text must be non-empty")
        positions: list[int] = []
        cursor = 0
        while (position := body.find(old, cursor)) >= 0:
            positions.append(position)
            cursor = position + 1
        if len(positions) != 1:
            raise ValueError(
                f"replacement {index} old text {abbreviate(old)} must match exactly once in the "
                f"original body (found {len(positions)} matches)"
            )
        start = positions[0]
        matches.append((start, start + len(old), new))
    ordered = sorted(matches)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError("exact body replacements overlap")
    result = body
    for start, end, new in reversed(ordered):
        result = result[:start] + new + result[end:]
    return result



def _apply_body_edit(
    body: str,
    *,
    body_mode: str | None,
    body_text: str,
    body_replacements: list[tuple[str, str]] | None,
    exact_replacements_text: str | None,
) -> str:
    """Apply the requested body edit mode to a card body string."""
    if body_mode == "replace_pairs" and body_replacements is not None:
        return apply_body_replacements(body, body_replacements)
    if body_mode == "replace_file" and exact_replacements_text is not None:
        return apply_exact_body_replacements(body, exact_replacements_text)
    if body_mode == "append":
        return body + blank_line_separator(body) + body_text if body_text else body
    if body_mode == "replace":
        return body_text
    return body


def _build_edit_metadata(
    resolved: Workspace,
    kind: str,
    current_properties: dict[str, object],
    *,
    metadata_fields: dict[str, object] | None,
    assignments: list[str] | tuple[str, ...],
    tag_add: list[str] | tuple[str, ...] | None,
    tag_remove: list[str] | tuple[str, ...] | None,
) -> dict[str, object]:
    """Parse --set assignments, merge tag add/remove updates, and validate status edits."""
    metadata: dict[str, object] = dict(metadata_fields or {})
    for assignment in assignments or ():
        field, separator, value = assignment.partition("=")
        if not separator or not field:
            raise ValueError("--set requires FIELD=VALUE")
        allowed = card_format.EDITABLE_FIELDS[kind]
        if field not in allowed:
            raise ValueError(f"field '{field}' cannot be set on a {kind}; choose from {', '.join(sorted(allowed))}")
        if field in metadata:
            raise ValueError(f"field '{field}' was assigned more than once")
        if field == "tags":
            metadata[field] = parse_tag_json(value)
        elif field in card_format.BOOLEAN_METADATA:
            if value not in {"true", "false"}:
                raise ValueError(f"{field} must be true or false")
            metadata[field] = value == "true"
        elif field in {"next_action", "processing_instructions"}:
            metadata[field] = None if value == "null" or value == "" else value
        elif field == "title":
            if not value.strip() or any(char in value for char in "\r\n\x00"):
                raise ValueError("title must be non-empty and single-line")
            metadata[field] = value.strip()
        else:
            metadata[field] = None if value == "null" else value
    if tag_add or tag_remove:
        if "tags" in metadata:
            raise ValueError("use one of --tags/--set tags or --tag/--remove-tag")
        if any(not tag.strip() for tag in list(tag_add or []) + list(tag_remove or [])):
            raise ValueError("tags must be non-empty")
        current_tags = list(current_properties.get("tags") or [])
        remove = {tag.strip().casefold() for tag in (tag_remove or [])}
        current_tags = [tag for tag in current_tags if str(tag).strip().casefold() not in remove]
        existing = {str(tag).strip().casefold() for tag in current_tags}
        for tag in tag_add or []:
            tag = tag.strip()
            if tag.casefold() not in existing:
                current_tags.append(tag)
                existing.add(tag.casefold())
        metadata["tags"] = current_tags
    if "status" in metadata:
        if kind != "task":
            raise ValueError("field 'status' cannot be set on a note")
        new_status = metadata["status"]
        if not isinstance(new_status, str) or not new_status.strip():
            raise ValueError("status must be non-empty")
        new_status = new_status.strip()
        if new_status == "doing":
            raise ValueError("status 'doing' requires claiming; use mt move doing or mt claim")
        if new_status == "done":
            raise ValueError("status 'done' requires completing; use mt done")
        allowed_open = open_statuses(resolved)
        if new_status not in allowed_open:
            raise ValueError(
                f"status must be one of: {', '.join(status for status in allowed_open if status != 'doing')}"
            )
        metadata["status"] = new_status
    return metadata


def _edit_locked(
    resolved: Workspace,
    query: str,
    *,
    match_mode: str = "exact",
    expected_hash: str | None = None,
    body_mode: str | None = None,
    body_text: str = "",
    body_replacements: list[tuple[str, str]] | None = None,
    exact_replacements_text: str | None = None,
    metadata_fields: dict[str, object] | None = None,
    assignments: list[str] | tuple[str, ...] = (),
    tag_add: list[str] | tuple[str, ...] | None = None,
    tag_remove: list[str] | tuple[str, ...] | None = None,
    by: str | None = None,
) -> WriteResult:
    """Apply metadata and/or body edits to one card while holding the mutation lock."""
    _require_mutation_lock()
    item = _resolve_unique_card(resolved, query.strip(), match_mode=match_mode)
    kind = item.kind
    guard_args = argparse.Namespace(expected_hash=expected_hash)
    try:
        original = item.path.read_bytes()
        verify_edit_guards(item, guard_args, original)
        if original != item.raw.encode("utf-8"):
            raise ConflictError("conflict: item changed since it was loaded (content changed)")
        initial_properties, initial_body = dict(item.frontmatter), item.body
        body = _apply_body_edit(
            initial_body,
            body_mode=body_mode,
            body_text=body_text,
            body_replacements=body_replacements,
            exact_replacements_text=exact_replacements_text,
        )
        metadata = _build_edit_metadata(
            resolved,
            kind,
            initial_properties,
            metadata_fields=metadata_fields,
            assignments=assignments,
            tag_add=tag_add,
            tag_remove=tag_remove,
        )
        properties = card_format.apply_property_patch(dict(initial_properties), metadata, kind=kind)

        # A claim blocks conflicting work (lifecycle, archiving, body rewrites),
        # not additions: appends, comments, and other property edits are open.
        rewrites_body = bool(body_mode and body_mode != "append")
        if item.is_task and item.frontmatter.get("claimed_by") and (
                rewrites_body or "status" in metadata or "archived" in metadata or "hidden" in metadata):
            try:
                check_claim(item, current_username(by, ws=resolved))
            except ValueError as error:
                raise ConflictError(f"invalid edit: {error}", exit_code=2) from error

        if properties == initial_properties and body == initial_body:
            return WriteResult(
                card=item,
                receipt=saved_receipt(item, updated=False, ws=resolved),
                message=f"No changes: {item.path.relative_to(resolved.root)}",
                changed=False,
            )

        timestamp = now_stamp()
        properties["updated"] = timestamp
        raw = card_format.render_document(properties, body, expected_kind=kind)
        if item.is_task and properties.get("status") != item.frontmatter.get("status"):
            raw = transition_claim(item, raw, str(properties["status"]), current_username(by, ws=resolved), timestamp)
            properties, body = card_format.parse_document(raw, expected_kind=kind)
            properties["updated"] = timestamp
            raw = card_format.render_document(properties, body, expected_kind=kind)
        timer = None
        if item.is_task and (properties.get("status") != "doing"
                             or properties.get("archived") is True or properties.get("hidden") is True):
            raw, timer = finish_lifecycle_timer(item, raw, int(clock_time.time()), ws=resolved)
        edited_item = save_item_edit(item, raw, guard_args, ws=resolved)
        cleanup_lifecycle_timer(item, timer, ws=resolved)
    except TaskError:
        raise
    except (OSError, ValueError) as error:
        if "conflict:" in str(error):
            raise ConflictError(str(error), exit_code=1) from error
        raise InputError(f"invalid edit: {error}", exit_code=2) from error
    return WriteResult(
        card=edited_item,
        receipt=saved_receipt(edited_item, updated=True, ws=resolved),
        message=f"Edited and validated: {edited_item.path.relative_to(resolved.root)}",
        changed=True,
    )



def op_edit(
    ws: Workspace | None,
    query: str,
    *,
    match_mode: str = "exact",
    expected_hash: str | None = None,
    body_mode: str | None = None,
    body_text: str = "",
    body_replacements: list[tuple[str, str]] | None = None,
    exact_replacements_text: str | None = None,
    metadata_fields: dict[str, object] | None = None,
    assignments: list[str] | tuple[str, ...] = (),
    tag_add: list[str] | tuple[str, ...] | None = None,
    tag_remove: list[str] | tuple[str, ...] | None = None,
    by: str | None = None,
    sync: bool = True,
) -> WriteResult:
    """Edit card metadata and/or body text."""
    return _run_write_operation(
        ws,
        lambda resolved: _edit_locked(
            resolved,
            query,
            match_mode=match_mode,
            expected_hash=expected_hash,
            body_mode=body_mode,
            body_text=body_text,
            body_replacements=body_replacements,
            exact_replacements_text=exact_replacements_text,
            metadata_fields=metadata_fields,
            assignments=assignments,
            tag_add=tag_add,
            tag_remove=tag_remove,
            by=by,
        ),
        sync=sync,
    )



def verify_edit_guards(item: Card, args: argparse.Namespace, original: bytes) -> None:
    expected_hash = getattr(args, "expected_hash", None)
    if expected_hash is not None and hashlib.sha256(original).hexdigest().casefold() != expected_hash.casefold():
        raise ValueError("conflict: item changed since it was loaded (content hash mismatch)")



def save_item_edit(item: Card, raw: str, args: argparse.Namespace, ws: Workspace | None = None) -> Card:
    resolved = _ws(ws)
    base = active_root(resolved)
    relative = str(item.path.relative_to(base))
    original = item.path.read_bytes()
    verify_edit_guards(item, args, original)
    if original != item.raw.encode("utf-8"):
        raise ValueError("conflict: item changed since it was loaded (content changed)")
    transaction.apply_item_transaction({relative: raw.encode("utf-8")}, expected={relative: original}, source_root=base, ws=resolved)
    return Card.read(item.path)



def _bulk_edit_assignments(values: list[str] | dict[str, object]) -> dict[str, str]:
    """Parse and validate the metadata assignments shared by bulk edit plans."""
    assignments: dict[str, str] = {}
    entries = values.items() if isinstance(values, dict) else (
        (entry.partition("=")[0], entry.partition("=")[2]) for entry in values
    )
    for field, value in entries:
        if not isinstance(field, str) or not field or not isinstance(value, str) or not value:
            raise ValueError("--set requires non-empty FIELD=VALUE assignments")
        if field in assignments:
            raise ValueError(f"field '{field}' was assigned more than once")
        available = card_format.EDITABLE_FIELDS["task"] | card_format.EDITABLE_FIELDS["note"]
        if field not in available:
            raise ValueError(
                f"field '{field}' cannot be set; choose from "
                f"{', '.join(sorted(available))}"
            )
        if field in card_format.BOOLEAN_METADATA and value not in {"true", "false"}:
            raise ValueError(f"{field} must be true or false")
        assignments[field] = value
    if not assignments:
        raise ValueError("bulk-edit requires at least one --set FIELD=VALUE")
    return assignments



def _apply_bulk_metadata(raw: str, kind: str, assignments: dict[str, str], updated: str) -> str:
    frontmatter, body = card_format.parse_document(raw, expected_kind=kind)
    allowed = card_format.EDITABLE_FIELDS[kind]
    targets: dict[str, object] = {}
    for field, value in assignments.items():
        if field not in allowed:
            raise ValueError(f"field '{field}' cannot be set on a {kind}")
        if field == "status":
            if value == "doing":
                raise ValueError("status 'doing' requires claiming; use 'mt move doing' or 'mt claim'")
            if value == "done":
                raise ValueError("status 'done' requires completing; use 'mt done'")
            if value not in open_statuses():
                valid = [s for s in open_statuses() if s != "doing"]
                raise ValueError(f"status must be one of: {', '.join(valid)}")
            target = value
        elif field == "tags":
            target = parse_tag_json(value)
        elif field in card_format.BOOLEAN_METADATA:
            if value not in {"true", "false"}:
                raise ValueError(f"{field} must be true or false")
            target = value == "true"
        else:
            target = None if value == "null" else value
        targets[field] = target
    patched = card_format.apply_property_patch(frontmatter, targets, kind=kind)
    if patched == frontmatter:
        return raw
    if kind == "task" and patched.get("status") != frontmatter.get("status"):
        for field in CLAIM_FIELDS:
            patched.pop(field, None)
    patched["updated"] = updated
    return card_format.render_document(patched, body, expected_kind=kind)



def _plan_query(plan: dict[str, object]) -> card_query.Query:
    """The query a saved plan holds, checked like one typed on the command line."""
    stored = plan["query"]
    if not isinstance(stored, dict):
        raise ValueError("bulk edit plan has an invalid query")
    try:
        query = card_query.query_from_mapping(stored)
        card_query.validate(query, statuses=store.statuses(), priorities=list(store.policy().get("priorities", {})))
    except card_query.QueryError as error:
        raise ValueError(f"bulk edit plan has an invalid query: {error}") from error
    return query



def _bulk_plan_item(item: Card, base: Path, assignments: dict[str, str], updated: str) -> tuple[dict[str, object], bytes | None]:
    kind = item.kind
    if item.path.is_symlink():
        raise ValueError(f"cannot edit a symlinked card: {item.path}")
    original = item.path.read_bytes()
    if original != item.raw.encode("utf-8"):
        raise ValueError(f"conflict: item changed during bulk preview: {item.path}")
    before = {field: item.frontmatter.get(field) for field in assignments}
    candidate = _apply_bulk_metadata(item.raw, kind, assignments, updated)
    after_frontmatter = card_format.parse_yaml(card_format.parse_frontmatter(candidate)[0] or "")
    after = {field: after_frontmatter.get(field) for field in assignments}
    changed = candidate.encode("utf-8") != original
    record: dict[str, object] = {
        "kind": kind,
        "path": item.path.relative_to(base).as_posix(),
        "ref": f"@{item.short_ref}",
        "title": item.title,
        "content_hash": hashlib.sha256(original).hexdigest(),
        "changed": changed,
        "before": before,
        "after": after,
    }
    return record, candidate.encode("utf-8") if changed else None



def _time_start_locked(
    resolved: Workspace,
    query: str,
    *,
    parallel: bool = False,
    by: str | None = None,
) -> WriteResult:
    _require_mutation_lock()
    query = query.strip()
    if not query:
        raise InputError("time start requires a task")
    task = _resolve_unique_card(resolved, query, statuses=open_statuses(resolved))
    try:
        username = current_username(by, ws=resolved)
        check_claim(task, username)
        if is_archived(task) or is_hidden(task):
            raise ValueError("unarchive and unhide the task before starting a timer")
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        timers = read_time_timers(resolved)
        if task_id in timers:
            raise ValueError(f"timer already running for [@{task.short_ref}] {task.title}")
        started_epoch = int(clock_time.time())
        timestamp = local_stamp(started_epoch)
        updated = task.raw
        if task.frontmatter.get("status") != "doing" or task.frontmatter.get("claimed_by") != username:
            updated = card_format.replace_property(updated, "status", "doing")
            updated = card_format.replace_property(updated, "updated", timestamp)
            updated = transition_claim(task, updated, "doing", username, timestamp)
        base = active_root(resolved)
        relative = str(task.path.relative_to(base))
        changes = {relative: updated.encode("utf-8")}
        expected = {relative: task.raw.encode("utf-8")}
        if not parallel:
            for other_id, other_timer in list(timers.items()):
                if other_timer["by"] != username:
                    continue
                other = task_by_id(other_id, ws=resolved)
                if other is None:
                    raise ValueError(f"running timer task no longer exists: {other_id}; use mt time cancel {other_id}")
                entry, already_saved = time_session_for(other_id, other_timer, started_epoch, ws=resolved)
                if not already_saved:
                    other_raw = append_time_entry(other.raw, entry)
                    other_raw = card_format.replace_property(other_raw, "updated", timestamp)
                    other_path = str(other.path.relative_to(base))
                    changes[other_path] = other_raw.encode("utf-8")
                    expected[other_path] = other.raw.encode("utf-8")
                del timers[other_id]
        timer = {"session_id": uuid.uuid4().hex, "start": timestamp,
                 "start_epoch": started_epoch, "by": username}
        transaction.apply_item_transaction(changes, expected=expected, source_root=base, ws=resolved)
        try:
            timers[task_id] = timer
            transaction.write_time_timers(timers, ws=resolved)
        except OSError:
            transaction.apply_item_transaction(expected, expected=changes, source_root=base, ws=resolved)
            raise
    except (OSError, ValueError) as error:
        raise ConflictError(f"could not start timer: {error}", exit_code=1) from error
    saved = Card.read(task.path)
    changed = any(content != expected[path] for path, content in changes.items())
    return WriteResult(
        card=saved,
        receipt=saved_receipt(saved, updated=changed, ws=resolved),
        message=f"Started timer for [@{task.short_ref}] {task.title} at {timer['start']} ({username}).",
        changed=changed,
    )



def op_time_start(
    ws: Workspace | None,
    query: str,
    *,
    parallel: bool = False,
    by: str | None = None,
    sync: bool = True,
) -> WriteResult:
    """Start a timer on an open task (claiming and moving it to doing if needed)."""
    return _run_write_operation(
        ws,
        lambda resolved: _time_start_locked(resolved, query, parallel=parallel, by=by),
        sync=sync,
    )



def _time_end_locked(
    resolved: Workspace,
    *,
    query: str = "",
    action: str = "stop",
    at: str | None = None,
    duration: str | None = None,
    by: str | None = None,
) -> WriteResult:
    _require_mutation_lock()
    try:
        timers = read_time_timers(resolved)
    except ValueError as error:
        raise InputError(str(error), exit_code=1) from error
    if not timers:
        raise NotFoundError("no timer is running", exit_code=1, prefixed=True)
    query = query.strip()
    if query in timers:
        task_id, timer = query, timers[query]
    elif query:
        task = _resolve_unique_card(resolved, query, kind="task")
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        timer = timers.get(task_id)
        if timer is None:
            raise NotFoundError(f"no timer is running for [@{task.short_ref}] {task.title}", exit_code=1, prefixed=True)
    elif len(owned := {key: value for key, value in timers.items()
                       if value["by"] == current_username(by, ws=resolved)}) == 1:
        task_id, timer = next(iter(owned.items()))
    elif not owned:
        raise NotFoundError("no timer is running for this user; specify a task", exit_code=1, prefixed=True)
    else:
        refs = []
        for running_task_id in sorted(owned):
            running_task = task_by_id(running_task_id, ws=resolved)
            refs.append(f"@{running_task.short_ref}" if running_task else running_task_id)
        raise ConflictError(f"multiple timers are running; specify a task: {', '.join(refs)}", exit_code=1)
    task = task_by_id(task_id, ws=resolved)
    if timer["by"] != current_username(by, ws=resolved):
        raise ConflictError(f"timer belongs to {timer['by']}; use --by {shlex.quote(str(timer['by']))}", exit_code=1)
    if task is None and action != "cancel":
        raise NotFoundError(
            f"running timer task no longer exists: {task_id}; use mt time cancel {task_id}",
            exit_code=1,
            prefixed=True,
        )
    if action == "cancel":
        try:
            transaction.clear_time_timer(task_id, str(timer["session_id"]), ws=resolved)
        except OSError as error:
            raise InputError(f"could not cancel timer: {error}", exit_code=1) from error
        label = f"[@{task.short_ref}] {task.title}" if task else task_id
        return WriteResult(
            card=task,
            receipt=saved_receipt(task, updated=False, ws=resolved) if task else {"cancelled": True, "task_id": task_id},
            message=f"Cancelled timer for {label}; no time recorded.",
            changed=False,
        )
    assert task is not None
    try:
        end_epoch = int(clock_time.time())
        if at:
            end_epoch = parse_time_argument(at)
        elif duration:
            end_epoch = int(timer["start_epoch"]) + parse_duration(duration)
        if end_epoch < int(timer["start_epoch"]) or end_epoch > int(clock_time.time()):
            raise ValueError("timer end must be between its start and now")
    except ValueError as error:
        raise InputError(str(error), exit_code=2) from error
    elapsed = max(0, end_epoch - int(timer["start_epoch"]))
    saved, message = save_time_session(task, timer, end_epoch, ws=resolved)
    if not saved:
        raise ValidationError(message, exit_code=1)
    try:
        if not transaction.clear_time_timer(task_id, str(timer["session_id"]), ws=resolved):
            raise ConflictError("timer state changed after saving; retry stop safely", exit_code=1, sync_needed=True)
    except OSError as error:
        raise ConflictError(
            f"session saved, but timer cleanup failed ({error}); retry stop safely",
            exit_code=1,
            sync_needed=True,
        ) from error
    updated_task = Card.read(task.path)
    summary = (
        "Timer session was already saved; cleared the running state."
        if message == "already saved"
        else f"Stopped timer for [@{task.short_ref}] {task.title}: {format_duration(elapsed)}."
    )
    return WriteResult(
        card=updated_task,
        receipt=saved_receipt(updated_task, updated=(message != "already saved"), ws=resolved),
        message=summary,
        changed=True,
    )



def op_time_end(
    ws: Workspace | None,
    *,
    query: str = "",
    action: str = "stop",
    at: str | None = None,
    duration: str | None = None,
    by: str | None = None,
    sync: bool = True,
) -> WriteResult:
    """Stop or cancel a running timer."""
    return _run_write_operation(
        ws,
        lambda resolved: _time_end_locked(
            resolved, query=query, action=action, at=at, duration=duration, by=by
        ),
        sync=sync,
    )



def workspace_topic_counts(cards: list[Card]) -> dict[str, int]:
    """Cards per exact topic path, counting every card including hidden and archived."""
    counts: dict[str, int] = {}
    for card in cards:
        topic = card.frontmatter.get("topic")
        if isinstance(topic, str) and topic:
            counts[topic] = counts.get(topic, 0) + 1
    return counts



def suggested_topics(cfg: dict) -> list[str]:
    values = cfg.get("suggested_topics")
    return [card_format.normalize_topic(value) for value in values
            if isinstance(value, str) and card_format.topic_path_error(value) is None] if isinstance(values, list) else []



def workspace_topic_tree(cards: list[Card], cfg: dict) -> list[dict[str, object]]:
    return card_format.topic_tree(workspace_topic_counts(cards), suggested_topics(cfg))



def topic_query(path: str) -> card_query.Query:
    """Every card at or under a topic, whether archived or hidden."""
    return card_query.Query(topic=(path,), archived="include", hidden="include")



def topic_rename_plan(old: str, new: str, merge: bool, ws: Workspace | None = None) -> list[tuple[Card, str]]:
    """Cards to move and their new topics, or ValueError when the rename is not allowed."""
    resolved = _ws(ws)
    for label, value in (("OLD", old), ("NEW", new)):
        problem = card_format.topic_path_error(value)
        if problem:
            raise CardInputError(f"{label} topic {problem}")
    old, new = card_format.normalize_topic(old), card_format.normalize_topic(new)
    old_key, new_key = card_format.topic_key(old), card_format.topic_key(new)
    if new == old:
        raise CardInputError("OLD and NEW are the same topic")
    if len(new_key) > len(old_key) and new_key[:len(old_key)] == old_key:
        raise CardInputError(f"cannot move topic '{old}' into its own subtree '{new}'")
    cards = discover(ws=resolved)
    affected = select_cards(cards, topic_query(old))
    if not affected:
        raise CardInputError(f"no cards use topic '{old}' or anything under it")
    if new_key != old_key and not merge:
        moving = {card.path for card in affected}
        existing = select_cards([card for card in cards if card.path not in moving], topic_query(new))
        if existing or any(card_format.topic_key(item) == new_key for item in suggested_topics(store.policy(resolved))):
            raise CardInputError(f"topic '{new}' already exists; use --merge to combine into it")
    plan = []
    for card in affected:
        parts = str(card.frontmatter["topic"]).split(card_format.TOPIC_SEPARATOR)
        rest = parts[len(old_key):]
        plan.append((card, card_format.TOPIC_SEPARATOR.join([new, *rest])))
    return plan



def _save_new_draft(resolved: Workspace, draft: dict[str, object]) -> tuple[Card, bool]:
    """Capture a new card from a 'new'-mode draft."""
    body = str(draft.get("body") or "")
    capture_args = argparse.Namespace(
        text=[body],
        title=str(draft.get("title") or "").strip() or None,
        capture_id=str(draft.get("capture_id") or ""),
        created_at=str(draft.get("created") or ""),
        instructions_text=str(draft.get("instructions") or ""),
    )
    for field in ("priority", "project", "topic", "due", "due_kind"):
        setattr(capture_args, field, draft.get(field))
    capture_args.tags = draft.get("tags") or []
    if draft["kind"] == "task":
        capture_args.status = draft.get("status")
        capture_args.task_type = draft.get("type")
    return capture_card(capture_args, body, draft["kind"], ws=resolved)


def _save_edit_draft(resolved: Workspace, draft: dict[str, object]) -> tuple[Card, bool]:
    """Verify baseline guards and apply an 'edit'-mode draft to its target card."""
    target = str(draft.get("target") or "")
    if not re.fullmatch(r"(?:\.mt/demo/)?cards/[^/]+\.md", target):
        raise ValueError("draft target path is invalid")
    target_path = resolved.root / target
    if target_path.is_symlink() or not target_path.is_file():
        raise draft_store.DraftConflict("draft target is missing; draft was kept")
    original = target_path.read_bytes()
    if draft.get("base_hash") == draft_store.UNVERIFIED_BASE_HASH:
        raise draft_store.DraftConflict(
            "draft has no verified target baseline; compare the saved text and create a fresh edit draft before applying"
        )
    if hashlib.sha256(original).hexdigest() != draft.get("base_hash"):
        raise draft_store.DraftConflict("target changed since the draft was created; draft was kept")
    current = Card.read(target_path)
    if draft.get("origin") != "card-body":
        raise draft_store.DraftConflict(
            "edit draft uses a retired card format; create a fresh edit draft before saving"
        )
    if target_path.read_bytes() != original:
        raise draft_store.DraftConflict("target changed while saving; draft was kept")
    if (draft["kind"] == "note") != current.is_note:
        raise ValueError("draft kind does not match target kind")
    if current.is_task and current.frontmatter.get("claimed_by"):
        check_claim(current, current_username(None, ws=resolved))
    properties, current_body = dict(current.frontmatter), current.body
    body = str(draft.get("body") or "")
    title = str(draft.get("title") or "").strip()
    if title:
        properties = card_format.apply_property_patch(
            properties, {"title": title}, kind=str(draft["kind"])
        )
    changed = body != current_body or properties != current.frontmatter
    if not changed:
        return current, False
    properties["updated"] = now_stamp()
    raw = card_format.render_document(properties, body, expected_kind=str(draft["kind"]))
    if target.startswith(".mt/demo/"):
        if not demo_active(resolved):
            raise draft_store.DraftConflict("demo target is no longer active; draft was kept")
        base = demo_root(resolved)
        relative = target[len(".mt/demo/"):]
    else:
        base = resolved.root
        relative = target
    transaction.apply_item_transaction(
        {relative: raw.encode("utf-8")}, expected={relative: original}, source_root=base, ws=resolved
    )
    return Card.read(target_path), True


def _draft_save_locked(
    resolved: Workspace,
    draft_id: str,
    *,
    expected_revision: int,
    expected_hash: str | None = None,
) -> tuple[WriteResult, bool]:
    _require_mutation_lock()
    path = draft_store.draft_path(resolved.root, draft_id)
    item: Card | None = None
    changed = False
    draft_removed = False
    try:
        lock_handle = draft_store.lock(resolved.root)
        try:
            if not path.is_file():
                raise draft_store.DraftConflict("draft was removed outside microtask; local copy was kept")
            draft = draft_store.read(resolved.root, draft_id)
            if draft["revision"] != expected_revision:
                raise draft_store.DraftConflict("draft changed since it was loaded; refresh and retry")
            if expected_hash is not None and draft["content_hash"] != expected_hash:
                raise draft_store.DraftConflict("draft content changed outside microtask; refresh and reconcile")
            if draft["mode"] == "new":
                item, changed = _save_new_draft(resolved, draft)
                conflict_label = "card saved"
            else:
                item, changed = _save_edit_draft(resolved, draft)
                conflict_label = "changes applied"
            if draft_store.read(resolved.root, draft_id)["content_hash"] != draft["content_hash"]:
                raise draft_store.DraftConflict(
                    f"draft content changed outside microtask; {conflict_label} and draft retained"
                )
            path.unlink()
            draft_removed = True
            if item is None:
                raise ValueError("draft save did not create or update a card")
        finally:
            draft_store.unlock(lock_handle)
    except draft_store.DraftConflict as error:
        raise ConflictError(str(error), exit_code=1) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise InputError(str(error), exit_code=1) from error
    receipt = item_receipt(item, updated=changed, ws=resolved)
    result = WriteResult(
        card=item,
        receipt=receipt,
        message=f"Applied draft to {receipt['ref']}: {receipt['path']}",
        changed=changed,
    )
    return result, draft_removed



def op_draft_save(
    ws: Workspace | None,
    draft_id: str,
    *,
    expected_revision: int,
    expected_hash: str | None = None,
    sync: bool = True,
) -> WriteResult:
    """Apply a draft (new or edit) to the workspace and remove the draft file."""
    resolved = _ws(ws)
    # Keep the global mutation lock order aligned with migration and card
    # writes: time/mutation first, drafts second. Auto-sync runs below after
    # both locks have been released.
    with time_mutation_lock(resolved):
        result, draft_removed = _draft_save_locked(
            resolved,
            draft_id,
            expected_revision=expected_revision,
            expected_hash=expected_hash,
        )
    if sync and (result.changed or draft_removed):
        sync_error = transaction.run_auto_sync_for_receipt(resolved)
        if sync_error:
            return replace(result, sync_error=sync_error)
    return result


def op_card_metadata(
    ws: Workspace | None,
    query: str,
    patch: dict[str, object],
    *,
    expected_hash: str,
    kind: str | None = None,
    match_mode: str = "exact",
    sync: bool = True,
) -> WriteResult:
    """Apply a card metadata patch with a whole-card compare-and-swap."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash or ""):
        raise InputError("expected hash must be a 64-character SHA-256 digest", exit_code=2)
    if not isinstance(patch, dict) or not patch:
        raise InputError("metadata patch contains unsupported or missing fields", exit_code=2)

    def _run(resolved: Workspace) -> WriteResult:
        item = _resolve_unique_card(resolved, query, match_mode=match_mode)
        item_kind = item.kind
        if kind is not None and item_kind != kind:
            raise InputError(f"{kind} metadata can only be applied to a {kind}")
        # Status and visibility flags have their own guarded commands.
        allowed_fields = card_format.EDITABLE_FIELDS[item_kind] - {"status", *card_format.BOOLEAN_METADATA}
        if set(patch) - allowed_fields:
            raise InputError("metadata patch contains unsupported or missing fields", exit_code=2)
        original = item.path.read_bytes()
        actual_hash = hashlib.sha256(original).hexdigest()
        if actual_hash.casefold() != expected_hash.casefold():
            raise ConflictError("conflict: card changed since it was loaded (content hash mismatch)")
        properties, body = dict(item.frontmatter), item.body
        try:
            patched = card_format.apply_property_patch(properties, patch, kind=item_kind)
        except ValueError as error:
            raise InputError(str(error)) from error
        if patched == properties:
            receipt = store.item_receipt(item, ws=resolved)
            return WriteResult(
                card=item,
                receipt=receipt,
                message=f"Updated {item_kind} properties: {receipt['ref']}",
                changed=False,
            )
        patched["updated"] = store.now_stamp()
        raw = card_format.render_document(patched, body, expected_kind=item_kind)
        return _commit_item_write(
            resolved,
            item,
            raw,
            expected=original,
            failure_prefix="edit failed",
            summary=f"Updated {item_kind} properties: @{item.short_ref}",
            receipt_fn=lambda saved, updated=True: store.item_receipt(saved, updated=updated, ws=resolved),
        )

    return _run_write_operation(ws, _run, sync=sync)

