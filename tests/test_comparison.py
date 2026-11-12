"""Confronto (prova) del controller e confronto in ombra: solo letture e calcoli, mai una scrittura sul controller."""

import asyncio
import json
import logging
import sqlite3
from html import escape

import pytest
from fastapi.testclient import TestClient
from test_full_restore import bench_like
from test_page_time_zone import NAMES, needs_node, run_page

from app import main
from app.compare import KIND_TITLES, KINDS, LISTS
from app.main import create_app
from app.vapix import WRITE_PREFIXES, ControllerGuard

ADMIN = {"X-Remote-User-Name": "rising", "X-Remote-User-Display-Name": "Rising"}


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "confronto.db")
    with TestClient(app) as test_client:
        test_client.get("/api/me", headers=ADMIN)
        yield test_client


def controller_id(client):
    return client.get("/api/installation", headers=ADMIN).json()["controllers"][0]["id"]


def writes(controller):
    """Scritture tentate, anche quelle non riuscite (controller spento)."""
    return [name for name in controller.attempts if name.startswith(WRITE_PREFIXES)]


def loop_time(client):
    async def now():
        return asyncio.get_running_loop().time()

    return client.portal.call(now)


def by_kind(data):
    return {group["kind"]: group for group in data["kinds"]}


def test_comparison_rows_by_kind_and_never_a_write(client, controller):
    anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
    client.post("/api/persons", json={"name": "Bruno", "card": "04bb"}, headers=ADMIN)
    token = anna["cards"][0]["axis_credential_token"]
    # Fatti a mano sul controller: descrizione cambiata, un PIN, una tessera di uno script, uno sblocco programmato
    controller.credentials[token]["Description"] = "Anna (portineria)"
    controller.credentials[token]["IdData"].append({"Name": "PIN", "Value": "4321"})
    script = {"token": "script", "Enabled": True, "IdData": [{"Name": "Card", "Value": "0499"}]}
    controller.credentials["script"] = script
    controller.door_configs["porta"]["Configuration"].append({"Name": "DoorScheduleConfiguration.X", "Value": "s"})
    controller.calls.clear()
    controller.attempts.clear()
    controller.reads.clear()

    response = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN)

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True and data["error"] == "" and data["changed"] is False
    assert [g["kind"] for g in data["kinds"]] == list(KINDS)
    assert [g["title"] for g in data["kinds"]] == [KIND_TITLES[k] for k in KINDS]
    groups = by_kind(data)
    cards = {row["token"]: row for row in groups["tessera"]["rows"]}
    assert groups["tessera"]["counts"] == {"riporta": 1, "uguale": 1, "togli": 1}
    changed = cards[token]
    assert changed["action"] == "riporta" and changed["text"] == "Riporterebbe com'è nell'app"
    changes = {c["label"]: c for c in changed["changes"]}
    assert (changes["Descrizione"]["old"], changes["Descrizione"]["new"]) == ("Anna (portineria)", "Anna")
    # Il PIN non si mostra mai
    assert "4321" not in str(data) and "••••" in changes["Numero e PIN"]["old"]
    assert cards["script"]["action"] == "togli" and cards["script"]["text"] == "Toglierebbe: nell'app non c'è"
    assert cards["script"]["surprises"] == ["creata fuori dall'app (per esempio da uno script)"]
    # Lo sblocco programmato sta con la configurazione delle porte
    [unlock] = [r for r in groups["config_porta"]["rows"] if r["kind"] == "sblocco"]
    assert unlock["action"] == "togli" and unlock["text"].startswith("Toglierebbe: ")
    # Senza la presa in carico la programmazione si prenderebbe com'è; i lettori non si leggono (finto): non letto
    assert {r["action"] for r in groups["porta"]["rows"]} == {"prende"}
    assert groups["porta"]["rows"][0]["text"] == "Prenderebbe in carico com'è sul controller"
    assert groups["config_lettore"]["unread"] and groups["config_lettore"]["rows"] == []
    assert data["notes"][0]["text"].startswith("Non si fa: elenchi non letti")
    # Solo letture: ogni elenco una volta, nessuna scrittura né tentativo
    assert controller.calls == [] and writes(controller) == []
    lists = [name for name in controller.attempts if name.startswith("list_")]
    assert sorted(lists) == sorted({LISTS[k][1] for k in LISTS} - {"list_authentication_profiles"})


