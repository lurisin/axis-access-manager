"""Il kit di prove al banco (tools/banco/prove_a1210.py) eseguito tutto sul controller finto, dietro HTTP."""

import asyncio
import copy
import importlib.util
import itertools
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from conftest import FakeController, refused

from app.rules import DAYS, schedule_ical
from app.vapix import ControllerUnreachable, VapixError

KIT_PATH = Path(__file__).resolve().parents[1] / "tools" / "banco" / "prove_a1210.py"
_spec = importlib.util.spec_from_file_location("prove_a1210", KIT_PATH)
kit = importlib.util.module_from_spec(_spec)
sys.modules["prove_a1210"] = kit
_spec.loader.exec_module(kit)

PREFIX = "aam_prova_"
SERIAL = "B8A44FE6A074"
CARD = "c0110b18"
# Quello che il kit può mandare, scritto qui a parte e non preso dal kit
ALLOWED = {
    "getAllProperties",
    "getDateTimeInfo",
    "axudb:GetUserList",
    "axudb:GetUser",
    "axudb:SetUser",
    "axudb:RemoveUser",
    "pacsaxis:GetCredentialList",
    "pacsaxis:GetCredential",
    "pacsaxis:SetCredential",
    "pacsaxis:RemoveCredential",
    "pacsaxis:EnableCredential",
    "pacsaxis:DisableCredential",
    "pacsaxis:GetAccessPointList",
    "pacsaxis:GetAccessPoint",
    "pacsaxis:SetAccessPoint",
    "pacsaxis:RemoveAccessPoint",
    "pacsaxis:GetAccessProfileList",
    "pacsaxis:GetAccessProfile",
    "pacsaxis:SetAccessProfile",
    "pacsaxis:RemoveAccessProfile",
    "pacsaxis:GetAuthenticationProfileList",
    "pacsaxis:GetAccessControllerList",
    "axsch:GetScheduleList",
    "axsch:SetSchedule",
    "axsch:RemoveSchedule",
    "axtdc:GetDoorList",
    "axtdc:GetDoor",
    "axtdc:SetDoor",
    "axtdc:RemoveDoor",
    "axtdc:RemoveDoorConfiguration",
    "axtdc:GetDoorConfigurationList",
    "axtid:GetIdPointList",
    "axtid:GetIdPoint",
    "axtid:SetIdPoint",
    "axtid:RemoveIdPoint",
    "axtid:RemoveIdPointConfiguration",
    "axtid:GetIdPointConfigurationList",
    "axisio:GetIoInfoList",
    "axisio:GetIoAssignmentList",
    "axlog:FetchEvents",
}
FORBIDDEN = re.compile(
    r"AccessDoor|UnlockDoor|LockDoor|LockDown|BlockDoor|DoorSchedule|SetDoorConfiguration|SetIdPointConfiguration|"
    r"SetIoAssignment|SetAccessController|SetAuthenticationProfile|TimeZone|NTP|configure|activate|getSupportedVersions"
)
FAKE_WRITES = {
    "set_user",
    "set_credential",
    "set_schedule",
    "set_access_profile",
    "set_door",
    "create_idpoint",
    "create_access_point",
    "remove_user",
    "remove_credential",
    "remove_schedule",
    "remove_access_profile",
    "remove_door",
    "remove_idpoint",
    "remove_access_point",
    "enable_credential",
    "disable_credential",
}
LISTS = {
    "axudb:GetUserList": ("User", "list_users"),
    "pacsaxis:GetCredentialList": ("Credential", "list_credentials"),
    "pacsaxis:GetAccessPointList": ("AccessPoint", "list_access_points"),
    "pacsaxis:GetAccessProfileList": ("AccessProfile", "list_access_profiles"),
    "pacsaxis:GetAuthenticationProfileList": ("AuthenticationProfile", "list_authentication_profiles"),
    "pacsaxis:GetAccessControllerList": ("AccessController", "list_access_controllers"),
    "axsch:GetScheduleList": ("Schedule", "list_schedules"),
    "axtdc:GetDoorList": ("Door", "list_doors"),
    "axtdc:GetDoorConfigurationList": ("DoorConfiguration", "list_door_configurations"),
    "axtid:GetIdPointList": ("IdPoint", "list_idpoints"),
    "axtid:GetIdPointConfigurationList": ("IdPointConfiguration", "list_idpoint_configurations"),
}
GETS = {
    "axudb:GetUser": ("User", "get_users"),
    "pacsaxis:GetCredential": ("Credential", "get_credentials"),
    "pacsaxis:GetAccessPoint": ("AccessPoint", "get_access_points"),
    "pacsaxis:GetAccessProfile": ("AccessProfile", "get_access_profiles"),
    "axtdc:GetDoor": ("Door", "get_doors"),
    "axtid:GetIdPoint": ("IdPoint", "get_idpoints"),
}
SETS = {
    "axudb:SetUser": ("User", "set_user"),
    "pacsaxis:SetCredential": ("Credential", "set_credential"),
    "pacsaxis:SetAccessPoint": ("AccessPoint", "create_access_point"),
    "pacsaxis:SetAccessProfile": ("AccessProfile", "set_access_profile"),
    "axsch:SetSchedule": ("Schedule", "set_schedule"),
    "axtdc:SetDoor": ("Door", "set_door"),
    "axtid:SetIdPoint": ("IdPoint", "create_idpoint"),
}
REMOVES = {
    "axudb:RemoveUser": ("users", "remove_user"),
    "pacsaxis:RemoveCredential": ("credentials", "remove_credential"),
    "pacsaxis:RemoveAccessPoint": ("access_points", "remove_access_point"),
    "pacsaxis:RemoveAccessProfile": ("profiles", "remove_access_profile"),
    "axsch:RemoveSchedule": ("schedules", "remove_schedule"),
    "axtdc:RemoveDoor": ("doors", "remove_door"),
    "axtid:RemoveIdPoint": ("idpoints", "remove_idpoint"),
}


class VapixServer:
    """Il controller finto dietro HTTP come lo vede il client vapix: richieste VAPIX → metodi del finto.
    Spento (down) non risponde; una risposta persa del finto diventa un tempo scaduto; un Fault torna con il suo
    codice. Un Remove di un oggetto che non c'è risponde NotFound, la variante prudente finché T7 non è fatta."""

    def __init__(self, controller: FakeController) -> None:
        self.controller = controller
        self.methods: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.startswith("/axis-cgi/"):
            method, args = body["method"], body.get("params") or {}
        else:
            method, args = next(iter(body.items()))
        self.methods.append(method)
        if self.controller.down:
            raise httpx.ConnectTimeout("spento", request=request)
        try:
            data = await self.answer(method, args)
        except ControllerUnreachable as err:
            raise httpx.ReadTimeout("risposta persa", request=request) from err
        except VapixError as err:
            return httpx.Response(400, json={"Fault": err.fault or "env:Receiver ter:ActionNotSupported"})
        return httpx.Response(200, json=data)

    def exists(self, kind: str, token: str) -> bool:
        objects = getattr(self.controller, kind)
        return any(o["token"] == token for o in objects) if isinstance(objects, list) else token in objects

    async def answer(self, method: str, args: dict) -> dict:
        c = self.controller
        if method == "getAllProperties":
            return {"data": {"propertyList": await c.device_info()}}
        if method == "getDateTimeInfo":
            return {"data": await c.date_time_info()}
        if method == "getNTPInfo":
            return {"data": await c.ntp_info()}
        if method == "getClientStatus":
            return {"data": await c.mqtt_client_config()}
        if method == "getEventPublicationConfig":
            return {"data": await c.mqtt_event_config()}
        if method in LISTS:
            key, name = LISTS[method]
            items = await getattr(c, name)()
            start, limit = int(args.get("StartReference") or 0), int(args.get("Limit") or 1000)
            page = {key: items[start : start + limit]}
            if start + limit < len(items):
                page["NextStartReference"] = str(start + limit)
            return page
        if method in GETS:
            key, name = GETS[method]
            return {key: await getattr(c, name)(args["Token"])}
        if method in SETS:
            key, name = SETS[method]
            item = args[key][0]
            return {"Token": [await getattr(c, name)(item) or item["token"]]}
        if method in REMOVES:
            kind, name = REMOVES[method]
            for token in args["Token"]:
                if not self.exists(kind, token):
                    raise refused("ter:NotFound")
                await getattr(c, name)(token)
            return {}
        if method in ("axtdc:RemoveDoorConfiguration", "axtid:RemoveIdPointConfiguration"):
            configs = c.door_configs if method.startswith("axtdc") else c.idpoint_configs
            if args["Token"][0] not in configs:
                raise refused("ter:NotFound")
            configs.pop(args["Token"][0])
            return {}
        if method == "pacsaxis:EnableCredential":
            await c.enable_credential(args["Token"])
            return {}
        if method == "pacsaxis:DisableCredential":
            await c.disable_credential(args["Token"])
            return {}
        if method in ("axisio:GetIoInfoList", "axisio:GetIoAssignmentList"):
            info, assigned = await c.list_io()
            return {"IoInfo": info} if method.endswith("InfoList") else {"IoAssignment": assigned}
        if method == "axlog:FetchEvents":
            if args.get("Descending"):
                return {"Event": await c.latest_events(args["Limit"], args.get("RowID"))}
            rows, more = await c.fetch_events(args["Start"], args["Limit"], args.get("RowID"))
            return {"Event": rows, "More": more}
        # Comandi alle porte, configurazioni e impostazioni: il finto non li esegue, il test li vede in methods
        return {}


class BenchOperator(kit.Operatore):
    """Chi sta al banco, nel test: fa sul finto quello che il kit gli chiede."""

    def __init__(self, controller: FakeController) -> None:
        super().__init__(scrivi=lambda *_: None)
        self.controller = controller
        self.asked: list[str] = []

    def chiedi(self, chiave: str, testo: str) -> str:
        self.asked.append(chiave)
        c = self.controller
        if chiave == "stacca_cavo":
            c.down = True
        elif chiave in ("riattacca_cavo", "riattacca_cavo_pulizia"):
            c.down = False
        elif chiave == "reset":
            c.factory_reset()
            c.fingerprint = "CC:DD"
        elif chiave == "configura_lettore":
            # Come la procedura guidata della pagina del controller: porta, lettore e punto gestito
            c.doors["porta_banco"] = {"token": "porta_banco", "Name": "Porta banco", "AccessTime": "PT7S"}
            c.idpoints["lettore_banco"] = {"token": "lettore_banco", "Name": "Lettore banco"}
            c.access_points.append(
                {
                    "token": "accesso_banco",
                    "Name": "Ingresso banco",
                    "Entity": "porta_banco",
                    "IdPointDevice": [{"IdPoint": "lettore_banco", "DeviceUUID": ""}],
                    "Attribute": [{"Name": "Direction", "Value": "in"}],
                }
            )
            next(iter(c.access_controllers.values()))["AccessPoint"].append("accesso_banco")
        elif testo.startswith("Passa"):
            self.swipe()
        elif chiave == "sentinella_sito":
            return "nome leggibile, descrizione intera"
        return ""

    def swipe(self) -> None:
        """Il controller registra la tessera e la nega. IdData e Denied come nel registro del banco (0.18); il motivo
        è inventato: quello vero lo dice T8."""
        c = self.controller
        row = max((e["rowid"] for e in c.log_events), default=0)
        credential = next((t for t, v in c.credentials.items() if v["IdData"][0]["Value"] == CARD), "")

        def entry(rowid, topics, **values):
            keys = [{"Key": f"topic{i}", "Value": t} for i, t in enumerate(topics)]
            return {
                "rowid": rowid,
                "UtcTime": "2026-11-07T10:00:00Z",
                "KeyValues": keys + [{"Key": k, "Value": v} for k, v in values.items()],
            }

        c.log_events += [
            entry(row + 1, ["IdPoint", "Request", "IdData"], Card=CARD, IdPointToken="lettore_banco"),
            entry(
                row + 2,
                ["AccessControl", "Denied", "Credential"],
                Reason="CredentialNotEnabled",
                CredentialToken=credential,
                CredentialHolderName="aam_prova_t8_titolare",
                AccessPointToken="accesso_banco",
            ),
        ]


async def no_wait(seconds):
    pass


