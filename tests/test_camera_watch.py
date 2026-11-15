"""Telecamera Non Disponibile che resta aperta con la telecamera tornata: il 13/11 sera, dopo il riavvio di Home
Assistant, la foto di un'apertura non è riuscita e il guasto è rimasto finché nessuno suonava o apriva.

Orologio finto (anche le attese del ricontrollo) e Home Assistant finto; le foto passano da main._save_snapshot come
quelle degli eventi. Telecamere e nomi di fantasia."""

import asyncio
import heapq
import logging
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app import main
from app.camera_watch import API_DOWN_RECHECK, CameraWatch
from app.ha import CAMERA_TIMEOUT, HomeAssistant

ITALIA = timezone(timedelta(hours=1))
NOT_LOADED = "telecamera non disponibile (risposta 404 di Home Assistant)"
NO_ANSWER = "Home Assistant non risponde"


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


class FakeTime:
    """Orologio monotono finto e attese che finiscono solo quando il test manda avanti l'orologio."""

    def __init__(self, local: str = "20:02:58"):
        self.start = datetime.fromisoformat(f"2026-11-13T{local}").replace(tzinfo=ITALIA)
        self.now = 1000.0
        self._sleepers: list = []
        self._order = 0

    def __call__(self) -> float:
        return self.now

    def local(self, local: str) -> float:
        """Il valore dell'orologio monotono all'ora locale del 13/11."""
        target = datetime.fromisoformat(f"2026-11-13T{local}").replace(tzinfo=ITALIA)
        return 1000.0 + (target - self.start).total_seconds()

    def iso(self) -> str:
        return (self.start + timedelta(seconds=self.now - 1000.0)).astimezone(UTC).isoformat(timespec="seconds")

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + seconds, self._order, future))
        self._order += 1
        await future

    async def until(self, moment: float) -> None:
        """Avanti fino a moment, svegliando le attese nell'ordine."""
        while True:
            await settle()
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)
            if not self._sleepers or self._sleepers[0][0] > moment:
                break
            at, _, future = heapq.heappop(self._sleepers)
            self.now = max(self.now, at)
            future.set_result(None)
        self.now = max(self.now, moment)
        await settle()

    async def advance(self, seconds: float) -> None:
        await self.until(self.now + seconds)


class FakeHA:
    """Home Assistant finto: ogni telecamera risponde da un certo momento in poi; l'API può essere giù."""

    def __init__(self, clock: FakeTime):
        self.clock = clock
        self.back: dict[str, float] = {}
        self.api_down_until = float("-inf")
        self.api_up = None
        self.on_api_back = None
        self.saved: list[tuple[float, str, str]] = []
        self.probes: list[tuple[float, str]] = []

    def _api(self, up: bool) -> None:
        was, self.api_up = self.api_up, up
        if up and was is False and self.on_api_back:
            self.on_api_back()

    def _answer(self, camera: str) -> str:
        if self.clock() < self.api_down_until:
            self._api(False)
            return NO_ANSWER
        self._api(True)
        return "" if self.clock() >= self.back.get(camera, float("-inf")) else NOT_LOADED

    async def save_snapshot(self, camera: str, name: str) -> tuple[str, str]:
        self.saved.append((self.clock(), camera, name))
        error = self._answer(camera)
        return ("", error) if error else (f"{name}.jpg", "")

    async def probe_camera(self, camera: str) -> str:
        self.probes.append((self.clock(), camera))
        return self._answer(camera)


class Plant:
    """Quello che main usa per le foto degli eventi: database, Home Assistant, guasti e ricontrollo."""

    def __init__(self, db, clock: FakeTime, cameras=("camera.cortile",)):
        self.db, self.clock = db, clock
        self.ha = FakeHA(clock)
        self.errors: dict[str, dict] = {}
        self.cameras = set(cameras)
        self.watch = CameraWatch(
            self.ha, self.errors, wanted=lambda camera: camera in self.cameras, clock=clock, sleep=clock.sleep
        )
        self.state = SimpleNamespace(db=db, ha=self.ha, snapshot_errors=self.errors, camera_watch=self.watch)

    async def event(self, camera: str = "camera.cortile", kind: str = "apertura_remota") -> int:
        """Un evento della porta con la sua foto, come after_event."""
        row = {"ts": self.clock.iso(), "kind": kind, "door": "porta"}
        row["id"] = self.db.add_event(**row)
        await main._save_snapshot(self.state, camera, row)
        await settle()
        return row["id"]

    def row(self, event_id: int) -> dict:
        return next(e for e in self.db.list_events() if e["id"] == event_id)


