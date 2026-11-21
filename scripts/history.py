"""Local SQLite audit and revision history store (.mt/history.sqlite)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import difflib
import hashlib
from pathlib import Path
import sqlite3
import threading
import zlib

import card_format
import store
from workspace import Workspace


SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    source      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'committed'
);

CREATE TABLE IF NOT EXISTS revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,
    card_id       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    change_type   TEXT NOT NULL,
    before_sha256 TEXT,
    after_sha256  TEXT,
    before_blob   BLOB,
    after_blob    BLOB
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_revisions_event ON revisions(event_id);
CREATE INDEX IF NOT EXISTS idx_revisions_path ON revisions(path);
CREATE INDEX IF NOT EXISTS idx_revisions_card_id ON revisions(card_id);
CREATE INDEX IF NOT EXISTS idx_revisions_change_type ON revisions(change_type);
"""

_CONTEXT_STATE = threading.local()


@dataclass
class HistoryContext:
    """Thread-local metadata for the active workspace write operation."""

    action: str
    actor: str
    source: str = "cli"
    note: str = ""
    require: bool = False
    warning: str = ""


def current_context() -> HistoryContext | None:
    """Return the active thread-local HistoryContext, if any."""
    return getattr(_CONTEXT_STATE, "current", None)


@contextmanager
def capture_context(
    *,
    action: str,
    actor: str,
    source: str = "cli",
    note: str = "",
    require: bool = False,
) -> Iterator[HistoryContext]:
    """Bind a HistoryContext for the current thread and restore any outer context on exit."""
    previous = current_context()
    ctx = HistoryContext(action=action, actor=actor, source=source, note=note, require=require)
    _CONTEXT_STATE.current = ctx
    try:
        yield ctx
    finally:
        _CONTEXT_STATE.current = previous


def is_enabled(ws: Workspace) -> bool:
    """Return True unless rules.local_history is explicitly false."""
    return store.policy(ws).get("rules", {}).get("local_history", True) is not False


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _extract_card_meta(raw_bytes: bytes | None) -> tuple[str, str]:
    """Best-effort extraction of (card_id, title) from card Markdown bytes."""
    if not raw_bytes:
        return "", ""
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
        properties, _body = card_format.parse_document(text)
    except Exception:
        return "", ""
    card_id = str(properties.get("id") or "").strip()
    title = str(properties.get("title") or "").strip()
    return card_id, title


def _sha256_or_none(data: bytes | None) -> str | None:
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def _compress_or_none(data: bytes | None) -> sqlite3.Binary | None:
    if data is None:
        return None
    return sqlite3.Binary(zlib.compress(data, level=6))


def _build_revision_rows(
    original: dict[str, bytes | None],
    changes: dict[str, bytes],
    deletions: set[str],
) -> list[tuple]:
    rows: list[tuple] = []
    for path in sorted(set(original) | set(changes) | set(deletions)):
        before = original.get(path)
        after = None if path in deletions and path not in changes else changes.get(path)
        if before == after:
            continue
        if before is None:
            change_type = "create"
        elif after is None:
            change_type = "delete"
        else:
            change_type = "update"
        card_id, title = _extract_card_meta(after if after is not None else before)
        rows.append((
            path,
            card_id,
            title,
            change_type,
            _sha256_or_none(before),
            _sha256_or_none(after),
            _compress_or_none(before),
            _compress_or_none(after),
        ))
    return rows


def _path_short_ref(path_str: str) -> str:
    return hashlib.sha256(Path(path_str).stem.encode()).hexdigest()[:8]


def _open_db(ws: Workspace) -> sqlite3.Connection:
    db_path = ws.history_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.create_function("short_ref", 1, _path_short_ref)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        version_row = conn.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0]) if version_row else 0
        if version == 0:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _SCHEMA_SQL.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        elif version != SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"unsupported local history schema version {version} (expected {SCHEMA_VERSION})"
            )
        else:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "status" not in cols:
                conn.execute("ALTER TABLE events ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'")
    except Exception:
        conn.close()
        raise
    return conn


