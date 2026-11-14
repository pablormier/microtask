"""Canonical task and note document format.

A card is one YAML mapping followed by an opaque Markdown body.  The body is
never parsed or rewritten while changing properties.
"""
from __future__ import annotations

import hashlib
import math
import re
import uuid
import calendar
from datetime import date, datetime, timedelta
from typing import Any

from vendor import yaml


CLAIM_FIELDS = ("claimed_by", "claimed_at")
BOOLEAN_METADATA = ("archived", "hidden", "needs_review")
GITHUB_SOURCE_FIELDS = ("github_source", "github_source_hash")

# Known properties are emitted first in this order. Unknown extension values
# retain their original insertion order after the known fields.
FIELD_ORDER = (
    "id", "capture_id", "title", "kind", "status", "priority", "type",
    "project", "topic", "due", "due_kind", "created", "updated",
    "completed", "archived", "hidden", "needs_review", "tags",
    "next_action", "processing_instructions", *CLAIM_FIELDS,
    *GITHUB_SOURCE_FIELDS, "comments", "time_entries",
)
EDITABLE_METADATA = frozenset({
    "title", *BOOLEAN_METADATA, "topic", "due", "due_kind", "priority", "project", "tags",
    "next_action", "processing_instructions",
})
TASK_ONLY_FIELDS = frozenset({"next_action", "status", "type", "completed", "time_entries", *CLAIM_FIELDS})
COMMON_REQUIRED = (
    "id", "title", "kind", "priority", "created", "updated",
)


class CardFormatError(ValueError):
    """A card is malformed or violates the canonical document schema."""


class _SafeCardLoader(yaml.SafeLoader):
    """SafeLoader with timestamp inference disabled and duplicate keys rejected."""

    def compose_node(self, parent: object, index: object) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            raise CardFormatError("YAML aliases are not supported in card properties")
        return super().compose_node(parent, index)


