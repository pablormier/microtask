"""Card store, lookups, cache, and error types for microtask."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
import getpass
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time as clock_time

import card_format
import card_query
import card_references
import drafts as draft_store
import workspace
from card_format import CLAIM_FIELDS, parse_frontmatter, parse_yaml, time_entries_from
from workspace import Workspace

SHORT_REF_RE = re.compile(r"^@[0-9a-f]{3,8}$", re.IGNORECASE)
KIND_LINE_RE = re.compile(r"""^kind:\s*["']?(task|note)["']?\s*$""")
TIME_DURATION_RE = re.compile(r"(\d+)([smhd])")
FOCUS_REASONS = {
    "due_today": "due today",
    "high_overdue": "overdue and high priority",
    "doing": "already started",
    "due_soon": "deadline in the next 7 days",
    "deadline_review": "needs deadline review",
    "active": "ready to start",
    "inbox": "needs a small restart step",
    "manual": "manually focused",
    "cycle": "cycled focus",
    "default": "time-sensitive",
}
RESTART_COMMANDS = {
    "inbox": "mt read {ref}",
    "active": "mt move {ref} doing",
    "waiting": "mt followup",
    "default": "mt read {ref}",
}
VAGUE_RESTART_PATTERN = r"^(plan|research|work on|continue|figure out)\b"
atomic_write = draft_store.atomic_write
is_timestamp = card_format.is_timestamp
is_due_value = card_format.is_metadata_time
time_log_entries = card_format.time_log_entries


def _ws(ws: Workspace | None) -> Workspace:
    return ws if ws is not None else Workspace.resolve()



class TaskError(Exception):
    """Base error for core workspace operations."""

    exit_code: int = 1
    prefixed: bool = True

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        prefixed: bool | None = None,
        sync_needed: bool = False,
    ) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code
        if prefixed is not None:
            self.prefixed = prefixed
        self.sync_needed = sync_needed



class InputError(TaskError, ValueError):
    """Invalid caller input or disallowed operation arguments (HTTP 400)."""

    exit_code = 2



class NotFoundError(TaskError):
    """Requested card or draft does not exist (HTTP 404)."""

    exit_code = 1
    prefixed = False



class ConflictError(TaskError):
    """Claim, ambiguity, or optimistic-concurrency conflict (HTTP 409)."""

    exit_code = 1



class ValidationError(TaskError, ValueError):
    """Card or workspace validation failed and changes were rolled back (HTTP 400)."""

    exit_code = 1



def now_stamp() -> str:
    return datetime.now().replace(microsecond=0).isoformat()



def due_sort_value(value: object) -> str:
    """A sortable local ISO time for a due value (a date alone counts as the end of its day); "" when none."""
    moment = card_query.due_moment(value, datetime.now()) if value else None
    return moment.isoformat() if moment else ""



def due_kind_value(fm: dict[str, object]) -> object:
    return fm.get("due_kind")



def due_kind_error(value: object, due: object, cfg: dict | None = None) -> str | None:
    if due is None:
        return None if value is None else "due_kind must be null when due is null"
    allowed = (cfg or policy()).get("due_kinds", {})
    if not isinstance(value, str) or not isinstance(allowed, dict) or value not in allowed:
        choices = ", ".join(str(name) for name in allowed) if isinstance(allowed, dict) else ""
        return f"due_kind must be one of {choices or 'the effective policy values'} when due is set"
    return None



def done_window(ws: Workspace | None = None) -> str:
    """The age (rules.done_window, default 2w) after which done tasks drop out of default views."""
    return str(policy(ws).get("rules", {}).get("done_window", "2w"))



def is_archived(item: "Card") -> bool:
    return item.frontmatter.get("archived") is True



def is_hidden(item: "Card") -> bool:
    return item.frontmatter.get("hidden") is True



def item_state_suffix(item: "Card") -> str:
    states = [name for name, flagged in (("archived", is_archived(item)), ("hidden", is_hidden(item))) if flagged]
    return f" ({', '.join(states)})" if states else ""



