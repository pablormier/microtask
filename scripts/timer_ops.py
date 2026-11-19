"""Timer lifecycle and time-session helpers."""
from __future__ import annotations

import card_format
from store import (
    Card,
    _ws,
    now_stamp,
    time_entry_from_timer,
    time_session_for,
)
import transaction
from workspace import Workspace


def append_time_entry(raw: str, entry: dict[str, object]) -> str:
    properties, body = card_format.parse_document(raw)
    if not isinstance(entry, dict) or set(entry) != {"session_id", "start", "end", "elapsed_seconds"}:
        raise ValueError("time entry must contain session_id, start, end, and elapsed_seconds")
    records = properties.setdefault("time_entries", [])
    if not isinstance(records, list):
        raise ValueError("time_entries must be a list")
    records.append(dict(entry))
    return card_format.render_document(properties, body)


def settle_timer(raw: str, end_epoch: int) -> tuple[str, dict | None]:
    """Remove the card's running timer and record its session, in the one text.

    Returns the new text and the timer that was settled, or ``raw`` unchanged
    and None when no timer runs. A session the card already holds is not
    recorded twice.
    """
    properties, body = card_format.parse_document(raw)
    timer = card_format.timer_from(properties)
    if timer is None:
        return raw, None
    del properties["timer"]
    if not any(entry["session_id"] == timer["session_id"] for entry in card_format.time_entries_from(properties)):
        records = properties.setdefault("time_entries", [])
        if not isinstance(records, list):
            raise ValueError("time_entries must be a list")
        records.append(time_entry_from_timer(timer, end_epoch))
    return card_format.render_document(properties, body), timer


def save_time_session(
    task: Card, timer: dict[str, object], end_epoch: int, ws: Workspace | None = None,
) -> tuple[bool, str]:
    """Append one session to a card without touching its timer; report validation failure."""
    resolved = _ws(ws)
    transaction._require_mutation_lock(resolved)
    entry, already_saved = time_session_for(task, timer, end_epoch)
    if already_saved:
        return True, "already saved"
    original = task.raw.encode("utf-8")
    updated = append_time_entry(task.raw, entry)
    updated = card_format.replace_property(updated, "updated", now_stamp())
    base = resolved.root
    relative = str(task.path.relative_to(base))
    try:
        transaction.apply_item_transaction(
            {relative: updated.encode("utf-8")},
            expected={relative: original},
            source_root=base,
            ws=resolved,
        )
    except (OSError, ValueError):
        return False, "time log failed validation"
    return True, "saved"
