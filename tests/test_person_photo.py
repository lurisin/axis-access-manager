"""Foto della persona (person_photo, 0.28.3): una per persona, solo nell'app. La vede chi ha il permesso persone, la
cambia chi le può modificare. Riconosciuta dai primi byte (JPEG, PNG, WebP) e mai dal tipo dichiarato, al massimo
512 KB. L'elenco delle persone ne riporta solo la versione, letta dall'indice; mai i byte nell'elenco, nei backup
dell'app, in Home Assistant, nel registro o nel log. Non sono le foto della telecamera degli eventi.

Le immagini sono disegnate qui: una sagoma generica, mai la foto di una persona."""

import base64
import json
import logging
import sqlite3
import struct
import zlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_backup import Plant
from test_ha_mqtt import FakeClient

from app import db as db_module
from app.db import Database
from app.events import Event, EventMonitor
from app.ha_mqtt import HaBridge
from app.main import create_app
from app.service import PHOTO_MAX, AccessService, ServiceError, check_photo

ADMIN = {"X-Remote-User-Name": "admin"}
VIEWER = {"X-Remote-User-Name": "consulta"}
LOG_ONLY = {"X-Remote-User-Name": "portiere"}


def silhouette_png(size: int = 48, shade: int = 90) -> bytes:
    """PNG in scala di grigi con una sagoma generica (testa e spalle) su fondo chiaro, scritto a mano: niente
    dipendenze e nessuna persona vera."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            head = (x - size / 2) ** 2 + (y - size * 0.38) ** 2 <= (size * 0.18) ** 2
            shoulders = y > size * 0.62 and abs(x - size / 2) < size * 0.38 - (size - y) * 0.2
            row.append(shade if head or shoulders else 230)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


# Intestazioni dei formati con dati di riempimento: il server guarda solo i primi byte
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64 + b"\xff\xd9"
WEBP = b"RIFF" + struct.pack("<I", 30) + b"WEBPVP8L" + struct.pack("<I", 10) + b"\x2f" + b"\x00" * 13
PNG = silhouette_png()


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    with TestClient(app) as client:
        client.get("/api/me", headers=ADMIN)
        client.post("/api/users", json={"username": "consulta", "perms": ["persone"]}, headers=ADMIN)
        client.post("/api/users", json={"username": "portiere", "perms": ["stato", "registro"]}, headers=ADMIN)
        yield client


def new_person(client, **fields) -> dict:
    body = {"first_name": "Mario", "last_name": "Rossi", "card": "04aa", **fields}
    response = client.post("/api/persons", json=body, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def put_photo(client, person_id: int, data: bytes, content_type: str = "image/jpeg", headers=ADMIN):
    return client.put(
        f"/api/persons/{person_id}/photo", content=data, headers={**headers, "Content-Type": content_type}
    )


def test_drawn_silhouette_is_a_real_png():
    assert check_photo(PNG) == "image/png"
    assert PNG[12:16] == b"IHDR" and struct.unpack(">II", PNG[16:24]) == (48, 48)


def test_photo_is_seen_with_persone_and_changed_with_persone_modifica(client):
    person = new_person(client)
    url = f"/api/persons/{person['id']}/photo"
    assert person["photo"] == ""
    response = client.get(url, headers=ADMIN)
    assert response.status_code == 404 and response.json()["detail"] == "nessuna foto"
    assert client.get("/api/persons/999/photo", headers=ADMIN).status_code == 404

    # Senza il permesso di modificare le persone: niente foto nuova, niente foto tolta
    assert put_photo(client, person["id"], PNG, headers=VIEWER).status_code == 403
    assert put_photo(client, person["id"], PNG, headers=LOG_ONLY).status_code == 403
    # Il tipo dichiarato non conta: è un PNG
    response = put_photo(client, person["id"], PNG, "image/jpeg")
    assert response.status_code == 200, response.text
    version = response.json()["photo"]
    assert version
    assert client.delete(url, headers=VIEWER).status_code == 403

    # Chi vede le persone vede la foto; chi non le vede no
    assert client.get(url, headers=LOG_ONLY).status_code == 403
    response = client.get(url, params={"v": version}, headers=VIEWER)
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == "inline"
    # Con la versione nell'URL il browser la tiene, solo per sé; senza, o con una vecchia, la richiede
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    for params in ({}, {"v": "2020-01-01T00:00:00.000000+00:00"}):
        assert client.get(url, params=params, headers=VIEWER).headers["cache-control"] == "private, no-cache"

    # Elenco e scheda: solo la versione, mai i byte
    [listed] = client.get("/api/persons", headers=VIEWER).json()
    detail = client.get(f"/api/persons/{person['id']}", headers=VIEWER).json()
    assert listed["photo"] == detail["photo"] == version
    for payload in (listed, detail):
        text = json.dumps(payload)
        assert base64.b64encode(PNG[:30]).decode()[:20] not in text and "IHDR" not in text

    # Sostituita: un'altra versione, e il tipo nuovo
    replaced = put_photo(client, person["id"], JPEG, "image/png").json()["photo"]
    assert replaced > version
    response = client.get(url, params={"v": replaced}, headers=ADMIN)
    assert response.headers["content-type"] == "image/jpeg" and response.content == JPEG

    response = client.delete(url, headers=ADMIN)
    assert response.status_code == 200 and response.json()["photo"] == ""
    assert client.get(url, headers=ADMIN).status_code == 404
    # Tolta di nuovo: niente da togliere, nessuna riga in Attività
    assert client.delete(url, headers=ADMIN).status_code == 200

    audit = [(a["action"], a["detail"]) for a in client.get("/api/audit", headers=ADMIN).json()]
    photos = [row for row in audit if row[0].startswith("foto")]
    assert photos == [
        ("foto tolta", "Mario Rossi"),
        ("foto sostituita", "Mario Rossi"),
        ("foto aggiunta", "Mario Rossi"),
    ]


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (WEBP, "image/webp"),
        # Tutto il resto, anche dichiarato come JPEG
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', ""),
        (b"<?xml version='1.0'?><svg></svg>", ""),
        (b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>", ""),
        (b"GIF89a\x01\x00\x01\x00\x00\x00\x00;", ""),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", ""),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", ""),
        (b"\xff\xd8", ""),
        (b"", ""),
    ],
)
def test_format_comes_from_the_first_bytes(client, data, mime):
    person = new_person(client)
    response = put_photo(client, person["id"], data, "image/jpeg")
    if not mime:
        assert response.status_code == 400
        assert response.json()["detail"] == "formato della foto non supportato: usa JPEG, PNG o WebP"
        assert client.get(f"/api/persons/{person['id']}/photo", headers=ADMIN).status_code == 404
        return
    assert response.status_code == 200
    photo = client.get(f"/api/persons/{person['id']}/photo", headers=ADMIN)
    assert photo.headers["content-type"] == mime and photo.content == data


def test_photo_up_to_512_kb(client):
    person = new_person(client)
    largest = JPEG + b"\x00" * (PHOTO_MAX - len(JPEG))
    assert put_photo(client, person["id"], largest).status_code == 200
    for body in (largest + b"\x00", iter([largest[:300_000], largest[300_000:], b"\x00"])):
        # Dichiarata più grande, o mandata a pezzi senza la lunghezza: si ferma oltre il limite
        response = put_photo(client, person["id"], body)
        assert response.status_code == 413
        assert response.json()["detail"] == "foto troppo grande: al massimo 512 KB"
    photo = client.get(f"/api/persons/{person['id']}/photo", headers=ADMIN)
    assert photo.content == largest
    with pytest.raises(ServiceError, match="512 KB"):
        check_photo(largest + b"\x00")


def test_photo_goes_away_with_the_person(client):
    person = new_person(client)
    other = new_person(client, first_name="Anna", last_name="Bianchi", card="04bb")
    put_photo(client, person["id"], PNG)
    put_photo(client, other["id"], JPEG)
    db = client.app.state.db
    assert client.delete(f"/api/persons/{person['id']}", headers=ADMIN).status_code == 200
    rows = db._query("SELECT person_id FROM person_photos")
    assert rows == [{"person_id": other["id"]}]
    # Anche cancellata direttamente nel database: i vincoli sono attivi
    with db._lock:
        assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db._conn.execute("DELETE FROM persons WHERE id = ?", (other["id"],))
        db._conn.commit()
    assert db._query("SELECT person_id FROM person_photos") == []


def test_versions_for_the_list_come_from_the_index_without_the_photos(db):
    """L'elenco delle persone si chiede ogni 15 secondi: la versione delle foto si legge dall'indice, senza toccare le
    righe con i byte, e la versione di una persona sola senza scorrere la foto (updated_at prima dei byte)."""
    plan = db._query("EXPLAIN QUERY PLAN " + db_module.PHOTO_VERSIONS)
    assert "COVERING INDEX person_photos_version" in plan[0]["detail"]
    columns = [row["name"] for row in db._query("PRAGMA table_info(person_photos)")]
    assert columns == ["person_id", "mime", "updated_at", "data"]
    ids = [db.add_person(f"Persona {n}", None) for n in range(3)]
    db.set_person_photo(ids[0], "image/png", PNG)
    db.set_person_photo(ids[2], "image/jpeg", JPEG)
    versions = db.person_photo_versions()
    assert set(versions) == {ids[0], ids[2]}
    persons = {p["id"]: p for p in db.list_persons()}
    assert [persons[i]["photo"] for i in ids] == [versions[ids[0]], "", versions[ids[2]]]
    assert all(isinstance(p["photo"], str) for p in persons.values())


def test_a_photo_left_without_its_person_is_removed_at_startup(tmp_path):
    path = tmp_path / "foto.db"
    db = Database(path)
    person = db.add_person("Mario Rossi", None)
    db.set_person_photo(person, "image/png", PNG)
    with db._lock:
        db._conn.execute("PRAGMA foreign_keys = OFF")
        db._conn.execute("DELETE FROM persons")
        db._conn.commit()
    db.close()
    db = Database(path)
    try:
        assert db._query("SELECT * FROM person_photos") == []
        # Una persona rimessa con quel numero (ripristino da backup) non eredita foto
        db.add_person("Mario Rossi", None, person_id=person)
        assert db.get_person(person)["photo"] == ""
    finally:
        db.close()


def test_disk_full_leaves_no_photo_and_no_half_change(client):
    person = new_person(client)
    db = client.app.state.db
    before = db.get_person(person["id"])
    with db._lock:
        pages = db._conn.execute("PRAGMA page_count").fetchone()[0]
        # Il database non può crescere: SQLite risponde «database or disk is full» (SQLITE_FULL)
        db._conn.execute(f"PRAGMA max_page_count = {pages + 2}")
    big = JPEG + bytes(range(256)) * 1000
    response = put_photo(client, person["id"], big)
    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "spazio su disco esaurito: la modifica non è stata salvata, libera spazio e riprova"
    )
    with db._lock:
        db._conn.execute("PRAGMA max_page_count = 1073741823")
    # La scrittura dopo non salva niente della foto rimasta a metà
    client.patch(f"/api/persons/{person['id']}", json={"company": "Pulizie Srl"}, headers=ADMIN)
    after = db.get_person(person["id"])
    assert after["photo"] == "" and db._query("SELECT count(*) AS n FROM person_photos") == [{"n": 0}]
    assert after["company"] == "Pulizie Srl" and after["created_at"] == before["created_at"]
    assert "foto aggiunta" not in [a["action"] for a in client.get("/api/audit", headers=ADMIN).json()]


# Mai fuori dall'app


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    return Plant(db, controller, settings, tmp_path / "backup")


async def test_photo_stays_out_of_backups_and_restore_does_not_touch_it(plant, db):
    mario = await plant.access.create_person("", "04aa", "admin", first_name="Mario", last_name="Rossi")
    anna = await plant.access.create_person("", "", "admin", first_name="Anna", last_name="Bianchi")
    plant.access.set_person_photo(mario["id"], PNG, "admin")
    plant.access.set_person_photo(anna["id"], JPEG, "admin")
    backup = await plant.backup()
    raw = json.dumps(plant.backups.load(backup["id"]))
    assert "IHDR" not in raw and "JFIF" not in raw and "photo" not in raw
    assert base64.b64encode(PNG).decode()[:40] not in raw

    # Cambiata dopo il backup: il ripristino di Mario rimette la ditta e lascia la foto di adesso
    plant.access.set_person_photo(mario["id"], JPEG, "admin")
    version = db.get_person(mario["id"])["photo"]
    await plant.access.update_person(mario["id"], "admin", company="Altra Ditta")
    await plant.access.delete_person(anna["id"], "admin")
    _, result = await plant.restore(backup["id"], ["persone"])
    assert not result["failed"]
    restored = db.get_person(mario["id"])
    assert restored["company"] == "" and restored["photo"] == version
    assert db.get_person_photo(mario["id"])["data"] == JPEG
    # Eliminata e ricreata dal backup: senza foto
    again = db.get_person(anna["id"])
    assert again is not None and again["photo"] == "" and db.get_person_photo(anna["id"]) is None


async def test_photo_never_reaches_home_assistant_events_or_logs(db, controller, settings, monkeypatch, caplog):
    service = AccessService(db, controller, settings, EventMonitor(prefix="accessi/x", door_token="porta"))
    with caplog.at_level(logging.DEBUG):
        person = await service.create_person("", "04aa", "admin", first_name="Mario", last_name="Rossi")
        service.set_person_photo(person["id"], PNG, "admin")
        with pytest.raises(ServiceError):
            service.set_person_photo(person["id"], b"<svg>" + PNG, "admin")
        token = person["cards"][0]["axis_credential_token"]
        service.record_event(
            Event(
                "AccessControl",
                "AccessControl/AccessGranted/Credential/x",
                datetime(2026, 11, 13, 8, tzinfo=UTC),
                {"CredentialToken": token},
            )
        )
        bridge = HaBridge(settings, service.monitor, db)
        sent = []

        async def publish(topic, payload, retain=False):
            sent.append((topic, payload))
            return True

        monkeypatch.setattr(service.monitor, "publish", publish)
        for row in db.list_events():
            await bridge.publish_event(row)
        mqtt = FakeClient()
        await bridge.on_connect(mqtt)
    published = json.dumps([[t, str(p)] for t, p in sent] + [[t, str(p)] for t, p in mqtt.published.items()])
    assert "Mario Rossi" in published and "IHDR" not in published and "photo" not in published
    rows = db._query("SELECT * FROM events")
    assert rows and all("IHDR" not in json.dumps(row) and "photo" not in json.dumps(row) for row in rows)
    assert all("IHDR" not in json.dumps(item) for item in [controller.users, controller.credentials])
    assert "IHDR" not in caplog.text and "svg" not in caplog.text


def test_photo_bytes_never_in_the_log_even_when_refused(client, caplog):
    person = new_person(client)
    with caplog.at_level(logging.DEBUG):
        put_photo(client, person["id"], b"<svg>segreto</svg>", "image/svg+xml")
        put_photo(client, person["id"], JPEG + b"segreto" + b"\x00" * PHOTO_MAX)
        # Una richiesta sbagliata (numero di persona non valido) con una foto: nel log il motivo, non i byte
        client.put("/api/persons/abc/photo", content=PNG + b"segreto", headers=ADMIN)
    assert "segreto" not in caplog.text and "IHDR" not in caplog.text


def test_person_photo_is_not_an_event_snapshot(client):
    """Due cose diverse: la foto della persona sta nel database, le istantanee della telecamera nella cartella delle
    istantanee, con la loro pulizia. La foto della persona non passa di lì."""
    person = new_person(client)
    put_photo(client, person["id"], PNG)
    snapshots = client.app.state.ha.snapshot_dir
    assert not snapshots.exists() or not any(snapshots.iterdir())
    assert client.get("/api/events/1/snapshot", headers=ADMIN).status_code == 404


def test_set_person_photo_rolls_back_inside_the_lock(tmp_path, monkeypatch):
    """Con il disco pieno a metà (foto scritta, Ultima Modifica no) non resta niente: la transazione si annulla."""
    db = Database(tmp_path / "foto.db")
    try:
        person = db.add_person("Mario Rossi", None)
        before = db.get_person(person)["updated_at"]

        def full(ids):
            err = sqlite3.OperationalError("database or disk is full")
            err.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise err

        monkeypatch.setattr(db, "_touch_persons", full)
        with pytest.raises(sqlite3.OperationalError):
            db.set_person_photo(person, "image/png", PNG)
        monkeypatch.undo()
        db.set_setting("prova", "1")
        assert db.get_person_photo(person) is None and db.get_person(person)["updated_at"] == before
    finally:
        db.close()
