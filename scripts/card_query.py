"""The one way to select cards: field names, parsing, validation, and matching.

The CLI, the dashboard server, and saved bulk-edit plans translate their input
into a `Query` and use the cards `select` returns. Nothing else matches cards
against filter criteria. Standard library only; policy values come in as
arguments so this module never imports tasklib.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time, timedelta
from typing import Mapping

import card_format

KINDS = ("task", "note")
DUE_MODES = ("overdue", "today", "week", "any", "none")
DUE_OVERDUE, DUE_TODAY, DUE_WEEK, DUE_ANY, DUE_NONE = DUE_MODES
VISIBILITY = ("exclude", "include", "only")
MATCH_MODES = ("exact", "and", "or", "fuzzy")
LIST_FIELDS = ("kind", "status", "priority", "type", "project", "topic", "tag", "due")
DATE_FIELDS = tuple(f"{name}_{edge}" for name in ("created", "updated", "completed", "due")
                    for edge in ("since", "before"))
DATE_HELP = "YYYY-MM-DD or an age like 2w, 3m, 1y"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Accepted spellings, applied only where input is parsed (CLI arguments, URL
# parameters, plan JSON), never when matching. Each maps to the canonical
# fields it sets; None keeps the value given with the alias.
ALIASES = {
    "since": {"updated_since": None},
    "due_after": {"due_since": None},
    "task_type": {"type": None},
    "all": {"archived": "include", "hidden": "include"},
    "include_archived": {"archived": "include"},
    "include_hidden": {"hidden": "include"},
    "due=next": {"due": ("overdue", "week")},
    "due=next7": {"due": ("week",)},
    # Dashboard addresses saved before the shared query.
    "q": {"text": None},
    "review": {"needs_review": None},
    "hidden=1": {"hidden": "include"},
    "hidden=true": {"hidden": "include"},
}
# The old dashboard `updated` option: an age for "since", or `older-AGE` for "before".
LEGACY_UPDATED = "updated"


class QueryError(ValueError):
    """A query that cannot be used; the message names the flag and is meant for people."""


@dataclass(frozen=True)
class Query:
    """Which cards to select. AND across fields, OR within a list; empty means no constraint."""

    kind: tuple[str, ...] = ()
    status: tuple[str, ...] = ()
    priority: tuple[str, ...] = ()
    type: tuple[str, ...] = ()
    project: tuple[str, ...] = ()
    topic: tuple[str, ...] = ()
    tag: tuple[str, ...] = ()
    needs_review: bool = False
    triage: bool = False
    created_since: str | None = None
    created_before: str | None = None
    updated_since: str | None = None
    updated_before: str | None = None
    completed_since: str | None = None
    completed_before: str | None = None
    due_since: str | None = None
    due_before: str | None = None
    due: tuple[str, ...] = ()
    archived: str = "exclude"
    hidden: str = "exclude"
    text: str = ""
    match: str = "exact"

    def __post_init__(self) -> None:
        for name in LIST_FIELDS:
            object.__setattr__(self, name, tuple(getattr(self, name)))


DEFAULT = Query()


# ---- Parsing -------------------------------------------------------------

def _option(field: str) -> str:
    return "--" + field.replace("_", "-")


def _values(value: object) -> tuple[str, ...]:
    items = value if isinstance(value, (list, tuple)) else [value]
    return tuple(dict.fromkeys(text for text in (str(item).strip() for item in items if item is not None) if text))


def _last(value: object) -> str:
    items = value if isinstance(value, (list, tuple)) else [value]
    return next((text for text in (str(item).strip() for item in reversed(items) if item is not None) if text), "")


def _flag(value: object, field: str) -> bool:
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0", ""):
        return False
    raise QueryError(f"{_option(field)} must be true or false")


def parse_day(value: object, field: str, today: date) -> str | None:
    text = _last(value)
    if not text:
        return None
    if DATE_RE.fullmatch(text):
        try:
            date.fromisoformat(text)
            return text
        except ValueError:
            pass
    age = card_format.relative_date(text, today)
    if age is None:
        raise QueryError(f"{_option(field)} must be a date in YYYY-MM-DD format or an age like 2w, 3m, 1y")
    return age.isoformat()


def _expand_aliases(values: Mapping) -> dict:
    """Rename or expand alias keys and alias due values to canonical fields."""
    result: dict = {}
    for key, value in values.items():
        if key == LEGACY_UPDATED:
            text = _last(value)
            if text and text != "all":
                field = "updated_before" if text.startswith("older-") else "updated_since"
                result[field] = text.removeprefix("older-")
            continue
        if key != "due" and f"{key}={_last(value)}" in ALIASES:
            result.update(ALIASES[f"{key}={_last(value)}"])
            continue
        if key in ALIASES:
            if key in ("all", "include_archived", "include_hidden") and not _flag(value, key):
                continue
            if key == "review" and not _flag(value, key):
                continue
            for field, fixed in ALIASES[key].items():
                result[field] = value if fixed is None else fixed
        else:
            result[key] = value
    if "due" in result:
        due: list[str] = []
        for item in _values(result["due"]):
            due.extend(ALIASES.get(f"due={item}", {}).get("due", (item,)))
        result["due"] = due
    return result


def uses_aliases(values: Mapping) -> bool:
    """Whether input spells any field another way, so a client can adopt the canonical query."""
    return _expand_aliases(values) != {key: value for key, value in values.items()}


def query_from_mapping(values: Mapping, *, today: date | None = None) -> Query:
    """Build a query from URL parameters or plan JSON. Ages are resolved to dates here."""
    today = today or date.today()
    values = _expand_aliases(values)
    known = {field.name for field in fields(Query)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise QueryError(f"unknown filter: {', '.join(unknown)}")
    parsed: dict = {}
    for name in LIST_FIELDS:
        if name in values:
            items = _values(values[name])
            parsed[name] = tuple(card_format.normalize_topic(item) for item in items) if name == "topic" else items
    for name in ("needs_review", "triage"):
        if name in values:
            parsed[name] = _flag(values[name], name)
    for name in DATE_FIELDS:
        if name in values:
            parsed[name] = parse_day(values[name], name, today)
    for name in ("archived", "hidden", "match"):
        if name in values and _last(values[name]):
            parsed[name] = _last(values[name]).lower()
    if "text" in values:
        parsed["text"] = _last(values["text"])
    # Only archived cards means all of them, hidden or not, unless hidden is given (and vice versa).
    for field, other in (("archived", "hidden"), ("hidden", "archived")):
        if parsed.get(field) == "only" and other not in parsed:
            parsed[other] = "include"
    return Query(**parsed)


def query_to_mapping(query: Query) -> dict:
    """The canonical JSON-safe form: only fields that differ from the default."""
    result: dict = {}
    for field in fields(Query):
        value = getattr(query, field.name)
        visibility = field.name in ("archived", "hidden") and "only" in (query.archived, query.hidden)
        if value == getattr(DEFAULT, field.name) and not visibility:
            continue
        result[field.name] = list(value) if isinstance(value, tuple) else value
    return result


class VisibilityAction(argparse.Action):
    """--archived / --hidden: bare means only; a mode word selects that mode.

    Any other word after a bare flag is not a mode, so it goes back to the search words.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if values in VISIBILITY or values is None:
            setattr(namespace, self.dest, values or self.const)
            return
        if not getattr(namespace, "query_text", False):
            parser.error(f"argument {option_string}: invalid choice: {values!r} "
                         f"(choose from {', '.join(VISIBILITY)})")
        setattr(namespace, self.dest, self.const)
        namespace.words_after = [*getattr(namespace, "words_after", []), values]


