"""Card document rendering, body editing, metadata parsing, and topic tree helpers."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import shlex

import card_format
from card_format import CLAIM_FIELDS
import card_query
import store
from store import (
    Card,
    CardInputError,
    _ws,
    abbreviate,
    discover,
    due_kind_error,
    due_kind_value,
    is_archived,
    is_due_value,
    is_hidden,
    now_stamp,
    open_statuses,
    select_cards,
)
from workspace import Workspace


def unique_item_timestamp(*, ws: Workspace | None = None) -> str:
    resolved = _ws(ws)
    current = datetime.now().replace(microsecond=0)
    while True:
        timestamp = current.strftime("%Y-%m-%d-%H%M%S")
        root = resolved.cards_root()
        if not any(path.name.startswith(f"{timestamp}-") for path in root.glob("*.md")):
            return timestamp
        current += timedelta(seconds=1)


def clean_text_values(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def append_comments(raw: str, comments: list[str], timestamp: str, by: str | None = None) -> str:
    """Add comments to a card. A name in `by` is recorded as their author; without one the record stays {at, text}."""
    cleaned = clean_text_values(comments)
    if not cleaned:
        return raw
    properties, body = card_format.parse_document(raw)
    records = properties.setdefault("comments", [])
    if not isinstance(records, list):
        raise ValueError("comments must be a list")
    records.extend({"at": timestamp, "text": comment, **({"by": by} if by else {})} for comment in cleaned)
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
        if not isinstance(capture_id, str) or not capture_id.strip():
            raise ValueError("capture_id must not be empty")
        properties["capture_id"] = capture_id
    return card_format.render_document(properties, body_text, expected_kind=item_mode)


def parse_tag_json(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("tags must be a JSON string array") from error
    if not isinstance(parsed, list) or any(
        not card_format.is_single_line_text(tag) for tag in parsed
    ):
        raise ValueError("tags must be a JSON array of non-empty single-line strings")
    return list(dict.fromkeys(tag.strip() for tag in parsed))


def check_claim(task: Card, username: str, *, takeover: bool = False) -> None:
    owner = task.frontmatter.get("claimed_by")
    if owner and owner != username and not takeover:
        raise CardInputError(
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
            raise ValueError(f"settings {field} must name a configured {singular}")
        values.append(value)
    return values[0], values[1], values[2]


def unassigned_priority(cfg: dict) -> str:
    """Use the unassigned class when supported, for explicit unassigned flows."""
    return "none" if "none" in cfg.get("priorities", {}) else task_defaults(cfg)[1]


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
        raise ValueError("priority must be an allowed effective settings value")
    normalized: dict[str, object] = {"priority": selected_priority}
    if project is not None and not card_format.is_single_line_text(project):
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
    if not isinstance(tags, list) or any(not card_format.is_single_line_text(tag) for tag in tags):
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
    ws: Workspace,
) -> str:
    """Render a new card; tasks fill status and type from settings defaults."""
    cfg = store.policy(ws)
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


def render_converted_task(
    note: Card, today: str, *, status_override: str | None = None,
    type_override: str | None = None, next_action_override: str | None = None, ws: Workspace,
) -> str:
    cfg = store.policy(ws)
    default_status, _priority, default_type = task_defaults(cfg)
    status = status_override or default_status
    task_type = type_override or default_type
    allowed_open = open_statuses(ws)
    if status not in allowed_open:
        raise ValueError(f"task status must be one of: {', '.join(allowed_open)}")
    if task_type not in cfg.get("types", {}):
        raise ValueError("task type must be an allowed effective settings value")
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


_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]\r\n]*)\]\((?:<[^>\r\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\r\n]+)\]\((?:<[^>\r\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")


def plain_title_text(text: str) -> str:
    """Body text as a title reads it: images become their alt text and links their label."""
    text = _MARKDOWN_IMAGE_RE.sub(lambda match: match.group(1), text)
    return _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), text)


def capture_title(text: str) -> str:
    """Keep generated titles short and mark omitted text without summarizing it."""
    title = re.sub(r"\s+", " ", plain_title_text(text)).strip()
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
        value = capture_title(re.sub(r"^#{1,6}\s+", "", value))
        if value:
            return value
    return "Meeting notes" if is_meeting else "Untitled note"


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


def parse_field_assignment(field: str, value: str) -> object:
    """Parse one ``FIELD=VALUE`` string into a typed metadata property value."""
    if field == "tags":
        return parse_tag_json(value)
    if field in card_format.BOOLEAN_METADATA:
        if value not in {"true", "false"}:
            raise ValueError(f"{field} must be true or false")
        return value == "true"
    if field in {"next_action", "processing_instructions"}:
        return None if value in {"null", ""} else value
    if field == "title":
        if not card_format.is_single_line_text(value):
            raise ValueError("title must be non-empty and single-line")
        return value.strip()
    return None if value == "null" else value


def validate_open_status_change(resolved: Workspace, kind: str, new_status: object) -> str:
    if kind != "task":
        raise ValueError("field 'status' cannot be set on a note")
    if not isinstance(new_status, str) or not new_status.strip():
        raise ValueError("status must be non-empty")
    normalized = new_status.strip()
    if normalized == "doing":
        raise ValueError("status 'doing' requires claiming; use mt move doing or mt claim")
    if normalized == "done":
        raise ValueError("status 'done' requires completing; use mt done")
    allowed_open = open_statuses(resolved)
    if normalized not in allowed_open:
        raise ValueError(
            f"status must be one of: {', '.join(status for status in allowed_open if status != 'doing')}"
        )
    return normalized


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
    allowed = card_format.EDITABLE_FIELDS[kind]
    for assignment in assignments or ():
        field, separator, value = assignment.partition("=")
        if not separator or not field:
            raise ValueError("--set requires FIELD=VALUE")
        if field not in allowed:
            raise ValueError(f"field '{field}' cannot be set on a {kind}; choose from {', '.join(sorted(allowed))}")
        if field in metadata:
            raise ValueError(f"field '{field}' was assigned more than once")
        metadata[field] = parse_field_assignment(field, value)
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
        metadata["status"] = validate_open_status_change(resolved, kind, metadata["status"])
    return metadata


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


def _apply_bulk_metadata(raw: str, kind: str, assignments: dict[str, str], updated: str, ws: Workspace) -> str:
    frontmatter, body = card_format.parse_document(raw, expected_kind=kind)
    allowed = card_format.EDITABLE_FIELDS[kind]
    targets: dict[str, object] = {}
    for field, value in assignments.items():
        if field not in allowed:
            raise ValueError(f"field '{field}' cannot be set on a {kind}")
        if field == "status":
            targets[field] = validate_open_status_change(ws, kind, value)
        else:
            targets[field] = parse_field_assignment(field, value)
    patched = card_format.apply_property_patch(frontmatter, targets, kind=kind)
    if patched == frontmatter:
        return raw
    if kind == "task" and patched.get("status") != frontmatter.get("status"):
        for field in CLAIM_FIELDS:
            patched.pop(field, None)
    patched["updated"] = updated
    return card_format.render_document(patched, body, expected_kind=kind)


def _plan_query(plan: dict[str, object], ws: Workspace) -> card_query.Query:
    """The query a saved plan holds, checked like one typed on the command line."""
    stored = plan["query"]
    if not isinstance(stored, dict):
        raise ValueError("bulk edit plan has an invalid query")
    try:
        query = card_query.query_from_mapping(stored)
        card_query.validate(query, statuses=store.statuses(ws), priorities=list(store.policy(ws).get("priorities", {})))
    except card_query.QueryError as error:
        raise ValueError(f"bulk edit plan has an invalid query: {error}") from error
    return query


def _bulk_plan_item(item: Card, base: Path, assignments: dict[str, str], updated: str, ws: Workspace) -> tuple[dict[str, object], bytes | None]:
    kind = item.kind
    if item.path.is_symlink():
        raise ValueError(f"cannot edit a symlinked card: {item.path}")
    original = item.path.read_bytes()
    if original != item.raw.encode("utf-8"):
        raise ValueError(f"conflict: item changed during bulk preview: {item.path}")
    before = {field: item.frontmatter.get(field) for field in assignments}
    candidate = _apply_bulk_metadata(item.raw, kind, assignments, updated, ws)
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