async def test_13_11_the_camera_back_after_18_s_never_becomes_a_fault(db, caplog):
    """La cronologia del 13/11: nessun guasto. La foto delle 20:03:04 non riesce 6 s dopo l'avvio dell'add-on, dentro
    la tolleranza di 60 s (fino alle 20:03:58): l'app la riprova ogni 3 s, la telecamera torna alle 20:03:22 e il
    tentativo di quel momento riesce, prima della fine della tolleranza. Il guasto non si apre mai, quindi niente
    «Guasti 1» sul cruscotto. La foto riuscita è di 18 s dopo l'apertura: non va sull'evento."""
    caplog.set_level(logging.DEBUG, logger="app.camera_watch")
    clock = FakeTime("20:02:58")  # 20:01:30 Home Assistant si spegne; 20:02:58 riparte l'add-on
    plant = Plant(db, clock)
    # 20:01:32 la telecamera diventa non disponibile; torna (streaming) alle 20:03:22
    plant.ha.back["camera.cortile"] = clock.local("20:03:22")
    await clock.until(clock.local("20:03:04"))
    opened = await plant.event()
    # Risposta 404: come allora, ma il guasto aspetta la fine della tolleranza
    assert plant.row(opened)["snapshot_error"] == NOT_LOADED
    assert plant.errors == {} and plant.watch.watching == ["camera.cortile"]
    while clock.now < clock.local("20:04:30"):
        await clock.advance(1)
        assert plant.errors == {}, "mai un guasto aperto"

    assert plant.watch.watching == []
    # Un tentativo con la foto dell'evento entro 5 s (20:03:07), poi solo prove: l'ultima alle 20:03:22, riuscita
    name = f"{opened}_{main._file_stamp(plant.row(opened)['ts'])}"
    assert [(at - clock.local("20:03:04"), saved) for at, _, saved in plant.ha.saved] == [(0, name), (3, name)]
    assert [at - clock.local("20:03:04") for at, _ in plant.ha.probes] == [6, 9, 12, 15, 18]
    # La foto riuscita mostrerebbe un ingresso vuoto: l'evento resta senza, con il motivo
    assert plant.row(opened)["snapshot"] == "" and plant.row(opened)["snapshot_error"] == NOT_LOADED
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_long_fault_opens_and_closes_when_the_camera_is_back(db, caplog):
    caplog.set_level(logging.DEBUG, logger="app.camera_watch")
    clock = FakeTime("09:00:00")
    plant = Plant(db, clock)
    plant.ha.back["camera.cortile"] = float("inf")
    await clock.advance(3600)
    # Fuori dalla tolleranza dell'avvio, ma la prima foto non riuscita di una telecamera ha la sua: un tentativo ogni
    # 3 s, e il guasto si apre se dopo un minuto la telecamera è ancora giù
    first = await plant.event()
    start = clock.now
    assert plant.errors == {}
    await clock.advance(59)
    assert plant.errors == {}
    await clock.advance(1)
    assert plant.errors == {"camera.cortile": {"ts": plant.row(first)["ts"], "error": NOT_LOADED}}
    # Poi il ricontrollo con attese crescenti: 5, 15, 30, 60 s e poi ogni 5 minuti. Il primo tentativo, entro 5 s
    # dall'evento, è ancora la foto dell'evento; gli altri sono prove non salvate
    await clock.advance(20 * 60 - 60)
    assert [round(at - start) for at, _, _ in plant.ha.saved] == [0, 3]
    assert [round(at - start) for at, _ in plant.ha.probes] == [*range(6, 61, 3), 65, 80, 110, 170, 470, 770, 1070]
    # Un'altra foto non riuscita: stesso guasto, dalla stessa ora, e sempre un solo ricontrollo
    await clock.advance(30)
    await plant.event()
    assert plant.errors["camera.cortile"]["ts"] == plant.row(first)["ts"]
    assert plant.watch.watching == ["camera.cortile"]
    # La telecamera torna: al ricontrollo dopo il guasto si chiude, come dopo una foto buona
    plant.ha.back["camera.cortile"] = clock.now + 60
    await clock.advance(6 * 60)
    assert plant.errors == {} and plant.watch.watching == []
    # Nel log una riga quando si apre e una quando si chiude: i tentativi solo in debug
    lines = [(r.levelname, r.getMessage()) for r in caplog.records if r.levelno >= logging.INFO]
    assert lines == [
        ("WARNING", f"Telecamera camera.cortile: {NOT_LOADED}; l'app la ricontrolla da sola"),
        ("INFO", "Telecamera camera.cortile di nuovo disponibile"),
    ]


