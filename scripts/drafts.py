"""Workspace-owned, hash-guarded Markdown drafts."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import re
import sys
import uuid

import card_format


ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FIELDS = (
    "id", "kind", "mode", "created", "updated", "title",
    "capture_id", "origin", "status", "priority", "type", "project",
    "topic", "tags", "due", "due_kind", "instructions",
)


class DraftConflict(ValueError):
    pass


class RetiredDraftError(ValueError):
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
    if mode == "edit":
        raise RetiredDraftError(
            f"{path}: retired edit draft; apply or delete it manually (card edits no longer use drafts)"
        )
    if mode != "new":
        raise ValueError(f"{path}: malformed draft mode; expected new")
    if not isinstance(metadata.get("title"), str) or any(char in metadata["title"] for char in "\r\n"):
        raise ValueError(f"malformed draft title: {path}")
    for field in ("created", "updated"):
        value = metadata.get(field)
        if not isinstance(value, str) or not card_format.is_timestamp(value):
            raise ValueError(f"malformed draft {field}: {path}")
    capture_id = metadata.get("capture_id")
    try:
        parsed_capture_id = uuid.UUID(capture_id) if isinstance(capture_id, str) else None
    except ValueError:
        parsed_capture_id = None
    if parsed_capture_id is None or str(parsed_capture_id) != capture_id.lower():
        raise ValueError(f"malformed new capture draft identity: {path}")
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
    items: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.md")):
        try:
            items.append(read_path(path))
        except RetiredDraftError as error:
            print(f"warning: skipping {error}", file=sys.stderr)
    return items


def save(
    root: Path,
    *,
    draft_id: str | None,
    fields: dict[str, object],
    body: str,
    now: str,
    expected_content_hash: str | None = None,
) -> dict[str, object]:
    if not isinstance(fields, dict):
        raise ValueError("draft fields must be a mapping")
    if expected_content_hash is not None and (not isinstance(expected_content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_content_hash)):
        raise ValueError("expected_content_hash must be a SHA-256 hash or null")
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
        if mode != "new":
            raise ValueError("draft mode must be new")
        if previous is None:
            if expected_content_hash is not None:
                raise DraftConflict("draft changed since it was loaded; refresh and retry")
            created = str(fields.get("created") or now)
        else:
            if expected_content_hash is None and fields.get("origin") and fields.get("origin") == previous.get("origin"):
                if body == previous.get("body") and str(fields.get("title") or "") == str(previous.get("title") or ""):
                    return previous
                raise DraftConflict("an import with this origin already exists; local changes were retained")
            if expected_content_hash is None or expected_content_hash != previous["content_hash"]:
                raise DraftConflict("draft content changed outside microtask; refresh and reconcile")
            for field in ("mode", "capture_id"):
                if field in fields and fields[field] != previous.get(field):
                    raise ValueError(f"draft field '{field}' cannot change after creation")
            created = str(previous.get("created") or now)

        title = fields.get("title", previous.get("title", "") if previous else "")
        if not isinstance(title, str) or any(char in title for char in "\r\n"):
            raise ValueError("draft title must be single-line text")
        capture_id = fields.get("capture_id", previous.get("capture_id") if previous else None)
        created_value = fields.get("created", previous.get("created") if previous else now)
        try:
            parsed_capture_id = uuid.UUID(capture_id) if isinstance(capture_id, str) else None
        except ValueError:
            parsed_capture_id = None
        if parsed_capture_id is None or str(parsed_capture_id) != capture_id.lower():
            raise ValueError("new note draft capture ID must be a canonical UUID")
        if not isinstance(created_value, str) or not card_format.is_timestamp(created_value):
            raise ValueError("new capture draft created time must be a valid local ISO timestamp with seconds")
        metadata: dict[str, object] = {
            "id": draft_id,
            "kind": kind,
            "mode": mode,
            "created": created,
            "updated": now,
            "title": title,
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
    expected_content_hash: str | None = None,
) -> bool:
    path = draft_path(root, draft_id)
    lock_handle = lock(root)
    try:
        if not path.is_file():
            return False
        try:
            current = read_path(path)
        except RetiredDraftError:
            if expected_content_hash is not None:
                raise
            path.unlink()
            return True
        if expected_content_hash is not None and current["content_hash"] != expected_content_hash:
            raise DraftConflict("draft content changed outside microtask; refresh and reconcile")
        path.unlink()
        return True
    finally:
        unlock(lock_handle)


def read(root: Path, draft_id: str) -> dict[str, object]:
    return read_path(draft_path(root, draft_id))

