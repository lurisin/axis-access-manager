"""Tessere fisiche (0.29.0): l'elenco delle tessere, Libere o assegnate. Una tessera Libera sta solo nell'app, mai sul
controller; togliere una tessera a una persona o eliminare la persona la rimette Libera, con il suo codice e il suo
Nome Tessera; si elimina per sempre solo dall'elenco. «Da Registrare» non c'è più: una tessera che non è di nessuno,
passata a una porta, è solo un evento «Tessera Non Valida».

Solo il FakeController; codici e nomi di fantasia."""

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_direct import ADMIN as PAGE_ADMIN
from test_direct import clients, fast_hash  # noqa: F401
from test_direct_plain import plant  # noqa: F401
from test_direct_plain_page import ingress_server  # noqa: F401
from test_page_morph import _has_playwright

from app.db import SCHEMA_VERSION, Database
from app.events import Event
from app.main import create_app
from app.service import AccessService, ServiceError
from app.transfer import import_persons
from app.vapix import WRITE_PREFIXES

ADMIN = {"X-Remote-User-Name": "rising", "X-Remote-User-Display-Name": "Rising"}
TS = "2026-11-20T08:00:00+00:00"
# Tabella delle tessere della 0.28.11, parola per parola: la persona è obbligatoria e si porta via le tessere
CARDS_0_28_11 = """
CREATE TABLE cards (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    number TEXT NOT NULL UNIQUE COLLATE NOCASE,
    enabled INTEGER NOT NULL DEFAULT 1,
    axis_credential_token TEXT UNIQUE,
    created_at TEXT NOT NULL
, access_profile TEXT NOT NULL DEFAULT '', valid_from TEXT NOT NULL DEFAULT '', valid_to TEXT NOT NULL DEFAULT '',
 missing_since TEXT NOT NULL DEFAULT ''{code})
"""
UNKNOWN_0_28_11 = """
CREATE TABLE unknown_cards (
    number TEXT PRIMARY KEY COLLATE NOCASE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1
)
"""


def database_0_28_11(path, code: bool = True) -> None:
    """Database com'è con la 0.28.11 (schema 10): due persone con una tessera, una tessera rimasta senza la sua
    persona, una tessera da registrare e un cruscotto salvato con il suo riquadro."""
    Database(path, "0.28.11").close()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX IF EXISTS cards_printed_code")
    conn.execute("DROP TABLE cards")
    conn.execute(CARDS_0_28_11.format(code=", printed_code TEXT NOT NULL DEFAULT ''" if code else ""))
    conn.execute(UNKNOWN_0_28_11)
    conn.execute("INSERT INTO unknown_cards VALUES ('04ff', ?, ?, 2)", (TS, TS))
    for person_id, name, token in ((1, "Anna Neri", "u1"), (2, "Bruno Verdi", "u2")):
        conn.execute(
            "INSERT INTO persons (id, name, axis_user_token, created_at, updated_at, first_name, last_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (person_id, name, token, TS, TS, *name.split()),
        )
    rows = [
        (3, 1, "04aa", 1, "c1", "p1", "Numero 1"),
        (5, 2, "04bb", 0, "c2", "p2", ""),
        (8, 99, "04cc", 1, "c9", "", ""),
    ]
    for card_id, person_id, number, enabled, token, profile, code_value in rows:
        conn.execute(
            "INSERT INTO cards (id, person_id, number, enabled, axis_credential_token, created_at, access_profile) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (card_id, person_id, number, enabled, token, TS, profile),
        )
        if code and code_value:
            conn.execute("UPDATE cards SET printed_code = ? WHERE id = ?", (code_value, card_id))
    layout = [{"type": "accessi_oggi", "size": 1}, {"type": "da_registrare", "size": 2}]
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('dashboard_layout:rising', ?)", (json.dumps(layout),))
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("code", [True, False])
def test_database_of_0_28_11_keeps_the_cards_and_lets_them_be_free(tmp_path, code):
    path = tmp_path / "tessere.db"
    database_0_28_11(path, code)
    for _ in range(2):
        # Anche una seconda volta: niente da rifare
        db = Database(path, "0.29.0")
        try:
            assert db.schema_version == SCHEMA_VERSION == 11
            cards = {c["id"]: c for c in db.list_cards()}
            assert {i: (c["number"], c["person_id"], c["person_name"]) for i, c in cards.items()} == {
                3: ("04aa", 1, "Anna Neri"),
                5: ("04bb", 2, "Bruno Verdi"),
                # Rimasta senza la sua persona: Libera, senza token né accesso
                8: ("04cc", None, ""),
            }
            assert (cards[3]["axis_credential_token"], cards[3]["access_profile"], cards[3]["enabled"]) == (
                "c1",
                "p1",
                True,
            )
            assert (cards[5]["enabled"], cards[5]["created_at"]) == (False, TS)
            assert (cards[8]["axis_credential_token"], cards[8]["access_profile"]) == (None, "")
            assert cards[3]["printed_code"] == ("Numero 1" if code else "")
            tables = {row["name"] for row in db._query("SELECT name FROM sqlite_master")}
            assert "unknown_cards" not in tables and "cards_new" not in tables and "cards_printed_code" in tables
            assert json.loads(db.get_setting("dashboard_layout:rising"))[1] == {"type": "tessere_libere", "size": 2}
            # I numeri uguali restano rifiutati, senza distinguere le maiuscole
            with pytest.raises(sqlite3.IntegrityError):
                db.add_free_card("04AA")
        finally:
            db.close()


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "tessere.db")
    with TestClient(app) as test_client:
        test_client.get("/api/me", headers=ADMIN)
        yield test_client


