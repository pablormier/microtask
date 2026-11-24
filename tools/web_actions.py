"""Dashboard API mutation handlers and background git sync."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
import card_query  # noqa: E402
import drafts as draft_store  # noqa: E402
import git_ops  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace  # noqa: E402

try:
    from .web_payloads import draft_to_dict, task_to_dict
except ImportError:
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from web_payloads import draft_to_dict, task_to_dict


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
        self.last_synced_at = ""

    def request(self, ws: Workspace) -> None:
        if not store.policy(ws).get("rules", {}).get("auto_sync", False):
            return
        with self.lock:
            if self.thread is not None:
                self.again = True
                return
            self.thread = threading.Thread(target=self.run, args=(ws,), daemon=True)
            self.thread.start()

    def run(self, ws: Workspace) -> None:
        while True:
            warning = sync_once(ws.for_operation())
            with self.lock:
                self.warning = warning
                if not warning:
                    self.last_synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if not self.again:
                    self.thread = None
                    return
                self.again = False

    def wait(self, timeout: float | None = None) -> None:
        with self.lock:
            thread = self.thread
        if thread is not None:
            thread.join(timeout)

    def current_warning(self) -> str:
        with self.lock:
            return self.warning

    def status(self) -> dict[str, object]:
        with self.lock:
            return {
                "syncing": self.thread is not None,
                "last_synced_at": self.last_synced_at,
            }


def sync_once(ws: Workspace) -> str:
    """Sync the workspace and return a warning, or an empty string on success."""
    status = git_ops.workspace_status(ws.root)
    if status.state in {"missing", "uninitialized"}:
        message = transaction.auto_sync_unavailable_message(status, ws)
        transaction.record_sync_result(message, ws)
        return message
    output = io.StringIO()
    errors = io.StringIO()
    returncode = transaction.command_sync(argparse.Namespace(message=None), ws=ws, out=output, err=errors)
    if returncode == 0:
        return ""
    return transaction.read_sync_error(ws)[1] or "automatic sync failed"


# Draft autosaves change only drafts/, which sync does not need to publish on every keystroke.
UNSYNCED_ACTIONS = {"/api/draft-save", "/api/draft-discard", "/api/focus", "/api/focus-clear"}


def item_by_ref(ws: Workspace, ref: object) -> dict | None:
    if not isinstance(ref, str) or not store.SHORT_REF_RE.fullmatch(ref):
        return None
    items = store.items_by_ref(ref, ws)
    item = items[0] if len(items) == 1 and f"@{items[0].short_ref}" == ref else None
    return task_to_dict(item, ws=ws, detail=False) if item else None


def _run_operation(ws: Workspace, action, message: str, *, include_saved_ref: bool = False) -> tuple[HTTPStatus, dict]:
    try:
        result = action(ws)
    except store.HistoryRequiredError:
        raise
    except store.RollbackError as error:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)}
    except store.ConflictError as error:
        payload: dict[str, object] = {"error": str(error), "conflict": True}
        if error.sync_needed:
            payload["sync_needed"] = True  # Part of the change was saved; the server still syncs it.
        return HTTPStatus.CONFLICT, payload
    except store.TaskError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    payload = {"message": message}
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


def handle_review(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    """Clear needs_review on any card kind, guarded by the loaded content hash."""
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    if not item.get("needs_review"):
        return HTTPStatus.OK, {"message": "Already reviewed", "saved_ref": item["ref"]}
    return _run_operation(
        ws,
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            expected_hash=expected_hash,
            assignments=["needs_review=false"],
            match_mode="exact",
            by=by,
            sync=False,
        ),
        "Marked reviewed",
        include_saved_ref=True,
    )


def handle_checkbox(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
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
    cards = store.items_by_ref(item["ref"], ws)
    if not cards:
        return HTTPStatus.NOT_FOUND, {"error": "Card no longer exists in this workspace."}
    card = cards[0]
    try:
        new_body = card_format.toggle_task_checkbox(card.body, index, count, checked)
    except ValueError as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    return _run_operation(
        ws,
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            expected_hash=expected_hash,
            body_mode="replace",
            body_text=new_body,
            match_mode="exact",
            by=by,
            sync=False,
        ),
        "Checklist updated",
        include_saved_ref=True,
    )


def handle_draft_save(ws: Workspace, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    expected_hash = body.get("expected_hash")
    if expected_hash is not None and not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash when updating a draft"}
    if not isinstance(body.get("body"), str):
        return HTTPStatus.BAD_REQUEST, {"error": "body must be valid UTF-8 text"}
    kind = body.get("kind")
    # Drafts are always new cards. The field stays optional for older clients.
    mode = body.get("mode", "new")
    if not isinstance(kind, str) or kind not in {"note", "task"}:
        return HTTPStatus.BAD_REQUEST, {"error": "kind must be note or task"}
    if mode != "new":
        return HTTPStatus.BAD_REQUEST, {"error": "mode must be new"}
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
    fields.update(capture_id=body.get("capture_id"), created=body.get("created"))
    origin = body.get("origin")
    if origin is not None:
        if not isinstance(origin, str) or len(origin) > 512:
            return HTTPStatus.BAD_REQUEST, {"error": "origin must be a short string"}
        fields["origin"] = origin
    if (expected_hash is None and not title.strip()
            and not body["body"].strip() and not str(body.get("instructions") or "").strip()):
        return HTTPStatus.BAD_REQUEST, {"error": "new drafts require a non-empty title or text"}
    try:
        draft = draft_store.save(
            ws.root,
            draft_id=draft_id if isinstance(draft_id, str) else None,
            fields=fields,
            body=body["body"],
            now=store.now_stamp(),
            expected_content_hash=expected_hash,
            viewer=by,
        )
    except draft_store.DraftNotFound:
        return HTTPStatus.NOT_FOUND, {"error": "draft not found"}
    except draft_store.DraftConflict as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (OSError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    return HTTPStatus.OK, {"message": "Draft saved", "saved_draft": draft_to_dict(draft)}


def handle_draft_discard(ws: Workspace, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    if not isinstance(draft_id, str) or not draft_id:
        return HTTPStatus.BAD_REQUEST, {"error": "draft id is required"}
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    try:
        removed = draft_store.discard(ws.root, draft_id, expected_hash, viewer=by)
    except draft_store.DraftConflict as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (OSError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    if not removed:
        return HTTPStatus.NOT_FOUND, {"error": "draft not found"}
    return HTTPStatus.OK, {"message": "Draft discarded"}


def handle_draft_finish(ws: Workspace, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    draft_id = body.get("id")
    if not isinstance(draft_id, str) or not draft_id:
        return HTTPStatus.BAD_REQUEST, {"error": "draft id is required"}
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if by is not None:
        try:
            draft_store.read(ws.root, draft_id, by)
        except draft_store.DraftNotFound:
            return HTTPStatus.NOT_FOUND, {"error": "draft not found"}
        except (OSError, ValueError):
            pass  # The operation reports a malformed or missing draft itself.
    return _run_operation(
        ws,
        lambda ws: operations.op_draft_save(
            ws,
            draft_id,
            expected_hash=expected_hash,
            by=by,
            sync=False,
        ),
        "Draft applied",
        include_saved_ref=True,
    )


def handle_card_edit(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    expected_hash = body.get("expected_hash")
    text = body.get("body")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if not isinstance(text, str) or not valid_utf8(text):
        return HTTPStatus.BAD_REQUEST, {"error": "body must be valid UTF-8 text"}
    return _run_operation(
        ws,
        lambda ws: operations.op_edit(
            ws,
            item["ref"],
            match_mode="exact",
            expected_hash=expected_hash,
            body_mode="replace",
            body_text=text,
            by=by,
            sync=False,
        ),
        "Content saved",
        include_saved_ref=True,
    )


def handle_card_metadata(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    expected_hash = body.get("expected_hash")
    patch = body.get("patch")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if not isinstance(patch, dict) or not patch:
        return HTTPStatus.BAD_REQUEST, {"error": "metadata patch must be a non-empty object"}
    return _run_operation(
        ws,
        lambda ws: operations.op_card_metadata(
            ws,
            item["ref"],
            patch,
            expected_hash=expected_hash,
            kind=item["kind"],
            by=by,
            match_mode="exact",
            sync=False,
        ),
        "Properties saved",
        include_saved_ref=True,
    )


def handle_move(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    destination = body.get("status")
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can move"}
    open_statuses = store.open_statuses(ws)
    if destination not in open_statuses:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid destination status"}
    label = store.policy(ws).get("statuses", {}).get(str(destination)) or destination
    return _run_operation(
        ws,
        lambda ws: operations.op_move(
            ws, item["ref"], str(destination), match_mode="exact", by=by, sync=False
        ),
        f"Moved to {label}",
    )


def handle_order(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    position = body.get("position")
    before = body.get("before")
    after = body.get("after")
    choices = sum([position is not None, before is not None, after is not None])
    if choices != 1:
        return HTTPStatus.BAD_REQUEST, {"error": "order requires exactly one of position, before, or after"}
    if position is not None and position not in {"top", "bottom", "clear", "clear_panel"}:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid order position"}
    for name, value in (("before", before), ("after", after)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return HTTPStatus.BAD_REQUEST, {"error": f"{name} must be a card reference or id"}
    return _run_operation(
        ws,
        lambda ws: operations.op_order(
            ws,
            item["ref"],
            position=position,
            before=before,
            after=after,
            by=by,
            match_mode="exact",
            sync=False,
        ),
        "Panel order reset" if position == "clear_panel" else ("Order cleared" if position == "clear" else "Order updated"),
    )


def handle_comment(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return HTTPStatus.BAD_REQUEST, {"error": "comment requires text"}
    return _run_operation(
        ws,
        lambda ws: operations.op_comment(
            ws, item["ref"], [message], match_mode="exact", by=by, sync=False
        ),
        "Comment added",
    )


def handle_comment_edit(
    ws: Workspace,
    item: dict,
    body: dict,
    by: str | None = None,
    *,
    is_admin: bool = False,
) -> tuple[HTTPStatus, dict]:
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    index = body.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return HTTPStatus.BAD_REQUEST, {"error": "index must be a non-negative integer"}
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return HTTPStatus.BAD_REQUEST, {"error": "comment requires text"}
    return _run_operation(
        ws,
        lambda ws: operations.op_comment_edit(
            ws,
            item["ref"],
            index,
            message,
            expected_hash=expected_hash,
            by=by,
            is_admin=is_admin,
            match_mode="exact",
            sync=False,
        ),
        "Comment updated",
    )


def handle_comment_delete(
    ws: Workspace,
    item: dict,
    body: dict,
    by: str | None = None,
    *,
    is_admin: bool = False,
) -> tuple[HTTPStatus, dict]:
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    index = body.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return HTTPStatus.BAD_REQUEST, {"error": "index must be a non-negative integer"}
    return _run_operation(
        ws,
        lambda ws: operations.op_comment_delete(
            ws,
            item["ref"],
            index,
            expected_hash=expected_hash,
            by=by,
            is_admin=is_admin,
            match_mode="exact",
            sync=False,
        ),
        "Comment deleted",
    )


def handle_time_start(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can start a timer"}
    return _run_operation(
        ws,
        lambda ws: operations.op_time_start(ws, item["ref"], by=by, sync=False),
        "Timer started",
    )


def handle_time_stop(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
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
        ws,
        lambda ws: operations.op_time_end(
            ws,
            query=item["ref"],
            action="stop",
            at=cleaned_ends.get("at"),
            duration=cleaned_ends.get("duration"),
            by=by,
            sync=False,
        ),
        "Timer stopped and saved",
    )


def handle_time_cancel(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task":
        return HTTPStatus.BAD_REQUEST, {"error": "this task has no running timer"}
    return _run_operation(
        ws,
        lambda ws: operations.op_time_end(ws, query=item["ref"], action="cancel", by=by, sync=False),
        "Timer discarded; no time recorded",
    )


def handle_done(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can be marked done"}
    comment = body.get("comment")
    comments = [comment] if isinstance(comment, str) and comment.strip() else ()
    return _run_operation(
        ws,
        lambda ws: operations.op_complete(
            ws, item["ref"], comments=comments, match_mode="exact", by=by, sync=False
        ),
        "Marked done",
    )


def handle_convert(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    """Convert a card between task and note, guarded by the loaded content hash."""
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    destination = body.get("destination")
    if destination not in {"task", "note"}:
        return HTTPStatus.BAD_REQUEST, {"error": "destination must be task or note"}
    status = body.get("status")
    task_type = body.get("task_type")
    next_action = body.get("next_action")
    for key, value in (("status", status), ("task_type", task_type), ("next_action", next_action)):
        if value is not None and (not isinstance(value, str) or not valid_utf8(value)):
            return HTTPStatus.BAD_REQUEST, {"error": f"{key} must be valid UTF-8 text"}
    return _run_operation(
        ws,
        lambda ws: operations.op_convert(
            ws,
            item["ref"],
            destination,
            expected_hash=expected_hash,
            status=status,
            task_type=task_type,
            next_action=next_action,
            by=by,
            match_mode="exact",
            sync=False,
        ),
        f"Converted to {destination}",
        include_saved_ref=True,
    )


def handle_flag_update(field: str, message: str):
    def _handler(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
        flag = body.get(field)
        if not isinstance(flag, bool):
            return HTTPStatus.BAD_REQUEST, {"error": f"{field} must be true or false"}
        value = "true" if flag else "false"
        return _run_operation(
            ws,
            lambda ws: operations.op_edit(
                ws, item["ref"], assignments=[f"{field}={value}"], match_mode="exact", by=by, sync=False
            ),
            message,
        )

    return _handler


def handle_delete(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    expected_hash = body.get("expected_hash")
    if not _valid_sha256(expected_hash):
        return HTTPStatus.BAD_REQUEST, {"error": "expected_hash must be a SHA-256 hash"}
    if expected_hash != item.get("content_hash"):
        return HTTPStatus.CONFLICT, {"error": "card changed since it was loaded; refresh and retry", "conflict": True}
    return _run_operation(
        ws,
        lambda ws: operations.op_delete(ws, item["ref"], by=by, match_mode="exact", sync=False),
        "Card deleted",
    )


def handle_takeover(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    if item["kind"] != "task" or item["status"] == "done":
        return HTTPStatus.BAD_REQUEST, {"error": "only open tasks can be taken over"}
    if not item.get("claimed_by"):
        return HTTPStatus.BAD_REQUEST, {"error": "task is not claimed; move it to doing instead"}
    return _run_operation(
        ws,
        lambda ws: operations.op_move(ws, item["ref"], "doing", by=by, takeover=True, match_mode="exact", sync=False),
        "Claim taken over",
    )


def handle_focus(ws: Workspace, item: dict, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    """Choose the signed-in person's own focus card. Other people's focus is not touched."""
    try:
        operations.op_focus(ws, card_query.DEFAULT, query=item["ref"], by=by, focus_user=by)
    except store.HistoryRequiredError:
        raise
    except store.ConflictError as error:
        return HTTPStatus.CONFLICT, {"error": str(error), "conflict": True}
    except (store.TaskError, ValueError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    return HTTPStatus.OK, {"message": "Focus updated"}


def handle_focus_clear(ws: Workspace, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    operations.op_focus(ws, card_query.DEFAULT, clear=True, by=by, focus_user=by)
    return HTTPStatus.OK, {"message": "Focus cleared"}


def handle_topic_rename(ws: Workspace, body: dict, by: str | None = None) -> tuple[HTTPStatus, dict]:
    old, new = body.get("old"), body.get("new")
    if not isinstance(old, str) or not isinstance(new, str) or not old.strip() or not new.strip():
        return HTTPStatus.BAD_REQUEST, {"error": "old and new topic names are required"}
    return _run_operation(
        ws,
        lambda ws: operations.op_topic_rename(ws, old, new, merge=body.get("merge") is True, by=by, sync=False),
        "Topic renamed",
    )


DRAFT_ACTION_HANDLERS = {
    "/api/draft-save": handle_draft_save,
    "/api/draft-discard": handle_draft_discard,
    "/api/draft-finish": handle_draft_finish,
}

ITEM_ACTION_HANDLERS = {
    "/api/card-edit": handle_card_edit,
    "/api/card-metadata": handle_card_metadata,
    "/api/checkbox": handle_checkbox,
    "/api/review": handle_review,
    "/api/move": handle_move,
    "/api/order": handle_order,
    "/api/comment": handle_comment,
    "/api/comment-edit": handle_comment_edit,
    "/api/comment-delete": handle_comment_delete,
    "/api/time-start": handle_time_start,
    "/api/time-stop": handle_time_stop,
    "/api/time-cancel": handle_time_cancel,
    "/api/done": handle_done,
    "/api/convert": handle_convert,
    "/api/archive": handle_flag_update("archived", "Archive updated"),
    "/api/hidden": handle_flag_update("hidden", "Visibility updated"),
}

# Actions that exist only in a shared workspace (the single-user dashboard has no such endpoints).
# The first group needs an administrator; the second is open to everyone who is signed in.
HOSTED_ADMIN_ITEM_HANDLERS = {
    "/api/delete": handle_delete,
    "/api/takeover": handle_takeover,
}
HOSTED_ITEM_HANDLERS = {
    "/api/focus": handle_focus,
}
HOSTED_ADMIN_HANDLERS = {
    "/api/topic-rename": handle_topic_rename,
}
HOSTED_HANDLERS = {
    "/api/focus-clear": handle_focus_clear,
}
# Actions that remove or overwrite work. A pre-mutation local history snapshot is required before they run.
SNAPSHOT_BEFORE = {"/api/delete", "/api/takeover", "/api/topic-rename", "/api/comment-delete"}


def handle_api_action(ws: Workspace, path: str, body: dict, actor=None) -> tuple[HTTPStatus, dict]:
    """Run one dashboard action. `actor` is the signed-in person of a shared workspace, or None for one person."""
    by = actor.user if actor is not None else None
    draft_handler = DRAFT_ACTION_HANDLERS.get(path)
    if draft_handler is not None:
        return draft_handler(ws, body, by)

    if actor is not None and path in {**HOSTED_ADMIN_HANDLERS, **HOSTED_HANDLERS}:
        if path in HOSTED_ADMIN_HANDLERS and not actor.admin:
            return HTTPStatus.FORBIDDEN, {"error": ADMIN_ONLY_MESSAGE}
        return {**HOSTED_ADMIN_HANDLERS, **HOSTED_HANDLERS}[path](ws, body, by)

    item_handler = ITEM_ACTION_HANDLERS.get(path)
    if item_handler is None and actor is not None:
        item_handler = {**HOSTED_ADMIN_ITEM_HANDLERS, **HOSTED_ITEM_HANDLERS}.get(path)
        if path in HOSTED_ADMIN_ITEM_HANDLERS and not actor.admin:
            return HTTPStatus.FORBIDDEN, {"error": ADMIN_ONLY_MESSAGE}
    if item_handler is None:
        return HTTPStatus.NOT_FOUND, {"error": "not found"}

    item = item_by_ref(ws, body.get("ref"))
    if item is None:
        return HTTPStatus.BAD_REQUEST, {"error": "unknown item ref"}
    if path in {"/api/comment-edit", "/api/comment-delete"}:
        return item_handler(ws, item, body, by, is_admin=bool(actor and actor.admin))
    return item_handler(ws, item, body, by)


ADMIN_ONLY_MESSAGE = "Only a workspace administrator can do this."
