"""Core write operations for microtask cards, timers, focus, and drafts."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re
import shlex
import time as clock_time
import uuid

import attachments
import card_format
import card_query
import card_render
from card_render import (  # noqa: F401 - re-exported for callers
    _apply_body_edit,
    _apply_bulk_metadata,
    _build_edit_metadata,
    _bulk_edit_assignments,
    _bulk_plan_item,
    _plan_query,
    append_comments,
    apply_body_replacements,
    apply_exact_body_replacements,
    blank_line_separator,
    capture_title,
    card_capture_fields,
    check_claim,
    clean_text_values,
    parse_tag_json,
    render_converted_note,
    render_converted_task,
    render_item_document,
    render_new_card,
    suggested_topics,
    task_defaults,
    title_from_note_body,
    topic_query,
    topic_rename_plan,
    transition_claim,
    unassigned_priority,
    unique_item_timestamp,
    validate_shared_metadata,
    workspace_topic_counts,
    workspace_topic_tree,
)
import drafts as draft_store
import focus
from focus import (  # noqa: F401 - re-exported for callers
    focus_context,
    focus_sort_key,
    followup_sort_key,
    has_deadline_due,
    is_high_priority_task,
    next_action_needs_restart_step,
    ranked_focus_candidates,
    task_display_sort_key,
)
import store
from store import (
    Card,
    CardInputError,
    ConflictError,
    InputError,
    NotFoundError,
    TaskError,
    ValidationError,
    _resolve_unique_card,
    _ws,
    card_by_capture_id,
    cards_root,
    clear_focus_task_id,
    current_username,
    discover,
    format_duration,
    is_archived,
    is_hidden,
    is_timestamp,
    item_receipt,
    local_stamp,
    now_stamp,
    open_statuses,
    parse_duration,
    parse_frontmatter,
    parse_time_argument,
    parse_yaml,
    saved_receipt,
    select_cards,
    slugify,
    time_log_entries,
    timer_session_saved,
    write_focus_state,
    write_focus_task_id,
)
import timer_ops
from timer_ops import (  # noqa: F401 - re-exported for callers
    append_time_entry,
    save_time_session,
    settle_timer,
)
import transaction
from transaction import (
    _require_mutation_lock,
    time_mutation_lock,
)
from workspace import Workspace


@dataclass(frozen=True)
class WriteResult:
    """Result of a workspace write operation."""

    card: Card | None
    receipt: dict[str, object]
    message: str = ""
    changed: bool = True
    sync_error: str | None = None
    sync_exit_code: int = 0


def _run_write_operation(
    ws: Workspace | None,
    operation,
    *,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Execute one write operation under time_mutation_lock, then run auto-sync if enabled."""
    resolved = _ws(ws)
    try:
        with time_mutation_lock(resolved):
            result = operation(resolved)
    except TaskError as error:
        if sync and error.sync_needed and store.policy(resolved).get("rules", {}).get("auto_sync", False):
            if quiet_sync:
                transaction.run_auto_sync_for_receipt(ws=resolved)
            else:
                transaction._run_auto_sync(ws=resolved)
        raise
    if sync and result.changed and store.policy(resolved).get("rules", {}).get("auto_sync", False):
        if quiet_sync:
            sync_error = transaction.run_auto_sync_for_receipt(ws=resolved)
            if sync_error:
                return replace(result, sync_error=sync_error)
        else:
            # The sync prints as it runs, so print the result first and do not repeat it.
            if result.message:
                print(result.message, flush=True)
                result = replace(result, message="")
            sync_code = transaction._run_auto_sync(ws=resolved)
            if sync_code:
                return replace(result, sync_exit_code=sync_code)
    return result


def _commit_item_write(
    resolved: Workspace,
    item: Card,
    updated: str,
    *,
    expected: bytes | None = None,
    failure_prefix: str,
    summary: str,
    receipt_fn=None,
) -> WriteResult:
    _require_mutation_lock(resolved)
    base = resolved.root
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
    saved = Card.read(item.path)
    receipt = receipt_fn(saved, updated=True) if receipt_fn is not None else saved_receipt(saved, updated=True, ws=resolved)
    return WriteResult(
        card=saved,
        receipt=receipt,
        message=summary,
        changed=True,
    )


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
            raise CardInputError("task type must be an allowed effective settings value")
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
    card_id = f"{unique_item_timestamp(ws=resolved)}-{slugify(title)}"
    destination = cards_root(resolved) / f"{card_id}.md"
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
            ws=resolved,
        )
        if kind == "task" and status == "doing":
            frontmatter, body = parse_frontmatter(rendered)
            pending = Card(destination, rendered, frontmatter, body, parse_yaml(frontmatter))
            rendered = transition_claim(pending, rendered, "doing", current_username(by, ws=resolved), now_stamp())
    except ValueError as error:
        raise CardInputError(str(error)) from error
    relative = str(destination.relative_to(resolved.root))
    transaction.apply_item_transaction(
        {relative: rendered.encode("utf-8")},
        expected={relative: None},
        source_root=resolved.root,
        ws=resolved,
    )
    return Card.read(destination), True


def capture_card(
    text: str,
    kind: str,
    *,
    title: str | None = None,
    capture_id: str | None = None,
    created_at: str | None = None,
    capture_timestamp: str | None = None,
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
    needs_review: bool | None = None,
    archived: bool = False,
    hidden: bool = False,
    by: str | None = None,
    ws: Workspace | None = None,
) -> tuple[Card, bool]:
    """Save one quick capture with a retry-safe capture ID when one is supplied."""
    resolved_title = title.strip() if isinstance(title, str) else ""
    if not resolved_title and text.strip():
        # A body with only an image or link has no words for a title.
        resolved_title = title_from_note_body(text) if kind == "note" else (capture_title(text) or "Untitled task")
    if not resolved_title:
        raise CardInputError(f"{kind} requires non-empty text or a title")
    normalized_capture_id = None
    if capture_id:
        try:
            normalized_capture_id = str(uuid.UUID(capture_id))
        except (AttributeError, ValueError) as error:
            raise CardInputError("capture ID must be a canonical UUID") from error
        if normalized_capture_id != capture_id.lower():
            raise CardInputError("capture ID must be a canonical UUID")
        if not created_at or not is_timestamp(created_at):
            raise CardInputError("a capture ID requires a created-at time in YYYY-MM-DDTHH:MM:SS format")
    elif created_at:
        raise CardInputError("a created-at time requires a capture ID")
    return create_card(
        kind,
        text,
        title=resolved_title,
        capture_id=normalized_capture_id,
        created=created_at or capture_timestamp,
        status=status if kind == "task" else None,
        task_type=task_type if kind == "task" else None,
        next_action=next_action if kind == "task" else "",
        instructions=instructions,
        priority=priority,
        project=project,
        topic=topic,
        due=due,
        due_kind=due_kind,
        tags=tags,
        needs_review=(kind == "note") if needs_review is None else bool(needs_review),
        archived=bool(archived),
        hidden=bool(hidden),
        by=by if kind == "task" else None,
        ws=ws,
    )