def add_query_arguments(parser: argparse.ArgumentParser, *, defaults: dict | None = None,
                        text: bool = False) -> None:
    """Register every query field as a flag. `defaults` changes visibility defaults for a command."""
    group = parser.add_argument_group("selecting cards")

    def names(field: str) -> list[str]:
        return [_option(field)] + [_option(alias) for alias, target in ALIASES.items()
                                   if target.get(field, 0) is None]

    def many(field: str, metavar: str, help: str, **extra: object) -> None:
        group.add_argument(*names(field), dest=field, action="append", metavar=metavar, help=help, **extra)

    many("kind", "KIND", "select tasks or notes (task, note); repeat to select both", choices=KINDS)
    many("status", "STATUS", "select tasks with this status; repeat for several (notes never match)")
    many("priority", "PRIORITY", "select tasks with this priority; repeat for several")
    many("type", "TYPE", "select tasks of this type, case-insensitive")
    many("project", "PROJECT", "exact case-insensitive project; repeat for several")
    many("topic", "PATH", "this topic and everything under it, whole segments, case-insensitive")
    many("tag", "TAG", "exact case-insensitive tag; repeat for several")
    group.add_argument("--needs-review", action="store_true", help="select cards marked for review")
    group.add_argument("--triage", action="store_true",
                       help="select cards needing triage (inbox tasks and cards needing review)")
    for name in ("created", "updated", "completed", "due"):
        for edge, meaning in (("since", "on or after"), ("before", "before")):
            field = f"{name}_{edge}"
            group.add_argument(*names(field), dest=field, metavar="DATE",
                               help=f"cards with {name} {meaning} this day: {DATE_HELP}")
    many("due", "MODE", "overdue (before now), today, week (today through 7 days ahead), any "
                        "(has a due date), or none; repeat for several; next means overdue plus week")
    for field, noun in (("archived", "archived"), ("hidden", "hidden")):
        group.add_argument(_option(field), dest=field, nargs="?", const="only", metavar="MODE",
                           action=VisibilityAction,
                           help=f"{noun} cards: exclude (default), include, or only; bare means only")
    group.add_argument("--all", action="store_true", help="include archived and hidden cards")
    group.add_argument("--include-archived", action="store_true", help="include archived cards")
    group.add_argument("--include-hidden", action="store_true", help="include hidden cards")
    if text:
        parser.add_argument("words", nargs="*", metavar="WORDS", help="select cards whose title, ID, reference, tags, or body match")
        group.add_argument("--match", dest="match", choices=MATCH_MODES, metavar="MODE",
                           help="how WORDS match: exact (default, one phrase), and, or, fuzzy")
        group.add_argument("--and", dest="match", action="store_const", const="and", help="every word must match")
        group.add_argument("--or", dest="match", action="store_const", const="or", help="any word may match")
        group.add_argument("--fuzzy", dest="match", action="store_const", const="fuzzy",
                           help="allow close word matches")
    parser.set_defaults(query_args=True, query_defaults=dict(defaults or {}), query_text=text)