def _insert_event_and_revisions(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    source: str,
    note: str,
    rows: list[tuple],
    status: str = "committed",
) -> int:
    cursor = conn.execute(
        "INSERT INTO events (ts, actor, action, source, note, status) VALUES (?, ?, ?, ?, ?, ?)",
        (_now_iso(), actor, action, source, note, status),
    )
    event_id = int(cursor.lastrowid)
    if rows:
        conn.executemany(
            """
            INSERT INTO revisions (
                event_id, path, card_id, title, change_type,
                before_sha256, after_sha256, before_blob, after_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(event_id, *row) for row in rows],
        )
    return event_id


def prepare_transaction(
    ws: Workspace,
    ctx: HistoryContext,
    original: dict[str, bytes | None],
    changes: dict[str, bytes],
    deletions: set[str],
) -> int | None:
    """Durably commit the recovery snapshot in SQLite before touching card files.

    Used when ``ctx.require`` is True so any SQLite open, insert, or commit failure
    aborts before any card file is deleted or overwritten. Returns the committed event id.
    """
    if not is_enabled(ws):
        return None
    rows = _build_revision_rows(original, changes, deletions)
    if not rows:
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_db(ws)
        conn.execute("BEGIN IMMEDIATE")
        event_id = _insert_event_and_revisions(
            conn,
            actor=ctx.actor,
            action=ctx.action,
            source=ctx.source,
            note=ctx.note,
            rows=rows,
            status="prepared",
        )
        conn.execute("COMMIT")
        return event_id
    except (sqlite3.Error, OSError) as error:
        if conn is not None and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise store.HistoryRequiredError(f"could not save local history snapshot: {error}") from error
    finally:
        if conn is not None:
            conn.close()


def finalize_event(ws: Workspace, event_id: int) -> str:
    """Mark a pre-committed 'prepared' event as 'committed' after card files are written."""
    try:
        conn = _open_db(ws)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE events SET status = 'committed' WHERE id = ?", (int(event_id),))
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as error:
        return f"Local history finalization failed: {error}"
    return ""


def delete_event(ws: Workspace, event_id: int) -> None:
    """Best-effort removal of a pre-committed event when the subsequent file write rolls back."""
    try:
        conn = _open_db(ws)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM events WHERE id = ?", (int(event_id),))
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


def record_transaction(
    ws: Workspace,
    ctx: HistoryContext,
    original: dict[str, bytes | None],
    changes: dict[str, bytes],
    deletions: set[str],
) -> int | None:
    """Record a completed filesystem transaction in .mt/history.sqlite."""
    if not is_enabled(ws):
        return None
    rows = _build_revision_rows(original, changes, deletions)
    if not rows:
        return None
    conn = _open_db(ws)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            event_id = _insert_event_and_revisions(
                conn,
                actor=ctx.actor,
                action=ctx.action,
                source=ctx.source,
                note=ctx.note,
                rows=rows,
            )
            conn.execute("COMMIT")
            return event_id
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def record_attachment_event(
    ws: Workspace,
    *,
    actor: str,
    source: str,
    path: str,
    size_bytes: int,
    filename: str,
) -> str:
    """Record attachment upload metadata without storing binary blobs in SQLite."""
    if not is_enabled(ws):
        return ""
    note = f"{filename} ({size_bytes} bytes)" if filename else f"{size_bytes} bytes"
    rows = [(path, "", filename, "create", None, None, None, None)]
    try:
        conn = _open_db(ws)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _insert_event_and_revisions(
                    conn,
                    actor=actor,
                    action="upload",
                    source=source,
                    note=note,
                    rows=rows,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as error:
        return f"Local history is unavailable: {error}"
    return ""


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_events(
    ws: Workspace,
    *,
    card_query: str | None = None,
    deleted_only: bool = False,
    actor: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Return recent history events (newest first) with their revision summaries."""
    db_path = ws.history_db_path()
    if not db_path.is_file():
        return []
    clauses: list[str] = []
    params: list[object] = []
    if actor:
        clauses.append("e.actor = ?")
        params.append(actor)
    if deleted_only:
        clauses.append("r.change_type = 'delete'")
    if card_query:
        query = card_query.lstrip("@")
        if query:
            needle = f"%{_like_escape(query)}%"
            clauses.append(
                "(r.path LIKE ? ESCAPE '\\' OR r.card_id LIKE ? ESCAPE '\\' "
                "OR r.title LIKE ? ESCAPE '\\' OR short_ref(r.path) LIKE ? ESCAPE '\\')"
            )
            params.extend([needle, needle, needle, needle])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _open_db(ws)
    try:
        event_rows = conn.execute(
            f"""
            SELECT DISTINCT e.id, e.ts, e.actor, e.action, e.source, e.note, e.status
            FROM events e
            LEFT JOIN revisions r ON r.event_id = e.id
            {where}
            ORDER BY e.id DESC
            LIMIT ?
            """,
            [*params, max(1, int(limit))],
        ).fetchall()
        if not event_rows:
            return []
        event_ids = [int(row["id"]) for row in event_rows]
        placeholders = ",".join("?" for _ in event_ids)
        rev_rows = conn.execute(
            f"""
            SELECT id, event_id, path, card_id, title, change_type, before_sha256, after_sha256
            FROM revisions
            WHERE event_id IN ({placeholders})
            ORDER BY id ASC
            """,
            event_ids,
        ).fetchall()
    finally:
        conn.close()

    by_event: dict[int, list[dict]] = {eid: [] for eid in event_ids}
    for rev in rev_rows:
        by_event[int(rev["event_id"])].append({
            "id": int(rev["id"]),
            "path": rev["path"],
            "card_id": rev["card_id"],
            "title": rev["title"],
            "change_type": rev["change_type"],
            "before_sha256": rev["before_sha256"],
            "after_sha256": rev["after_sha256"],
        })
    return [
        {
            "id": int(ev["id"]),
            "ts": ev["ts"],
            "actor": ev["actor"],
            "action": ev["action"],
            "source": ev["source"],
            "note": ev["note"],
            "status": ev["status"],
            "revisions": by_event[int(ev["id"])],
        }
        for ev in event_rows
    ]


def _decompress_blob(blob: bytes | None) -> bytes | None:
    if blob is None:
        return None
    return zlib.decompress(bytes(blob))


def get_event(
    ws: Workspace,
    event_id: int,
    *,
    path: str | None = None,
    include_diffs: bool = True,
) -> dict | None:
    """Return one event with decompressed revision content and optional unified diffs."""
    db_path = ws.history_db_path()
    if not db_path.is_file():
        return None
    conn = _open_db(ws)
    try:
        ev = conn.execute(
            "SELECT id, ts, actor, action, source, note, status FROM events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
        if ev is None:
            return None
        if path is not None:
            rev_rows = conn.execute(
                """
                SELECT id, path, card_id, title, change_type,
                       before_sha256, after_sha256, before_blob, after_blob
                FROM revisions
                WHERE event_id = ? AND path = ?
                ORDER BY id ASC
                """,
                (int(event_id), path),
            ).fetchall()
        else:
            rev_rows = conn.execute(
                """
                SELECT id, path, card_id, title, change_type,
                       before_sha256, after_sha256, before_blob, after_blob
                FROM revisions
                WHERE event_id = ?
                ORDER BY id ASC
                """,
                (int(event_id),),
            ).fetchall()
    finally:
        conn.close()

    revisions: list[dict] = []
    for rev in rev_rows:
        before_bytes = _decompress_blob(rev["before_blob"])
        after_bytes = _decompress_blob(rev["after_blob"]) if include_diffs else None
        before_text = before_bytes.decode("utf-8", errors="replace") if (include_diffs and before_bytes is not None) else None
        after_text = after_bytes.decode("utf-8", errors="replace") if (include_diffs and after_bytes is not None) else None
        diff_text = ""
        if include_diffs:
            diff_text = "".join(
                difflib.unified_diff(
                    (before_text or "").splitlines(keepends=True),
                    (after_text or "").splitlines(keepends=True),
                    fromfile=f"a/{rev['path']}",
                    tofile=f"b/{rev['path']}",
                )
            )
        revisions.append({
            "id": int(rev["id"]),
            "path": rev["path"],
            "card_id": rev["card_id"],
            "title": rev["title"],
            "change_type": rev["change_type"],
            "before_sha256": rev["before_sha256"],
            "after_sha256": rev["after_sha256"],
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "before_text": before_text,
            "after_text": after_text,
            "diff": diff_text,
        })
    return {
        "id": int(ev["id"]),
        "ts": ev["ts"],
        "actor": ev["actor"],
        "action": ev["action"],
        "source": ev["source"],
        "note": ev["note"],
        "status": ev["status"],
        "revisions": revisions,
    }


def prune_history(
    ws: Workspace,
    *,
    keep_days: int | None = None,
    max_events: int | None = None,
) -> int:
    """Delete old events beyond keep_days or max_events. Returns count of deleted events."""
    db_path = ws.history_db_path()
    if not db_path.is_file():
        return 0
    deleted = 0
    conn = _open_db(ws)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if keep_days is not None:
                cutoff = (datetime.now().astimezone() - timedelta(days=int(keep_days))).isoformat(timespec="seconds")
                cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                deleted += cur.rowcount
            if max_events is not None:
                keep_n = max(0, int(max_events))
                cur = conn.execute(
                    """
                    DELETE FROM events
                    WHERE id NOT IN (
                        SELECT id FROM events ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (keep_n,),
                )
                deleted += cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return deleted