def real_cards_root(ws: Workspace | None = None) -> Path:
    return _ws(ws).real_cards_root()



def demo_root(ws: Workspace | None = None) -> Path:
    return _ws(ws).demo_root()



def active_root(ws: Workspace | None = None) -> Path:
    return _ws(ws).active_root()



def pin_demo_state(ws: Workspace | None = None) -> None:
    """Pin the demo decision once per operation so mid-command mutations stay consistent."""
    _ws(ws).pin_demo()



def demo_active(ws: Workspace | None = None) -> bool:
    return _ws(ws).demo_active()



def maybe_seed_demo(ws: Workspace | None = None) -> None:
    _ws(ws).maybe_seed_demo()



def cards_root(ws: Workspace | None = None) -> Path:
    return _ws(ws).cards_root()



def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)



def parse_duration(value: str) -> int:
    position = 0
    total = 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for match in TIME_DURATION_RE.finditer(value):
        if match.start() != position:
            break
        total += int(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(value) or total < 1:
        raise ValueError("duration must use positive units such as 45s, 90m, or 1h30m")
    return total



@dataclass(frozen=True)
class Card:
    """One task or note card; ``kind`` decides whether lifecycle fields apply."""

    path: Path
    raw: str
    frontmatter_text: str
    body: str
    frontmatter: dict[str, object]

    @classmethod
    def read(cls, path: Path) -> "Card":
        with path.open(encoding="utf-8", newline="") as source_file:
            raw = source_file.read()
        frontmatter_text, body = parse_frontmatter(raw)
        if frontmatter_text is None:
            raise ValueError(f"{path}: missing YAML frontmatter")
        properties = parse_yaml(frontmatter_text)
        missing = {"title", "kind"} - set(properties)
        if missing:
            raise ValueError(f"{path}: missing {', '.join(sorted(missing))} properties")
        card_format.require_valid_properties(properties)
        return cls(path, raw, frontmatter_text, body, properties)

    @property
    def title(self) -> str:
        title = self.frontmatter.get("title")
        return title if isinstance(title, str) else self.path.stem

    @property
    def kind(self) -> str:
        return str(self.frontmatter.get("kind"))

    @property
    def is_task(self) -> bool:
        return self.kind == "task"

    @property
    def is_note(self) -> bool:
        return self.kind == "note"

    @property
    def next_action(self) -> str:
        value = self.frontmatter.get("next_action") if self.is_task else None
        return value if isinstance(value, str) else ""

    @property
    def short_ref(self) -> str:
        return hashlib.sha256(self.path.stem.encode()).hexdigest()[:8]



def policy(ws: Workspace | None = None) -> dict:
    return _ws(ws).policy()



def view_marker(ws: Workspace | None = None) -> dict[str, object] | None:
    """Read and validate the provenance marker for a materialized view."""
    return _ws(ws).view_marker()



def statuses(ws: Workspace | None = None) -> list[str]:
    return _ws(ws).statuses()



def open_statuses(ws: Workspace | None = None) -> list[str]:
    return [name for name in statuses(ws) if name != "done"]



def focus_state_path(ws: Workspace | None = None) -> Path:
    return _ws(ws).focus_state_path()



@dataclass(frozen=True)
class FocusState:
    task_id: str
    mode: str



def read_focus_state(ws: Workspace | None = None) -> FocusState | None:
    path = focus_state_path(ws)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return FocusState(raw, "manual")
    if not isinstance(data, dict):
        return None
    task_id = data.get("task_id")
    mode = data.get("mode")
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if mode not in {"manual", "cycle"}:
        return None
    return FocusState(task_id.strip(), str(mode))



def write_focus_task_id(task_id: str, ws: Workspace | None = None) -> None:
    write_focus_state(task_id, "manual", ws=ws)



def write_focus_state(task_id: str, mode: str, ws: Workspace | None = None) -> None:
    path = focus_state_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"task_id": task_id, "mode": mode}, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )



def clear_focus_task_id(ws: Workspace | None = None) -> None:
    try:
        focus_state_path(ws).unlink()
    except FileNotFoundError:
        pass