def _env_scope(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    applied = {}
    for field, variable in (("project", "MT_PROJECT"), ("topic", "MT_TOPIC")):
        value = env.get(variable, "").strip()
        if value and not getattr(args, field, None):
            applied[field] = (value, variable)
    return applied


def env_scope(args: argparse.Namespace, env: Mapping[str, str] = os.environ) -> list[str]:
    """What MT_PROJECT / MT_TOPIC add to this command's query, for the caller's "Scope:" line."""
    if not getattr(args, "query_args", False):
        return []
    return [f"{field} {value} ({variable})" for field, (value, variable) in _env_scope(args, env).items()]


def query_from_args(args: argparse.Namespace, *, env: Mapping[str, str] = os.environ,
                    today: date | None = None) -> Query:
    """Build a query from parsed arguments; MT_PROJECT / MT_TOPIC fill in a missing project or topic."""
    raw: dict = dict(args.query_defaults)
    given: dict = {}
    for alias in ALIASES:
        if "=" not in alias and getattr(args, alias, None):
            given[alias] = getattr(args, alias)
    raw.update(_expand_aliases(given))
    for field in fields(Query):
        value = getattr(args, field.name, None)
        if value is not None and value is not False and value != []:
            raw[field.name] = value
    for field, (value, _variable) in _env_scope(args, env).items():
        raw[field] = value
    if args.query_text:
        raw["text"] = " ".join([*args.words, *getattr(args, "words_after", [])])
    return query_from_mapping(raw, today=today)


# ---- Validation ----------------------------------------------------------

def validate(query: Query, *, statuses: list[str], priorities: list[str]) -> None:
    """Raise QueryError when the query asks for something that cannot exist."""
    for field, allowed in (("status", statuses), ("priority", priorities)):
        unknown = [value for value in getattr(query, field) if value not in allowed]
        if unknown:
            raise QueryError(f"{_option(field)}: unknown {field} {', '.join(unknown)}; "
                             f"choose from {', '.join(allowed)}")
    if any(value not in KINDS for value in query.kind):
        raise QueryError(f"{_option('kind')} must be task or note")
    if query.kind == ("note",):
        for field in ("status", "priority", "type"):
            if getattr(query, field):
                raise QueryError(f"{_option(field)} selects tasks, so it cannot be combined with --kind note")
    for name in ("created", "updated", "completed", "due"):
        since, before = getattr(query, f"{name}_since"), getattr(query, f"{name}_before")
        if since and before and since >= before:
            raise QueryError(f"{_option(name + '_since')} must be earlier than {_option(name + '_before')}")
    if any(mode not in DUE_MODES for mode in query.due):
        raise QueryError(f"{_option('due')} must be one of {', '.join(DUE_MODES)}")
    for field in ("archived", "hidden"):
        if getattr(query, field) not in VISIBILITY:
            raise QueryError(f"{_option(field)} must be one of {', '.join(VISIBILITY)}")
    if query.match not in MATCH_MODES:
        raise QueryError(f"{_option('match')} must be one of {', '.join(MATCH_MODES)}")


# ---- Matching ------------------------------------------------------------

def due_moment(value: object, now: datetime) -> datetime | None:
    """A due value as a datetime in `now`'s time zone; a date alone means the end of that day."""
    try:
        text = str(value)
        result = (datetime.combine(datetime.fromisoformat(text).date(), time(23, 59, 59))
                  if len(text) == 10 else datetime.fromisoformat(text.replace("Z", "+00:00")))
        if now.tzinfo is None:
            # Local rules at the due date, including daylight saving changes.
            return result if result.tzinfo is None else result.astimezone().replace(tzinfo=None)
        return result.replace(tzinfo=now.tzinfo) if result.tzinfo is None else result.astimezone(now.tzinfo)
    except (TypeError, ValueError):
        return None


def due_matches(value: object, mode: str, now: datetime) -> bool:
    """Whether a due value fits one due mode (overdue, today, week, any, none) at `now`."""
    if mode == "none":
        return not value
    if mode == "any":
        return bool(value)
    moment = due_moment(value, now) if value else None
    if moment is None:
        return False
    days = (moment.date() - now.date()).days
    if mode == "overdue":
        return moment < now
    if mode == "today":
        return days == 0
    return 0 <= days <= 7  # week


def _day_of(card: object, field: str, now: datetime) -> str | None:
    value = card.frontmatter.get(field)
    if field == "due":
        moment = due_moment(value, now) if value else None
        return moment.date().isoformat() if moment else None
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _text_regions(card: object) -> tuple[str, str, str]:
    """Title; ID and tags; body, next action and comments."""
    comments = card.frontmatter.get("comments", []) or []
    body = "\n".join([card.body, str(card.next_action or ""), *[
        str(comment.get("text") or "") for comment in comments if isinstance(comment, dict)]])
    names = " ".join([str(card.frontmatter.get("id") or ""),
                      *[str(tag) for tag in card.frontmatter.get("tags") or []]])
    return card.title.casefold(), names.casefold(), body.casefold()


def text_score(card: object, text: str, mode: str) -> float | None:
    """Relevance of a card's title, ID, tags, and body to `text` (None for no match).

    Text starting with `@` is a short reference prefix: it matches the cards whose reference starts with it.
    """
    stripped = text.strip()
    if stripped.startswith("@"):
        return 1.0 if getattr(card, "short_ref", "").startswith(stripped[1:].casefold()) else None
    title, card_id, body = _text_regions(card)
    regions = ((title, 3), (card_id, 2), (body, 1))
    if mode == "exact":
        needle = text.strip().casefold()
        weights = [weight for region, weight in regions if needle in region]
        return float(max(weights)) if weights else None
    if mode in ("and", "or"):
        terms = list(dict.fromkeys(text.casefold().split()))
        if not terms:
            return None
        weights = [max((weight for region, weight in regions if term in region), default=0) for term in terms]
        if mode == "and" and any(weight == 0 for weight in weights):
            return None
        if mode == "or" and all(weight == 0 for weight in weights):
            return None
        return float(sum(weights))
    terms = re.findall(r"[\w-]+", text.casefold())
    candidates = sorted(set(re.findall(r"[\w-]+", f"{card.title}\n{card_id}\n{body}".casefold())))
    if not terms or not candidates:
        return None
    scores = [max(difflib.SequenceMatcher(None, term, candidate).ratio() for candidate in candidates)
              for term in terms]
    return sum(scores) / len(scores) if min(scores) >= 0.75 else None


def _visible(flag: bool, mode: str) -> bool:
    return flag if mode == "only" else (mode == "include" or not flag)


def matches(card: object, query: Query, *, now: datetime) -> bool:
    props = card.frontmatter
    is_task = card.kind == "task"
    if query.kind and card.kind not in query.kind:
        return False
    if query.status and not (is_task and props.get("status") in query.status):
        return False
    if query.priority and not (is_task and props.get("priority") in query.priority):
        return False
    if query.type and not (is_task and str(props.get("type") or "").casefold()
                           in {value.casefold() for value in query.type}):
        return False
    if query.project and str(props.get("project") or "").casefold() not in {
            value.casefold() for value in query.project}:
        return False
    if query.topic and not any(card_format.topic_within(props.get("topic"), path) for path in query.topic):
        return False
    if query.tag and not {tag.casefold() for tag in query.tag} & {
            str(tag).casefold() for tag in props.get("tags") or []}:
        return False
    reviewed = props.get("needs_review") is True
    if query.needs_review and not reviewed:
        return False
    if query.triage and not (reviewed or (is_task and props.get("status") == "inbox")):
        return False
    if not _visible(props.get("archived") is True, query.archived):
        return False
    if not _visible(props.get("hidden") is True, query.hidden):
        return False
    for name in ("created", "updated", "completed", "due"):
        since, before = getattr(query, f"{name}_since"), getattr(query, f"{name}_before")
        if since or before:
            day = _day_of(card, name, now)
            if day is None or (since and day < since) or (before and day >= before):
                return False
    if query.due and not any(due_matches(props.get("due"), mode, now) for mode in query.due):
        return False
    return not query.text.strip() or text_score(card, query.text, query.match) is not None


def select(cards: list, query: Query, *, now: datetime, done_cutoff: str | None = None) -> list:
    """The cards the query selects, in their given order. Ordering is the caller's job.

    With `done_cutoff` (an ISO date), done tasks completed, or without that
    updated, before it are left out; undated done tasks stay.
    """
    selected = [card for card in cards if matches(card, query, now=now)]
    if done_cutoff:
        selected = [card for card in selected if not (
            card.kind == "task" and card.frontmatter.get("status") == "done"
            and "" < str(card.frontmatter.get("completed") or card.frontmatter.get("updated") or "")[:10] < done_cutoff)]
    return selected


def default_done_cutoff(query: Query, policy_window: str, today: date) -> str | None:
    """The date before which done tasks count as old, or None when the query asks for old work."""
    if (query.archived != "exclude" or query.status or query.completed_since or query.completed_before):
        return None
    cutoff = card_format.relative_date(policy_window, today)
    return cutoff.isoformat() if cutoff else None


def narrow(query: Query | None, field: str, allowed: list[str] | tuple[str, ...]) -> Query | None:
    """The query limited to the `allowed` values of a list field, or None when nothing can match."""
    if query is None:
        return None
    wanted = getattr(query, field)
    values = tuple(value for value in wanted if value in allowed) if wanted else tuple(allowed)
    return replace(query, **{field: values}) if values else None
