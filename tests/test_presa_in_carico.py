"""Presa in carico in memoria: impianto come al banco → presa in carico → stato voluto → confronto senza righe, e i
casi che il confronto deve mostrare (vedi presa_in_carico.py). Mai una scrittura sul controller."""

import pytest
from test_backup import Plant
from test_full_restore import bench_like
from test_rules_service import OFFICE

from app.compare import compare, read_strict
from app.desired import build, load_conf
from app.presa_in_carico import confronto, presa_in_carico
from app.rules import schedule_ical


def door_rule(plant, schedule="standard_always"):
    return {"target": "door", "target_id": plant.scala, "schedule": schedule}


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    """L'A1210 del banco con l'app che ha fatto tutto: tipologia predefinita, persone, un relè del citofono, una fascia
    con le festività. Il pulsante fatto a mano del banco resta sul controller, senza morsetto: è un lettore e un punto
    opachi. Nessun profilo esterno."""
    plant = Plant(db, controller, settings, tmp_path / "backup")
    bench_like(controller)
    return plant


async def app_made(plant):
    controller = plant.controller
    controller.profiles.pop("profilo_sempre")
    category = await plant.rules.save_category(None, {"name": "Personale", "rules": [door_rule(plant)]}, "admin")
    plant.rules.set_default_category(category["id"], "admin")
    anna = await plant.access.create_person("Anna", "04AA", "admin")
    await plant.access.create_person("Bruno", "04bb", "admin", valid_to="2030-01-01T00:00:00+02:00")
    await plant.access.set_card_enabled(anna["cards"][0]["id"], False, "admin")
    await plant.programming.update_input("REX", {"name": "Relè Citofono", "role": "intercom", "door": "porta"}, "a")
    await plant.rules.create_schedule("Ufficio", "Orario del personale", OFFICE, True, "admin")
    await plant.rules.add_holidays([{"day": "2026-12-25", "name": "Natale", "yearly": True}], "admin")
    return category


async def compared(plant, notes=False):
    controller = plant.controller
    calls = list(controller.calls)
    letto = await read_strict(controller)
    conf = load_conf(plant.db, plant.access.settings.access_profile)
    adopted, found = presa_in_carico(conf, letto, plant.controller_id)
    result = compare(build(adopted, plant.controller_id), letto, found if notes else None)
    assert controller.calls == calls
    return result, adopted, found


def changed(result):
    """Righe con qualcosa da scrivere o da togliere: non «uguale» né «prende» (lo prende com'è)."""
    return [r for r in result["rows"] if r["action"] not in ("uguale", "prende")]


async def test_bench_taken_in_charge_has_no_differences(plant, db):
    await app_made(plant)

    result, adopted, notes = await compared(plant)

    assert changed(result) == [] and result["unread"] == []
    # Tutto preso in carico: le porte del controller, anche Cantina che l'app non mostrava, lettori e punti
    doors = {d["door_token"]: d for d in adopted["doors"]}
    assert doors["porta"]["name"] == "Ingresso scala" and doors["porta"]["access_time"] == 7
    assert doors["porta2"]["id"] < 0 and doors["porta2"]["access_time"] == 5
    assert doors["porta"]["lock_config"] == {"Lock.Type": "FailSecure"}
    assert {r["token"] for r in adopted["readers"]} == {"lettore-1", "lettore-2", "Axis-b8a44fe6a074:1.1"}
    assert {p["token"] for p in adopted["access_points"]} == {"ap-in", "Axis-b8a44fe6a074:1.2"}
    assert next(n for n in notes if n["key"] == "porta")["warnings"] == [
        "nell'app si chiamava Porta Scala: prende il nome del controller"
    ]
    # Nella 0.28 la presa in carico non si salva
    assert load_conf(db)["doors"][0]["name"] == "Porta Scala" and "readers" not in load_conf(db)
    # Con le note, quello che l'app prende è «prende»; niente da riportare, ricreare o togliere
    full = confronto(load_conf(db, "profilo_sempre"), await read_strict(plant.controller), plant.controller_id)
    actions = {r["action"] for r in full["rows"]}
    assert actions <= {"prende", "uguale"} and "prende" in actions
    assert {r["key"] for r in full["rows"] if r["action"] == "uguale" and r["kind"] == "tessera"} == {
        f"tessera:{c['axis_credential_token']}" for c in db.all_cards()
    }