def test_comparison_with_the_controller_off(client, controller):
    """Spento davvero: risposta chiara, niente righe, una sola chiamata e nessuna scrittura."""
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.down = True
    controller.calls.clear()
    controller.attempts.clear()

    response = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN)

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False and data["error"] == "il controller non risponde (tempo scaduto)"
    assert data["kinds"] == [] and data["notes"] == []
    assert controller.attempts == ["device_info"] and controller.calls == []
    # Nel confronto in ombra la programmazione resta non letta, non vuota
    shadow = {k["kind"]: k for k in data["shadow"]["kinds"]}
    assert shadow["porta"]["non_letto"] == "il controller non risponde (tempo scaduto)"
    # Il giro di Confronta Ora, non quello di ogni ora
    assert data["shadow"]["rounds"]["confronta_ora"]["error"] == "il controller non risponde (tempo scaduto)"
    assert "programmazione" not in data["shadow"]["rounds"]


def test_comparison_needs_the_plant_permission(client, controller):
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    for username, perms in (("mario", ["persone", "registro", "backup"]), ("tecnico", ["impianti"])):
        assert client.post("/api/users", json={"username": username, "perms": perms}, headers=ADMIN).status_code == 200
    path = f"/api/controllers/{controller_id(client)}/comparison"
    controller.attempts.clear()

    assert client.get(path, headers={"X-Remote-User-Name": "mario"}).status_code == 403
    assert controller.attempts == []
    # Impianti senza Persone: tessere e utenti solo contati
    data = client.get(path, headers={"X-Remote-User-Name": "tecnico"}).json()
    groups = by_kind(data)
    for kind in ("tessera", "utente"):
        assert groups[kind]["rows"] == [] and groups[kind]["counts"] == {"uguale": 1}
        assert groups[kind]["hidden"] == "serve il permesso «Vedere persone, tessere e tipologie»"
    assert "04aa" not in str(groups) and "Anna" not in str(groups)
    assert groups["porta"]["rows"]
    assert client.get("/api/controllers/99/comparison", headers=ADMIN).status_code == 404
    assert writes(controller) == []


def test_comparison_with_another_controller_at_the_address(client, controller):
    """Controller cambiato e non ancora confermato: la scheda lo dice e mostra il confronto, ma il confronto in ombra
    non mescola la sua programmazione con quella del controller conosciuto."""
    state = client.app.state
    state.controller.paused = "all'indirizzo risponde un altro controller"

    data = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).json()

    assert data["ok"] is True and data["paused"] == "all'indirizzo risponde un altro controller"
    assert by_kind(data)["porta"]["rows"]
    assert "programmazione" not in state.shadow.rounds and writes(controller) == []


def test_each_list_is_read_in_its_own_block(client, controller, monkeypatch):
    """Una nuova configurazione aspetta al massimo un elenco: ogni elenco nel suo blocco _reading, con il client preso
    dentro. Se intanto il client cambia, gli elenchi rimasti non si leggono con quello nuovo."""
    state = client.app.state
    blocks, clients = [], []
    real = main.read_strict

    async def spy(client_, kinds):
        [done] = state.readers
        blocks.append(done)
        clients.append(client_)
        if len(blocks) == 3:
            # Come reload_runtime con un indirizzo nuovo
            state.controller = ControllerGuard(controller)
        return await real(client_, kinds)

    monkeypatch.setattr(main, "read_strict", spy)
    data = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).json()

    assert len(blocks) == 3 and len(set(map(id, blocks))) == 3 and len(set(map(id, clients))) == 1
    assert state.readers == set()
    unread = {g["kind"]: g["unread"] for g in data["kinds"] if g["unread"]}
    assert unread["tessera"] == "configurazione del controller cambiata durante la lettura"
    assert not unread.get("dispositivo") and not unread.get("fascia")


