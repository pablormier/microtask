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

import attachments
import card_format
import card_query
from cli_helpers import (
    _cli_call_op,
    _cli_print_error,
    command_query,
    create_editor_recovery_copy,
    editor_command,
    emit_json,
    resolve_unique,
)
from operations import (
    _bulk_edit_assignments,
    _bulk_plan_item,
    _plan_query,
    capture_title,
    op_bulk_edit,
    op_comment,
    op_complete,
    op_convert,
    op_create,
    op_delete,
    op_edit,
    op_focus,
    op_move,
    op_store_attachment,
    op_topic_rename,
    parse_tag_json,
    suggested_topics,
    task_defaults,
    workspace_topic_tree,
)
import store
from store import (
    RESTART_COMMANDS,
    Card,
    CardInputError,
    ReferenceIndex,
    _ws,
    current_username,
    discover,
    done_window,
    is_due_value,
    is_timestamp,
    item_compact_json_record,
    item_json_record,
    item_outline_json_record,
    item_read_record,
    item_receipt,
    item_state_suffix,
    now_stamp,
    open_statuses,
    saved_receipt,
    select_cards,
    sort_card_items,
)
from transaction import validate_item_changes
from workspace import Workspace


def card_text_option(args: argparse.Namespace, name: str) -> str:
    """Read an optional text property supplied inline or from a --*-file option."""
    value = str(getattr(args, f"{name}_text", None) or getattr(args, name, "") or "")
    return value if value.strip() else ""


