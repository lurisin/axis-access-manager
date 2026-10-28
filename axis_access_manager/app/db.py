"""Database SQLite dell'add-on: anagrafica, tessere, tipologie, regole, eventi, utenti, audit."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from .backup_copy import recover, update_copy

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
CREATE TABLE IF NOT EXISTS category_rules (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    schedule TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS controller_inputs (
    controller_id INTEGER NOT NULL REFERENCES controllers(id) ON DELETE CASCADE,
    io TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    door_token TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT 'no',
    supervised INTEGER NOT NULL DEFAULT 0,
    schedule TEXT NOT NULL DEFAULT '',
    idpoint_token TEXT NOT NULL DEFAULT '',
    access_point_token TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (controller_id, io)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""

# Fino alla 0.17 si aggiungevano solo colonne: (tabella, colonna, definizione). Sono la versione 1 dello schema.
LEGACY_COLUMNS = [
    ("cards", "access_profile", "TEXT NOT NULL DEFAULT ''"),
    ("cards", "valid_from", "TEXT NOT NULL DEFAULT ''"),
    ("cards", "valid_to", "TEXT NOT NULL DEFAULT ''"),
    ("persons", "company", "TEXT NOT NULL DEFAULT ''"),
    ("events", "snapshot", "TEXT NOT NULL DEFAULT ''"),
    ("events", "snapshot_error", "TEXT NOT NULL DEFAULT ''"),
    ("persons", "category_id", "INTEGER NOT NULL DEFAULT 0"),
    ("events", "door", "TEXT NOT NULL DEFAULT ''"),
    ("doors", "area_id", "INTEGER NOT NULL DEFAULT 0"),
    ("controllers", "area_id", "INTEGER NOT NULL DEFAULT 0"),
    ("controllers", "description", "TEXT NOT NULL DEFAULT ''"),
    ("controllers", "snapshot_camera", "TEXT NOT NULL DEFAULT ''"),
]

# Migrazioni numerate: (versione, descrizione, istruzioni SQL). PRAGMA user_version tiene l'ultima applicata.
# Ogni migrazione gira una volta sola, in una transazione; prima si fa una copia del database.
SCHEMA_STEPS: list[tuple[int, str, list[str]]] = [
    (1, "colonne aggiunte fino alla 0.17", []),
    (
        2,
        "identità del controller, tessere non presenti sul controller, eventi recuperati",
        [
            "ALTER TABLE controllers ADD COLUMN serial TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE controllers ADD COLUMN model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE controllers ADD COLUMN firmware TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE controllers ADD COLUMN cert_fingerprint TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE controllers ADD COLUMN identity_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE controllers ADD COLUMN identity_change TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE cards ADD COLUMN missing_since TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE events ADD COLUMN recovered INTEGER NOT NULL DEFAULT 0",
        ],
    ),
    (
        3,
        "impronta degli eventi del controller, per non registrarli due volte",
        [
            "ALTER TABLE events ADD COLUMN ref TEXT NOT NULL DEFAULT ''",
            # Stessa formula di service.event_ref
            "UPDATE events SET ref = 'lettura|' || door || '|' || lower(card) "
            "WHERE kind IN ('lettura', 'sconosciuta') AND source = 'lettore'",
            "UPDATE events SET ref = kind || '|' || door "
            "WHERE kind IN ('concesso', 'negato') AND source = 'controller'",
            "UPDATE events SET ref = kind || '|' || door || '|' || detail WHERE kind IN ('allarme', 'manomissione')",
            # Doppioni lasciati dal recupero della 0.18.0: recuperati con un gemello arrivato via MQTT
            # nello stesso secondo
            "DELETE FROM events WHERE recovered = 1 AND ref != '' AND EXISTS (SELECT 1 FROM events AS live "
            "WHERE live.recovered = 0 AND live.ref = events.ref "
            "AND abs(julianday(live.ts) - julianday(events.ts)) * 86400 <= 1.5)",
        ],
    ),
    (
        4,
        "eventi staccati dalle persone nuove che avevano preso il numero di una persona eliminata",
        [
            # Un evento più vecchio della persona non può essere suo: era della persona eliminata con lo stesso numero
            "UPDATE events SET person_id = NULL WHERE person_id IS NOT NULL "
            "AND ts < (SELECT created_at FROM persons WHERE persons.id = events.person_id)",
        ],
    ),
    (
        5,
        "eventi staccati dalle persone eliminate",
        [
            # Prima si ricorda il numero più alto citato dagli eventi, che dopo non lo citano più
            "INSERT INTO app_settings (key, value) SELECT 'persons_last_id', CAST(COALESCE(MAX(m), 0) AS TEXT) FROM ("
            "SELECT MAX(id) AS m FROM persons UNION ALL SELECT MAX(person_id) FROM events "
            "UNION ALL SELECT CAST(value AS INTEGER) FROM app_settings WHERE key = 'persons_last_id') "
            "WHERE true ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            "UPDATE events SET person_id = NULL WHERE person_id IS NOT NULL "
            "AND person_id NOT IN (SELECT id FROM persons)",
            # Persone ripristinate dopo la migrazione 4 con il numero di una eliminata: di nuovo gli eventi più vecchi
            "UPDATE events SET person_id = NULL WHERE person_id IS NOT NULL "
            "AND ts < (SELECT created_at FROM persons WHERE persons.id = events.person_id)",
        ],
    ),
    (
        6,
        "aperture dai pulsanti di uscita come Uscita",
        # Con la 0.21.4 erano aperture remote, con ruolo e morsetto nel dettaglio
        ["UPDATE events SET kind = 'uscita' WHERE kind = 'apertura_remota' AND detail LIKE 'Pulsante Uscita · %'"],
    ),
    (
        7,
        "indice degli eventi per porta, morsetti AUX in maiuscolo",
        [
            # Ultimo passaggio di ogni porta senza leggere e ordinare tutto il registro
            "CREATE INDEX IF NOT EXISTS events_door_ts ON events(door, ts)",
            "UPDATE events SET detail = replace(detail, ' · Aux ', ' · AUX ') "
            "WHERE kind IN ('uscita', 'apertura_remota', 'negato') AND instr(detail, ' · Aux ') > 0",
        ],
    ),
]
SCHEMA_VERSION = SCHEMA_STEPS[-1][0]
SETTING_APP_VERSION = "app_version"
# Numero più alto mai dato a una persona: i numeri delle persone eliminate non si riusano
SETTING_LAST_PERSON = "persons_last_id"
# Lo stesso per controller, porte, aree, strutture e tipologie: impostazione e righe che citano ancora i numeri
# degli eliminati (righe di accesso delle tipologie, Storico Modifiche), che non devono passare a uno nuovo
LAST_IDS = {
    "controllers": ("controllers_last_id", "SELECT MAX(controller_id) FROM controller_changes"),
    "doors": ("doors_last_id", "SELECT MAX(target_id) FROM category_rules WHERE target = 'door'"),
    "areas": ("areas_last_id", "SELECT MAX(target_id) FROM category_rules WHERE target = 'area'"),
    "sites": ("sites_last_id", "SELECT MAX(target_id) FROM category_rules WHERE target = 'site'"),
    "categories": ("categories_last_id", "SELECT 0"),
}


# Campi degli eventi che servono alle pagine (il messaggio originale resta nel database)
EVENT_COLUMNS = "id, ts, kind, card, person_id, person_name, detail, source, snapshot, snapshot_error, door, recovered"
# Origini fisse degli eventi: nel database il codice, per l'utente l'etichetta
SOURCE_LABELS = {"lettore": "Lettore", "controller": "Controller", "porta": "Porta"}


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def local_day_start(days_ago: int = 0, now: datetime | None = None) -> str:
    """Mezzanotte locale di oggi (o di days_ago giorni fa), in UTC come i timestamp del log.

    La mezzanotte senza fuso si converte con le regole del fuso locale di quel giorno: nei giorni del cambio
    dell'ora ha uno scarto diverso da quello di adesso."""
    day = (now or datetime.now(tz=UTC)).astimezone().date() - timedelta(days=days_ago)
    return datetime.combine(day, time.min).astimezone(UTC).isoformat(timespec="seconds")