# PyYAML's SafeLoader otherwise turns ISO dates into datetime.date objects.
_SafeCardLoader.yaml_implicit_resolvers = {
    initial: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(loader: _SafeCardLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise CardFormatError("YAML mapping keys must be scalar values") from error
        if duplicate:
            raise CardFormatError(f"duplicate YAML mapping key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_SafeCardLoader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)


def _reject_non_json_yaml(loader: _SafeCardLoader, node: yaml.Node) -> object:
    raise CardFormatError(f"YAML tag {node.tag!r} is not supported in card properties")


for _tag in ("binary", "set", "timestamp", "omap", "pairs"):
    _SafeCardLoader.add_constructor(f"tag:yaml.org,2002:{_tag}", _reject_non_json_yaml)


class _CardDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def _represent_string(dumper: _CardDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_CardDumper.add_representer(str, _represent_string)


def parse_frontmatter(raw: str) -> tuple[str | None, str]:
    """Split the first YAML frontmatter block, preserving the body verbatim."""
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, raw
    offset = len(lines[0])
    frontmatter_start = offset
    for line in lines[1:]:
        bare = line.rstrip("\r\n")
        if bare == "---":
            frontmatter = raw[frontmatter_start:offset]
            body = raw[offset + len(line):]
            return frontmatter, body
        offset += len(line)
    return None, raw


def parse_yaml(frontmatter: str) -> dict[str, object]:
    try:
        value = yaml.load(frontmatter, Loader=_SafeCardLoader)
    except CardFormatError:
        raise
    except yaml.YAMLError as error:
        raise CardFormatError(f"invalid YAML frontmatter: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CardFormatError("frontmatter must be a mapping with string keys")
    def json_values(item: object) -> bool:
        if item is None or isinstance(item, (str, bool, int)):
            return True
        if isinstance(item, float):
            return math.isfinite(item)
        if isinstance(item, list):
            return all(json_values(child) for child in item)
        if isinstance(item, dict):
            return all(isinstance(key, str) and json_values(child) for key, child in item.items())
        return False
    if not json_values(value):
        raise CardFormatError("frontmatter values must use JSON-compatible YAML types")
    return value


def validate_records(properties: dict[str, object]) -> list[str]:
    errors: list[str] = []
    for field in ("comments", "time_entries"):
        if field not in properties:
            continue
        records = properties[field]
        if not isinstance(records, list):
            errors.append(f"{field} must be a list")
            continue
        for index, record in enumerate(records):
            prefix = f"{field}[{index}]"
            if not isinstance(record, dict):
                errors.append(f"{prefix} must be a mapping")
                continue
            if field == "comments":
                if set(record) != {"at", "text"}:
                    errors.append(f"{prefix} must contain exactly at and text")
                    continue
                if not isinstance(record.get("at"), str) or not is_metadata_time(record["at"]):
                    errors.append(f"{prefix}.at must be an ISO date or local timestamp")
                if not isinstance(record.get("text"), str) or not record["text"].strip():
                    errors.append(f"{prefix}.text must be non-empty text")
                continue
            if set(record) != {"session_id", "start", "end", "elapsed_seconds"}:
                errors.append(f"{prefix} must contain session_id, start, end, and elapsed_seconds")
                continue
            if not isinstance(record.get("session_id"), str) or not re.fullmatch(
                r"[0-9a-f]{32}", str(record.get("session_id"))
            ):
                errors.append(f"{prefix}.session_id must be 32 lowercase hexadecimal characters")
            for field_name in ("start", "end"):
                value = record.get(field_name)
                if not isinstance(value, str) or not is_timestamp(value):
                    errors.append(f"{prefix}.{field_name} must be a local ISO timestamp")
            elapsed = record.get("elapsed_seconds")
            if type(elapsed) is not int or elapsed < 0:
                errors.append(f"{prefix}.elapsed_seconds must be a non-negative integer")
    return errors


TOPIC_SEPARATOR = "/"


def normalize_topic(value: str) -> str:
    """Trim whitespace around each '/'-separated segment; validity is checked separately."""
    return TOPIC_SEPARATOR.join(segment.strip() for segment in value.split(TOPIC_SEPARATOR)).strip()


def topic_path_error(value: str) -> str | None:
    """Why `value` is not a usable topic path (single-line, no empty segments), or None."""
    if any(char in value for char in "\r\n\x00"):
        return "must be single-line"
    if not value.strip():
        return "must not be empty"
    if any(not segment.strip() for segment in value.split(TOPIC_SEPARATOR)):
        return "must not have empty segments, or a leading or trailing '/'"
    return None


def topic_key(value: str) -> tuple[str, ...]:
    """Case-insensitive identity of a topic path as its segment tuple."""
    return tuple(segment.strip().casefold() for segment in value.split(TOPIC_SEPARATOR))


def topic_within(topic: object, ancestor: str) -> bool:
    """True when `topic` is `ancestor` or below it, matching whole segments only."""
    if not isinstance(topic, str) or not topic:
        return False
    wanted = topic_key(ancestor)
    return topic_key(topic)[:len(wanted)] == wanted


def relative_date(value: str, today: date) -> date | None:
    """`today` minus an age like 0d, 2w, 3m, or 1y; months and years clamp the day. None otherwise."""
    match = re.fullmatch(r"(\d+)([dwmy])", value.strip().lower())
    if not match:
        return None
    count, unit = int(match[1]), match[2]
    if unit in "dw":
        try:
            return today - timedelta(days=count * (7 if unit == "w" else 1))
        except OverflowError:
            return None
    months = today.year * 12 + today.month - 1 - count * (12 if unit == "y" else 1)
    year, month = divmod(months, 12)
    if not 1 <= year <= 9999:
        return None
    return date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))


def topic_tree(counts: dict[str, int], suggested: list[str] | tuple[str, ...] = ()) -> list[dict[str, object]]:
    """Flat, depth-first topic nodes with implied parents and descendant counts.

    `counts` maps each card's topic to its number of cards. The first spelling
    seen (sorted) names a node. Suggested topics add zero-count nodes.
    """
    nodes: dict[tuple[str, ...], dict[str, object]] = {}
    for path, count in sorted([*counts.items(), *((item, 0) for item in suggested)]):
        parts = [segment.strip() for segment in path.split(TOPIC_SEPARATOR)]
        for depth in range(1, len(parts) + 1):
            key = tuple(part.casefold() for part in parts[:depth])
            node = nodes.setdefault(key, {
                "path": TOPIC_SEPARATOR.join(parts[:depth]), "name": parts[depth - 1],
                "depth": depth - 1, "count": 0, "own": 0,
            })
            node["count"] = int(node["count"]) + count
            if depth == len(parts):
                node["own"] = int(node["own"]) + count
    return [nodes[key] for key in sorted(nodes)]


def _validate_core_fields(
    properties: dict[str, object],
    *,
    kind: str | None,
    effective_kind: object,
    stored_kind: object,
) -> list[str]:
    """Validate required identity, timestamp, title, kind, and priority properties."""
    errors: list[str] = []
    required = COMMON_REQUIRED + (("status", "type") if effective_kind == "task" else ())
    for field in required:
        if field not in properties or properties[field] is None:
            errors.append(f"{field} is required")
    for field in ("id", "created", "updated"):
        value = properties.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be non-empty text")
        elif field in {"created", "updated"} and not is_metadata_time(value):
            errors.append(f"{field} must be an ISO date or local timestamp")
        elif field == "id" and any(char in value for char in "\r\n\x00"):
            errors.append("id must be single-line text")
    for field in ("title", "kind"):
        value = properties.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be non-empty text")
    title = properties.get("title")
    if isinstance(title, str) and any(char in title for char in "\r\n\x00"):
        errors.append("title must be single-line")
    if not isinstance(stored_kind, str) or stored_kind not in ("task", "note"):
        errors.append("kind must be task or note")
    priority = properties.get("priority")
    if not isinstance(priority, str) or not priority.strip() or any(char in priority for char in "\r\n\x00"):
        errors.append("priority must be non-empty single-line text")
    if kind is not None and stored_kind != kind:
        errors.append(f"kind property must be {kind!r} for this card location")
    return errors


def _validate_organization_fields(properties: dict[str, object], *, effective_kind: object) -> list[str]:
    """Validate project, topic, due, tags, task status/type, text blocks, flags, and completion."""
    errors: list[str] = []
    project = properties.get("project")
    if "project" in properties and project is not None and (
        not isinstance(project, str) or not project.strip() or any(char in project for char in "\r\n\x00")
    ):
        errors.append("project must be non-empty single-line text or null")
    topic = properties.get("topic")
    if "topic" in properties and topic is not None:
        if not isinstance(topic, str):
            errors.append("topic must be a '/'-separated path of text or null")
        else:
            error = topic_path_error(topic)
            if error is None and topic != normalize_topic(topic):
                error = "must not have spaces around '/' or at its ends"
            if error:
                errors.append(f"topic {error}")
    due = properties.get("due")
    if "due" in properties and due is not None and (not isinstance(due, str) or not is_metadata_time(due)):
        errors.append("due must be an ISO date or local timestamp or null")
    due_kind = properties.get("due_kind")
    if "due_kind" in properties and due_kind is not None and (
        not isinstance(due_kind, str) or not due_kind.strip() or any(char in due_kind for char in "\r\n\x00")
    ):
        errors.append("due_kind must be non-empty single-line text or null")
    if due is not None and due_kind is None:
        errors.append("due_kind is required when due is set")
    if due is None and due_kind is not None:
        errors.append("due_kind requires due")
    if "tags" in properties:
        tags = properties["tags"]
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) or not tag.strip() or any(char in tag for char in "\r\n\x00")
            for tag in tags
        ):
            errors.append("tags must be a list of non-empty strings")
    if effective_kind == "task":
        for field in ("status", "type"):
            value = properties.get(field)
            if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n\x00"):
                errors.append(f"{field} must be non-empty single-line text")
    for field in ("next_action", "processing_instructions"):
        if field in properties and not isinstance(properties[field], str):
            errors.append(f"{field} must be text")
    for field in BOOLEAN_METADATA:
        if field in properties and type(properties[field]) is not bool:
            errors.append(f"{field} must be true or false")
    if "completed" in properties and properties["completed"] is not None and (
        not isinstance(properties["completed"], str) or not is_metadata_time(properties["completed"])
    ):
        errors.append("completed must be an ISO date or local timestamp or null")
    return errors


def _validate_provenance_and_claims(properties: dict[str, object], *, stored_kind: object) -> list[str]:
    """Validate capture/GitHub provenance, claim fields, and task-only field exclusions on notes."""
    errors: list[str] = []
    if "capture_id" in properties:
        capture_id = properties["capture_id"]
        try:
            canonical_id = isinstance(capture_id, str) and str(uuid.UUID(capture_id)) == capture_id
        except ValueError:
            canonical_id = False
        if not canonical_id:
            errors.append("capture_id must be a canonical UUID")
    for field in GITHUB_SOURCE_FIELDS:
        if field in properties and not isinstance(properties[field], str):
            errors.append(f"{field} must be text")
    source, digest = (properties.get(field) for field in GITHUB_SOURCE_FIELDS)
    if source is not None or digest is not None:
        if not isinstance(source, str) or not isinstance(digest, str):
            errors.append("github_source and github_source_hash must be set together")
        else:
            if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/issues/\d+", source):
                errors.append("github_source must be a canonical GitHub issue URL")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                errors.append("github_source_hash must be a lowercase SHA-256 hash")
    has_owner = properties.get("claimed_by") is not None
    has_claim_time = properties.get("claimed_at") is not None
    if has_owner != has_claim_time:
        errors.append("claimed_by and claimed_at must be set together")
    if has_owner:
        owner, claimed_at = properties.get("claimed_by"), properties.get("claimed_at")
        if not isinstance(owner, str) or not owner.strip() or owner != owner.strip() or any(
            char in owner for char in "\r\n\x00"
        ):
            errors.append("claimed_by must be a non-empty single-line name")
        if not isinstance(claimed_at, str) or not is_timestamp(claimed_at):
            errors.append("claimed_at must be a local ISO-second timestamp")
    if stored_kind == "note":
        for field in sorted(TASK_ONLY_FIELDS):
            if field in properties:
                errors.append(f"notes cannot have task field {field}")
    return errors


def validate_properties(properties: dict[str, object], *, kind: str | None = None) -> list[str]:
    stored_kind = properties.get("kind")
    effective_kind = kind if kind is not None else stored_kind
    errors = _validate_core_fields(
        properties, kind=kind, effective_kind=effective_kind, stored_kind=stored_kind
    )
    errors.extend(_validate_organization_fields(properties, effective_kind=effective_kind))
    errors.extend(_validate_provenance_and_claims(properties, stored_kind=stored_kind))
    errors.extend(validate_records(properties))
    return errors


def require_valid_properties(properties: dict[str, object], *, kind: str | None = None) -> None:
    errors = validate_properties(properties, kind=kind)
    if errors:
        raise CardFormatError("; ".join(errors))


def is_timestamp(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_metadata_time(value: str) -> bool:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
        except ValueError:
            return False
    return is_timestamp(value)


def parse_document(raw: str, *, expected_kind: str | None = None) -> tuple[dict[str, object], str]:
    frontmatter, body = parse_frontmatter(raw)
    if frontmatter is None:
        raise CardFormatError("missing or malformed YAML frontmatter")
    properties = parse_yaml(frontmatter)
    require_valid_properties(properties, kind=expected_kind)
    return properties, body


def time_entries_from(properties: dict[str, object]) -> list[dict[str, object]]:
    """Validated time entries from already parsed card properties."""
    entries = properties.get("time_entries", [])
    if not isinstance(entries, list):
        raise ValueError("time_entries must be a list")
    errors = validate_records({"time_entries": entries})
    if errors:
        raise ValueError(errors[0])
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def time_log_entries(markdown: str | None) -> list[dict[str, object]]:
    if not markdown:
        return []
    properties, _body = parse_document(markdown)
    return time_entries_from(properties)


def _ordered_properties(properties: dict[str, object]) -> dict[str, object]:
    result = {key: properties[key] for key in FIELD_ORDER if key in properties}
    result.update((key, value) for key, value in properties.items() if key not in result)
    return result


def dump_properties(properties: dict[str, object]) -> str:
    return yaml.dump(
        _ordered_properties(properties), Dumper=_CardDumper, sort_keys=False,
        allow_unicode=True, default_flow_style=False, indent=2, width=1000,
        line_break="\n",
    )


def render_document(properties: dict[str, object], body: str, *, expected_kind: str | None = None) -> str:
    require_valid_properties(properties, kind=expected_kind)
    return f"---\n{dump_properties(properties)}---\n{body}"


def content_hash(raw: str | bytes) -> str:
    encoded = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def replace_property(raw: str, field: str, value: object, *, expected_kind: str | None = None) -> str:
    properties, body = parse_document(raw, expected_kind=expected_kind)
    properties[field] = value
    return render_document(properties, body, expected_kind=expected_kind)


# Keep references out of fenced code blocks. Headings are ordinary body text.
def fenced_line_numbers(markdown: str) -> set[int]:
    result: set[int] = set()
    fence_char: str | None = None
    fence_size = 0
    for number, line in enumerate(markdown.splitlines()):
        if fence_char is not None:
            result.add(number)
            if re.fullmatch(rf" {{0,3}}{re.escape(fence_char)}{{{fence_size},}}[ \t]*", line):
                fence_char, fence_size = None, 0
            continue
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opener and (opener.group(1)[0] != "`" or "`" not in opener.group(2)):
            fence_char, fence_size = opener.group(1)[0], len(opener.group(1))
            result.add(number)
    return result


TASK_CHECKBOX_RE = re.compile(r"^((?:[ \t]*>)*[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+)\[([ xX])\](?=[ \t]|$)")


def task_checkbox_spans(body: str) -> list[tuple[int, int, str]]:
    """Return (start, end, state) for each GFM task marker outside fenced code.

    start..end covers the bracketed marker, for example "[ ]".
    """
    fenced = fenced_line_numbers(body)
    spans: list[tuple[int, int, str]] = []
    offset = 0
    for number, line in enumerate(body.splitlines(keepends=True)):
        match = None if number in fenced else TASK_CHECKBOX_RE.match(line)
        if match:
            start = offset + match.end(1)
            spans.append((start, start + 3, match.group(2)))
        offset += len(line)
    return spans


def toggle_task_checkbox(body: str, index: int, count: int, checked: bool) -> str:
    """Flip one task checkbox to `checked`.

    The caller rendered the box in the opposite state. A different count or a
    box already in the requested state means the caller's view and the body
    disagree, so the edit is refused rather than applied to the wrong line.
    """
    spans = task_checkbox_spans(body)
    if len(spans) != count:
        raise ValueError("checklist changed; reload the card")
    if not 0 <= index < len(spans):
        raise ValueError("checkbox index out of range")
    start, end, state = spans[index]
    if (state != " ") == checked:
        raise ValueError("checklist changed; reload the card")
    return body[:start] + ("[x]" if checked else "[ ]") + body[end:]


EDITABLE_FIELDS = {
    "task": frozenset(EDITABLE_METADATA | {"status", "type"}),
    "note": frozenset(EDITABLE_METADATA - {"next_action"}),
}


def apply_property_patch(
    properties: dict[str, object], patch: dict[str, object], *, kind: str,
) -> dict[str, object]:
    """Apply one typed, semantic property patch without serializing a card.

    Callers parse the document once and render the returned properties once;
    the candidate transaction applies workspace policy. Null clears nullable
    fields, empty action/guidance clears the property, and
    clearing due also clears its due_kind.
    """
    if kind not in EDITABLE_FIELDS:
        raise CardFormatError("kind must be task or note")
    if not isinstance(patch, dict):
        raise CardFormatError("property patch must be a mapping")
    allowed = EDITABLE_FIELDS[kind]
    unsupported = set(patch) - allowed
    if unsupported:
        field = sorted(unsupported)[0]
        raise CardFormatError(f"field '{field}' cannot be set on a {kind}")

    changes = dict(patch)
    if "due" in changes and changes["due"] is None:
        if changes.get("due_kind") is not None:
            raise CardFormatError("due_kind requires due")
        changes["due_kind"] = None

    result = dict(properties)
    nullable_fields = {"project", "topic", "due", "due_kind"}
    for field, value in changes.items():
        current = properties.get(field)
        if field == "title":
            if isinstance(value, str):
                value = value.strip()
        elif field in nullable_fields:
            if value is None:
                if current is None:
                    continue
                result.pop(field, None)
                continue
            if field == "project" and isinstance(value, str):
                value = value.strip()
            elif field == "topic" and isinstance(value, str):
                value = normalize_topic(value)
        elif field == "tags":
            if value is None:
                value = []
            if isinstance(value, list) and all(isinstance(tag, str) for tag in value):
                value = list(dict.fromkeys(tag.strip() for tag in value))
            if value == (current or []):
                continue
        elif field in BOOLEAN_METADATA:
            if value is False and field not in properties:
                continue
        elif field in {"next_action", "processing_instructions"}:
            if value is None or value == "":
                if current in (None, ""):
                    continue
                result.pop(field, None)
                continue
        # Do not use Python equality to suppress assignments here: False == 0,
        # so an invalid numeric boolean could otherwise pass schema validation.
        result[field] = value
    require_valid_properties(result, kind=kind)
    return result