def op_create(
    ws: Workspace | None,
    kind: str,
    text: str,
    *,
    title: str | None = None,
    capture_id: str | None = None,
    created_at: str | None = None,
    capture_timestamp: str | None = None,
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
    needs_review: bool | None = None,
    archived: bool = False,
    hidden: bool = False,
    by: str | None = None,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Create or idempotently replay one task or note capture."""
    def _run(resolved: Workspace) -> WriteResult:
        try:
            card, changed = capture_card(
                text,
                kind,
                title=title,
                capture_id=capture_id,
                created_at=created_at,
                capture_timestamp=capture_timestamp,
                status=status,
                task_type=task_type,
                next_action=next_action,
                instructions=instructions,
                priority=priority,
                project=project,
                topic=topic,
                due=due,
                due_kind=due_kind,
                tags=tags,
                needs_review=needs_review,
                archived=archived,
                hidden=hidden,
                by=by,
                ws=resolved,
            )
        except (OSError, ValueError) as error:
            raise InputError(f"{kind} was not saved: {error}", exit_code=1) from error
        return WriteResult(
            card=card,
            receipt=saved_receipt(card, updated=changed, ws=resolved),
            message=str(card.path),
            changed=changed,
        )

    return _run_write_operation(ws, _run, sync=sync, quiet_sync=quiet_sync)


def _complete_locked(
    resolved: Workspace,
    query: str,
    *,
    notes: list[str] | tuple[str, ...] = (),
    comments: list[str] | tuple[str, ...] = (),
    by: str | None = None,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock(resolved)
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
    updated = card_format.replace_property(task.raw, "status", "done")
    updated = card_format.replace_property(updated, "updated", completed_at)
    updated = card_format.replace_property(updated, "completed", completed_at)
    updated = transition_claim(task, updated, "done", username, completed_at)
    try:
        updated, _timer = settle_timer(updated, completed_epoch)
        updated = append_comments(updated, [*notes, *comments], completed_at)
    except ValueError as error:
        raise InputError(f"completion update failed: {error}", exit_code=1) from error
    return _commit_item_write(
        resolved,
        task,
        updated,
        failure_prefix="completion failed validation; change rolled back",
        summary=f"Completed and validated: {task.path.name}",
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
    quiet_sync: bool = True,
) -> WriteResult:
    """Complete an open task and record an optional completion note or comment."""
    return _run_write_operation(
        ws,
        lambda resolved: _complete_locked(
            resolved, query, notes=notes, comments=comments, by=by, match_mode=match_mode
        ),
        sync=sync,
        quiet_sync=quiet_sync,
    )


def _move_locked(
    resolved: Workspace,
    query: str,
    destination: str,
    *,
    comments: list[str] | tuple[str, ...] = (),
    by: str | None = None,
    takeover: bool = False,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock(resolved)
    query = query.strip()
    if not query:
        raise InputError("move requires a non-empty query")
    allowed = open_statuses(resolved)
    if destination not in allowed:
        raise InputError(f"move destination must be one of: {', '.join(allowed)}")
    task = _resolve_unique_card(resolved, query, kind="task", match_mode=match_mode)
    if task.frontmatter.get("status") == "done":
        raise InputError(
            "completed tasks cannot be reopened with move; use mt add if there is follow-up work",
            exit_code=1,
        )
    try:
        username = current_username(by, ws=resolved)
        check_claim(task, username, takeover=takeover)
    except ValueError as error:
        raise ConflictError(str(error), exit_code=1) from error
    if takeover and destination != "doing":
        raise InputError("--takeover is only valid when moving to doing")
    if takeover and not task.frontmatter.get("claimed_by"):
        raise InputError("task is not claimed; --takeover is not needed")
    if task.frontmatter.get("status") == destination and not (
        destination == "doing" and task.frontmatter.get("claimed_by") != username
    ):
        return WriteResult(
            card=task,
            receipt=saved_receipt(task, updated=False, ws=resolved),
            message=f"Already {destination}: {task.path.name}",
            changed=False,
        )
    moved_epoch = int(clock_time.time())
    moved_at = local_stamp(moved_epoch)
    updated = card_format.replace_property(task.raw, "status", destination)
    updated = card_format.replace_property(updated, "updated", moved_at)
    try:
        updated = transition_claim(task, updated, destination, username, moved_at)
        if destination != "doing" or takeover:
            updated, _timer = settle_timer(updated, moved_epoch)
        updated = append_comments(updated, list(comments), moved_at)
    except ValueError as error:
        raise InputError(f"move failed: {error}", exit_code=1) from error
    return _commit_item_write(
        resolved,
        task,
        updated,
        failure_prefix="move failed validation; change rolled back",
        summary=f"Moved to {destination} and validated: {task.path.name}",
    )


def op_move(
    ws: Workspace | None,
    query: str,
    destination: str,
    *,
    comments: list[str] | tuple[str, ...] = (),
    by: str | None = None,
    takeover: bool = False,
    match_mode: str = "exact",
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Move an open task to another open status, updating claims and timers."""
    return _run_write_operation(
        ws,
        lambda resolved: _move_locked(
            resolved, query, destination, comments=comments, by=by, takeover=takeover, match_mode=match_mode
        ),
        sync=sync,
        quiet_sync=quiet_sync,
    )


def _comment_locked(
    resolved: Workspace,
    query: str,
    comments: list[str] | tuple[str, ...],
    *,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock(resolved)
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
        expected=item.raw.encode("utf-8"),
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
    quiet_sync: bool = True,
) -> WriteResult:
    """Append one or more timestamped comments to a task or note."""
    return _run_write_operation(
        ws,
        lambda resolved: _comment_locked(resolved, query, comments, match_mode=match_mode),
        sync=sync,
        quiet_sync=quiet_sync,
    )


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
    _require_mutation_lock(resolved)
    item = _resolve_unique_card(resolved, query.strip(), match_mode=match_mode)
    kind = item.kind
    original = item.raw.encode("utf-8")
    if expected_hash is not None and hashlib.sha256(original).hexdigest().casefold() != expected_hash.casefold():
        raise ConflictError("conflict: item changed since it was loaded (content hash mismatch)", exit_code=1)
    try:
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
        if item.is_task and (properties.get("status") != "doing"
                             or properties.get("archived") is True or properties.get("hidden") is True):
            raw, _timer = settle_timer(raw, int(clock_time.time()))
        return _commit_item_write(
            resolved,
            item,
            raw,
            expected=original,
            failure_prefix="invalid edit",
            summary=f"Edited and validated: {item.path.relative_to(resolved.root)}",
        )
    except TaskError:
        raise
    except (OSError, ValueError) as error:
        if "conflict:" in str(error):
            raise ConflictError(str(error), exit_code=1) from error
        raise InputError(f"invalid edit: {error}", exit_code=2) from error


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
    quiet_sync: bool = True,
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
        quiet_sync=quiet_sync,
    )


