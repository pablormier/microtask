"""Dashboard payload serialization, detail lookup, and query validation."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sys

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(APP_ROOT / "scripts"))

import card_format  # noqa: E402
import card_query  # noqa: E402
import drafts as draft_store  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace  # noqa: E402

try:
    from .web_queries import collection_results
except ImportError:
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from web_queries import collection_results

MAX_INCLUDED_IDS = 8


def workspace_key(ws: Workspace, user: str | None = None) -> str:
    """Names the workspace in browser storage. In a shared workspace it also names the signed-in
    person, so that one browser profile never offers a person another's unsaved edits."""
    seed = str(ws.root.resolve()) if user is None else f"{ws.root.resolve()}\n{user}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def bootstrap_config(ws: Workspace, token: str, user: str | None = None) -> dict:
    return {"api_token": token, "workspace_key": workspace_key(ws, user)}


def extract_excerpt(text: str, max_len: int = 180) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if re.fullmatch(r"\s*(?:`{3,}|~{3,})[^`~]*|\s*(?:\$\$|\\\[|\\\])\s*|\s*([-*_])(?:\s*\1){2,}\s*", line):
            continue
        # Table rows read as their cells; the delimiter row under the header carries no text.
        if re.fullmatch(r"\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*", line):
            continue
        if line.lstrip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            line = " · ".join(cell for cell in cells if cell)
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        line = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", line)
        line = re.sub(r"^\s*>\s*", "", line)
        if line.strip():
            cleaned_lines.append(line.strip())
    clean_content = " ".join(cleaned_lines)
    clean_content = re.sub(r'''!?\[([^\]]*)\]\((?:[^\s()]|\([^()]*\))*(?:\s+["'][^"']*["'])?\s*\)''', r"\1", clean_content)
    clean_content = re.sub(r"`([^`]+)`", r"\1", clean_content).replace("`", "")
    # Math keeps its source text without the delimiters the detail view renders with KaTeX.
    clean_content = re.sub(r"\$\$(.+?)\$\$|\\\((.+?)\\\)|\\\[(.+?)\\\]",
                           lambda match: (match[1] or match[2] or match[3]).strip(), clean_content)
    clean_content = re.sub(r"(?<![\w$\\])\$([^\s$](?:[^$]*[^\s$])?)\$(?![\w$])", r"\1", clean_content)
    clean_content = re.sub(r"\*\*([^*]+)\*\*|~~([^~]+)~~", lambda match: match[1] or match[2], clean_content)
    clean_content = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"\1", clean_content)
    clean_content = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", clean_content)
    clean_content = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", clean_content)
    excerpt = " ".join(clean_content.split())
    if len(excerpt) > max_len:
        excerpt = excerpt[:max_len - 3].rstrip() + "..."
    return excerpt


def _card_summary_fields(task: store.Card) -> tuple[str, str]:
    cached = getattr(task, "_summary_fields", None)
    if cached is not None:
        return cached
    fields = (extract_excerpt(task.body), hashlib.sha256(task.raw.encode("utf-8")).hexdigest())
    try:
        object.__setattr__(task, "_summary_fields", fields)
    except AttributeError:
        pass
    return fields


