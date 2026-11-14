"""Dashboard API mutation handlers and background git sync."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, time
from http import HTTPStatus
import io
from pathlib import Path
import re
import sys
import threading

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(APP_ROOT / "scripts"))

import card_format  # noqa: E402
import drafts as draft_store  # noqa: E402
import git_ops  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import transaction  # noqa: E402

try:
    from .web_payloads import current_workspace, task_to_dict, workspace_root
except ImportError:
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from web_payloads import current_workspace, task_to_dict, workspace_root


class BackgroundSync:
    """Run auto-sync after dashboard writes, one sync at a time.

    Writes that arrive during a sync are folded into one more sync. The last
    failure is kept so the board can show it on its next refresh.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.again = False
        self.warning = ""

    def request(self) -> None:
        if not store.policy().get("rules", {}).get("auto_sync", False):
            return
        with self.lock:
            if self.thread is not None:
                self.again = True
                return
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def run(self) -> None:
        while True:
            warning = sync_once()
            with self.lock:
                self.warning = warning
                if not self.again:
                    self.thread = None
                    return
                self.again = False

    def wait(self, timeout: float | None = None) -> None:
        with self.lock:
            thread = self.thread
        if thread is not None:
            thread.join(timeout)


def sync_once() -> str:
    """Sync the workspace and return a warning, or an empty string on success."""
    ws = current_workspace()
    status = git_ops.workspace_status(ws.root)
    if status.state in {"missing", "uninitialized"}:
        return transaction.auto_sync_unavailable_message(status, ws)
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        returncode = transaction.command_sync(argparse.Namespace(message=None), ws=ws)
    if returncode == 0:
        return ""
    error_lines = [line for line in errors.getvalue().splitlines()
                   if not line.startswith(("Workspace: ", "Application: "))]
    return "\n".join([*error_lines, output.getvalue()]).strip() or "automatic sync failed"


# Draft autosaves change only drafts/, which sync does not need to publish on every keystroke.
UNSYNCED_ACTIONS = {"/api/draft-save", "/api/draft-discard"}


def item_by_ref(ref: object) -> dict | None:
    if not isinstance(ref, str) or not store.SHORT_REF_RE.fullmatch(ref):
        return None
    store.pin_demo_state()
    store.maybe_seed_demo()
    items = store.items_by_ref(ref)
    item = items[0] if len(items) == 1 and f"@{items[0].short_ref}" == ref else None
    return task_to_dict(item, detail=False) if item else None


def _run_operation(action, message: str, *, include_saved_ref: bool = False) -> tuple[HTTPStatus, dict]:
    try:
        result = action(current_workspace())
    except store.ConflictError as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except store.TaskError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    payload: dict[str, object] = {"message": message}
    if include_saved_ref:
        payload["saved_ref"] = result.receipt.get("ref")
    return HTTPStatus.OK, payload


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def valid_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def handle_review(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    """Clear needs_review on any card kind, guarded by the loaded content hash."""
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    if not item.get("needs_review"):
        return HTTPStatus.OK, {"message": "Already reviewed", "saved_ref": item["ref"]}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            expected_hash=expected_hash,
            assignments=["needs_review=false"],
            match_mode="exact",
            sync=False,
        ),
        "Marked reviewed",
        include_saved_ref=True,
    )