async def test_hand_made_button_becomes_an_input(plant, controller):
    """Il pulsante del banco (relè del citofono sul REX) fatto con la procedura guidata: diventa l'ingresso REX
    dell'app con i suoi token, e prende il permesso di aprire che oggi non ha."""
    category = await plant.rules.save_category(None, {"name": "Personale", "rules": [door_rule(plant)]}, "admin")
    plant.rules.set_default_category(category["id"], "admin")
    controller.profiles.pop("profilo_sempre")

    result, adopted, notes = await compared(plant, notes=True)

    row = adopted["inputs"][plant.controller_id]["REX"]
    assert (row["role"], row["door_token"], row["contact"]) == ("rex", "porta", "no")
    assert (row["idpoint_token"], row["access_point_token"]) == ("Axis-b8a44fe6a074:1.1", "Axis-b8a44fe6a074:1.2")
    rex = adopted["controllers"][0]["rex_profile"]
    assert rex.startswith("aam_pulsanti_1_")
    rows = {r["key"]: r for r in changed(result)}
    # Riscritti come li scrive l'app: descrizione, attesa e configurazione del punto di identificazione
    assert {k: r["action"] for k, r in rows.items()} == {
        "lettore:Axis-b8a44fe6a074:1.1": "riporta",
        "config_lettore:Axis-b8a44fe6a074:1.1": "riporta",
        "punto:Axis-b8a44fe6a074:1.2": "riporta",
        f"pulsanti:{rex}": "ricrea",
    }
    assert all(rows[k]["surprises"] == ["pulsante fatto a mano sul controller"] for k in rows if k != f"pulsanti:{rex}")
    morsetto = next(r for r in result["rows"] if r["key"] == "morsetto:REX")
    assert morsetto["action"] == "prende" and morsetto["surprises"] == ["pulsante fatto a mano sul controller"]
    # Il permesso di aprire che oggi manca
    [policy] = build(adopted, plant.controller_id)["objects"]["pulsanti"][rex]["AccessPolicy"]
    assert policy["AccessPoint"] == "Axis-b8a44fe6a074:1.2"


