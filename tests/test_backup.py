import gzip

import pytest
from fastapi.testclient import TestClient

from app.backup import COMMON, BackupError, BackupService, BackupStore
from app.main import create_app
from app.notifications import NotificationService
from app.programming import ProgrammingService
from app.restore import Runtime, apply, plan
from app.rules_service import RulesService
from app.service import AccessService

DOOR = {
    "snapshot_camera": "",
    "snapshot_on_granted": 0,
    "ha_buttons": 1,
    "intercom_name": "",
    "intercom_open_door": 0,
    "intercom_relay": 1,
    "intercom_mqtt_prefix": "hmd",
}


class Plant:
    """Impianto di prova: un controller con due porte, i servizi e i backup in una cartella temporanea."""

    def __init__(self, db, controller, settings, root):
        fields = {f: "" for f in db.CONTROLLER_FIELDS}
        fields |= {"name": "A1210", "host": "a1210.local", "username": "root", "password": "segreta", "verify_ssl": 0}
        self.controller_id = db.save_controller(None, fields)
        self.site = db.save_site(None, "Teatro", "")
        self.area = db.save_area(None, self.site, "Scale")
        self.scala = db.save_door(None, self.controller_id, DOOR | {"door_token": "porta", "name": "Porta Scala"})
        self.db, self.controller = db, controller
        self.access = AccessService(db, controller, settings)
        self.rules = RulesService(self.access)
        self.programming = ProgrammingService(db, controller, self.controller_id, self.access.lock)
        self.backups = BackupService(db, root, "0.19.0")

        async def reader_configurations():
            return []

        # Come l'A1210 del banco, che le configurazioni dei lettori le restituisce
        controller.list_idpoint_configurations = reader_configurations

    @property
    def clients(self):
        return {self.controller_id: self.controller}

    async def backup(self, kind="manuale", name="", skip=False):
        return await self.backups.create(kind, name, "admin", self.clients, skip_unchanged=skip)

    async def restore(self, backup_id, sections=("tipologie", "persone", "porte"), keys=None):
        """Anteprima e ripristino; keys None = le righe spuntate di serie."""
        _, saved = self.backups.load(backup_id)
        current, _ = await self.backups.snapshot(self.clients)
        items = plan(saved, current, list(sections), self.controller_id)
        chosen = [i["key"] for i in items if i["default"]] if keys is None else keys
        runtime = Runtime(self.db, self.access, self.rules, self.programming, self.controller_id)
        return items, await apply(items, chosen, runtime, "admin")


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    return Plant(db, controller, settings, tmp_path / "backup")


async def test_unchanged_parts_are_reused(plant):
    first = await plant.backup()
    assert first["summary"] == "Primo backup" and set(first["parts"]) == {COMMON, f"controller:{plant.controller_id}"}

    # Automatico senza modifiche: non si crea; manuale sì, ma riusa le stesse parti
    assert await plant.backup("automatico", skip=True) is None
    second = await plant.backup(name="Prima dei lavori")
    assert second["summary"] == "Nessuna modifica" and second["name"] == "Prima dei lavori"
    assert {k: v["hash"] for k, v in second["parts"].items()} == {k: v["hash"] for k, v in first["parts"].items()}
    assert len(list(plant.backups.store.parts.glob("*.json.gz"))) == 2
    assert [m["id"] for m in plant.backups.listing()["backups"]] == [second["id"], first["id"]]


async def test_summary_lists_what_changed(plant, controller):
    await plant.backup()
    await plant.access.create_person("Anna", "04aa", "admin")
    await plant.programming.update_door("porta", {"access_time": 9}, "admin")

    manifest = await plant.backup()

    assert manifest["summary"] == "+1 persona, +1 tessera, Porta Scala modificata"
    controller_part = manifest["parts"][f"controller:{plant.controller_id}"]
    assert controller_part["summary"] == "Porta Scala modificata"
    assert manifest["parts"][COMMON]["counts"]["persone"] == 1
    diff = await plant.backups.compare(manifest["id"], "precedente", plant.clients)
    door = next(g for g in diff["groups"] if g["part"] != COMMON)["sections"][0]["changed"][0]
    assert door["fields"] == [{"field": "AccessTime", "label": "Tempo di Apertura", "old": "7 s", "new": "9 s"}]


