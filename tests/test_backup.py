import gzip
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.backup import COMMON, DEFAULT_POLICY, BackupError, BackupService, BackupStore, retention
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
    # Pagina mai dalla cache e versione nel riepilogo: dopo un aggiornamento la pagina si ricarica
    assert client.get("/", headers=mario).headers["cache-control"] == "no-store"
    assert client.get("/api/summary", headers=mario).json()["version"]
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


def _manifests(*moments, locked=()):
    """Backup finti, dal più recente, con l'ora locale indicata."""
    items = [
        {"id": f"b{n}", "created_at": moment.astimezone(UTC).isoformat(timespec="seconds"), "locked": n in locked}
        for n, moment in enumerate(moments)
    ]
    return sorted(items, key=lambda m: m["created_at"], reverse=True)


def test_retention_keeps_like_restic_forget():
    base = datetime(2026, 10, 25, 12, 0).astimezone()
    # Due al giorno per 40 giorni, dal più recente
    moments = [base - timedelta(days=d, hours=h) for d in range(40) for h in (0, 6)]
    items = _manifests(*moments, locked=(79,))
    policy = DEFAULT_POLICY | {"keep_last": 3, "keep_daily": 5, "keep_weekly": 2, "keep_monthly": 2, "keep_yearly": 1}
    reasons = retention(items, policy)
    kept = [m for m in items if reasons[m["id"]]]

    assert [reasons[m["id"]] for m in items[:3]] == [
        ["Ultimi", "Giornaliero", "Settimanale", "Mensile", "Annuale"],
        ["Ultimi"],
        ["Ultimi", "Giornaliero"],
    ]
    # Il più recente di ognuno degli ultimi 5 giorni
    daily = [m["created_at"][:10] for m in items if "Giornaliero" in reasons[m["id"]]]
    assert len(daily) == 5 and len(set(daily)) == 5
    # Settimana precedente e mese precedente: il più recente di ciascuno
    assert sum("Settimanale" in reasons[m["id"]] for m in items) == 2
    assert sum("Mensile" in reasons[m["id"]] for m in items) == 2
    # Il bloccato (il più vecchio) resta, gli altri senza motivo scadono
    assert reasons["b79"] == ["Bloccato"]
    # Ultimi 3, altri 3 giorni, la settimana e il mese precedenti, il bloccato
    assert len(kept) == 3 + 3 + 1 + 1 + 1
    # Ultimi è almeno 1: il backup più recente non scade mai
    assert retention(items, policy | {"keep_last": 0, "keep_daily": 0, "keep_weekly": 0})[items[0]["id"]]


async def test_expired_backups_are_removed_after_each_new_one(plant, db):
    await plant.backups.set_policy(
        {"keep_last": 2, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0}, "admin"
    )
    first = await plant.backup()
    await plant.backups.set_locked(first["id"], True, "admin")
    second = await plant.backup(name="secondo")
    await plant.access.create_person("Anna", "04aa", "admin")
    third = await plant.backup(name="terzo")
    await plant.access.create_person("Bruno", "04bb", "admin")
    fourth = await plant.backup(name="quarto")

    listing = plant.backups.listing()
    assert [m["id"] for m in listing["backups"]] == [fourth["id"], third["id"], first["id"]]
    assert listing["backups"][2]["keep"] == ["Bloccato"]
    assert second["id"] not in {m["id"] for m in listing["backups"]}
    # Le parti del secondo erano quelle del primo (bloccato): restano le due parti di ognuno dei tre
    assert len(list(plant.backups.store.parts.glob("*.json.gz"))) == 6


async def test_policy_that_removes_backups_needs_confirmation(plant):
    for name in ("uno", "due", "tre"):
        await plant.backup(name=name)
    rules = {"keep_last": 1, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0}

    answer = await plant.backups.set_policy(rules, "admin")
    assert answer == {"saved": False, "would_remove": 2, "policy": answer["policy"]}
    assert plant.backups.policy()["keep_last"] == 24
    assert len(plant.backups.store.list()) == 3

    answer = await plant.backups.set_policy(rules, "admin", confirm=True)
    assert answer["saved"] and answer["removed"] == 2
    assert [m["name"] for m in plant.backups.store.list()] == ["tre"]
    with pytest.raises(BackupError, match="orario"):
        await plant.backups.set_policy({"night": "25:00"}, "admin")