@pytest.fixture
def service(db, controller, settings):
    return AccessService(db, controller, settings)


def writes(controller) -> list[str]:
    return [name for name in controller.attempts if name.startswith(WRITE_PREFIXES)]


def on_controller(controller) -> set[str]:
    """Codici delle tessere scritte sul controller."""
    return {c["IdData"][0]["Value"].lower() for c in controller.credentials.values()}


def card(client, number: str) -> dict | None:
    cards = client.get("/api/cards", headers=ADMIN).json()["cards"]
    return next((c for c in cards if c["number"].lower() == number.lower()), None)


def check(client, controller, wanted: dict[str, tuple[str, str]]) -> None:
    """wanted: per ogni codice il Nome Tessera e la persona ("" se Libera). Le Libere non sono sul controller, le
    assegnate sì, e dopo la sincronizzazione il confronto in ombra non ha differenze."""
    cards = {
        c["number"].lower(): (c["printed_code"], c["person_name"])
        for c in client.get("/api/cards", headers=ADMIN).json()["cards"]
    }
    assert cards == wanted
    assigned = {number for number, (_, person) in wanted.items() if person}
    assert on_controller(controller) == assigned
    assert client.post("/api/sync", headers=ADMIN).status_code == 200
    shadow = {k["kind"]: k for k in client.app.state.shadow.as_dict()["kinds"]}
    assert (shadow["utente"]["differenze"], shadow["tessera"]["differenze"]) == (0, 0)
    # Un elenco di utenti vuoto non si distingue da uno non mandato: non conta come letto
    assert not shadow["tessera"]["non_letto"] and (not shadow["utente"]["non_letto"] or not controller.users)
    # La sincronizzazione non cambia l'elenco
    again = {
        c["number"].lower(): (c["printed_code"], c["person_name"])
        for c in client.get("/api/cards", headers=ADMIN).json()["cards"]
    }
    assert again == wanted and on_controller(controller) == assigned
    assert not client.get("/api/missing-cards", headers=ADMIN).json()


