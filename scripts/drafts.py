"""Workspace-owned, revision-guarded Markdown drafts."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from datetime import datetime

import card_format


ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FIELDS = (
    "id", "kind", "mode", "revision", "created", "updated", "title", "target",
    "base_hash", "base_updated", "capture_id", "origin", "status", "priority",
    "type", "project", "topic", "tags", "due", "due_kind", "instructions",
)
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
# Older recovery copies may not contain the target version they were opened
# against. This reserved digest makes their edit drafts explicitly unbased.
UNVERIFIED_BASE_HASH = "0" * 64


class DraftConflict(ValueError):
    pass


def draft_directory(root: Path) -> Path:
    directory = root / "drafts"
    if directory.is_symlink():
        raise ValueError("drafts/ must not be a symbolic link")
    return directory


def draft_path(root: Path, draft_id: str) -> Path:
    if not isinstance(draft_id, str) or not ID_RE.fullmatch(draft_id):
        raise ValueError("draft ID must be a canonical UUID")
    directory = draft_directory(root)
    path = directory / f"{draft_id}.md"
    if path.parent.resolve() != directory.resolve():
        raise ValueError("draft path is outside drafts/")
    if path.is_symlink():
        raise ValueError("draft file must not be a symbolic link")
    return path


def lock(root: Path):
    control = root / ".mt"
    if control.is_symlink():
        raise ValueError(".mt/ must not be a symbolic link")
    control.mkdir(parents=True, exist_ok=True)
    lock_path = control / "drafts.lock"
    if lock_path.is_symlink():
        raise ValueError("draft lock must not be a symbolic link")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def unlock(handle) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def serialize(metadata: dict[str, object], body: str) -> bytes:
    header = card_format.dump_properties({field: metadata[field] for field in FIELDS if field in metadata})
    return f"---\n{header}---\n{body}".encode("utf-8")


def read_path(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"draft file must not be a symbolic link: {path}")
    raw = path.read_bytes().decode("utf-8")
    frontmatter, body = card_format.parse_frontmatter(raw)
    if frontmatter is None:
        raise ValueError(f"malformed draft: {path}")
    metadata = card_format.parse_yaml(frontmatter)
    draft_id = metadata.get("id")
    if not isinstance(draft_id, str) or not ID_RE.fullmatch(draft_id) or path.stem != draft_id:
        raise ValueError(f"malformed draft ID: {path}")
    kind = metadata.get("kind")
    if not isinstance(kind, str) or kind not in {"note", "task"}:
        raise ValueError(f"{path}: malformed draft kind; expected note or task")
    mode = metadata.get("mode")
    if not isinstance(mode, str) or mode not in {"new", "edit"}:
        raise ValueError(f"{path}: malformed draft mode; expected new or edit")
    revision = metadata.get("revision")
    if isinstance(revision, str) and re.fullmatch(r"[1-9][0-9]*", revision):
        revision = int(revision)
        metadata["revision"] = revision
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError(f"malformed draft revision: {path}")
    if not isinstance(metadata.get("title"), str) or any(char in metadata["title"] for char in "\r\n"):
        raise ValueError(f"malformed draft title: {path}")
    for field in ("created", "updated"):
        value = metadata.get(field)
        if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
            raise ValueError(f"malformed draft {field}: {path}")
        try:
            datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"malformed draft {field}: {path}") from error
    mode = metadata["mode"]
    kind = metadata["kind"]
    if mode == "new":
        capture_id = metadata.get("capture_id")
        try:
            parsed_capture_id = uuid.UUID(capture_id) if isinstance(capture_id, str) else None
        except ValueError:
            parsed_capture_id = None
        if parsed_capture_id is None or str(parsed_capture_id) != capture_id.lower():
            raise ValueError(f"malformed new capture draft identity: {path}")
    else:
        target, base_hash = metadata.get("target"), metadata.get("base_hash")
        if not isinstance(target, str) or not target or not isinstance(base_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", base_hash):
            raise ValueError(f"malformed edit draft target: {path}")
        if metadata.get("origin") != "card-body":
            raise ValueError(f"{path}: edit draft uses a retired card format; create a fresh edit draft")
    origin = metadata.get("origin")
    if origin is not None and not isinstance(origin, str):
        raise ValueError(f"malformed draft origin: {path}")
    instructions = metadata.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ValueError(f"malformed draft instructions: {path}")
    metadata["body"] = body
    metadata["path"] = str(path)
    metadata["content_hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return metadata


def list_drafts(root: Path) -> list[dict[str, object]]:
    directory = draft_directory(root)
    if not directory.is_dir():
        return []
    return [read_path(path) for path in sorted(directory.glob("*.md"))]


def save(
    root: Path,
    *,
    draft_id: str | None,
    fields: dict[str, object],
    body: str,
    expected_revision: int | None,
    now: str,
    expected_content_hash: str | None = None,
) -> dict[str, object]:
    if not isinstance(fields, dict):
        raise ValueError("draft fields must be a mapping")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ValueError("expected_revision must be a non-negative integer or null")
    if not isinstance(body, str):
        raise ValueError("draft body must be a string")
    try:
        body.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("draft body must be valid UTF-8") from error
    draft_id = draft_id or str(uuid.uuid4())
    path = draft_path(root, draft_id)
    directory = draft_directory(root)
    if directory.exists() and not directory.is_dir():
        raise ValueError("drafts exists but is not a directory")
    lock_handle = lock(root)
    try:
        previous = read_path(path) if path.is_file() else None
        kind = fields.get("kind", previous.get("kind") if previous else None)
        mode = fields.get("mode", previous.get("mode") if previous else None)
        if not isinstance(kind, str) or kind not in {"note", "task"}:
            raise ValueError("draft kind must be note or task")
        if not isinstance(mode, str) or mode not in {"new", "edit"}:
            raise ValueError("draft mode must be new or edit")
        if previous is None:
            if expected_revision not in (None, 0):
                raise DraftConflict("draft changed since it was loaded; refresh and retry")
            revision = 1
            created = str(fields.get("created") or now)
        else:
            if expected_revision == 0 and fields.get("origin") and fields.get("origin") == previous.get("origin"):
                if body == previous.get("body") and str(fields.get("title") or "") == str(previous.get("title") or ""):
                    return previous
                raise DraftConflict("an import with this origin already exists; local changes were retained")
            if expected_revision is None or expected_revision != previous["revision"]:
                raise DraftConflict("draft changed since it was loaded; refresh and retry")
            if expected_content_hash is None or expected_content_hash != previous["content_hash"]:
                raise DraftConflict("draft content changed outside microtask; refresh and reconcile")
            for field in ("mode", "target", "base_hash", "base_updated", "capture_id"):
                if field in fields and fields[field] != previous.get(field):
                    raise ValueError(f"draft field '{field}' cannot change after creation")
            if previous["mode"] != "new" and fields.get("kind", previous.get("kind")) != previous.get("kind"):
                raise ValueError("draft kind cannot change after creation")
            revision = int(previous["revision"]) + 1
            created = str(previous.get("created") or now)

        if previous and previous["mode"] == "edit" and kind != previous.get("kind"):
            raise ValueError("edit draft kind cannot change after creation")
        title = fields.get("title", previous.get("title", "") if previous else "")
        if not isinstance(title, str) or any(char in title for char in "\r\n"):
            raise ValueError("draft title must be single-line text")
        if mode == "new":
            capture_id = fields.get("capture_id", previous.get("capture_id") if previous else None)
            created_value = fields.get("created", previous.get("created") if previous else now)
            try:
                parsed_capture_id = uuid.UUID(capture_id) if isinstance(capture_id, str) else None
            except ValueError:
                parsed_capture_id = None
            if parsed_capture_id is None or str(parsed_capture_id) != capture_id.lower():
                raise ValueError("new note draft capture ID must be a canonical UUID")
            if not isinstance(created_value, str) or not TIMESTAMP_RE.fullmatch(created_value):
                raise ValueError("new capture draft created time must be a local ISO timestamp with seconds")
            try:
                datetime.fromisoformat(created_value)
            except ValueError as error:
                raise ValueError("new capture draft created time is invalid") from error
        else:
            target = fields.get("target", previous.get("target") if previous else None)
            base_hash = fields.get("base_hash", previous.get("base_hash") if previous else None)
            if not isinstance(target, str) or not target or not isinstance(base_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", base_hash):
                raise ValueError("edit drafts require a target and its SHA-256 base hash")
            origin = fields.get("origin", previous.get("origin") if previous else None)
            if origin != "card-body":
                raise ValueError("edit drafts must store a logical Markdown body with origin card-body")
        metadata: dict[str, object] = {
            "id": draft_id,
            "kind": kind,
            "mode": mode,
            "revision": revision,
            "created": created,
            "updated": now,
            "title": title,
            "target": fields.get("target", previous.get("target") if previous else None),
            "base_hash": fields.get("base_hash", previous.get("base_hash") if previous else None),
            "base_updated": fields.get("base_updated", previous.get("base_updated") if previous else None),
            "capture_id": fields.get("capture_id", previous.get("capture_id") if previous else None),
            "origin": fields.get("origin", previous.get("origin") if previous else None),
        }
        for field in ("status", "priority", "type", "project", "topic", "tags", "due", "due_kind", "instructions"):
            if field in fields:
                metadata[field] = fields[field]
            elif previous and field in previous:
                metadata[field] = previous[field]
        instructions = metadata.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("draft instructions must be text or null")
        if isinstance(instructions, str):
            try:
                instructions.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("draft instructions must be valid UTF-8") from error
        if previous and not fields.get("title") and "title" not in fields:
            metadata["title"] = previous.get("title", "")
        atomic_write(path, serialize(metadata, body))
        return read_path(path)
    finally:
        unlock(lock_handle)


def discard(
    root: Path,
    draft_id: str,
    expected_revision: int | None = None,
    expected_content_hash: str | None = None,
) -> bool:
    path = draft_path(root, draft_id)
    lock_handle = lock(root)
    try:
        if not path.is_file():
            return False
        current = read_path(path)
        if expected_revision is not None and current["revision"] != expected_revision:
            raise DraftConflict("draft changed since it was loaded; refresh and retry")
        if expected_content_hash is not None and current["content_hash"] != expected_content_hash:
            raise DraftConflict("draft content changed outside microtask; refresh and reconcile")
        path.unlink()
        return True
    finally:
        unlock(lock_handle)


def import_source(root: Path, *, origin: str, fields: dict[str, object], body: str, now: str) -> dict[str, object]:
    """Import once by origin. The original recovery source stays in place."""
    for draft in list_drafts(root):
        if draft.get("origin") == origin:
            return draft
    return save(root, draft_id=None, fields={**fields, "origin": origin}, body=body,
                expected_revision=0, now=now)


def read(root: Path, draft_id: str) -> dict[str, object]:
    return read_path(draft_path(root, draft_id))

