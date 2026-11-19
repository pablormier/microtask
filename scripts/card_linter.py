"""Advisory card and workspace lint checks with configurable severity."""
from __future__ import annotations

from pathlib import Path

import card_format
import card_references

VALID_LEVELS = ("off", "warn")
DEFAULT_LEVELS = {
    "backticked_ref": "warn",
    "topic_case": "warn",
}


def normalize_lint_policy(raw: object) -> dict[str, str]:
    """Validate and normalize the optional ``lint`` settings mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("settings lint must be a mapping of rule names to 'off' or 'warn'")
    normalized: dict[str, str] = {}
    for rule, level in raw.items():
        if rule not in DEFAULT_LEVELS:
            raise ValueError(f"unknown settings lint rule '{rule}'")
        if level is False:
            # YAML 1.1 parses unquoted 'off' as boolean False.
            level = "off"
        if not isinstance(level, str) or level not in VALID_LEVELS:
            raise ValueError(f"settings lint.{rule} must be 'off' or 'warn'")
        normalized[str(rule)] = level
    return normalized


def rule_level(policy: dict, rule: str) -> str:
    """Return configured severity ('off' or 'warn') for a lint rule."""
    configured = policy.get("lint")
    if isinstance(configured, dict) and rule in configured:
        return str(configured[rule])
    return DEFAULT_LEVELS[rule]


def backticked_ref_messages(rel: Path | str, fm: dict[str, object], body: str) -> list[str]:
    """Report inline-backticked `` `@ref` `` mentions that will not render as links."""
    texts: list[str] = [body]
    for field in ("next_action", "processing_instructions"):
        value = fm.get(field)
        if isinstance(value, str) and value:
            texts.append(value)
    comments = fm.get("comments")
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict) and isinstance(comment.get("text"), str):
                texts.append(comment["text"])

    seen: set[str] = set()
    refs: list[str] = []
    for text in texts:
        for ref in card_references.backticked_references(text):
            if ref.casefold() not in seen:
                seen.add(ref.casefold())
                refs.append(ref)
    if not refs:
        return []
    joined = ", ".join(refs)
    return [f"{rel}: backticked card reference {joined} will not render as a link; remove the backticks"]


def _topic_prefix_keys(topics: list[str]) -> set[tuple[str, ...]]:
    separator = card_format.TOPIC_SEPARATOR
    keys: set[tuple[str, ...]] = set()
    for topic in topics:
        parts = topic.split(separator)
        for depth in range(1, len(parts) + 1):
            keys.add(card_format.topic_key(separator.join(parts[:depth])))
    return keys


def topic_case_messages(
    topics: list[str],
    *,
    focus_topics: list[str] | None = None,
) -> list[str]:
    """Warn when one topic path is spelled with different casing across cards."""
    separator = card_format.TOPIC_SEPARATOR
    focus_keys = _topic_prefix_keys(focus_topics) if focus_topics is not None else None
    spellings: dict[tuple[str, ...], dict[str, int]] = {}
    for topic in topics:
        parts = topic.split(separator)
        for depth in range(1, len(parts) + 1):
            prefix = separator.join(parts[:depth])
            key = card_format.topic_key(prefix)
            if focus_keys is not None and key not in focus_keys:
                continue
            counts = spellings.setdefault(key, {})
            counts[prefix] = counts.get(prefix, 0) + 1
    return [
        "topic '" + "' and '".join(sorted(counts)) + "' differ only by letter case; they are treated as one topic, "
        "so pick one spelling (mt topic rename OLD NEW)"
        for counts in spellings.values()
        if len(counts) > 1
    ]


def lint_item(rel: Path | str, fm: dict[str, object], body: str, policy: dict) -> list[str]:
    """Run per-card lint rules and return warning messages."""
    if rule_level(policy, "backticked_ref") == "off":
        return []
    return backticked_ref_messages(rel, fm, body)


def lint_workspace(
    topics: list[str],
    policy: dict,
    *,
    focus_topics: list[str] | None = None,
) -> list[str]:
    """Run workspace-wide lint rules and return warning messages."""
    if rule_level(policy, "topic_case") == "off":
        return []
    return topic_case_messages(topics, focus_topics=focus_topics)