def make_kit(
    controller,
    operator=None,
    serial=SERIAL,
    quante=500,
    tmp_path=None,
    limiti=False,
    tetti=None,
    servizio=False,
    tessera=CARD,
    partenza=None,
):
    server = VapixServer(controller)
    client, recorder = kit.cliente("banco.invalid", "root", "segreta", trasporto=httpx.MockTransport(server))
    # Il certificato il client lo legge con un collegamento TLS a parte: qui risponde il finto
    client.certificate_fingerprint = controller.certificate_fingerprint
    lines: list[str] = []
    bench = kit.Kit(
        client,
        recorder,
        serial,
        operator or kit.SenzaOperatore(),
        scrivi=lines.append,
        quante=quante,
        tessera=tessera,
        pausa=no_wait,
        limiti=limiti,
        tetti=tetti,
        servizio=servizio,
        partenza=partenza,
    )
    if tmp_path is not None:
        bench.file = tmp_path / "rapporto.json"
    return bench, server, lines


def factory_bench(check_references=True) -> FakeController:
    controller = FakeController.factory()
    controller.serial = SERIAL
    controller.check_references = check_references
    return controller


def leftovers(controller: FakeController) -> list[str]:
    found = []
    for name in (
        "users",
        "credentials",
        "schedules",
        "profiles",
        "doors",
        "idpoints",
        "door_configs",
        "idpoint_configs",
    ):
        for token, item in getattr(controller, name).items():
            if any(str(item.get(k) or "").startswith(PREFIX) for k in ("token", "Name", "Description", "UserToken")):
                found.append(f"{name} {token}")
            elif token.startswith(PREFIX):
                found.append(f"{name} {token}")
    found += [f"access_points {a['token']}" for a in controller.access_points if a["token"].startswith(PREFIX)]
    return found


def assert_nothing_forbidden(controller: FakeController, server: VapixServer) -> None:
    assert set(server.methods) <= ALLOWED, set(server.methods) - ALLOWED
    assert not [m for m in server.methods if FORBIDDEN.search(m)]
    writes = [c for c in controller.calls if c[0] not in FAKE_WRITES]
    assert writes == []
    assert all(str(c[1]).startswith(PREFIX) for c in controller.calls), controller.calls


@pytest.mark.parametrize("check_references", [False, True], ids=["riferimenti_liberi", "riferimenti_controllati"])
async def test_the_whole_kit_on_a_factory_controller(tmp_path, check_references):
    controller = factory_bench(check_references)
    operator = BenchOperator(controller)
    bench, server, lines = make_kit(controller, operator, tmp_path=tmp_path)

    data = await kit.esegui(bench, list(kit.ORDINE))

    assert list(data["prove"]) == kit.ORDINE
    assert {code: test["esito"] for code, test in data["prove"].items()} == dict.fromkeys(kit.ORDINE, "fatta")
    assert leftovers(controller) == []
    assert data["fine"]["pulizia"]["non_tolti"] == []
    assert "sul controller non resta niente del kit" in "\n".join(lines)
    assert_nothing_forbidden(controller, server)
    # Gli oggetti che non sono del kit restano com'erano (in T4 l'operatore ha configurato un lettore)
    changed = {code: list(test.get("estranei_cambiati") or {}) for code, test in data["prove"].items()}
    assert changed.pop("T4") == ["porte", "lettori", "punti", "controllo_accessi"]
    assert all(v == [] for v in changed.values()), changed
    # Le parti guidate sono passate dall'operatore
    assert {"stacca_cavo", "riattacca_cavo", "configura_lettore", "reset", "sentinella_sito"} <= set(operator.asked)
    # Il rapporto JSON c'è, con le richieste grezze e senza la password
    saved = json.loads((tmp_path / "rapporto.json").read_text())
    assert saved["controller"]["serie"] == SERIAL
    assert "segreta" not in (tmp_path / "rapporto.json").read_text()
    t1 = saved["prove"]["T1"]
    assert all(e.startswith("accettato: ") for kinds in t1["tabella"].values() for e in kinds.values())
    assert any(op["http"] and op["http"][0]["richiesta"] for op in t1["operazioni"])
    notes = {code: "\n".join(test["osservazioni"]) for code, test in saved["prove"].items()}
    assert "tessera: con il cavo staccato il controller non risponde" in notes["T2"]
    assert "NotFound" in notes["T7"]
    # T11 vede il controller com'è dopo il reset, prima che l'operatore configuri il lettore per T4 e T8
    for name in ("utenti", "porte", "lettori", "punti di accesso", "configurazioni delle porte"):
        assert f"{name}: chiave presente con []" in notes["T11"]
    assert "IdData arrivato" in notes["T8"] and "Reason=CredentialNotEnabled" in notes["T8"]
    # Con un controller che sospende davvero, T4 fa tutti e due i passaggi
    assert {"t4_set_disabled", "t4_disable"} <= set(operator.asked)
    assert "sospesa con SetCredential: IdData arrivato" in notes["T4"]
    assert "sospesa con DisableCredential, senza profilo (confronta con T8): IdData arrivato" in notes["T4"]
    assert "credenziale disabilitata: IdData arrivato" in notes["T8"]
    assert "scritte 500 tessere" in notes["T18"]
    assert "pagine da 300: 2" in notes["T18"]
    assert "T18 parziale: solo i tempi. NON provati i limiti con CapabilityViolated" in notes["T18"]
    assert "impronta del certificato CAMBIATA" in notes["T12"]
    assert saved["prove"]["T12"]["oggetti_di_serie"]["fasce"]
    if check_references:
        assert "tessera con un utente che non c'è: rifiutato" in notes["T6"]
        assert "fascia in uso da un profilo: rifiutato" in notes["T6"]
    else:
        assert "tessera con un utente che non c'è: accettato" in notes["T6"]


async def test_kit_without_operator_skips_the_guided_parts():
    controller = factory_bench()
    bench, server, _ = make_kit(controller, quante=20)

    data = await kit.esegui(bench, list(kit.ORDINE))

    outcomes = {code: test["esito"] for code, test in data["prove"].items()}
    assert {code for code, outcome in outcomes.items() if outcome == "saltata"} == {"T2", "T8", "T12"}
    assert set(outcomes.values()) == {"fatta", "saltata"}
    assert leftovers(controller) == []
    assert_nothing_forbidden(controller, server)


async def test_kit_does_not_start_on_another_controller():
    controller = factory_bench()
    bench, server, _ = make_kit(controller, serial="ALTRO000000")

    with pytest.raises(kit.Rifiuto, match="non è il controller di banco"):
        await kit.esegui(bench, list(kit.ORDINE))
    assert server.methods == ["getAllProperties"]
    assert controller.calls == []


async def test_kit_does_not_start_on_a_controller_in_service():
    controller = FakeController.bench()
    controller.serial = SERIAL
    controller.add_existing("utente_mario_rossi", "Mario Rossi", "04aa")
    bench, server, _ = make_kit(controller)

    with pytest.raises(kit.Rifiuto, match="utente_mario_rossi"):
        await kit.esegui(bench, list(kit.ORDINE))
    assert controller.calls == []
    assert not [m for m in server.methods if "Set" in m or "Remove" in m]


async def test_kit_cleans_up_after_an_error_and_says_what_is_left(tmp_path):
    controller = factory_bench()
    # T4 crea l'utente, poi la tessera è rifiutata: la prova si ferma con un errore
    controller.refuse("set_credential", "ter:CapabilityViolated", times=1)
    bench, server, lines = make_kit(controller, tmp_path=tmp_path)

    data = await kit.esegui(bench, ["T4"])

    assert list(data["prove"]) == ["T4"]
    assert data["prove"]["T4"]["esito"] == "errore"
    assert "utente aam_prova_t4_utente" in data["prove"]["T4"]["pulizia"]["tolti"]
    assert leftovers(controller) == []

    # Una rimozione che non riesce si vede nel rapporto e alla fine
    controller.refuse("remove_user", "ter:OperationProhibited")
    bench, server, lines = make_kit(controller, tmp_path=tmp_path)
    data = await kit.esegui(bench, ["T16"])
    assert data["prove"]["T16"]["pulizia"]["non_tolti"]
    printed = "\n".join(lines)
    assert "NON SONO RIUSCITO A TOGLIERE:" in printed and "utente aam_prova_impianto" in printed
    assert data["fine"]["pulizia"]["non_tolti"]
    assert_nothing_forbidden(controller, server)

    # Il giro dopo, con la rimozione di nuovo possibile, il kit toglie quello che era rimasto prima di partire
    controller.refuse("remove_user")
    bench, server, lines = make_kit(controller, tmp_path=tmp_path)
    data = await kit.esegui(bench, ["T7"])
    assert "rimasti da una volta precedente" in "\n".join(data["avvio"]["osservazioni"])
    assert leftovers(controller) == []


async def test_kit_cleans_up_when_the_controller_stops_answering():
    controller = factory_bench()
    controller.fail_after(30)
    bench, server, lines = make_kit(controller)

    data = await kit.esegui(bench, ["T6"])

    assert data["prove"]["T6"]["pulizia"]["non_tolti"]
    controller.heal()
    bench, server, lines = make_kit(controller)
    await kit.esegui(bench, ["T11"])
    assert leftovers(controller) == []


async def test_kit_cleans_up_when_interrupted():
    """Ctrl+C annulla il kit mentre aspetta: prima di fermarsi toglie la sentinella appena creata."""
    controller = factory_bench()
    bench, server, lines = make_kit(controller)

    async def interrupted(seconds):
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    bench.pausa = interrupted
    with pytest.raises(asyncio.CancelledError):
        await kit.esegui(bench, ["T16"])
    assert ("set_user", "aam_prova_impianto") in controller.calls
    assert bench.dati["prove"]["T16"]["esito"] == "interrotta"
    assert leftovers(controller) == []
    assert "sul controller non resta niente del kit" in "\n".join(lines)


async def test_one_test_at_a_time():
    controller = factory_bench()
    bench, server, _ = make_kit(controller)

    data = await kit.esegui(bench, ["T7"])

    assert list(data["prove"]) == ["T7"]
    assert not [m for m in server.methods if m.startswith(("axudb:Set", "pacsaxis:Set", "axsch:Set"))]


async def test_forbidden_requests_never_leave():
    controller = factory_bench()
    controller.doors["porta"] = {"token": "porta", "Name": "Porta"}
    bench, server, _ = make_kit(controller)
    client = bench.client
    attempts = [
        client.access_door("porta"),
        client.lock_door("porta"),
        client.set_door({"token": "porta", "Name": "Porta"}),
        client.remove_door("porta"),
        client.set_door_configuration({"token": f"{PREFIX}porta", "Configuration": []}),
        client.set_idpoint_configuration(f"{PREFIX}lettore", "", {}),
        client.set_io_assignment("REX", "in:pu/gnd", []),
        client.set_access_controller({"token": "Axis-x AccessController", "AccessPoint": []}),
        client.set_user({"token": "utente_vero", "Name": "Vero"}),
        client.set_user({"token": f"{PREFIX}utente", "Name": "nome senza prefisso"}),
        client.set_time_zone("UTC"),
        client.set_ntp(True, "static", ["pool.ntp.org"]),
        client.set_mqtt_client({"host": "x"}),
    ]
    for attempt in attempts:
        with pytest.raises(kit.Vietata):
            await attempt
    assert server.methods == []
    for method, args in (("access_door", ("porta",)), ("set_user", ({"token": "utente_vero", "Name": "x"},))):
        with pytest.raises(kit.Vietata):
            await bench.tenta(method, *args)
    assert server.methods == [] and controller.calls == []
    await bench.client.aclose()


# T4 e T8: la tessera vera dell'operatore non deve mai poter aprire la porta del banco


class IgnoresEnabled(FakeController):
    """SetCredential che ignora Enabled e Status: la tessera resta abilitata (proprio la domanda di T4)."""

    async def set_credential(self, credential):
        return await super().set_credential({**credential, "Enabled": True, "Status": "Enabled"})


class KeepsProfiles(FakeController):
    """SetCredential su un token esistente che unisce: con CredentialAccessProfile vuoto tiene i profili (T3)."""

    async def set_credential(self, credential):
        old = self.credentials.get(credential.get("token") or "")
        if old and not credential.get("CredentialAccessProfile"):
            credential = {**credential, "CredentialAccessProfile": old.get("CredentialAccessProfile") or []}
        return await super().set_credential(credential)


class DisableDoesNothing(IgnoresEnabled):
    """SetCredential ignora Enabled e DisableCredential risponde bene ma la tessera resta abilitata."""

    async def disable_credential(self, token):
        enabled = self.credentials.get(token, {}).get("Enabled")
        await super().disable_credential(token)
        if token in self.credentials:
            self.credentials[token]["Enabled"] = enabled