async def test_scheduled_backups(plant):
    now = datetime.now(tz=UTC)
    # Nessun backup: il notturno della notte scorsa è dovuto subito, e lontano dalla sua ora si chiama Automatico
    await plant.backups.set_policy({"night": (now - timedelta(minutes=5)).astimezone().strftime("%H:%M")}, "admin")
    first = await plant.backups.run_scheduled(plant.clients, now=now)
    assert first["kind"] == "notturno" and first["summary"] == "Primo backup"
    assert await plant.backups.run_scheduled(plant.clients, now=now) is None
    due = plant.backups.next_run(now)
    assert due["kind"] == "automatico"
    assert abs(
        datetime.fromisoformat(due["at"]) - (datetime.fromisoformat(first["created_at"]) + timedelta(hours=1))
    ) < timedelta(seconds=2)

    # Dopo un'ora senza modifiche: si controlla ma non si crea niente, e il prossimo controllo slitta
    later = now + timedelta(minutes=61)
    assert await plant.backups.run_scheduled(plant.clients, now=later) is None
    assert plant.backups.checked_at == later
    await plant.access.create_person("Anna", "04aa", "admin")
    changed = await plant.backups.run_scheduled(plant.clients, now=later + timedelta(minutes=61))
    assert changed["kind"] == "automatico" and changed["summary"].startswith("+1 persona")

    # Solo notturno: alla sua ora si fa anche se non è cambiato niente
    night = (now + timedelta(hours=3)).astimezone()
    await plant.backups.set_policy({"frequency": "notte", "night": night.strftime("%H:%M")}, "admin")
    assert await plant.backups.run_scheduled(plant.clients, now=now + timedelta(hours=2)) is None
    nightly = await plant.backups.run_scheduled(plant.clients, now=now + timedelta(hours=3, minutes=1))
    assert nightly["kind"] == "notturno" and nightly["summary"] == "Nessuna modifica"
    # Automatico spento: niente di pianificato
    await plant.backups.set_policy({"auto": False}, "admin")
    assert plant.backups.next_run(now) is None


async def test_backup_before_changes_once_per_session(plant, db):
    await plant.backup()
    await plant.access.create_person("Anna", "04aa", "admin")
    made = await plant.backups.before_change("prima_modifica", "admin", plant.clients)
    assert made["kind"] == "prima_modifica" and made["user"] == "admin"
    await plant.access.create_person("Bruno", "04bb", "admin")
    # Seconda modifica di seguito: niente backup
    assert await plant.backups.before_change("prima_modifica", "admin", plant.clients) is None
    # L'importazione lo fa sempre
    imported = await plant.backups.before_change("prima_importazione", "admin", plant.clients, window=False)
    assert imported["kind"] == "prima_importazione"
    plant.backups.change_checked_at -= timedelta(minutes=11)
    # Niente di nuovo dall'ultimo: niente backup
    assert await plant.backups.before_change("prima_modifica", "admin", plant.clients) is None
    await plant.backups.set_policy({"before_changes": False}, "admin")
    plant.backups.change_checked_at = None
    await plant.access.create_person("Carla", "04cc", "admin")
    assert await plant.backups.before_change("prima_modifica", "admin", plant.clients) is None
    # Nell'attività solo i backup chiesti da una persona
    assert [a["action"] for a in db.list_audit()].count("backup creato") == 1


async def test_backup_after_add_on_update(plant, db, tmp_path):
    await plant.backup()
    updated = BackupService(db, tmp_path / "backup", "0.20.0")
    made = await updated.after_update(plant.clients)
    assert made["kind"] == "aggiornamento" and made["name"] == "Versione 0.20.0"
    assert await updated.after_update(plant.clients) is None


async def test_offline_controller_is_not_waited(plant, controller):
    first = await plant.backup()
    controller.offline = True
    manifest = await plant.backups.create(
        "automatico", "", "", plant.clients, offline={plant.controller_id: "non raggiungibile (timeout)"}
    )
    key = f"controller:{plant.controller_id}"
    assert manifest["parts"][key]["stale"] == first["id"]
    assert manifest["errors"] == ["A1210: non raggiungibile (timeout)"]