def test_loading_cards_never_touches_the_controller_and_proposes_the_next_name(client, controller):
    assert client.get("/api/cards", headers=ADMIN).json() == {"cards": [], "next_name": "Numero 1"}
    controller.attempts.clear()
    names = []
    for number in ("04a1b2c3", "04a1b2c4", "04a1b2c5"):
        response = client.post(
            "/api/cards", json={"card": number, "printed_code": f"Numero {len(names) + 1}"}, headers=ADMIN
        )
        assert response.status_code == 200, response.text
        names.append(response.json()["next_name"])
    assert names == ["Numero 2", "Numero 3", "Numero 4"]
    assert controller.attempts == [] and controller.credentials == {}
    # Già caricata: lo dice subito, con il nome e lo stato (anche con altre maiuscole e con gli spazi)
    response = client.post("/api/cards", json={"card": " 04A1B2C4 "}, headers=ADMIN)
    assert response.status_code == 400 and response.json()["detail"] == "tessera già caricata (Numero 2): libera"
    response = client.post("/api/cards", json={"card": "04a1b2c6", "printed_code": "numero 3"}, headers=ADMIN)
    assert response.status_code == 400 and response.json()["detail"] == "Nome Tessera già usato da una tessera libera"
    assert client.post("/api/cards", json={"card": "zz"}, headers=ADMIN).status_code == 400
    # La 2 eliminata per sempre (rotta): si propone di nuovo la 2
    second = card(client, "04a1b2c4")
    assert client.delete(f"/api/cards/{second['id']}/forever", headers=ADMIN).status_code == 200
    assert client.get("/api/cards", headers=ADMIN).json()["next_name"] == "Numero 2"
    assert controller.attempts == []
    actions = [(a["action"], a["detail"]) for a in client.app.state.db.list_audit()][:2]
    assert actions == [
        ("tessera eliminata", "tessera 04a1b2c4, Numero 2"),
        ("tessera caricata", "tessera 04a1b2c5, Numero 3"),
    ]


def test_next_name_follows_the_last_loaded_shape(db, service):
    assert service.next_card_name() == "Numero 1"
    service.load_card("04a1", "admin", "Mario Rossi")
    # Senza un numero in fondo non c'è una serie da continuare
    assert service.next_card_name() == "Numero 1"
    service.load_card("04a2", "admin", "A-07")
    assert service.next_card_name() == "A-01"
    service.load_card("04a3", "admin", "A-01")
    service.load_card("04a4", "admin", "a-02")
    # Come l'ultimo caricato, lettere comprese
    assert service.next_card_name() == "a-03"
    service.load_card("04a5", "admin", "12")
    assert service.next_card_name() == "1"


def test_every_operation_keeps_free_cards_off_the_controller_and_the_shadow_clean(client, controller):
    """Stati (assente, Libera, assegnata, sospesa, non presente sul controller) per operazioni (carica, nuova persona,
    aggiungi tessera, togli, elimina persona, Togli dall'App, Riscrivi, elimina per sempre)."""
    for number, name in (("04aa", "Numero 1"), ("04bb", "Numero 2"), ("04cc", "Numero 3")):
        assert client.post("/api/cards", json={"card": number, "printed_code": name}, headers=ADMIN).status_code == 200
    check(client, controller, {"04aa": ("Numero 1", ""), "04bb": ("Numero 2", ""), "04cc": ("Numero 3", "")})
    # Nuova persona con una Libera (scritta in maiuscolo), e con una tessera mai caricata
    anna = client.post("/api/persons", json={"name": "Anna Neri", "card": "04AA"}, headers=ADMIN).json()
    bruno = client.post("/api/persons", json={"name": "Bruno Verdi", "card": "04dd"}, headers=ADMIN).json()
    assert anna["cards"][0]["id"] == 1 and anna["cards"][0]["printed_code"] == "Numero 1"
    wanted = {
        "04aa": ("Numero 1", "Anna Neri"),
        "04bb": ("Numero 2", ""),
        "04cc": ("Numero 3", ""),
        "04dd": ("", "Bruno Verdi"),
    }
    check(client, controller, wanted)
    # Aggiungi Tessera: una Libera, poi una già di un altro (rifiutata prima di scrivere sul controller)
    response = client.post(f"/api/persons/{anna['id']}/cards", json={"card": "04bb"}, headers=ADMIN)
    assert response.status_code == 200 and [c["id"] for c in response.json()["cards"]] == [1, 2]
    controller.attempts.clear()
    response = client.post(f"/api/persons/{bruno['id']}/cards", json={"card": "04BB"}, headers=ADMIN)
    assert response.status_code == 400 and "già registrata a nome Anna Neri" in response.json()["detail"]
    assert writes(controller) == []
    wanted["04bb"] = ("Numero 2", "Anna Neri")
    check(client, controller, wanted)
    # Sospesa e poi tolta: torna Libera e attiva, con lo stesso nome; una Libera non si sospende né si toglie
    assert client.patch("/api/cards/2", json={"enabled": False}, headers=ADMIN).status_code == 200
    assert client.delete("/api/cards/2", headers=ADMIN).json()["person"]["cards"][0]["id"] == 1
    assert (card(client, "04bb")["enabled"], card(client, "04bb")["axis_credential_token"]) == (True, None)
    controller.attempts.clear()
    assert client.patch("/api/cards/2", json={"enabled": False}, headers=ADMIN).status_code == 400
    assert client.delete("/api/cards/2", headers=ADMIN).json()["detail"] == "la tessera è già libera"
    assert writes(controller) == []
    wanted["04bb"] = ("Numero 2", "")
    check(client, controller, wanted)
    # Il nome di una Libera si cambia dall'elenco, senza controller
    response = client.patch("/api/cards/2", json={"printed_code": "Numero 20"}, headers=ADMIN)
    assert response.status_code == 200 and response.json()["card"]["printed_code"] == "Numero 20"
    assert writes(controller) == []
    wanted["04bb"] = ("Numero 20", "")
    # Non presente sul controller: «Riscrivi» la riscrive, «Togli dall'App» la rimette Libera
    token = client.app.state.db.get_card(1)["axis_credential_token"]
    controller.credentials.pop(token)
    assert client.post("/api/sync", headers=ADMIN).json()["missing"] == 1
    assert client.post("/api/missing-cards/restore", json={}, headers=ADMIN).json()["restored"] == 1
    check(client, controller, wanted)
    controller.credentials.pop(client.app.state.db.get_card(1)["axis_credential_token"])
    assert client.post("/api/sync", headers=ADMIN).json()["missing"] == 1
    assert client.post("/api/missing-cards/forget", json={"ids": [1]}, headers=ADMIN).json() == {"cards": 1}
    wanted["04aa"] = ("Numero 1", "")
    # Anna resta, senza tessere: il suo utente sul controller non serve più
    controller.users.pop(anna["axis_user_token"], None)
    check(client, controller, wanted)
    # Elimina persona: la sua tessera torna Libera; eliminata per sempre solo una Libera
    assert client.delete(f"/api/cards/{bruno['cards'][0]['id']}/forever", headers=ADMIN).status_code == 400
    assert client.delete(f"/api/persons/{bruno['id']}", headers=ADMIN).status_code == 200
    wanted["04dd"] = ("", "")
    check(client, controller, wanted)
    assert client.delete(f"/api/cards/{bruno['cards'][0]['id']}/forever", headers=ADMIN).status_code == 200
    del wanted["04dd"]
    check(client, controller, wanted)
    assert controller.users == {} and controller.credentials == {}