def time_timer_path(ws: Workspace | None = None) -> Path:
    return _ws(ws).time_timer_path()



def local_stamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds).replace(microsecond=0).isoformat()



def parse_time_argument(value: str) -> int:
    """Accept a naive local ISO timestamp or today's HH:MM[:SS]."""
    try:
        if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value):
            value = local_stamp(int(clock_time.time()))[:10] + "T" + value
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None or "T" not in value and " " not in value:
            raise ValueError()
        return int(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        raise ValueError("TIME must be a local YYYY-MM-DDTHH:MM[:SS] or today's HH:MM[:SS] (without offset)") from None



def time_entry_from_timer(timer: dict[str, object], end_epoch: int) -> dict[str, object]:
    start_epoch = int(timer["start_epoch"])
    elapsed = max(0, end_epoch - start_epoch)
    return {
        "session_id": str(timer["session_id"]),
        "start": str(timer["start"]),
        "end": local_stamp(end_epoch),
        "elapsed_seconds": elapsed,
    }



def task_by_id(task_id: str, ws: Workspace | None = None) -> Card | None:
    return next(
        (task for task in discover("task", ws=ws) if str(task.frontmatter.get("id") or task.path.stem) == task_id),
        None,
    )



def timer_session_saved(task_id: str, timer: dict[str, object], ws: Workspace | None = None) -> bool:
    task = task_by_id(task_id, ws=ws)
    if task is None:
        return False
    return any(
        entry["session_id"] == timer["session_id"]
        for entry in time_log_entries(task.raw)
    )



def time_session_for(
    task_id: str, timer: dict[str, object], end_epoch: int, ws: Workspace | None = None,
) -> tuple[dict[str, object], bool]:
    """Return one structured session and whether the task already has its ID."""
    task = task_by_id(task_id, ws=ws)
    if task is None:
        raise ValueError(f"running timer task no longer exists: {task_id}")
    session_id = str(timer["session_id"])
    if any(entry["session_id"] == session_id for entry in time_log_entries(task.raw)):
        return {}, True
    return time_entry_from_timer(timer, end_epoch), False



def saved_time_seconds(task: Card) -> int:
    # The card's properties are already parsed and validated; reparsing task.raw doubled load time.
    return sum(int(entry["elapsed_seconds"]) for entry in time_entries_from(task.frontmatter))



def card_kind_hint(path: Path) -> str | None:
    """Cheaply guess a card's kind from its frontmatter header; None when unclear."""
    try:
        with path.open(encoding="utf-8", newline="") as source_file:
            if source_file.readline().rstrip("\r\n") != "---":
                return None
            for line in source_file:
                line = line.rstrip("\r\n")
                if line == "---":
                    return None
                match = KIND_LINE_RE.match(line)
                if match:
                    return match.group(1)
    except (OSError, UnicodeDecodeError):
        return None
    return None



class CardCache:
    """Parsed cards keyed by path, reused while the file's stat signature is unchanged."""

    def __init__(self) -> None:
        self.entries: dict[Path, tuple[tuple[int, int, int, int], Card]] = {}
        self.lock = threading.Lock()

    def read(self, path: Path) -> Card:
        """Like ``Card.read``; the caller holds ``lock``."""
        try:
            stat = path.stat()  # Stat before reading: a change in between leaves an older signature, so the next call re-parses.
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            entry = self.entries.get(path)
            if entry is not None and entry[0] == signature:
                return entry[1]
            card = Card.read(path)
        except BaseException:
            self.entries.pop(path, None)
            raise
        self.entries[path] = (signature, card)
        return card

    def prune(self, root: Path, kept: set[Path]) -> None:
        """Forget cached files directly in ``root`` that are not in ``kept``."""
        for path in [path for path in self.entries if path.parent == root and path not in kept]:
            del self.entries[path]



def read_cards(
    root: Path, kind: str | None = None, *, cache: CardCache | None = None, ws: Workspace | None = None,
) -> list[Card]:
    """Read and validate the cards in ``root``; the kind hint only skips files that cannot match ``kind``.

    With a ``cache``, unchanged files are not parsed again; validation still runs on every call.
    """
    if not root.is_dir():
        return []
    allowed = set(statuses(ws))
    cards = []
    listed = sorted(root.glob("*.md"))
    skipped = False
    with cache.lock if cache is not None else nullcontext():
        read = cache.read if cache is not None else Card.read
        for path in listed:
            if kind is not None:
                hint = card_kind_hint(path)
                if hint is not None and hint != kind:
                    skipped = True
                    continue
            card = read(path)
            if kind is not None and card.kind != kind:
                continue
            status = card.frontmatter.get("status")
            if card.is_task and (not isinstance(status, str) or status not in allowed):
                raise ValueError(
                    f"{card.path}: invalid or missing task status {status!r}; "
                    "custom status IDs must be defined in mt-policy.yaml"
                )
            cards.append(card)
        if cache is not None and not skipped:
            cache.prune(root, set(listed))
    return cards



def discover(kind: str | None = None, *, cache: CardCache | None = None, ws: Workspace | None = None) -> list[Card]:
    """All cards in the active workspace; `kind` only skips reading files of the other kind."""
    return read_cards(cards_root(ws), kind, cache=cache, ws=ws)



class ReferenceIndex:
    """Resolve short refs against one unfiltered workspace snapshot."""

    def __init__(self, items: list[Card]):
        self.prefixes: dict[str, list[Card]] = {}
        for item in items:
            short_ref = item.short_ref
            for length in range(3, 9):
                self.prefixes.setdefault(short_ref[:length], []).append(item)

    def resolve(self, ref: str) -> dict[str, object]:
        matches = self.prefixes.get(ref[1:].casefold(), [])
        result: dict[str, object] = {"ref": ref, "state": "missing"}
        if len(matches) > 1:
            result["state"] = "ambiguous"
        elif matches:
            target = matches[0]
            result.update(state="resolved", target={
                "id": str(target.frontmatter.get("id") or target.path.stem),
                "ref": f"@{target.short_ref}", "title": target.title, "kind": target.kind,
            })
        return result

    def occurrences(self, text: str) -> list[dict[str, object]]:
        return [{"start": start, "end": end, **self.resolve(text[start:end])}
                for start, end in card_references.reference_spans(text)]



def item_references(item: Card, index: ReferenceIndex) -> list[dict[str, object]]:
    values = [item.body, str(item.next_action or ""),
              str(item.frontmatter.get("processing_instructions") or "")]
    comments = item.frontmatter.get("comments", [])
    if isinstance(comments, list):
        values.extend(str(comment.get("text") or "") for comment in comments if isinstance(comment, dict))
    text = "\n\n".join(values)
    return [index.resolve(ref) for ref in card_references.extract_references(text)]



def item_json_record(
    item: Card, reference_index: ReferenceIndex | None = None, *, ws: Workspace | None = None,
) -> dict[str, object]:
    """Serialize one discovered item for CLI composition commands."""
    resolved = _ws(ws)
    fields = (
        "id", "status", "priority", "type", "project", "topic",
        "due", "due_kind", "created", "updated", "completed", "archived", "hidden", "tags",
        *CLAIM_FIELDS,
    )
    record = {field: item.frontmatter.get(field) for field in fields}
    record["due_kind"] = due_kind_value(item.frontmatter)
    record["hidden"] = is_hidden(item)
    record["needs_review"] = item.frontmatter.get("needs_review") is True
    record.update(
        {
            "kind": item.kind,
            "ref": f"@{item.short_ref}",
            "title": item.title,
            "next_action": item.next_action,
            "path": str(item.path.relative_to(resolved.root)),
            "content_hash": hashlib.sha256(item.raw.encode("utf-8")).hexdigest(),
        }
    )
    if reference_index is not None:
        record["references"] = item_references(item, reference_index)
    return record



def item_compact_json_record(item: Card) -> dict[str, object]:
    """Serialize one item as a compact minimal-field record for fast context searches."""
    record: dict[str, object] = {
        "ref": f"@{item.short_ref}",
        "id": item.frontmatter.get("id") or item.path.stem,
        "kind": item.kind,
        "title": item.title,
        "status": item.frontmatter.get("status") if item.is_task else None,
        "priority": item.frontmatter.get("priority"),
        "project": item.frontmatter.get("project"),
        "topic": item.frontmatter.get("topic"),
        "due": item.frontmatter.get("due"),
        "tags": list(item.frontmatter.get("tags") or []),
    }
    if item.is_task and item.frontmatter.get("type"):
        record["type"] = item.frontmatter.get("type")
    if item.frontmatter.get("due"):
        record["due_kind"] = due_kind_value(item.frontmatter)
    if item.frontmatter.get("needs_review") is True:
        record["needs_review"] = True
    if item.frontmatter.get("archived") is True:
        record["archived"] = True
    if is_hidden(item):
        record["hidden"] = True
    return record



def workspace_env(ws: Workspace | None = None) -> dict[str, str]:
    """Environment for this application's own children, so they stay on this workspace."""
    return _ws(ws).workspace_env()



def foreign_env(ws: Workspace | None = None) -> dict[str, str]:
    """Environment for another application copy, which must use its own workspace."""
    return _ws(ws).foreign_env()



def workspace_error(ws: Workspace | None = None) -> str | None:
    """Explain why MT_WORKSPACE cannot be used, or None when it is unset or valid."""
    resolved = _ws(ws)
    return workspace.workspace_error(resolved.environ, app_root=resolved.app_root)



def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50].rstrip("-")
    return slug or "task"



