"""Card lifecycle, edit, bulk-edit, query, focus, and topic CLI commands."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time as clock_time
import uuid

import card_format
import card_query
from cli_helpers import (
    _cli_print_error,
    _cli_run_write,
    create_editor_recovery_copy,
    editor_command,
    resolve_unique,
)
import operations
from operations import (
    _apply_bulk_metadata,
    _bulk_edit_assignments,
    _bulk_plan_item,
    _comment_locked,
    _commit_item_change,
    _complete_locked,
    _edit_locked,
    _move_locked,
    _plan_query,
    capture_card,
    capture_title,
    card_text_option,
    check_claim,
    cleanup_lifecycle_timer,
    create_card,
    finish_lifecycle_timer,
    focus_context,
    followup_sort_key,
    parse_tag_json,
    render_converted_note,
    render_converted_task,
    suggested_topics,
    task_defaults,
    topic_rename_plan,
    transition_claim,
    workspace_topic_tree,
)
import store
from store import (
    RESTART_COMMANDS,
    Card,
    CardInputError,
    ReferenceIndex,
    TaskError,
    _ws,
    active_root,
    command_query,
    current_username,
    demo_active,
    discover,
    done_window,
    is_due_value,
    is_timestamp,
    item_compact_json_record,
    item_json_record,
    item_read_record,
    item_receipt,
    item_state_suffix,
    now_stamp,
    open_statuses,
    saved_receipt,
    select_cards,
    sort_card_items,
    time_log_entries,
    write_focus_state,
    write_focus_task_id,
    clear_focus_task_id,
)
import transaction
from transaction import (
    MutationReceipt,
    auto_sync_after_mutation,
    read_time_timers,
    run_auto_sync_for_receipt,
    serialized_time_mutation,
    time_mutation_lock,
    validate_item_changes,
)
from workspace import Workspace


@serialized_time_mutation
def command_create(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    kind = getattr(args, "kind", "task")
    as_json = getattr(args, "as_json", False)
    if getattr(args, "capture", False):
        try:
            card, changed = capture_card(args, " ".join(args.text), kind, ws=resolved)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    else:
        next_action = card_text_option(args, "next_action")
        text = " ".join(args.text)
        try:
            card, changed = create_card(
                kind, text, title=getattr(args, "title", None) or capture_title(text),
                status=getattr(args, "status", None), task_type=getattr(args, "task_type", None),
                next_action=next_action, instructions=card_text_option(args, "instructions"),
                priority=getattr(args, "priority", None), project=getattr(args, "project", None),
                topic=getattr(args, "topic", None), due=getattr(args, "due", None),
                due_kind=getattr(args, "due_kind", None), tags=getattr(args, "tags", None),
                needs_review=bool(getattr(args, "needs_review", False)),
                archived=bool(getattr(args, "archived", False)), hidden=bool(getattr(args, "hidden", False)),
                by=getattr(args, "by", None), ws=resolved,
            )
        except CardInputError as error:
            print(f"error: invalid capture: {error}", file=sys.stderr)
            return 2
        except (OSError, ValueError) as error:
            print(f"error: generated item failed validation; creation rolled back: {error}", file=sys.stderr)
            return 1
    if as_json:
        auto_sync_after_mutation(changed=changed, ws=resolved)
        return MutationReceipt(saved_receipt(card, updated=changed))
    print(card.path)
    return auto_sync_after_mutation(changed=changed, ws=resolved)


def command_add(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    """Create a card; a note with no text or --file opens the human-only editor."""
    if args.kind == "note" and args.file is None and not args.text:
        return command_note_editor(args, ws=ws)
    return command_create(args, ws=ws)


@serialized_time_mutation
def save_terminal_capture(fields: dict, ws: Workspace | None = None) -> int:
    """Lock only the final write, using the same transaction as dashboard capture."""
    resolved = _ws(ws)

    def optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value if value and value != "-" else None

    normalized = dict(fields)
    for key in ("title", "project", "topic", "due", "due_kind"):
        normalized[key] = optional_text(normalized.get(key))
    args = argparse.Namespace(**normalized, capture_id=str(uuid.uuid4()), created_at=now_stamp())
    try:
        item, changed = capture_card(args, normalized["text"], fields["kind"], ws=resolved)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Created {item_receipt(item, ws=resolved)['ref']}: {item.path}")
    return auto_sync_after_mutation(changed=changed, ws=resolved)


def command_capture(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    import capture_cli

    resolved = _ws(ws)
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        print("error: capture requires an interactive terminal; use mt add or mt note with text or --file -", file=sys.stderr)
        return 2

    def edit_text(text: str) -> str:
        try:
            command = editor_command("capture")
        except ValueError as error:
            print(f"Cannot open editor: {error}")
            return text
        recovery = create_editor_recovery_copy("terminal-capture", text.encode("utf-8"), ws=resolved)
        try:
            result = subprocess.run([*command, str(recovery)], cwd=resolved.root, check=False)
        except (OSError, KeyboardInterrupt) as error:
            print(f"Editor interrupted or unavailable: {error}; copy preserved at {recovery}")
            return text
        if result.returncode:
            print(f"Editor failed; copy preserved at {recovery}")
            return text
        edited = recovery.read_text(encoding="utf-8")
        recovery.unlink()
        return edited

    try:
        cfg = store.policy(resolved)
        fields = capture_cli.collect(cfg, task_defaults(cfg), " ".join(args.text), edit_text, is_due_value)
    except (KeyboardInterrupt, EOFError):
        print("\nCapture cancelled.")
        return 0
    if fields is None:
        print("Capture cancelled.")
        return 0
    return save_terminal_capture(fields, ws=resolved)


def sync_error_after_json_save(ws: Workspace | None = None) -> str | None:
    resolved = _ws(ws)
    if not store.policy(resolved).get("rules", {}).get("auto_sync", False):
        return None
    return run_auto_sync_for_receipt(resolved)


def command_note_editor(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        command = editor_command("note")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    template = "### Discussion\n\n### Decisions\n\n### Actions\n" if args.template == "meeting" else ""
    timestamp = now_stamp()  # Capture creation time when the editor opens.
    try:
        draft_path = create_editor_recovery_copy("new-note", template.encode("utf-8"), ws=resolved)
    except OSError as error:
        print(f"error: could not prepare note draft: {error}", file=sys.stderr)
        return 1
    keep_draft = False
    try:
        keep_draft = True
        try:
            edited = subprocess.run([*command, str(draft_path)], cwd=resolved.root, check=False)
        except FileNotFoundError:
            keep_draft = False
            print(f"error: editor not found: {command[0]}", file=sys.stderr)
            return 1
        if edited.returncode != 0:
            print(f"error: editor exited with status {edited.returncode}; note was not saved", file=sys.stderr)
            return edited.returncode
        candidate = draft_path.read_bytes().decode("utf-8")
        unchanged_template = template and (
            [line.strip() for line in candidate.splitlines() if line.strip()] ==
            [line.strip() for line in template.splitlines() if line.strip()]
        )
        if unchanged_template or not candidate.strip():
            keep_draft = False
            print("No changes: note was not saved.")
            return 0
        args.capture_timestamp = timestamp
        try:
            with time_mutation_lock():
                note_item, changed = capture_card(args, candidate, "note")
        except (OSError, ValueError, RuntimeError) as error:
            print(f"error: note was not saved: {error}", file=sys.stderr)
            return 1
        keep_draft = False
        if getattr(args, "as_json", False):
            receipt = saved_receipt(note_item, updated=changed)
            sync_error = sync_error_after_json_save() if changed else None
            if sync_error:
                receipt["sync_error"] = sync_error
            print(json.dumps(receipt, ensure_ascii=False))
            return 0
        print(note_item.path)
        return auto_sync_after_mutation(changed=changed)
    except (OSError, UnicodeError) as error:
        print(f"error: could not read edited note draft: {error}", file=sys.stderr)
        return 1
    finally:
        if keep_draft:
            print(f"Edited copy preserved for recovery: {draft_path}", file=sys.stderr)
        else:
            draft_path.unlink(missing_ok=True)


def command_done(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_run_write(
        ws,
        lambda resolved: _complete_locked(
            resolved,
            " ".join(args.query),
            notes=args.notes,
            comments=args.comments,
            by=getattr(args, "by", None),
            match_mode=args.match_mode,
        ),
        as_json=args.as_json,
    )


def command_move(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_run_write(
        ws,
        lambda resolved: _move_locked(
            resolved,
            " ".join(args.query),
            args.destination,
            comments=args.comments,
            takeover=getattr(args, "takeover", False),
            by=getattr(args, "by", None),
            match_mode=args.match_mode,
        ),
        as_json=getattr(args, "as_json", False),
    )


def command_comment(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_run_write(
        ws,
        lambda resolved: _comment_locked(
            resolved,
            " ".join(args.query),
            args.comments,
            match_mode=args.match_mode,
        ),
        as_json=args.as_json,
    )


@serialized_time_mutation
def command_convert(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    query = " ".join(args.query).strip()
    if not query:
        print("error: convert requires a non-empty query", file=sys.stderr)
        return 2
    to_note = args.destination == "note"
    if to_note and any(getattr(args, name, None) is not None for name in ("status", "task_type", "next_action")):
        print("error: --status, --type, and --next-action apply only when converting a note to a task", file=sys.stderr)
        return 2
    item, result = resolve_unique(
        query, kind=None if to_note else "note",
        statuses=open_statuses(resolved) if to_note else None,
        match_mode=args.match_mode,
        ws=resolved,
    )
    if item is None:
        return result
    if item.kind == args.destination:
        print(f"error: convert {args.destination} destination requires a "
              f"{'task' if to_note else 'note'} source", file=sys.stderr)
        return 2
    now = now_stamp()
    try:
        if to_note:
            if item.frontmatter.get("claimed_by"):
                raise ValueError("release the task claim before converting it to a note")
            task_id = str(item.frontmatter.get("id") or item.path.stem)
            if time_log_entries(item.raw) or task_id in read_time_timers(resolved):
                raise ValueError("a task with tracked time or a running timer cannot be converted to a note")
            updated = render_converted_note(item, now)
        else:
            updated = render_converted_task(
                item, now, status_override=getattr(args, "status", None),
                type_override=getattr(args, "task_type", None),
                next_action_override=getattr(args, "next_action", None),
            )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return _commit_item_change(
        item, updated,
        failure_prefix="conversion failed validation; change rolled back",
        summary=f"Converted to {args.destination}: {item.path.relative_to(resolved.root)}",
        as_json=args.as_json,
        receipt_fn=lambda saved, updated=True: item_receipt(saved, updated=updated, ws=resolved),
        ws=resolved,
    )


def parse_edit_tags(value: str) -> list[str]:
    try:
        return parse_tag_json(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def command_edit(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _command_edit_serialized(args, ws=ws)


def command_card_metadata(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    """Apply a card metadata patch with a whole-card compare-and-swap."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_hash or ""):
        print("error: expected hash must be a 64-character SHA-256 digest", file=sys.stderr)
        return 2
    try:
        patch = json.loads(args.patch)
    except (TypeError, json.JSONDecodeError) as error:
        print(f"error: metadata patch must be JSON: {error}", file=sys.stderr)
        return 2
    if not isinstance(patch, dict) or not patch:
        print("error: metadata patch contains unsupported or missing fields", file=sys.stderr)
        return 2
    resolved = _ws(ws)
    try:
        result = operations.op_card_metadata(
            resolved,
            args.ref,
            patch,
            expected_hash=args.expected_hash,
            kind=args.kind,
            match_mode="exact",
            sync=False,
        )
    except TaskError as error:
        return _cli_print_error(error)
    if args.as_json:
        print(json.dumps(result.receipt, ensure_ascii=False))
    else:
        print(f"Updated {result.receipt['kind']} properties: {result.receipt['ref']}")
    return auto_sync_after_mutation(changed=bool(result.receipt["changed"]), ws=resolved)