def opening_cards(controller: FakeController, point: str = "accesso_banco") -> list[tuple[str, str]]:
    """Tessere con il numero vero che aprirebbero il punto: abilitate e con un profilo che ha una regola per il punto
    (acceso o spento, per prudenza)."""
    found = []
    for credential in controller.credentials.values():
        if credential.get("Enabled") is False or not any(d.get("Value") == CARD for d in credential["IdData"]):
            continue
        for link in credential.get("CredentialAccessProfile") or []:
            profile = controller.profiles.get(link.get("AccessProfile")) or {}
            if any(p.get("AccessPoint") == point for p in profile.get("AccessPolicy") or []):
                found.append((credential["token"], profile["token"]))
    return found


class SwipeWatcher(BenchOperator):
    """L'operatore che a ogni passaggio di tessera guarda se la porta del banco si aprirebbe."""

    def __init__(self, controller: FakeController) -> None:
        super().__init__(controller)
        self.opening: list[tuple[str, str]] = []
        self.swipes = 0

    def swipe(self) -> None:
        self.swipes += 1
        self.opening += opening_cards(self.controller)
        super().swipe()


@pytest.mark.parametrize(
    "variant",
    ["ignora_enabled", "ignora_enabled_disable_rifiutato", "unisce_disable_rifiutato", "disable_che_non_sospende"],
)
async def test_t4_and_t8_never_ask_a_swipe_with_a_card_that_opens(variant):
    cls = {
        "ignora_enabled": IgnoresEnabled,
        "ignora_enabled_disable_rifiutato": IgnoresEnabled,
        "unisce_disable_rifiutato": KeepsProfiles,
        "disable_che_non_sospende": DisableDoesNothing,
    }[variant]
    controller = cls.factory()
    controller.serial = SERIAL
    if variant.endswith("disable_rifiutato"):
        controller.refuse("disable_credential", "env:Receiver ter:ActionNotSupported")
    operator = SwipeWatcher(controller)
    bench, server, _ = make_kit(controller, operator)

    data = await kit.esegui(bench, ["T4", "T8"])

    assert operator.opening == []
    # Le tessere abilitate senza un profilo che apra quella porta si passano lo stesso (T8, e T4 se Disable riesce)
    assert operator.swipes >= 3
    assert {code: test["esito"] for code, test in data["prove"].items()} == {"T4": "fatta", "T8": "fatta"}
    notes = {code: "\n".join(test["osservazioni"]) for code, test in data["prove"].items()}
    if variant != "unisce_disable_rifiutato":
        assert "sospesa con SetCredential: passaggio non fatto (SetCredential non sospende:" in notes["T4"]
        assert "credenziale disabilitata: passaggio non fatto (SetCredential non sospende:" in notes["T8"]
        assert "t4_set_disabled" not in operator.asked and "credenziale disabilitata" not in operator.asked
    if variant != "ignora_enabled":
        assert "sospesa con DisableCredential, senza profilo (confronta con T8): DisableCredential" in notes["T4"]
        assert "t4_disable" not in operator.asked
    assert leftovers(controller) == []
    assert_nothing_forbidden(controller, server)


async def test_t4_never_writes_the_real_card_enabled_with_its_profile():
    """Se SetCredential non sospende, la tessera vera con il profilo che apre non si scrive nemmeno: basta la tessera
    di prova, con un numero del kit."""
    controller = IgnoresEnabled.factory()
    controller.serial = SERIAL
    operator = SwipeWatcher(controller)
    seen: list[tuple[str, str]] = []
    original = controller.set_credential

    async def watching(credential):
        token = await original(credential)
        seen.extend(opening_cards(controller))
        return token

    controller.set_credential = watching
    bench, server, _ = make_kit(controller, operator)

    await kit.esegui(bench, ["T4", "T8"])

    assert seen == []
    assert leftovers(controller) == []


class RefusesTheProbeCard(FakeController):
    """SetCredential della tessera di prova rifiutata per un motivo che con la sospensione non c'entra."""

    async def set_credential(self, credential):
        if str(credential.get("token") or "").endswith("_prova_sospesa"):
            raise refused("ter:InvalidArgVal")
        return await super().set_credential(credential)


async def test_probe_card_not_written_is_not_an_answer_about_suspension():
    """La tessera di prova non scritta (rifiuto, tempo scaduto) non dice che SetCredential non sospende: la prova
    resta non conclusa e il passaggio non si chiede."""
    controller = RefusesTheProbeCard.factory()
    controller.serial = SERIAL
    operator = SwipeWatcher(controller)
    bench, server, _ = make_kit(controller, operator)

    data = await kit.esegui(bench, ["T4", "T8"])

    notes = {code: "\n".join(test["osservazioni"]) for code, test in data["prove"].items()}
    assert "sospesa con SetCredential: passaggio non fatto (prova della sospensione non conclusa:" in notes["T4"]
    assert "credenziale disabilitata: passaggio non fatto (prova della sospensione non conclusa:" in notes["T8"]
    assert "SetCredential non sospende" not in notes["T4"] + notes["T8"]
    assert operator.opening == [] and leftovers(controller) == []


async def configured_bench():
    """Banco con il lettore configurato dall'operatore, un utente e un profilo del kit che apre il punto del banco."""
    controller = factory_bench()
    operator = BenchOperator(controller)
    operator.chiedi("configura_lettore", "")
    bench, server, _ = make_kit(controller, operator)
    await bench.crea("utente", bench.modello("utente", f"{PREFIX}g_utente"))
    profile = bench.modello("profilo", f"{PREFIX}g_profilo", {"fascia": "standard_always", "punti": ["accesso_banco"]})
    await bench.crea("profilo", profile)
    operator.asked.clear()
    return controller, operator, bench


async def test_swipe_is_not_asked_when_a_kit_card_could_open():
    controller, operator, bench = await configured_bench()
    card = bench.modello(
        "tessera",
        f"{PREFIX}g_tessera",
        {"utente": f"{PREFIX}g_utente", "profilo": f"{PREFIX}g_profilo"},
        IdData=[{"Name": "Card", "Value": CARD.upper()}],
    )
    await bench.crea("tessera", card)

    assert await bench.passaggio("caso", "Passa la tessera.", CARD, "accesso_banco") is None

    assert operator.asked == []
    assert controller.credentials[f"{PREFIX}g_tessera"]["Enabled"] is False
    assert "ATTENZIONE, la tessera aam_prova_g_tessera" in "\n".join(bench.dati["avvio"]["osservazioni"])

    # DisableCredential rifiutato: la tessera si toglie
    await bench.crea("tessera", card)
    controller.refuse("disable_credential", "env:Receiver ter:ActionNotSupported")
    assert await bench.passaggio("caso", "Passa la tessera.", CARD, "accesso_banco") is None
    assert operator.asked == [] and f"{PREFIX}g_tessera" not in controller.credentials

    # Né sospesa né tolta: la prova si ferma
    await bench.crea("tessera", card)
    controller.refuse("remove_credential", "env:Receiver ter:ActionNotSupported")
    with pytest.raises(RuntimeError, match="né sospesa né tolta"):
        await bench.passaggio("caso", "Passa la tessera.", CARD, "accesso_banco")
    assert operator.asked == []
    await bench.client.aclose()


async def test_swipe_is_not_asked_with_a_foreign_card_or_without_reading():
    controller, operator, bench = await configured_bench()
    # Tessere o profili non letti: non si sa, il passaggio non si chiede
    controller.refuse("list_credentials", "env:Receiver ter:ActionNotSupported", times=1)
    assert await bench.passaggio("caso", "Passa la tessera.", CARD, "accesso_banco") is None
    card = bench.modello(
        "tessera",
        f"{PREFIX}g_tessera",
        {"utente": f"{PREFIX}g_utente", "profilo": f"{PREFIX}g_profilo"},
        IdData=[{"Name": "Card", "Value": CARD}],
    )
    await bench.crea("tessera", card)
    for method in ("get_credentials", "get_access_profiles"):
        controller.refuse(method, "env:Receiver ter:ActionNotSupported", times=1)
        assert await bench.passaggio("caso", "Passa la tessera.", CARD, "accesso_banco") is None
    assert operator.asked == []
    await bench.tenta("remove_credential", f"{PREFIX}g_tessera")
    # Una tessera che non è del kit e apre: il kit non la tocca e si ferma
    controller.profiles["profilo_vero"] = {
        "token": "profilo_vero",
        "Name": "Vero",
        "AccessPolicy": [{"AccessPoint": "accesso_banco", "Schedule": ["standard_always"]}],
    }
    foreign = controller.add_existing("utente_vero", "Vero", CARD)
    controller.credentials[foreign]["CredentialAccessProfile"] = [{"AccessProfile": "profilo_vero"}]
    with pytest.raises(RuntimeError, match="può ancora aprire"):
        await bench.passaggio("caso", "Passa la tessera.", CARD, "accesso_banco")
    assert operator.asked == []
    assert not [c for c in controller.calls if foreign in c]
    assert "non è del kit, non la tocco" in "\n".join(bench.dati["avvio"]["osservazioni"])
    await bench.client.aclose()


async def test_swipe_is_asked_with_a_suspended_card_or_one_that_opens_nothing():
    controller, operator, bench = await configured_bench()
    for profile in ("", f"{PREFIX}g_profilo"):
        card = bench.modello(
            "tessera",
            f"{PREFIX}g_tessera",
            {"utente": f"{PREFIX}g_utente", "profilo": profile},
            IdData=[{"Name": "Card", "Value": CARD}],
            Enabled=bool(not profile),
        )
        await bench.crea("tessera", card)
        assert await bench.passaggio("caso", "Passa la tessera.", CARD, "accesso_banco") is not None
    assert operator.asked == ["caso", "caso"]
    await bench.client.aclose()


# T18: limiti con CapabilityViolated e pulsante di uscita durante la scrittura


class LimitedController(FakeController):
    """A1210 finto pieno oltre capacity: risponde CapabilityViolated. utenti, tessere, profili, fasce e punti contano
    gli oggetti in tutto (anche quelli che non sono del kit), eccezioni quelle di una fascia, regole quelle di un
    profilo. Tiene i punti di accesso citati dalle regole dei profili scritti."""

    def __init__(self) -> None:
        super().__init__()
        self.capacity: dict[str, int] = {}
        self.policy_points: set[str] = set()

    def _full(self, name: str, exists: bool, count: int) -> None:
        if not exists and count >= self.capacity.get(name, 10**9):
            raise refused("ter:CapabilityViolated")

    async def set_user(self, user):
        self._full("utenti", user["token"] in self.users, len(self.users))
        return await super().set_user(user)

    async def set_credential(self, credential):
        self._full("tessere", credential.get("token") in self.credentials, len(self.credentials))
        return await super().set_credential(credential)

    async def set_access_profile(self, profile):
        self._full("profili", profile.get("token") in self.profiles, len(self.profiles))
        policies = profile.get("AccessPolicy") or []
        if len(policies) > self.capacity.get("regole", 10**9):
            raise refused("ter:CapabilityViolated")
        self.policy_points.update(p.get("AccessPoint") for p in policies)
        return await super().set_access_profile(profile)

    async def set_schedule(self, schedule):
        self._full("fasce", schedule.get("token") in self.schedules, len(self.schedules))
        exceptions = str(schedule.get("ExceptionScheduleDefinition") or "").count("BEGIN:VEVENT")
        if exceptions > self.capacity.get("eccezioni", 10**9):
            raise refused("ter:CapabilityViolated")
        return await super().set_schedule(schedule)

    async def create_access_point(self, access_point):
        exists = any(a["token"] == access_point.get("token") for a in self.access_points)
        self._full("punti", exists, len(self.access_points))
        return await super().create_access_point(access_point)


class InterruptedSearch(LimitedController):
    """Durante il dimezzamento delle eccezioni un altro rifiuto: il limite resta solo un intervallo."""

    async def set_schedule(self, schedule):
        exceptions = str(schedule.get("ExceptionScheduleDefinition") or "").count("BEGIN:VEVENT")
        if schedule.get("token") == f"{PREFIX}t18_lim_eccezioni" and 25 < exceptions < 50:
            raise refused("ter:InvalidArgVal")
        return await super().set_schedule(schedule)


