"""Applicazione web servita in Home Assistant tramite Ingress."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .config import Settings, load_settings, resolve_mqtt
from .db import DB_PATH, Database
from .events import EventMonitor
from .service import AccessService, ServiceError
from .vapix import A1210Client, VapixError

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Con Ingress le richieste arrivano solo dal Supervisor
INGRESS_PROXY = "172.30.32.2"

SYNC_INTERVAL = 15 * 60
SYNC_RETRY = 60
PRUNE_INTERVAL = 24 * 3600


class PersonIn(BaseModel):
    name: str
    card: str


class RenameIn(BaseModel):
    name: str


class CardIn(BaseModel):
    card: str


class CardStateIn(BaseModel):
    enabled: bool


class DoorIn(BaseModel):
    extended: bool = False


def request_user(request: Request) -> str:
    """Utente di Home Assistant che sta usando la pagina (intestazioni aggiunte da Ingress)."""
    return (
        request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name") or "sconosciuto"
    )


def create_app(
    settings: Settings | None = None,
    allow_any_client: bool | None = None,
    controller: A1210Client | None = None,
    db_path: Path | str | None = None,
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
        app.state.monitor = EventMonitor(prefix=cfg.mqtt_topic_prefix, door_token=cfg.door_token)
        app.state.service = AccessService(app.state.db, app.state.controller, cfg, app.state.monitor)
        app.state.monitor.on_event = app.state.service.record_event
        app.state.mqtt_configured = False
        app.state.last_sync = {"ok": None, "error": "", "result": None}
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
        app.state.db.close()

    app = FastAPI(title="Axis Access Manager", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def only_ingress(request: Request, call_next):
        client = request.client.host if request.client else ""
        if not allow_any_client and client not in (INGRESS_PROXY, "127.0.0.1"):
            return JSONResponse({"detail": "accesso consentito solo da Home Assistant"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, err: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=400)

    @app.exception_handler(VapixError)
    async def vapix_error(request: Request, err: VapixError) -> JSONResponse:
        return JSONResponse({"detail": f"errore del controller: {err}"}, status_code=502)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status(request: Request) -> dict:
        state = request.app.state
        return {
            "controller": await _controller_status(state.controller, state.settings),
            "mqtt": _mqtt_status(state.monitor, state.mqtt_configured),
            "sync": state.last_sync,
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

    @app.post("/api/sync")
    async def sync(request: Request) -> dict:
        result = await request.app.state.service.sync_from_controller()
        request.app.state.last_sync = {"ok": True, "error": "", "result": result}
        request.app.state.db.add_audit(request_user(request), "sincronizzazione", str(result))
        return result

    @app.get("/api/persons")
    async def persons(request: Request) -> list[dict]:
        return request.app.state.db.list_persons()

    @app.post("/api/persons")
    async def create_person(body: PersonIn, request: Request) -> dict:
        return await request.app.state.service.create_person(body.name, body.card, request_user(request))

    @app.patch("/api/persons/{person_id}")
    async def rename_person(person_id: int, body: RenameIn, request: Request) -> dict:
        return await request.app.state.service.rename_person(person_id, body.name, request_user(request))

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

    @app.get("/api/events")
    async def events(
        request: Request, kind: str = "", q: str = "", before_id: int | None = None, limit: int = 100
    ) -> list[dict]:
        return request.app.state.db.list_events(kind=kind, search=q, before_id=before_id, limit=min(limit, 500))

    @app.post("/api/door/open")
    async def open_door(body: DoorIn, request: Request) -> dict:
        await request.app.state.service.open_door(body.extended, request_user(request))
        return {"ok": True}

    return app


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
                state.last_sync = {"ok": True, "error": "", "result": result}
                _LOGGER.debug("Tessere sincronizzate dal controller: %s", result)
            except VapixError as err:
                state.last_sync = {"ok": False, "error": str(err), "result": None}
                _LOGGER.warning("Sincronizzazione non riuscita: %s", err)
                delay = SYNC_RETRY
        if loop.time() - last_prune >= PRUNE_INTERVAL or last_prune == 0.0:
            state.db.prune_events(state.settings.event_retention_days)
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