def task_to_dict(
    task: store.Card,
    reference_index: store.ReferenceIndex | None = None,
    *,
    ws: Workspace,
    detail: bool = True,
    cfg: dict | None = None,
    rank: dict[str, int] | None = None,
) -> dict:
    cfg = cfg if cfg is not None else store.policy(ws)
    fm = task.frontmatter
    tags = fm.get("tags", [])
    body = task.body
    if rank is None:
        rank = {name: index for index, name in enumerate(cfg.get("priorities", {}))}
    ws_root = ws.root
    processing_instructions = fm.get("processing_instructions", "")
    comments = fm.get("comments", [])
    time_entries = fm.get("time_entries", [])
    excerpt, content_hash = _card_summary_fields(task)
    result = {
        "id": fm["id"],
        "kind": task.kind,
        "ref": f"@{task.short_ref}",
        "status": fm.get("status", ""),
        "priority": fm["priority"],
        "type": fm.get("type", ""),
        "project": fm.get("project"),
        "topic": fm.get("topic"),
        "due": fm.get("due"),
        "due_kind": store.due_kind_value(fm),
        "created": fm["created"],
        "updated": fm["updated"],
        "completed": fm.get("completed", ""),
        "claimed_by": fm.get("claimed_by"),
        "claimed_at": fm.get("claimed_at"),
        "archived": store.is_archived(task),
        "hidden": store.is_hidden(task),
        "tags": list(tags),
        "title": task.title,
        "next_action": task.next_action,
        "needs_restart": operations.next_action_needs_restart_step(task),
        "display_rank": operations.task_display_sort_key(task, rank),
        "excerpt": excerpt,
        "content_hash": content_hash,
        "needs_review": fm.get("needs_review") is True,
        "time_seconds": store.saved_time_seconds(task) if task.kind == "task" else 0,
        "comment_count": len(comments) if isinstance(comments, list) else 0,
        "path": str(task.path.relative_to(ws_root)) if task.path.is_relative_to(ws_root) else str(task.path),
    }
    if detail:
        references = store.item_references(task, reference_index) if reference_index else []
        result.update({
            "body": body,
            "processing_instructions": processing_instructions,
            "comments": comments,
            "time_entries": time_entries,
            "references": references,
            "reference_spans": {
                "body": reference_index.occurrences(body),
                "next_action": reference_index.occurrences(task.next_action),
                "processing_instructions": reference_index.occurrences(processing_instructions),
                "comments": [reference_index.occurrences(comment["text"]) for comment in comments],
            } if reference_index else {},
        })
    return result


def dashboard_detail(ws: Workspace, cache: store.CardCache, identity: str) -> dict | None:
    transaction.migrate_legacy_timers_if_pending(ws)
    items = store.discover(cache=cache, ws=ws)
    item = next((item for item in items if str(item.frontmatter.get("id") or item.path.stem) == identity), None)
    return task_to_dict(item, store.ReferenceIndex(items), ws=ws) if item else None


def dashboard_meta(
    ws: Workspace,
    *,
    api_token: str = "",
    items: list[store.Card] | None = None,
    cfg: dict | None = None,
    demo: bool = False,
    user: str | None = None,
) -> dict:
    cfg = cfg if cfg is not None else store.policy(ws)
    default_status, default_priority, default_type = operations.task_defaults(cfg)
    statuses = list(cfg.get("statuses", {}))
    configured_status_labels = cfg.get("statuses", {})
    if not isinstance(configured_status_labels, dict):
        configured_status_labels = {}
    status_labels = {
        status: str(configured_status_labels.get(status) or status)
        for status in statuses
    }
    configured_view_labels = cfg.get("view_labels", {})
    if not isinstance(configured_view_labels, dict):
        configured_view_labels = {}
    view_labels = {"archive": "Archive"}
    view_labels.update({
        str(name): str(label)
        for name, label in configured_view_labels.items()
        if label is not None and str(label)
    })
    rules = cfg.get("rules", {})
    raw_limit = rules.get("max_doing_tasks") if isinstance(rules, dict) else None
    max_doing = int(str(raw_limit)) if raw_limit is not None and str(raw_limit).isdigit() else 1
    item_values = items or []
    # Timers live on the cards the payload already loaded, so a refresh reads no extra files.
    timers = {
        store.card_task_id(card): {**timer, "saved": store.timer_session_saved(card, timer),
                                   "title": card.title, "ref": f"@{card.short_ref}"}
        for card in item_values
        if card.kind == "task" and (timer := card_format.timer_from(card.frontmatter)) is not None
    }
    workspace_tags: set[str] = set()
    usage: dict[str, dict[str, int]] = {"project": {}, "topic": {}, "tag": {}}
    for item in item_values:
        raw_tags = item.frontmatter.get("tags") or []
        tag_values = raw_tags if isinstance(raw_tags, list) else [raw_tags]
        workspace_tags.update(str(tag) for tag in tag_values if tag)
        for tag in {str(tag) for tag in tag_values if tag}:
            usage["tag"][tag] = usage["tag"].get(tag, 0) + 1
        for field in ("project", "topic"):
            if item.frontmatter.get(field):
                name = str(item.frontmatter[field])
                usage[field][name] = usage[field].get(name, 0) + 1
    return {
        "workspace_key": workspace_key(ws, user),
        "status_labels": status_labels,
        "view_labels": view_labels,
        "priority_order": list(cfg.get("priorities", {}).keys()),
        "due_kinds": list(cfg.get("due_kinds", {}).keys()),
        "task_defaults": {"status": default_status, "priority": default_priority, "type": default_type},
        "task_statuses": [status for status in statuses if status != "done"],
        "task_priorities": cfg.get("priorities", {}),
        "task_types": cfg.get("types", {}),
        "task_due_kinds": cfg.get("due_kinds", {}),
        "suggested_tags": cfg.get("suggested_tags", []),
        "workspace_projects": sorted({str(item.frontmatter["project"]) for item in item_values if item.frontmatter.get("project")}),
        "topic_tree": card_format.topic_tree(usage["topic"], operations.suggested_topics(cfg)),
        "workspace_tags": sorted(workspace_tags),
        "workspace_usage": usage,
        "max_doing_tasks": max_doing,
        "show_year_progress": bool(rules.get("show_year_progress", True)),
        "show_deadline_progress": bool(rules.get("show_deadline_progress", True)),
        "deadline_progress_start": str(rules.get("deadline_progress_start") or "created"),
        "api_token": api_token,
        "demo": bool(demo),
        "time_running": timers,
        "timer_warn_hours": float(rules.get("timer_warn_hours", 8)),
    }