def current_username(explicit: str | None = None, ws: Workspace | None = None) -> str:
    resolved = _ws(ws)
    value = explicit if explicit is not None else resolved.env_default("MT_USERNAME")
    if value is None:
        value = policy(resolved).get("default_username")
    username = (value if value is not None else getpass.getuser()).strip()
    if not username or any(char in username for char in "\r\n\x00"):
        raise ValueError("username must be a non-empty, single-line name")
    return username



class CardInputError(InputError):
    """New-card input was rejected before anything was written."""

    exit_code = 1



def card_by_capture_id(capture_id: str, ws: Workspace | None = None) -> Card | None:
    # Search both kinds, including archived/hidden cards.
    for card in read_cards(real_cards_root(ws), ws=ws):
        if card.frontmatter.get("capture_id") == capture_id:
            return card
    return None



def items_by_ref(ref: str, ws: Workspace | None = None) -> list[Card]:
    """Resolve a filename-based ref before reading other cards' content."""
    if not SHORT_REF_RE.fullmatch(ref):
        raise ValueError("short reference must be @ followed by 3 to 8 hexadecimal characters")
    root = cards_root(ws)
    return [Card.read(path) for path in (sorted(root.glob("*.md")) if root.is_dir() else [])
            if hashlib.sha256(path.stem.encode()).hexdigest()[:8].startswith(ref[1:].lower())]



