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

from .config import Settings, load_settings, resolve_mqtt
from .events import EventMonitor
from .vapix import A1210Client, VapixError

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Con Ingress le richieste arrivano solo dal Supervisor
INGRESS_PROXY = "172.30.32.2"


def create_app(settings: Settings | None = None, allow_any_client: bool | None = None) -> FastAPI:
    if allow_any_client is None:
        allow_any_client = os.environ.get("AAM_ALLOW_ANY_CLIENT") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings or load_settings()
        cfg = app.state.settings
        app.state.controller = A1210Client(cfg.a1210_host, cfg.a1210_username, cfg.a1210_password, cfg.a1210_verify_ssl)
        app.state.monitor = EventMonitor(prefix=cfg.mqtt_topic_prefix)
        app.state.mqtt_configured = False
        task = None
        mqtt = await resolve_mqtt(cfg)
        if mqtt:
            app.state.mqtt_configured = True
            task = asyncio.create_task(app.state.monitor.run(mqtt))
        else:
            _LOGGER.warning("Nessun broker MQTT: installa Mosquitto o imposta mqtt_host")
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.controller.aclose()

    app = FastAPI(title="Axis Access Manager", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def only_ingress(request: Request, call_next):
        client = request.client.host if request.client else ""
        if not allow_any_client and client not in (INGRESS_PROXY, "127.0.0.1"):
            return JSONResponse({"detail": "accesso consentito solo da Home Assistant"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status(request: Request) -> dict:
        state = request.app.state
        return {
            "controller": await _controller_status(state.controller, state.settings),
            "mqtt": _mqtt_status(state.monitor, state.mqtt_configured),
        }

    return app


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