def handle_checkbox(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    """Toggle one task-list checkbox in the card body, guarded by the loaded content hash."""
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    index, count, checked = body.get("index"), body.get("count"), body.get("checked")
    for name, value in (("index", index), ("count", count)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return HTTPStatus.BAD_REQUEST, {"error": f"{name} must be a non-negative integer"}
    if not isinstance(checked, bool):
        return HTTPStatus.BAD_REQUEST, {"error": "checked must be true or false"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    card = store.items_by_ref(item["ref"])[0]
    try:
        new_body = card_format.toggle_task_checkbox(card.body, index, count, checked)
    except ValueError as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            expected_hash=expected_hash,
            body_mode="replace",
            body_text=new_body,
            match_mode="exact",
            sync=False,
        ),
        "Checklist updated",
        include_saved_ref=True,
    )


def handle_draft_save(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    expected_revision = body.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be a non-negative integer"}
    expected_hash = body.get("expected_hash")
    if expected_revision > 0 and not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash when updating a draft"}
    if not isinstance(body.get("body"), str):
        return HTTPStatus.BAD_REQUEST, {"error": "body must be valid UTF-8 text"}
    kind = body.get("kind")
    mode = body.get("mode")
    if not isinstance(kind, str) or kind not in {"note", "task"}:
        return HTTPStatus.BAD_REQUEST, {"error": "kind must be note or task"}
    if not isinstance(mode, str) or mode not in {"new", "edit"}:
        return HTTPStatus.BAD_REQUEST, {"error": "mode must be new or edit"}
    title = body.get("title", "")
    if not isinstance(title, str) or not valid_utf8(title):
        return HTTPStatus.BAD_REQUEST, {"error": "title must be valid UTF-8 text"}
    fields: dict[str, object] = {"kind": kind, "mode": mode, "title": title}
    for field in ("status", "priority", "type", "project", "topic", "tags", "due", "due_kind"):
        if field not in body:
            continue
        value = body[field]
        if field == "tags":
            if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
                return HTTPStatus.BAD_REQUEST, {"error": "tags must be an array of strings"}
        elif value is not None and not isinstance(value, str):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be text or null"}
        if isinstance(value, str) and not valid_utf8(value):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be valid UTF-8 text"}
        fields[field] = value
    if "instructions" in body:
        fields["instructions"] = body["instructions"]
    if mode == "new":
        fields.update(capture_id=body.get("capture_id"), created=body.get("created"))
    elif mode == "edit" and expected_revision == 0:
        item = item_by_ref(body.get("target_ref"))
        if item is None or item["kind"] != kind:
            return HTTPStatus.BAD_REQUEST, {"error": "edit draft target is unavailable"}
        item_path = workspace_root() / str(item["path"])
        if item_path.is_symlink() or not item_path.is_file():
            return HTTPStatus.BAD_REQUEST, {"error": "edit draft target is unavailable"}
        supplied_hash = body.get("base_hash")
        if supplied_hash is None:
            supplied_hash = draft_store.UNVERIFIED_BASE_HASH
        if not _valid_sha256(supplied_hash):
            return HTTPStatus.BAD_REQUEST, {"error": "base_hash must be a SHA-256 hash"}
        supplied_updated = body.get("base_updated", "")
        if not isinstance(supplied_updated, str):
            return HTTPStatus.BAD_REQUEST, {"error": "base_updated must be text"}
        fields.update(target=str(item["path"]), base_hash=supplied_hash, base_updated=supplied_updated)
    origin = body.get("origin")
    if origin is not None:
        if not isinstance(origin, str) or len(origin) > 512:
            return HTTPStatus.BAD_REQUEST, {"error": "origin must be a short string"}
        fields["origin"] = origin
    if (mode == "new" and expected_revision == 0 and not title.strip()
            and not body["body"].strip() and not str(body.get("instructions") or "").strip()):
        return HTTPStatus.BAD_REQUEST, {"error": "new drafts require a non-empty title or text"}
    try:
        draft = draft_store.save(
            workspace_root(),
            draft_id=draft_id if isinstance(draft_id, str) else None,
            fields=fields,
            body=body["body"],
            expected_revision=expected_revision,
            now=store.now_stamp(),
            expected_content_hash=expected_hash if expected_revision > 0 else None,
        )
    except draft_store.DraftConflict as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (OSError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    return HTTPStatus.OK, {"message": "Draft saved", "saved_draft": draft}


def handle_draft_discard(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    revision = body.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be an integer"}
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    try:
        removed = draft_store.discard(workspace_root(), str(draft_id), revision, expected_hash)
    except draft_store.DraftConflict as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (OSError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    if not removed:
        return HTTPStatus.NOT_FOUND, {"error": "draft not found"}
    return HTTPStatus.OK, {"message": "Draft discarded"}


def handle_draft_finish(body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    revision = body.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_revision must be an integer"}
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    return _run_operation(
        lambda ws: operations.op_draft_save(
            ws,
            str(draft_id),
            expected_revision=revision,
            expected_hash=expected_hash,
            sync=False,
        ),
        "Draft applied",
        include_saved_ref=True,
    )


def handle_card_metadata(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    expected_hash = body.get("expected_hash")
    patch = body.get("patch")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if not isinstance(patch, dict) or not patch:
        return HTTPStatus.BAD_REQUEST, {"error": "metadata patch must be a non-empty object"}
    return _run_operation(
        lambda ws: operations.op_card_metadata(
            ws,
            item["ref"],
            patch,
            expected_hash=expected_hash,
            kind=item["kind"],
            match_mode="exact",
            sync=False,
        ),
        "Properties saved",
        include_saved_ref=True,
    )


def handle_move(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    destination = body.get("status")
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can move"}
    open_statuses = [status for status in store.statuses() if status != "done"]
    if destination not in open_statuses:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid destination status"}
    label = store.policy().get("statuses", {}).get(str(destination)) or destination
    return _run_operation(
        lambda ws: operations.op_move(
            ws, item["ref"], str(destination), match_mode="exact", sync=False
        ),
        f"Moved to {label}",
    )


def handle_comment(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return HTTPStatus.BAD_REQUEST, {"error": "comment requires text"}
    return _run_operation(
        lambda ws: operations.op_comment(
            ws, item["ref"], [message], match_mode="exact", sync=False
        ),
        "Comment added",
    )


def handle_time_start(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can start a timer"}
    return _run_operation(
        lambda ws: operations.op_time_start(ws, item["ref"], sync=False),
        "Timer started",
    )


def handle_time_stop(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task":
        return HTTPStatus.BAD_REQUEST, {"error": "this task has no running timer"}
    ends = {key: body.get(key) for key in ("at", "duration") if body.get(key) not in (None, "")}
    if len(ends) > 1:
        return HTTPStatus.BAD_REQUEST, {"error": "give either at or duration, not both"}
    cleaned_ends: dict[str, str] = {}
    for key, value in ends.items():
        if not isinstance(value, str) or not value.strip():
            return HTTPStatus.BAD_REQUEST, {"error": f"{key} must be text"}
        cleaned_ends[key] = value.strip()
    return _run_operation(
        lambda ws: operations.op_time_end(
            ws,
            query=item["ref"],
            action="stop",
            at=cleaned_ends.get("at"),
            duration=cleaned_ends.get("duration"),
            sync=False,
        ),
        "Timer stopped and saved",
    )


def handle_time_cancel(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task":
        return HTTPStatus.BAD_REQUEST, {"error": "this task has no running timer"}
    return _run_operation(
        lambda ws: operations.op_time_end(ws, query=item["ref"], action="cancel", sync=False),
        "Timer discarded; no time recorded",
    )


def handle_done(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can be marked done"}
    comment = body.get("comment")
    comments = [comment] if isinstance(comment, str) and comment.strip() else ()
    return _run_operation(
        lambda ws: operations.op_complete(
            ws, item["ref"], comments=comments, match_mode="exact", sync=False
        ),
        "Marked done",
    )


def handle_flag_update(field: str, message: str):
    def _handler(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
        flag = body.get(field)
        if not isinstance(flag, bool):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be true or false"}
        value = "true" if flag else "false"
        return _run_operation(
            lambda ws: operations.op_edit(
                ws, item["ref"], assignments=[f"{field}={value}"], match_mode="exact", sync=False
            ),
            message,
        )

    return _handler


def handle_priority(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    priority = body.get("priority")
    allowed_priorities = list(store.policy().get("priorities", {}).keys())
    if not isinstance(priority, str) or priority not in allowed_priorities:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid priority"}
    if priority == item.get("priority"):
        return HTTPStatus.BAD_REQUEST, {"error": "priority unchanged"}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws, item["ref"], assignments=[f"priority={priority}"], match_mode="exact", sync=False
        ),
        f"Priority set to {priority}",
    )


def handle_due_set(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    due = body.get("due")
    due_time = body.get("due_time")
    due_kind = body.get("due_kind")
    try:
        parsed = date.fromisoformat(due) if isinstance(due, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != due:
        return HTTPStatus.BAD_REQUEST, {"error": "due must be a YYYY-MM-DD date"}
    if due_time:
        try:
            parsed_time = time.fromisoformat(due_time) if isinstance(due_time, str) else None
        except ValueError:
            parsed_time = None
        if parsed_time is None:
            return HTTPStatus.BAD_REQUEST, {"error": "due_time must be an HH:MM time"}
        value = f"{due}T{parsed_time.strftime('%H:%M:%S')}"
        message = f"Deadline set to {due} {parsed_time.strftime('%H:%M')}"
    else:
        value = due
        message = f"Deadline set to {due}"
    allowed_kinds = list(store.policy().get("due_kinds", {}).keys())
    if due_kind not in allowed_kinds:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid due kind"}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            assignments=[f"due={value}", f"due_kind={due_kind}"],
            match_mode="exact",
            sync=False,
        ),
        message,
    )


def handle_due_clear(item: dict, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    if not item.get("due"):
        return HTTPStatus.BAD_REQUEST, {"error": "item has no due date"}
    return _run_operation(
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            assignments=["due=null", "due_kind=null"],
            match_mode="exact",
            sync=False,
        ),
        "Deadline removed",
    )


DRAFT_ACTION_HANDLERS = {
    "/api/draft-save": handle_draft_save,
    "/api/draft-discard": handle_draft_discard,
    "/api/draft-finish": handle_draft_finish,
}

ITEM_ACTION_HANDLERS = {
    "/api/card-metadata": handle_card_metadata,
    "/api/checkbox": handle_checkbox,
    "/api/review": handle_review,
    "/api/move": handle_move,
    "/api/comment": handle_comment,
    "/api/time-start": handle_time_start,
    "/api/time-stop": handle_time_stop,
    "/api/time-cancel": handle_time_cancel,
    "/api/done": handle_done,
    "/api/archive": handle_flag_update("archived", "Archive updated"),
    "/api/hidden": handle_flag_update("hidden", "Visibility updated"),
    "/api/priority": handle_priority,
    "/api/due-set": handle_due_set,
    "/api/due-clear": handle_due_clear,
}


def handle_api_action(path: str, body: dict, token: str) -> tuple[HTTPStatus, dict]:
    draft_handler = DRAFT_ACTION_HANDLERS.get(path)
    if draft_handler is not None:
        return draft_handler(body, token)

    item_handler = ITEM_ACTION_HANDLERS.get(path)
    if item_handler is None:
        return HTTPStatus.NOT_FOUND, {"error": "not found"}

    item = item_by_ref(body.get("ref"))
    if item is None:
        return HTTPStatus.BAD_REQUEST, {"error": "unknown item ref"}
    return item_handler(item, body, token)
