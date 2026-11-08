"""Il controller finto stesso: guasti, riferimenti, copie e costruttori (vedi FakeController in conftest)."""

import copy
import inspect

import httpx
import pytest
from conftest import FakeController, refused

from app.vapix import A1210Client, ControllerGuard, ControllerUnreachable, VapixError

STATE = (
    "users",
    "credentials",
    "schedules",
    "profiles",
    "access_points",
    "doors",
    "idpoints",
    "door_configs",
    "idpoint_configs",
    "access_controllers",
    "authentication_profiles",
    "io_assignments",
    "log_events",
)


def client_methods() -> list[str]:
    return [n for n, m in vars(FakeController).items() if inspect.iscoroutinefunction(m) and n != "aclose"]


def dummy_args(name: str) -> list:
    """Un argomento qualsiasi per ogni parametro obbligatorio: a controller spento non si guarda."""
    parameters = list(inspect.signature(getattr(FakeController, name)).parameters.values())[1:]
    return ["x" for p in parameters if p.default is p.empty]


def state(controller: FakeController) -> dict:
    return copy.deepcopy({name: getattr(controller, name) for name in STATE})


def scramble(value) -> None:
    """Rovina tutto quello che il chiamante ha ricevuto, anche dentro le liste e i dizionari annidati."""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            scramble(item)
            value[key] = "rotto" if not isinstance(item, dict | list) else item
        value["aggiunta"] = "rotto"
    elif isinstance(value, list):
        for item in value:
            scramble(item)
        value.append("rotto")


def event(rowid: int) -> dict:
    return {
        "rowid": rowid,
        "UtcTime": "2026-11-07T10:00:00Z",
        "KeyValues": [{"Key": "topic0", "Value": "IdPoint"}, {"Key": "Card", "Value": "04aa"}],
    }


async def test_down_controller_fails_every_call_reads_included():
    controller = FakeController.bench()
    controller.add_existing("utente_anna", "Anna", "04aa")
    before = state(controller)
    controller.down = True
    methods = client_methods()
    assert {"list_users", "device_info", "access_door", "set_credential", "latest_events"} <= set(methods)
    for name in methods:
        with pytest.raises(VapixError) as err:
            await getattr(controller, name)(*dummy_args(name))
        # Il certificato il client vero lo legge con un collegamento a parte, che senza risposta dà VapixError
        assert isinstance(err.value, ControllerUnreachable) == (name != "certificate_fingerprint"), name
    assert controller.calls == [] and controller.reads == []
    assert controller.attempts == methods
    assert state(controller) == before
    # Anche attraverso il client con le scritture sospendibili dell'app
    with pytest.raises(ControllerUnreachable):
        await ControllerGuard(controller).list_users()
    controller.down = False
    assert [u["token"] for u in await controller.list_users()] == ["utente_anna"]


async def test_offline_still_fails_only_device_info():
    controller = FakeController()
    controller.offline = True
    with pytest.raises(VapixError):
        await controller.device_info()
    assert await controller.list_doors()
    await controller.set_user({"token": "u", "Name": "U"})
    assert "u" in controller.users


async def test_lost_response_after_the_write():
    controller = FakeController()
    controller.lose_response("set_credential")
    with pytest.raises(ControllerUnreachable):
        await controller.set_credential({"token": "t1", "UserToken": "u", "IdData": [{"Name": "Card", "Value": "01"}]})
    # Il controller ha scritto: solo la risposta è andata persa
    assert controller.credentials["t1"]["UserToken"] == "u"
    assert controller.calls == [("set_credential", "t1")]
    assert await controller.set_credential({"token": "t2"}) == "t2"

    controller.lose_response("remove_credential", times=2)
    for token in ("t1", "t2"):
        with pytest.raises(ControllerUnreachable):
            await controller.remove_credential(token)
    assert controller.credentials == {}
    await controller.remove_credential("t3")


async def test_writes_fail_after_n_successful_ones():
    controller = FakeController()
    controller.fail_after(2)
    await controller.set_user({"token": "a"})
    await controller.set_schedule({"token": "s"})
    with pytest.raises(ControllerUnreachable):
        await controller.set_user({"token": "b"})
    with pytest.raises(ControllerUnreachable):
        await controller.remove_user("a")
    assert set(controller.users) == {"a"} and "s" in controller.schedules
    # Le letture e i comandi alle porte non sono scritture della configurazione
    assert [u["token"] for u in await controller.list_users()] == ["a"]
    await controller.access_door("porta")
    # Una scrittura rifiutata dal controller non conta come riuscita
    controller.fail_after(1)
    controller.refuse("set_user", "ter:InvalidArgVal", times=1)
    with pytest.raises(VapixError):
        await controller.set_user({"token": "c"})
    await controller.set_user({"token": "c"})
    with pytest.raises(ControllerUnreachable):
        await controller.set_user({"token": "d"})
    controller.fail_after(None)
    await controller.set_user({"token": "d"})
    assert set(controller.users) == {"a", "c", "d"}