def test_a_credential_written_outside_the_app_for_a_free_card_assigns_it(client, controller):
    """La sincronizzazione non si ferma più su una tessera Libera scritta sul controller: nell'app passa a chi la ha
    sul controller (la porta con lei apre già), con una riga in Attività, e tiene il suo nome."""
    anna = client.post("/api/persons", json={"name": "Anna Neri", "card": "04aa"}, headers=ADMIN).json()
    assert (
        client.post("/api/cards", json={"card": "04bb", "printed_code": "Numero 2"}, headers=ADMIN).status_code == 200
    )
    controller.credentials["fuori"] = {
        "token": "fuori",
        "UserToken": anna["axis_user_token"],
        "Enabled": True,
        "IdData": [{"Name": "Card", "Value": "04BB"}],
    }
    for _ in range(2):
        assert client.post("/api/sync", headers=ADMIN).status_code == 200
    found = card(client, "04bb")
    assert (found["id"], found["person_name"], found["printed_code"]) == (2, "Anna Neri", "Numero 2")
    audit = [a for a in client.app.state.db.list_audit() if a["action"] == "tessere libere trovate sul controller"]
    assert [a["detail"] for a in audit] == ["1 tessera assegnata a chi la ha sul controller: 04BB"]


def event(topic: str, data: dict, second: int = 0) -> Event:
    """Evento del controller di un'ora fa (le notifiche guardano le ultime 24 ore)."""
    moment = (datetime.now(tz=UTC) - timedelta(hours=1)).replace(minute=0, second=second, microsecond=0)
    return Event(topic.split("/")[0], topic, moment, data)