def _edit_body_mode_from_args(args: argparse.Namespace) -> str | None:
    if not args.body:
        return None
    if args.old is not None:
        return "replace_pairs"
    if args.replace_file is not None:
        return "replace_file"
    if args.append:
        return "append"
    return "replace"


def _edit_metadata_from_args(args: argparse.Namespace) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if args.tags is not None:
        metadata["tags"] = args.tags
    for argument, field in (("project", "project"), ("topic", "topic"),
                            ("priority", "priority"), ("task_type", "type"),
                            ("due", "due"), ("due_kind", "due_kind"),
                            ("hidden", "hidden"), ("archived", "archived"),
                            ("needs_review", "needs_review"), ("status", "status")):
        value = getattr(args, argument, None)
        if value is not None:
            metadata[field] = value
    if args.title is not None:
        metadata["title"] = args.title
    return metadata


def _command_edit_serialized(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_run_write(
        ws,
        lambda resolved: _edit_locked(
            resolved,
            " ".join(args.query),
            match_mode=args.match_mode,
            expected_hash=getattr(args, "expected_hash", None),
            body_mode=_edit_body_mode_from_args(args),
            body_text=getattr(args, "file_text", ""),
            body_replacements=list(zip(args.old, args.new)) if args.old is not None else None,
            exact_replacements_text=getattr(args, "replacements_text", None),
            metadata_fields=_edit_metadata_from_args(args),
            assignments=args.assignments or (),
            tag_add=getattr(args, "tag_add", None),
            tag_remove=getattr(args, "tag_remove", None),
            by=getattr(args, "by", None),
        ),
        as_json=args.as_json,
    )


@serialized_time_mutation
def command_delete(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    query = " ".join(args.query).strip()
    if not query:
        print("error: delete requires a non-empty query", file=sys.stderr)
        return 2
    task, result = resolve_unique(query, match_mode=args.match_mode, ws=resolved)
    if task is None:
        return result
    if task.is_task:
        try:
            check_claim(task, current_username(getattr(args, "by", None), ws=resolved))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        try:
            timers = read_time_timers(resolved)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        task_id = str(task.frontmatter.get("id") or task.path.stem)
        if task_id in timers:
            print("error: a task with a running timer cannot be deleted", file=sys.stderr)
            return 1
    base = active_root(resolved)
    relative = task.path.relative_to(base).as_posix()
    original = task.path.read_bytes()
    try:
        transaction.apply_item_transaction({}, deletions={relative}, expected={relative: original}, source_root=base)
    except (OSError, ValueError) as error:
        print(f"error: delete failed validation; change rolled back: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        receipt = item_receipt(task)
        receipt["deleted"] = True
        receipt["changed"] = True
        auto_sync_after_mutation()
        return MutationReceipt(receipt)
    print(f"Deleted: {task.path}")
    return auto_sync_after_mutation()


def _run_mdviz(resolved_ws: Workspace, target: Path, args: argparse.Namespace) -> int:
    tools_dir = str(resolved_ws.app_root / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import mdviz

    mdviz_args = [str(target)]
    if args.out:
        mdviz_args.extend(["--out", str(args.out)])
    if args.no_open:
        mdviz_args.append("--no-open")
    return mdviz.main(mdviz_args)


def command_show(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved_ws = _ws(ws)
    query = " ".join(args.query).strip()
    candidate = Path(query).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_ws.root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_ws.root / ".mt")
    except (FileNotFoundError, ValueError):
        resolved = None
    if resolved is not None and resolved.is_file() and resolved.suffix == ".md":
        return _run_mdviz(resolved_ws, resolved, args)
    task, result = resolve_unique(query, match_mode=args.match_mode, ws=resolved_ws)
    if task is None:
        return result
    return _run_mdviz(resolved_ws, task.path, args)


def _read_item_data(task: Card, args: argparse.Namespace) -> tuple[dict[str, object] | None, str | None]:
    if args.as_json:
        return item_read_record(task), None
    if getattr(args, "body", False):
        return None, task.body
    return None, task.raw


def command_read(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    is_multi_ref = len(args.query) > 1 and all(q.startswith("@") for q in args.query)
    if is_multi_ref:
        tasks = []
        for ref in args.query:
            task, result = resolve_unique(ref, match_mode="exact", ws=resolved)
            if task is None:
                return result
            tasks.append(task)
        if args.as_json:
            records = []
            for task in tasks:
                rec, _ = _read_item_data(task, args)
                if rec is None:
                    return 2
                records.append(rec)
            print(json.dumps(records, ensure_ascii=False))
            return 0
        for task in tasks:
            _, text = _read_item_data(task, args)
            if text is None:
                return 2
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
        return 0

    query = " ".join(args.query).strip()
    task, result = resolve_unique(query, match_mode=args.match_mode, ws=resolved)
    if task is None:
        return result
    rec, text = _read_item_data(task, args)
    if rec is None and text is None:
        return 2
    if args.as_json:
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    assert text is not None
    sys.stdout.write(text)
    return 0


def _bulk_preview(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    query = command_query(args, ws=resolved)
    if query is None:
        return 2
    if query == card_query.DEFAULT:
        print("error: bulk-edit requires a selection such as --status, --kind, or another filter", file=sys.stderr)
        return 2
    try:
        assignments = _bulk_edit_assignments(args.assignments or [])
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    base = active_root(resolved)
    matching = select_cards(discover(ws=resolved), query)
    if not matching:
        print("error: no cards match the bulk-edit selection", file=sys.stderr)
        return 2
    allowed = set.intersection(*(set(card_format.EDITABLE_FIELDS[item.kind]) for item in matching)) if matching else set()
    invalid_for_kind = sorted(set(assignments) - allowed) if matching else []
    if invalid_for_kind:
        kinds = ", ".join(sorted({item.kind for item in matching}))
        print(f"error: field(s) {', '.join(invalid_for_kind)} are not available on every selected {kinds} card", file=sys.stderr)
        return 2
    timestamp = now_stamp()
    try:
        records = []
        changes: dict[str, bytes] = {}
        for item in matching:
            record, contents = _bulk_plan_item(item, base, assignments, timestamp)
            records.append(record)
            if contents is not None:
                changes[str(Path(str(record["path"])).as_posix())] = contents
        if changes:
            validate_item_changes(changes, source_root=base)
    except (OSError, ValueError) as error:
        prefix = "conflict" if "conflict:" in str(error) else "invalid bulk edit"
        print(f"error: {error}" if prefix == "conflict" else f"error: {prefix}: {error}", file=sys.stderr)
        return 1 if prefix == "conflict" else 2
    plan = {
        "version": 1,
        "workspace": str(resolved.root.resolve()),
        "data_root": "demo" if demo_active(resolved) else "workspace",
        "assignments": assignments,
        "query": card_query.query_to_mapping(query),
        "selected": len(records),
        "changed": sum(bool(record["changed"]) for record in records),
        "unchanged": sum(not bool(record["changed"]) for record in records),
        "items": records,
    }
    if args.as_json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    changed = sum(bool(record["changed"]) for record in records)
    unchanged = len(records) - changed
    print(f"Bulk edit preview: {len(records)} selected; {changed} would change; {unchanged} already set.")
    for record in records:
        status = "change" if record["changed"] else "no change"
        details = ", ".join(
            f"{field}: {record['before'][field]!r} -> {record['after'][field]!r}"
            for field in assignments
        )
        print(f"  {record['ref']} {record['title']} ({status}; {details})")
    print("Save this preview with --json, review it, then run 'mt bulk-edit --apply PLAN.json'.")
    return 0


def _read_bulk_plan(path_text: str, ws: Workspace | None = None) -> dict[str, object]:
    resolved = _ws(ws)
    if path_text == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path_text).expanduser().read_text(encoding="utf-8")
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"bulk edit plan is not valid JSON: {error}") from error
    if isinstance(plan, dict) and "selection" in plan:
        raise ValueError("this plan was made by an older mt; run the preview again")
    if not isinstance(plan, dict) or set(plan) != {
        "version", "workspace", "data_root", "assignments", "query", "selected", "changed", "unchanged", "items"
    }:
        raise ValueError("bulk edit plan has an unsupported structure")
    if type(plan["version"]) is not int or plan["version"] != 1:
        raise ValueError("unsupported bulk edit plan version")
    if not isinstance(plan["workspace"], str) or not isinstance(plan["data_root"], str):
        raise ValueError("bulk edit plan has invalid workspace metadata")
    if plan["workspace"] != str(resolved.root.resolve()):
        raise ValueError("bulk edit plan belongs to a different workspace")
    if plan["data_root"] not in {"demo", "workspace"}:
        raise ValueError("bulk edit plan has an invalid data root")
    expected_root = "demo" if demo_active(resolved) else "workspace"
    if plan["data_root"] != expected_root:
        raise ValueError("workspace data changed since preview; create a new bulk edit plan")
    assignments_raw = plan["assignments"]
    if not isinstance(assignments_raw, dict):
        raise ValueError("bulk edit plan assignments must be an object")
    assignments = _bulk_edit_assignments(assignments_raw)
    if not isinstance(plan["items"], list) or not plan["items"]:
        raise ValueError("bulk edit plan must contain at least one selected card")
    _plan_query(plan)
    if any(type(plan[field]) is not int or plan[field] < 0 for field in ("selected", "changed", "unchanged")):
        raise ValueError("bulk edit plan has invalid selection counts")
    records = plan["items"]
    if any(not isinstance(entry, dict) or type(entry.get("changed")) is not bool for entry in records):
        raise ValueError("bulk edit plan contains a malformed card record")
    changed_count = sum(entry["changed"] for entry in records)
    if (
        plan["selected"] != len(records)
        or plan["changed"] != changed_count
        or plan["unchanged"] != len(records) - changed_count
    ):
        raise ValueError("bulk edit plan selection counts do not match its card records")
    plan["assignments"] = assignments
    return plan


def _apply_bulk_plan_locked(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved_ws = _ws(ws)
    try:
        plan = _read_bulk_plan(args.apply_plan, ws=resolved_ws)
        assignments = plan["assignments"]
        assert isinstance(assignments, dict)
        base = active_root(resolved_ws)
        current_items = select_cards(discover(ws=resolved_ws), _plan_query(plan))
        current_members = {item.path.relative_to(base).as_posix() for item in current_items}
        planned_members = {
            entry.get("path") for entry in plan["items"]
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        if current_members != planned_members:
            raise ValueError("conflict: bulk-edit selection membership changed since preview")
        expected: dict[str, bytes] = {}
        changes: dict[str, bytes] = {}
        receipts = []
        finished_timers = []
        seen: set[str] = set()
        timestamp = now_stamp()
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
            root = store.cards_root(resolved_ws)
            try:
                resolved = card_path.resolve(strict=True)
                resolved.relative_to(root.resolve())
            except (OSError, ValueError) as error:
                raise ValueError(f"bulk edit plan path is outside the current {kind} cards: {relative_text}") from error
            if card_path.is_symlink() or resolved.parent != root.resolve():
                raise ValueError(f"bulk edit plan path is outside the current {kind} cards: {relative_text}")
            original = card_path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            if not isinstance(entry["content_hash"], str) or digest != entry["content_hash"]:
                raise ValueError(f"conflict: {relative_text} changed since the bulk edit preview")
            item = Card.read(card_path)
            if entry["ref"] != f"@{item.short_ref}" or entry["title"] != item.title:
                raise ValueError(f"conflict: {relative_text} no longer matches the preview")
            if set(assignments) - set(card_format.EDITABLE_FIELDS[kind]):
                raise ValueError(f"bulk edit field is not available on {kind} {relative_text}")
            before = {field: item.frontmatter.get(field) for field in assignments}
            raw = _apply_bulk_metadata(item.raw, kind, assignments, timestamp)
            parsed = card_format.parse_yaml(card_format.parse_frontmatter(raw)[0] or "")
            after = {field: parsed.get(field) for field in assignments}
            changed = raw.encode("utf-8") != original
            if (
                type(entry["changed"]) is not bool
                or entry["changed"] is not changed
                or entry["before"] != before
                or entry["after"] != after
            ):
                raise ValueError(f"conflict: {relative_text} no longer matches the previewed metadata change")
            expected[relative_text] = original
            if changed:
                if item.is_task and set(assignments) & {"status", "archived", "hidden"}:
                    username = current_username(getattr(args, "by", None), ws=resolved_ws)
                    check_claim(item, username)
                    if parsed.get("status") != item.frontmatter.get("status"):
                        raw = transition_claim(item, raw, str(parsed["status"]), username, timestamp)
                    if parsed.get("status") != "doing" or parsed.get("archived") is True or parsed.get("hidden") is True:
                        raw, timer = finish_lifecycle_timer(item, raw, int(clock_time.time()), ws=resolved_ws)
                        finished_timers.append((item, timer))
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
            for item, timer in finished_timers:
                cleanup_lifecycle_timer(item, timer, ws=resolved_ws)
    except (OSError, ValueError) as error:
        prefix = "conflict" if "conflict:" in str(error) else "bulk edit failed validation; changes rolled back"
        print(f"error: {error}" if prefix == "conflict" else f"error: {prefix}: {error}", file=sys.stderr)
        return 1
    if args.as_json:
        auto_sync_after_mutation(changed=bool(changes), ws=resolved_ws)
        return MutationReceipt({
            "saved": True,
            "selected": len(receipts),
            "changed": changed_count,
            "unchanged": unchanged_count,
            "items": receipts,
        })
    print(f"Bulk edit applied: {changed_count} changed; {unchanged_count} already set.")
    auto_sync_after_mutation(changed=bool(changes), ws=resolved_ws)
    return 0


@serialized_time_mutation
def _command_bulk_edit_apply(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _apply_bulk_plan_locked(args, ws=ws)


def command_bulk_edit(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    if args.apply_plan is not None:
        try:
            explicit = card_query.query_from_args(args, env={})
        except card_query.QueryError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        if explicit != card_query.DEFAULT or args.assignments:
            print("error: --apply cannot be combined with a new selection or --set", file=sys.stderr)
            return 2
        return _command_bulk_edit_apply(args, ws=ws)
    return _bulk_preview(args, ws=ws)


def _run_card_query(args: argparse.Namespace, *, search: bool, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    if args.limit is not None and args.limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if args.tail is not None and args.tail < 1:
        print("error: --tail must be a positive integer", file=sys.stderr)
        return 2
    query = command_query(args)
    if query is None:
        return 2
    if search and not query.text.strip():
        print("error: search requires a non-empty query", file=sys.stderr)
        return 2
    all_items = discover(ws=resolved)
    done_cutoff = None if search else card_query.default_done_cutoff(query, done_window(resolved), date.today())
    selected = select_cards(all_items, query, done_cutoff=done_cutoff)
    relevance_scores = (
        {item.path: card_query.text_score(item, query.text, query.match) for item in selected}
        if search else None
    )
    priorities = list(store.policy(resolved).get("priorities", {}).keys())
    rank = {name: index for index, name in enumerate(priorities)}
    selected = sort_card_items(
        selected, field=args.sort, order=args.order,
        priority_rank=rank, relevance_scores=relevance_scores,
    )
    if args.limit is not None:
        selected = selected[:args.limit]
    elif args.tail is not None:
        selected = selected[-args.tail:]

    if getattr(args, "compact", False):
        for item in selected:
            print(json.dumps(item_compact_json_record(item, ws=resolved), ensure_ascii=False))
        return 0
    if args.full:
        print(json.dumps([item_read_record(item, ws=resolved) for item in selected], ensure_ascii=False))
        return 0
    if args.json:
        reference_index = ReferenceIndex(all_items)
        print(json.dumps([item_json_record(item, reference_index, ws=resolved) for item in selected],
                         ensure_ascii=False, indent=2))
        return 0

    for item in selected:
        status = f" [{item.frontmatter['status']}]" if item.is_task else ""
        priority = str(item.frontmatter.get("priority") or "-")
        claim = f" [claimed: {item.frontmatter['claimed_by']}]" if item.frontmatter.get("claimed_by") else ""
        fuzzy_suffix = (
            f" (fuzzy {relevance_scores[item.path]:.0%})"
            if search and relevance_scores is not None and query.match == "fuzzy" else ""
        )
        print(f"[@{item.short_ref}] [{item.kind}]{status} [{priority}] {item.title}"
              f"{item_state_suffix(item)}{claim}{fuzzy_suffix}")
    return 0


def command_list(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _run_card_query(args, search=False, ws=ws)


def command_topic_list(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    tree = workspace_topic_tree(discover(ws=resolved), store.policy(resolved))
    start, end, depth = 0, len(tree), 0  # `tree` is depth-first, so a topic's subtree is the run after it
    if args.topic:
        # Accept "learning/" as printed by the one-level listing.
        wanted = card_format.topic_key(args.topic.strip().rstrip(card_format.TOPIC_SEPARATOR))
        found = [index for index, node in enumerate(tree) if card_format.topic_key(str(node["path"])) == wanted]
        if not found:
            print(f"error: unknown topic: {args.topic}", file=sys.stderr)
            return 1
        start, depth = found[0], int(tree[found[0]]["depth"])
        end = start + 1
        while end < len(tree) and int(tree[end]["depth"]) > depth:
            end += 1
    subtree = tree[start:end]  # the topic itself (when given) and everything below it
    if args.as_json:
        print(json.dumps({"topics": subtree}, ensure_ascii=False))
        return 0
    if args.paths:
        for node in subtree:
            print(node["path"])
        return 0
    if not tree:
        print("No topics yet. Set one with 'mt edit REF --topic PATH'.")
        return 0
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"

    def line(prefix: str, label: str, index: int) -> str:
        node = subtree[index]
        has_children = index + 1 < len(subtree) and int(subtree[index + 1]["depth"]) > int(node["depth"])
        text = f"{prefix}{label}{'/' if has_children and not args.tree else ''} ({node['count']})"
        return f"\033[2m{text}\033[0m" if color and not node["count"] else text

    if not args.tree:
        level = depth + 1 if args.topic else 0  # one level: the roots, or the children of TOPIC
        for index, node in enumerate(subtree):
            if int(node["depth"]) == level:
                print(line("", str(node["name"]), index))
        return 0
    # --tree: a node is last when no later node sits at its depth before the subtree climbs higher.
    base = depth if args.topic else 0
    for index, node in enumerate(subtree):
        level = int(node["depth"]) - base
        if level == 0:
            print(line("", str(node["path"]) if args.topic else str(node["name"]), index))
            continue
        siblings_after = [later for later in subtree[index + 1:]
                          if int(later["depth"]) <= int(node["depth"])]
        is_last = not siblings_after or int(siblings_after[0]["depth"]) < int(node["depth"])
        prefix = ""
        for ancestor_level in range(1, level):
            ancestor_depth = base + ancestor_level
            later = [item for item in subtree[index + 1:] if int(item["depth"]) <= ancestor_depth]
            prefix += "│   " if later and int(later[0]["depth"]) == ancestor_depth else "    "
        print(line(prefix + ("└── " if is_last else "├── "), str(node["name"]), index))
    return 0


@serialized_time_mutation
def command_topic_rename(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        plan = topic_rename_plan(args.old, args.new, args.merge, ws=resolved)
        username = current_username(getattr(args, "by", None))
        claimed = []
        for card, _new_topic in plan:
            try:
                check_claim(card, username)
            except ValueError:
                claimed.append(f"@{card.short_ref} (claimed by {card.frontmatter.get('claimed_by')})")
        if claimed:
            raise CardInputError(
                f"cannot rename: claimed by someone else: {', '.join(claimed)}. "
                f"Release the claim or pass --by with the claiming username; nothing was changed")
        base = active_root(resolved)
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
        if not args.dry_run and changes:
            transaction.apply_item_transaction(changes, expected=expected, source_root=base, ws=resolved)
    except CardInputError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"error: topic rename failed; changes rolled back: {error}", file=sys.stderr)
        return 1
    saved = not args.dry_run
    if args.as_json:
        payload = {"saved": saved, "dry_run": args.dry_run, "selected": len(records),
                   "changed": len(changes), "items": records}
        if saved:
            auto_sync_after_mutation(changed=bool(changes), ws=resolved)
            return MutationReceipt(payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    verb = "Would move" if args.dry_run else "Moved"
    for record in records:
        print(f"{record['ref']}  {record['from']} -> {record['to']}  {record['title']}")
    print(f"{verb} {len(changes)} card(s) from '{card_format.normalize_topic(args.old)}' to '{card_format.normalize_topic(args.new)}'.")
    if any(card_format.topic_key(item)[:len(card_format.topic_key(args.old))] == card_format.topic_key(args.old)
           for item in suggested_topics(store.policy(resolved))):
        print("Note: suggested_topics in the workspace policy still lists the old name; update it by hand.")
    return auto_sync_after_mutation(changed=bool(changes), ws=resolved) if saved else 0


def command_focus(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    query = " ".join(getattr(args, "query", []) or []).strip()
    set_query = (getattr(args, "set_query", None) or "").strip()
    if query and set_query:
        print("error: use either a focus query or --set, not both", file=sys.stderr)
        return 2
    if args.next and (query or set_query):
        print("error: --next cannot be combined with a focus query or --set", file=sys.stderr)
        return 2
    if args.clear and (query or set_query):
        print("error: --clear cannot be combined with a focus query or --set", file=sys.stderr)
        return 2
    if args.clear and args.next:
        print("error: --clear cannot be combined with --next", file=sys.stderr)
        return 2
    selection = command_query(args)
    if selection is None:
        return 2
    if (query or set_query or args.clear) and card_query.query_from_args(args, env={}) != card_query.DEFAULT:
        print("error: set and clear focus operations cannot be combined with filters", file=sys.stderr)
        return 2
    actor = current_username(getattr(args, "by", None))
    if args.clear:
        clear_focus_task_id(resolved)
    elif query or set_query:
        task, result = resolve_unique(query or set_query, statuses=open_statuses(resolved), ws=resolved)
        if task is None:
            return result
        if task.frontmatter.get("status") not in {"inbox", "active", "doing"}:
            print("error: focus requires an inbox, active, or doing task; read blocked or deferred work first", file=sys.stderr)
            return 2
        check_claim(task, actor)
        write_focus_task_id(str(task.frontmatter.get("id") or task.path.stem), ws=resolved)
    open_query = card_query.narrow(selection, "status", open_statuses(resolved))
    open_tasks = select_cards(discover(ws=resolved), open_query) if open_query else []
    context = focus_context(open_tasks, actor=actor, ws=resolved)
    candidates = context["candidates"]
    if args.next:
        if candidates:
            current_id = context["selected"]["id"]
            current_index = next(index for index, item in enumerate(candidates) if item["id"] == current_id)
            selected_task = candidates[(current_index + 1) % len(candidates)]
            write_focus_state(str(selected_task["id"]), "cycle", ws=resolved)
            context = focus_context(open_tasks, actor=actor, ws=resolved)
    if getattr(args, "json", False):
        print(json.dumps(context, ensure_ascii=False, indent=2))
        return 0
    selected = context["selected"]
    if selected:
        status = str(selected["status"] or "")
        command = RESTART_COMMANDS.get(status, RESTART_COMMANDS["default"]).format(ref=selected["ref"])
        print("Focus:")
        print(f"  [{selected['ref']}] [{selected['priority']:<7}] {selected['title']}")
        print(f"       due:     {selected['due'] or '-'}")
        print(f"       next:    {selected['next_action'] or '-'}")
        print(f"       why:     {selected['reason']}")
        print(f"       restart: {command}")
    else:
        print("No open tasks to focus on.")
    alerts = [item for item in context["alerts"] if not selected or item["id"] != selected["id"]]
    if alerts:
        print(f"\nDeadlines to check ({len(alerts)}):")
        for item in alerts:
            print(f"  [{item['ref']}] {item['due']} | {item['title']} | {item['action_label']}")
    return 0


def command_unfocus(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    from cli_parser import parser

    resolved = _ws(ws)
    clear_focus_task_id(resolved)
    return command_focus(parser().parse_args(["focus"]), ws=resolved)


def command_followup(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    selection = command_query(args)
    if selection is None:
        return 2
    priorities = list(store.policy(resolved).get("priorities", {}).keys())
    rank = {name: index for index, name in enumerate(priorities)}
    waiting = card_query.narrow(selection, "status", ["waiting"])
    tasks = select_cards(discover(ws=resolved), waiting) if waiting else []
    tasks = sorted(tasks, key=lambda task: followup_sort_key(task, rank))
    if not tasks:
        print("No waiting follow-ups.")
        return 0
    print(f"Follow-ups ({len(tasks)} waiting):\n")
    for task in tasks:
        priority = str(task.frontmatter.get("priority") or "medium")
        due = str(task.frontmatter.get("due") or "-")
        ref = f"@{task.short_ref}"
        print(f"  [{ref}] [{priority:<7}] {task.title}")
        print(f"       due:  {due}")
        print(f"       next: {task.next_action or '-'}")
        print(f"       cmds: mt read {ref}")
    return 0


def command_search(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _run_card_query(args, search=True, ws=ws)


def _validate_edit_args(root_parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.file is not None and not args.body:
        root_parser.error("--file requires --body")
    if (args.old is None) != (args.new is None) or len(args.old or ()) != len(args.new or ()):
        root_parser.error(f"--old and --new must be given the same number of times "
                          f"(got {len(args.old or ())} --old, {len(args.new or ())} --new)")
    if sum(value is not None for value in (args.old, args.replace_file, args.file)) > 1:
        root_parser.error("--old/--new, --replace-file, and --file are mutually exclusive body operations")
    if args.append and (not args.body or args.file is None):
        root_parser.error("--append requires --body --file")
    if args.replace_file is not None or args.old is not None:
        args.body = True
    if args.body and args.file is None and args.replace_file is None and args.old is None:
        root_parser.error("--body requires --file, --old/--new, or --replace-file")
    if args.tags is not None and (args.tag_add or args.tag_remove):
        root_parser.error("--tags cannot be combined with --tag or --remove-tag")
    deterministic = bool(args.assignments or args.body or args.replace_file is not None or args.old is not None
                         or args.title is not None or args.tags is not None
                         or any(getattr(args, field, None) is not None for field in (
                             "project", "topic", "priority", "task_type", "due", "due_kind",
                             "hidden", "archived", "needs_review", "tag_add", "tag_remove", "status",
                         )))
    if not deterministic:
        root_parser.error("edit requires --title, --tags, --set, or a body operation")
    if args.title is not None:
        if not args.title.strip() or any(c in args.title for c in "\r\n\x00"):
            root_parser.error("--title must be non-empty and single-line")
        args.title = args.title.strip()
    if args.expected_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_hash):
        root_parser.error("--expected-hash must be a 64-character SHA-256 hex digest")
    if args.file is not None and not args.append and not args.expected_hash:
        root_parser.error("body replacement requires --expected-hash from read --json")
    if args.expected_hash and not deterministic:
        root_parser.error("--expected-hash requires an edit operation")
    if args.as_json and not deterministic:
        root_parser.error("--json requires a deterministic edit")


def _validate_capture_args(root_parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.file is not None and args.text:
        root_parser.error("capture text and --file cannot be combined")
    has_input = args.file is not None or bool(args.text)
    if args.kind == "note":
        if any(getattr(args, name, None) is not None
               for name in ("status", "task_type", "next_action", "next_action_file", "by")):
            root_parser.error("--status, --type, --next-action, --next-action-file, and --by are task-only options")
    else:
        if args.template:
            root_parser.error("--template applies only to notes")
        if not has_input:
            root_parser.error("add requires text or --file")
    if args.capture and not has_input:
        root_parser.error("--capture requires text or --file")
    if args.template and has_input:
        root_parser.error("--template cannot be combined with capture text or --file")
    if not has_input and (args.instructions is not None or args.instructions_file is not None):
        root_parser.error("--instructions requires text or --file")
    if args.capture_id is not None and not args.capture:
        root_parser.error("--capture-id requires --capture")
    if args.created_at is not None and not args.capture:
        root_parser.error("--created-at requires --capture")
    if args.capture_id is not None and args.created_at is None:
        root_parser.error("--capture-id requires --created-at")
    if args.created_at is not None and args.capture_id is None:
        root_parser.error("--created-at requires --capture-id")
    if args.capture_id is not None:
        try:
            uuid.UUID(args.capture_id)
        except ValueError:
            root_parser.error("--capture-id must be a valid UUID")
        if not is_timestamp(args.created_at or ""):
            root_parser.error("--created-at must be a valid timestamp in YYYY-MM-DDTHH:MM:SS format")
    if args.instructions is not None and args.instructions_file is not None:
        root_parser.error("--instructions and --instructions-file cannot be combined")
    if args.next_action is not None and args.next_action_file is not None:
        root_parser.error("--next-action and --next-action-file cannot be combined")
    if args.title is not None:
        if not args.title.strip() or "\n" in args.title or "\r" in args.title:
            root_parser.error("--title must be non-empty and single-line")
        args.title = args.title.strip()
