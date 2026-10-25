"""Database SQLite dell'add-on: anagrafica, tessere, eventi, tessere sconosciute, audit."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

DB_PATH = Path("/data/axis_access_manager.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    axis_user_token TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    number TEXT NOT NULL UNIQUE COLLATE NOCASE,
    enabled INTEGER NOT NULL DEFAULT 1,
    axis_credential_token TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    card TEXT NOT NULL DEFAULT '',
    person_id INTEGER,
    person_name TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS unknown_cards (
    number TEXT PRIMARY KEY COLLATE NOCASE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class Database:
    """Accesso al database da più thread: una connessione protetta da un lock."""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.lastrowid

    # Persone e tessere

    def list_persons(self) -> list[dict]:
        persons = self._query("SELECT * FROM persons ORDER BY name COLLATE NOCASE")
        cards = self._query("SELECT * FROM cards ORDER BY id")
        by_person: dict[int, list[dict]] = {}
        for card in cards:
            card["enabled"] = bool(card["enabled"])
            by_person.setdefault(card["person_id"], []).append(card)
        for person in persons:
            person["cards"] = by_person.get(person["id"], [])
        return persons

    def get_person(self, person_id: int) -> dict | None:
        rows = self._query("SELECT * FROM persons WHERE id = ?", (person_id,))
        if not rows:
            return None
        person = rows[0]
        person["cards"] = self.cards_of(person_id)
        return person

    def person_by_user_token(self, token: str) -> dict | None:
        rows = self._query("SELECT * FROM persons WHERE axis_user_token = ?", (token,))
        return rows[0] if rows else None

    def add_person(self, name: str, axis_user_token: str | None) -> int:
        ts = now_iso()
        return self._execute(
            "INSERT INTO persons (name, axis_user_token, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, axis_user_token, ts, ts),
        )

    def rename_person(self, person_id: int, name: str) -> None:
        self._execute("UPDATE persons SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), person_id))

    def delete_person(self, person_id: int) -> None:
        self._execute("DELETE FROM persons WHERE id = ?", (person_id,))

    def cards_of(self, person_id: int) -> list[dict]:
        cards = self._query("SELECT * FROM cards WHERE person_id = ? ORDER BY id", (person_id,))
        for card in cards:
            card["enabled"] = bool(card["enabled"])
        return cards

    def get_card(self, card_id: int) -> dict | None:
        rows = self._query("SELECT * FROM cards WHERE id = ?", (card_id,))
        if not rows:
            return None
        rows[0]["enabled"] = bool(rows[0]["enabled"])
        return rows[0]

    def card_by_number(self, number: str) -> dict | None:
        rows = self._query(
            "SELECT cards.*, persons.name AS person_name FROM cards JOIN persons ON persons.id = cards.person_id "
            "WHERE cards.number = ?",
            (number,),
        )
        return rows[0] if rows else None

    def card_by_token(self, token: str) -> dict | None:
        rows = self._query(
            "SELECT cards.*, persons.name AS person_name FROM cards JOIN persons ON persons.id = cards.person_id "
            "WHERE cards.axis_credential_token = ?",
            (token,),
        )
        return rows[0] if rows else None

    def add_card(self, person_id: int, number: str, axis_credential_token: str, enabled: bool = True) -> int:
        card_id = self._execute(
            "INSERT INTO cards (person_id, number, enabled, axis_credential_token, created_at) VALUES (?, ?, ?, ?, ?)",
            (person_id, number, int(enabled), axis_credential_token, now_iso()),
        )
        self.forget_unknown_card(number)
        return card_id

    def update_card(self, card_id: int, *, person_id: int, number: str, enabled: bool) -> None:
        self._execute(
            "UPDATE cards SET person_id = ?, number = ?, enabled = ? WHERE id = ?",
            (person_id, number, int(enabled), card_id),
        )

    def set_card_enabled(self, card_id: int, enabled: bool) -> None:
        self._execute("UPDATE cards SET enabled = ? WHERE id = ?", (int(enabled), card_id))

    def delete_card(self, card_id: int) -> None:
        self._execute("DELETE FROM cards WHERE id = ?", (card_id,))

    def all_cards(self) -> list[dict]:
        return self._query("SELECT * FROM cards")

    def all_persons(self) -> list[dict]:
        return self._query("SELECT * FROM persons")

    # Tessere sconosciute

    def note_unknown_card(self, number: str, ts: str) -> None:
        self._execute(
            "INSERT INTO unknown_cards (number, first_seen, last_seen, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(number) DO UPDATE SET last_seen = excluded.last_seen, count = count + 1",
            (number, ts, ts),
        )

    def list_unknown_cards(self) -> list[dict]:
        return self._query("SELECT * FROM unknown_cards ORDER BY last_seen DESC")

    def forget_unknown_card(self, number: str) -> None:
        self._execute("DELETE FROM unknown_cards WHERE number = ?", (number,))

    # Eventi

    def add_event(
        self,
        *,
        ts: str,
        kind: str,
        card: str = "",
        person_id: int | None = None,
        person_name: str = "",
        detail: str = "",
        source: str = "",
        raw: dict | None = None,
    ) -> int:
        return self._execute(
            "INSERT INTO events (ts, kind, card, person_id, person_name, detail, source, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, kind, card, person_id, person_name, detail, source, json.dumps(raw) if raw else ""),
        )

    def list_events(
        self, *, kind: str = "", search: str = "", before_id: int | None = None, limit: int = 100
    ) -> list[dict]:
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if search:
            clauses.append("(person_name LIKE ? OR card LIKE ? OR detail LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        if before_id:
            clauses.append("id < ?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._query(
            f"SELECT id, ts, kind, card, person_id, person_name, detail, source FROM events {where} "
            "ORDER BY id DESC LIMIT ?",
            tuple(params),
        )

    def prune_events(self, days: int) -> None:
        limit = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        self._execute("DELETE FROM events WHERE ts < ?", (limit,))

    # Audit

    def add_audit(self, user: str, action: str, detail: str = "") -> None:
        self._execute(
            "INSERT INTO audit (ts, user, action, detail) VALUES (?, ?, ?, ?)", (now_iso(), user, action, detail)
        )

    def list_audit(self, limit: int = 100) -> list[dict]:
        return self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