def command_create(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    kind = getattr(args, "kind", "task")
    as_json = getattr(args, "as_json", False)
    text = " ".join(args.text)
    is_capture = bool(getattr(args, "capture", False))
    needs_review = getattr(args, "needs_review", None)
    return _cli_call_op(
        lambda: op_create(
            ws,
            kind,
            text,
            title=getattr(args, "title", None) if is_capture else (getattr(args, "title", None) or capture_title(text)),
            capture_id=getattr(args, "capture_id", None) if is_capture else None,
            created_at=getattr(args, "created_at", None) if is_capture else None,
            status=getattr(args, "status", None) if kind == "task" else None,
            task_type=getattr(args, "task_type", None) if kind == "task" else None,
            next_action=card_text_option(args, "next_action") if kind == "task" else "",
            instructions=card_text_option(args, "instructions"),
            priority=getattr(args, "priority", None),
            project=getattr(args, "project", None),
            topic=getattr(args, "topic", None),
            due=getattr(args, "due", None),
            due_kind=getattr(args, "due_kind", None),
            tags=getattr(args, "tags", None),
            needs_review=((kind == "note") if needs_review is None else bool(needs_review)) if is_capture else bool(needs_review),
            archived=bool(getattr(args, "archived", False)),
            hidden=bool(getattr(args, "hidden", False)),
            by=getattr(args, "by", None) if kind == "task" else None,
            quiet_sync=as_json,
        ),
        as_json=as_json,
    )


def command_add(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    """Create a card; a note with no text or --file opens the human-only editor."""
    if args.kind == "note" and args.file is None and not args.text:
        return command_note_editor(args, ws=ws)
    return command_create(args, ws=ws)


def save_terminal_capture(fields: dict, ws: Workspace | None = None) -> int:
    """Lock only the final write, using the same transaction as dashboard capture."""
    def optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value if value and value != "-" else None

    normalized = dict(fields)
    for key in ("title", "project", "topic", "due", "due_kind"):
        normalized[key] = optional_text(normalized.get(key))
    try:
        result = op_create(
            ws,
            normalized["kind"],
            normalized["text"],
            title=normalized.get("title"),
            capture_id=str(uuid.uuid4()),
            created_at=now_stamp(),
            status=normalized.get("status"),
            task_type=normalized.get("task_type"),
            next_action=str(normalized.get("next_action") or ""),
            instructions=str(normalized.get("instructions") or ""),
            priority=normalized.get("priority"),
            project=normalized.get("project"),
            topic=normalized.get("topic"),
            due=normalized.get("due"),
            due_kind=normalized.get("due_kind"),
            tags=normalized.get("tags"),
            by=normalized.get("by"),
            quiet_sync=False,
        )
    except store.TaskError as error:
        return _cli_print_error(error)
    assert result.card is not None
    print(f"Created {result.receipt['ref']}: {result.card.path}")
    return result.sync_exit_code


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


def command_note_editor(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        command = editor_command("note")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("error: interactive note editor requires a terminal; pass --file or inline text", file=sys.stderr)
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
            if getattr(args, "as_json", False):
                print(json.dumps({"saved": False, "changed": False, "reason": "no_changes"}, ensure_ascii=False))
            else:
                print("No changes: note was not saved.")
            return 0
        as_json = getattr(args, "as_json", False)
        exit_code = _cli_call_op(
            lambda: op_create(
                resolved,
                "note",
                candidate,
                title=getattr(args, "title", None),
                capture_timestamp=timestamp,
                instructions=card_text_option(args, "instructions"),
                priority=getattr(args, "priority", None),
                project=getattr(args, "project", None),
                topic=getattr(args, "topic", None),
                due=getattr(args, "due", None),
                due_kind=getattr(args, "due_kind", None),
                tags=getattr(args, "tags", None),
                needs_review=getattr(args, "needs_review", None),
                archived=bool(getattr(args, "archived", False)),
                hidden=bool(getattr(args, "hidden", False)),
                quiet_sync=as_json,
            ),
            as_json=as_json,
        )
        keep_draft = exit_code != 0
        return exit_code
    except (OSError, UnicodeError) as error:
        print(f"error: could not read edited note draft: {error}", file=sys.stderr)
        return 1
    finally:
        if keep_draft:
            print(f"Edited copy preserved for recovery: {draft_path}", file=sys.stderr)
        else:
            draft_path.unlink(missing_ok=True)


def command_done(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_call_op(
        lambda: op_complete(
            ws,
            " ".join(args.query),
            notes=args.notes,
            comments=args.comments,
            by=getattr(args, "by", None),
            match_mode=args.match_mode,
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )


def command_move(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    as_json = getattr(args, "as_json", False)
    return _cli_call_op(
        lambda: op_move(
            ws,
            " ".join(args.query),
            args.destination,
            comments=args.comments,
            takeover=getattr(args, "takeover", False),
            by=getattr(args, "by", None),
            match_mode=args.match_mode,
            quiet_sync=as_json,
        ),
        as_json=as_json,
    )


def command_comment(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_call_op(
        lambda: op_comment(
            ws,
            " ".join(args.query),
            args.comments,
            by=getattr(args, "by", None),
            match_mode=args.match_mode,
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )


def command_attach(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    source = Path(args.source)
    if not source.is_file():
        print(f"error: attachment file not found: {args.source}", file=sys.stderr)
        return 2
    try:
        size = source.stat().st_size
        policy = store.policy(_ws(ws))
        if size > attachments.max_attachment_bytes(policy):
            limit_mb = policy.get("rules", {}).get("max_attachment_mb", attachments.DEFAULT_MAX_ATTACHMENT_MB)
            print(f"error: attachment exceeds max_attachment_mb ({limit_mb} MB)", file=sys.stderr)
            return 2
        data = source.read_bytes()
    except OSError as error:
        print(f"error: could not read attachment file: {error}", file=sys.stderr)
        return 1
    card_ref = " ".join(args.card).strip() if args.card else None
    return _cli_call_op(
        lambda: op_store_attachment(
            ws,
            data,
            filename=source.name,
            card_ref=card_ref or None,
            match_mode=args.match_mode,
            label=args.label,
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )


def command_convert(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_call_op(
        lambda: op_convert(
            ws,
            " ".join(args.query),
            args.destination,
            status=getattr(args, "status", None),
            task_type=getattr(args, "task_type", None),
            next_action=getattr(args, "next_action", None),
            match_mode=args.match_mode,
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )


def parse_edit_tags(value: str) -> list[str]:
    try:
        return parse_tag_json(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def command_edit(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _command_edit_serialized(args, ws=ws)


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
    return _cli_call_op(
        lambda: op_edit(
            ws,
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
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )


def command_delete(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    return _cli_call_op(
        lambda: op_delete(
            ws,
            " ".join(args.query),
            by=getattr(args, "by", None),
            match_mode=args.match_mode,
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )


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
        resolved.relative_to((resolved_ws.root / ".mt").resolve())
    except (FileNotFoundError, ValueError):
        resolved = None
    if resolved is not None and resolved.is_file() and resolved.suffix == ".md":
        return _run_mdviz(resolved_ws, resolved, args)
    task, result = resolve_unique(query, match_mode=args.match_mode, ws=resolved_ws)
    if task is None:
        return result
    return _run_mdviz(resolved_ws, task.path, args)


def _read_item_data(task: Card, args: argparse.Namespace, ws: Workspace) -> tuple[dict[str, object] | None, str | None]:
    if args.as_json:
        return item_read_record(task, ws), None
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
            print(json.dumps([_read_item_data(task, args, resolved)[0] for task in tasks], ensure_ascii=False))
            return 0
        for task in tasks:
            _, text = _read_item_data(task, args, resolved)
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
        return 0

    query = " ".join(args.query).strip()
    task, result = resolve_unique(query, match_mode=args.match_mode, ws=resolved)
    if task is None:
        return result
    rec, text = _read_item_data(task, args, resolved)
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
    base = resolved.root
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
            record, contents = _bulk_plan_item(item, base, assignments, timestamp, resolved)
            records.append(record)
            if contents is not None:
                changes[str(Path(str(record["path"])).as_posix())] = contents
        if changes:
            validate_item_changes(changes, source_root=base, ws=resolved)
    except (OSError, ValueError) as error:
        prefix = "conflict" if "conflict:" in str(error) else "invalid bulk edit"
        print(f"error: {error}" if prefix == "conflict" else f"error: {prefix}: {error}", file=sys.stderr)
        return 1 if prefix == "conflict" else 2
    plan = {
        "version": 1,
        "workspace": str(resolved.root.resolve()),
        "data_root": "workspace",
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


def _read_bulk_plan(path_text: str, ws: Workspace | None = None, *, raw_text: str | None = None) -> dict[str, object]:
    resolved = _ws(ws)
    if raw_text is not None:
        raw = raw_text
    elif path_text == "-":
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
    if plan["data_root"] != "workspace":
        raise ValueError("bulk edit plan has an invalid data root")
    assignments_raw = plan["assignments"]
    if not isinstance(assignments_raw, dict):
        raise ValueError("bulk edit plan assignments must be an object")
    assignments = _bulk_edit_assignments(assignments_raw)
    if not isinstance(plan["items"], list) or not plan["items"]:
        raise ValueError("bulk edit plan must contain at least one selected card")
    _plan_query(plan, resolved)
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


def _command_bulk_edit_apply(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        plan = _read_bulk_plan(args.apply_plan, ws=resolved, raw_text=getattr(args, "apply_text", None))
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return _cli_call_op(
        lambda: op_bulk_edit(
            resolved,
            plan,
            by=getattr(args, "by", None),
            quiet_sync=args.as_json,
        ),
        as_json=args.as_json,
    )


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
    query = command_query(args, ws=resolved)
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
            print(json.dumps(item_compact_json_record(item), ensure_ascii=False))
        return 0
    if getattr(args, "outline", False):
        for item in selected:
            print(json.dumps(item_outline_json_record(item), ensure_ascii=False))
        return 0
    if args.full:
        print(json.dumps([item_read_record(item, ws=resolved) for item in selected], ensure_ascii=False))
        return 0
    if getattr(args, "as_json", False) or getattr(args, "json", False):
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


def command_topic_rename(args: argparse.Namespace, ws: Workspace | None = None) -> int:
    resolved = _ws(ws)
    try:
        result = op_topic_rename(
            resolved,
            args.old,
            args.new,
            merge=args.merge,
            dry_run=args.dry_run,
            by=getattr(args, "by", None),
            quiet_sync=args.as_json,
        )
    except store.TaskError as error:
        return _cli_print_error(error)
    payload = dict(result.receipt)
    if result.sync_error:
        payload["sync_error"] = result.sync_error
    if args.as_json:
        emit_json(payload)
        return 0
    records = payload["items"]
    verb = "Would move" if args.dry_run else "Moved"
    for record in records:
        print(f"{record['ref']}  {record['from']} -> {record['to']}  {record['title']}")
    print(f"{verb} {payload['changed']} card(s) from '{card_format.normalize_topic(args.old)}' to '{card_format.normalize_topic(args.new)}'.")
    if any(card_format.topic_key(item)[:len(card_format.topic_key(args.old))] == card_format.topic_key(args.old)
           for item in suggested_topics(store.policy(resolved))):
        print("Note: suggested_topics in the workspace settings still lists the old name; update it by hand.")
    return result.sync_exit_code


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
    selection = command_query(args, ws=resolved)
    if selection is None:
        return 2
    if (query or set_query or args.clear) and card_query.query_from_args(args, env={}) != card_query.DEFAULT:
        print("error: set and clear focus operations cannot be combined with filters", file=sys.stderr)
        return 2
    try:
        context = op_focus(
            resolved,
            selection,
            query=query or set_query,
            clear=args.clear,
            cycle_next=args.next,
            by=getattr(args, "by", None),
        )
    except store.TaskError as error:
        return _cli_print_error(error)
    if getattr(args, "as_json", False) or getattr(args, "json", False):
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


def _validate_capture_args(root_parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.file is not None and args.text:
        root_parser.error("capture text and --file cannot be combined")
    has_input = args.file is not None or bool(args.text)
    if args.kind == "note":
        if any(getattr(args, name, None) is not None
               for name in ("status", "task_type", "next_action", "next_action_file", "by")):
            root_parser.error("--status, --task-type, --next-action, --next-action-file, and --by are task-only options")
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
