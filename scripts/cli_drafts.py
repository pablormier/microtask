"""Draft management CLI commands for microtask."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

from cli_helpers import _cli_print_error, editor_command, resolve_unique
import drafts as draft_store
from operations import op_draft_save, task_defaults
import store
from store import (
    TaskError,
    _ws,
    is_timestamp,
    now_stamp,
)
from transaction import managed_temp_directory
from workspace import Workspace


def command_draft_list(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    values = draft_store.list_drafts(_ws(ws).root)
    if args.as_json:
        print(json.dumps({"drafts": values}, ensure_ascii=False))
        return 0
    if not values:
        print("No drafts.")
        return 0
    for draft in values:
        excerpt = " ".join(str(draft.get("body") or "").split())
        if len(excerpt) > 110:
            excerpt = excerpt[:107].rstrip() + "..."
        label = str(draft.get("title") or "").strip() or excerpt[:70] or "Untitled draft"
        target = f" → {draft.get('target')}" if draft.get("target") else ""
        print(f"{draft['id']}  {draft['kind']} {draft['mode']}{target}  {label}")
    return 0


def command_draft_recover(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    recovery = _ws(ws).recovery_dir()
    unresolved = [source.name for source in sorted(recovery.glob("*.md"))
                  if source.is_file() and not source.is_symlink()] if recovery.is_dir() else []
    if args.as_json:
        print(json.dumps({"recovered": [], "unresolved": unresolved}, ensure_ascii=False))
    elif unresolved:
        print("Legacy editor recovery copies need manual review:")
        for name in unresolved:
            print(f"  {name}")
    else:
        print("No editor recovery copies.")
    return 0


def command_draft_read(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    draft = draft_store.read(_ws(ws).root, args.draft_id)
    if args.as_json:
        print(json.dumps(draft, ensure_ascii=False))
    else:
        sys.stdout.write(str(draft.get("body") or ""))
    return 0


def command_draft_create(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    if args.title is not None and (not args.title.strip() or any(character in args.title for character in "\r\n")):
        print("error: draft title must be non-empty and single-line", file=sys.stderr)
        return 2
    fields: dict[str, object] = {"kind": args.kind, "mode": "new", "title": args.title or ""}
    body = args.section_text
    if args.target:
        item, result = resolve_unique(args.target, match_mode="exact", ws=resolved)
        if item is None:
            return result
        if (args.kind == "task") != item.is_task:
            print(f"error: {args.kind} drafts require a matching {args.kind} target", file=sys.stderr)
            return 2
        fields.update({
            "mode": "edit", "target": str(item.path.relative_to(resolved.root)),
            "base_hash": hashlib.sha256(item.raw.encode("utf-8")).hexdigest(),
            "base_updated": str(item.frontmatter.get("updated") or ""), "title": args.title or "",
            "origin": "card-body",
        })
        # Seed from the card unless --file supplies the proposed body.
        body = args.section_text if args.file is not None else item.body
    else:
        if args.created_at and not is_timestamp(args.created_at):
            print("error: --created-at must be a local ISO timestamp with seconds", file=sys.stderr)
            return 2
        capture_id = args.capture_id or str(uuid.uuid4())
        try:
            parsed_capture_id = uuid.UUID(capture_id)
        except (AttributeError, ValueError):
            print("error: --capture-id must be a valid UUID", file=sys.stderr)
            return 2
        if str(parsed_capture_id) != capture_id.lower():
            print("error: --capture-id must be a canonical UUID", file=sys.stderr)
            return 2
        fields.update({"capture_id": str(parsed_capture_id), "created": args.created_at or now_stamp()})
        if args.kind == "task":
            default_status, default_priority, default_type = task_defaults(store.policy(resolved))
            fields.update({
                "status": default_status, "priority": default_priority, "type": default_type,
                "project": None, "topic": None, "tags": [], "due": None, "due_kind": None,
            })
    try:
        draft = draft_store.save(
            resolved.root, draft_id=args.draft_id, fields=fields, body=body,
            expected_revision=0, now=now_stamp(),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(draft, ensure_ascii=False))
    else:
        print(f"Draft saved: {draft['id']}")
    return 0


def draft_conflict_copy(
    existing: dict[str, object], body: str, title: str, ws: Workspace | None = None,
) -> dict[str, object]:
    fields = {field: existing.get(field) for field in draft_store.FIELDS
              if field in existing and field not in {"id", "revision", "updated"}}
    fields["title"] = title
    return draft_store.save(
        _ws(ws).root, draft_id=None, fields=fields, body=body,
        expected_revision=0, now=now_stamp(),
    )


def command_draft_edit(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        existing = draft_store.read(resolved.root, args.draft_id)
        expected_revision = (args.expected_revision if args.expected_revision is not None
                             else int(existing["revision"]))
        if expected_revision != existing["revision"]:
            raise draft_store.DraftConflict("draft changed since it was loaded; refresh and retry")
        title = args.title if args.title is not None else str(existing.get("title") or "")
        if any(character in title for character in "\r\n"):
            raise ValueError("draft title must be single-line")
        if args.file is not None:
            if args.expected_revision is None:
                raise ValueError("--expected-revision is required when using --file")
            body = args.section_text
        else:
            command = editor_command("draft edit")
            with managed_temp_directory("draft-edit-", ws=resolved) as directory:
                editor_path = Path(directory) / "draft.md"
                with editor_path.open("w", encoding="utf-8", newline="") as editor_file:
                    editor_file.write(str(existing.get("body") or ""))
                try:
                    edited = subprocess.run([*command, str(editor_path)], cwd=resolved.root, check=False)
                except FileNotFoundError as error:
                    raise ValueError(f"editor not found: {command[0]}") from error
                with editor_path.open(encoding="utf-8", newline="") as editor_file:
                    body = editor_file.read()
                if edited.returncode != 0:
                    if body != existing.get("body"):
                        copy = draft_conflict_copy(existing, body, title, ws=resolved)
                        print(f"Editor exited with status {edited.returncode}; changes kept as draft {copy['id']}", file=sys.stderr)
                    else:
                        print(f"Editor exited with status {edited.returncode}; original draft was kept", file=sys.stderr)
                    return edited.returncode or 1
                if body == existing.get("body") and title == str(existing.get("title") or ""):
                    print("No changes: draft was not updated.")
                    return 0
        fields = {field: existing.get(field) for field in draft_store.FIELDS
                  if field in existing and field not in {"id", "revision", "updated"}}
        fields["title"] = title
        try:
            updated = draft_store.save(
                resolved.root, draft_id=args.draft_id, fields=fields, body=body,
                expected_revision=expected_revision, expected_content_hash=str(existing["content_hash"]),
                now=now_stamp(),
            )
        except draft_store.DraftConflict:
            copy = draft_conflict_copy(existing, body, title, ws=resolved)
            raise draft_store.DraftConflict(
                f"draft changed since it was loaded; edits were kept as draft {copy['id']}"
            )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(updated, ensure_ascii=False))
    else:
        print(f"Draft saved: {updated['id']} revision {updated['revision']}")
    return 0


def command_draft_discard(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        existing = draft_store.read(resolved.root, args.draft_id)
        removed = draft_store.discard(
            resolved.root, args.draft_id, args.expected_revision,
            expected_content_hash=str(existing["content_hash"]),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not removed:
        print("error: draft not found", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps({"discarded": True, "id": args.draft_id}))
    else:
        print("Draft discarded.")
    return 0


def command_draft_save(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    try:
        result = op_draft_save(
            ws,
            args.draft_id,
            expected_revision=args.expected_revision,
            expected_hash=args.expected_hash,
        )
    except TaskError as error:
        return _cli_print_error(error)
    receipt = dict(result.receipt)
    if result.sync_error:
        receipt["sync_error"] = result.sync_error
    if args.as_json:
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        print(result.message)
    return 0