def included_ids(value: object) -> list[str]:
    """Card IDs a client asked to receive whatever the filters say; a short list of text only."""
    values = value if isinstance(value, list) else []
    return [item for item in values if isinstance(item, str) and item][:MAX_INCLUDED_IDS]


def request_query(ws: Workspace, values: object) -> card_query.Query:
    """The validated query a request's parameters describe; raises QueryError with a message for the client."""
    if values is not None and not isinstance(values, dict):
        raise card_query.QueryError("query must be an object")
    query = card_query.query_from_mapping(values or {})
    card_query.validate(query, statuses=store.statuses(ws), priorities=list(store.policy(ws).get("priorities", {})))
    return query


def view_topic_trees(ws: Workspace, cards: list[store.Card], query: card_query.Query, *, now: datetime,
                     done_cutoff: object, topics: list[str]) -> dict[str, list[dict[str, object]]]:
    """The topics panel's tree for each view that shows it, counting the cards that view would show.

    Counts follow every filter except the topic itself, so choosing a topic does not zero the
    others. Every workspace topic stays in each tree, with no count where the view has none.
    """
    by_id = {str(card.frontmatter.get("id") or card.path.stem): card for card in cards}
    found = collection_results(cards, replace(query, topic=()), now=now, done_cutoff=done_cutoff,
                               open_statuses=store.open_statuses(ws))
    trees = {}
    for view in ("tasks", "notes", "archive"):
        counts: dict[str, int] = {}
        for identity in found[view]:
            topic = by_id[identity].frontmatter.get("topic")
            if topic:
                counts[str(topic)] = counts.get(str(topic), 0) + 1
        trees[view] = card_format.topic_tree(counts, topics)
    return trees


def draft_to_dict(draft: dict) -> dict:
    return {**draft, "excerpt": extract_excerpt(str(draft.get("body") or ""))}


def draft_list_payload(root: Path, viewer: str | None = None) -> list[dict]:
    """The drafts to show. In a shared workspace `viewer` limits them to that person's own."""
    return [draft_to_dict(draft) for draft in draft_store.list_drafts(root, viewer)]


