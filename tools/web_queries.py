"""Dashboard collections: each one is the request's query narrowed for that view, then ordered."""
from dataclasses import replace
from datetime import datetime

import card_query  # on sys.path via tools/taskweb.py, which inserts scripts/


def card_id(card):
    return str(card.frontmatter.get("id") or card.path.stem)


def recent_rank(card):
    return (str(card.frontmatter.get("completed") or card.frontmatter.get("updated") or ""), card_id(card))


def _narrowed(query, **allowed):
    for field, values in allowed.items():
        query = card_query.narrow(query, field, values)
    return query


def collection_results(cards, query, *, now=None, done_cutoff=None, open_statuses, display_rank=None):
    """Return ordered card IDs per collection.

    `tasks` holds live tasks by rank (done last, by recency); `notes` holds live notes by recency.
    `archive` holds archived cards and done tasks by recency, with `archive_tasks` and
    `archive_notes` as its parts. `open` holds open, visible tasks for the Focus view and
    `deadlines` cards with a due date, hidden ones included, by due time. Each is `query` narrowed
    with `replace` and `card_query.narrow`; `done_cutoff` leaves old done tasks out of `tasks`.
    """
    now = now or datetime.now()
    rank = display_rank or (lambda card: card_id(card))

    def pick(derived, cutoff=None):
        return card_query.select(cards, derived, now=now, done_cutoff=cutoff) if derived else []

    # Notes have no status, priority, or type, so those filters must not hide them.
    note_base = replace(query, status=(), priority=(), type=())
    live_tasks = _narrowed(query, kind=("task",))
    live_notes = _narrowed(note_base, kind=("note",))
    tasks = pick(live_tasks, done_cutoff)
    ordered = sorted((card for card in tasks if card.frontmatter.get("status") != "done"), key=rank)
    ordered += sorted((card for card in tasks if card.frontmatter.get("status") == "done"),
                      key=recent_rank, reverse=True)
    notes = sorted(pick(live_notes), reverse=True, key=lambda card: (
        str(card.frontmatter.get("updated") or card.frontmatter.get("created") or ""), card_id(card)))
    open_query = _narrowed(replace(query, archived="exclude", hidden="exclude"),
                           kind=("task",), status=open_statuses)

    # The archive ignores the status filter: it holds archived cards and every done task.
    archive_tasks = {card_id(card): card
                     for derived in (_narrowed(replace(query, status=(), archived="only"), kind=("task",)),
                                     _narrowed(replace(query, status=("done",), archived="include"), kind=("task",)))
                     for card in pick(derived)}
    archive_tasks = sorted(archive_tasks.values(), key=recent_rank, reverse=True)
    archive_notes = sorted(pick(_narrowed(replace(note_base, archived="only"), kind=("note",))),
                           key=recent_rank, reverse=True)

    # Deadlines follow the board filters, so one project's board shows only that project's dates.
    def dated(derived):
        # Every other due mode already needs a due date; "none" asks for cards a deadline list cannot hold.
        modes = tuple(mode for mode in derived.due if mode != card_query.DUE_NONE)
        modes = modes or (() if derived.due else (card_query.DUE_ANY,))
        return replace(derived, hidden="include", due=modes) if modes else None

    deadline_tasks = _narrowed(query, kind=("task",), status=open_statuses)
    deadline_notes = live_notes
    deadlines = pick(dated(deadline_tasks) if deadline_tasks else None) + pick(
        dated(deadline_notes) if deadline_notes else None)
    deadlines.sort(key=lambda card: card_query.due_moment(card.frontmatter["due"], now)
                   or datetime.max.replace(tzinfo=now.tzinfo))

    collections = {
        "tasks": ordered,
        "notes": notes,
        "open": sorted(pick(open_query), key=rank),
        "archive": sorted(archive_tasks + archive_notes, key=recent_rank, reverse=True),
        "archive_tasks": archive_tasks,
        "archive_notes": archive_notes,
        "deadlines": deadlines,
    }
    return {name: [card_id(card) for card in found] for name, found in collections.items()}