def test_a_free_card_at_a_door_is_an_invalid_card_with_its_name(client, controller):
    """Una Libera passata a una porta non è di nessuno: «Tessera Non Valida», con il suo nome per chi guarda il
    registro; lo stesso recuperata dal registro del controller. Mai un errore di scrittura del registro."""
    state = client.app.state
    assert (
        client.post("/api/cards", json={"card": "04bb", "printed_code": "Numero 2"}, headers=ADMIN).status_code == 200
    )
    service = state.service

    def record(passage: Event) -> None:
        # Nel ciclo dell'app, come gli eventi veri
        client.portal.call(service.record_event, passage)

    record(event("IdPoint/Request/IdData/x", {"Card": "04BB"}))
    record(event("AccessControl/Denied/CredentialNotFound/Card", {"Card": "04BB", "AccessPointToken": "ap"}))
    record(event("IdPoint/Request/IdData/x", {"Card": "05ff"}, 5))
    service._recovering = True
    try:
        record(event("IdPoint/Request/IdData/x", {"Card": "04BB"}, 30))
        record(event("AccessControl/Denied/CredentialNotFound/Card", {"Card": "04BB", "AccessPointToken": "ap"}, 30))
    finally:
        service._recovering = False
    rows = client.get("/api/events", headers=ADMIN).json()
    rows = rows["events"] if isinstance(rows, dict) else rows
    shown = [(e["kind"], e["card"], e["person_name"], e["detail"], e["printed_code"], e["recovered"]) for e in rows]
    assert shown == [
        ("negato", "04BB", "", "tessera libera", "Numero 2", 1),
        ("sconosciuta", "04BB", "", "tessera libera", "Numero 2", 1),
        ("sconosciuta", "05ff", "", "", "", 0),
        ("negato", "04BB", "", "tessera libera", "Numero 2", 0),
        ("sconosciuta", "04BB", "", "tessera libera", "Numero 2", 0),
    ]
    assert not state.service.write_error
    items = [i for i in client.get("/api/notifications", headers=ADMIN).json()["items"] if i["type"] == "unknown_card"]
    assert [(i["title"], i["text"], i["link"]) for i in items] == [
        ("Tessera Non Valida ×2", "Tessera 04BB · Numero 2 · libera", "#tessere")
    ]
    # In Home Assistant mai il nome della tessera
    assert "Numero 2" not in json.dumps(state.bridge._last_payload())


async def test_import_gives_a_free_card_to_the_person_of_the_file(service, db, controller):
    loaded = service.load_card("04bb", "admin", "Numero 2")
    text = "nome;tessera\nAnna Neri;04BB\n"
    groups = [{"token": "profilo_sempre", "name": "Sempre"}]
    result = await import_persons(service, text, groups, "admin", apply=True)
    assert result["riepilogo"]["errori"] == 0
    [anna] = db.list_persons()
    [got] = anna["cards"]
    assert (got["id"], got["number"], got["printed_code"]) == (loaded["id"], "04bb", "Numero 2")
    assert on_controller(controller) == {"04bb"}


async def test_backups_carry_free_cards_and_stay_the_same_without(service, db):
    from app.backup import common_snapshot

    await service.create_person("Anna Neri", "04aa", "admin")
    before = json.dumps(common_snapshot(db, []), sort_keys=True)
    assert '"person_id": null' not in before
    loaded = service.load_card("04bb", "admin", "Numero 2")
    cards = {c["number"]: c for c in common_snapshot(db, [])["cards"]}
    assert cards["04bb"] == {
        "number": "04bb",
        "person_id": None,
        "enabled": True,
        "access_profile": "",
        "valid_from": "",
        "valid_to": "",
        "printed_code": "Numero 2",
    }
    service.delete_free_card(loaded["id"], "admin")
    assert json.dumps(common_snapshot(db, []), sort_keys=True) == before
    with pytest.raises(ServiceError, match="tessera non trovata"):
        service.delete_free_card(loaded["id"], "admin")


