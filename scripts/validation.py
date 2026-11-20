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
    seen_ids: dict[str, Path],
    seen_time_sessions: dict[str, Path],
    seen_capture_ids: dict[str, Path],
    errors: list[str],
    policy: dict[str, object],
    *,
    raw: str | None = None,
    collect: dict[str, list] | None = None,
    warnings: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Validate one card; return its (kind, status), or (None, None) when unreadable."""
    rel = path.relative_to(data_root)
    if raw is None:
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"{rel}: cannot read card: {error}")
            return None, None
    if raw.strip() == "":
        errors.append(f"{rel}: file is empty")
        return None, None
    fm_text, body = card_format.parse_frontmatter(raw)
    if fm_text is None:
        errors.append(f"{rel}: missing YAML frontmatter")
        return None, None
    try:
        fm = card_format.parse_yaml(fm_text)
    except ValueError as error:
        errors.append(f"{rel}: {error}")
        return None, None
    if collect is not None and isinstance(fm.get("topic"), str):
        collect["topics"].append(fm["topic"])
    kind = fm.get("kind")
    if not isinstance(fm.get("title"), str) or not isinstance(kind, str):
        errors.append(f"{rel}: card must have title and kind properties")
        return None, None
    if kind not in ("task", "note"):
        errors.append(f"{rel}: kind must be task or note")
        return None, None
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
    if isinstance(item_id, str) and item_id:
        if item_id in seen_ids:
            errors.append(
                f"{rel}: duplicate id '{item_id}' (also in {seen_ids[item_id].relative_to(data_root)})"
            )
        else:
            seen_ids[item_id] = path
    capture_id = fm.get("capture_id")
    if isinstance(capture_id, str):
        if capture_id in seen_capture_ids:
            errors.append(
                f"{rel}: duplicate capture_id '{capture_id}' "
                f"(also in {seen_capture_ids[capture_id].relative_to(data_root)})"
            )
        else:
            seen_capture_ids[capture_id] = path
    status = fm.get("status") if kind == "task" else None
    if collect is not None and "claimants" in collect:
        collect["claimants"][path] = _doing_claimant(fm) or ""
    return kind, status if isinstance(status, str) else None


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

    The second value is None unless the card is a doing task. For a doing task it is the
    claimant's name, or "" when nobody holds the claim.
    """
    if raw is None:
        return {"id": set(), "capture_id": set(), "time session id": set()}, None
    if fm is None:
        fm_text, _body = card_format.parse_frontmatter(raw)
        fm = card_format.parse_yaml(fm_text) if fm_text is not None else {}
    capture = fm.get("capture_id")
    sessions: set[str] = set()
    if check_time and fm.get("kind") == "task":
        try:
            sessions = {str(entry["session_id"]) for entry in card_format.time_log_entries(raw)}
        except ValueError as error:
            if strict_time:
                raise ValueError(f"{path.relative_to(data_root)}: cannot check time-session uniqueness: {error}") from error
            sessions = set()  # The changed card's validators report malformed time logs.
    return {
        "id": {str(fm["id"])} if fm.get("id") else set(),
        "capture_id": {capture} if isinstance(capture, str) else set(),
        "time session id": sessions,
    }, _doing_claimant(fm)


def _doing_claimant(fm: dict) -> str | None:
    if fm.get("kind") != "task" or fm.get("status") != "doing":
        return None
    claimant = fm.get("claimed_by")
    return claimant if isinstance(claimant, str) else ""


def doing_limit_bucket(policy: dict, claimant: str) -> str:
    """The group a doing task counts in: one for the workspace, or one per claimant.

    With rules.doing_limit_scope per_claimant, tasks nobody has claimed share one group ("").
    """
    scope = policy.get("rules", {}).get("doing_limit_scope", "workspace")
    return claimant if scope == "per_claimant" else ""