def _resolve_unique_card(
    resolved: Workspace,
    query: str,
    *,
    kind: str | None = None,
    statuses: list[str] | None = None,
    match_mode: str = "exact",
) -> Card:
    includes_notes = kind != "task" and statuses is None
    # Resolution sees every card, archived and hidden ones included; `statuses` limits it to those tasks.
    scope = card_query.Query(kind=(kind,) if kind else (), status=tuple(statuses or ()),
                             archived="include", hidden="include")
    try:
        if query.startswith("@") and not SHORT_REF_RE.fullmatch(query):
            raise ValueError("short reference must be @ followed by 3 to 8 hexadecimal characters")
        if statuses is not None and not statuses:
            candidates = []
        else:
            # A reference is found from file names, so unrelated cards are never read.
            found = items_by_ref(query, ws=resolved) if query.startswith("@") else discover(kind, ws=resolved)
            candidates = select_cards(found, scope)
        matches = [card for card in candidates if card_query.text_score(card, query, match_mode) is not None]
    except ValueError as error:
        raise InputError(str(error)) from error
    if not matches:
        label = "item" if includes_notes else "task"
        raise NotFoundError(f"No {label} matches '{query}'.")
    if len(matches) > 1:
        label = "items" if includes_notes else "tasks"
        lines = [f"Multiple {label} match '{query}':"]
        for task in matches:
            rel = task.path.relative_to(resolved.root) if task.path.is_relative_to(resolved.root) else task.path
            lines.append(f"  [@{task.short_ref}] {rel} - {task.title}")
            lines.append(f"    next: {task.next_action or '-'}")
        raise ConflictError("\n".join(lines), exit_code=2, prefixed=False)
    return matches[0]