def test_shadow_after_the_sync_reads_nothing_more(client, controller, caplog):
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.reads.clear()
    controller.attempts.clear()

    with caplog.at_level(logging.WARNING, logger="app.shadow"):
        assert client.post("/api/sync", headers=ADMIN).status_code == 200

    # Le stesse letture della sincronizzazione, niente di più
    assert [r[0] for r in controller.reads] == ["list_credentials", "list_users", "list_access_points"]
    assert writes(controller) == []
    shadow = client.app.state.shadow.as_dict()
    counts = {k["kind"]: (k["uguali"], k["differenze"], k["non_letto"]) for k in shadow["kinds"]}
    assert counts == {"utente": (1, 0, ""), "tessera": (1, 0, "")}
    assert shadow["rounds"]["utenti_tessere"]["at"] and "programmazione" not in shadow["rounds"]
    assert caplog.records == []
    # Riepilogo nello stato del controller
    [health] = client.get("/api/installation", headers=ADMIN).json()["controllers"][0:1]
    assert health["health"]["shadow"]["kinds"][1]["uguali"] == 1


def test_shadow_warns_once_for_the_same_difference(client, controller, caplog):
    anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
    token = anna["cards"][0]["axis_credential_token"]
    credential = controller.credentials[token]

    def warnings():
        return [r.getMessage() for r in caplog.records if r.name == "app.shadow"]

    with caplog.at_level(logging.WARNING, logger="app.shadow"):
        credential["Description"] = "Anna (a mano)"
        client.post("/api/sync", headers=ADMIN)
        client.post("/api/sync", headers=ADMIN)
        assert warnings() == [
            "Confronto in ombra: Tessera 04aa (Anna): Riporterebbe com'è nell'app · Descrizione Anna (a mano) → Anna"
        ]
        # Un'altra differenza sullo stesso oggetto: una riga nuova
        credential["Description"] = "Anna B"
        client.post("/api/sync", headers=ADMIN)
        assert len(warnings()) == 2 and warnings()[-1].endswith("Descrizione Anna B → Anna")
        # Sistemata e poi tornata: si scrive di nuovo
        credential["Description"] = "Anna"
        client.post("/api/sync", headers=ADMIN)
        credential["Description"] = "Anna B"
        client.post("/api/sync", headers=ADMIN)
    assert len(warnings()) == 3
    kinds = {k["kind"]: k for k in client.app.state.shadow.as_dict()["kinds"]}
    assert kinds["tessera"]["differenze"] == 1 and kinds["tessera"]["uguali"] == 0


def test_shadow_users_read_empty_are_unread(client, controller, caplog):
    """La sincronizzazione legge gli utenti senza strict: vuoto non vuol dire che sul controller non ci sono."""
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.keyless.add("list_users")

    with caplog.at_level(logging.WARNING, logger="app.shadow"):
        assert client.post("/api/sync", headers=ADMIN).status_code == 200

    kinds = {k["kind"]: k for k in client.app.state.shadow.as_dict()["kinds"]}
    assert kinds["utente"]["non_letto"].startswith("utenti: elenco vuoto") and kinds["utente"]["differenze"] == 0
    assert kinds["tessera"]["uguali"] == 1
    assert caplog.records == []


def test_shadow_unread_is_not_resolved(client, controller, caplog):
    """Una differenza già nel log, poi l'elenco non letto, poi di nuovo la stessa differenza: nessuna riga nuova."""
    anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
    controller.users[anna["axis_user_token"]]["Attribute"] = [{"Name": "Reparto", "Value": "Sala"}]

    with caplog.at_level(logging.WARNING, logger="app.shadow"):
        client.post("/api/sync", headers=ADMIN)
        assert len(caplog.records) == 1 and caplog.records[0].getMessage().startswith("Confronto in ombra: Utente Anna")
        controller.keyless.add("list_users")
        client.post("/api/sync", headers=ADMIN)
        assert client.app.state.shadow.kinds["utente"]["non_letto"]
        controller.keyless.clear()
        client.post("/api/sync", headers=ADMIN)
    assert len(caplog.records) == 1
    assert client.app.state.shadow.kinds["utente"]["differenze"] == 1


def test_shadow_failure_does_not_change_the_sync(client, controller, monkeypatch, caplog):
    """Disco o database che non si leggono: la sincronizzazione riesce lo stesso, il confronto in ombra no."""
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.attempts.clear()

    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(main, "load_conf", broken)
    with caplog.at_level(logging.ERROR, logger="app.service"):
        response = client.post("/api/sync", headers=ADMIN)

    assert response.status_code == 200 and response.json()["cards"] == 1
    assert "Confronto in ombra di utenti e tessere non riuscito" in caplog.text
    assert writes(controller) == []


