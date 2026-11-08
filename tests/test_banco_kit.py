"""Il kit di prove al banco (tools/banco/prove_a1210.py) eseguito tutto sul controller finto, dietro HTTP."""

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from conftest import FakeController, refused

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
        elif chiave == "riattacca_cavo":
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


def make_kit(controller, operator=None, serial=SERIAL, quante=500, tmp_path=None, limiti=False, tetti=None):
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
        tessera=CARD,
        pausa=no_wait,
        limiti=limiti,
        tetti=tetti,
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
        assert "sospesa con SetCredential: SetCredential non sospende: passaggio non fatto" in notes["T4"]
        assert "credenziale disabilitata: SetCredential non sospende: passaggio non fatto" in notes["T8"]
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