async def test_password_and_volatile_fields_are_not_saved(plant, db):
    await plant.access.create_person("Anna", "04aa", "admin")
    db.touch_app_user("admin", "Admin")
    manifest = await plant.backup()
    _, parts = plant.backups.load(manifest["id"])
    controller_part = parts[f"controller:{plant.controller_id}"]
    assert "password" not in controller_part["controller"] and controller_part["controller"]["host"] == "a1210.local"
    assert "segreta" not in str(parts)
    assert "axis_credential_token" not in str(parts[COMMON]) and "last_seen" not in str(parts[COMMON])
    # Riferimenti alle tessere sul controller, non copie della persona
    assert controller_part["credentials"][0]["card"] == "04aa"


async def test_damaged_part_is_detected(plant):
    manifest = await plant.backup()
    digest = manifest["parts"][COMMON]["hash"]
    path = plant.backups.store.parts / f"{digest}.json.gz"
    path.write_bytes(gzip.compress(b'{"persons": []}'))
    with pytest.raises(BackupError, match="rovinato"):
        plant.backups.load(manifest["id"])


async def test_unreachable_controller_makes_an_incomplete_backup(plant, controller, db):
    first = await plant.backup()
    controller.offline = True
    manifest = await plant.backup()

    key = f"controller:{plant.controller_id}"
    assert manifest["parts"][key]["stale"] == first["id"]
    assert manifest["parts"][key]["hash"] == first["parts"][key]["hash"]
    assert "non risponde" in manifest["errors"][0]
    error = plant.backups.last_error()
    items = NotificationService(db).collect(
        mqtt_configured=False,
        mqtt_connected=False,
        controller_configured=True,
        controller_online=None,
        sync={},
        door_names={},
        backup=error,
    )
    assert [(i["title"], i["link"]) for i in items] == [("Backup Incompleto", "#impianti/backup")]

    controller.offline = False
    await plant.backup()
    assert plant.backups.last_error() is None


async def test_locked_backup_is_kept_and_unused_parts_are_removed(plant):
    first = await plant.backup()
    await plant.access.create_person("Anna", "04aa", "admin")
    second = await plant.backup()
    await plant.backups.set_locked(first["id"], True, "admin")
    with pytest.raises(BackupError, match="bloccato"):
        await plant.backups.delete(first["id"], "admin")

    await plant.backups.delete(second["id"], "admin")

    assert [m["id"] for m in plant.backups.listing()["backups"]] == [first["id"]]
    assert len(list(plant.backups.store.parts.glob("*.json.gz"))) == 2


def test_store_rejects_bad_ids(tmp_path):
    store = BackupStore(tmp_path)
    with pytest.raises(BackupError):
        store.get("../../etc/passwd")
    with pytest.raises(BackupError):
        store.read_part("../x")


async def test_restore_deleted_person_with_same_number_and_state(plant, controller, db):
    anna = await plant.access.create_person("Anna", "04aa", "admin", company="Pulizie Srl", notes="chiavi")
    anna = await plant.access.set_card_enabled(anna["cards"][0]["id"], False, "admin")
    backup = await plant.backup()
    await plant.access.delete_person(anna["id"], "admin")

    items, result = await plant.restore(backup["id"], ["persone"])

    assert [(i["action"], i["label"]) for i in items] == [("ricrea", "Anna")]
    assert result["applied"] == [{"label": "Anna", "text": "Ricrea la persona"}] and not result["failed"]
    restored = db.get_person(anna["id"])
    assert restored["created_at"] == anna["created_at"] and restored["company"] == "Pulizie Srl"
    card = restored["cards"][0]
    assert card["number"] == "04aa" and card["enabled"] is False
    credential = controller.credentials[card["axis_credential_token"]]
    assert credential["Enabled"] is False and credential["UserToken"] in controller.users
    # Di nuovo uguale al backup: niente più da ripristinare
    items, _ = await plant.restore(backup["id"], ["persone"])
    assert items == []