def test_hourly_comparison_at_most_once_an_hour(client, controller, caplog):
    state = client.app.state
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)

    async def configurations(*, strict=False):
        return []

    # Come l'A1210 del banco, che le configurazioni dei lettori le restituisce: la presa in carico si calcola. Una
    # differenza vera, la porta con 90 s
    controller.list_idpoint_configurations = configurations
    controller.doors["porta"]["AccessTime"] = "PT90S"
    controller.reads.clear()
    controller.attempts.clear()
    controller.calls.clear()

    # Appena avviato: il primo dopo SHADOW_DELAY
    assert client.portal.call(main._shadow_programming, state) is False
    assert controller.reads == []
    assert state.shadow.next_full - loop_time(client) > main.SHADOW_DELAY - 60

    state.shadow.next_full = 0
    with caplog.at_level(logging.WARNING, logger="app.shadow"):
        assert client.portal.call(main._shadow_programming, state) is True
        read = [r[0] for r in controller.reads]
        # Solo la programmazione: tessere e utenti li confronta la sincronizzazione
        assert "list_doors" in read and "list_credentials" not in read and "list_users" not in read
        assert 3590 < state.shadow.next_full - loop_time(client) <= main.SHADOW_INTERVAL
        logged = len(caplog.records)
        assert logged
        # Prima dell'ora: niente
        assert client.portal.call(main._shadow_programming, state) is False
        assert len(controller.reads) == len(read)
        # L'ora dopo, con le stesse differenze: si legge, ma niente di nuovo nel log
        state.shadow.next_full = 0
        assert client.portal.call(main._shadow_programming, state) is True
        assert len(caplog.records) == logged
    assert writes(controller) == [] and controller.calls == []
    shadow = state.shadow.as_dict()
    assert shadow["rounds"]["programmazione"]["kinds"] == [k for k in KINDS if k not in ("utente", "tessera")]


def test_compare_now_keeps_its_own_time_apart_from_the_hourly_round(client, controller):
    """Collaudo 0.28.0, punto K: Confronta Ora spostava l'ora del giro «Programmazione, ogni ora» (da «6 min fa» ad
    «adesso»). Ora ha la sua ora, e il giro di ogni ora resta com'era e si fa alla sua ora."""
    state = client.app.state
    bench_like(controller)
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.attempts.clear()
    state.shadow.next_full = 0
    assert client.portal.call(main._shadow_programming, state) is True
    hourly = "2000-01-01T09:21:01+01:00"
    state.shadow.rounds["programmazione"]["at"] = hourly
    due = state.shadow.next_full
    assert due - loop_time(client) > 3590

    data = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).json()

    rounds = data["shadow"]["rounds"]
    assert rounds["programmazione"]["at"] == hourly
    assert rounds["confronta_ora"]["at"] > hourly and rounds["confronta_ora"]["error"] == ""
    assert rounds["confronta_ora"]["kinds"] == rounds["programmazione"]["kinds"]
    # Non sposta il giro di ogni ora: quando è ora si fa, e ha la sua ora
    assert state.shadow.next_full == due
    state.shadow.next_full = 0
    assert client.portal.call(main._shadow_programming, state) is True
    assert state.shadow.rounds["programmazione"]["at"] != hourly
    assert writes(controller) == []