def _convert_locked(
    resolved: Workspace,
    query: str,
    destination: str,
    *,
    status: str | None = None,
    task_type: str | None = None,
    next_action: str | None = None,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock(resolved)
    query = query.strip()
    if not query:
        raise InputError("convert requires a non-empty query")
    to_note = destination == "note"
    if to_note and any(val is not None for val in (status, task_type, next_action)):
        raise InputError("--status, --task-type, and --next-action apply only when converting a note to a task")
    item = _resolve_unique_card(
        resolved,
        query,
        kind=None if to_note else "note",
        statuses=open_statuses(resolved) if to_note else None,
        match_mode=match_mode,
    )
    if item.kind == destination:
        raise InputError(f"convert {destination} destination requires a {'task' if to_note else 'note'} source")
    now = now_stamp()
    try:
        if to_note:
            if item.frontmatter.get("claimed_by"):
                raise ValueError("release the task claim before converting it to a note")
            task_id = str(item.frontmatter.get("id") or item.path.stem)
            if time_log_entries(item.raw) or task_id in transaction.read_time_timers(resolved):
                raise ValueError("a task with tracked time or a running timer cannot be converted to a note")
            updated = render_converted_note(item, now)
        else:
            updated = render_converted_task(
                item, now, status_override=status, type_override=task_type,
                next_action_override=next_action, ws=resolved,
            )
    except ValueError as error:
        raise InputError(str(error), exit_code=1) from error
    return _commit_item_write(
        resolved,
        item,
        updated,
        failure_prefix="conversion failed validation; change rolled back",
        summary=f"Converted to {destination}: {item.path.relative_to(resolved.root)}",
        receipt_fn=lambda saved, updated=True: item_receipt(saved, updated=updated, ws=resolved),
    )


def op_convert(
    ws: Workspace | None,
    query: str,
    destination: str,
    *,
    status: str | None = None,
    task_type: str | None = None,
    next_action: str | None = None,
    match_mode: str = "exact",
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Convert an open task to a note, or a note to a task."""
    return _run_write_operation(
        ws,
        lambda resolved: _convert_locked(
            resolved, query, destination, status=status, task_type=task_type,
            next_action=next_action, match_mode=match_mode,
        ),
        sync=sync,
        quiet_sync=quiet_sync,
    )


def _delete_locked(
    resolved: Workspace,
    query: str,
    *,
    by: str | None = None,
    match_mode: str = "exact",
) -> WriteResult:
    _require_mutation_lock(resolved)
    query = query.strip()
    if not query:
        raise InputError("delete requires a non-empty query")
    task = _resolve_unique_card(resolved, query, match_mode=match_mode)
    if task.is_task:
        try:
            check_claim(task, current_username(by, ws=resolved))
            timers = transaction.read_time_timers(resolved)
        except ValueError as error:
            raise ConflictError(str(error), exit_code=1) from error
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        if task_id in timers:
            raise ConflictError("a task with a running timer cannot be deleted", exit_code=1)
    base = resolved.root
    relative = task.path.relative_to(base).as_posix()
    original = task.raw.encode("utf-8")
    try:
        transaction.apply_item_transaction(
            {}, deletions={relative}, expected={relative: original}, source_root=base, ws=resolved,
        )
    except (OSError, ValueError) as error:
        raise ValidationError(f"delete failed validation; change rolled back: {error}") from error
    receipt = item_receipt(task, ws=resolved)
    receipt["deleted"] = True
    receipt["changed"] = True
    return WriteResult(card=None, receipt=receipt, message=f"Deleted: {task.path}", changed=True)


def op_delete(
    ws: Workspace | None,
    query: str,
    *,
    by: str | None = None,
    match_mode: str = "exact",
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Delete one card after verifying claims and running timers."""
    return _run_write_operation(
        ws,
        lambda resolved: _delete_locked(resolved, query, by=by, match_mode=match_mode),
        sync=sync,
        quiet_sync=quiet_sync,
    )


def _bulk_edit_locked(
    resolved_ws: Workspace,
    plan: dict[str, object],
    *,
    by: str | None = None,
) -> WriteResult:
    _require_mutation_lock(resolved_ws)
    try:
        assignments = plan["assignments"]
        assert isinstance(assignments, dict)
        base = resolved_ws.root
        current_items = select_cards(discover(ws=resolved_ws), _plan_query(plan, resolved_ws))
        current_members = {item.path.relative_to(base).as_posix() for item in current_items}
        planned_members = {
            entry.get("path") for entry in plan["items"]
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        if current_members != planned_members:
            raise ConflictError("conflict: bulk-edit selection membership changed since preview", exit_code=1)
        expected: dict[str, bytes] = {}
        changes: dict[str, bytes] = {}
        receipts = []
        seen: set[str] = set()
        timestamp = now_stamp()
        root_resolved = store.cards_root(resolved_ws).resolve()
        for entry in plan["items"]:
            if not isinstance(entry, dict) or set(entry) != {
                "kind", "path", "ref", "title", "content_hash", "changed", "before", "after"
            }:
                raise ValueError("bulk edit plan contains a malformed card record")
            kind, relative_text = entry["kind"], entry["path"]
            if not isinstance(kind, str) or kind not in {"task", "note"} or not isinstance(relative_text, str):
                raise ValueError("bulk edit plan contains a malformed card path or kind")
            relative = Path(relative_text)
            if (
                relative.is_absolute()
                or relative.as_posix() != relative_text
                or len(relative.parts) != 2
                or relative.parts[0] != "cards"
                or relative.suffix != ".md"
                or relative.name in {"", ".", ".."}
                or relative_text in seen
            ):
                raise ValueError(f"bulk edit plan contains an unsafe or duplicate path: {relative_text}")
            seen.add(relative_text)
            card_path = base / relative
            try:
                resolved = card_path.resolve(strict=True)
                resolved.relative_to(root_resolved)
            except (OSError, ValueError) as error:
                raise ValueError(f"bulk edit plan path is outside the current {kind} cards: {relative_text}") from error
            if card_path.is_symlink() or resolved.parent != root_resolved:
                raise ValueError(f"bulk edit plan path is outside the current {kind} cards: {relative_text}")
            original = card_path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            if not isinstance(entry["content_hash"], str) or digest != entry["content_hash"]:
                raise ConflictError(f"conflict: {relative_text} changed since the bulk edit preview", exit_code=1)
            item = Card.read(card_path)
            if entry["ref"] != f"@{item.short_ref}" or entry["title"] != item.title:
                raise ConflictError(f"conflict: {relative_text} no longer matches the preview", exit_code=1)
            if set(assignments) - set(card_format.EDITABLE_FIELDS[kind]):
                raise ValueError(f"bulk edit field is not available on {kind} {relative_text}")
            before = {field: item.frontmatter.get(field) for field in assignments}
            raw = _apply_bulk_metadata(item.raw, kind, assignments, timestamp, resolved_ws)
            parsed = card_format.parse_yaml(card_format.parse_frontmatter(raw)[0] or "")
            after = {field: parsed.get(field) for field in assignments}
            changed = raw.encode("utf-8") != original
            if (
                type(entry["changed"]) is not bool
                or entry["changed"] is not changed
                or entry["before"] != before
                or entry["after"] != after
            ):
                raise ConflictError(f"conflict: {relative_text} no longer matches the previewed metadata change", exit_code=1)
            expected[relative_text] = original
            if changed:
                if item.is_task and set(assignments) & {"status", "archived", "hidden"}:
                    username = current_username(by, ws=resolved_ws)
                    check_claim(item, username)
                    if parsed.get("status") != item.frontmatter.get("status"):
                        raw = transition_claim(item, raw, str(parsed["status"]), username, timestamp)
                    if parsed.get("status") != "doing" or parsed.get("archived") is True or parsed.get("hidden") is True:
                        raw, _timer = settle_timer(raw, int(clock_time.time()))
                changes[relative_text] = raw.encode("utf-8")
            receipts.append({"ref": entry["ref"], "path": relative_text, "changed": changed})
        changed_count = len(changes)
        unchanged_count = len(receipts) - changed_count
        if (
            plan["selected"] != len(receipts)
            or plan["changed"] != changed_count
            or plan["unchanged"] != unchanged_count
        ):
            raise ValueError("bulk edit plan counts do not match the current cards")
        if changes:
            transaction.apply_item_transaction(changes, expected=expected, source_root=base, ws=resolved_ws)
    except TaskError:
        raise
    except (OSError, ValueError) as error:
        if "conflict:" in str(error):
            raise ConflictError(str(error), exit_code=1) from error
        raise ValidationError(f"bulk edit failed validation; changes rolled back: {error}") from error
    return WriteResult(
        card=None,
        receipt={
            "saved": True,
            "selected": len(receipts),
            "changed": changed_count,
            "unchanged": unchanged_count,
            "items": receipts,
        },
        message=f"Bulk edit applied: {changed_count} changed; {unchanged_count} already set.",
        changed=bool(changes),
    )


def op_bulk_edit(
    ws: Workspace | None,
    plan: dict[str, object],
    *,
    by: str | None = None,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Apply a validated, hash-guarded bulk-edit plan."""
    return _run_write_operation(
        ws,
        lambda resolved: _bulk_edit_locked(resolved, plan, by=by),
        sync=sync,
        quiet_sync=quiet_sync,
    )


def _topic_rename_locked(
    resolved: Workspace,
    old: str,
    new: str,
    *,
    merge: bool = False,
    dry_run: bool = False,
    by: str | None = None,
) -> WriteResult:
    _require_mutation_lock(resolved)
    try:
        plan = topic_rename_plan(old, new, merge, ws=resolved)
        username = current_username(by, ws=resolved)
        claimed = []
        for card, _new_topic in plan:
            try:
                check_claim(card, username)
            except ValueError:
                claimed.append(f"@{card.short_ref} (claimed by {card.frontmatter.get('claimed_by')})")
        if claimed:
            raise CardInputError(
                f"cannot rename: claimed by someone else: {', '.join(claimed)}. "
                f"Release the claim or pass --by with the claiming username; nothing was changed"
            )
        base = resolved.root
        timestamp = now_stamp()
        changes: dict[str, bytes] = {}
        expected: dict[str, bytes | None] = {}
        records = []
        for card, new_topic in plan:
            relative = card.path.relative_to(base).as_posix()
            if card.path.is_symlink():
                raise CardInputError(f"cannot edit a symlinked card: {card.path}")
            original = card.path.read_bytes()
            if original != card.raw.encode("utf-8"):
                raise CardInputError(f"conflict: card changed while planning: {relative}")
            records.append({"ref": f"@{card.short_ref}", "path": relative, "title": card.title,
                            "from": card.frontmatter["topic"], "to": new_topic})
            if card.frontmatter["topic"] == new_topic:
                continue
            properties, body = card_format.parse_document(card.raw, expected_kind=card.kind)
            patched = card_format.apply_property_patch(properties, {"topic": new_topic}, kind=card.kind)
            patched["updated"] = timestamp
            changes[relative] = card_format.render_document(patched, body, expected_kind=card.kind).encode("utf-8")
            expected[relative] = original
        if not dry_run and changes:
            transaction.apply_item_transaction(changes, expected=expected, source_root=base, ws=resolved)
    except CardInputError as error:
        raise InputError(str(error), exit_code=1) from error
    except (OSError, ValueError) as error:
        raise ValidationError(f"topic rename failed; changes rolled back: {error}") from error
    saved = not dry_run
    return WriteResult(
        card=None,
        receipt={"saved": saved, "dry_run": dry_run, "selected": len(records),
                 "changed": len(changes), "items": records},
        changed=saved and bool(changes),
    )


def op_topic_rename(
    ws: Workspace | None,
    old: str,
    new: str,
    *,
    merge: bool = False,
    dry_run: bool = False,
    by: str | None = None,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Rename or merge a topic subtree across cards."""
    return _run_write_operation(
        ws,
        lambda resolved: _topic_rename_locked(resolved, old, new, merge=merge, dry_run=dry_run, by=by),
        sync=sync and not dry_run,
        quiet_sync=quiet_sync,
    )


def op_focus(
    ws: Workspace | None,
    selection: card_query.Query,
    *,
    query: str = "",
    clear: bool = False,
    cycle_next: bool = False,
    by: str | None = None,
) -> dict:
    """Update or read local focus state under the mutation lock and return focus context."""
    def _run(resolved: Workspace) -> WriteResult:
        actor = current_username(by, ws=resolved)
        if clear:
            clear_focus_task_id(resolved)
        elif query:
            task = _resolve_unique_card(resolved, query, statuses=open_statuses(resolved))
            if task.frontmatter.get("status") not in {"inbox", "active", "doing"}:
                raise InputError(
                    "focus requires an inbox, active, or doing task; read blocked or deferred work first"
                )
            check_claim(task, actor)
            write_focus_task_id(str(task.frontmatter.get("id") or task.path.stem), ws=resolved)
        open_query = card_query.narrow(selection, "status", open_statuses(resolved))
        open_tasks = select_cards(discover(ws=resolved), open_query) if open_query else []
        context = focus_context(open_tasks, actor=actor, ws=resolved)
        candidates = context["candidates"]
        if cycle_next and candidates:
            current_id = context["selected"]["id"]
            current_index = next(index for index, item in enumerate(candidates) if item["id"] == current_id)
            selected_task = candidates[(current_index + 1) % len(candidates)]
            write_focus_state(str(selected_task["id"]), "cycle", ws=resolved)
            context = focus_context(open_tasks, actor=actor, ws=resolved)
        return WriteResult(card=None, receipt=context, changed=False)

    return _run_write_operation(ws, _run, sync=False).receipt


def _stop_timer_card(
    resolved: Workspace, card: Card, end_epoch: int, timestamp: str,
) -> Card:
    """Stop one card's timer in a single atomic card write; return the card as saved."""
    updated, timer = settle_timer(card.raw, end_epoch)
    if timer is None:
        return card
    updated = card_format.replace_property(updated, "updated", timestamp)
    result = _commit_item_write(
        resolved,
        card,
        updated,
        expected=card.raw.encode("utf-8"),
        failure_prefix="time log failed validation",
        summary="",
    )
    assert result.card is not None
    return result.card


def _time_start_locked(
    resolved: Workspace,
    query: str,
    *,
    parallel: bool = False,
    by: str | None = None,
) -> WriteResult:
    _require_mutation_lock(resolved)
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
        running = {store.card_task_id(card): (card, timer)
                   for card, timer in transaction.read_time_timer_cards(resolved)}
        if task_id in running:
            raise ValueError(f"timer already running for [@{task.short_ref}] {task.title}")
        started_epoch = int(clock_time.time())
        timestamp = local_stamp(started_epoch)
        timer = {"session_id": uuid.uuid4().hex, "start": timestamp,
                 "start_epoch": started_epoch, "by": username}
        updated = task.raw
        if task.frontmatter.get("status") != "doing" or task.frontmatter.get("claimed_by") != username:
            updated = card_format.replace_property(updated, "status", "doing")
            updated = transition_claim(task, updated, "doing", username, timestamp)
        updated = card_format.replace_property(updated, "timer", timer)
        updated = card_format.replace_property(updated, "updated", timestamp)
        saved_result = _commit_item_write(
            resolved,
            task,
            updated,
            expected=task.raw.encode("utf-8"),
            failure_prefix="could not start timer",
            summary="",
        )
    except TaskError as error:
        raise ConflictError(str(error), exit_code=1) from error
    except (OSError, ValueError) as error:
        raise ConflictError(f"could not start timer: {error}", exit_code=1) from error
    if not parallel:
        for other_id, (other, other_timer) in sorted(running.items()):
            if other_timer["by"] != username:
                continue
            try:
                _stop_timer_card(resolved, other, max(started_epoch, int(other_timer["start_epoch"])), timestamp)
            except (OSError, ValueError, TaskError) as error:
                raise ConflictError(
                    f"started the timer, but could not stop the timer on {other.title}: "
                    f"{error}; both timers are running, use mt time stop on the one to end",
                    exit_code=1, sync_needed=True,
                ) from error
    assert saved_result.card is not None
    return WriteResult(
        card=saved_result.card,
        receipt=saved_receipt(saved_result.card, updated=True, ws=resolved),
        message=f"Started timer for [@{task.short_ref}] {task.title} at {timer['start']} ({username}).",
        changed=True,
    )


def op_time_start(
    ws: Workspace | None,
    query: str,
    *,
    parallel: bool = False,
    by: str | None = None,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Start a timer on an open task (claiming and moving it to doing if needed)."""
    return _run_write_operation(
        ws,
        lambda resolved: _time_start_locked(resolved, query, parallel=parallel, by=by),
        sync=sync,
        quiet_sync=quiet_sync,
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
    _require_mutation_lock(resolved)
    try:
        running = {store.card_task_id(card): (card, timer)
                   for card, timer in transaction.read_time_timer_cards(resolved)}
    except ValueError as error:
        raise InputError(str(error), exit_code=1) from error
    if not running:
        raise NotFoundError("no timer is running", exit_code=1, prefixed=True)
    query = query.strip()
    if query in running:
        task, timer = running[query]
    elif query:
        found = _resolve_unique_card(resolved, query, kind="task")
        selected = running.get(str(found.frontmatter.get("id") or found.path.stem))
        if selected is None:
            raise NotFoundError(f"no timer is running for [@{found.short_ref}] {found.title}", exit_code=1, prefixed=True)
        task, timer = selected
    elif len(owned := {key: value for key, value in running.items()
                       if value[1]["by"] == current_username(by, ws=resolved)}) == 1:
        task, timer = next(iter(owned.values()))
    elif not owned:
        raise NotFoundError("no timer is running for this user; specify a task", exit_code=1, prefixed=True)
    else:
        refs = [f"@{card.short_ref}" for _, (card, _timer) in sorted(owned.items())]
        raise ConflictError(f"multiple timers are running; specify a task: {', '.join(refs)}", exit_code=1)
    if timer["by"] != current_username(by, ws=resolved):
        raise ConflictError(f"timer belongs to {timer['by']}; use --by {shlex.quote(str(timer['by']))}", exit_code=1)
    if action == "cancel":
        properties, body = card_format.parse_document(task.raw)
        del properties["timer"]
        properties["updated"] = now_stamp()
        try:
            cancelled = _commit_item_write(
                resolved,
                task,
                card_format.render_document(properties, body),
                expected=task.raw.encode("utf-8"),
                failure_prefix="could not cancel timer",
                summary="",
            ).card
        except ConflictError as error:
            raise ConflictError(
                f"card changed since the timer was read; nothing was written, run the command again: {error}",
                exit_code=1,
            ) from error
        except ValidationError as error:
            raise InputError(str(error), exit_code=1) from error
        assert cancelled is not None
        return WriteResult(
            card=cancelled,
            receipt=saved_receipt(cancelled, updated=True, ws=resolved),
            message=f"Cancelled timer for [@{task.short_ref}] {task.title}; no time recorded.",
            changed=True,
        )
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
    already_saved = timer_session_saved(task, timer)
    try:
        updated_task = _stop_timer_card(resolved, task, end_epoch, now_stamp())
    except ConflictError as error:
        raise ConflictError(
            f"card changed since the timer was read; nothing was written, run the command again: {error}",
            exit_code=1,
        ) from error
    except (OSError, ValueError, ValidationError) as error:
        raise ValidationError(f"time log failed validation; running timer was retained: {error}", exit_code=1) from error
    summary = (
        "Timer session was already saved; cleared the running state."
        if already_saved
        else f"Stopped timer for [@{task.short_ref}] {task.title}: {format_duration(elapsed)}."
    )
    return WriteResult(
        card=updated_task,
        receipt=saved_receipt(updated_task, updated=not already_saved, ws=resolved),
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
    quiet_sync: bool = True,
) -> WriteResult:
    """Stop or cancel a running timer."""
    return _run_write_operation(
        ws,
        lambda resolved: _time_end_locked(resolved, query=query, action=action, at=at, duration=duration, by=by),
        sync=sync,
        quiet_sync=quiet_sync,
    )


def _time_remove_locked(resolved: Workspace, session_id_arg: str) -> WriteResult:
    _require_mutation_lock(resolved)
    session_ref = session_id_arg.lower()
    if not re.fullmatch(r"[0-9a-f]{8,32}", session_ref):
        raise InputError("use a session ID from 'mt time list' (at least 8 hexadecimal characters)", exit_code=2)
    matches = [
        (task, entry)
        for task in discover("task", ws=resolved)
        for entry in time_log_entries(task.raw)
        if str(entry["session_id"]).startswith(session_ref)
    ]
    if not matches:
        raise NotFoundError(f"no recorded session matches '{session_ref}'", exit_code=1, prefixed=True)
    if len(matches) > 1:
        lines = "\n".join(f"  {entry['session_id']}  [@{task.short_ref}] {task.title}" for task, entry in matches)
        raise InputError(f"multiple sessions match; use a longer session ID:\n{lines}", exit_code=2)
    task, entry = matches[0]
    session_id = str(entry["session_id"])
    properties, body = card_format.parse_document(task.raw, expected_kind="task")
    entries = properties.get("time_entries", [])
    if not isinstance(entries, list):
        raise ValidationError("task time_entries property is malformed")
    properties["time_entries"] = [
        item for item in entries if not isinstance(item, dict) or item.get("session_id") != session_id
    ]
    if not properties["time_entries"]:
        properties.pop("time_entries")
    updated = card_format.render_document(properties, body, expected_kind="task")
    updated = card_format.replace_property(updated, "updated", now_stamp())
    result = _commit_item_write(
        resolved,
        task,
        updated,
        expected=task.raw.encode("utf-8"),
        failure_prefix="session removal failed validation; task was restored",
        summary=f"Removed session {session_id} ({format_duration(int(entry['elapsed_seconds']))}) "
                f"from [@{task.short_ref}] {task.title}.",
    )
    return result


def op_time_remove(ws: Workspace | None, session_id: str, *, sync: bool = True, quiet_sync: bool = True) -> WriteResult:
    """Remove a recorded time session from a task."""
    return _run_write_operation(ws, lambda resolved: _time_remove_locked(resolved, session_id), sync=sync, quiet_sync=quiet_sync)


def _time_add_locked(
    resolved: Workspace,
    query: str,
    *,
    duration: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> WriteResult:
    _require_mutation_lock(resolved)
    try:
        end_epoch = parse_time_argument(end) if end else int(clock_time.time())
        if start:
            if duration:
                raise ValueError("use either DURATION or --start, not both")
            start_epoch = parse_time_argument(start)
            seconds = end_epoch - start_epoch
        else:
            if not duration:
                raise ValueError("provide DURATION or --start TIME")
            seconds = parse_duration(duration)
            start_epoch = end_epoch - seconds
        if seconds <= 0 or end_epoch > int(clock_time.time()):
            raise ValueError("session start must precede end, and end cannot be in the future")
    except ValueError as error:
        raise InputError(str(error), exit_code=2) from error
    task = _resolve_unique_card(resolved, query.strip(), kind="task")
    timer = {
        "session_id": uuid.uuid4().hex,
        "task_id": str(task.frontmatter.get("id") or task.path.stem),
        "start": local_stamp(start_epoch),
        "start_epoch": start_epoch,
    }
    saved, message = save_time_session(task, timer, end_epoch, ws=resolved)
    if not saved:
        raise ValidationError(message)
    updated = Card.read(task.path)
    return WriteResult(
        card=updated,
        receipt=saved_receipt(updated, updated=True, ws=resolved),
        message=f"Added {format_duration(seconds)} to [@{task.short_ref}] {task.title}.",
        changed=True,
    )


def op_time_add(
    ws: Workspace | None,
    query: str,
    *,
    duration: str | None = None,
    start: str | None = None,
    end: str | None = None,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Record a manual time session on a task."""
    return _run_write_operation(
        ws,
        lambda resolved: _time_add_locked(resolved, query, duration=duration, start=start, end=end),
        sync=sync,
        quiet_sync=quiet_sync,
    )


def op_draft_create(
    ws: Workspace | None,
    kind: str,
    *,
    draft_id: str | None = None,
    title: str | None = None,
    body: str = "",
    capture_id: str | None = None,
    created: str | None = None,
) -> dict[str, object]:
    """Create a new-mode draft in the workspace."""
    resolved = _ws(ws)
    if title is not None and not card_format.is_single_line_text(title):
        raise InputError("draft title must be non-empty and single-line", exit_code=2)
    stamp = now_stamp()
    if created is not None and (not isinstance(created, str) or not is_timestamp(created)):
        raise InputError("--created-at must be a local ISO timestamp with seconds", exit_code=2)
    raw_capture_id = capture_id or str(uuid.uuid4())
    try:
        parsed_capture_id = uuid.UUID(raw_capture_id)
    except (AttributeError, ValueError) as error:
        raise InputError("--capture-id must be a valid UUID", exit_code=2) from error
    if str(parsed_capture_id) != raw_capture_id.lower():
        raise InputError("--capture-id must be a canonical UUID", exit_code=2)
    fields: dict[str, object] = {
        "kind": kind,
        "mode": "new",
        "title": title or "",
        "capture_id": str(parsed_capture_id),
        "created": created or stamp,
    }
    if kind == "task":
        default_status, default_priority, default_type = task_defaults(store.policy(resolved))
        fields.update({
            "status": default_status, "priority": default_priority, "type": default_type,
            "project": None, "topic": None, "tags": [], "due": None, "due_kind": None,
        })
    try:
        return draft_store.save(
            resolved.root,
            draft_id=draft_id,
            fields=fields,
            body=body,
            now=stamp,
        )
    except (OSError, ValueError) as error:
        raise InputError(str(error), exit_code=1) from error


def op_draft_edit(
    ws: Workspace | None,
    draft_id: str,
    *,
    fields: dict[str, object],
    body: str,
    expected_hash: str,
) -> dict[str, object]:
    """Update an existing draft with compare-and-swap."""
    resolved = _ws(ws)
    return draft_store.save(
        resolved.root,
        draft_id=draft_id,
        fields=fields,
        body=body,
        expected_content_hash=expected_hash,
        now=now_stamp(),
    )


def op_draft_discard(
    ws: Workspace | None,
    draft_id: str,
    *,
    expected_hash: str | None,
) -> bool:
    """Discard a draft with compare-and-swap."""
    resolved = _ws(ws)
    return draft_store.discard(resolved.root, draft_id, expected_hash)


def _save_new_draft(resolved: Workspace, draft: dict[str, object]) -> tuple[Card, bool]:
    """Capture a new card from a 'new'-mode draft."""
    body = str(draft.get("body") or "")
    return capture_card(
        body,
        str(draft["kind"]),
        title=str(draft.get("title") or "").strip() or None,
        capture_id=str(draft.get("capture_id") or ""),
        created_at=str(draft.get("created") or ""),
        instructions=str(draft.get("instructions") or ""),
        priority=draft.get("priority") if isinstance(draft.get("priority"), str) else None,
        project=draft.get("project") if isinstance(draft.get("project"), str) else None,
        topic=draft.get("topic") if isinstance(draft.get("topic"), str) else None,
        due=draft.get("due") if isinstance(draft.get("due"), str) else None,
        due_kind=draft.get("due_kind") if isinstance(draft.get("due_kind"), str) else None,
        tags=list(draft.get("tags") or []) if isinstance(draft.get("tags"), list) else [],
        status=draft.get("status") if draft["kind"] == "task" and isinstance(draft.get("status"), str) else None,
        task_type=draft.get("type") if draft["kind"] == "task" and isinstance(draft.get("type"), str) else None,
        ws=resolved,
    )


def _draft_save_locked(
    resolved: Workspace,
    draft_id: str,
    *,
    expected_hash: str | None = None,
) -> tuple[WriteResult, bool]:
    _require_mutation_lock(resolved)
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
            if expected_hash is not None and draft["content_hash"] != expected_hash:
                raise draft_store.DraftConflict("draft content changed outside microtask; refresh and reconcile")
            if draft["mode"] != "new":
                raise ValueError("draft mode must be new")
            item, changed = _save_new_draft(resolved, draft)
            if draft_store.read(resolved.root, draft_id)["content_hash"] != draft["content_hash"]:
                raise draft_store.DraftConflict(
                    "draft content changed outside microtask; card saved and draft retained"
                )
            path.unlink()
            draft_removed = True
            if item is None:
                raise ValueError("draft save did not create a card")
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
    expected_hash: str | None = None,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Apply a draft to the workspace and remove the draft file."""
    def _run(resolved: Workspace) -> WriteResult:
        result, draft_removed = _draft_save_locked(
            resolved,
            draft_id,
            expected_hash=expected_hash,
        )
        if draft_removed and not result.changed:
            return replace(result, changed=True)
        return result

    return _run_write_operation(ws, _run, sync=sync, quiet_sync=quiet_sync)


def op_card_metadata(
    ws: Workspace | None,
    query: str,
    patch: dict[str, object],
    *,
    expected_hash: str,
    kind: str | None = None,
    match_mode: str = "exact",
    sync: bool = True,
    quiet_sync: bool = True,
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
        allowed_fields = card_format.EDITABLE_FIELDS[item_kind] - {"status", *card_format.BOOLEAN_METADATA}
        if set(patch) - allowed_fields:
            raise InputError("metadata patch contains unsupported or missing fields", exit_code=2)
        original = item.raw.encode("utf-8")
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

    return _run_write_operation(ws, _run, sync=sync, quiet_sync=quiet_sync)


def _store_attachment_locked(
    resolved: Workspace,
    data: bytes,
    *,
    filename: str = "",
    content_type: str | None = None,
    card_ref: str | None = None,
    match_mode: str = "exact",
    label: str | None = None,
) -> WriteResult:
    _require_mutation_lock(resolved)
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise InputError("attachment must not be empty", exit_code=2)
    payload_bytes = bytes(data)
    policy = store.policy(resolved)
    max_bytes = attachments.max_attachment_bytes(policy)
    if len(payload_bytes) > max_bytes:
        limit_mb = policy.get("rules", {}).get("max_attachment_mb", attachments.DEFAULT_MAX_ATTACHMENT_MB)
        raise InputError(f"attachment exceeds max_attachment_mb ({limit_mb} MB)", exit_code=2)

    item = _resolve_unique_card(resolved, card_ref, match_mode=match_mode) if card_ref else None
    target_dir = resolved.attachments_root()
    if target_dir.is_symlink():
        raise ValidationError("attachments/ must not be a symbolic link")
    if target_dir.exists() and not target_dir.is_dir():
        raise ValidationError("attachments exists but is not a directory")

    base_name = attachments.build_attachment_filename(payload_bytes, filename, content_type)
    stored_name = base_name
    target_path = target_dir / stored_name
    deduplicated = False
    file_changed = False

    if target_path.exists():
        if target_path.is_symlink() or not target_path.is_file():
            raise ValidationError(f"invalid existing attachment path: attachments/{stored_name}")
        if target_path.read_bytes() == payload_bytes:
            deduplicated = True
        else:
            stem = Path(base_name).stem
            suffix = Path(base_name).suffix
            counter = 2
            while True:
                candidate = f"{stem}-{counter}{suffix}"
                candidate_path = target_dir / candidate
                if not candidate_path.exists():
                    stored_name = candidate
                    target_path = candidate_path
                    break
                if candidate_path.is_file() and not candidate_path.is_symlink() and candidate_path.read_bytes() == payload_bytes:
                    stored_name = candidate
                    target_path = candidate_path
                    deduplicated = True
                    break
                counter += 1

    if not deduplicated:
        card_format.atomic_write(target_path, payload_bytes)
        file_changed = True

    relative_path = f"../attachments/{stored_name}"
    is_image = attachments.is_image_filename(stored_name)
    if label is not None:
        md_label = label
    elif filename and not is_image:
        md_label = Path(filename.replace("\\", "/")).name
    else:
        md_label = ""
    markdown = attachments.markdown_link(relative_path, md_label, is_image=is_image)

    receipt: dict[str, object] = {
        "filename": stored_name,
        "path": f"attachments/{stored_name}",
        "relative_path": relative_path,
        "url": f"/attachments/{stored_name}",
        "markdown": markdown,
        "size": len(payload_bytes),
        "is_image": is_image,
        "deduplicated": deduplicated,
    }

    if item is not None:
        properties = dict(item.frontmatter)
        body = item.body
        new_body = f"{body.rstrip()}\n\n{markdown}" if body.strip() else markdown
        properties["updated"] = store.now_stamp()
        raw = card_format.render_document(properties, new_body, expected_kind=item.kind)
        try:
            card_result = _commit_item_write(
                resolved,
                item,
                raw,
                failure_prefix="attach failed",
                summary=f"Attached {stored_name} to @{item.short_ref}",
                receipt_fn=lambda saved, updated=True: {
                    **receipt,
                    "card": store.item_receipt(saved, updated=updated, ws=resolved),
                },
            )
        except (ConflictError, ValidationError):
            if file_changed:
                target_path.unlink(missing_ok=True)
            raise
        return WriteResult(
            card=card_result.card,
            receipt=card_result.receipt,
            message=card_result.message,
            changed=True,
        )

    return WriteResult(
        card=None,
        receipt=receipt,
        message=f"{markdown}",
        changed=file_changed,
    )


def op_store_attachment(
    ws: Workspace | None,
    data: bytes,
    *,
    filename: str = "",
    content_type: str | None = None,
    card_ref: str | None = None,
    match_mode: str = "exact",
    label: str | None = None,
    sync: bool = True,
    quiet_sync: bool = True,
) -> WriteResult:
    """Store an attachment in attachments/ and optionally append its Markdown link to a card."""
    return _run_write_operation(
        ws,
        lambda resolved: _store_attachment_locked(
            resolved,
            data,
            filename=filename,
            content_type=content_type,
            card_ref=card_ref,
            match_mode=match_mode,
            label=label,
        ),
        sync=sync,
        quiet_sync=quiet_sync,
    )