class ButtonOperator(BenchOperator):
    """L'operatore al banco durante T18: tiene ogni domanda con le scritture fatte fino a lì, e alla fine del blocco
    il registro ha un evento del pulsante."""

    def __init__(self, controller: FakeController) -> None:
        super().__init__(controller)
        self.prompts: list[tuple[str, str, int]] = []
        self.cards_at_button: list[str] = []

    def chiedi(self, chiave: str, testo: str) -> str:
        c = self.controller
        self.prompts.append((chiave, testo, len(c.calls)))
        if chiave == "pulsante_esito":
            # Le tessere scritte mentre l'operatore premeva il pulsante
            self.cards_at_button = [v["IdData"][0]["Value"] for v in c.credentials.values()]
            row = max((e["rowid"] for e in c.log_events), default=0)
            keys = [{"Key": f"topic{i}", "Value": t} for i, t in enumerate(["Door", "Status", "Unlocked"])]
            c.log_events.append({"rowid": row + 1, "UtcTime": "2026-11-07T10:00:00Z", "KeyValues": keys})
            return "sì, subito"
        return super().chiedi(chiave, testo)


SMALL_CAPS = {"utenti": 100, "tessere": 100, "profili": 100, "fasce": 100, "eccezioni": 100, "regole": 50}


async def test_t18_without_limits_says_what_was_not_tested():
    controller = factory_bench()
    operator = ButtonOperator(controller)
    bench, server, _ = make_kit(controller, operator, quante=5)

    data = await kit.esegui(bench, ["T18"])

    t18 = data["prove"]["T18"]
    notes = "\n".join(t18["osservazioni"])
    assert (
        "T18 parziale: solo i tempi. NON provati i limiti con CapabilityViolated (tessere, utenti, profili, fasce, "
        "eccezioni per fascia, regole per profilo) né le aperture con il pulsante durante la scrittura: si provano "
        "con --limiti"
    ) in notes
    assert t18["limiti"] == {"provati": False}
    assert not [p for p in operator.prompts if p[0].startswith("pulsante")]
    assert leftovers(controller) == []


async def test_t18_limits_until_capability_violated(tmp_path):
    controller = LimitedController.factory()
    controller.serial = SERIAL
    controller.capacity = {"utenti": 30, "tessere": 45, "profili": 12, "fasce": 15, "punti": 9, "eccezioni": 37}
    controller.capacity["regole"] = 7
    operator = ButtonOperator(controller)
    bench, server, lines = make_kit(controller, operator, quante=5, tmp_path=tmp_path, limiti=True, tetti=SMALL_CAPS)

    data = await kit.esegui(bench, ["T18"])

    t18 = data["prove"]["T18"]
    assert t18["esito"] == "fatta"
    results = t18["limiti"]["risultati"]
    assert {name: (r["esito"], r["limite"]) for name, r in results.items()} == {
        "utenti": ("CapabilityViolated", 30),
        "tessere": ("CapabilityViolated", 45),
        "profili": ("CapabilityViolated", 12),
        # Le due fasce di serie contano
        "fasce": ("CapabilityViolated", 15),
        "eccezioni": ("CapabilityViolated", 37),
        # Il punto di accesso della porta del banco, configurato dall'operatore per il pulsante, conta
        "punti": ("CapabilityViolated", 9),
        "regole": ("CapabilityViolated", 7),
    }
    assert results["fasce"]["accettati"] == 13 and results["fasce"]["gia_presenti"] == 2
    assert results["eccezioni"]["esatto"] and results["regole"]["esatto"]
    assert results["tessere"]["utenti"] == 30
    notes = "\n".join(t18["osservazioni"])
    assert "utenti: CapabilityViolated al 31°: entrano 30 aam_prova_" in notes
    assert "fasce: CapabilityViolated al 14°: entrano 13 aam_prova_ più 2 già presenti" in notes
    assert "eccezioni: CapabilityViolated con 38: entrano 37" in notes
    assert (
        "limiti per hardware.LIMITS: utenti 30, tessere 45, profili 12, fasce 15, eccezioni 37, punti 9, regole 7"
    ) in notes
    # Il pulsante: chiesto prima del primo blocco di tessere, con la risposta dopo; scritture in mezzo, nessuna
    # tessera da passare e nessuna tessera con il numero vero sul controller
    keys = [p[0] for p in operator.prompts]
    assert keys.count("pulsante_pronto") == 1 and keys.count("pulsante_esito") == 1
    ready = next(p for p in operator.prompts if p[0] == "pulsante_pronto")
    answer = next(p for p in operator.prompts if p[0] == "pulsante_esito")
    assert answer[2] - ready[2] == 10
    assert "PULSANTE DI USCITA" in ready[1] and "NON passare tessere" in ready[1]
    assert not [p for p in operator.prompts if p[1].startswith("Passa")]
    assert operator.cards_at_button and CARD not in operator.cards_at_button
    assert t18["limiti"]["pulsante"]["risposta"] == "sì, subito"
    assert "«sì, subito»; eventi intanto: 1 (Door/Status/Unlocked)" in notes
    # Le regole dei profili del kit citano solo punti di accesso del kit
    assert controller.policy_points and all(p.startswith(PREFIX) for p in controller.policy_points)
    # Tutto tolto, dopo ogni ricerca e alla fine
    assert [r["dopo"] for r in t18["limiti"]["rimozioni"]] == [
        "i tempi",
        "utenti e tessere",
        "profili",
        "fasce",
        "eccezioni",
        "regole",
    ]
    assert all(r["non_tolti"] == [] for r in t18["limiti"]["rimozioni"])
    assert leftovers(controller) == []
    assert_nothing_forbidden(controller, server)
    # Nel rapporto delle migliaia di scritture restano per intero solo quelle non riuscite, come il CapabilityViolated
    saved = json.loads((tmp_path / "rapporto.json").read_text())
    operations = saved["prove"]["T18"]["operazioni"]
    refusals = [o for o in operations if o["esito"] == "errore" and o["metodo"].startswith(("set_", "create_"))]
    assert len(refusals) >= 7 and all("CapabilityViolated" in json.dumps(o["http"]) for o in refusals)
    assert not [o for o in operations if o["metodo"] == "set_user" and o["esito"] == "riuscita" and "http" in o][5:]


async def test_t18_limits_stop_at_the_caps_without_operator():
    controller = factory_bench()
    caps = dict.fromkeys(kit.TETTI, 12)
    bench, server, _ = make_kit(controller, quante=3, limiti=True, tetti=caps)

    data = await kit.esegui(bench, ["T18"])

    t18 = data["prove"]["T18"]
    results = t18["limiti"]["risultati"]
    assert {name: r["esito"] for name, r in results.items()} == dict.fromkeys(
        ["utenti", "tessere", "profili", "fasce", "eccezioni", "punti", "regole"], "tetto"
    )
    notes = "\n".join(t18["osservazioni"])
    assert "utenti: tetto di 12 raggiunto senza CapabilityViolated: entrano almeno 12 aam_prova_" in notes
    assert "fasce almeno 14" in notes and "regole almeno 12" in notes
    assert "aperture con il pulsante durante la scrittura non provate: serve l'operatore" in notes
    assert leftovers(controller) == []
    assert_nothing_forbidden(controller, server)


async def test_t18_limits_other_errors_and_the_controller_that_stops_answering():
    controller = LimitedController.factory()
    controller.serial = SERIAL
    controller.capacity = {"eccezioni": 30}
    # Un rifiuto che non è un limite: la ricerca dei profili si ferma e le altre vanno avanti
    controller.refuse("set_access_profile", "env:Sender ter:InvalidArgVal", times=6)
    bench, server, _ = make_kit(controller, quante=2, limiti=True, tetti=dict.fromkeys(kit.TETTI, 40))

    data = await kit.esegui(bench, ["T18"])

    results = data["prove"]["T18"]["limiti"]["risultati"]
    # Il primo rifiuto è il profilo senza regole delle tessere (scritte senza profilo), poi i profili
    assert results["tessere"]["con_profilo"] is False and results["tessere"]["esito"] == "tetto"
    assert results["profili"]["esito"] == "altro errore" and results["profili"]["accettati"] == 0
    assert results["eccezioni"]["limite"] == 30
    assert leftovers(controller) == []

    # Il controller smette di rispondere a metà degli utenti: la prova si ferma e la pulizia dice cosa resta
    controller.fail_after(25)
    bench, server, _ = make_kit(controller, quante=2, limiti=True, tetti=dict.fromkeys(kit.TETTI, 40))
    data = await kit.esegui(bench, ["T18"])
    t18 = data["prove"]["T18"]
    assert t18["esito"] == "errore"
    assert t18["limiti"]["risultati"]["utenti"]["esito"] == "controller non risponde"
    assert "profili" not in t18["limiti"]["risultati"]
    assert data["fine"]["pulizia"]["non_tolti"]
    # Il giro dopo toglie quello che era rimasto
    controller.heal()
    bench, server, _ = make_kit(controller)
    await kit.esegui(bench, ["T11"])
    assert leftovers(controller) == []


async def test_t18_limits_cleaned_up_when_interrupted():
    controller = factory_bench()
    operator = ButtonOperator(controller)
    bench, server, lines = make_kit(controller, operator, quante=2, limiti=True, tetti=SMALL_CAPS)

    def interrupt(chiave, testo):
        if chiave == "pulsante_esito":
            raise KeyboardInterrupt
        return ButtonOperator.chiedi(operator, chiave, testo)

    operator.chiedi = interrupt
    with pytest.raises(KeyboardInterrupt):
        await kit.esegui(bench, ["T18"])
    assert bench.dati["prove"]["T18"]["esito"] == "interrotta"
    assert leftovers(controller) == []
    assert "sul controller non resta niente del kit" in "\n".join(lines)


def test_limits_options_on_the_command_line():
    args = kit.argomenti(["--host", "x", "--serie", "y", "--limiti", "--tetto", "tessere=5000", "--tetto", "regole=3"])
    assert args.limiti is True and dict(args.tetto) == {"tessere": 5000, "regole": 3}
    args = kit.argomenti(["--host", "x", "--serie", "y"])
    assert args.limiti is False and args.tetto == []
    for wrong in ("porte=3", "tessere=0", "tessere", "tessere=molte"):
        with pytest.raises(SystemExit):
            kit.argomenti(["--host", "x", "--serie", "y", "--tetto", wrong])
    bench = kit.Kit(None, kit.Registratore(None), "y", kit.SenzaOperatore(), tetti={"tessere": 5000})
    assert bench.tetti == {**kit.TETTI, "tessere": 5000}


async def test_t18_limit_found_only_as_a_range_is_not_given_as_exact():
    controller = InterruptedSearch.factory()
    controller.serial = SERIAL
    controller.capacity = {"eccezioni": 37}
    caps = dict.fromkeys(kit.TETTI, 100) | {"eccezioni": 250}
    bench, server, _ = make_kit(controller, ButtonOperator(controller), quante=2, limiti=True, tetti=caps)

    data = await kit.esegui(bench, ["T18"])

    result = data["prove"]["T18"]["limiti"]["risultati"]["eccezioni"]
    assert (result["esito"], result["limite"], result["rifiutate"], result["esatto"]) == (
        "CapabilityViolated",
        25,
        50,
        False,
    )
    notes = "\n".join(data["prove"]["T18"]["osservazioni"])
    assert "eccezioni almeno 25 (tra 25 e 50)" in notes
    assert leftovers(controller) == []


# --impianto-in-servizio: l'A1210 del Teatro Eliseo, in servizio, con il tecnico sul posto e l'add-on fermo

