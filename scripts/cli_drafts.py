"""Draft management CLI commands for microtask."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from cli_helpers import _cli_call_op, _cli_print_error, editor_command, emit_json
import drafts as draft_store
from operations import op_draft_create, op_draft_discard, op_draft_edit, op_draft_save
from store import TaskError, _ws, now_stamp
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
        owner = f"  (owner {draft['owner']})" if draft.get("owner") else ""
        print(f"{draft['id']}  {draft['kind']} {draft['mode']}  {label}{owner}")
    return 0


def command_draft_read(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    draft = draft_store.read(_ws(ws).root, args.draft_id)
    if args.as_json:
        print(json.dumps(draft, ensure_ascii=False))
    else:
        sys.stdout.write(str(draft.get("body") or ""))
    return 0


def command_draft_create(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    try:
        draft = op_draft_create(
            ws,
            args.kind,
            draft_id=args.draft_id,
            title=args.title,
            body=getattr(args, "file_text", ""),
            capture_id=args.capture_id,
            created=args.created_at,
        )
    except TaskError as error:
        return _cli_print_error(error)
    if args.as_json:
        emit_json(draft)
    else:
        print(f"Draft saved: {draft['id']}")
    return 0


def draft_conflict_copy(
    existing: dict[str, object], body: str, title: str, ws: Workspace | None = None,
) -> dict[str, object]:
    fields = {field: existing.get(field) for field in draft_store.FIELDS
              if field in existing and field not in {"id", "updated"}}
    fields["title"] = title
    return draft_store.save(
        _ws(ws).root, draft_id=None, fields=fields, body=body,
        now=now_stamp(),
    )


def command_draft_edit(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        existing = draft_store.read(resolved.root, args.draft_id)
        expected_hash = (args.expected_hash if args.expected_hash is not None
                         else str(existing["content_hash"]))
        if expected_hash != existing["content_hash"]:
            raise draft_store.DraftConflict("draft content changed outside microtask; refresh and reconcile")
        title = args.title if args.title is not None else str(existing.get("title") or "")
        if any(character in title for character in "\r\n"):
            raise ValueError("draft title must be single-line")
        if args.file is not None:
            if args.expected_hash is None:
                raise ValueError("--expected-hash is required when using --file")
            body = getattr(args, "file_text", "")
        elif args.title is not None:
            body = str(existing.get("body") or "")
            if title == str(existing.get("title") or ""):
                if args.as_json:
                    print(json.dumps({"saved": False, "changed": False, "id": args.draft_id, "reason": "no_changes"}, ensure_ascii=False))
                else:
                    print("No changes: draft was not updated.")
                return 0
        else:
            if not sys.stdin.isatty():
                print("error: interactive draft editor requires a terminal; pass --file or --title", file=sys.stderr)
                return 2
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
                    if args.as_json:
                        print(json.dumps({"saved": False, "changed": False, "id": args.draft_id, "reason": "no_changes"}, ensure_ascii=False))
                    else:
                        print("No changes: draft was not updated.")
                    return 0
        fields = {field: existing.get(field) for field in draft_store.FIELDS
                  if field in existing and field not in {"id", "updated"}}
        fields["title"] = title
        try:
            updated = op_draft_edit(
                resolved, args.draft_id, fields=fields, body=body, expected_hash=expected_hash,
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
        print(f"Draft saved: {updated['id']}")
    return 0


def command_draft_discard(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        try:
            existing = draft_store.read(resolved.root, args.draft_id)
            expected_hash: str | None = (args.expected_hash if args.expected_hash is not None
                                         else str(existing["content_hash"]))
        except draft_store.RetiredDraftError:
            if args.expected_hash is not None:
                raise
            expected_hash = None
        removed = op_draft_discard(resolved, args.draft_id, expected_hash=expected_hash)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not removed:
        print("error: draft not found", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps({"discarded": True, "id": args.draft_id}, ensure_ascii=False))
    else:
        print("Draft discarded.")
    return 0


def command_draft_assign(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    """Give a draft to a user of a shared workspace; drafts without an owner are hidden from them."""
    try:
        draft = draft_store.assign(_ws(ws).root, args.draft_id, args.owner)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(draft, ensure_ascii=False))
    else:
        print(f"Draft {draft['id']} now belongs to {args.owner}.")
    return 0


def command_draft_save(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_call_op(
        lambda: op_draft_save(
            ws,
            args.draft_id,
            expected_hash=args.expected_hash,
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )
