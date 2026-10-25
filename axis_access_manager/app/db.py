"""Database SQLite dell'add-on: anagrafica, tessere, tipologie, regole, eventi, utenti, audit."""

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
CREATE TABLE IF NOT EXISTS schedule_rules (
    schedule_token TEXT PRIMARY KEY,
    rules TEXT NOT NULL,
    holidays INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS holidays (
    day TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    yearly INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '',
    group_token TEXT NOT NULL DEFAULT '',
    valid_days INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hidden_items (
    kind TEXT NOT NULL,
    token TEXT NOT NULL,
    PRIMARY KEY (kind, token)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_users (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    perms TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS controllers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    verify_ssl INTEGER NOT NULL DEFAULT 0,
    mqtt_topic_prefix TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doors (
    id INTEGER PRIMARY KEY,
    controller_id INTEGER NOT NULL REFERENCES controllers(id) ON DELETE CASCADE,
    door_token TEXT NOT NULL,
    name TEXT NOT NULL,
    snapshot_camera TEXT NOT NULL DEFAULT '',
    snapshot_on_granted INTEGER NOT NULL DEFAULT 0,
    ha_buttons INTEGER NOT NULL DEFAULT 1,
    intercom_name TEXT NOT NULL DEFAULT '',
    intercom_open_door INTEGER NOT NULL DEFAULT 0,
    intercom_relay INTEGER NOT NULL DEFAULT 1,
    intercom_mqtt_prefix TEXT NOT NULL DEFAULT 'hmd',
    UNIQUE (controller_id, door_token)
);
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS areas (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (site_id, name)
);
CREATE TABLE IF NOT EXISTS controller_changes (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    controller_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    token TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""

# Colonne aggiunte dopo la prima versione: (tabella, colonna, definizione)
MIGRATIONS = [
    ("cards", "access_profile", "TEXT NOT NULL DEFAULT ''"),
    ("cards", "valid_from", "TEXT NOT NULL DEFAULT ''"),
    ("cards", "valid_to", "TEXT NOT NULL DEFAULT ''"),
    ("persons", "company", "TEXT NOT NULL DEFAULT ''"),
    ("events", "snapshot", "TEXT NOT NULL DEFAULT ''"),
    ("persons", "category_id", "INTEGER NOT NULL DEFAULT 0"),
    ("events", "door", "TEXT NOT NULL DEFAULT ''"),
    ("doors", "area_id", "INTEGER NOT NULL DEFAULT 0"),
    ("controllers", "area_id", "INTEGER NOT NULL DEFAULT 0"),
    ("controllers", "description", "TEXT NOT NULL DEFAULT ''"),
]


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def local_day_start(days_ago: int = 0, now: datetime | None = None) -> str:
    """Mezzanotte locale di oggi (o di days_ago giorni fa), in UTC come i timestamp del log."""
    local = (now or datetime.now(tz=UTC)).astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


class Database:
    """Accesso al database da più thread: una connessione protetta da un lock."""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:
                columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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

    def update_person_details(self, person_id: int, company: str, notes: str) -> None:
        self._execute(
            "UPDATE persons SET company = ?, notes = ?, updated_at = ? WHERE id = ?",
            (company, notes, now_iso(), person_id),
        )

    def set_person_category(self, person_id: int, category_id: int) -> None:
        self._execute(
            "UPDATE persons SET category_id = ?, updated_at = ? WHERE id = ?", (category_id, now_iso(), person_id)
        )

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

    def add_card(
        self,
        person_id: int,
        number: str,
        axis_credential_token: str,
        enabled: bool = True,
        access: dict | None = None,
    ) -> int:
        access = access or {}
        card_id = self._execute(
            "INSERT INTO cards (person_id, number, enabled, axis_credential_token, created_at, "
            "access_profile, valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                person_id,
                number,
                int(enabled),
                axis_credential_token,
                now_iso(),
                access.get("access_profile", ""),
                access.get("valid_from", ""),
                access.get("valid_to", ""),
            ),
        )
        self.forget_unknown_card(number)
        return card_id

    def update_card(
        self, card_id: int, *, person_id: int, number: str, enabled: bool, access: dict | None = None
    ) -> None:
        self._execute(
            "UPDATE cards SET person_id = ?, number = ?, enabled = ? WHERE id = ?",
            (person_id, number, int(enabled), card_id),
        )
        if access is not None:
            self.set_card_access(card_id, access)

    def set_card_access(self, card_id: int, access: dict) -> None:
        """Gruppo e validità della tessera: {access_profile, valid_from, valid_to}."""
        self._execute(
            "UPDATE cards SET access_profile = ?, valid_from = ?, valid_to = ? WHERE id = ?",
            (access.get("access_profile", ""), access.get("valid_from", ""), access.get("valid_to", ""), card_id),
        )

    def set_card_enabled(self, card_id: int, enabled: bool) -> None:
        self._execute("UPDATE cards SET enabled = ? WHERE id = ?", (int(enabled), card_id))

    def delete_card(self, card_id: int) -> None:
        self._execute("DELETE FROM cards WHERE id = ?", (card_id,))

    def all_cards(self) -> list[dict]:
        return self._query("SELECT * FROM cards")

    def all_persons(self) -> list[dict]:
        return self._query("SELECT * FROM persons")

    def profile_usage(self) -> dict[str, int]:
        """Numero di persone per gruppo."""
        rows = self._query(
            "SELECT access_profile, COUNT(DISTINCT person_id) AS persons FROM cards GROUP BY access_profile"
        )
        return {row["access_profile"]: row["persons"] for row in rows}

    # Fasce orarie create dall'add-on e festività

    def schedule_rules(self) -> dict[str, dict]:
        rows = self._query("SELECT * FROM schedule_rules")
        return {
            row["schedule_token"]: {"rules": json.loads(row["rules"]), "holidays": bool(row["holidays"])}
            for row in rows
        }

    def save_schedule_rules(self, token: str, rules: list[dict], holidays: bool) -> None:
        self._execute(
            "INSERT INTO schedule_rules (schedule_token, rules, holidays) VALUES (?, ?, ?) "
            "ON CONFLICT(schedule_token) DO UPDATE SET rules = excluded.rules, holidays = excluded.holidays",
            (token, json.dumps(rules), int(holidays)),
        )

    def delete_schedule_rules(self, token: str) -> None:
        self._execute("DELETE FROM schedule_rules WHERE schedule_token = ?", (token,))

    def list_holidays(self) -> list[dict]:
        rows = self._query("SELECT * FROM holidays ORDER BY yearly DESC, substr(day, 6), day")
        for row in rows:
            row["yearly"] = bool(row["yearly"])
        return rows

    def add_holiday(self, day: str, name: str, yearly: bool) -> None:
        self._execute(
            "INSERT INTO holidays (day, name, yearly) VALUES (?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET name = excluded.name, yearly = excluded.yearly",
            (day, name, int(yearly)),
        )

    def delete_holiday(self, day: str) -> None:
        self._execute("DELETE FROM holidays WHERE day = ?", (day,))

    # Tipologie di tessera

    def list_categories(self) -> list[dict]:
        rows = self._query(
            "SELECT categories.*, (SELECT COUNT(*) FROM persons WHERE persons.category_id = categories.id) AS persons "
            "FROM categories ORDER BY name COLLATE NOCASE"
        )
        return rows

    def get_category(self, category_id: int) -> dict | None:
        rows = self._query("SELECT * FROM categories WHERE id = ?", (category_id,))
        return rows[0] if rows else None

    def category_by_name(self, name: str) -> dict | None:
        rows = self._query("SELECT * FROM categories WHERE name = ?", (name,))
        return rows[0] if rows else None

    def save_category(self, category_id: int | None, fields: dict) -> int:
        values = (fields["name"], fields["color"], fields["group_token"], fields["valid_days"], fields["description"])
        if category_id:
            self._execute(
                "UPDATE categories SET name = ?, color = ?, group_token = ?, valid_days = ?, description = ? "
                "WHERE id = ?",
                (*values, category_id),
            )
            return category_id
        return self._execute(
            "INSERT INTO categories (name, color, group_token, valid_days, description) VALUES (?, ?, ?, ?, ?)", values
        )

    def delete_category(self, category_id: int) -> None:
        self._execute("UPDATE persons SET category_id = 0 WHERE category_id = ?", (category_id,))
        self._execute("DELETE FROM categories WHERE id = ?", (category_id,))

    def persons_of_category(self, category_id: int) -> list[dict]:
        return [p for p in self.list_persons() if p["category_id"] == category_id]

    # Elementi nascosti e impostazioni dell'app

    def hidden(self, kind: str) -> set[str]:
        return {row["token"] for row in self._query("SELECT token FROM hidden_items WHERE kind = ?", (kind,))}

    def set_hidden(self, kind: str, token: str, hidden: bool) -> None:
        if hidden:
            self._execute("INSERT OR IGNORE INTO hidden_items (kind, token) VALUES (?, ?)", (kind, token))
        else:
            self._execute("DELETE FROM hidden_items WHERE kind = ? AND token = ?", (kind, token))

    def get_setting(self, key: str, default: str = "") -> str:
        rows = self._query("SELECT value FROM app_settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # Impianti: controller e porte

    CONTROLLER_FIELDS = (
        "name",
        "host",
        "username",
        "password",
        "verify_ssl",
        "mqtt_topic_prefix",
        "area_id",
        "description",
    )
    DOOR_FIELDS = (
        "door_token",
        "name",
        "snapshot_camera",
        "snapshot_on_granted",
        "ha_buttons",
        "intercom_name",
        "intercom_open_door",
        "intercom_relay",
        "intercom_mqtt_prefix",
        "area_id",
    )
    _DOOR_FLAGS = ("snapshot_on_granted", "ha_buttons", "intercom_open_door")

    def list_controllers(self) -> list[dict]:
        controllers = self._query("SELECT * FROM controllers ORDER BY id")
        doors = self.list_doors()
        for controller in controllers:
            controller["verify_ssl"] = bool(controller["verify_ssl"])
            controller["doors"] = [d for d in doors if d["controller_id"] == controller["id"]]
        return controllers

    def get_controller(self, controller_id: int) -> dict | None:
        return next((c for c in self.list_controllers() if c["id"] == controller_id), None)

    def save_controller(self, controller_id: int | None, fields: dict) -> int:
        fields = {"area_id": 0, "description": "", **fields}
        values = tuple(fields[f] for f in self.CONTROLLER_FIELDS)
        ts = now_iso()
        if controller_id:
            sets = ", ".join(f"{f} = ?" for f in self.CONTROLLER_FIELDS)
            self._execute(f"UPDATE controllers SET {sets}, updated_at = ? WHERE id = ?", (*values, ts, controller_id))
            return controller_id
        columns = ", ".join(self.CONTROLLER_FIELDS)
        marks = ", ".join("?" * len(self.CONTROLLER_FIELDS))
        return self._execute(
            f"INSERT INTO controllers ({columns}, created_at, updated_at) VALUES ({marks}, ?, ?)", (*values, ts, ts)
        )

    def delete_controller(self, controller_id: int) -> None:
        self._execute("DELETE FROM controllers WHERE id = ?", (controller_id,))

    def list_doors(self) -> list[dict]:
        doors = self._query("SELECT * FROM doors ORDER BY controller_id, id")
        for door in doors:
            for flag in self._DOOR_FLAGS:
                door[flag] = bool(door[flag])
        return doors

    def get_door(self, door_id: int) -> dict | None:
        return next((d for d in self.list_doors() if d["id"] == door_id), None)

    def save_door(self, door_id: int | None, controller_id: int, fields: dict) -> int:
        fields = {"area_id": 0, **fields}
        values = tuple(fields[f] for f in self.DOOR_FIELDS)
        if door_id:
            sets = ", ".join(f"{f} = ?" for f in self.DOOR_FIELDS)
            self._execute(f"UPDATE doors SET {sets} WHERE id = ?", (*values, door_id))
            return door_id
        columns = ", ".join(self.DOOR_FIELDS)
        marks = ", ".join("?" * len(self.DOOR_FIELDS))
        return self._execute(
            f"INSERT INTO doors (controller_id, {columns}) VALUES (?, {marks})", (controller_id, *values)
        )

    def delete_door(self, door_id: int) -> None:
        self._execute("DELETE FROM doors WHERE id = ?", (door_id,))

    # Strutture e aree: dove si trovano controller e porte

    def list_sites(self) -> list[dict]:
        sites = self._query("SELECT * FROM sites ORDER BY name COLLATE NOCASE")
        areas = self._query("SELECT * FROM areas ORDER BY name COLLATE NOCASE")
        for site in sites:
            site["areas"] = [a for a in areas if a["site_id"] == site["id"]]
        return sites

    def get_site(self, site_id: int) -> dict | None:
        return next((s for s in self.list_sites() if s["id"] == site_id), None)

    def site_by_name(self, name: str) -> dict | None:
        rows = self._query("SELECT * FROM sites WHERE name = ?", (name,))
        return rows[0] if rows else None

    def save_site(self, site_id: int | None, name: str, description: str) -> int:
        if site_id:
            self._execute("UPDATE sites SET name = ?, description = ? WHERE id = ?", (name, description, site_id))
            return site_id
        return self._execute("INSERT INTO sites (name, description) VALUES (?, ?)", (name, description))

    def delete_site(self, site_id: int) -> None:
        with self._lock:
            for table in ("doors", "controllers"):
                self._conn.execute(
                    f"UPDATE {table} SET area_id = 0 WHERE area_id IN (SELECT id FROM areas WHERE site_id = ?)",
                    (site_id,),
                )
            self._conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
            self._conn.commit()

    def get_area(self, area_id: int) -> dict | None:
        rows = self._query("SELECT * FROM areas WHERE id = ?", (area_id,))
        return rows[0] if rows else None

    def area_by_name(self, site_id: int, name: str) -> dict | None:
        rows = self._query("SELECT * FROM areas WHERE site_id = ? AND name = ?", (site_id, name))
        return rows[0] if rows else None

    def save_area(self, area_id: int | None, site_id: int, name: str) -> int:
        if area_id:
            self._execute("UPDATE areas SET name = ?, site_id = ? WHERE id = ?", (name, site_id, area_id))
            return area_id
        return self._execute("INSERT INTO areas (site_id, name) VALUES (?, ?)", (site_id, name))

    def delete_area(self, area_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE doors SET area_id = 0 WHERE area_id = ?", (area_id,))
            self._conn.execute("UPDATE controllers SET area_id = 0 WHERE area_id = ?", (area_id,))
            self._conn.execute("DELETE FROM areas WHERE id = ?", (area_id,))
            self._conn.commit()

    def set_door_area(self, door_id: int, area_id: int) -> None:
        self._execute("UPDATE doors SET area_id = ? WHERE id = ?", (area_id, door_id))

    def set_controller_area(self, controller_id: int, area_id: int) -> None:
        self._execute("UPDATE controllers SET area_id = ? WHERE id = ?", (area_id, controller_id))

    # Modifiche alla programmazione del controller, per poterle annullare

    def add_controller_change(
        self, user: str, controller_id: int, kind: str, token: str, summary: str, before: dict, after: dict
    ) -> int:
        return self._execute(
            "INSERT INTO controller_changes (ts, user, controller_id, kind, token, summary, before, after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), user, controller_id, kind, token, summary, json.dumps(before), json.dumps(after)),
        )

    def list_controller_changes(self, controller_id: int, limit: int = 30) -> list[dict]:
        rows = self._query(
            "SELECT id, ts, user, kind, token, summary, undone FROM controller_changes WHERE controller_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (controller_id, limit),
        )
        for row in rows:
            row["undone"] = bool(row["undone"])
        return rows

    def get_controller_change(self, change_id: int) -> dict | None:
        rows = self._query("SELECT * FROM controller_changes WHERE id = ?", (change_id,))
        if not rows:
            return None
        row = rows[0]
        row["before"], row["after"], row["undone"] = (
            json.loads(row["before"]),
            json.loads(row["after"]),
            bool(row["undone"]),
        )
        return row

    def list_controller_change_objects(self, controller_id: int, kind: str) -> list[dict]:
        """Oggetti salvati prima e dopo ogni modifica di un tipo, dalla più recente."""
        rows = self._query(
            "SELECT before, after FROM controller_changes WHERE controller_id = ? AND kind = ? ORDER BY id DESC",
            (controller_id, kind),
        )
        return [json.loads(row[side]) for row in rows for side in ("before", "after")]

    def mark_change_undone(self, change_id: int) -> None:
        self._execute("UPDATE controller_changes SET undone = 1 WHERE id = ?", (change_id,))

    # Utenti dell'app

    def list_app_users(self) -> list[dict]:
        rows = self._query("SELECT * FROM app_users ORDER BY name COLLATE NOCASE, username")
        for row in rows:
            row["perms"] = json.loads(row["perms"])
        return rows

    def get_app_user(self, username: str) -> dict | None:
        rows = self._query("SELECT * FROM app_users WHERE username = ?", (username,))
        if not rows:
            return None
        rows[0]["perms"] = json.loads(rows[0]["perms"])
        return rows[0]

    def save_app_user(self, username: str, name: str, perms: list[str]) -> None:
        self._execute(
            "INSERT INTO app_users (username, name, perms, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET name = excluded.name, perms = excluded.perms",
            (username, name, json.dumps(perms), now_iso()),
        )

    def touch_app_user(self, username: str, name: str) -> None:
        self._execute(
            "UPDATE app_users SET last_seen = ?, name = CASE WHEN ? != '' THEN ? ELSE name END WHERE username = ?",
            (now_iso(), name, name, username),
        )

    def delete_app_user(self, username: str) -> None:
        self._execute("DELETE FROM app_users WHERE username = ?", (username,))

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
        door: str = "",
    ) -> int:
        return self._execute(
            "INSERT INTO events (ts, kind, card, person_id, person_name, detail, source, raw, door) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, kind, card, person_id, person_name, detail, source, json.dumps(raw) if raw else "", door),
        )

    @staticmethod
    def _event_filter(
        kind: str = "",
        search: str = "",
        since: str = "",
        until: str = "",
        person_id: int | None = None,
        door: str = "",
    ) -> tuple[list[str], list]:
        clauses, params = [], []
        if kind:
            kinds = kind.split(",")
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params += kinds
        if door:
            doors = door.split(",")
            clauses.append(f"door IN ({','.join('?' * len(doors))})")
            params += doors
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts < ?")
            params.append(until)
        if person_id:
            clauses.append("person_id = ?")
            params.append(person_id)
        if search:
            clauses.append("(person_name LIKE ? OR card LIKE ? OR detail LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        return clauses, params

    def list_events(
        self,
        *,
        kind: str = "",
        search: str = "",
        before_id: int | None = None,
        limit: int = 100,
        since: str = "",
        until: str = "",
        person_id: int | None = None,
        door: str = "",
    ) -> list[dict]:
        clauses, params = self._event_filter(kind, search, since, until, person_id, door)
        if before_id:
            clauses.append("id < ?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._query(
            f"SELECT id, ts, kind, card, person_id, person_name, detail, source, snapshot, door FROM events {where} "
            "ORDER BY id DESC LIMIT ?",
            tuple(params),
        )

    def count_matching_events(self, **filters) -> int:
        clauses, params = self._event_filter(**filters)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._query(f"SELECT COUNT(*) AS n FROM events {where}", tuple(params))[0]["n"]

    def door_event_counts(self, since: str) -> dict[str, dict[str, int]]:
        """Eventi per porta e per tipo dal momento indicato."""
        result: dict[str, dict[str, int]] = {}
        rows = self._query("SELECT door, kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY door, kind", (since,))
        for row in rows:
            result.setdefault(row["door"], {})[row["kind"]] = row["n"]
        return result

    def last_door_events(self, kinds: tuple[str, ...]) -> dict[str, dict]:
        """Ultimo evento di ogni porta tra i tipi indicati."""
        marks = ",".join("?" * len(kinds))
        rows = self._query(
            f"SELECT e.door, e.ts, e.kind, e.person_name, e.card FROM events e JOIN "
            f"(SELECT door, MAX(id) AS id FROM events WHERE kind IN ({marks}) GROUP BY door) last "
            "ON e.id = last.id",
            kinds,
        )
        return {row["door"]: row for row in rows}

    def get_event(self, event_id: int) -> dict | None:
        rows = self._query("SELECT * FROM events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def set_event_snapshot(self, event_id: int, filename: str) -> None:
        self._execute("UPDATE events SET snapshot = ? WHERE id = ?", (filename, event_id))

    def count_events(self, since: str) -> dict[str, int]:
        rows = self._query("SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY kind", (since,))
        return {row["kind"]: row["n"] for row in rows}

    def event_kinds_since(self, since: str) -> list[dict]:
        """Ora e tipo degli eventi dal momento indicato, per le statistiche."""
        return self._query("SELECT ts, kind FROM events WHERE ts >= ?", (since,))

    def last_accesses(self) -> dict[int, str]:
        rows = self._query(
            "SELECT person_id, MAX(ts) AS ts FROM events WHERE person_id IS NOT NULL "
            "AND kind IN ('concesso', 'lettura') GROUP BY person_id"
        )
        return {row["person_id"]: row["ts"] for row in rows}

    def prune_events(self, days: int) -> list[str]:
        """Cancella gli eventi vecchi; restituisce i file delle istantanee da eliminare."""
        limit = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        snapshots = [
            row["snapshot"]
            for row in self._query("SELECT snapshot FROM events WHERE ts < ? AND snapshot != ''", (limit,))
        ]
        self._execute("DELETE FROM events WHERE ts < ?", (limit,))
        return snapshots

    # Audit

    def add_audit(self, user: str, action: str, detail: str = "") -> None:
        self._execute(
            "INSERT INTO audit (ts, user, action, detail) VALUES (?, ?, ?, ?)", (now_iso(), user, action, detail)
        )

    def list_audit(self, limit: int = 100, before_id: int | None = None) -> list[dict]:
        if before_id:
            return self._query("SELECT * FROM audit WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        return self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