# Quello che sull'impianto può partire, scritto qui a parte e non preso dal kit: letture, e scritture e rimozioni di
# utenti, tessere, profili e fasce
WRITES_IN_SERVICE = {
    "axudb:SetUser",
    "axudb:RemoveUser",
    "pacsaxis:SetCredential",
    "pacsaxis:RemoveCredential",
    "pacsaxis:EnableCredential",
    "pacsaxis:DisableCredential",
    "pacsaxis:SetAccessProfile",
    "pacsaxis:RemoveAccessProfile",
    "axsch:SetSchedule",
    "axsch:RemoveSchedule",
}
ALLOWED_IN_SERVICE = WRITES_IN_SERVICE | {
    "getAllProperties",
    "getDateTimeInfo",
    "getNTPInfo",
    "getClientStatus",
    "getEventPublicationConfig",
    "axudb:GetUserList",
    "axudb:GetUser",
    "pacsaxis:GetCredentialList",
    "pacsaxis:GetCredential",
    "pacsaxis:GetAccessPointList",
    "pacsaxis:GetAccessPoint",
    "pacsaxis:GetAccessProfileList",
    "pacsaxis:GetAccessProfile",
    "pacsaxis:GetAuthenticationProfileList",
    "pacsaxis:GetAccessControllerList",
    "axsch:GetScheduleList",
    "axtdc:GetDoorList",
    "axtdc:GetDoor",
    "axtdc:GetDoorConfigurationList",
    "axtid:GetIdPointList",
    "axtid:GetIdPoint",
    "axtid:GetIdPointConfigurationList",
    "axisio:GetIoInfoList",
    "axisio:GetIoAssignmentList",
    "axlog:FetchEvents",
}
FAKE_WRITES_IN_SERVICE = {
    "set_user",
    "set_credential",
    "set_schedule",
    "set_access_profile",
    "remove_user",
    "remove_credential",
    "remove_schedule",
    "remove_access_profile",
    "enable_credential",
    "disable_credential",
}
REAL_CARDS = {"utente_anna": "04a1b2c3", "utente_luca": "04d4e5f6", "utente_marco": "0400aa11"}
SERVICE_TESTS = [code for code in kit.ORDINE if code != "T12"]
# Tutto lo stato del finto che il kit non deve cambiare
STATE = (
    "users",
    "credentials",
    "profiles",
    "schedules",
    "doors",
    "door_configs",
    "idpoints",
    "idpoint_configs",
    "access_points",
    "access_controllers",
    "authentication_profiles",
    "io_assignments",
    "time_zone",
    "mqtt",
    "mqtt_events",
)
ANNA_CHANGED = 'utenti · utente_anna: Name prima "Anna Bianchi", dopo "Anna B."'