async def test_restore_changed_person_and_leave_new_ones(plant, controller, db):
    anna = await plant.access.create_person("Anna", "04aa", "admin")
    backup = await plant.backup()
    await plant.access.update_person(anna["id"], "admin", name="Anna Rossi", company="Teatro")
    await plant.access.add_card(anna["id"], "04bb", "admin")
    await plant.access.set_card_enabled(anna["cards"][0]["id"], False, "admin")
    bruno = await plant.access.create_person("Bruno", "04cc", "admin")

    items, result = await plant.restore(backup["id"], ["persone"])

    by_label = {i["label"]: i for i in items}
    assert by_label["Anna"]["action"] == "riporta" and by_label["Anna"]["default"] is True
    labels = [c["label"] for c in by_label["Anna"]["changes"]]
    assert labels == ["Nome", "Ditta o Reparto", "Tessera 04aa: stato", "Tessera 04bb"]
    assert by_label["Bruno"]["action"] == "togli" and by_label["Bruno"]["default"] is False
    assert not result["failed"]
    restored = db.get_person(anna["id"])
    assert restored["name"] == "Anna" and restored["company"] == ""
    assert [(c["number"], c["enabled"]) for c in restored["cards"]] == [("04aa", True)]
    assert controller.users[restored["axis_user_token"]]["Name"] == "Anna"
    assert db.get_person(bruno["id"]) is not None


async def test_card_now_owned_by_someone_else_stays(plant, db):
    anna = await plant.access.create_person("Anna", "04aa", "admin")
    await plant.access.create_person("Carlo", "04cc", "admin")
    backup = await plant.backup()
    await plant.access.delete_person(anna["id"], "admin")
    await plant.access.create_person("Bruno", "04aa", "admin")

    items, result = await plant.restore(backup["id"], ["persone"])

    anna_item = next(i for i in items if i["label"] == "Anna")
    assert anna_item["blocked"] == "tutte le sue tessere ora sono di altre persone"
    assert "ora è di Bruno" in anna_item["warnings"][0]
    assert result["applied"] == [] and db.card_by_number("04aa")["person_name"] == "Bruno"


async def test_restore_deleted_category_and_its_persons(plant, controller, db):
    rule = {"target": "door", "target_id": plant.scala, "schedule": "standard_always"}
    category = await plant.rules.save_category(None, {"name": "Pulizie", "color": "teal", "rules": [rule]}, "admin")
    anna = await plant.access.create_person("Anna", "04aa", "admin", category_id=category["id"])
    backup = await plant.backup()
    await plant.access.delete_person(anna["id"], "admin")
    await plant.rules.delete_category(category["id"], "admin")
    assert category["token"] not in controller.profiles

    items, result = await plant.restore(backup["id"], ["tipologie", "persone"])

    assert [(i["section"], i["action"]) for i in items] == [("tipologie", "ricrea"), ("persone", "ricrea")]
    assert not result["failed"]
    restored = db.get_category(category["id"])
    assert restored["name"] == "Pulizie" and restored["group_token"] in controller.profiles
    person = db.get_person(anna["id"])
    assert person["category_id"] == category["id"]
    assert person["cards"][0]["access_profile"] == restored["group_token"]


async def test_category_created_after_backup_is_offered_but_not_ticked(plant):
    backup = await plant.backup()
    rule = {"target": "door", "target_id": plant.scala, "schedule": "standard_always"}
    await plant.rules.save_category(None, {"name": "Ospite", "rules": [rule]}, "admin")

    items, result = await plant.restore(backup["id"], ["tipologie"])

    assert [(i["action"], i["default"], i["label"]) for i in items] == [("togli", False, "Ospite")]
    assert result["applied"] == []


async def test_restore_door_times_and_app_settings(plant, controller, db):
    backup = await plant.backup()
    await plant.programming.update_door("porta", {"name": "Scala", "access_time": 12}, "admin")
    door = db.get_door(plant.scala)
    db.save_door(plant.scala, plant.controller_id, {**door, "snapshot_camera": "camera.scala"})
    await plant.programming.update_reader("lettore-1", {"name": "Lettore Nuovo"}, "admin")

    items, result = await plant.restore(backup["id"], ["porte"])

    assert {i["key"] for i in items} == {"porta:porta", "lettore:lettore-1"}
    assert not result["failed"] and result["reload"] is True
    assert controller.doors["porta"]["AccessTime"] == "PT7S" and controller.doors["porta"]["Name"] == "Ingresso scala"
    assert controller.idpoints["lettore-1"]["Name"] == "Lettore esterno"
    assert db.get_door(plant.scala)["snapshot_camera"] == ""
    # Come una modifica a mano: si può annullare dallo storico del controller
    assert db.list_controller_changes(plant.controller_id)[0]["summary"] == "lettore Lettore esterno"