def test_shadow_round_without_the_presa_changes_nothing_in_the_log(client, controller, caplog):
    """Giro normale, giro con le configurazioni delle porte rifiutate una volta (la presa in carico non si calcola),
    giro normale: le differenze vanno nel log una volta sola, e il profilo della tipologia dell'app non diventa mai
    «esterno». Il giro senza presa dice perché non ha confrontato."""
    state = client.app.state
    bench_like(controller)
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.doors["porta"]["AccessTime"] = "PT90S"
    controller.attempts.clear()

    def run():
        state.shadow.next_full = 0
        assert client.portal.call(main._shadow_programming, state) is True
        return [r.getMessage() for r in caplog.records if r.name == "app.shadow"]

    with caplog.at_level(logging.WARNING, logger="app.shadow"):
        first = run()
        assert any(m.startswith("Confronto in ombra: Porta Ingresso scala: Riporterebbe") for m in first)
        assert any(m.startswith("Confronto in ombra: Profilo di Accesso Sempre: Riporterebbe") for m in first)
        assert run() == first
        controller.refuse("list_door_configurations", "ter:Busy", times=1)
        assert run() == first
        shadow = state.shadow.as_dict()
        error = shadow["rounds"]["programmazione"]["error"]
        assert error.startswith("non confrontato: presa in carico non calcolata: configurazione delle porte: ")
        kinds = {k["kind"]: k for k in shadow["kinds"]}
        assert kinds["config_porta"]["non_letto"].startswith("configurazione delle porte: ")
        assert kinds["porta"]["non_letto"] == kinds["tipologia"]["non_letto"] == error
        assert kinds["porta"]["differenze"] == kinds["porta"]["sorprese"] == 0
        assert run() == first
    assert "profilo esterno" not in " ".join(first)
    shadow = state.shadow.as_dict()
    assert shadow["rounds"]["programmazione"]["error"] == ""
    kinds = {k["kind"]: k for k in shadow["kinds"]}
    assert kinds["porta"]["differenze"] == 1 and kinds["porta"]["non_letto"] == ""
    assert writes(controller) == []


def test_comparison_without_the_presa_keeps_the_profiles_of_the_app(client, controller):
    """Senza la presa in carico (il finto non dà le configurazioni dei lettori) la scheda mostra i profili come sono,
    e il profilo della tipologia dell'app non è «esterno»; un profilo che nessuna tipologia usa sì."""
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.profiles["esterno"] = {**controller.profiles["profilo_sempre"], "token": "esterno", "Name": "Esterno"}
    controller.attempts.clear()

    data = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).json()

    assert data["notes"][0]["text"].startswith("Non si fa: elenchi non letti")
    rows = {row["token"]: row for row in by_kind(data)["tipologia"]["rows"]}
    assert (rows["profilo_sempre"]["action"], rows["profilo_sempre"]["surprises"]) == ("prende", [])
    assert rows["esterno"]["surprises"] == ["profilo esterno: non è di una tipologia dell'app"]
    assert writes(controller) == []


def test_hourly_comparison_waits_for_the_controller(client, controller):
    """Un controller che non risponde per il controllo di stato non si legge, e appena torna non aspetta un'ora."""
    state = client.app.state
    state.shadow.next_full = 0
    controller.offline = True
    for _ in range(3):
        client.portal.call(state.health.check_all)
    controller.attempts.clear()

    assert client.portal.call(main._shadow_programming, state) is False
    assert controller.attempts == [] and state.shadow.next_full == 0

    controller.offline = False
    client.portal.call(state.health.check_all)
    assert client.portal.call(main._shadow_programming, state) is True
    assert writes(controller) == []


def test_comparison_with_a_profile_the_app_would_not_accept(client, controller, monkeypatch, caplog):
    """Anna ha come Accesso Personalizzato un profilo fatto a mano con una descrizione di 230 caratteri, che l'app non
    accetta: il confronto non si ferma per questo. Le righe ci sono, quella del profilo dice che la descrizione si
    accorcia, e il confronto in ombra di ogni ora non fallisce."""
    state = client.app.state
    bench_like(controller)
    anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
    policy = {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Attribute": [], "Schedule": ["standard_always"]}
    controller.profiles["esterno"] = {
        "token": "esterno",
        "Name": "Impresa",
        "Description": "x" * 230,
        "Enabled": True,
        "Schedule": ["standard_always"],
        "AccessPolicy": [policy],
    }
    token = anna["cards"][0]["axis_credential_token"]
    controller.credentials[token]["CredentialAccessProfile"][0]["AccessProfile"] = "esterno"
    client.post("/api/sync", headers=ADMIN)
    controller.attempts.clear()

    response = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN)

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True and data["error"] == ""
    groups = by_kind(data)
    assert groups["porta"]["rows"] and groups["tessera"]["rows"] and groups["config_lettore"]["unread"] == ""
    [row] = [r for r in groups["tipologia"]["rows"] if r["token"] == "esterno"]
    assert row["warnings"] == ["descrizione accorciata a 200 caratteri (sul controller ne ha 230)"]
    assert row["surprises"] == ["profilo esterno usato da tessere dell'app"]
    assert [c["label"] for c in row["changes"]] == ["Descrizione"]
    assert any(n["text"].startswith("Accesso Personalizzato: passa alla tipologia Impresa") for n in data["notes"])

    # Un giro del confronto in ombra, come nel ciclo di ogni ora: senza l'avviso «non riuscito»
    done = asyncio.Event()
    hourly = main._shadow_programming

    async def once(state):
        try:
            return await hourly(state)
        finally:
            done.set()

    async def one_round():
        task = asyncio.create_task(main._shadow_loop(client.app))
        await done.wait()
        task.cancel()

    monkeypatch.setattr(main, "_shadow_programming", once)
    state.shadow.next_full = 0
    with caplog.at_level(logging.WARNING, logger="app.main"):
        client.portal.call(one_round)
    assert "Confronto in ombra della programmazione non riuscito" not in caplog.text
    assert state.shadow.as_dict()["rounds"]["programmazione"]["error"] == ""
    assert state.shadow.next_full - loop_time(client) > 3590
    assert writes(controller) == []


