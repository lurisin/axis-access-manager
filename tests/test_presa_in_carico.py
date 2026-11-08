"""Presa in carico in memoria: impianto come al banco → presa in carico → stato voluto → confronto senza righe, e i
casi che il confronto deve mostrare (vedi presa_in_carico.py). Mai una scrittura sul controller."""

import logging

import pytest
from fastapi.testclient import TestClient
from test_backup import Plant
from test_comparison import ADMIN
from test_full_restore import bench_like
from test_rules_service import OFFICE

from app.compare import compare, read_strict
from app.desired import build, load_conf
from app.main import create_app
from app.presa_in_carico import category_for, confronto, presa_in_carico
from app.rules import schedule_ical
from app.service import ServiceError


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


def external_profile(controller, token="lungo", **fields):
    """Profilo fatto a mano sul sito Axis, sulla porta dell'app: di serie con una descrizione di 250 caratteri, che
    l'app non accetta."""
    policy = {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Attribute": [], "Schedule": ["standard_always"]}
    controller.profiles[token] = {
        "token": token,
        "Name": "Manutenzione",
        "Description": "x" * 250,
        "Enabled": True,
        "Schedule": ["standard_always"],
        "AccessPolicy": [policy],
        **fields,
    }


async def test_adopt_groups_keeps_who_comes_before_a_profile_it_does_not_accept(plant, controller, db):
    """Come la 0.27, che sistemava e salvava una persona alla volta in ordine di nome: un profilo esterno con una
    descrizione di 250 caratteri ferma adopt_groups alla persona che lo usa. Anna, che viene prima, resta in Sempre nel
    database; la modifica di Sempre riscrive profilo_sempre al suo posto, senza un profilo nuovo."""
    db.set_setting("default_group", "profilo_sempre")
    assert await plant.rules.adopt_groups() == 1
    [sempre] = plant.rules.list_categories()
    external_profile(controller)
    # Tessere create da uno script e importate dalla sincronizzazione: persone senza tipologia
    anna = await plant.access.create_person("Anna", "04aa", "admin", group="profilo_sempre")
    zoe = await plant.access.create_person("Zoe", "04ee", "admin", group="lungo")
    for person in (anna, zoe):
        db.set_person_category(person["id"], 0)
    calls, audit = list(controller.calls), db.list_audit()

    with pytest.raises(ServiceError, match="descrizione oltre il limite di 200 caratteri"):
        await plant.rules.adopt_groups()

    assert [db.get_person(p["id"])["category_id"] for p in (anna, zoe)] == [sempre["id"], 0]
    assert [c["name"] for c in plant.rules.list_categories()] == ["Sempre"]
    # Come la 0.27: niente sul controller, e la riga nel registro delle modifiche solo quando tutto è sistemato
    assert controller.calls == calls and db.list_audit() == audit
    # Al giro dopo si ferma ancora su Zoe, senza toccare Anna
    with pytest.raises(ServiceError, match="descrizione oltre il limite di 200 caratteri"):
        await plant.rules.adopt_groups()
    assert db.get_person(anna["id"])["category_id"] == sempre["id"]

    controller.calls.clear()
    rules = [door_rule(plant, "standard_office_hours")]
    result = await plant.rules.save_category(sempre["id"], {"name": "Sempre", "rules": rules}, "admin")

    assert result["token"] == "profilo_sempre"
    assert controller.calls == [("set_access_profile", "profilo_sempre")]
    assert controller.profiles["profilo_sempre"]["Schedule"] == ["standard_office_hours"]
    assert db.get_person(anna["id"])["cards"][0]["access_profile"] == "profilo_sempre"


@pytest.mark.parametrize(
    ("fields", "error", "match", "born"),
    [
        ({}, ServiceError, "descrizione oltre il limite di 200 caratteri", []),
        ({"Name": "   ", "Description": ""}, ServiceError, "manca il nome", []),
        ({"Name": "N" * 150, "Description": ""}, ServiceError, "nome troppo lungo", []),
        # Una regola che non si sa leggere: come nella 0.27 la tipologia di Zoe è già nata, senza righe
        ({"Description": "", "AccessPolicy": ["illeggibile"]}, AttributeError, "no attribute 'get'", ["Manutenzione"]),
    ],
)
async def test_adopt_groups_on_a_new_installation_saves_what_comes_before_the_error(
    plant, controller, db, fields, error, match, born
):
    """Installazione nuova con le tessere già sul controller: Sempre nasce per Anna e resta nel database anche se
    Zoe usa un profilo che l'app non accetta o non sa leggere. La tipologia predefinita viene dopo le persone: come
    nella 0.27, non si arriva a sceglierla."""
    db.set_setting("default_group", "profilo_sempre")
    external_profile(controller, **fields)
    anna = await plant.access.create_person("Anna", "04aa", "admin", group="profilo_sempre")
    zoe = await plant.access.create_person("Zoe", "04ee", "admin", group="lungo")

    with pytest.raises(error, match=match):
        await plant.rules.adopt_groups()

    categories = {c["name"]: c for c in plant.rules.list_categories()}
    sempre = categories.pop("Sempre")
    assert (sempre["group_token"], sempre["rules"]) == ("profilo_sempre", [door_rule(plant)])
    assert [(c["name"], c["rules"]) for c in categories.values()] == [(name, []) for name in born]
    assert [db.get_person(p["id"])["category_id"] for p in (anna, zoe)] == [sempre["id"], 0]
    assert not db.get_setting("default_category")


def test_adopt_groups_after_a_restart_like_0_27(settings, controller, tmp_path, monkeypatch, caplog):
    """Impianto in servizio: uno script crea sul controller la tessera di Anna (profilo di Sempre) e quella di Zoe
    (profilo con la descrizione lunga). Al riavvio la sincronizzazione le importa senza tipologia e la riparazione
    dei gruppi non riesce su Zoe, ma Anna resta in Sempre: la modifica di Sempre riscrive profilo_sempre e Anna entra
    solo in orario d'ufficio, come tutti quelli di Sempre."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    path = tmp_path / "riavvio.db"
    with TestClient(create_app(settings, allow_any_client=True, controller=controller, db_path=path)) as client:
        client.get("/api/me", headers=ADMIN)
    external_profile(controller, Description="Profilo creato a mano per la ditta di manutenzione. " * 5)
    for token, name, number, profile in (("s1", "Anna", "0a01", "profilo_sempre"), ("s2", "Zoe", "0a02", "lungo")):
        controller.users[f"u_{token}"] = {"token": f"u_{token}", "Name": name, "Attribute": []}
        controller.credentials[token] = {
            "token": token,
            "UserToken": f"u_{token}",
            "Description": name,
            "Enabled": True,
            "IdData": [{"Name": "Card", "Value": number}],
            "CredentialAccessProfile": [{"AccessProfile": profile, "ValidFrom": "", "ValidTo": ""}],
            "ValidFrom": "",
            "ValidTo": "",
        }

    with (
        caplog.at_level(logging.WARNING, logger="app.main"),
        TestClient(create_app(settings, allow_any_client=True, controller=controller, db_path=path)) as client,
    ):
        client.get("/api/me", headers=ADMIN)
        assert "Riparazione all'avvio (gruppi) non riuscita, si riprova: descrizione oltre il limite" in caplog.text
        [sempre] = client.get("/api/categories", headers=ADMIN).json()
        persons = {p["name"]: p["category_id"] for p in client.app.state.db.list_persons()}
        assert persons == {"Anna": sempre["id"], "Zoe": 0}
        door = client.get("/api/installation", headers=ADMIN).json()["controllers"][0]["doors"][0]["id"]
        controller.calls.clear()
        rules = [{"target": "door", "target_id": door, "schedule": "standard_office_hours"}]
        response = client.put(f"/api/categories/{sempre['id']}", json={"name": "Sempre", "rules": rules}, headers=ADMIN)

    assert response.status_code == 200
    assert controller.calls == [("set_access_profile", "profilo_sempre")]
    assert controller.credentials["s1"]["CredentialAccessProfile"][0]["AccessProfile"] == "profilo_sempre"
    assert controller.profiles["profilo_sempre"]["Schedule"] == ["standard_office_hours"]


async def test_profile_the_app_would_not_accept_is_shortened_in_the_presa(plant, controller, db):
    """Nella presa in carico in memoria un profilo esterno con un nome o una descrizione che l'app non accetta non
    ferma niente: la tipologia nuova li accorcia (un nome fatto solo di spazi diventa il token) e la riga del suo
    profilo lo dice. Sia per l'Accesso Personalizzato (Carla) sia per chi non ha tipologia (Dario, Elio)."""
    await app_made(plant)
    external_profile(controller, "lungo")
    external_profile(controller, "spazi", Name="   ", Description="")
    external_profile(controller, "nome", Name="N" * 150, Description="Fatto a mano")
    carla = await plant.access.create_person("Carla", "04cc", "admin", group="lungo")
    dario = await plant.access.create_person("Dario", "04dd", "admin", group="spazi")
    elio = await plant.access.create_person("Elio", "04ee", "admin", group="nome")
    for person in (dario, elio):
        db.set_person_category(person["id"], 0)

    result, adopted, notes = await compared(plant, notes=True)

    categories = {c["group_token"]: c for c in adopted["categories"]}
    lungo, spazi, nome = categories["lungo"], categories["spazi"], categories["nome"]
    assert (lungo["name"], lungo["description"]) == ("Manutenzione", "x" * 200)
    assert (spazi["name"], nome["name"], nome["description"]) == ("spazi", "N" * 40, "Fatto a mano")
    persons = {p["id"]: p["category_id"] for p in adopted["persons"]}
    assert [persons[p["id"]] for p in (carla, dario, elio)] == [lungo["id"], spazi["id"], nome["id"]]
    rows = {r["token"]: r for r in result["rows"] if r["kind"] == "tipologia"}
    assert rows["lungo"]["warnings"] == ["descrizione accorciata a 200 caratteri (sul controller ne ha 250)"]
    assert rows["spazi"]["warnings"] == [
        "nome fatto solo di spazi sul controller: la tipologia prende il nome dal token del profilo"
    ]
    assert rows["nome"]["warnings"] == ["nome accorciato a 40 caratteri (ne ha 150)"]
    # Riscriverebbe il profilo con nome e descrizione della tipologia: chi entra dove resta com'è
    assert {t: [c["label"] for c in rows[t]["changes"]] for t in ("lungo", "spazi", "nome")} == {
        "lungo": ["Descrizione"],
        "spazi": ["Nome", "Descrizione"],
        "nome": ["Nome"],
    }
    external = ["profilo esterno usato da tessere dell'app"]
    assert all(rows[t]["surprises"] == external for t in ("lungo", "spazi", "nome"))
    # Nella 0.28 la presa in carico non si salva
    assert [c["name"] for c in plant.rules.list_categories()] == ["Personale"]


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


async def test_adopt_groups_stops_like_0_27_when_the_group_name_is_taken_too(plant, controller, db):
    """Tre profili fatti a mano con lo stesso nome: il primo diventa «Impresa», il secondo «Impresa (gruppo)», e sul
    terzo la 0.27 si fermava (nome già usato al salvataggio), con quello che veniva prima già salvato."""
    db.set_setting("default_group", "profilo_sempre")
    assert await plant.rules.adopt_groups() == 1
    persons = []
    for name, token, card in (("Anna", "om1", "04a1"), ("Bruno", "om2", "04b2"), ("Carla", "om3", "04c3")):
        external_profile(controller, token, Name="Impresa", Description="")
        persons.append(await plant.access.create_person(name, card, "admin", group=token))
        db.set_person_category(persons[-1]["id"], 0)

    with pytest.raises(ServiceError, match=r"esiste già una tipologia con il nome Impresa \(gruppo\)"):
        await plant.rules.adopt_groups()

    names = {c["id"]: c["name"] for c in plant.rules.list_categories()}
    assert [names.get(db.get_person(p["id"])["category_id"]) for p in persons] == ["Impresa", "Impresa (gruppo)", None]


def test_presa_gives_a_free_name_when_the_group_name_is_taken_too():
    """Nella presa in carico in memoria lo stesso caso non ferma niente: il terzo prende il primo numero libero, e la
    riga lo dice."""
    categories = [
        {"id": 1, "name": "Impresa", "group_token": "om1"},
        {"id": 2, "name": "Impresa (gruppo)", "group_token": "om2"},
    ]
    warnings = []
    category = category_for("om3", {"Name": "Impresa", "Description": ""}, categories, warnings)
    assert category["name"] == "Impresa (gruppo 2)"
    assert warnings == ["il nome Impresa (gruppo) è già di un'altra tipologia: diventa Impresa (gruppo 2)"]
    with pytest.raises(ServiceError, match="esiste già una tipologia"):
        category_for("om3", {"Name": "Impresa", "Description": ""}, categories)