async def test_refused_call_has_the_raw_fault_like_the_real_client():
    controller = FakeController()
    controller.refuse("set_schedule", "ter:CapabilityViolated")
    for _ in range(2):
        with pytest.raises(VapixError) as err:
            await controller.set_schedule({"token": "s"})
        assert not isinstance(err.value, ControllerUnreachable)
        assert err.value.fault == "ter:CapabilityViolated"
        assert str(err.value) == "il controller ha rifiutato la richiesta (limite del controller raggiunto)"
    assert "s" not in controller.schedules and controller.calls == []
    controller.refuse("set_schedule")
    assert await controller.set_schedule({"token": "s"}) == "s"

    # Anche le letture, e per un numero di volte
    controller.refuse("list_users", "ter:ActionNotSupported", times=1)
    with pytest.raises(VapixError, match="operazione non supportata"):
        await controller.list_users()
    assert await controller.list_users() == []


async def test_refused_matches_the_client_answering_a_fault():
    def handler(request):
        return httpx.Response(400, json={"Fault": "env:Sender", "FaultCode": "ter:ReferenceInUse"})

    with pytest.raises(VapixError) as err:
        await A1210Client("a1210.local", "u", "p", transport=httpx.MockTransport(handler)).remove_schedule("s")
    fake = refused("ter:ReferenceInUse")
    assert (str(fake), fake.fault) == (str(err.value), err.value.fault)


async def test_enable_and_disable_of_a_missing_credential_are_not_found():
    controller = FakeController()
    for method in (controller.enable_credential, controller.disable_credential):
        with pytest.raises(VapixError) as err:
            await method("manca")
        assert "NotFound" in err.value.fault and not isinstance(err.value, ControllerUnreachable)
    assert controller.calls == []
    token = controller.add_existing("u", "Anna", "04aa")
    await controller.disable_credential(token)
    assert controller.credentials[token]["Enabled"] is False


async def test_references_are_free_by_default():
    controller = FakeController()
    await controller.set_credential(
        {"token": "c", "UserToken": "manca", "CredentialAccessProfile": [{"AccessProfile": "manca"}]}
    )
    await controller.remove_schedule("standard_always")
    await controller.remove_door("porta")
    assert "c" in controller.credentials and "porta" not in controller.doors


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("set_credential", [{"token": "c", "UserToken": "manca"}]),
        ("set_credential", [{"token": "c", "UserToken": "u", "CredentialAccessProfile": [{"AccessProfile": "x"}]}]),
        ("set_access_profile", [{"token": "p", "Schedule": ["manca"], "AccessPolicy": []}]),
        ("set_access_profile", [{"token": "p", "Schedule": [], "AccessPolicy": [{"AccessPoint": "manca"}]}]),
        (
            "set_access_profile",
            [{"token": "p", "Schedule": [], "AccessPolicy": [{"AccessPoint": "ap-in", "Schedule": ["manca"]}]}],
        ),
        ("create_access_point", [{"token": "a", "Entity": "manca", "IdPointDevice": []}]),
        ("set_access_point", [{"token": "a", "Entity": "porta", "IdPointDevice": [{"IdPoint": "manca"}]}]),
    ],
)
async def test_checked_references_refuse_missing_objects(call, args):
    controller = FakeController.bench()
    controller.users["u"] = {"token": "u", "Name": "U"}
    controller.check_references = True
    before = state(controller)
    with pytest.raises(VapixError) as err:
        await getattr(controller, call)(*args)
    assert "ReferenceNotFound" in err.value.fault
    assert state(controller) == before and controller.calls == []


@pytest.mark.parametrize(
    ("call", "token"),
    [
        ("remove_user", "utente_anna"),
        ("remove_access_profile", "profilo_sempre"),
        ("remove_schedule", "standard_always"),
        ("remove_access_point", "ap-in"),
        ("remove_door", "porta"),
        ("remove_idpoint", "lettore-1"),
    ],
)
async def test_checked_references_keep_objects_still_in_use(call, token):
    controller = FakeController.bench()
    controller.add_existing("utente_anna", "Anna", "04aa")
    controller.check_references = True
    before = state(controller)
    with pytest.raises(VapixError) as err:
        await getattr(controller, call)(token)
    assert "ReferenceInUse" in err.value.fault
    assert str(err.value) == "il controller ha rifiutato la richiesta (oggetto ancora in uso)"
    assert state(controller) == before and controller.calls == []