async def test_external_profile_becomes_a_category_with_the_same_token(plant, controller, db):
    """Una tessera con il profilo fatto a mano sul sito Axis (Accesso Personalizzato): la persona passa alla tipologia
    nuova con lo stesso token, e nessuna tessera cambia accesso."""
    await app_made(plant)
    controller.profiles["esterno"] = {
        "token": "esterno",
        "Name": "Impresa Pulizie",
        "Description": "",
        "Enabled": True,
        "Schedule": ["standard_always"],
        "AccessPolicy": [
            {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Attribute": [], "Schedule": ["standard_always"]}
        ],
    }
    carla = await plant.access.create_person("Carla", "04cc", "admin", group="esterno")

    result, adopted, notes = await compared(plant, notes=True)

    category = next(c for c in adopted["categories"] if c["group_token"] == "esterno")
    assert category["name"] == "Impresa Pulizie" and category["rules"] == [door_rule(plant)]
    assert next(p for p in adopted["persons"] if p["id"] == carla["id"])["category_id"] == category["id"]
    rows = changed(result)
    assert [r["kind"] for r in rows] == ["tipologia"]
    # Cambia solo la descrizione del profilo: chi entra dove resta com'è
    assert [c["label"] for c in rows[0]["changes"]] == ["Descrizione"]
    assert "profilo esterno usato da tessere dell'app" in rows[0]["surprises"]
    assert db.get_person(carla["id"])["category_id"] != category["id"]


async def test_factory_schedule_in_use_becomes_an_app_schedule(plant, controller):
    await app_made(plant)
    controller.schedules["standard_office_hours"]["ScheduleDefinition"] = schedule_ical(OFFICE)
    rules = [door_rule(plant, "standard_office_hours")]
    await plant.rules.save_category(None, {"name": "Uffici", "rules": rules}, "admin")

    result, adopted, notes = await compared(plant, notes=True)

    new = next(s for s in adopted["schedules"] if s["name"] == "Orario d'Ufficio")
    assert new["token"].startswith("aam_fascia_1_") and new["rules"] == OFFICE
    category = next(c for c in adopted["categories"] if c["name"] == "Uffici")
    assert category["rules"] == [door_rule(plant, new["token"])]
    rows = {r["key"]: r for r in changed(result)}
    assert rows[f"fascia:{new['token']}"]["action"] == "ricrea"
    profile = rows[f"tipologia:{category['group_token']}"]
    assert profile["action"] == "riporta" and {c["label"] for c in profile["changes"]} == {
        "Fascia del Profilo",
        "Regole",
    }
    # La fascia di fabbrica resta com'è: mai tra quelle da togliere
    assert set(rows) == {f"fascia:{new['token']}", f"tipologia:{category['group_token']}"}


async def test_door_time_over_the_lock_limit_is_brought_back(plant, controller):
    await app_made(plant)
    controller.doors["porta"]["AccessTime"] = "PT90S"

    result, adopted, notes = await compared(plant, notes=True)

    assert next(d for d in adopted["doors"] if d["door_token"] == "porta")["access_time"] == 30
    [row] = changed(result)
    assert (row["kind"], row["action"]) == ("porta", "riporta")
    assert row["changes"] == [{"label": "Tempo di Apertura", "old": "90 s", "new": "30 s"}]
    assert "tempi della porta oltre i limiti della serratura" in row["surprises"]
    assert "Tempo di Apertura: 90 s è fuori dai limiti della serratura (1-30 s), si scrive 30 s" in row["warnings"]


async def test_scheduled_unlock_is_removed(plant, controller):
    await app_made(plant)
    controller.door_configs["porta"]["DoorScheduleConfiguration"] = {"ScheduleToken": ["standard_office_hours"]}

    result, adopted, notes = await compared(plant)

    [row] = changed(result)
    assert (row["kind"], row["action"], row["token"]) == ("sblocco", "togli", "porta")
    assert row["surprises"] == ["sblocco programmato della porta (DoorSchedule)"]
    assert "DoorScheduleConfiguration" not in str(adopted["doors"])


async def test_custom_access_and_shared_profiles(plant, controller, db):
    """Accesso Personalizzato: la persona passa alla tipologia che usa già il profilo delle sue tessere. Profilo in
    comune tra due tipologie: la prima tiene il token, l'altra ne riceve uno suo con le stesse righe."""
    personale = await app_made(plant)
    ospiti = await plant.rules.save_category(None, {"name": "Ospiti", "rules": [door_rule(plant)]}, "admin")
    dario = await plant.access.create_person(
        "Dario", "04dd", "admin", group=personale["token"], category_id=ospiti["id"]
    )
    fields = {"name": "Vecchia", "color": "gray", "group_token": personale["token"], "valid_days": 0}
    vecchia = db.save_category(None, {**fields, "description": ""})
    db.set_category_rules(vecchia, [door_rule(plant)])
    elio = await plant.access.create_person("Elio", "04ee", "admin", category_id=vecchia)

    result, adopted, notes = await compared(plant, notes=True)

    persons = {p["id"]: p for p in adopted["persons"]}
    assert persons[dario["id"]]["category_id"] == personale["id"]
    split = next(c for c in adopted["categories"] if c["id"] == vecchia)
    assert split["group_token"].startswith("aam_tipologia_1_")
    assert persons[elio["id"]]["cards"][0]["access_profile"] == split["group_token"]
    rows = {r["key"]: r for r in changed(result)}
    elio_card = db.get_person(elio["id"])["cards"][0]["axis_credential_token"]
    assert {k: r["action"] for k, r in rows.items()} == {
        f"tipologia:{split['group_token']}": "ricrea",
        f"tessera:{elio_card}": "riporta",
    }
    assert rows[f"tipologia:{split['group_token']}"]["surprises"] == ["profilo in comune tra più tipologie"]
    # Stesse righe, stesse porte: cambia solo il profilo citato dalla tessera
    assert [c["label"] for c in rows[f"tessera:{elio_card}"]["changes"]] == ["Accesso"]
    assert any(n["kind"] == "persona" and n["key"] == str(dario["id"]) for n in notes)


async def test_old_groups_become_categories_in_creation_order(plant, controller, db):
    """adopt_groups, spostato in presa_in_carico: le tipologie nuove nascono nell'ordine delle persone, con il colore e
    il nome di sempre, e il database dà loro i numeri nello stesso ordine."""
    db.set_setting("default_group", "profilo_sempre")
    fields = {"name": "Ospite", "color": "purple", "group_token": "", "valid_days": 1, "description": ""}
    ospite = db.save_category(None, fields)
    policy = {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Schedule": ["standard_office_hours"]}
    controller.profiles["esterno"] = {
        "token": "esterno",
        "Name": "Sempre",
        "Description": "A mano",
        "AccessPolicy": [policy],
    }
    controller.profiles["pulizie"] = {"token": "pulizie", "Name": "Pulizie", "Description": "", "AccessPolicy": []}
    anna = await plant.access.create_person("Anna", "04aa", "admin", group="profilo_sempre")
    bruno = await plant.access.create_person("Bruno", "04bb", "admin", group="esterno")
    carla = await plant.access.create_person("Carla", "04cc", "admin", group="pulizie")
    dario = await plant.access.create_person("Dario", "", "admin")

    assert await plant.rules.adopt_groups() == 7

    categories = {c["name"]: c for c in plant.rules.list_categories()}
    sempre, gruppo, pulizie = categories["Sempre"], categories["Sempre (gruppo)"], categories["Pulizie"]
    assert ospite < sempre["id"] < gruppo["id"] < pulizie["id"]
    assert (sempre["color"], gruppo["color"], pulizie["color"]) == ("green", "orange", "red")
    assert (gruppo["group_token"], gruppo["description"]) == ("esterno", "A mano")
    assert gruppo["rules"] == [door_rule(plant, "standard_office_hours")] and pulizie["rules"] == []
    assert categories["Ospite"]["group_token"] == "profilo_sempre" and categories["Ospite"]["rules"] == [
        door_rule(plant)
    ]
    assert [db.get_person(p["id"])["category_id"] for p in (anna, bruno, carla, dario)] == [
        sempre["id"],
        gruppo["id"],
        pulizie["id"],
        sempre["id"],
    ]
    assert sempre["default"] is True
    assert await plant.rules.adopt_groups() == 0


async def test_card_made_by_a_script_is_extra(plant, controller):
    await app_made(plant)
    token = controller.add_existing("utente_script_250101120000", "Ospite Script", "04ff")

    result, adopted, notes = await compared(plant)

    rows = {r["key"]: r for r in changed(result)}
    assert set(rows) == {f"tessera:{token}", "utente:utente_script_250101120000"}
    assert rows[f"tessera:{token}"]["action"] == "togli"
    assert "creata fuori dall'app (per esempio da uno script)" in rows[f"tessera:{token}"]["surprises"]
    assert rows["utente:utente_script_250101120000"]["action"] == "togli"
    assert rows["utente:utente_script_250101120000"]["surprises"]


async def test_factory_schedules_are_never_in_the_desired_state(plant, controller, db):
    """Fasce di fabbrica modificate nell'app (anche una che sul controller non c'è più): diventano fasce dell'app con
    un token nuovo, mai standard_*; Sempre resta di fabbrica e non si scrive."""
    await app_made(plant)
    await plant.rules.update_schedule("standard_office_hours", "Ufficio", "", OFFICE, False, "admin")
    db.save_schedule_rules("standard_weekends", [{"days": ["SA", "SU"], "start": "00:00", "end": "24:00"}], False)
    db.save_schedule_rules("standard_always", [{"days": ["MO"], "start": "00:00", "end": "24:00"}], False)

    result, adopted, notes = await compared(plant)

    schedules = build(adopted, plant.controller_id, ["fascia"])["objects"]["fascia"]
    assert schedules and not [t for t in schedules if t.startswith("standard_")]
    assert sorted(s["name"] for s in adopted["schedules"] if s["token"].startswith("aam_fascia_")) == [
        "Fine Settimana",
        "Orario d'Ufficio",
    ]
    assert [s["token"] for s in adopted["schedules"] if s["factory"]] == ["standard_always"]
    assert not [r for r in result["rows"] if r["action"] == "togli" or r["token"].startswith("standard_")]
