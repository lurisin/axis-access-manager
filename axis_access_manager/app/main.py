"""Applicazione web servita in Home Assistant tramite Ingress."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings, resolve_mqtt
from .db import DB_PATH, Database, local_day_start
from .events import EventMonitor
from .ha import HomeAssistant
from .ha_mqtt import COMMANDS, HaBridge
from .rules_service import RulesService
from .service import AccessService, ServiceError
from .transfer import events_csv, import_persons, persons_csv
from .vapix import A1210Client, VapixError

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Con Ingress le richieste arrivano solo dal Supervisor
INGRESS_PROXY = "172.30.32.2"

# Eventi per cui salvare un'istantanea della telecamera
SNAPSHOT_KINDS = {"negato", "sconosciuta", "citofono", "allarme", "manomissione"}
GRANTED_KINDS = {"concesso", "lettura", "apertura_remota"}
EXPORT_LIMIT = 50000

SYNC_INTERVAL = 15 * 60
SYNC_RETRY = 60
PRUNE_INTERVAL = 24 * 3600


class PersonIn(BaseModel):
    name: str
    card: str
    group: str = ""
    valid_from: str = ""
    valid_to: str = ""
    company: str = ""
    notes: str = ""


class PersonUpdateIn(BaseModel):
    name: str | None = None
    company: str | None = None
    notes: str | None = None


class AccessIn(BaseModel):
    group: str = ""
    valid_from: str = ""
    valid_to: str = ""


class CardIn(BaseModel):
    card: str


class CardStateIn(BaseModel):
    enabled: bool


class DoorIn(BaseModel):
    extended: bool = False


class ScheduleRuleIn(BaseModel):
    days: list[str]
    start: str
    end: str


class ScheduleIn(BaseModel):
    name: str
    description: str = ""
    rules: list[ScheduleRuleIn]
    holidays: bool = True


class GroupIn(BaseModel):
    name: str
    description: str = ""
    schedule: str


class HolidayIn(BaseModel):
    day: str
    name: str = ""
    yearly: bool = False


class ItalianHolidaysIn(BaseModel):
    year: int = Field(default_factory=lambda: date.today().year)


class ImportIn(BaseModel):
    csv: str
    apply: bool = False


def request_user(request: Request) -> str:
    """Utente di Home Assistant che sta usando la pagina (intestazioni aggiunte da Ingress)."""
    return (
        request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name") or "sconosciuto"
    )


def is_readonly(request: Request, settings: Settings) -> bool:
    username = (request.headers.get("X-Remote-User-Name") or "").lower()
    return bool(username) and username in settings.readonly_users


def create_app(
    settings: Settings | None = None,
    allow_any_client: bool | None = None,
    controller: A1210Client | None = None,
    db_path: Path | str | None = None,
    home_assistant: HomeAssistant | None = None,
) -> FastAPI:
    if allow_any_client is None:
        allow_any_client = os.environ.get("AAM_ALLOW_ANY_CLIENT") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or load_settings()
        app.state.settings = cfg
        app.state.controller = controller or A1210Client(
            cfg.a1210_host, cfg.a1210_username, cfg.a1210_password, cfg.a1210_verify_ssl
        )
        app.state.db = Database(db_path or os.environ.get("AAM_DB_PATH") or DB_PATH)
        app.state.ha = home_assistant or HomeAssistant()
        app.state.monitor = EventMonitor(prefix=cfg.mqtt_topic_prefix, door_token=cfg.door_token)
        app.state.service = AccessService(app.state.db, app.state.controller, cfg, app.state.monitor)
        app.state.rules = RulesService(app.state.service)
        app.state.bridge = HaBridge(cfg, app.state.monitor, app.state.db)
        _wire(app)
        app.state.mqtt_configured = False
        app.state.last_sync = {"ok": None, "error": "", "result": None, "at": None}
        tasks = [asyncio.create_task(_maintenance(app))]
        mqtt = await resolve_mqtt(cfg)
        if mqtt:
            app.state.mqtt_configured = True
            tasks.append(asyncio.create_task(app.state.monitor.run(mqtt)))
        else:
            _LOGGER.warning("Nessun broker MQTT: installa Mosquitto o imposta mqtt_host")
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.controller.aclose()
        await app.state.ha.aclose()
        app.state.db.close()

    app = FastAPI(title="Axis Access Manager", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def only_ingress(request: Request, call_next):
        client = request.client.host if request.client else ""
        if not allow_any_client and client not in (INGRESS_PROXY, "127.0.0.1"):
            return JSONResponse({"detail": "accesso consentito solo da Home Assistant"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.url.path.startswith("/api/"):
            cfg = getattr(request.app.state, "settings", None)
            if cfg and is_readonly(request, cfg):
                return JSONResponse({"detail": "il tuo utente può solo consultare"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, err: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=400)

    @app.exception_handler(VapixError)
    async def vapix_error(request: Request, err: VapixError) -> JSONResponse:
        return JSONResponse({"detail": f"errore del controller: {err}"}, status_code=502)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/me")
    async def me(request: Request) -> dict:
        cfg = request.app.state.settings
        return {
            "user": request_user(request),
            "readonly": is_readonly(request, cfg),
            "default_group": cfg.access_profile,
            "door": cfg.door_token,
            "version": __version__,
        }

    @app.get("/api/status")
    async def status(request: Request) -> dict:
        state = request.app.state
        cfg = state.settings
        return {
            "controller": await _controller_status(state.controller, cfg),
            "mqtt": _mqtt_status(state.monitor, state.mqtt_configured),
            "sync": state.last_sync,
            "home_assistant": {
                "api": state.ha.available,
                "integration": cfg.ha_integration,
                "door_buttons": cfg.ha_door_buttons,
                "topic_prefix": cfg.ha_topic_prefix,
                "snapshot_camera": cfg.snapshot_camera,
                "snapshot_on_granted": cfg.snapshot_on_granted,
            },
            "options": {
                "door_token": cfg.door_token,
                "access_profile": cfg.access_profile,
                "event_retention_days": cfg.event_retention_days,
                "readonly_users": list(cfg.readonly_users),
            },
        }

    @app.get("/api/summary")
    async def summary(request: Request) -> dict:
        """Dati leggeri per l'intestazione: porta e tessere da registrare."""
        state = request.app.state
        return {
            "door_mode": state.monitor.door_mode,
            "mqtt_connected": state.monitor.connected,
            "unknown_cards": len(state.db.list_unknown_cards()),
            "sync": state.last_sync,
        }

    @app.get("/api/dashboard")
    async def dashboard(request: Request) -> dict:
        db = request.app.state.db
        today = db.count_events(local_day_start())
        week = _week_series(db.event_kinds_since(local_day_start(6)))
        persons = db.list_persons()
        cards = [c for p in persons for c in p["cards"]]
        soon = (datetime.now().astimezone() + timedelta(days=7)).isoformat()
        now = datetime.now().astimezone().isoformat()
        expiring = []
        for person in persons:
            ends = [c["valid_to"] for c in person["cards"] if c.get("valid_to")]
            if ends and now < _iso(min(ends)) <= soon:
                expiring.append({"id": person["id"], "name": person["name"], "valid_to": min(ends)})
        return {
            "today": {
                "granted": today.get("concesso", 0),
                "denied": today.get("negato", 0),
                "unknown": today.get("sconosciuta", 0),
                "remote": today.get("apertura_remota", 0),
                "alarms": today.get("allarme", 0) + today.get("manomissione", 0),
                "intercom": today.get("citofono", 0),
                "reads": today.get("lettura", 0),
            },
            "week": week,
            "persons": len(persons),
            "cards": len(cards),
            "cards_suspended": sum(1 for c in cards if not c["enabled"]),
            "unknown_cards": len(db.list_unknown_cards()),
            "expiring": sorted(expiring, key=lambda e: e["valid_to"]),
            "recent": db.list_events(limit=8),
        }

    @app.post("/api/sync")
    async def sync(request: Request) -> dict:
        result = await request.app.state.service.sync_from_controller()
        request.app.state.last_sync = {"ok": True, "error": "", "result": result, "at": _now()}
        request.app.state.db.add_audit(request_user(request), "sincronizzazione", _sync_text(result))
        return result

    # Persone e tessere

    @app.get("/api/persons")
    async def persons(request: Request) -> list[dict]:
        db = request.app.state.db
        last = db.last_accesses()
        result = db.list_persons()
        for person in result:
            person["last_access"] = last.get(person["id"], "")
        return result

    @app.post("/api/persons")
    async def create_person(body: PersonIn, request: Request) -> dict:
        return await request.app.state.service.create_person(
            body.name,
            body.card,
            request_user(request),
            body.group,
            body.valid_from,
            body.valid_to,
            body.company,
            body.notes,
        )

    @app.get("/api/persons/{person_id}")
    async def person_detail(person_id: int, request: Request) -> dict:
        db = request.app.state.db
        person = db.get_person(person_id)
        if person is None:
            raise ServiceError("persona non trovata")
        person["events"] = db.list_events(person_id=person_id, limit=50)
        person["last_access"] = db.last_accesses().get(person_id, "")
        return person

    @app.patch("/api/persons/{person_id}")
    async def update_person(person_id: int, body: PersonUpdateIn, request: Request) -> dict:
        return await request.app.state.service.update_person(
            person_id, request_user(request), name=body.name, company=body.company, notes=body.notes
        )

    @app.put("/api/persons/{person_id}/access")
    async def set_access(person_id: int, body: AccessIn, request: Request) -> dict:
        return await request.app.state.service.set_person_access(
            person_id, body.group, body.valid_from, body.valid_to, request_user(request)
        )

    @app.delete("/api/persons/{person_id}")
    async def delete_person(person_id: int, request: Request) -> dict:
        await request.app.state.service.delete_person(person_id, request_user(request))
        return {"ok": True}

    @app.post("/api/persons/{person_id}/cards")
    async def add_card(person_id: int, body: CardIn, request: Request) -> dict:
        return await request.app.state.service.add_card(person_id, body.card, request_user(request))

    @app.patch("/api/cards/{card_id}")
    async def set_card_state(card_id: int, body: CardStateIn, request: Request) -> dict:
        return await request.app.state.service.set_card_enabled(card_id, body.enabled, request_user(request))

    @app.delete("/api/cards/{card_id}")
    async def delete_card(card_id: int, request: Request) -> dict:
        person = await request.app.state.service.delete_card(card_id, request_user(request))
        return {"ok": True, "person": person}

    @app.get("/api/unknown-cards")
    async def unknown_cards(request: Request) -> list[dict]:
        return request.app.state.db.list_unknown_cards()

    @app.delete("/api/unknown-cards/{number}")
    async def ignore_unknown_card(number: str, request: Request) -> dict:
        request.app.state.db.forget_unknown_card(number)
        return {"ok": True}

    # Registro

    @app.get("/api/events")
    async def events(
        request: Request,
        kind: str = "",
        q: str = "",
        before_id: int | None = None,
        limit: int = 100,
        since: str = "",
        until: str = "",
        person_id: int | None = None,
    ) -> list[dict]:
        return request.app.state.db.list_events(
            kind=kind,
            search=q,
            before_id=before_id,
            limit=min(limit, 500),
            since=_utc(since),
            until=_utc(until),
            person_id=person_id,
        )

    @app.get("/api/events/{event_id}/snapshot")
    async def snapshot(event_id: int, request: Request) -> Response:
        event = request.app.state.db.get_event(event_id)
        if not event or not event.get("snapshot"):
            return JSONResponse({"detail": "nessuna istantanea"}, status_code=404)
        path = request.app.state.ha.snapshot_dir / Path(event["snapshot"]).name
        if not path.exists():
            return JSONResponse({"detail": "istantanea cancellata"}, status_code=404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/export/events.csv")
    async def export_events(
        request: Request, kind: str = "", q: str = "", since: str = "", until: str = "", person_id: int | None = None
    ) -> Response:
        rows = request.app.state.db.list_events(
            kind=kind, search=q, since=_utc(since), until=_utc(until), person_id=person_id, limit=EXPORT_LIMIT
        )
        return _csv_response(events_csv(rows), "registro_accessi")

    @app.get("/api/export/persons.csv")
    async def export_persons(request: Request) -> Response:
        state = request.app.state
        names = await _group_names(state)
        return _csv_response(persons_csv(state.db.list_persons(), names, state.db.last_accesses()), "anagrafica")

    @app.post("/api/import/persons")
    async def import_csv(body: ImportIn, request: Request) -> dict:
        state = request.app.state
        try:
            groups = (await state.rules.list_groups())["groups"]
        except VapixError as err:
            _LOGGER.warning("Gruppi non leggibili per l'importazione: %s", err)
            groups = []
        return await import_persons(state.service, body.csv, groups, request_user(request), body.apply)

    @app.get("/api/audit")
    async def audit(request: Request, before_id: int | None = None, limit: int = 100) -> list[dict]:
        return request.app.state.db.list_audit(limit=min(limit, 500), before_id=before_id)

    # Regole

    @app.get("/api/groups")
    async def groups(request: Request) -> dict:
        return await request.app.state.rules.list_groups()

    @app.post("/api/groups")
    async def create_group(body: GroupIn, request: Request) -> dict:
        return await request.app.state.rules.create_group(
            body.name, body.description, body.schedule, request_user(request)
        )

    @app.put("/api/groups/{token:path}")
    async def update_group(token: str, body: GroupIn, request: Request) -> dict:
        return await request.app.state.rules.update_group(
            token, body.name, body.description, body.schedule, request_user(request)
        )

    @app.delete("/api/groups/{token:path}")
    async def delete_group(token: str, request: Request) -> dict:
        await request.app.state.rules.delete_group(token, request_user(request))
        return {"ok": True}

    @app.get("/api/schedules")
    async def schedules(request: Request) -> list[dict]:
        return await request.app.state.rules.list_schedules()

    @app.post("/api/schedules")
    async def create_schedule(body: ScheduleIn, request: Request) -> dict:
        return await request.app.state.rules.create_schedule(
            body.name, body.description, [r.model_dump() for r in body.rules], body.holidays, request_user(request)
        )

    @app.put("/api/schedules/{token:path}")
    async def update_schedule(token: str, body: ScheduleIn, request: Request) -> dict:
        return await request.app.state.rules.update_schedule(
            token,
            body.name,
            body.description,
            [r.model_dump() for r in body.rules],
            body.holidays,
            request_user(request),
        )

    @app.delete("/api/schedules/{token:path}")
    async def delete_schedule(token: str, request: Request) -> dict:
        await request.app.state.rules.delete_schedule(token, request_user(request))
        return {"ok": True}

    @app.get("/api/holidays")
    async def holidays(request: Request) -> list[dict]:
        return request.app.state.db.list_holidays()

    @app.post("/api/holidays")
    async def add_holiday(body: HolidayIn, request: Request) -> dict:
        await request.app.state.rules.add_holidays([body.model_dump()], request_user(request))
        return {"ok": True}

    @app.post("/api/holidays/italia")
    async def add_italian_holidays(body: ItalianHolidaysIn, request: Request) -> dict:
        await request.app.state.rules.add_italian_holidays(body.year, request_user(request))
        return {"ok": True}

    @app.delete("/api/holidays/{day}")
    async def delete_holiday(day: str, request: Request) -> dict:
        await request.app.state.rules.delete_holiday(day, request_user(request))
        return {"ok": True}

    @app.get("/api/cameras")
    async def cameras(request: Request) -> list[dict]:
        return await request.app.state.ha.states()

    # Porta

    @app.post("/api/door/open")
    async def open_door(body: DoorIn, request: Request) -> dict:
        await request.app.state.service.open_door(body.extended, request_user(request))
        return {"ok": True}

    return app


def _wire(app: FastAPI) -> None:
    """Collega eventi, istantanee e Home Assistant."""
    state = app.state
    cfg: Settings = state.settings
    monitor: EventMonitor = state.monitor
    service: AccessService = state.service
    bridge: HaBridge = state.bridge
    monitor.on_event = service.record_event

    async def after_event(row: dict) -> None:
        kinds = SNAPSHOT_KINDS | (GRANTED_KINDS if cfg.snapshot_on_granted else set())
        if cfg.snapshot_camera and row.get("kind") in kinds:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = await state.ha.save_snapshot(cfg.snapshot_camera, f"{row['id']}_{stamp}")
            if filename:
                state.db.set_event_snapshot(row["id"], filename)
        await bridge.publish_event(row)

    async def on_change() -> None:
        await bridge.publish_state()

    service.after_event = after_event
    service.on_change = on_change
    if cfg.ha_integration:
        monitor.will = bridge.will()
        monitor.on_connect = bridge.on_connect
        if cfg.ha_door_buttons:
            monitor.command_topic = bridge.command_topic

            async def on_command(command: str) -> None:
                if command not in COMMANDS:
                    _LOGGER.warning("Comando sconosciuto da Home Assistant: %s", command)
                    return
                try:
                    await service.open_door(COMMANDS[command], "Home Assistant")
                except (ServiceError, VapixError) as err:
                    _LOGGER.warning("Apertura da Home Assistant non eseguita: %s", err)

            monitor.on_command = on_command
    else:
        # Rimuove le entità pubblicate in precedenza
        monitor.on_connect = bridge.on_connect


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(value: str) -> str:
    """Data del controller confrontabile con quelle locali in formato ISO."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().isoformat()
    except ValueError:
        return ""


def _utc(value: str) -> str:
    """Filtro data dalla pagina (ISO locale o con fuso) in UTC come i timestamp del log."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise ServiceError(f"data non valida: {value}") from err
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _week_series(rows: list[dict]) -> list[dict]:
    """Ultimi 7 giorni locali: accessi concessi, negati e tessere sconosciute per giorno."""
    today = datetime.now().astimezone().date()
    days = {today - timedelta(days=i): {"granted": 0, "denied": 0, "unknown": 0} for i in range(6, -1, -1)}
    for row in rows:
        try:
            day = datetime.fromisoformat(row["ts"]).astimezone().date()
        except ValueError:
            continue
        bucket = days.get(day)
        if bucket is None:
            continue
        if row["kind"] in ("concesso", "apertura_remota"):
            bucket["granted"] += 1
        elif row["kind"] == "negato":
            bucket["denied"] += 1
        elif row["kind"] == "sconosciuta":
            bucket["unknown"] += 1
    return [{"day": day.isoformat(), **counts} for day, counts in days.items()]


def _sync_text(result: dict) -> str:
    return f"{result['cards']} tessere, {result['added']} nuove, {result['removed']} rimosse"


def _csv_response(text: str, name: str) -> Response:
    filename = f"{name}_{datetime.now():%Y%m%d_%H%M}.csv"
    return Response(
        text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _group_names(state) -> dict[str, str]:
    try:
        profiles = await state.controller.list_access_profiles()
    except VapixError:
        return {}
    return {p.get("token", ""): p.get("Name") or p.get("token", "") for p in profiles}


async def _maintenance(app: FastAPI) -> None:
    """Importa periodicamente le tessere dal controller e cancella ogni giorno gli eventi vecchi."""
    state = app.state
    last_prune = 0.0
    loop = asyncio.get_running_loop()
    while True:
        delay = SYNC_INTERVAL
        if state.settings.a1210_host:
            try:
                result = await state.service.sync_from_controller()
                state.last_sync = {"ok": True, "error": "", "result": result, "at": _now()}
                _LOGGER.debug("Tessere sincronizzate dal controller: %s", result)
            except VapixError as err:
                state.last_sync = {"ok": False, "error": str(err), "result": None, "at": _now()}
                _LOGGER.warning("Sincronizzazione non riuscita: %s", err)
                delay = SYNC_RETRY
        if loop.time() - last_prune >= PRUNE_INTERVAL or last_prune == 0.0:
            state.ha.delete_snapshots(state.db.prune_events(state.settings.event_retention_days))
            last_prune = loop.time()
        await asyncio.sleep(delay)


async def _controller_status(controller: A1210Client, settings: Settings) -> dict:
    if not settings.a1210_host:
        return {"ok": False, "error": "indirizzo del controller non configurato"}
    try:
        info = await controller.device_info()
        doors = await controller.door_info_list()
        credentials = await controller.list_credentials()
    except VapixError as err:
        return {"ok": False, "error": str(err)}
    tokens = [door.get("token") for door in doors]
    return {
        "ok": True,
        "model": info.get("ProdNbr") or info.get("ProdShortName", ""),
        "firmware": info.get("Version", ""),
        "serial": info.get("SerialNumber", ""),
        "doors": tokens,
        "door_found": settings.door_token in tokens,
        "credentials": len(credentials),
    }


def _mqtt_status(monitor: EventMonitor, configured: bool) -> dict:
    return {
        "configured": configured,
        "connected": monitor.connected,
        "controller_online": monitor.controller_online,
        "error": monitor.last_error,
        "prefix": monitor.prefix,
        "door_mode": monitor.door_mode,
        "categories": {
            name: {
                "count": stats.count,
                "last_topic": stats.last_topic,
                "last_seen": stats.last_seen.isoformat() if stats.last_seen else None,
            }
            for name, stats in sorted(monitor.categories.items())
        },
        "access_events_seen": "AccessControl" in monitor.categories,
        "last_card": (
            {"card": monitor.last_card["card"], "read_at": monitor.last_card["read_at"].isoformat()}
            if monitor.last_card
            else None
        ),
    }