class Database:
    """Accesso al database da più thread: una connessione protetta da un lock."""

    def __init__(self, path: Path | str = DB_PATH, app_version: str = "") -> None:
        """app_version: versione dell'add-on; se cambia rispetto all'ultimo avvio si fa una copia del database."""
        self.path = Path(path) if str(path) != ":memory:" else None
        self.recovered = recover(self.path) if self.path else ""
        existed = bool(self.path and self.path.exists() and self.path.stat().st_size > 0)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # Scritture dentro batch(): il commit si fa una volta alla fine
        self._batch = 0
        self.copied = ""
        with self._lock:
            # Con il journal WAL chi legge non ferma chi scrive: la copia per il backup di Home Assistant
            # (un altro processo, anche per molti secondi) non blocca più gli eventi e le pagine
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            previous = self._app_version() if existed else ""
            if existed and (version < SCHEMA_VERSION or (app_version and previous and previous != app_version)):
                # Prima di toccare lo schema o dopo un aggiornamento dell'add-on: copia del database com'era
                reason = f"v{previous}" if previous else f"schema{version}"
                self.copied = update_copy(self.path, reason).name
            self._conn.executescript(SCHEMA)
            for step, _, statements in SCHEMA_STEPS:
                if step <= version:
                    continue
                # Tutta la migrazione o niente: anche ALTER TABLE e user_version stanno nella transazione
                self._conn.execute("BEGIN")
                try:
                    if step == 1:
                        self._add_legacy_columns()
                    for sql in statements:
                        self._conn.execute(sql)
                    self._conn.execute(f"PRAGMA user_version = {step}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            if app_version:
                self._conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (SETTING_APP_VERSION, app_version),
                )
            self._conn.commit()

    def _app_version(self) -> str:
        try:
            row = self._conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTING_APP_VERSION,)).fetchone()
        except sqlite3.OperationalError:
            return ""
        return row[0] if row else ""

    def _add_legacy_columns(self) -> None:
        for table, column, definition in LEGACY_COLUMNS:
            columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._commit()
            return cursor.lastrowid

    def _commit(self) -> None:
        if not self._batch:
            self._conn.commit()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Molte scritture di seguito (sincronizzazione, recupero eventi) con un solo commit alla fine:
        un commit per riga sulla scheda SD ferma l'add-on per secondi."""
        self._batch += 1
        try:
            yield
        finally:
            self._batch -= 1
            if not self._batch:
                with self._lock:
                    self._conn.commit()

    def _insert_new(self, table: str, sql: str, params: tuple, new_id: int | None = None) -> int:
        """Riga nuova con un numero mai dato prima nella tabella (o new_id); sql ha l'id come primo parametro."""
        setting, cited = LAST_IDS[table]
        with self._lock:
            if new_id is None:
                new_id = (
                    self._conn.execute(
                        f"SELECT MAX(m) FROM (SELECT MAX(id) AS m FROM {table} UNION ALL {cited} "
                        "UNION ALL SELECT CAST(value AS INTEGER) FROM app_settings WHERE key = ?)",
                        (setting,),
                    ).fetchone()[0]
                    or 0
                ) + 1
            cursor = self._conn.execute(sql, (new_id, *params))
            self._remember_last_id(setting, cursor.lastrowid)
            self._commit()
            return cursor.lastrowid

    def _remember_last_id(self, setting: str, value: int) -> None:
        # Il valore è testo: senza CAST anche sul nuovo, MAX tra numero e testo dà sempre il testo
        self._conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
            (setting, str(value)),
        )

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

    def add_person(
        self, name: str, axis_user_token: str | None, person_id: int | None = None, created_at: str | None = None
    ) -> int:
        """person_id e created_at solo per rimettere una persona da un backup (stesso numero, se è libero).

        Il numero di una persona eliminata non si riusa: i suoi eventi lo citano ancora, e una persona nuova
        con lo stesso numero ne erediterebbe passaggi e ultimo accesso.
        """
        ts = now_iso()
        with self._lock:
            if person_id is None:
                person_id = (
                    self._conn.execute(
                        "SELECT MAX(m) FROM (SELECT MAX(id) AS m FROM persons "
                        "UNION ALL SELECT MAX(person_id) FROM events "
                        "UNION ALL SELECT CAST(value AS INTEGER) FROM app_settings WHERE key = ?)",
                        (SETTING_LAST_PERSON,),
                    ).fetchone()[0]
                    or 0
                ) + 1
            cursor = self._conn.execute(
                "INSERT INTO persons (id, name, axis_user_token, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (person_id, name, axis_user_token, created_at or ts, ts),
            )
            # Una persona rimessa con un numero più basso non abbassa il contatore
            self._remember_last_id(SETTING_LAST_PERSON, cursor.lastrowid)
            self._commit()
            return cursor.lastrowid

    def rename_person(self, person_id: int, name: str) -> None:
        self._execute("UPDATE persons SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), person_id))

    def set_person_user_token(self, person_id: int, token: str) -> None:
        self._execute(
            "UPDATE persons SET axis_user_token = ?, updated_at = ? WHERE id = ?", (token, now_iso(), person_id)
        )

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
        """Gli eventi restano nel registro con il nome, staccati dalla persona: nessuno li può ereditare."""
        with self._lock:
            self._conn.execute("UPDATE events SET person_id = NULL WHERE person_id = ?", (person_id,))
            self._conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
            self._commit()

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
        # Una tessera ricomparsa sul controller non è più "non presente"
        sql = "UPDATE cards SET person_id = ?, number = ?, enabled = ?, missing_since = ''"
        params: tuple = (person_id, number, int(enabled))
        if access is not None:
            sql += ", access_profile = ?, valid_from = ?, valid_to = ?"
            params += (access.get("access_profile", ""), access.get("valid_from", ""), access.get("valid_to", ""))
        self._execute(f"{sql} WHERE id = ?", (*params, card_id))

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

    def mark_cards_missing(self, card_ids: list[int], since: str) -> None:
        with self._lock:
            self._conn.executemany(
                "UPDATE cards SET missing_since = ? WHERE id = ? AND missing_since = ''",
                [(since, card_id) for card_id in card_ids],
            )
            self._commit()

    def missing_cards(self) -> list[dict]:
        """Tessere che l'app conosce ma che sul controller non ci sono più."""
        rows = self._query(
            "SELECT cards.*, persons.name AS person_name FROM cards JOIN persons ON persons.id = cards.person_id "
            "WHERE cards.missing_since != '' ORDER BY persons.name COLLATE NOCASE, cards.id"
        )
        for row in rows:
            row["enabled"] = bool(row["enabled"])
        return rows

    def set_card_token(self, card_id: int, token: str) -> None:
        """Tessera riscritta sul controller: nuovo token, di nuovo presente."""
        self._execute("UPDATE cards SET axis_credential_token = ?, missing_since = '' WHERE id = ?", (token, card_id))

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
        # Le ricorrenti per mese e giorno, le altre per data
        rows = self._query(
            "SELECT * FROM holidays ORDER BY yearly DESC, CASE WHEN yearly THEN substr(day, 6) ELSE day END, day"
        )
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

    def save_category(self, category_id: int | None, fields: dict, new_id: int | None = None) -> int:
        """new_id: numero da usare per una tipologia nuova (rimessa da un backup), se è libero."""
        values = (fields["name"], fields["color"], fields["group_token"], fields["valid_days"], fields["description"])
        if category_id:
            self._execute(
                "UPDATE categories SET name = ?, color = ?, group_token = ?, valid_days = ?, description = ? "
                "WHERE id = ?",
                (*values, category_id),
            )
            return category_id
        if new_id and self.get_category(new_id) is not None:
            new_id = None
        return self._insert_new(
            "categories",
            "INSERT INTO categories (id, name, color, group_token, valid_days, description) VALUES (?, ?, ?, ?, ?, ?)",
            values,
            new_id or None,
        )

    def category_rules(self, category_id: int) -> list[dict]:
        """Righe di accesso della tipologia: porta, area o struttura, con la fascia oraria."""
        return self._query(
            "SELECT target, target_id, schedule FROM category_rules WHERE category_id = ? ORDER BY id", (category_id,)
        )

    def all_category_rules(self) -> dict[int, list[dict]]:
        rows = self._query("SELECT category_id, target, target_id, schedule FROM category_rules ORDER BY id")
        result: dict[int, list[dict]] = {}
        for row in rows:
            result.setdefault(row.pop("category_id"), []).append(row)
        return result

    def set_category_rules(self, category_id: int, rules: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM category_rules WHERE category_id = ?", (category_id,))
            self._conn.executemany(
                "INSERT INTO category_rules (category_id, target, target_id, schedule) VALUES (?, ?, ?, ?)",
                [(category_id, r["target"], r["target_id"], r["schedule"]) for r in rules],
            )
            self._conn.commit()

    def delete_category(self, category_id: int) -> None:
        self._execute("UPDATE persons SET category_id = 0 WHERE category_id = ?", (category_id,))
        self._execute("DELETE FROM categories WHERE id = ?", (category_id,))

    def persons_of_category(self, category_id: int) -> list[dict]:
        return [p for p in self.list_persons() if p["category_id"] == category_id]

    # Elementi nascosti e impostazioni dell'app

    def all_hidden(self) -> list[dict]:
        return self._query("SELECT kind, token FROM hidden_items")

    def hidden(self, kind: str) -> set[str]:
        return {row["token"] for row in self._query("SELECT token FROM hidden_items WHERE kind = ?", (kind,))}

    def set_hidden(self, kind: str, token: str, hidden: bool) -> None:
        if hidden:
            self._execute("INSERT OR IGNORE INTO hidden_items (kind, token) VALUES (?, ?)", (kind, token))
        else:
            self._execute("DELETE FROM hidden_items WHERE kind = ? AND token = ?", (kind, token))

    # Colonne con i token del controller: se un oggetto ricreato riceve un token nuovo, si cambiano ovunque
    # (anche la porta degli eventi già nel registro, che restano della porta ricreata)
    TOKEN_COLUMNS = (
        ("events", "door"),
        ("schedule_rules", "schedule_token"),
        ("category_rules", "schedule"),
        ("categories", "group_token"),
        ("cards", "access_profile"),
        ("hidden_items", "token"),
        ("doors", "door_token"),
        ("controller_inputs", "door_token"),
        ("controller_inputs", "schedule"),
        ("controller_inputs", "idpoint_token"),
        ("controller_inputs", "access_point_token"),
        ("controller_changes", "token"),
    )

    def replace_token(self, old: str, new: str) -> int:
        """Sostituisce un token del controller in tutta l'app (ripristino completo); restituisce le righe cambiate."""
        if not old or old == new:
            return 0
        statements = [
            (f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old)) for table, column in self.TOKEN_COLUMNS
        ]
        # Anche la porta che in Home Assistant ha gli identificativi di sempre (ha_mqtt.LEGACY_DOOR_SETTING)
        statements.append(
            (
                "UPDATE app_settings SET value = ? WHERE key IN ('default_group', 'ha_legacy_door') AND value = ?",
                (new, old),
            )
        )
        # Storico Modifiche: Annulla riscrive l'oggetto salvato, che deve avere il token nuovo
        quoted = (json.dumps(old), json.dumps(new))
        statements.append(
            (
                "UPDATE controller_changes SET before = replace(before, ?1, ?2), after = replace(after, ?1, ?2) "
                "WHERE instr(before, ?1) OR instr(after, ?1)",
                quoted,
            )
        )
        with self._lock:
            changed = sum(self._conn.execute(sql, params).rowcount for sql, params in statements)
            self._conn.commit()
        return changed

    def all_settings(self) -> list[dict]:
        return self._query("SELECT key, value FROM app_settings ORDER BY key")

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
        return self._insert_new(
            "controllers",
            f"INSERT INTO controllers (id, {columns}, created_at, updated_at) VALUES (?, {marks}, ?, ?)",
            (*values, ts, ts),
        )

    def set_controller_camera(self, controller_id: int, camera: str) -> None:
        """Telecamera che fotografa il controller quando si apre il coperchio."""
        self._execute(
            "UPDATE controllers SET snapshot_camera = ?, updated_at = ? WHERE id = ?",
            (camera, now_iso(), controller_id),
        )

    def set_controller_identity(self, controller_id: int, identity: dict) -> None:
        """Numero di serie, modello, firmware e impronta del certificato del controller riconosciuto."""
        self._execute(
            "UPDATE controllers SET serial = ?, model = ?, firmware = ?, cert_fingerprint = ?, identity_at = ? "
            "WHERE id = ?",
            (
                identity.get("serial", ""),
                identity.get("model", ""),
                identity.get("firmware", ""),
                identity.get("cert_fingerprint", ""),
                now_iso(),
                controller_id,
            ),
        )

    def set_controller_identity_change(self, controller_id: int, observed: str) -> None:
        """Apparecchio diverso trovato all'indirizzo (JSON), vuoto quando non c'è niente da confermare."""
        self._execute("UPDATE controllers SET identity_change = ? WHERE id = ?", (observed, controller_id))

    def delete_controller(self, controller_id: int) -> None:
        """Porte e ingressi se ne vanno con il controller; il suo Storico Modifiche non si annulla su un altro."""
        with self._lock:
            self._conn.execute("DELETE FROM controller_changes WHERE controller_id = ?", (controller_id,))
            self._conn.execute("DELETE FROM controllers WHERE id = ?", (controller_id,))
            self._commit()

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
        return self._insert_new(
            "doors",
            f"INSERT INTO doors (id, controller_id, {columns}) VALUES (?, ?, {marks})",
            (controller_id, *values),
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
        return self._insert_new(
            "sites", "INSERT INTO sites (id, name, description) VALUES (?, ?, ?)", (name, description)
        )

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
        return self._insert_new("areas", "INSERT INTO areas (id, site_id, name) VALUES (?, ?, ?)", (site_id, name))

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

    # Ingressi fisici del controller: nome, ruolo e porta decisi nell'app

    def controller_inputs(self, controller_id: int) -> dict[str, dict]:
        rows = self._query("SELECT * FROM controller_inputs WHERE controller_id = ?", (controller_id,))
        for row in rows:
            row["supervised"] = bool(row["supervised"])
        return {row["io"]: row for row in rows}

    def all_controller_inputs(self) -> list[dict]:
        return self._query("SELECT * FROM controller_inputs ORDER BY controller_id")

    def save_controller_input(self, controller_id: int, io: str, values: dict) -> None:
        self._execute(
            "INSERT INTO controller_inputs (controller_id, io, name, role, door_token, contact, supervised, schedule, "
            "idpoint_token, access_point_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (controller_id, io) DO UPDATE SET name = excluded.name, role = excluded.role, "
            "door_token = excluded.door_token, contact = excluded.contact, supervised = excluded.supervised, "
            "schedule = excluded.schedule, idpoint_token = excluded.idpoint_token, "
            "access_point_token = excluded.access_point_token",
            (
                controller_id,
                io,
                values.get("name", ""),
                values.get("role", ""),
                values.get("door_token", ""),
                values.get("contact", "no"),
                int(bool(values.get("supervised"))),
                values.get("schedule", ""),
                values.get("idpoint_token", ""),
                values.get("access_point_token", ""),
            ),
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
        # Una lettura recuperata dal registro del controller può essere più vecchia di quelle già viste
        self._execute(
            "INSERT INTO unknown_cards (number, first_seen, last_seen, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(number) DO UPDATE SET first_seen = MIN(first_seen, excluded.first_seen), "
            "last_seen = MAX(last_seen, excluded.last_seen), count = count + 1",
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
        recovered: bool = False,
        ref: str = "",
    ) -> int:
        return self._execute(
            "INSERT INTO events (ts, kind, card, person_id, person_name, detail, source, raw, door, recovered, ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                kind,
                card,
                person_id,
                person_name,
                detail,
                source,
                json.dumps(raw) if raw else "",
                door,
                int(recovered),
                ref,
            ),
        )

    def holder_name(self, holder: str) -> str:
        """Nome registrato negli accessi di chi aveva questo token utente dell'A1210 (ora tolto dall'app)."""
        rows = self._query(
            "SELECT person_name FROM events WHERE kind = 'concesso' AND instr(raw, ?) > 0 AND person_name != '' "
            "AND person_name != ? AND instr(person_name, ?) = 0 ORDER BY id DESC LIMIT 1",
            (json.dumps({"CredentialHolderName": holder})[1:-1], holder, "(tolta dall'app)"),
        )
        return rows[0]["person_name"] if rows else ""

    def similar_event(self, *, ts: str, ref: str, only_recovered: bool = False) -> bool:
        """Stesso evento del controller (stessa impronta) già registrato entro un secondo;
        only_recovered: solo tra i recuperati."""
        try:
            moment = datetime.fromisoformat(ts)
        except ValueError:
            return False
        low = (moment - timedelta(seconds=1)).isoformat(timespec="seconds")
        high = (moment + timedelta(seconds=1)).isoformat(timespec="seconds")
        return bool(
            self._query(
                "SELECT 1 FROM events WHERE ts BETWEEN ? AND ? AND ref = ? AND recovered >= ? LIMIT 1",
                (low, high, ref, int(only_recovered)),
            )
        )

    def recent_event(self, kind: str, source: str, door: str, detail: str, ts: str, window: int) -> dict | None:
        """L'ultima riga dello stesso tipo, sorgente, porta e dettaglio (anche già segnata) nei window secondi
        prima di ts."""
        try:
            moment = datetime.fromisoformat(ts)
        except ValueError:
            return None
        low = (moment - timedelta(seconds=window)).isoformat(timespec="seconds")
        rows = self._query(
            "SELECT id, detail FROM events WHERE kind = ? AND source = ? AND door = ? AND instr(detail, ?) = 1 "
            "AND ts BETWEEN ? AND ? ORDER BY ts DESC, id DESC LIMIT 1",
            (kind, source, door, detail, low, ts),
        )
        return rows[0] if rows else None

    def set_event_detail(self, event_id: int, detail: str) -> None:
        self._execute("UPDATE events SET detail = ? WHERE id = ?", (detail, event_id))

    def last_controller_event(self, kinds: tuple[str, ...]) -> str:
        """Ora dell'ultimo evento arrivato dal controller: da lì si recuperano quelli persi."""
        marks = ", ".join("?" * len(kinds))
        rows = self._query(f"SELECT MAX(ts) AS ts FROM events WHERE kind IN ({marks})", kinds)
        return rows[0]["ts"] or "" if rows else ""

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
            # Anche chi ha aperto (Home Assistant, pulsanti, citofono: è l'origine, non la persona) e il nome
            # della porta; non i codici delle origini fisse. % e _ si cercano come caratteri
            clauses.append(
                "(person_name LIKE ? ESCAPE '\\' OR card LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' "
                f"OR (source LIKE ? ESCAPE '\\' AND source NOT IN ({','.join('?' * len(SOURCE_LABELS))})) "
                "OR door IN (SELECT door_token FROM doors WHERE name LIKE ? ESCAPE '\\'))"
            )
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            params += [like, like, like, like, *SOURCE_LABELS, like]
        return clauses, params

    def list_events(
        self,
        *,
        kind: str = "",
        search: str = "",
        before_id: int | None = None,
        before_ts: str = "",
        limit: int = 100,
        since: str = "",
        until: str = "",
        person_id: int | None = None,
        door: str = "",
    ) -> list[dict]:
        """Eventi dal più recente per ora, non per arrivo: quelli recuperati entrano dopo ma al loro posto.

        "Carica altri" riparte dall'ultimo mostrato (ora e numero, che a parità di ora decide l'ordine).
        """
        clauses, params = self._event_filter(kind, search, since, until, person_id, door)
        if before_id:
            if not before_ts:
                rows = self._query("SELECT ts FROM events WHERE id = ?", (before_id,))
                before_ts = rows[0]["ts"] if rows else ""
            if before_ts:
                clauses.append("(ts < ? OR (ts = ? AND id < ?))")
                params += [before_ts, before_ts, before_id]
            else:
                clauses.append("id < ?")
                params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._query(
            f"SELECT {EVENT_COLUMNS} FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?",
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
        """Ultimo evento di ogni porta tra i tipi indicati.

        Con l'indice per porta e ora: le porte si saltano da una all'altra e di ognuna si legge dalla fine,
        senza leggere e ordinare tutto il registro (la pagina delle porte lo chiede ogni 15 secondi)."""
        marks = ",".join("?" * len(kinds))
        rows = self._query(
            "WITH RECURSIVE doors_seen(door) AS (SELECT MIN(door) FROM events UNION ALL "
            "SELECT (SELECT MIN(door) FROM events WHERE door > doors_seen.door) FROM doors_seen "
            "WHERE doors_seen.door IS NOT NULL) "
            "SELECT door, ts, kind, person_name, card FROM events WHERE id IN ("
            "SELECT (SELECT id FROM events WHERE events.door = doors_seen.door "
            f"AND kind IN ({marks}) ORDER BY ts DESC, id DESC LIMIT 1) FROM doors_seen "
            "WHERE doors_seen.door IS NOT NULL)",
            kinds,
        )
        return {row["door"]: row for row in rows}

    def get_event(self, event_id: int) -> dict | None:
        rows = self._query("SELECT * FROM events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def event_row(self, event_id: int) -> dict | None:
        """Un evento con gli stessi campi delle liste del registro."""
        rows = self._query(f"SELECT {EVENT_COLUMNS} FROM events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def set_event_snapshot(self, event_id: int, filename: str, error: str = "") -> None:
        """Foto dell'evento, o perché non è stato possibile scattarla."""
        self._execute("UPDATE events SET snapshot = ?, snapshot_error = ? WHERE id = ?", (filename, error, event_id))

    def count_events(self, since: str) -> dict[str, int]:
        rows = self._query("SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY kind", (since,))
        return {row["kind"]: row["n"] for row in rows}

    def event_kinds_since(self, since: str) -> list[dict]:
        """Ora e tipo degli eventi dal momento indicato, per le statistiche."""
        return self._query("SELECT ts, kind FROM events WHERE ts >= ?", (since,))

    def last_accesses(self) -> dict[int, str]:
        """Ultimo accesso concesso di ogni persona. Non le letture: la tessera sospesa, scaduta o fuori fascia
        viene letta anche quando il controller poi rifiuta il passaggio."""
        rows = self._query(
            "SELECT person_id, MAX(ts) AS ts FROM events WHERE person_id IS NOT NULL "
            "AND kind = 'concesso' GROUP BY person_id"
        )
        return {row["person_id"]: row["ts"] for row in rows}

    def translate_details(self, kind: str, mapping: dict[str, str]) -> int:
        """Traduce i dettagli salvati con il codice del controller (eventi delle versioni precedenti)."""
        changed = 0
        with self._lock:
            for code, text in mapping.items():
                changed += self._conn.execute(
                    "UPDATE events SET detail = ? WHERE kind = ? AND detail = ?", (text, kind, code)
                ).rowcount
            self._conn.commit()
        return changed

    def rename_controller_events(self, detail: str, new_detail: str, source: str) -> int:
        """Eventi del controller delle versioni precedenti: nuovo dettaglio e nome del controller come origine."""
        with self._lock:
            changed = self._conn.execute(
                "UPDATE events SET detail = ?, source = ?, door = '' WHERE kind = 'manomissione' AND detail = ?",
                (new_detail, source, detail),
            ).rowcount
            self._conn.commit()
        return changed

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

    def set_controller_event_source(self, name: str, old: str = "") -> int:
        """Eventi del controller senza porta (coperchio aperto) con il nome attuale del controller.

        Con old solo quelli registrati con quel nome; senza, tutti (c'è un solo controller).
        """
        where = "kind = 'manomissione' AND door = '' AND source NOT IN ('porta', ?)"
        params: tuple = (name,)
        if old:
            where += " AND source = ?"
            params += (old,)
        with self._lock:
            cur = self._conn.execute(f"UPDATE events SET source = ? WHERE {where}", (name, *params))
            self._conn.commit()
            return cur.rowcount

    def add_audit(self, user: str, action: str, detail: str = "") -> None:
        self._execute(
            "INSERT INTO audit (ts, user, action, detail) VALUES (?, ?, ?, ?)", (now_iso(), user, action, detail)
        )

    def list_audit(self, limit: int = 100, before_id: int | None = None) -> list[dict]:
        if before_id:
            return self._query("SELECT * FROM audit WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        return self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
