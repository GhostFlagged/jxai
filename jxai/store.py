"""SQLite storage for targets, runs, and run results."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .config import DB_PATH, normalize_scope

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '[]',
    overrides   TEXT NOT NULL DEFAULT '{}',
    notes       TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT 'cli',
    dir         TEXT NOT NULL,
    started_at  REAL,
    finished_at REAL,
    error       TEXT NOT NULL DEFAULT '',
    stats       TEXT NOT NULL DEFAULT '{}',
    stages      TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_runs_target ON runs(target_id, started_at DESC);
"""

_write_lock = threading.Lock()


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> None:
    with _write_lock:
        conn = connect(path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _row_to_target(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "scope": json.loads(row["scope"] or "[]"),
        "overrides": json.loads(row["overrides"] or "{}"),
        "notes": row["notes"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "target_id": row["target_id"],
        "status": row["status"],
        "trigger": row["trigger"],
        "dir": row["dir"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "stats": json.loads(row["stats"] or "{}"),
        "stages": json.loads(row["stages"] or "[]"),
    }


# --- targets ---------------------------------------------------------------- #

def add_target(
    name: str,
    url: str,
    scope: list[str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    notes: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    name = name.strip()
    url = url.strip()
    if not name:
        raise ValueError("target name is required")
    if not url.startswith(("http://", "https://")):
        raise ValueError("target url must start with http:// or https://")
    now = time.time()
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            try:
                cur = conn.execute(
                    "INSERT INTO targets (name, url, scope, overrides, notes, enabled, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,1,?,?)",
                    (name, url.rstrip("/"), json.dumps(normalize_scope(scope)),
                     json.dumps(dict(overrides or {})), notes, now, now),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"a target named {name!r} already exists") from None
            conn.commit()
            row = conn.execute("SELECT * FROM targets WHERE id=?", (cur.lastrowid,)).fetchone()
        return _row_to_target(row)
    finally:
        if own:
            conn.close()


def update_target(target_id: int, conn: sqlite3.Connection | None = None, **fields: Any) -> dict[str, Any]:
    allowed = {"name", "url", "scope", "overrides", "notes", "enabled"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "scope":
            value = json.dumps(normalize_scope(value))
        elif key == "overrides":
            value = json.dumps(dict(value))
        elif key == "enabled":
            value = int(bool(value))
        elif key == "url":
            value = str(value).rstrip("/")
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return get_target(target_id, conn=conn)
    sets.append("updated_at=?")
    values.extend([time.time(), target_id])
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            conn.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id=?", values)
            conn.commit()
        return get_target(target_id, conn=conn)
    finally:
        if own:
            conn.close()


def get_target(target_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        return _row_to_target(row) if row else None
    finally:
        if own:
            conn.close()


def get_target_by_name(name: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM targets WHERE name=?", (name.strip(),)).fetchone()
        return _row_to_target(row) if row else None
    finally:
        if own:
            conn.close()


def list_targets(enabled_only: bool = False, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        sql = "SELECT * FROM targets"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY name"
        return [_row_to_target(r) for r in conn.execute(sql)]
    finally:
        if own:
            conn.close()


def delete_target(target_id: int, conn: sqlite3.Connection | None = None) -> bool:
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            cur = conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
            conn.commit()
            return cur.rowcount > 0
    finally:
        if own:
            conn.close()


# --- runs ------------------------------------------------------------------- #

def start_run(target_id: int, run_dir: str, trigger: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            cur = conn.execute(
                "INSERT INTO runs (target_id, status, trigger, dir, started_at) VALUES (?,?,?,?,?)",
                (target_id, "running", trigger, run_dir, time.time()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM runs WHERE id=?", (cur.lastrowid,)).fetchone()
        return _row_to_run(row)
    finally:
        if own:
            conn.close()


def finish_run(
    run_id: int,
    status: str,
    stats: Mapping[str, Any] | None = None,
    error: str = "",
    conn: sqlite3.Connection | None = None,
) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, error=?, stats=? WHERE id=?",
                (status, time.time(), error[:4000], json.dumps(dict(stats or {})), run_id),
            )
            conn.commit()
    finally:
        if own:
            conn.close()


def update_stages(run_id: int, stages: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            conn.execute("UPDATE runs SET stages=? WHERE id=?", (json.dumps(stages), run_id))
            conn.commit()
    finally:
        if own:
            conn.close()


def update_run_dir(run_id: int, run_dir: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Set the run directory. Kept separate from start_run because the directory
    name embeds the run id, which only exists after the row is inserted."""
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            conn.execute("UPDATE runs SET dir=? WHERE id=?", (run_dir, run_id))
            conn.commit()
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _row_to_run(row)
    finally:
        if own:
            conn.close()


def get_run(run_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None
    finally:
        if own:
            conn.close()


def list_runs(target_id: int | None = None, limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        if target_id is None:
            sql, params = "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        else:
            sql, params = ("SELECT * FROM runs WHERE target_id=? ORDER BY id DESC LIMIT ?", (target_id, limit))
        return [_row_to_run(r) for r in conn.execute(sql, params)]
    finally:
        if own:
            conn.close()


def recover_interrupted_runs(conn: sqlite3.Connection | None = None) -> int:
    """Mark runs left 'running' by a killed process as failed. Call at startup."""
    own = conn is None
    conn = conn or connect()
    try:
        with _write_lock:
            cur = conn.execute(
                "UPDATE runs SET status='interrupted', finished_at=?, error=? WHERE status='running'",
                (time.time(), "process exited before the run finished"),
            )
            conn.commit()
            return cur.rowcount
    finally:
        if own:
            conn.close()