async def test_shutdown_stops_the_recheck(db):
    clock = FakeTime("09:00:00")
    plant = Plant(db, clock)
    plant.ha.back["camera.cortile"] = float("inf")
    await clock.advance(120)
    await plant.event()
    # Guasto aperto dopo la tolleranza, ricontrollo in corso
    await clock.advance(90)
    checks = len(plant.ha.saved) + len(plant.ha.probes)
    assert plant.watch.watching == ["camera.cortile"] and set(plant.errors) == {"camera.cortile"}
    await plant.watch.stop()
    assert plant.watch.watching == []
    await clock.advance(3600)
    assert len(plant.ha.saved) + len(plant.ha.probes) == checks
    # Il guasto resta com'era: all'avvio dopo si riparte da capo
    assert set(plant.errors) == {"camera.cortile"}


async def test_cameras_are_rechecked_each_on_its_own(db):
    clock = FakeTime("09:00:00")
    plant = Plant(db, clock, cameras=("camera.cortile", "camera.cantina", "camera.quadro"))
    for camera in ("camera.cortile", "camera.cantina"):
        plant.ha.back[camera] = float("inf")
    await clock.advance(120)
    await plant.event("camera.cortile")
    await plant.event("camera.cantina")
    await plant.event("camera.quadro")
    await clock.advance(60)
    assert set(plant.errors) == {"camera.cortile", "camera.cantina"}
    assert plant.watch.watching == ["camera.cantina", "camera.cortile"]
    # La cantina torna, il cortile no
    plant.ha.back["camera.cantina"] = clock.now + 10
    await clock.advance(60)
    assert set(plant.errors) == {"camera.cortile"} and plant.watch.watching == ["camera.cortile"]
    # Una foto buona del cortile chiude il suo guasto e ferma il suo ricontrollo
    plant.ha.back["camera.cortile"] = clock.now
    await plant.event("camera.cortile")
    assert plant.errors == {} and plant.watch.watching == []
    # Una telecamera tolta dalla porta mentre è guasta: niente più guasto né ricontrollo
    plant.ha.back["camera.cantina"] = float("inf")
    await plant.event("camera.cantina")
    plant.cameras.discard("camera.cantina")
    await clock.advance(60)
    assert plant.errors == {} and plant.watch.watching == []


async def test_retry_within_five_seconds_keeps_the_photo_on_the_event(db):
    clock = FakeTime("09:00:00")
    plant = Plant(db, clock)
    # Appena avviata l'app la telecamera non risponde, e dopo 2 s sì: il tentativo dopo 3 s è ancora la foto
    # dell'evento
    plant.ha.back["camera.cortile"] = clock.now + 2
    event_id = await plant.event()
    await clock.advance(10)
    row = plant.row(event_id)
    assert row["snapshot"] == f"{event_id}_{main._file_stamp(row['ts'])}.jpg" and row["snapshot_error"] == ""
    assert plant.errors == {}


async def test_late_photo_is_never_attached_to_the_event(db):
    clock = FakeTime("09:00:00")
    plant = Plant(db, clock)
    # Torna dopo 7 s: il tentativo di 3 s dopo non riesce, quello di 6 s dopo è una prova, non la foto dell'evento
    plant.ha.back["camera.cortile"] = clock.now + 5.5
    event_id = await plant.event()
    await clock.advance(30)
    row = plant.row(event_id)
    assert row["snapshot"] == "" and row["snapshot_error"] == NOT_LOADED
    assert len(plant.ha.saved) == 2 and len(plant.ha.probes) == 1
    assert plant.errors == {} and plant.watch.watching == []


async def test_home_assistant_restart_gives_the_cameras_a_new_tolerance(db):
    """Home Assistant si riavvia con l'add-on acceso: la foto non riesce perché l'API non risponde; il guasto si apre
    solo dopo la tolleranza, e quando l'API torna la telecamera si riprova ogni pochi secondi finché non risponde."""
    clock = FakeTime("09:00:00")
    plant = Plant(db, clock)
    await clock.advance(3600)
    start = clock.now
    plant.ha.api_down_until = start + 150
    plant.ha.back["camera.cortile"] = start + 185
    await plant.event()
    assert plant.errors == {}
    await clock.advance(50)
    assert plant.errors == {}
    await clock.advance(20)
    # Ancora giù dopo 60 s: guasto aperto, «Home Assistant non risponde»
    assert plant.errors["camera.cortile"]["error"] == NO_ANSWER
    probes = len(plant.ha.probes)
    await clock.advance(80)
    # Con l'API giù le attese non superano mezzo minuto
    gaps = [b - a for (a, _), (b, _) in zip(plant.ha.probes[probes - 1 :], plant.ha.probes[probes:], strict=False)]
    assert gaps and max(gaps) <= API_DOWN_RECHECK
    # L'API torna alle 150 s e il ricontrollo se ne accorge entro mezzo minuto (170 s), con la telecamera ancora da
    # caricare: da lì un tentativo ogni 3 s, e il guasto si chiude appena la telecamera risponde
    await clock.until(start + 175)
    assert plant.errors["camera.cortile"]["error"] == NOT_LOADED
    await clock.until(start + 200)
    assert plant.errors == {} and plant.watch.watching == []
    assert [at - start for at, _ in plant.ha.probes if at >= start + 150] == [170, 173, 176, 179, 182, 185]