def abbreviate(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else flat[:limit] + "...")



def item_receipt(item: Card, *, updated: bool = False, ws: Workspace | None = None) -> dict[str, object]:
    return {
        "ref": f"@{item.short_ref}",
        "path": str(item.path.relative_to(_ws(ws).root)),
        "content_hash": card_format.content_hash(item.raw),
        "updated": item.frontmatter.get("updated"),
        "changed": bool(updated),
    }



def saved_receipt(item: Card, *, updated: bool = False, ws: Workspace | None = None) -> dict[str, object]:
    """Receipt plus the saved card in the same fields as read --json."""
    return {**item_receipt(item, updated=updated, ws=ws), "properties": dict(item.frontmatter), "body": item.body}



def item_read_record(task: Card, ws: Workspace | None = None) -> dict[str, object]:
    """Serialize one card as `mt read --json` does; `--full` on list and search reuses it."""
    return {
        "ref": f"@{task.short_ref}",
        "path": str(task.path.relative_to(_ws(ws).root)),
        "content_hash": card_format.content_hash(task.raw),
        "properties": dict(task.frontmatter),
        "body": task.body,
    }



def command_query(args: argparse.Namespace, ws: Workspace | None = None) -> card_query.Query | None:
    """The validated query a command's selection flags describe, or None after printing the error."""
    resolved = _ws(ws)
    try:
        query = card_query.query_from_args(args)
        card_query.validate(
            query,
            statuses=statuses(resolved),
            priorities=list(policy(resolved).get("priorities", {})),
        )
    except card_query.QueryError as error:
        print(f"error: {error}", file=sys.stderr)
        return None
    return query



def select_cards(cards: list[Card], query: card_query.Query, *, done_cutoff: str | None = None) -> list[Card]:
    return card_query.select(cards, query, now=datetime.now(), done_cutoff=done_cutoff)



def sort_card_items(
    items: list[Card],
    *,
    field: str,
    order: str,
    priority_rank: dict[str, int],
    relevance_scores: dict[Path, float] | None = None,
) -> list[Card]:
    """Sort cards with nulls last and an ascending stable ID tie-break."""
    def sort_value(item: Card) -> object | None:
        if field == "relevance":
            return (relevance_scores or {}).get(item.path)
        value = item.title if field == "title" else item.frontmatter.get(field)
        if value is None or value == "":
            return None
        if field == "priority":
            return (-priority_rank.get(str(value), len(priority_rank)), str(value).casefold())
        if field == "title":
            return str(value).casefold()
        return str(value)

    sorted_by_id = sorted(items, key=lambda item: str(item.frontmatter.get("id") or item.path.stem))
    present = [item for item in sorted_by_id if sort_value(item) is not None]
    missing = [item for item in sorted_by_id if sort_value(item) is None]
    present.sort(key=sort_value, reverse=order == "desc")
    return present + missing



def session_entries(task: Card, window: tuple[str | None, str | None]) -> list[dict[str, object]]:
    """The task's recorded sessions that started inside the window: since <= start day < before."""
    since, before = window
    return [entry for entry in time_entries_from(task.frontmatter)
            if (not since or str(entry["start"])[:10] >= since) and (not before or str(entry["start"])[:10] < before)]


Task = Card
Note = Card