def dashboard_payload(ws: Workspace, cache: store.CardCache, *, api_token: str = "", query: card_query.Query | None = None,
                      include: object = None, demo: bool = False, actor: str | None = None,
                      hosted: dict | None = None) -> dict:
    """Collections for ``query`` plus only the card summaries a client needs to show them.

    ``tasks`` holds the cards in any collection, the targets of saved drafts, and the
    ``include`` IDs (the open card and its navigation trail), which are echoed back.
    Workspace-wide figures are in ``meta``. In a shared workspace ``actor`` is the signed-in
    person: focus, drafts and the doing count are theirs, and ``hosted`` names them in ``meta``.
    """
    transaction.migrate_legacy_timers_if_pending(ws)
    cfg = store.policy(ws)
    query = query or card_query.DEFAULT
    statuses = list(cfg.get("statuses", {}))
    tasks = store.discover(cache=cache, ws=ws)
    ordered = sorted(
        tasks,
        key=lambda t: statuses.index(t.frontmatter.get("status") or "")
        if (t.frontmatter.get("status") or "") in statuses
        else len(statuses),
    )
    meta = dashboard_meta(ws, api_token=api_token, items=tasks, cfg=cfg, demo=demo, user=actor)
    now = datetime.now()
    meta["focus"] = operations.focus_context(card_query.select(tasks, card_query.Query(kind=("task",)), now=now),
                                             actor=actor, focus_user=actor, ws=ws)
    if hosted is not None:
        meta["hosted"] = hosted
    rank = {name: index for index, name in enumerate(cfg.get("priorities", {}))}
    done_cutoff = card_query.default_done_cutoff(query, store.done_window(ws), date.today())
    collections = collection_results(
        ordered, query, now=now, open_statuses=store.open_statuses(ws), done_cutoff=done_cutoff,
        display_rank=lambda card: operations.task_display_sort_key(card, rank))
    meta["topic_trees"] = view_topic_trees(ws, ordered, query, now=now, done_cutoff=done_cutoff,
                                           topics=[*meta["workspace_usage"]["topic"], *operations.suggested_topics(cfg)])
    drafts = draft_list_payload(ws.root, actor)
    included = included_ids(include)
    wanted = {identity for ids in collections.values() for identity in ids} | set(included)
    meta["draft_count"] = len(drafts)
    # The Focus view shows only open cards that pass the filters, so send the focus entries for those.
    shown = set(collections["open"])
    for key in ("candidates", "alerts"):
        meta["focus"][key] = [entry for entry in meta["focus"][key] if entry["id"] in shown]
    meta["focus"]["followup_ids"] = [identity for identity in meta["focus"]["followup_ids"] if identity in shown]
    doing = card_query.select(tasks, card_query.Query(status=("doing",), hidden="include"), now=now)
    if cfg.get("rules", {}).get("doing_limit_scope", "workspace") == "per_claimant":
        # The limit applies to each person's own doing tasks, so the board counts the signed-in person's.
        person = store.current_username(actor, ws=ws)
        doing = [card for card in doing if card.frontmatter.get("claimed_by") == person]
    meta["doing_count"] = len(doing)
    # Changes when any card is added, removed, renamed or converted, so an open card can refresh its references.
    meta["reference_revision"] = hashlib.sha256(json.dumps(sorted(
        [str(card.frontmatter["id"]), f"@{card.short_ref}", card.title, card.kind] for card in ordered
    )).encode("utf-8")).hexdigest()[:16]

    selected_cards = [
        card for card in ordered
        if str(card.frontmatter["id"]) in wanted
    ]
    return {
        "tasks": [task_to_dict(card, ws=ws, detail=False, cfg=cfg, rank=rank) for card in selected_cards],
        "statuses": statuses,
        "meta": meta,
        "query": card_query.query_to_mapping(query),
        # Echoed so a client can tell a card that is gone from one this request never asked for.
        "include": included,
        "collections": collections,
    }


def search_cards(ws: Workspace, cache: store.CardCache, query_text: str, *, limit: int = 12) -> dict:
    """Return ranked card summaries across the workspace for the quick-search palette."""
    cfg = store.policy(ws)
    rank = {name: index for index, name in enumerate(cfg.get("priorities", {}))}
    cards = store.discover(cache=cache, ws=ws)
    needle = (query_text or "").strip()
    capped = max(1, min(int(limit), 30))

    def _recency_key(card: store.Card) -> tuple:
        return (
            not store.is_archived(card),
            not store.is_hidden(card),
            str(card.frontmatter.get("updated") or card.frontmatter.get("created") or ""),
            str(card.frontmatter.get("id") or card.path.stem),
        )

    if not needle:
        matched = sorted(cards, key=_recency_key, reverse=True)[:capped]
    else:
        scored: list[tuple[tuple, store.Card]] = []
        for card in cards:
            score = card_query.text_score(card, needle, "and")
            if score is not None:
                scored.append(((
                    not store.is_archived(card),
                    score,
                    not store.is_hidden(card),
                    str(card.frontmatter.get("updated") or card.frontmatter.get("created") or ""),
                    str(card.frontmatter.get("id") or card.path.stem),
                ), card))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        matched = [card for _, card in scored[:capped]]

    return {
        "items": [task_to_dict(card, ws=ws, detail=False, cfg=cfg, rank=rank) for card in matched],
        "query": needle,
    }