async def test_real_client_probe_follows_the_api_and_stays_quiet(caplog, monkeypatch):
    """HomeAssistant vero con il Supervisor finto: la prova non salva niente e scrive nel log solo in debug; un 502
    del Supervisor è l'API giù, la risposta dopo è il suo ritorno."""
    answers = [
        httpx.Response(502),
        httpx.Response(404),
        httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"}),
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return answers.pop(0)

    ha = HomeAssistant(token="x", snapshot_dir=None)
    ha._client = httpx.AsyncClient(base_url="http://ha/api", transport=httpx.MockTransport(handler))
    back = []
    ha.on_api_back = lambda: back.append(True)
    caplog.set_level(logging.DEBUG, logger="app.ha")
    assert await ha.probe_camera("camera.cortile") == "telecamera non disponibile (risposta 502 di Home Assistant)"
    assert ha.api_up is False and back == []
    assert await ha.probe_camera("camera.cortile") == NOT_LOADED
    assert ha.api_up is True and back == [True]
    assert await ha.probe_camera("camera.cortile") == ""
    assert calls == ["/api/camera_proxy/camera.cortile"] * 3
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # Mai altre API: solo entità camera.*
    assert await ha.probe_camera("switch.porta") == ""
    assert len(calls) == 3
    await ha.aclose()


@pytest.mark.parametrize("error", [httpx.ConnectError("rifiutata"), httpx.ReadTimeout("lenta")])
async def test_real_client_without_answer_is_the_api_down(error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    ha = HomeAssistant(token="x")
    ha._client = httpx.AsyncClient(base_url="http://ha/api", transport=httpx.MockTransport(handler))
    assert (await ha.probe_camera("camera.cortile")).startswith(NO_ANSWER)
    assert ha.api_up is False
    await ha.aclose()


async def test_photo_not_written_is_not_a_camera_to_recheck(db):
    """Foto arrivata ma non salvata (cartella Media in sola lettura): una prova riuscirebbe e chiuderebbe il guasto
    senza che niente sia cambiato. Resta come prima, fino alla prossima foto salvata, e non si ricontrolla."""
    from app.ha import MEDIA_NOT_WRITABLE

    clock = FakeTime("09:00:00")
    plant = Plant(db, clock)

    async def not_written(camera, name):
        return "", MEDIA_NOT_WRITABLE

    plant.ha.save_snapshot = not_written
    event_id = await plant.event()
    assert plant.errors["camera.cortile"]["error"] == MEDIA_NOT_WRITABLE
    assert plant.row(event_id)["snapshot_error"] == MEDIA_NOT_WRITABLE
    await clock.advance(600)
    assert plant.watch.watching == [] and plant.ha.probes == []
    assert set(plant.errors) == {"camera.cortile"}


async def test_core_restart_with_the_addon_running_gives_the_camera_its_tolerance(db, caplog):
    """Revisione: si aggiorna solo il core di Home Assistant e l'add-on resta acceso. Nessuna richiesta dell'app cade
    mentre il core è giù, quindi l'app non vede né l'API giù né il suo ritorno; la foto delle 20:03:04 trova la
    telecamera non ancora caricata (404). La prima foto non riuscita ha comunque la sua tolleranza: la telecamera torna
    alle 20:03:22 e il guasto non si apre mai, senza WARNING. La foto riuscita è di 18 s dopo: non va sull'evento."""
    caplog.set_level(logging.DEBUG, logger="app.camera_watch")
    clock = FakeTime("19:00:00")  # add-on acceso dalle 19:00
    plant = Plant(db, clock)
    plant.ha.back["camera.cortile"] = clock.local("20:03:22")
    await clock.until(clock.local("20:03:04"))
    assert plant.ha.api_up is None
    opened = await plant.event()
    while clock.now < clock.local("20:04:30"):
        await clock.advance(1)
        assert plant.errors == {}, "mai un guasto aperto"

    assert plant.watch.watching == []
    assert [at - clock.local("20:03:04") for at, _ in plant.ha.probes] == [6, 9, 12, 15, 18]
    assert plant.row(opened)["snapshot"] == "" and plant.row(opened)["snapshot_error"] == NOT_LOADED
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


HANGING = "camera.cancello"  # Home Assistant aspetta l'immagine fino al tempo scaduto
WORKING = "camera.ingresso"


def hanging_home_assistant(clock: FakeTime, tmp_path):
    """HomeAssistant vero con il Supervisor finto: una telecamera fa scadere il tempo (10 s, quanto il client), l'altra
    e il resto dell'API rispondono."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((clock.now, request.url.path))
        if request.url.path.endswith(HANGING):
            clock.now += 10
            raise httpx.ReadTimeout("timed out", request=request)
        if request.url.path.endswith("/states"):
            return httpx.Response(200, json=[{"entity_id": HANGING, "attributes": {}}])
        if request.url.path == "/core/api/":
            return httpx.Response(200, json={"message": "API running."})
        return httpx.Response(200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})

    ha = HomeAssistant(token="x", snapshot_dir=tmp_path)
    ha._client = httpx.AsyncClient(base_url="http://supervisor/core/api", transport=httpx.MockTransport(handler))
    return ha, calls


@pytest.mark.parametrize("other_photos", [20, 50])
async def test_one_camera_timing_out_is_not_home_assistant_down(db, tmp_path, caplog, other_photos):
    """Revisione: un tempo scaduto su una sola telecamera segnava giù tutta l'API di Home Assistant; la risposta dopo
    (la foto di un'altra porta, i nomi delle telecamere) la segnava «di nuovo raggiungibile», riapriva la tolleranza
    di tutte le telecamere e faceva riprovare quella spenta ogni 3 s: con un'altra porta che fa foto spesso il guasto
    non si apriva mai. Ora l'app chiede all'API se risponde (GET /api/): sì, quindi è la telecamera."""
    caplog.set_level(logging.INFO)
    clock = FakeTime("09:00:00")
    ha, calls = hanging_home_assistant(clock, tmp_path)
    errors: dict = {}
    watch = CameraWatch(ha, errors, clock=clock, sleep=clock.sleep)
    state = SimpleNamespace(db=db, ha=ha, snapshot_errors=errors, camera_watch=watch)

    async def event(camera):
        row = {"ts": clock.iso(), "kind": "apertura_remota", "door": "porta"}
        row["id"] = db.add_event(**row)
        await main._save_snapshot(state, camera, row)
        return row["id"]

    await clock.advance(3600)
    first = await event(HANGING)
    assert next(e for e in db.list_events() if e["id"] == first)["snapshot_error"] == CAMERA_TIMEOUT
    assert ha.api_up is True and errors == {}
    start, opened_at, refreshed = clock.now, None, clock.now
    while clock.now < start + 1800:
        await clock.advance(other_photos)
        await event(WORKING)
        if clock.now - refreshed >= 300:
            refreshed = clock.now
            await ha.states()  # nomi delle telecamere, con la pagina aperta
        if HANGING in errors and opened_at is None:
            opened_at = clock.now - start
        assert ha.api_up is True
    await watch.stop()
    await ha.aclose()

    # Il guasto si apre dopo la tolleranza di un minuto (più il tempo dei tentativi da 10 s), con il motivo giusto
    assert opened_at is not None and opened_at <= 60 + 10 + 3 + other_photos
    assert errors[HANGING]["error"] == CAMERA_TIMEOUT
    assert not [r for r in caplog.records if "di nuovo raggiungibile" in r.getMessage()]
    # Poi le attese crescono fino a 5 minuti: in mezz'ora pochi tentativi, non uno ogni 3 s
    hanging = [at for at, path in calls if path.endswith(HANGING)]
    assert len(hanging) <= 20
    assert [round(b - a) for a, b in zip(hanging[-3:], hanging[-2:], strict=False)] == [310, 310]


async def test_camera_timeout_with_the_api_down_is_still_home_assistant_down():
    """Tempo scaduto sulla telecamera e l'API che non risponde neanche lei: Home Assistant è giù, come prima."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/core/api/":
            return httpx.Response(502)
        raise httpx.ReadTimeout("timed out", request=request)

    ha = HomeAssistant(token="x")
    ha._client = httpx.AsyncClient(base_url="http://supervisor/core/api", transport=httpx.MockTransport(handler))
    assert await ha.probe_camera("camera.cortile") == "Home Assistant non risponde (tempo scaduto)"
    assert ha.api_up is False
    await ha.aclose()