def _doing_limit_message(policy: dict, bucket: str, count: int, limit: int) -> str:
    if policy.get("rules", {}).get("doing_limit_scope", "workspace") != "per_claimant":
        return f"doing task limit exceeded: {count} tasks, maximum {limit}"
    owner = f"claimed by {bucket}" if bucket else "with no claimant"
    return f"doing task limit exceeded: {count} tasks {owner}, maximum {limit} per claimant"


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
    seen: dict[str, dict[str, Path]] = {"id": {}, "capture_id": {}, "time session id": {}}
    added: dict[str, set[str]] = {key: set() for key in seen}
    doing_delta: dict[str, int] = {}
    changed_doing: dict[str, int] = {}
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
            before_keys, before_doing = _item_identity(before, path, data_root=root)
        except ValueError:
            before_keys, before_doing = {key: set() for key in seen}, None
        after = decoded_changes.get(relative)
        after_keys, after_doing = _item_identity(after, path, data_root=root)
        for key in added:
            added[key].update(after_keys[key] - before_keys[key])
        if before_doing is not None:
            bucket = doing_limit_bucket(policy, before_doing)
            doing_delta[bucket] = doing_delta.get(bucket, 0) - 1
        if after_doing is not None:
            bucket = doing_limit_bucket(policy, after_doing)
            doing_delta[bucket] = doing_delta.get(bucket, 0) + 1
            changed_doing[bucket] = changed_doing.get(bucket, 0) + 1
        if after is not None:
            _check_item(
                root, path, seen["id"], seen["time session id"],
                seen["capture_id"], errors, policy, raw=after,
                collect={"topics": changed_topics}, warnings=warnings,
            )
    need_topic_scan = bool(changed_topics) and card_linter.rule_level(policy, "topic_case") != "off"
    grown = {bucket for bucket, delta in doing_delta.items() if delta > 0}
    if errors or (not any(added.values()) and not grown and not need_topic_scan):
        return ChangesReport(errors=errors, warnings=[] if errors else warnings)

    # Read only metadata from other cards, except when new time-session IDs
    # require checking their time logs. Do not validate unrelated card bodies.
    other_doing: dict[str, int] = {}
    other_topics: list[str] = []
    for path in sorted((root / "cards").glob("*.md")):
        relative = str(path.relative_to(root))
        if relative in affected:
            continue
        raw = path.read_text(encoding="utf-8")
        fm_text, _body = card_format.parse_frontmatter(raw)
        if fm_text is None:
            raise ValueError(f"cannot check cross-card invariants: {relative} has no frontmatter; run mt validate")
        fm = card_format.parse_yaml(fm_text)
        claimant = _doing_claimant(fm)
        if claimant is not None:
            bucket = doing_limit_bucket(policy, claimant)
            other_doing[bucket] = other_doing.get(bucket, 0) + 1
        if need_topic_scan and isinstance(fm.get("topic"), str):
            other_topics.append(fm["topic"])
        identity, _ = _item_identity(
            raw,
            path,
            data_root=root,
            strict_time=bool(added["time session id"]),
            fm=fm,
            check_time=bool(added["time session id"]),
        )
        for key, values in added.items():
            for value in values & identity[key]:
                errors.append(f"{relative}: duplicate {key} '{value}' introduced by card changes")
    limit = int(str(policy.get("rules", {}).get("max_doing_tasks") or 0))
    if limit > 0:
        for bucket in sorted(grown):
            count = other_doing.get(bucket, 0) + changed_doing.get(bucket, 0)
            if count > limit:
                errors.append(_doing_limit_message(policy, bucket, count, limit))
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
    seen_ids: dict[str, Path] = {}
    seen_time_sessions: dict[str, Path] = {}
    seen_capture_ids: dict[str, Path] = {}
    rules = policy.get("rules", {})
    raw_doing_limit = rules.get("max_doing_tasks") if isinstance(rules, dict) else None
    if not isinstance(raw_doing_limit, str) or not raw_doing_limit.isdigit():
        doing_limit = 0
        errors.append("settings rules.max_doing_tasks must be a positive integer")
    else:
        doing_limit = int(raw_doing_limit)
        if doing_limit < 1:
            errors.append("settings rules.max_doing_tasks must be a positive integer")

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

    collected: dict = {"topics": [], "claimants": {}}
    warnings: list[str] = []
    doing_counts: dict[str, int] = {}
    kind_counts = {"task": 0, "note": 0}
    for path in md_files:
        kind, status = _check_item(
            root, path, seen_ids, seen_time_sessions, seen_capture_ids, errors, policy,
            collect=collected, warnings=warnings,
        )
        if kind is not None:
            kind_counts[kind] += 1
        if kind == "task" and status == "doing":
            bucket = doing_limit_bucket(policy, collected["claimants"].get(path, ""))
            doing_counts[bucket] = doing_counts.get(bucket, 0) + 1

    if doing_limit > 0:
        for bucket in sorted(doing_counts):
            if doing_counts[bucket] > doing_limit:
                errors.append(_doing_limit_message(policy, bucket, doing_counts[bucket], doing_limit))

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