# La scheda Confronto della pagina, fatta girare con node sulle risposte vere dell'app (vedi test_page_time_zone)
PAGE_NAMES = (
    *NAMES,
    "esc",
    "plural",
    "cap",
    "icon",
    "capped",
    "cappedRows",
    "restoreItemHtml",
    "RESTORE_ACTIONS",
    "COMPARE_COUNTS",
    "compareCounts",
    "props",
    "statusDot",
    "shadowHtml",
    "comparisonHtml",
)


def page_html(expression: str, data) -> str:
    out = run_page(
        "Europe/Rome",
        {"html": expression},
        names=PAGE_NAMES,
        prelude=f"const ICONS = {{}}; const DATA = {json.dumps(data)};",
    )
    assert not out["html"].startswith("ERRORE"), out["html"]
    return out["html"]


@needs_node
def test_every_kind_has_its_list_in_the_comparison_tab(client, controller):
    """Collaudo 0.28.0, punto K: «Porte · 1 da prendere in carico» non aveva l'elenco, perché la porta (rinominata dalla
    presa in carico) c'era solo in Da Guardare. Ora ogni tipo ha tutte le sue righe; quelle anche in Da Guardare con il
    rimando."""
    bench_like(controller)
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    data = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).json()
    groups = by_kind(data)
    [door] = [r for r in groups["porta"]["rows"] if r["warnings"]]
    assert door["warnings"] == ["nell'app si chiamava Porta: prende il nome del controller"]

    html = page_html("comparisonHtml(DATA)", data)

    look, *parts = html.split('<h3 class="diff-part">')[1:]
    assert look.startswith("Da Guardare") and escape(door["label"]) in look
    sections = {part.split("<", 1)[0].strip(): part for part in parts}
    for group in data["kinds"]:
        section = sections[escape(group["title"])]
        for row in group["rows"]:
            assert escape(row["label"]) in section, (group["kind"], row["label"])
    porte = sections["Porte"]
    assert "<details" in porte and "2 da prendere in carico com&#39;è sul controller" in porte
    assert f"{escape(door['label'])} <span class=\"muted\">· in Da Guardare</span>" in porte


@needs_node
def test_shadow_shows_the_hourly_round_and_compare_now_apart(client, controller):
    """Le due ore nella scheda: il giro di ogni ora e Confronta Ora. Un giro orario senza niente letto e poi Confronta
    Ora riuscito: i tipi tornano nella tabella."""
    state = client.app.state
    bench_like(controller)
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    controller.down = True
    state.shadow.next_full = 0
    assert client.portal.call(main._shadow_programming, state) is True
    state.shadow.rounds["programmazione"]["at"] = "2000-01-01T09:21:01+01:00"
    for kind in state.shadow.kinds.values():
        kind["at"] = "2000-01-01T09:21:01+01:00" if kind["non_letto"] else kind["at"]
    controller.down = False
    data = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).json()

    html = page_html("shadowHtml(DATA)", data["shadow"])

    assert "Programmazione, ogni ora: 01/01/2000 09:21 <span" in html and "tempo scaduto" in html
    assert "Programmazione, con Confronta Ora: adesso</p>" in html
    assert "<dt>Porte</dt>" in html and "<dt>Profili di Accesso</dt>" in html
