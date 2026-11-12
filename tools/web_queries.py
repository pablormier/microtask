"""Dashboard collection selection and ordering; Markdown payloads remain unchanged."""
from datetime import datetime, time

import card_format  # on sys.path via tools/taskweb.py, which inserts scripts/


FILTER_FIELDS = ("search", "status", "priority", "type", "project", "topic", "tag", "due", "updated")


def normalize_query(values=None):
    values = values or {}
    query = {key: str(values.get(key) or ("" if key in ("search", "tag") else "all"))
             for key in FILTER_FIELDS}
    for key in ("show_hidden", "needs_review"):
        query[key] = values.get(key) is True or values.get(key) == "true"
    return query


def due_datetime(value, now):
    try:
        text = str(value)
        result = (datetime.combine(datetime.fromisoformat(text).date(), time(23, 59, 59))
                  if len(text) == 10 else datetime.fromisoformat(text.replace("Z", "+00:00")))
        if now.tzinfo is None:
            # Use local rules at the due date, including daylight saving changes.
            return result if result.tzinfo is None else result.astimezone().replace(tzinfo=None)
        return result.replace(tzinfo=now.tzinfo) if result.tzinfo is None else result.astimezone(now.tzinfo)
    except (TypeError, ValueError):
        return None


def matches(item, query, now, *, status=True):
    if query["needs_review"] and not item.get("needs_review"):
        return False
    for key in ("status", "priority", "type", "project", "topic"):
        if key == "status" and not status:
            continue
        value = item.get(key) or (item.get("kind") if key == "type" else "")
        if query[key] == "all":
            continue
        if key == "topic":
            # A topic filter selects the path and everything under it.
            if not card_format.topic_within(value, query[key]):
                return False
        elif key == "project":
            # Match `mt list --project`, so `mt web --project` links work in any case.
            if str(value).casefold() != query[key].casefold():
                return False
        elif value != query[key]:
            return False
    tag = query["tag"].strip().lower()
    if tag and not any(tag in value.lower() for value in item.get("tags", [])):
        return False
    due_filter = query["due"]
    if due_filter != "all":
        value = item.get("due")
        if (due_filter == "none" and value) or (due_filter != "none" and not value):
            return False
        if value:
            due = due_datetime(value, now)
            days = (due.date() - now.date()).days if due else None
            if due_filter == "overdue" and due and due >= now:
                return False
            if due_filter == "today" and days != 0:
                return False
            if due_filter == "next7" and (days is None or not 0 <= days <= 7):
                return False
    updated_filter = query["updated"]
    cutoff = card_format.relative_date(updated_filter.removeprefix("older-"), now.date())
    if cutoff:
        # ISO dates compare as text; a card without `updated` counts as old.
        recent = str(item.get("updated") or "")[:10] >= cutoff.isoformat()
        if recent == updated_filter.startswith("older-"):
            return False
    search = query["search"].strip().lower()
    if search:
        text = "\n".join(str(item.get(key) or "") for key in
                         ("title", "ref", "id", "next_action", "body"))
        text += "\n" + " ".join(item.get("tags", []))
        if search not in text.lower():
            return False
    return True


def task_rank(item):
    return item.get("display_rank") or [0, 0, "", item.get("path") or item["id"]]


def recent_rank(item):
    return (item.get("completed") or item.get("updated") or "", item.get("path") or item["id"])


def collection_results(items, values=None, *, now=None):
    """Return ordered IDs per collection.

    `cards` holds live cards: tasks by rank (done last, by recency), then notes by
    recency. `archive` holds archived cards and done tasks by recency. The client
    filters either by kind.
    """
    query = normalize_query(values)
    now = now or datetime.now()
    visible = [item for item in items if query["show_hidden"] or not item.get("hidden")]
    live = [item for item in visible if not item.get("archived")]
    tasks = [item for item in live if item["kind"] == "task" and matches(item, query, now)]
    notes = [item for item in live if item["kind"] == "note" and matches(item, query, now, status=False)]
    ordered = sorted((item for item in tasks if item.get("status") != "done"), key=task_rank)
    ordered += sorted((item for item in tasks if item.get("status") == "done"), key=recent_rank, reverse=True)
    notes.sort(key=lambda item: (item.get("updated") or item.get("created") or "",
                                 item.get("path") or item["id"]), reverse=True)
    archived = [item for item in visible
                if (item.get("archived") or (item["kind"] == "task" and item.get("status") == "done"))
                and matches(item, query, now, status=False)]
    collections = {
        "cards": ordered + notes,
        "open": [item for item in ordered if not item.get("hidden") and item.get("status") != "done"],
        "archive": sorted(archived, key=recent_rank, reverse=True),
        # Deadlines include hidden cards but follow the board filters, so one
        # project's board shows only that project's dates.
        "deadlines": sorted((item for item in items if not item.get("archived") and item.get("due")
                             and item.get("status") != "done"
                             and matches(item, query, now, status=item["kind"] == "task")),
                            key=lambda item: due_datetime(item["due"], now) or datetime.max.replace(tzinfo=now.tzinfo)),
    }
    return {name: [item["id"] for item in values] for name, values in collections.items()}