LOADING = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({viewport: {width: 1280, height: 900}, extraHTTPHeaders: config.headers});
  const page = await context.newPage();
  const out = {errors: [], posts: []};
  page.on("pageerror", (e) => out.errors.push(String(e)));
  // Il salvataggio dura più del tempo tra una tessera e l'altra: il lettore intanto scrive già la successiva
  await page.route("**/api/cards", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    out.posts.push(JSON.parse(route.request().postData()));
    await new Promise((resolve) => setTimeout(resolve, 400));
    return route.continue();
  });
  await page.goto(`${config.base}/#tessere`);
  await page.waitForSelector("#c-load [name=card]");
  out.focus = await page.evaluate(() => document.activeElement?.name || "");
  out.proposed = await page.inputValue("#c-load [name=printed_code]");
  // Il lettore da banco: i caratteri del codice e Invio, una tessera dietro l'altra, senza aspettare
  for (const code of config.codes) {
    await page.keyboard.type(code, {delay: 5});
    await page.keyboard.press("Enter");
  }
  // Un secondo Invio a campo vuoto non salva niente
  await page.keyboard.press("Enter");
  await page.waitForFunction((n) => document.querySelectorAll("#c-list > .item").length === n, config.codes.length);
  const rows = () => page.$$eval("#c-list > .item", (items) => items.map((i) => [
    i.querySelector(".title").textContent.trim(), i.querySelector(".mono").textContent.trim()]));
  out.rows = await rows();
  out.count = await page.textContent("#c-count");
  out.next = await page.inputValue("#c-load [name=printed_code]");
  out.focusAfter = await page.evaluate(() => document.activeElement?.name || "");
  out.field = await page.inputValue("#c-load [name=card]");
  // Già caricata: lo dice subito, e il campo resta pronto
  await page.keyboard.type(config.codes[0].toUpperCase());
  await page.keyboard.press("Enter");
  await page.waitForSelector("#c-note.alert.err");
  out.again = await page.textContent("#c-note");
  // Un nome scritto a mano vale per la tessera dopo, poi si torna alla serie
  await page.fill("#c-load [name=printed_code]", "Ospite Blu");
  await page.focus("#c-load [name=card]");
  await page.keyboard.type(config.extra);
  await page.keyboard.press("Enter");
  await page.waitForFunction((n) => document.querySelectorAll("#c-list > .item").length === n, config.codes.length + 1);
  out.note = await page.textContent("#c-note");
  out.nextAfter = await page.inputValue("#c-load [name=printed_code]");
  out.rowsAfter = await rows();
  out.scroll = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_cards_swiped_one_after_the_other_are_all_loaded(plant, ingress_server, controller):  # noqa: F811
    """Nella pagina vera: il lettore da banco scrive come una tastiera e manda Invio. Tre tessere di fila, più veloci
    del salvataggio: nessun carattere perso, ognuna con il suo nome, il campo sempre pronto, il controller mai
    toccato."""
    app = plant[3]
    controller.attempts.clear()
    codes = ["04a1b2c3", "04a1b2c4", "04a1b2c5"]
    config = {"base": ingress_server, "headers": PAGE_ADMIN, "codes": codes, "extra": "04a1b2c6"}
    result = subprocess.run(["node", "-e", LOADING, json.dumps(config)], capture_output=True, text=True, timeout=180)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["errors"] == [] and not out["scroll"]
    assert (out["focus"], out["proposed"]) == ("card", "Numero 1")
    # Il nome lo sceglie l'app al salvataggio: ognuna il suo, anche se sono partite insieme
    assert out["posts"][:3] == [{"card": code, "printed_code": None} for code in codes]
    assert out["rows"] == [["Numero 1", "04a1b2c3"], ["Numero 2", "04a1b2c4"], ["Numero 3", "04a1b2c5"]]
    assert out["count"] == "3 tessere · 3 libere"
    assert (out["next"], out["focusAfter"], out["field"]) == ("Numero 4", "card", "")
    assert out["again"] == "04A1B2C3: Tessera già caricata (Numero 1): libera"
    assert out["posts"][4] == {"card": "04a1b2c6", "printed_code": "Ospite Blu"}
    assert out["note"] == "Caricata: Ospite Blu · 04a1b2c6" and out["nextAfter"] == "Numero 4"
    assert out["rowsAfter"][-1] == ["Ospite Blu", "04a1b2c6"]
    stored = {c["number"]: (c["printed_code"], c["person_id"]) for c in app.state.db.list_cards()}
    assert stored == {
        "04a1b2c3": ("Numero 1", None),
        "04a1b2c4": ("Numero 2", None),
        "04a1b2c5": ("Numero 3", None),
        "04a1b2c6": ("Ospite Blu", None),
    }
    assert writes(controller) == [] and controller.credentials == {}