async def test_daily_verification_finds_damaged_backups(plant, db):
    first = await plant.backup()
    await plant.access.create_person("Anna", "04aa", "admin")
    await plant.backup()
    assert plant.backups.verify_due()
    result = await plant.backups.verify_now()
    assert result["ok"] and result["backups"] == 2 and result["parts"] == 4
    assert not plant.backups.verify_due()

    path = plant.backups.store.parts / f"{first['parts'][COMMON]['hash']}.json.gz"
    path.write_bytes(b"rovinato")
    (plant.backups.store.root / "20260101-000000.json").write_text("{")
    result = await plant.backups.verify_now()
    assert not result["ok"] and result["damaged"] == 2
    assert result["errors"][0] == "indice 20260101-000000.json illeggibile"
    items = NotificationService(db).collect(
        mqtt_configured=False,
        mqtt_connected=False,
        controller_configured=True,
        controller_online=None,
        sync={},
        door_names={},
        backup_verify=plant.backups.last_verify(),
    )
    assert [(i["title"], i["text"]) for i in items] == [
        ("Backup Rovinati", "2 backup non si possono ripristinare: file mancanti o rovinati")
    ]


def test_backup_before_programming_and_policy_api(client):
    admin = {"X-Remote-User-Name": "rising", "X-Remote-User-Display-Name": "Rising"}
    client.get("/api/me", headers=admin)
    client.post("/api/users", json={"username": "mario", "perms": ["backup", "regole"]}, headers=admin)
    mario = {"X-Remote-User-Name": "mario"}
    # Senza permesso la modifica è rifiutata prima del backup
    assert client.post("/api/holidays", json={"day": "2026-12-25"}, headers=mario).status_code == 403
    assert client.get("/api/backups", headers=admin).json()["backups"] == []

    assert client.post("/api/holidays", json={"day": "2026-12-25"}, headers=admin).status_code == 200
    listing = client.get("/api/backups", headers=admin).json()
    assert [b["kind_label"] for b in listing["backups"]] == ["Prima di una Modifica"]
    assert listing["policy"]["keep_daily"] == 14 and listing["next"]["kind"] in ("notturno", "automatico")
    assert listing["backups"][0]["keep"][0] == "Ultimi"

    # Impostazioni: solo amministratori; regole che eliminano backup chiedono conferma
    rules = {"keep_last": 1, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0}
    assert client.put("/api/backups/policy", json=rules, headers=mario).status_code == 403
    assert client.put("/api/backups/policy", json={"night": "3"}, headers=admin).status_code == 422
    assert client.put("/api/backups/policy", json={"keep_last": 0}, headers=admin).status_code == 422
    assert client.post("/api/backups", json={}, headers=admin).status_code == 200
    answer = client.put("/api/backups/policy", json=rules, headers=admin).json()
    assert answer["saved"] is False and answer["would_remove"] == 1
    answer = client.put("/api/backups/policy", json=rules | {"confirm": True}, headers=admin).json()
    assert answer["saved"] and answer["removed"] == 1
    assert client.post("/api/backups/verify", headers=mario).json()["ok"]
    # Riquadro del cruscotto
    board = client.get("/api/dashboard", headers=admin).json()["backup"]
    assert board["count"] == 1 and board["last"]["kind_label"] == "Manuale" and board["verify"]["ok"]
    assert (
        client.put(
            "/api/dashboard/layout", json={"widgets": [{"type": "backup", "size": 2}]}, headers=admin
        ).status_code
        == 200
    )


def test_backup_loop_makes_the_first_backup(settings, controller, tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(
        settings, allow_any_client=True, controller=controller, db_path=tmp_path / "loop.db", scheduled_backups=True
    )
    with TestClient(app) as client:
        listing = {}
        for _ in range(50):
            listing = client.get("/api/backups").json()
            # Il primo backup e subito dopo la prima verifica
            if listing["verify"]:
                break
            time.sleep(0.1)
        # Il notturno della notte scorsa, recuperato ora: si chiama Automatico (salvo prove fatte alle 3)
        assert [b["kind_label"] for b in listing["backups"]] in (["Automatico"], ["Notturno"])
        assert listing["verify"]["ok"] and listing["verify"]["backups"] == 1


async def test_late_nightly_is_called_automatic(plant):
    now = datetime.now(tz=UTC)
    await plant.backups.set_policy({"night": (now - timedelta(hours=2)).astimezone().strftime("%H:%M")}, "admin")
    made = await plant.backups.run_scheduled(plant.clients, now=now)
    assert made["kind"] == "automatico" and made["summary"] == "Primo backup"