async def test_checked_references_follow_the_right_order():
    controller = FakeController.bench()
    controller.check_references = True
    token = controller.add_existing("utente_anna", "Anna", "04aa")
    # Quello che nessuno cita si toglie
    await controller.remove_door("porta2")
    await controller.remove_idpoint("lettore-2")
    await controller.remove_schedule("standard_office_hours")
    # Dalla tessera in giù, nell'ordine giusto
    await controller.remove_credential(token)
    await controller.remove_user("utente_anna")
    await controller.remove_access_profile("profilo_sempre")
    controller.access_controllers["Axis-x AccessController"]["AccessPoint"] = []
    await controller.remove_access_point("ap-in")
    await controller.remove_door("porta")
    await controller.remove_idpoint("lettore-1")
    # E in salita: prima chi è citato
    await controller.set_user({"token": "u", "Name": "U"})
    await controller.set_schedule({"token": "s"})
    await controller.set_door({"token": "d", "Name": "D"})
    await controller.create_idpoint({"token": "i", "Name": "I"})
    await controller.create_access_point({"token": "a", "Entity": "d", "IdPointDevice": [{"IdPoint": "i"}]})
    await controller.set_access_profile({"token": "p", "Schedule": ["s"], "AccessPolicy": [{"AccessPoint": "a"}]})
    credential = {"token": "c", "UserToken": "u", "CredentialAccessProfile": [{"AccessProfile": "p"}]}
    assert await controller.set_credential(credential) == "c"


async def test_reads_give_copies():
    controller = FakeController()
    controller.add_existing("utente_anna", "Anna", "04aa")
    controller.log_events = [event(1), event(2)]
    before = state(controller)
    lists = [
        "list_users",
        "list_credentials",
        "list_schedules",
        "list_access_profiles",
        "list_access_points",
        "list_doors",
        "list_idpoints",
        "list_door_configurations",
        "list_access_controllers",
        "list_authentication_profiles",
    ]
    for name in lists:
        scramble(await getattr(controller, name)())
    gets = {
        "get_users": controller.users,
        "get_credentials": controller.credentials,
        "get_access_profiles": controller.profiles,
        "get_access_points": [a["token"] for a in controller.access_points],
        "get_doors": controller.doors,
        "get_idpoints": controller.idpoints,
    }
    for name, tokens in gets.items():
        scramble(await getattr(controller, name)(list(tokens)))
    scramble(await controller.latest_events(10))
    scramble((await controller.fetch_events("2026-01-01T00:00:00Z"))[0])
    scramble((await controller.list_io())[1])
    assert state(controller) == before


async def test_writes_keep_a_copy():
    controller = FakeController()
    user = {"token": "u", "Name": "U", "Attribute": [{"Name": "a", "Value": "1"}]}
    credential = {"token": "c", "UserToken": "u", "IdData": [{"Name": "Card", "Value": "04aa"}]}
    schedule = {"token": "s", "Name": "S", "Attribute": []}
    await controller.set_user(user)
    await controller.set_credential(credential)
    await controller.set_schedule(schedule)
    before = state(controller)
    for item in (user, credential, schedule):
        scramble(item)
    assert state(controller) == before


async def test_reads_are_logged_apart_from_writes():
    controller = FakeController()
    tokens = ["a"]
    await controller.list_users()
    await controller.get_users(tokens)
    tokens.append("b")
    await controller.set_user({"token": "a"})
    await controller.access_door("porta")
    controller.lose_response("list_doors")
    with pytest.raises(ControllerUnreachable):
        await controller.list_doors()
    assert controller.reads == [("list_users",), ("get_users", ["a"])]
    assert controller.calls == [("set_user", "a"), ("access_door", "porta", False)]
    assert controller.attempts == ["list_users", "get_users", "set_user", "access_door", "list_doors"]


async def test_log_events_belong_to_each_controller():
    first, second = FakeController(), FakeController()
    first.log_events.append(event(1))
    assert second.log_events == []
    assert await second.latest_events(10) == []


async def test_factory_and_bench_controllers():
    factory = FakeController.factory()
    assert factory.doors == factory.door_configs == factory.idpoints == factory.idpoint_configs == {}
    assert factory.access_points == [] and factory.profiles == factory.users == factory.credentials == {}
    assert set(factory.schedules) == {"standard_always", "standard_after_hours"}
    assert set(factory.authentication_profiles) == {"CardOnly", "REXOnly"}
    assert [c["AccessPoint"] for c in factory.access_controllers.values()] == [[]]
    assert factory.calls == factory.reads == factory.attempts == []
    assert factory.time_zone == "UTC"

    bench = FakeController.bench()
    assert set(bench.doors) == {"porta", "porta2"} and set(bench.idpoints) == {"lettore-1", "lettore-2"}
    assert [a["token"] for a in bench.access_points] == ["ap-in"] and set(bench.profiles) == {"profilo_sempre"}
    assert state(bench) == state(FakeController())
    # Ognuno il suo stato
    other = FakeController.factory()
    other.users["u"] = {"token": "u"}
    assert FakeController.factory().users == {} and factory.users == {}


async def test_heal_removes_every_fault():
    controller = FakeController()
    controller.down = controller.offline = True
    controller.refuse("list_users", "ter:ActionNotSupported")
    controller.lose_response("set_user")
    controller.fail_after(0)
    controller.heal()
    await controller.device_info()
    await controller.set_user({"token": "u"})
    assert [u["token"] for u in await controller.list_users()] == ["u"]
