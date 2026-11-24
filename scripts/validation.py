"""In-process workspace and card-change validation (the trust boundary)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import attachments
import card_format
import card_linter
import drafts as draft_store
import validators


@dataclass(frozen=True)
class AuditReport:
    errors: list[str]
    warnings: list[str]
    task_count: int
    note_count: int
    draft_count: int


@dataclass(frozen=True)
class ChangesReport:
    errors: list[str]
    warnings: list[str]


def _decode_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _check_item(
    data_root: Path,
    path: Path,
    seen_time_sessions: dict[str, Path],
    seen_capture_ids: dict[str, Path],
    errors: list[str],
    policy: dict[str, object],
    *,
    raw: str | None = None,
    collect: dict[str, list] | None = None,
    warnings: list[str] | None = None,
) -> str | None:
    """Validate one card; return its kind, or None when unreadable."""
    rel = path.relative_to(data_root)
    if raw is None:
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"{rel}: cannot read card: {error}")
            return None
    if raw.strip() == "":
        errors.append(f"{rel}: file is empty")
        return None
    fm_text, body = card_format.parse_frontmatter(raw)
    if fm_text is None:
        errors.append(f"{rel}: missing YAML frontmatter")
        return None
    try:
        fm = card_format.parse_yaml(fm_text)
    except ValueError as error:
        errors.append(f"{rel}: {error}")
        return None
    if collect is not None and isinstance(fm.get("topic"), str):
        collect["topics"].append(fm["topic"])
    kind = fm.get("kind")
    if not isinstance(fm.get("title"), str) or not isinstance(kind, str):
        errors.append(f"{rel}: card must have title and kind properties")
        return None
    if kind not in ("task", "note"):
        errors.append(f"{rel}: kind must be task or note")
        return None
    errors.extend(
        validators.run_item_validators(
            validators.CheckContext(
                kind=kind,
                rel=rel,
                fm=fm,
                body=body,
                policy=policy,
            )
        )
    )
    if warnings is not None:
        warnings.extend(card_linter.lint_item(rel, fm, body, policy, attachments_root=data_root / "attachments"))
    if kind == "task":
        try:
            entries = card_format.time_log_entries(raw)
        except ValueError:
            entries = []
        for entry in entries:
            session_id = str(entry["session_id"])
            if session_id in seen_time_sessions:
                errors.append(
                    f"{rel}: duplicate time session id '{session_id}' "
                    f"(also in {seen_time_sessions[session_id].relative_to(data_root)})"
                )
            else:
                seen_time_sessions[session_id] = path
    item_id = fm.get("id")
    if isinstance(item_id, str) and item_id.strip() and item_id != path.stem:
        errors.append(f"{rel}: id '{item_id}' must match filename stem '{path.stem}'")
    capture_id = fm.get("capture_id")
    if isinstance(capture_id, str):
        if capture_id in seen_capture_ids:
            errors.append(
                f"{rel}: duplicate capture_id '{capture_id}' "
                f"(also in {seen_capture_ids[capture_id].relative_to(data_root)})"
            )
        else:
            seen_capture_ids[capture_id] = path
    return kind


def _item_identity(
    raw: str | None,
    path: Path,
    *,
    data_root: Path,
    strict_time: bool = False,
    fm: dict | None = None,
    check_time: bool = True,
) -> tuple[dict[str, set[str]], str | None]:
    """Only the values that can introduce cross-card conflicts.

    Returns (unique_keys, topic).
    """
    if raw is None:
        return {"capture_id": set(), "time session id": set()}, None
    if fm is None:
        fm_text, _body = card_format.parse_frontmatter(raw)
        fm = card_format.parse_yaml(fm_text) if fm_text is not None else {}
    capture = fm.get("capture_id")
    topic = fm.get("topic")
    sessions: set[str] = set()
    if check_time and fm.get("kind") == "task":
        try:
            sessions = {str(entry["session_id"]) for entry in card_format.time_log_entries(raw)}
        except ValueError as error:
            if strict_time:
                raise ValueError(f"{path.relative_to(data_root)}: cannot check time-session uniqueness: {error}") from error
            sessions = set()  # The changed card's validators report malformed time logs.
    return {
        "capture_id": {capture} if isinstance(capture, str) else set(),
        "time session id": sessions,
    }, topic if isinstance(topic, str) else None


def _may_affect_cross_card(
    fm_text: str,
    *,
    topic_roots: set[str],
    capture_targets: set[str],
    session_targets: set[str],
) -> bool:
    """Conservative raw-frontmatter filter; returns False only if no active check can match."""
    if topic_roots and "topic" in fm_text:
        lowered = fm_text.casefold()
        if any(root in lowered for root in topic_roots):
            return True
    if capture_targets and any(cid in fm_text for cid in capture_targets):
        return True
    if session_targets and any(sid in fm_text for sid in session_targets):
        return True
    return False


def check_changes(
    root: Path,
    policy: dict,
    changes: dict[str, bytes | str],
    deletions: set[str] | list[str] | None = None,
    previous: dict[str, bytes | str | None] | None = None,
) -> ChangesReport:
    """Validate changed content and only cross-file invariants it can affect."""
    deletion_set = set(deletions or ())
    decoded_changes = {path: _decode_text(raw) for path, raw in changes.items()}
    affected = set(decoded_changes) | deletion_set
    if any(
        not isinstance(path, str)
        or not re.fullmatch(r"cards/[^/]+\.md", path)
        or ".." in Path(path).parts
        for path in affected
    ):
        raise ValueError("invalid item path in changes")
    decoded_previous: dict[str, str | None] | None = None
    if previous is not None:
        if set(previous) != affected:
            raise ValueError("previous must supply the original text or null for every affected path")
        decoded_previous = {
            path: (_decode_text(raw) if raw is not None else None)
            for path, raw in previous.items()
        }

    errors: list[str] = []
    warnings: list[str] = []
    changed_topics: list[str] = []
    seen: dict[str, dict[str, Path]] = {"capture_id": {}, "time session id": {}}
    added: dict[str, set[str]] = {key: set() for key in seen}
    for relative in sorted(affected):
        path = root / relative
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError(f"{relative}: symbolic links are not supported for card changes")
        before = (
            decoded_previous[relative]
            if decoded_previous is not None
            else (path.read_text(encoding="utf-8") if path.exists() else None)
        )
        try:
            before_keys, before_topic = _item_identity(before, path, data_root=root)
        except ValueError:
            before_keys, before_topic = {key: set() for key in seen}, None
        after = decoded_changes.get(relative)
        after_keys, after_topic = _item_identity(after, path, data_root=root)
        for key in added:
            added[key].update(after_keys[key] - before_keys[key])
        if after is not None:
            _check_item(
                root, path, seen["time session id"],
                seen["capture_id"], errors, policy, raw=after,
                warnings=warnings,
            )
            if after_topic is not None and after_topic != before_topic:
                changed_topics.append(after_topic)
    need_topic_scan = bool(changed_topics) and card_linter.rule_level(policy, "topic_case") != "off"
    if errors or (not any(added.values()) and not need_topic_scan):
        return ChangesReport(errors=errors, warnings=[] if errors else warnings)

    # Read only metadata from other cards, except when new time-session IDs
    # require checking their time logs. Do not validate unrelated card bodies.
    other_topics: list[str] = []
    check_added = any(added.values())
    check_time = bool(added["time session id"])
    topic_roots = {card_format.topic_key(topic)[0] for topic in changed_topics} if need_topic_scan else set()
    capture_targets = added["capture_id"]
    session_targets = added["time session id"]
    for path in sorted((root / "cards").glob("*.md")):
        relative = str(path.relative_to(root))
        if relative in affected:
            continue
        raw = path.read_text(encoding="utf-8")
        fm_text, _body = card_format.parse_frontmatter(raw)
        if fm_text is None:
            raise ValueError(f"cannot check cross-card invariants: {relative} has no frontmatter; run mt validate")
        if not _may_affect_cross_card(
            fm_text,
            topic_roots=topic_roots,
            capture_targets=capture_targets,
            session_targets=session_targets,
        ):
            continue
        fm = card_format.parse_yaml(fm_text)
        if need_topic_scan and isinstance(fm.get("topic"), str):
            other_topics.append(fm["topic"])
        if check_added:
            identity, _ = _item_identity(
                raw,
                path,
                data_root=root,
                strict_time=check_time,
                fm=fm,
                check_time=check_time,
            )
            for key, values in added.items():
                for value in values & identity[key]:
                    errors.append(f"{relative}: duplicate {key} '{value}' introduced by card changes")
    if not errors and need_topic_scan:
        warnings.extend(
            card_linter.lint_workspace(changed_topics + other_topics, policy, focus_topics=changed_topics)
        )
    return ChangesReport(errors=errors, warnings=[] if errors else warnings)


def check_changes_request(root: Path, policy: dict, request: object) -> ChangesReport:
    """Validate a JSON --changes payload from stdin and return errors and warnings."""
    if not isinstance(request, dict) or set(request) - {"changes", "deletions", "previous"}:
        raise ValueError("changes request must contain changes, deletions and optional previous content")
    changes = request.get("changes", {})
    deletions = request.get("deletions", [])
    previous = request.get("previous")
    if not isinstance(changes, dict) or any(not isinstance(raw, str) for raw in changes.values()):
        raise ValueError("changes must map item paths to Markdown text")
    if not isinstance(deletions, list) or any(not isinstance(path, str) for path in deletions):
        raise ValueError("deletions must be an array of item paths")
    if previous is not None and (
        not isinstance(previous, dict)
        or any(raw is not None and not isinstance(raw, str) for raw in previous.values())
    ):
        raise ValueError("previous must supply the original text or null for every affected path")
    return check_changes(root, policy, changes, deletions, previous)


def audit_workspace(root: Path, policy: dict) -> AuditReport:
    """Validate every card and draft in root against settings."""
    cards_root = root / "cards"
    errors: list[str] = []
    seen_time_sessions: dict[str, Path] = {}
    seen_capture_ids: dict[str, Path] = {}

    md_files: list[Path] = []
    if cards_root.is_symlink():
        errors.append("cards/ must not be a symbolic link")
    elif not cards_root.is_dir():
        errors.append("cards/ must exist as a directory")
    else:
        for entry in sorted(cards_root.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                errors.append(f"symbolic link under cards/: {entry.name}")
            elif entry.is_dir():
                errors.append(f"unexpected directory under cards/: {entry.name}/")
            elif not entry.is_file() or entry.suffix != ".md":
                errors.append(f"unexpected file under cards/: {entry.name}")
            else:
                md_files.append(entry)

    collected: dict = {"topics": []}
    warnings: list[str] = []
    kind_counts = {"task": 0, "note": 0}
    for path in md_files:
        kind = _check_item(
            root, path, seen_time_sessions, seen_capture_ids, errors, policy,
            collect=collected, warnings=warnings,
        )
        if kind is not None:
            kind_counts[kind] += 1

    draft_root = root / "drafts"
    draft_files: list[Path] = []
    if draft_root.is_symlink():
        errors.append("drafts/ must not be a symbolic link")
    elif draft_root.exists() and not draft_root.is_dir():
        errors.append("drafts exists but is not a directory")
    elif draft_root.is_dir():
        for entry in sorted(draft_root.iterdir()):
            if entry.is_symlink():
                errors.append(f"symbolic link under drafts/: {entry.name}")
            elif entry.is_dir():
                errors.append(f"unexpected directory under drafts/: {entry.name}/")
            elif not entry.is_file() or entry.suffix != ".md":
                errors.append(f"unexpected file under drafts/: {entry.name}")
            else:
                draft_files.append(entry)

        for path in draft_files:
            try:
                # Draft bodies are intentionally incomplete Markdown. Only
                # validate their canonical path and frontmatter metadata.
                draft_store.read_path(path)
            except (OSError, ValueError, UnicodeError) as error:
                message = str(error)
                prefix = f"{path}: "
                if message.startswith(prefix):
                    message = message[len(prefix):]
                errors.append(f"{path.relative_to(root)}: {message}")

    attachments_root = root / "attachments"
    max_bytes = attachments.max_attachment_bytes(policy)
    if attachments_root.is_symlink():
        errors.append("attachments/ must not be a symbolic link")
    elif attachments_root.exists() and not attachments_root.is_dir():
        errors.append("attachments exists but is not a directory")
    elif attachments_root.is_dir():
        def _walk_attachments(current_dir: Path) -> None:
            for entry in sorted(current_dir.iterdir()):
                if entry.name.startswith("."):
                    continue
                rel_entry = entry.relative_to(root)
                if entry.is_symlink():
                    errors.append(f"symbolic link under attachments/: {rel_entry}")
                elif entry.is_dir():
                    _walk_attachments(entry)
                elif entry.is_file():
                    try:
                        size = entry.stat().st_size
                    except OSError as error:
                        errors.append(f"could not inspect attachment {rel_entry}: {error}")
                        continue
                    if size > max_bytes:
                        limit_mb = policy.get("rules", {}).get("max_attachment_mb", attachments.DEFAULT_MAX_ATTACHMENT_MB)
                        errors.append(
                            f"{rel_entry}: attachment exceeds max_attachment_mb ({size} bytes > {limit_mb} MB)"
                        )
                else:
                    errors.append(f"unexpected entry under attachments/: {rel_entry}")

        _walk_attachments(attachments_root)

    if not errors:
        warnings.extend(card_linter.lint_workspace(collected["topics"], policy))
    return AuditReport(
        errors=errors,
        warnings=[] if errors else warnings,
        task_count=kind_counts["task"],
        note_count=kind_counts["note"],
        draft_count=len(draft_files),
    )


def format_audit_report(report: AuditReport) -> tuple[int, list[str], list[str]]:
    """Return (exit_code, stdout_lines, stderr_lines) for an AuditReport."""
    if report.errors:
        stderr_lines = [f"FAILED: {len(report.errors)} error(s):", *(f"  - {error}" for error in report.errors)]
        return 1, [], stderr_lines
    stderr_lines = [f"WARNING: {warning}" for warning in report.warnings]
    stdout_lines = [
        f"OK: validated {report.task_count} task file(s), {report.note_count} note file(s), "
        f"and {report.draft_count} draft file(s)."
    ]
    return 0, stdout_lines, stderr_lines


def audit(root: Path, policy: dict) -> list[str]:
    """Validate every card and draft in root and return any error messages."""
    return audit_workspace(root, policy).errors