class EliseoController(FakeController):
    """L'A1210 del Teatro Eliseo come domattina: quello del banco programmato (FakeController.bench(): porte,
    lettori, punto di accesso gestito, configurazione della porta, fasce) con 3 utenti e 3 tessere vere, i profili H24,
    Manutentore e Solo Teatro, gli ingressi programmati della porta, la configurazione del lettore, NTP e MQTT con la
    password. Ora, sincronizzazione NTP e stato del collegamento MQTT cambiano a ogni lettura. La configurazione dei
    lettori qui si legge (i backup dell'add-on la leggono): se sull'impianto non si leggesse il kit non partirebbe."""

    def __init__(self) -> None:
        super().__init__()
        self.serial = SERIAL
        self._ticks = itertools.count()
        teatro = schedule_ical([{"days": DAYS, "start": "17:00", "end": "24:00"}])
        self.schedules["fascia_teatro"] = {
            "token": "fascia_teatro",
            "Name": "Orario teatro",
            "Description": "",
            "ScheduleDefinition": teatro,
        }
        for token, name, schedule in (
            ("profilo_h24", "H24", "standard_always"),
            ("profilo_manutentore", "Manutentore", "standard_always"),
            ("profilo_solo_teatro", "Solo Teatro", "fascia_teatro"),
        ):
            self.profiles[token] = {
                "token": token,
                "Name": name,
                "Description": "",
                "Enabled": True,
                "Schedule": [schedule],
                "AuthenticationProfile": [],
                "AccessPolicy": [
                    {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Attribute": [], "Schedule": [schedule]}
                ],
            }
        names = {"utente_anna": "Anna Bianchi", "utente_luca": "Luca Verdi", "utente_marco": "Marco Neri"}
        profiles = {
            "utente_anna": "profilo_h24",
            "utente_luca": "profilo_manutentore",
            "utente_marco": "profilo_solo_teatro",
        }
        for user, card in REAL_CARDS.items():
            token = self.add_existing(user, names[user], card)
            self.credentials[token]["CredentialAccessProfile"] = [
                {"AccessProfile": profiles[user], "ValidFrom": "", "ValidTo": ""}
            ]
        # Ingressi programmati della porta: aperture a orario nella configurazione della porta
        self.door_configs["porta"]["Configuration"].append({"Name": "Schedule.Open", "Value": "fascia_teatro"})
        self.idpoint_configs = {
            "lettore-1": {
                "token": "lettore-1",
                "DeviceUUID": "uuid-locale",
                "Configuration": [{"Name": "Reader.Protocol", "Value": "OSDP"}],
            }
        }
        self.mqtt = {
            "server": {"host": "10.103.200.5", "port": 1883, "protocol": "tcp"},
            "username": "a1210",
            "password": "mqtt-segreta",
            "clientId": "a1210-eliseo",
        }
        self.mqtt_events = {"topicPrefix": "custom", "customTopicPrefix": "accessi/eliseo"}

    async def list_idpoint_configurations(self, *, strict=False):
        return [copy.deepcopy(c) for c in self.idpoint_configs.values()]

    async def date_time_info(self):
        tick = next(self._ticks)
        return {
            "dateTime": f"2026-11-13T07:{tick // 60 % 60:02d}:{tick % 60:02d}Z",
            "localDateTime": f"2026-11-13T08:{tick // 60 % 60:02d}:{tick % 60:02d}",
            "timeZone": self.time_zone,
            "posixTimeZone": "CET-1CEST,M3.5.0,M10.5.0/3",
            "dstEnabled": True,
        }

    async def ntp_info(self):
        tick = next(self._ticks)
        return {
            "client": {
                "enabled": True,
                "serversSource": "static",
                "staticServers": ["it.pool.ntp.org"],
                "advertisedServers": ["10.103.200.1"] if tick % 2 else [],
                "synced": bool(tick % 2),
                "timeOffset": tick / 1000,
                "timeToNextSync": 64 - tick % 64,
            }
        }

    async def mqtt_client_config(self):
        connected = "connected" if next(self._ticks) % 3 else "disconnected"
        return {"config": copy.deepcopy(self.mqtt), "status": {"state": "active", "connectionStatus": connected}}


class BreaksARealUser(EliseoController):
    """Firmware con un difetto: scrivendo un utente del kit cambia anche il nome di un utente vero."""

    async def set_user(self, user):
        await super().set_user(user)
        if user["token"].startswith(PREFIX):
            self.users["utente_anna"]["Name"] = "Anna B."


class ZoneChangesByItself(EliseoController):
    """Il fuso cambia mentre il kit scrive una fascia (qualcuno dalla pagina del controller?): il kit non scrive mai
    impostazioni, quindi va avanti, ma il confronto finale lo riporta."""

    async def set_schedule(self, schedule):
        self.time_zone = "UTC"
        return await super().set_schedule(schedule)


class Technician(BenchOperator):
    """Il tecnico sul posto: conferma, stacca e riattacca il cavo e passa la tessera di scorta; a ogni passaggio
    guarda se la porta dell'impianto si aprirebbe. Se il kit gli chiedesse di configurare un lettore direbbe salta."""

    def __init__(self, controller: FakeController, **answers: str) -> None:
        super().__init__(controller)
        self.answers = {"addon_fermo": "sì", "tecnico_sul_posto": "Sì"} | answers
        self.opening: list[tuple[str, str]] = []
        self.swipes = 0

    def chiedi(self, chiave: str, testo: str) -> str:
        if chiave in self.answers:
            self.asked.append(chiave)
            return self.answers[chiave]
        if chiave == "configura_lettore":
            self.asked.append(chiave)
            return "salta"
        return super().chiedi(chiave, testo)

    def swipe(self) -> None:
        self.swipes += 1
        self.opening += opening_cards(self.controller, "ap-in")
        super().swipe()


def state(controller: FakeController) -> dict:
    return copy.deepcopy({name: getattr(controller, name) for name in STATE})


def assert_nothing_forbidden_in_service(controller: FakeController, server: VapixServer) -> None:
    assert set(server.methods) <= ALLOWED_IN_SERVICE, set(server.methods) - ALLOWED_IN_SERVICE
    writes = [m for m in server.methods if not re.match(r"(\w+:)?(Get|Fetch|get)", m)]
    assert set(writes) <= WRITES_IN_SERVICE
    assert {c[0] for c in controller.calls} <= FAKE_WRITES_IN_SERVICE, controller.calls
    assert all(str(c[1]).startswith(PREFIX) for c in controller.calls), controller.calls


def service_kit(controller, operator=None, **options):
    operator = operator or Technician(controller)
    options.setdefault("quante", 100)
    bench, server, lines = make_kit(controller, operator, servizio=True, **options)
    return bench, server, lines, operator


@pytest.mark.parametrize("check_references", [False, True], ids=["riferimenti_liberi", "riferimenti_controllati"])
async def test_service_mode_runs_everything_and_leaves_the_installation_as_it_was(tmp_path, check_references):
    controller = EliseoController.bench()
    controller.check_references = check_references
    before = state(controller)
    bench, server, lines, operator = service_kit(controller, tmp_path=tmp_path)

    data = await kit.esegui(bench, list(SERVICE_TESTS))

    assert {code: test["esito"] for code, test in data["prove"].items()} == dict.fromkeys(SERVICE_TESTS, "fatta")
    # Alla fine, dopo la pulizia: tutto riletto e identico, il rapporto finisce con il verdetto
    assert data["impianto_in_servizio"]["verdetto"] == "IMPIANTO INVARIATO"
    assert data["verdetto_impianto"]["differenze"] == [] and data["verdetto_impianto"]["non_riletti"] == []
    assert data["verdetto_impianto"]["rimasti_del_kit"] == []
    assert lines[-1] == "\nIMPIANTO INVARIATO"
    # Lo dice anche il finto, indipendentemente dalla fotografia del kit: niente di quello che c'era è cambiato
    assert state(controller) == before
    assert leftovers(controller) == []
    assert data["fine"]["pulizia"]["non_tolti"] == []
    assert_nothing_forbidden_in_service(controller, server)
    # Dopo ogni prova l'impianto riletto è uguale
    assert all(test["impianto"]["differenze"] == [] for test in data["prove"].values())
    # Il tecnico: cavo staccato e riattaccato, tessera di scorta passata, mai una tessera che apre la porta vera, mai
    # un lettore da configurare
    assert {"addon_fermo", "tecnico_sul_posto", "stacca_cavo", "riattacca_cavo"} <= set(operator.asked)
    assert "configura_lettore" not in operator.asked
    assert operator.swipes >= 3 and operator.opening == []
    notes = {code: "\n".join(test["osservazioni"]) for code, test in data["prove"].items()}
    assert "sospesa con SetCredential: IdData arrivato" in notes["T4"]
    assert "credenziale disabilitata: IdData arrivato" in notes["T8"]
    assert "profilo acceso senza regole" in notes["T8"]
    assert notes["T11"].count("«non fattibile in servizio»") == len(kit.ELENCHI_T11)
    assert "scritte 100 tessere" in notes["T18"]
    assert "non provati sull'impianto in servizio: porta, lettore, punto" in notes["T1"]
    for code in ("T1", "T2", "T3", "T6", "T7", "T10"):
        methods = [h["metodo"] for op in data["prove"][code]["operazioni"] for h in op.get("http") or []]
        assert not [m for m in methods if m.startswith(("axtdc:", "axtid:")) and "Get" not in m], (code, methods)
    # Il rapporto dice in testa dove e quali prove, con le escluse; le password non ci sono
    text = (tmp_path / "rapporto.json").read_text()
    saved = json.loads(text)
    assert list(saved)[:2] == ["kit", "impianto_in_servizio"]
    head = saved["impianto_in_servizio"]
    assert "IMPIANTO IN SERVIZIO" in head["avviso"]
    assert head["prove"] == SERVICE_TESTS and head["non_chieste"] == []
    assert "T12" in head["escluse"] and "T18 con --limiti" in head["escluse"]
    assert head["conferme"] == {"addon_fermo": "sì", "tecnico_sul_posto": "Sì"}
    assert head["verdetto"] == "IMPIANTO INVARIATO"
    assert "segreta" not in text
    photo = saved["fotografie"]["prima"]["letti"]
    assert photo["mqtt"]["config"]["password"] == "<nascosta>"
    assert {u["token"] for u in photo["utenti"]} == set(REAL_CARDS)
    assert set(photo) == set(kit.ELENCHI_IMPIANTO) | {"morsetti", "assegnazioni_io"} | set(kit.DISPOSITIVO_IMPIANTO)
    assert saved["fotografie"]["dopo"]["non_letti"] == {}


async def test_without_the_service_option_the_kit_still_refuses_a_controller_in_service():
    controller = EliseoController.bench()
    bench, server, _ = make_kit(controller, Technician(controller))

    with pytest.raises(kit.Rifiuto, match="utente_anna.*--impianto-in-servizio"):
        await kit.esegui(bench, list(SERVICE_TESTS))
    assert controller.calls == []
    assert not [m for m in server.methods if "Set" in m or "Remove" in m]


@pytest.mark.parametrize(
    "options, prove, match",
    [
        ({}, ["T12"], "non si fa T12"),
        ({}, ["T1", "T12"], "non si fa T12"),
        ({"limiti": True}, ["T18"], "--limiti"),
        ({"tetti": {"tessere": 5}}, ["T18"], "--tetto"),
        ({"quante": 501}, ["T18"], "da 1 a 500"),
    ],
    ids=["T12", "T12_con_altre", "limiti", "tetto", "troppe_tessere"],
)
async def test_service_mode_refuses_t12_limits_and_the_rest_before_any_request(options, prove, match):
    controller = EliseoController.bench()
    bench, server, lines, operator = service_kit(controller, **options)

    with pytest.raises(kit.Rifiuto, match=match):
        await kit.esegui(bench, prove)
    assert server.methods == [] and controller.calls == [] and operator.asked == []


async def test_service_mode_needs_both_confirmations_and_an_operator():
    for answers in ({"addon_fermo": "no"}, {"tecnico_sul_posto": ""}, {"addon_fermo": "forse"}):
        controller = EliseoController.bench()
        bench, server, _, _ = service_kit(controller, Technician(controller, **answers))
        with pytest.raises(kit.Rifiuto, match="manca la conferma"):
            await kit.esegui(bench, ["T7"])
        assert server.methods == [] and controller.calls == []
    controller = EliseoController.bench()
    bench, server, _, _ = service_kit(controller, kit.SenzaOperatore())
    with pytest.raises(kit.Rifiuto, match="--non-interattivo"):
        await kit.esegui(bench, ["T7"])
    assert server.methods == []


async def test_service_mode_refuses_a_spare_card_that_is_already_on_the_controller():
    for number in ("04a1b2c3", "04A1B2C3"):
        controller = EliseoController.bench()
        bench, server, _, _ = service_kit(controller, tessera=number)
        with pytest.raises(kit.Rifiuto, match="già sul controller.*utente_anna.*tessera di scorta mai registrata"):
            await kit.esegui(bench, ["T4", "T8"])
        assert controller.calls == []
        assert not [m for m in server.methods if "Set" in m or "Remove" in m]
    # Scritta dal tecnico quando il kit la chiede: non la usa, i passaggi non si fanno
    controller = EliseoController.bench()
    operator = Technician(controller, numero_tessera="04d4e5f6")
    bench, server, _, _ = service_kit(controller, operator, tessera="")
    data = await kit.esegui(bench, ["T8"])
    assert data["prove"]["T8"]["esito"] == "saltata"
    assert "già sul controller" in "\n".join(data["prove"]["T8"]["osservazioni"])
    assert operator.swipes == 0 and leftovers(controller) == []
    assert data["impianto_in_servizio"]["verdetto"] == "IMPIANTO INVARIATO"


async def test_a_real_object_changed_by_mistake_stops_the_kit_with_installation_changed(tmp_path):
    controller = BreaksARealUser.bench()
    bench, server, lines, _ = service_kit(controller, tmp_path=tmp_path)

    with pytest.raises(kit.ImpiantoCambiato, match="dopo T5"):
        await kit.esegui(bench, ["T5", "T7", "T16"])

    data = bench.dati
    # Il kit si è fermato dopo T5, ha tolto quello che aveva creato e ha confrontato tutto
    assert list(data["prove"]) == ["T5"]
    assert leftovers(controller) == []
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO CAMBIATO"
    assert data["verdetto_impianto"]["righe"] == [ANNA_CHANGED]
    assert data["verdetto_impianto"]["differenze"][0]["prima"]["Name"] == "Anna Bianchi"
    assert lines[-2:] == ["\nATTENZIONE: IMPIANTO CAMBIATO", f"  {ANNA_CHANGED}"]
    assert ANNA_CHANGED in "\n".join(data["prove"]["T5"]["osservazioni"])
    assert kit.codice_di_uscita(data, 2) == 3
    assert_nothing_forbidden_in_service(controller, server)


async def test_a_setting_the_kit_never_writes_changed_does_not_stop_the_tests_but_the_verdict_says_it():
    controller = ZoneChangesByItself.bench()
    bench, server, lines, _ = service_kit(controller)

    data = await kit.esegui(bench, ["T9", "T16"])

    assert {code: test["esito"] for code, test in data["prove"].items()} == {"T9": "fatta", "T16": "fatta"}
    assert "cambiate impostazioni del dispositivo o morsetti" in "\n".join(data["prove"]["T9"]["osservazioni"])
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO CAMBIATO"
    assert data["verdetto_impianto"]["righe"] == ['orologio: timeZone prima "Europe/Rome", dopo "UTC"']
    assert lines[-1] == '  orologio: timeZone prima "Europe/Rome", dopo "UTC"'
    assert kit.codice_di_uscita(data, 0) == 3
    assert leftovers(controller) == []


async def test_a_list_not_read_before_stops_the_kit_and_one_not_read_after_is_not_unchanged():
    controller = EliseoController.bench()
    controller.refuse("list_access_controllers", "env:Receiver ter:ActionNotSupported", times=1)
    bench, server, _, _ = service_kit(controller)
    with pytest.raises(kit.Rifiuto, match="non si leggono controllo_accessi"):
        await kit.esegui(bench, ["T7"])
    assert controller.calls == [] and not [m for m in server.methods if "Set" in m or "Remove" in m]

    controller = EliseoController.bench()
    bench, server, lines, _ = service_kit(controller)
    closing = bench.chiusura

    async def doors_unreadable_at_the_end():
        controller.refuse("list_doors", "env:Receiver ter:ActionNotSupported")
        await closing()

    bench.chiusura = doors_unreadable_at_the_end
    data = await kit.esegui(bench, ["T5"])
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO NON VERIFICATO"
    assert data["verdetto_impianto"]["non_riletti"][0].startswith("porte (")
    assert "NON RILETTO dopo le prove: porte" in "\n".join(data["fine"]["osservazioni"])
    assert lines[-1].startswith("  non riletti: porte")
    assert kit.codice_di_uscita(data, 0) == 4
    assert leftovers(controller) == []


async def test_in_service_doors_readers_points_access_controller_and_settings_never_leave():
    controller = EliseoController.bench()
    bench, server, _, _ = service_kit(controller)
    client = bench.client
    kit_user = {"token": f"{PREFIX}u", "Name": f"{PREFIX}u"}
    attempts = [
        client.set_door({"token": f"{PREFIX}porta", "Name": f"{PREFIX}porta"}),
        client.set_door({"token": "porta", "Name": "Ingresso scala", "AccessTime": "PT1S"}),
        client.remove_door(f"{PREFIX}porta"),
        client.set_door_configuration({"token": f"{PREFIX}porta", "Configuration": []}),
        client.create_idpoint({"token": f"{PREFIX}lettore", "Name": f"{PREFIX}lettore"}),
        client.remove_idpoint(f"{PREFIX}lettore"),
        client.set_idpoint_configuration(f"{PREFIX}lettore", "", {}),
        client.create_access_point({"token": f"{PREFIX}punto", "Name": f"{PREFIX}punto"}),
        client.remove_access_point(f"{PREFIX}punto"),
        client.set_access_controller({"token": "Axis-x AccessController", "AccessPoint": ["ap-in"]}),
        client.set_access_controller({"token": f"{PREFIX}controllo", "Name": f"{PREFIX}c", "AccessPoint": []}),
        client.set_authentication_profile({"token": f"{PREFIX}modo", "Name": f"{PREFIX}modo"}),
        client.set_io_assignment("REX", "in:pu/gnd", []),
        client.access_door("porta"),
        client.lock_door("porta"),
        client.set_time_zone("UTC"),
        client.set_ntp(True, "static", ["pool.ntp.org"]),
        client.set_mqtt_client({"host": "x"}),
        client.set_mqtt_events({"topicPrefix": "default"}),
        client._post("/vapix/doorcontrol", {"axtdc:RemoveDoorConfiguration": {"Token": [f"{PREFIX}porta"]}}),
        # Un metodo permesso mandato al percorso sbagliato
        client._post("/vapix/doorcontrol", {"axudb:SetUser": {"User": [kit_user]}}),
        # Utenti, tessere e profili del kit che citano oggetti veri, o con il numero di una tessera vera
        client.set_user({"token": "utente_anna", "Name": "Anna"}),
        client.set_credential(bench.modello("tessera", f"{PREFIX}t", {"utente": "utente_anna"})),
        client.set_credential(
            bench.modello("tessera", f"{PREFIX}t", {"utente": f"{PREFIX}u", "profilo": "profilo_h24"})
        ),
        client.set_credential(bench.modello("tessera", f"{PREFIX}t", IdData=[{"Name": "Card", "Value": "04A1B2C3"}])),
        client.set_access_profile(bench.modello("profilo", f"{PREFIX}p", {"punti": ["ap-in"]})),
        client.remove_user("utente_anna"),
        client.remove_credential("cred-1"),
    ]
    # La fotografia dà al guardiano i numeri delle tessere vere
    bench.permessi.numeri_veri = set(REAL_CARDS.values())
    for attempt in attempts:
        with pytest.raises(kit.Vietata):
            await attempt
    assert server.methods == [] and controller.calls == []
    # Anche dal kit: fermate prima di arrivare al client
    for method, args in (
        ("set_door", ({"token": f"{PREFIX}porta", "Name": f"{PREFIX}porta"},)),
        ("create_access_point", ({"token": f"{PREFIX}punto", "Name": f"{PREFIX}punto"},)),
        ("set_access_controller", ({"token": "Axis-x AccessController", "AccessPoint": []},)),
        ("remove_door", (f"{PREFIX}porta",)),
    ):
        with pytest.raises(kit.Vietata, match="impianto in servizio"):
            await bench.tenta(method, *args)
    with pytest.raises(kit.Vietata):
        await bench.grezza("/vapix/doorcontrol", "axtdc:RemoveDoor", {"Token": [f"{PREFIX}porta"]})
    assert server.methods == [] and controller.calls == []

    # La tessera di scorta: con un profilo che apre la porta vera solo sospesa, e mai riattivata
    bench.permessi.scorta, bench.permessi.punti_citabili = CARD, {"ap-in"}
    await client.set_user(bench.modello("utente", f"{PREFIX}u"))
    await client.set_access_profile(bench.modello("profilo", f"{PREFIX}apre", {"punti": ["ap-in"]}))
    spare = bench.modello(
        "tessera",
        f"{PREFIX}scorta",
        {"utente": f"{PREFIX}u", "profilo": f"{PREFIX}apre"},
        IdData=[{"Name": "Card", "Value": CARD.upper()}],
    )
    with pytest.raises(kit.Vietata, match="tessera di scorta"):
        await client.set_credential(spare)
    await client.set_credential({**spare, "Enabled": False, "Status": "Disabled"})
    with pytest.raises(kit.Vietata, match="tessera di scorta"):
        await client.enable_credential(f"{PREFIX}scorta")
    # Abilitata senza profili che aprono sì, ma poi nessuno dei suoi profili può citare la porta vera
    await client.set_access_profile(bench.modello("profilo", f"{PREFIX}vuoto"))
    links = [{"AccessProfile": f"{PREFIX}vuoto"}]
    await client.set_credential({**spare, "token": f"{PREFIX}scorta_2", "CredentialAccessProfile": links})
    with pytest.raises(kit.Vietata, match="tessera di scorta abilitata"):
        await client.set_access_profile(bench.modello("profilo", f"{PREFIX}vuoto", {"punti": ["ap-in"]}))
    assert opening_cards(controller, "ap-in") == []
    assert_nothing_forbidden_in_service(controller, server)
    await client.aclose()


async def test_a_forbidden_request_during_a_test_stops_the_kit_and_cleans_up():
    controller = EliseoController.bench()
    bench, server, lines, _ = service_kit(controller)

    async def t5_with_a_mistake():
        await bench.crea("utente", bench.modello("utente", f"{PREFIX}sbaglio"))
        await bench.client.set_door({"token": f"{PREFIX}porta", "Name": f"{PREFIX}porta"})

    bench.t5 = t5_with_a_mistake
    data = await kit.esegui(bench, ["T5", "T7"])

    assert list(data["prove"]) == ["T5"] and data["prove"]["T5"]["esito"] == "interrotta"
    assert "axtdc:SetDoor" in data["vietata"]
    assert any(line.startswith("\nFERMO: il kit stava per mandare una richiesta vietata") for line in lines)
    assert ("remove_user", f"{PREFIX}sbaglio") in controller.calls
    assert leftovers(controller) == []
    assert data["impianto_in_servizio"]["verdetto"] == "IMPIANTO INVARIATO"
    assert_nothing_forbidden_in_service(controller, server)


async def test_service_mode_cleans_up_when_interrupted():
    controller = EliseoController.bench()
    before = state(controller)
    bench, server, lines, _ = service_kit(controller)

    async def interrupted(seconds):
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    bench.pausa = interrupted
    with pytest.raises(asyncio.CancelledError):
        await kit.esegui(bench, ["T16"])
    assert bench.dati["prove"]["T16"]["esito"] == "interrotta"
    assert leftovers(controller) == [] and state(controller) == before
    assert lines[-1] == "\nIMPIANTO INVARIATO"


async def yielding(seconds):
    """Pausa del kit che passa dal ciclo di eventi, come le richieste vere: lì arriva un Ctrl+C."""
    await asyncio.sleep(0)


class InterruptedWithTheCableOut(Technician):
    """Ctrl+C mentre il kit chiede di riattaccare il cavo in T2, poi Invio: il cavo resta staccato e l'interruzione
    arriva alla prima attesa. Alla pulizia finale il tecnico fa quello che il kit chiede (answers)."""

    def chiedi(self, chiave: str, testo: str) -> str:
        if chiave == "riattacca_cavo":
            self.asked.append(chiave)
            asyncio.current_task().cancel()
            return ""
        return super().chiedi(chiave, testo)


T2_OBJECTS = [f"utente {PREFIX}t2_utente", f"fascia {PREFIX}t2_fascia", f"tessera {PREFIX}t2_tessera"]
T2_OBJECTS.append(f"profilo {PREFIX}t2_profilo")


async def test_interrupted_with_the_cable_out_the_kit_asks_to_reattach_it_and_cleans_up():
    controller = EliseoController.bench()
    before = state(controller)
    operator = InterruptedWithTheCableOut(controller)
    bench, server, lines, _ = service_kit(controller, operator)
    bench.pausa = yielding

    with pytest.raises(asyncio.CancelledError):
        await kit.esegui(bench, ["T2", "T16"])

    # Prima della pulizia finale il kit ha chiesto di riattaccare il cavo e ha aspettato: tolto tutto, verificato
    assert operator.asked[-1] == "riattacca_cavo_pulizia"
    assert bench.dati["prove"]["T2"]["esito"] == "interrotta" and "T16" not in bench.dati["prove"]
    assert leftovers(controller) == [] and state(controller) == before
    assert bench.dati["fine"]["pulizia"]["non_tolti"] == []
    assert bench.dati["impianto_in_servizio"]["verdetto"] == "IMPIANTO INVARIATO"
    assert lines[-3:] == ["", "Add-on: puoi riavviarlo (LEGGIMI.md, «Dopo le prove»).", "\nIMPIANTO INVARIATO"]
    assert_nothing_forbidden_in_service(controller, server)


async def test_with_kit_objects_possibly_left_the_screen_says_not_to_restart_the_addon(tmp_path):
    controller = EliseoController.bench()
    operator = InterruptedWithTheCableOut(controller, riattacca_cavo_pulizia="salta")
    bench, server, lines, _ = service_kit(controller, operator, tmp_path=tmp_path)
    bench.pausa = yielding

    with pytest.raises(asyncio.CancelledError):
        await kit.esegui(bench, ["T2"])

    # Il controller non risponde: il rapporto dice quali oggetti del kit sono probabilmente rimasti, e sono quelli
    assert sorted(leftovers(controller)) == sorted(
        [f"users {PREFIX}t2_utente", f"credentials {PREFIX}t2_tessera", f"schedules {PREFIX}t2_fascia"]
        + [f"profiles {PREFIX}t2_profilo"]
    )
    non_tolti = bench.dati["fine"]["pulizia"]["non_tolti"]
    assert non_tolti == [
        "controller non raggiungibile: non ho tolto niente",
        f"probabilmente ancora sul controller: {', '.join(T2_OBJECTS)}",
    ]
    verdict = bench.dati["verdetto_impianto"]
    assert verdict["verdetto"] == "ATTENZIONE: IMPIANTO NON VERIFICATO"
    assert kit.codice_di_uscita(bench.dati, 130) == 4
    # Prima del verdetto, che resta in fondo: non riavviare l'add-on e come togliere gli oggetti rimasti
    advice = verdict["addon"]
    assert advice[0].startswith("NON RIAVVIARE L'ADD-ON: sul controller possono essere rimasti oggetti di prova")
    assert f"--prova T7 --fotografia-di-partenza {tmp_path / 'rapporto.json'}" in advice[1]
    assert "solo quando l'ultima riga è IMPIANTO INVARIATO" in advice[2]
    end = lines.index("\nATTENZIONE: IMPIANTO NON VERIFICATO")
    assert lines[end - len(advice) : end] == advice and lines[end + 1].startswith("  non riletti: ")
    assert lines[-1] == lines[end + 1]


class BreaksAnnaInT2(EliseoController):
    """Firmware con un difetto: scrivendo l'utente di T2 cambia il nome di un utente vero."""

    async def set_user(self, user):
        await super().set_user(user)
        if user["token"] == f"{PREFIX}t2_utente":
            self.users["utente_anna"]["Name"] = "Anna B."


async def run_in_service(controller, path, prove, attesa=None, **options):
    """Un giro sull'impianto in servizio con il rapporto in path; attesa: secondi di attesa del controller."""
    bench, _, lines, _ = service_kit(controller, **options)
    bench.file = path
    bench.attesa = bench.attesa if attesa is None else attesa
    try:
        data = await kit.esegui(bench, prove)
    except kit.Rifiuto:
        data = bench.dati
    return data, lines


async def test_a_run_after_one_not_verified_is_not_unchanged_and_the_first_photo_shows_the_change(tmp_path):
    controller = BreaksAnnaInT2.bench()
    first = tmp_path / "giro1.json"
    # Giro 1: in T2 Anna cambia, il controller non torna e alla fine il tecnico non può riattaccare il cavo
    operator = Technician(controller, riattacca_cavo="", riattacca_cavo_pulizia="salta")
    data, _ = await run_in_service(controller, first, ["T2"], operator=operator, attesa=0)
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO NON VERIFICATO"
    assert len(leftovers(controller)) == 4

    # Il controller torna. Giro 2 per togliere i resti, senza la fotografia del giro 1: non può dire INVARIATO
    controller.down = False
    second = tmp_path / "giro2.json"
    data, lines = await run_in_service(controller, second, ["T7"])
    assert leftovers(controller) == []
    assert data["verdetto_impianto"]["differenze"] == []
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO NON VERIFICATO"
    assert "4 oggetti aam_prova_ di un giro precedente" in data["impianto_in_servizio"]["partenza"]["incerta"]
    assert kit.codice_di_uscita(data, 0) == 4
    assert lines[-1].startswith("  rispetto alla partenza di questo giro nessuna differenza, ma il giro precedente")
    assert data["verdetto_impianto"]["addon"][0].startswith("NON RIAVVIARE ANCORA L'ADD-ON")

    # Giro 3 con la fotografia del giro 1: il confronto finale vede Anna cambiata nel giro 1
    third = tmp_path / "giro3.json"
    data, lines = await run_in_service(controller, third, ["T7"], partenza=kit.partenza_da_file(first, SERIAL))
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO CAMBIATO"
    assert data["verdetto_impianto"]["righe"] == [ANNA_CHANGED]
    assert lines[-2:] == ["\nATTENZIONE: IMPIANTO CAMBIATO", f"  {ANNA_CHANGED}"]
    assert kit.codice_di_uscita(data, 0) == 3
    assert ANNA_CHANGED in "\n".join(data["avvio"]["osservazioni"])
    # Il rapporto del giro 3 tiene la fotografia del giro 1: un giro con quel rapporto confronta ancora con quella
    data, _ = await run_in_service(
        controller, tmp_path / "giro4.json", ["T7"], partenza=kit.partenza_da_file(third, SERIAL)
    )
    assert data["verdetto_impianto"]["righe"] == [ANNA_CHANGED]
    # Con il rapporto del giro 2, partito dopo il giro 1 non chiuso, il giro precedente resta non verificato
    data, _ = await run_in_service(
        controller, tmp_path / "giro5.json", ["T7"], partenza=kit.partenza_da_file(second, SERIAL)
    )
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO NON VERIFICATO"
    assert data["verdetto_impianto"]["partenza_incerta"]


async def test_the_photo_of_a_previous_run_must_be_of_this_controller_and_complete(tmp_path):
    controller = EliseoController.bench()
    data, _ = await run_in_service(controller, tmp_path / "giro.json", ["T7"])
    assert data["impianto_in_servizio"]["verdetto"] == "IMPIANTO INVARIATO"
    assert kit.partenza_da_rapporto(data, SERIAL)["foto"] == data["fotografie"]["prima"]
    with pytest.raises(kit.Rifiuto, match="del controller"):
        kit.partenza_da_rapporto(data, "ALTRA")
    with pytest.raises(kit.Rifiuto, match="non è il rapporto di un giro con --impianto-in-servizio"):
        kit.partenza_da_rapporto({**data, "impianto_in_servizio": None}, SERIAL)
    with pytest.raises(kit.Rifiuto, match="non c'è la fotografia di partenza"):
        kit.partenza_da_rapporto({**data, "fotografie": {}}, SERIAL)
    incomplete = {**data["fotografie"]["prima"], "non_letti": {"porte": "tempo scaduto"}}
    with pytest.raises(kit.Rifiuto, match="non è completa"):
        kit.partenza_da_rapporto({**data, "fotografie": {"prima": incomplete}}, SERIAL)
    with pytest.raises(kit.Rifiuto, match="non riesco a leggere"):
        kit.partenza_da_file(tmp_path / "manca.json", SERIAL)
    # Un rapporto che finisce con INVARIATO non lascia oggetti di prova: se ce ne sono vengono da un altro giro
    controller.users[f"{PREFIX}resto"] = {"token": f"{PREFIX}resto", "Name": f"{PREFIX}resto"}
    partenza = kit.partenza_da_rapporto(data, SERIAL)
    data, _ = await run_in_service(controller, tmp_path / "dopo.json", ["T7"], partenza=partenza)
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO NON VERIFICATO"
    assert "vengono da un altro giro" in data["verdetto_impianto"]["partenza_incerta"]
    assert leftovers(controller) == []


def log_entry(rowid: int, topics: list[str], **values) -> dict:
    keys = [{"Key": f"topic{i}", "Value": t} for i, t in enumerate(topics)]
    return {
        "rowid": rowid,
        "UtcTime": "2026-11-13T07:30:00Z",
        "KeyValues": keys + [{"Key": k, "Value": v} for k, v in values.items()],
    }


class OthersSwipeToo(Technician):
    """Mentre il kit aspetta il passaggio della tessera di scorta passa anche Anna, con la sua tessera vera che apre,
    e poi qualcuno a cui il controller nega l'accesso senza dire la credenziale. La tessera di scorta la passa il
    tecnico solo con spare."""

    def __init__(self, controller: FakeController, spare: bool) -> None:
        super().__init__(controller)
        self.spare = spare

    def swipe(self) -> None:
        c = self.controller
        row = max((e["rowid"] for e in c.log_events), default=0)
        c.log_events += [
            log_entry(row + 1, ["IdPoint", "Request", "IdData"], Card="04A1B2C3", IdPointToken="lettore-1"),
            log_entry(
                row + 2,
                ["AccessControl", "AccessGranted", "Credential"],
                CredentialToken="cred-1",
                CredentialHolderName="Anna Bianchi",
                AccessPointToken="ap-in",
            ),
        ]
        if self.spare:
            super().swipe()
        else:
            self.swipes += 1
        row = max(e["rowid"] for e in c.log_events)
        c.log_events += [
            log_entry(row + 1, ["IdPoint", "Request", "IdData"], Card="0400aa11", IdPointToken="lettore-1"),
            log_entry(row + 2, ["AccessControl", "Denied", "Schedule"], AccessPointToken="ap-in"),
        ]


@pytest.mark.parametrize("spare", [False, True], ids=["scorta_non_letta", "scorta_letta"])
async def test_swipes_of_other_cards_while_waiting_are_not_the_spare_card(spare):
    controller = EliseoController.bench()
    operator = OthersSwipeToo(controller, spare)
    bench, server, _, _ = service_kit(controller, operator)

    data = await kit.esegui(bench, ["T4", "T8"])

    assert operator.swipes >= 3 and operator.opening == []
    notes = "\n".join(data["prove"]["T4"]["osservazioni"] + data["prove"]["T8"]["osservazioni"])
    # Il consenso di Anna non è della tessera di scorta: niente «accesso concesso», e Anna è tra gli altri passaggi
    assert "ATTENZIONE, accesso concesso" not in notes
    others = (
        "altri passaggi nella stessa attesa, ignorati: IdPoint/Request/IdData Card=04A1B2C3; "
        "AccessControl/AccessGranted/Credential CredentialToken=cred-1 AccessPointToken=ap-in; "
        "IdPoint/Request/IdData Card=0400aa11; AccessControl/Denied/Schedule AccessPointToken=ap-in"
    )
    assert notes.count(others) == operator.swipes
    if spare:
        assert "credenziale disabilitata: IdData arrivato; negato: AccessControl/Denied/Credential " in notes
        assert "Reason=CredentialNotEnabled; CredentialToken sì" in notes
        assert "non letta" not in notes
    else:
        assert "IdData arrivato" not in notes
        assert notes.count(f"tessera di scorta non letta: nessun IdData con il numero {CARD}") == operator.swipes
    assert data["impianto_in_servizio"]["verdetto"] == "IMPIANTO INVARIATO"


def test_events_of_the_spare_card():
    def event(row, topic, **values):
        return {"riga": row, "ora": "", "argomento": topic, **values}

    events = [
        event(1, "IdPoint/Request/IdData", Card="C0110B18"),
        event(2, "AccessControl/Denied/Credential", Reason="CredentialNotEnabled"),
        event(3, "Door/State/DoorMode", State="Locked"),
        event(4, "IdPoint/Request/IdData", Card="04a1b2c3"),
        event(5, "AccessControl/AccessGranted/Credential", CredentialToken="cred-1", AccessPointToken="ap-in"),
        event(6, "AccessControl/Denied/Credential", AccessPointToken="ap-in"),
        event(7, "AccessControl/Denied/Credential", CredentialToken="scorta-del-controller"),
        event(8, "IdPoint/Request/IdData", Card=CARD),
        event(9, "AccessControl/Denied/Credential", AccessPointToken="altro-punto"),
        event(10, "AccessControl/AccessGranted/Anonymous", AccessPointToken="ap-in"),
    ]
    spare, others = kit.eventi_della_tessera(events, CARD, {"scorta-del-controller"}, "ap-in")
    assert [e["riga"] for e in spare] == [1, 2, 7, 8, 10]
    assert [e["riga"] for e in others] == [4, 5, 6, 9]


async def test_a_list_answered_without_its_key_is_not_read_in_the_photo():
    # Alla partenza: non letto, il kit non parte
    controller = EliseoController.bench()
    bench, server, _, _ = service_kit(controller)
    answer = server.answer
    keyless = {"on": True}

    async def without_key(method, args):
        data = await answer(method, args)
        if keyless["on"] and method == "axtdc:GetDoorConfigurationList":
            data.pop("DoorConfiguration", None)
        return data

    server.answer = without_key
    with pytest.raises(kit.Rifiuto, match="non si leggono configurazioni_porte: risposta del controller non"):
        await kit.esegui(bench, ["T9"])
    assert controller.calls == [] and not [m for m in server.methods if "Set" in m or "Remove" in m]

    # Alla fine: non riletto, quindi non verificato (e non una configurazione «tolta»)
    controller = EliseoController.bench()
    bench, server, lines, _ = service_kit(controller)
    answer = server.answer
    server.answer = without_key
    keyless["on"] = False
    closing = bench.chiusura

    async def keyless_at_the_end():
        keyless["on"] = True
        await closing()

    bench.chiusura = keyless_at_the_end
    data = await kit.esegui(bench, ["T9"])
    assert data["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO NON VERIFICATO"
    assert data["verdetto_impianto"]["differenze"] == []
    assert data["verdetto_impianto"]["non_riletti"] == [
        "configurazioni_porte (risposta del controller non riconosciuta)"
    ]
    assert kit.codice_di_uscita(data, 0) == 4
    assert leftovers(controller) == []


KIT_CLIENT = kit.cliente


def run_main(monkeypatch, tmp_path, controller, operator, argv):
    """Il kit da riga di comando sul finto: password dall'ambiente, tecnico al posto della tastiera."""
    server = VapixServer(controller)

    def fake_client(host, utente, password, verifica=False, trasporto=None):
        client, recorder = KIT_CLIENT(host, utente, password, verifica, httpx.MockTransport(server))
        client.certificate_fingerprint = controller.certificate_fingerprint
        return client, recorder

    monkeypatch.setattr(kit, "cliente", fake_client)
    monkeypatch.setattr(kit, "Operatore", lambda: operator)
    monkeypatch.setenv("AAM_BANCO_PASSWORD", "segreta")
    report = tmp_path / "rapporto.json"
    base = ["--host", "10.103.200.10", "--serie", SERIAL, "--impianto-in-servizio", "--uscita", str(report)]
    return kit.main(base + argv), server


def test_main_in_service_ends_with_the_verdict_and_its_exit_code(monkeypatch, tmp_path, capsys):
    controller = EliseoController.bench()
    code, server = run_main(monkeypatch, tmp_path, controller, Technician(controller), ["--prova", "T16"])
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and out[-1] == "IMPIANTO INVARIATO"
    assert out[-3] == "Add-on: puoi riavviarlo (LEGGIMI.md, «Dopo le prove»)."
    assert leftovers(controller) == []

    controller = BreaksARealUser.bench()
    code, server = run_main(monkeypatch, tmp_path, controller, Technician(controller), ["--prova", "T16"])
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 3
    assert "MI SONO FERMATO: dopo T16" in "\n".join(out)
    assert out[-2:] == ["ATTENZIONE: IMPIANTO CAMBIATO", f"  {ANNA_CHANGED}"]
    assert out[-5].startswith("NON RIAVVIARE L'ADD-ON: è cambiato un oggetto dell'impianto")
    saved = json.loads((tmp_path / "rapporto.json").read_text())
    assert saved["impianto_in_servizio"]["verdetto"] == "ATTENZIONE: IMPIANTO CAMBIATO"


@pytest.mark.parametrize(
    "argv",
    [
        ["--prova", "T12"],
        ["--limiti"],
        ["--prova", "T18", "--tetto", "tessere=5"],
        ["--non-interattivo"],
        ["--quante", "501"],
        ["--quante", "0"],
    ],
    ids=["T12", "limiti", "tetto", "non_interattivo", "troppe_tessere", "zero_tessere"],
)
def test_main_in_service_refuses_before_asking_the_password(monkeypatch, capsys, argv):
    def never(*args, **kwargs):
        raise AssertionError("non doveva arrivare fin qui")

    monkeypatch.delenv("AAM_BANCO_PASSWORD", raising=False)
    monkeypatch.setattr(kit.getpass, "getpass", never)
    monkeypatch.setattr(kit, "cliente", never)
    code = kit.main(["--host", "10.103.200.10", "--serie", SERIAL, "--impianto-in-servizio", *argv])
    assert code == 2
    assert "NON PARTO" in capsys.readouterr().out


def test_main_with_the_photo_of_a_previous_run(monkeypatch, tmp_path, capsys):
    def never(*args, **kwargs):
        raise AssertionError("non doveva arrivare fin qui")

    monkeypatch.delenv("AAM_BANCO_PASSWORD", raising=False)
    monkeypatch.setattr(kit.getpass, "getpass", never)
    monkeypatch.setattr(kit, "cliente", never)
    base = ["--host", "10.103.200.10", "--serie", SERIAL]
    # Solo sull'impianto in servizio, e con un rapporto che si legge: prima della password
    assert kit.main([*base, "--fotografia-di-partenza", "giro1.json"]) == 2
    assert "NON PARTO: --fotografia-di-partenza vale solo con --impianto-in-servizio" in capsys.readouterr().out
    missing = tmp_path / "manca.json"
    assert kit.main([*base, "--impianto-in-servizio", "--fotografia-di-partenza", str(missing)]) == 2
    assert "NON PARTO: --fotografia-di-partenza: non riesco a leggere" in capsys.readouterr().out
    other = tmp_path / "altro.json"
    other.write_text(json.dumps({"impianto_in_servizio": {}, "serie_attesa": "ALTRA"}))
    assert kit.main([*base, "--impianto-in-servizio", "--fotografia-di-partenza", str(other)]) == 2
    assert "è del controller 'ALTRA'" in capsys.readouterr().out


def test_main_stopped_before_the_verdict_says_not_to_restart_the_addon(monkeypatch, tmp_path, capsys):
    async def stopped(bench, prove):
        # Secondo Ctrl+C durante la pulizia: scritto qualcosa, nessun verdetto
        await bench.tenta("set_user", bench.modello("utente", f"{PREFIX}u"))
        raise KeyboardInterrupt

    monkeypatch.setattr(kit, "esegui", stopped)
    controller = EliseoController.bench()
    code, _ = run_main(monkeypatch, tmp_path, controller, Technician(controller), ["--prova", "T16"])
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 130
    assert out[-1].startswith("NON RIAVVIARE L'ADD-ON: il kit si è fermato prima del confronto finale")
    assert f"--prova T7 --fotografia-di-partenza {tmp_path / 'rapporto.json'}" in out[-1]


def test_service_mode_options_and_comparison_details():
    args = kit.argomenti(["--host", "x", "--serie", "y", "--impianto-in-servizio"])
    assert args.impianto_in_servizio is True and args.quante is None
    assert kit.PROVE_IN_SERVIZIO == SERVICE_TESTS
    # Ora, sincronizzazione e stato del collegamento non contano; il fuso sì
    users = [{"token": "u", "Name": "A"}]
    before = {"letti": {"orologio": {"dateTime": "a", "timeZone": "Europe/Rome"}, "utenti": users}}
    after = {"letti": {"orologio": {"dateTime": "b", "timeZone": "Europe/Rome"}, "utenti": users}}
    assert kit.differenze(before, after) == ([], [])
    after["letti"]["orologio"]["timeZone"] = "UTC"
    lines = kit.righe_differenze(kit.differenze(before, after)[0])
    assert lines == ['orologio: timeZone prima "Europe/Rome", dopo "UTC"']
    # L'ordine degli oggetti non conta, gli oggetti del kit nemmeno; aggiunti e tolti si vedono con prima e dopo
    two = [{"token": "a", "Name": "A"}, {"token": "b", "Name": "B"}]
    assert kit.differenze({"letti": {"utenti": two}}, {"letti": {"utenti": two[::-1]}}) == ([], [])
    kit_user = {"token": f"{PREFIX}x", "Name": f"{PREFIX}x"}
    assert kit.differenze({"letti": {"utenti": two}}, {"letti": {"utenti": [*two, kit_user]}}) == ([], [])
    lines = kit.righe_differenze(kit.differenze({"letti": {"utenti": two}}, {"letti": {"utenti": [two[0]]}})[0])
    assert lines == ['utenti · b: TOLTO; prima: {"Name": "B", "token": "b"}; dopo: assente']
    lines = kit.righe_differenze(kit.differenze({"letti": {"utenti": [two[0]]}}, {"letti": {"utenti": two}})[0])
    assert lines == ['utenti · b: AGGIUNTO; prima: assente; dopo: {"Name": "B", "token": "b"}']
    # Non riletto non è invariato
    missing = {"letti": {}, "non_letti": {"porte": "tempo scaduto"}}
    assert kit.differenze({"letti": {"porte": []}}, missing) == ([], ["porte (tempo scaduto)"])
    leftover = {"letti": {"utenti": [*two, kit_user], "orologio": {"token": f"{PREFIX}x"}}}
    assert kit.rimasti_del_kit(leftover) == [f"utenti {PREFIX}x"]
    hidden = kit.senza_password({"a": [{"password": "x", "Password": ""}]})
    assert hidden == {"a": [{"password": "<nascosta>", "Password": ""}]}