async def test_door_time_over_lock_limit_is_not_restored(plant, controller):
    controller.doors["porta"]["AccessTime"] = "PT45S"
    backup = await plant.backup()
    await plant.programming.update_door("porta", {"access_time": 10}, "admin")

    items, result = await plant.restore(backup["id"], ["porte"])

    assert "fuori dai limiti della serratura" in items[0]["warnings"][0]
    assert controller.doors["porta"]["AccessTime"] == "PT10S"


async def test_door_missing_on_controller_is_blocked(plant, controller):
    backup = await plant.backup()
    controller.doors.pop("porta2")

    items, _ = await plant.restore(backup["id"], ["porte"])

    assert items[0]["key"] == "porta:porta2" and "ripristino completo" in items[0]["blocked"]


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    with TestClient(app) as test_client:
        yield test_client


def test_backup_api_permissions_and_restore(client, controller):
    admin = {"X-Remote-User-Name": "rising", "X-Remote-User-Display-Name": "Rising"}
    client.get("/api/me", headers=admin)
    assert client.post("/api/users", json={"username": "mario", "perms": ["backup"]}, headers=admin).status_code == 200
    mario = {"X-Remote-User-Name": "mario"}
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=admin).json()

    created = client.post("/api/backups", json={"name": "Prima dei lavori"}, headers=mario).json()
    listing = client.get("/api/backups", headers=mario).json()
    assert listing["backups"][0]["name"] == "Prima dei lavori" and listing["can_restore"] is False
    assert client.put(f"/api/backups/{created['id']}/lock", json={"locked": True}, headers=mario).json()["locked"]
    # Ripristinare ed eliminare: solo gli amministratori
    assert client.post(f"/api/backups/{created['id']}/preview", json={}, headers=mario).status_code == 403
    assert client.delete(f"/api/backups/{created['id']}", headers=mario).status_code == 403
    assert client.delete(f"/api/backups/{created['id']}", headers=admin).status_code == 400

    client.delete(f"/api/persons/{person['id']}", headers=admin)
    compare = client.get(f"/api/backups/{created['id']}/compare?other=attuale", headers=mario).json()
    assert compare["newer"]["kind_label"] == "Stato Attuale"
    assert compare["groups"][0]["sections"][0]["removed"][0]["label"] == "Anna"

    preview = client.post(f"/api/backups/{created['id']}/preview", json={"sections": ["persone"]}, headers=admin).json()
    keys = [i["key"] for i in preview["items"] if i["default"]]
    assert len(keys) == 1 and "_person" not in preview["items"][0]
    result = client.post(
        f"/api/backups/{created['id']}/restore", json={"sections": ["persone"], "keys": keys}, headers=admin
    ).json()
    assert result["applied"][0]["label"] == "Anna"
    assert client.get("/api/persons", headers=admin).json()[0]["cards"][0]["number"] == "04aa"
    backups = client.get("/api/backups", headers=admin).json()["backups"]
    assert backups[0]["id"] == result["backup_before"] and backups[0]["kind_label"] == "Prima del Ripristino"
    # Con la data del backup di partenza: niente catene di nomi
    assert backups[0]["name"].startswith("Prima del ripristino del backup del ")
    assert "Prima dei lavori" not in backups[0]["name"]
    actions = [a["action"] for a in client.get("/api/audit", headers=admin).json()]
    assert "ripristino da backup" in actions and "persona ricreata da backup" in actions


def test_controller_counts_readers_apart_from_inputs():
    from app.backup import part_counts

    # Collaudo 0.19: un lettore e due ingressi contavano "3 lettori"
    part = {
        "idpoints": [{"token": "lettore-1"}, {"token": "rex-1"}, {"token": "rele-1"}],
        "app_inputs": [
            {"io": "I0", "role": "uscita", "idpoint_token": "rex-1"},
            {"io": "I1", "role": "citofono", "idpoint_token": "rele-1"},
            {"io": "I2", "role": "", "idpoint_token": ""},
        ],
    }
    counts = part_counts("controller:1", part)
    assert counts["lettori"] == 1 and counts["ingressi"] == 2
