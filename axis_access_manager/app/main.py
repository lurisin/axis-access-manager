"""Applicazione web servita in Home Assistant tramite Ingress."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .backup import BACKUP_DIR, BackupError, BackupService
from .backup_copy import ha_copy_path, update_copies
from .config import Settings, load_settings, normalize_host, resolve_mqtt
from .db import DB_PATH, Database, local_day_start
from .events import EventMonitor
from .full_restore import FULL_SECTIONS
from .ha import HomeAssistant
from .ha_mqtt import HaBridge, parse_command
from .health import HEALTH_INTERVAL, HealthMonitor, clock_fault, clock_problem, controller_synced, paused_reason
from .installation import MAX_CONTROLLERS, InstallationService
from .intercom import IntercomBridge, IntercomSettings
from .notifications import FAULT_TYPES, NotificationService
from .programming import INPUT_ROLES, ProgrammingService
from .restore import ALL_SECTIONS, RESTORE_SECTIONS, Runtime, apply, plan, public
from .rules_service import RulesService
from .service import (
    CASING_OPEN,
    CONTROLLER_KINDS,
    DENIED_REASONS,
    INPUT_TERMINALS,
    AccessService,
    ServiceError,
    mass_missing,
    plural,
)
from .transfer import events_csv, import_persons, persons_csv
from .users import PERMISSIONS, ROLES, AppUser, UserDirectory, UserError, role_of
from .vapix import A1210Client, ControllerGuard, VapixError

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Con Ingress le richieste arrivano solo dal Supervisor
INGRESS_PROXY = "172.30.32.2"

# Eventi per cui salvare un'istantanea della telecamera
# Istantanea sempre: rifiuti, citofono, allarmi e aperture a distanza (chi entra senza tessera).
# Per una tessera una sola foto, sull'esito: la lettura e la tessera sconosciuta arrivano insieme al negato.
SNAPSHOT_KINDS = {"negato", "citofono", "allarme", "manomissione", "apertura_remota"}
GRANTED_KINDS = {"concesso"}
# Passaggi contati come accessi nei numeri del giorno: la lettura della tessera precede il concesso (sarebbe doppia)
# e le chiamate al citofono non sono accessi; le aperture dal citofono arrivano come apertura remota.
ACCESS_COUNT_KINDS = ("concesso", "apertura_remota")
EXPORT_LIMIT = 50000

# Recupero degli eventi persi: da poco prima dell'ultimo registrato, al massimo degli ultimi 7 giorni
RECOVER_OVERLAP = 10
RECOVER_LIMIT = timedelta(days=7)

SYNC_INTERVAL = 15 * 60
SYNC_RETRY = 60
PRUNE_INTERVAL = 24 * 3600
# Ogni quanto si guarda se è ora di un backup pianificato o della verifica
BACKUP_TICK = 60


class PersonIn(BaseModel):
    name: str
    card: str
    group: str = ""
    valid_from: str = ""
    valid_to: str = ""
    company: str = ""
    notes: str = ""
    category_id: int = 0


class PersonUpdateIn(BaseModel):
    name: str | None = None
    company: str | None = None
    notes: str | None = None


class AccessIn(BaseModel):
    group: str = ""
    valid_from: str = ""
    valid_to: str = ""
    category_id: int | None = None


class CardIn(BaseModel):
    card: str


class CardStateIn(BaseModel):
    enabled: bool


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


class CategoryRuleIn(BaseModel):
    target: str
    target_id: int
    schedule: str


class CategoryIn(BaseModel):
    name: str
    color: str = ""
    valid_days: int = 0
    description: str = ""
    rules: list[CategoryRuleIn] = []


class DefaultCategoryIn(BaseModel):
    category_id: int


class HiddenIn(BaseModel):
    kind: str
    token: str
    hidden: bool


class DefaultGroupIn(BaseModel):
    token: str


class ControllerIn(BaseModel):
    name: str
    host: str
    username: str
    password: str = ""
    verify_ssl: bool = False
    mqtt_topic_prefix: str
    area_id: int | None = None
    description: str = ""


class ControllerTestIn(BaseModel):
    host: str
    username: str
    password: str = ""
    verify_ssl: bool = False
    controller_id: int | None = None


class DoorConfigIn(BaseModel):
    door_token: str
    name: str
    snapshot_camera: str = ""
    snapshot_on_granted: bool = False
    ha_buttons: bool = True
    intercom_name: str = ""
    intercom_open_door: bool = False
    intercom_relay: int = 1
    intercom_mqtt_prefix: str = "hmd"
    area_id: int | None = None


class SiteIn(BaseModel):
    name: str
    description: str = ""


class AreaIn(BaseModel):
    name: str
    site_id: int | None = None


class HardwareDoorIn(BaseModel):
    name: str
    description: str = ""
    access_time: float | None = None
    extended_access_time: float | None = None
    open_too_long_time: float | None = None
    extended_open_too_long_time: float | None = None
    pre_alarm_time: float | None = None


class HardwareReaderIn(BaseModel):
    name: str
    description: str = ""


class ControllerInputIn(BaseModel):
    name: str = ""
    role: str = ""
    door: str = ""
    contact: str = "no"
    supervised: bool = False


class LinkIn(BaseModel):
    reader: str
    door: str
    direction: str = ""
    enabled: bool = True


class LinksIn(BaseModel):
    links: list[LinkIn]


class SeenIn(BaseModel):
    ids: list[str] | None = None


# Riquadri del cruscotto; larghezza in quarti di riga (1-4), altezza in righe della griglia
DASHBOARD_WIDGETS = (
    "da_gestire",
    "accessi_oggi",
    "negati_oggi",
    "allarmi_oggi",
    "persone",
    "porte",
    "settimana",
    "ultimi_eventi",
    "scadenze",
    "da_registrare",
    "impianto",
    "backup",
)
SETTING_DASHBOARD = "dashboard_layout:"
CAMERA_NAMES_TTL = 300


class ControllerCameraIn(BaseModel):
    camera: str = Field(default="", max_length=255)


class DashboardWidgetIn(BaseModel):
    type: str
    size: int = Field(ge=1, le=4)
    height: int | None = Field(default=None, ge=1, le=12)


class DashboardLayoutIn(BaseModel):
    widgets: list[DashboardWidgetIn] = Field(max_length=len(DASHBOARD_WIDGETS))


class MissingCardsIn(BaseModel):
    # Nessun elenco = tutte le tessere non presenti
    ids: list[int] | None = None


class HardwareAccessPointIn(BaseModel):
    name: str | None = None
    door: str | None = None
    readers: list[str] | None = None
    direction: str | None = None
    enabled: bool | None = None


class BackupIn(BaseModel):
    name: str = Field("", max_length=200)


class BackupLockIn(BaseModel):
    locked: bool


class BackupPolicyIn(BaseModel):
    auto: bool | None = None
    frequency: Literal["ora", "6ore", "notte"] | None = None
    night: str | None = Field(None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")
    before_changes: bool | None = None
    keep_last: int | None = Field(None, ge=1, le=1000)
    keep_daily: int | None = Field(None, ge=0, le=1000)
    keep_weekly: int | None = Field(None, ge=0, le=1000)
    keep_monthly: int | None = Field(None, ge=0, le=1000)
    keep_yearly: int | None = Field(None, ge=0, le=100)
    # Se le regole nuove eliminano dei backup serve la conferma: senza, la risposta dice solo quanti
    confirm: bool = False


class RestorePreviewIn(BaseModel):
    sections: list[str] = Field(default_factory=lambda: list(RESTORE_SECTIONS))
    # Solo una parte (per esempio "controller:1" dalla scheda Backup del controller)
    part: str = ""


class RestoreIn(RestorePreviewIn):
    keys: list[str] = Field(default_factory=list, max_length=5000)
    # Ripristino completo: password del broker MQTT del controller, che nei backup non c'è. Non si salva
    mqtt_password: str = Field("", max_length=256)


class AppUserIn(BaseModel):
    username: str = ""
    name: str = ""
    perms: list[str] = []


def current_user(request: Request) -> AppUser:
    """Utente dell'app riconosciuto dalle intestazioni che Ingress aggiunge a ogni richiesta."""
    user = getattr(request.state, "user", None)
    if user is None:
        user = request.app.state.users.resolve(
            request.headers.get("X-Remote-User-Name") or "",
            request.headers.get("X-Remote-User-Display-Name") or "",
        )
        request.state.user = user
    return user


def request_user(request: Request) -> str:
    return current_user(request).label


def need(*perms: str, any_of: bool = False) -> Any:
    """Dipendenza: l'utente deve avere tutti i permessi indicati (o almeno uno con any_of)."""

    def check(request: Request) -> AppUser:
        user = current_user(request)
        allowed = any(user.can(p) for p in perms) if any_of else user.can(*perms)
        if not allowed:
            missing = ", ".join(PERMISSIONS[p].lower() for p in perms if not user.can(p))
            raise HTTPException(status_code=403, detail=f"il tuo utente non ha il permesso: {missing}")
        return user

    return Depends(check)


Admin = Annotated[AppUser, need("utenti")]


def before_change(kind: str = "prima_modifica") -> Any:
    """Dipendenza: backup prima di una modifica rischiosa, se è cambiato qualcosa. Va dopo il controllo dei
    permessi; una serie di modifiche di seguito fa un solo backup."""

    async def backup(request: Request) -> None:
        state = request.app.state
        await state.backups.before_change(
            kind, request_user(request), _backup_clients(state), _offline_controllers(state)
        )

    return Depends(backup)


def create_app(
    settings: Settings | None = None,
    allow_any_client: bool | None = None,
    controller: A1210Client | None = None,
    db_path: Path | str | None = None,
    home_assistant: HomeAssistant | None = None,
    scheduled_backups: bool | None = None,
) -> FastAPI:
    if allow_any_client is None:
        allow_any_client = os.environ.get("AAM_ALLOW_ANY_CLIENT") == "1"
    if scheduled_backups is None:
        # I test li spengono: un backup che parte da solo cambierebbe notifiche e attività sotto i loro piedi
        scheduled_backups = os.environ.get("AAM_SCHEDULED_BACKUPS", "1") != "0"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        options = settings or load_settings()
        app.state.options = options
        app.state.injected_controller = controller
        app.state.db = Database(db_path or os.environ.get("AAM_DB_PATH") or DB_PATH, __version__)
        if app.state.db.recovered:
            _LOGGER.error("All'avvio: %s", app.state.db.recovered)
        if app.state.db.copied:
            _LOGGER.info("Copia del database prima dell'aggiornamento: %s", app.state.db.copied)
        app.state.users = UserDirectory(app.state.db, options.readonly_users)
        app.state.users.add_status_permission()
        app.state.installation = InstallationService(app.state.db)
        app.state.installation.migrate_from_options(options)
        app.state.installation.ensure_default_site()
        app.state.installation.update_controller_event_names()
        app.state.notifications = NotificationService(app.state.db)
        db_folder = app.state.db.path.parent if app.state.db.path else Path(tempfile.mkdtemp())
        app.state.backups = BackupService(app.state.db, db_folder / BACKUP_DIR, __version__)
        app.state.ha = home_assistant or HomeAssistant()
        app.state.snapshot_errors = {}
        # Nomi delle telecamere di Home Assistant (entity_id → friendly name), riletti ogni CAMERA_NAMES_TTL
        app.state.camera_names = {}
        app.state.camera_names_at = None
        app.state.reload_lock = asyncio.Lock()
        app.state.last_sync = {"ok": None, "error": "", "result": None, "at": None}
        app.state.mqtt = await resolve_mqtt(options)
        if not app.state.mqtt:
            _LOGGER.warning("Nessun broker MQTT: installa Mosquitto o imposta mqtt_host")
        if moved := await asyncio.to_thread(app.state.ha.move_snapshots):
            _LOGGER.info("%s istantanee spostate in %s", moved, app.state.ha.snapshot_dir)
        app.state.health = HealthMonitor(app.state.db, lambda row: _health_client(app, row))
        # La prima sincronizzazione aspetta il primo controllo: un controller cambiato non va letto
        app.state.health_ready = asyncio.Event()
        # Controller tornato raggiungibile (riavvio, reset): le tessere si rileggono subito, non al giro dei 15 minuti
        app.state.sync_wake = asyncio.Event()
        app.state.last_recovery = None
        await _start_runtime(app)
        maintenance = asyncio.create_task(_maintenance(app))
        health = asyncio.create_task(_health_loop(app))
        tasks = [maintenance, health]
        if scheduled_backups:
            tasks.append(asyncio.create_task(_backup_loop(app)))
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.health.aclose()
        await _stop_runtime(app)
        await app.state.ha.aclose()
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

    @app.exception_handler(UserError)
    async def user_error(request: Request, err: UserError) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=400)

    @app.exception_handler(VapixError)
    async def vapix_error(request: Request, err: VapixError) -> JSONResponse:
        return JSONResponse({"detail": f"errore del controller: {err}"}, status_code=502)

    @app.get("/")
    async def index() -> FileResponse:
        # Mai dalla cache: dopo un aggiornamento dell'add-on serve subito la pagina nuova
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/me")
    async def me(request: Request) -> dict:
        user = current_user(request)
        perms = list(PERMISSIONS) if user.can("utenti") else [p for p in PERMISSIONS if p in user.perms]
        return {
            "user": user.label,
            "username": user.username,
            "perms": perms,
            "admin": user.can("utenti"),
            "role": ROLES.get(role_of(perms), ("",))[0],
            "default_group": request.app.state.service.default_group,
            "default_category": request.app.state.rules.default_category,
            "door": request.app.state.settings.door_token,
            "version": __version__,
        }

    @app.get("/api/status", dependencies=[need("impianti", "import_export", "utenti", any_of=True)])
    async def status(request: Request, controller: bool = True) -> dict:
        """Stato di collegamenti e integrazioni; controller=false evita di interrogare il controller."""
        state = request.app.state
        cfg = state.settings
        return {
            "controller": await _controller_status(state.controller, cfg, state.db.all_controller_inputs())
            if controller
            else None,
            "mqtt": _mqtt_status(state.monitor, state.mqtt_configured),
            "sync": state.last_sync,
            "home_assistant": {
                "api": state.ha.available,
                "integration": cfg.ha_integration,
                "topic_prefix": cfg.ha_topic_prefix,
                "entities": [c["name"] for _, _, c in state.bridge._entities()] if cfg.ha_integration else [],
            },
            "doors": _doors_status(state),
            "options": {
                "default_group": state.service.default_group,
                "event_retention_days": cfg.event_retention_days,
                "ha_integration": cfg.ha_integration,
                "ha_topic_prefix": cfg.ha_topic_prefix,
                "mqtt_override": bool(state.options.mqtt),
            },
            "intercoms": [_intercom_status(i) for i in state.intercoms],
            "controllers": _controllers_health(state),
            "recovery": state.last_recovery,
            "database": {
                "schema": state.db.schema_version,
                "copies": await asyncio.to_thread(update_copies, state.db.path) if state.db.path else [],
                "ha_copy": _file_time(ha_copy_path(state.db.path)) if state.db.path else "",
            },
            # Ultima voce, a risposta pronta: il browser la confronta con la sua ora
            "now": _now(),
        }

    @app.get("/api/summary")
    async def summary(request: Request) -> dict:
        """Dati leggeri per l'intestazione: porta e tessere da registrare.

        Tutti leggono i nomi delle porte (registro, tipologie); stato e notifiche solo con il permesso "stato".
        """
        state = request.app.state
        user = current_user(request)
        status = user.can("stato")
        doors = _doors_status(state)
        names = await _camera_names(state)
        listing = _notification_listing(request) if status else None
        return {
            "version": __version__,
            "door_mode": state.monitor.door_mode if status else None,
            "doors": doors if status else [{**d, "mode": None} for d in doors],
            "configured": bool(state.settings.a1210_host),
            "mqtt_connected": state.monitor.connected if status else None,
            "unknown_cards": len(state.db.list_unknown_cards()) if user.can("persone") else 0,
            "sync": state.last_sync,
            "notifications": listing["open"] if listing else 0,
            # Barra in alto: guasti finché durano, allarmi finché non se ne prende visione
            "faults": [_brief(i) for i in listing["items"] if i["type"] in FAULT_TYPES] if listing else [],
            "alarms": [_brief(i) for i in listing["items"] if i["type"] == "door_alarm" and not i["seen"]]
            if listing
            else [],
            # Per mostrare le telecamere delle porte con il nome di Home Assistant
            "camera_names": {d["camera"]: names.get(d["camera"], "") for d in doors if d["camera"]},
        }

    @app.get("/api/notifications", dependencies=[need("stato")])
    async def notifications(request: Request) -> dict:
        return _notification_listing(request)

    @app.post("/api/notifications/seen", dependencies=[need("stato")])
    async def notifications_seen(body: SeenIn, request: Request) -> dict:
        """Segna come viste le notifiche indicate, o tutte quelle che l'utente vede."""
        listing = _notification_listing(request)
        ids = body.ids if body.ids is not None else [i["id"] for i in listing["items"]]
        state = request.app.state
        state.notifications.mark_seen(ids, _current_notifications(state))
        # Chi ha preso visione di cosa: nel registro attività, come le altre modifiche
        marked = [i for i in listing["items"] if i["id"] in ids and not i["seen"]]
        if marked:
            titles = ", ".join(f"{i['title']} ({i['text']})" if i["text"] else i["title"] for i in marked[:3])
            more = f" e altre {len(marked) - 3}" if len(marked) > 3 else ""
            action = "presa visione" if len(marked) == 1 else f"segnate come viste {len(marked)} notifiche"
            state.db.add_audit(request_user(request), action, titles + more)
        return {"open": _notification_listing(request)["open"]}

    @app.put("/api/notifications/settings", dependencies=[need("utenti")])
    async def notification_settings(body: dict[str, str], request: Request) -> dict:
        request.app.state.notifications.set_levels(body, request_user(request))
        return {"types": request.app.state.notifications.types()}

    @app.get("/api/dashboard", dependencies=[need("stato")])
    async def dashboard(request: Request) -> dict:
        user = current_user(request)
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
            # Nomi e passaggi solo a chi può vedere anagrafica e registro
            "expiring": sorted(expiring, key=lambda e: e["valid_to"]) if user.can("persone") else [],
            "recent": db.list_events(limit=8) if user.can("registro") else [],
            "backup": await asyncio.to_thread(request.app.state.backups.overview) if user.can("backup") else None,
            "system": {
                "controller_online": request.app.state.monitor.controller_online,
                "mqtt_connected": request.app.state.monitor.connected if request.app.state.mqtt else None,
                "casing_open": request.app.state.monitor.casing_open,
                "last_event": next(iter(db.list_events(limit=1)), {}).get("ts"),
                "configured": bool(request.app.state.settings.a1210_host),
            },
        }

    @app.get("/api/dashboard/layout", dependencies=[need("stato")])
    async def dashboard_layout(request: Request) -> dict:
        """Cruscotto dell'utente: riquadri in ordine con larghezza e altezza; null se usa quello predefinito."""
        saved = request.app.state.db.get_setting(_dashboard_key(request))
        try:
            widgets = json.loads(saved) if saved else None
        except ValueError:
            widgets = None
        return {"widgets": widgets, "types": list(DASHBOARD_WIDGETS)}

    @app.put("/api/dashboard/layout", dependencies=[need("stato")])
    async def save_dashboard_layout(body: DashboardLayoutIn, request: Request) -> dict:
        widgets = []
        for widget in body.widgets:
            if widget.type not in DASHBOARD_WIDGETS:
                raise HTTPException(status_code=400, detail=f"riquadro sconosciuto: {widget.type}")
            if any(w["type"] == widget.type for w in widgets):
                raise HTTPException(status_code=400, detail=f"riquadro ripetuto: {widget.type}")
            widgets.append({"type": widget.type, "size": widget.size})
            if widget.height:
                widgets[-1]["height"] = widget.height
        request.app.state.db.set_setting(_dashboard_key(request), json.dumps(widgets))
        return {"widgets": widgets}

    @app.delete("/api/dashboard/layout", dependencies=[need("stato")])
    async def reset_dashboard_layout(request: Request) -> dict:
        request.app.state.db.set_setting(_dashboard_key(request), "")
        return {"widgets": None}

    @app.post("/api/sync", dependencies=[need("persone_modifica")])
    async def sync(request: Request) -> dict:
        result = await request.app.state.service.sync_from_controller()
        request.app.state.last_sync = {"ok": True, "error": "", "result": result, "at": _now()}
        request.app.state.db.add_audit(request_user(request), "sincronizzazione", _sync_text(result))
        return result

    @app.get("/api/missing-cards", dependencies=[need("persone")])
    async def missing_cards(request: Request) -> list[dict]:
        """Tessere che l'app conosce ma che sul controller non ci sono più."""
        return request.app.state.db.missing_cards()

    @app.post("/api/missing-cards/restore", dependencies=[need("persone_modifica")])
    async def restore_missing_cards(body: MissingCardsIn, request: Request) -> dict:
        return await request.app.state.service.restore_missing(request_user(request), body.ids)

    @app.post("/api/missing-cards/forget", dependencies=[need("persone_modifica")])
    async def forget_missing_cards(body: MissingCardsIn, request: Request) -> dict:
        return request.app.state.service.forget_missing(request_user(request), body.ids)

    # Persone e tessere

    @app.get("/api/persons", dependencies=[need("persone")])
    async def persons(request: Request) -> list[dict]:
        db = request.app.state.db
        last = db.last_accesses()
        result = db.list_persons()
        for person in result:
            person["last_access"] = last.get(person["id"], "")
        return result

    @app.post("/api/persons", dependencies=[need("persone_modifica")])
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
            body.category_id,
        )

    @app.get("/api/persons/{person_id}", dependencies=[need("persone")])
    async def person_detail(person_id: int, request: Request) -> dict:
        db = request.app.state.db
        person = db.get_person(person_id)
        if person is None:
            raise ServiceError("persona non trovata")
        can_log = current_user(request).can("registro")
        person["events"] = db.list_events(person_id=person_id, limit=50) if can_log else []
        person["last_access"] = db.last_accesses().get(person_id, "")
        return person

    @app.patch("/api/persons/{person_id}", dependencies=[need("persone_modifica")])
    async def update_person(person_id: int, body: PersonUpdateIn, request: Request) -> dict:
        return await request.app.state.service.update_person(
            person_id, request_user(request), name=body.name, company=body.company, notes=body.notes
        )

    @app.put("/api/persons/{person_id}/access", dependencies=[need("persone_modifica")])
    async def set_access(person_id: int, body: AccessIn, request: Request) -> dict:
        return await request.app.state.service.set_person_access(
            person_id, body.group, body.valid_from, body.valid_to, request_user(request), body.category_id
        )

    @app.delete("/api/persons/{person_id}", dependencies=[need("persone_modifica")])
    async def delete_person(person_id: int, request: Request) -> dict:
        await request.app.state.service.delete_person(person_id, request_user(request))
        return {"ok": True}

    @app.post("/api/persons/{person_id}/cards", dependencies=[need("persone_modifica")])
    async def add_card(person_id: int, body: CardIn, request: Request) -> dict:
        return await request.app.state.service.add_card(person_id, body.card, request_user(request))

    @app.patch("/api/cards/{card_id}", dependencies=[need("persone_modifica")])
    async def set_card_state(card_id: int, body: CardStateIn, request: Request) -> dict:
        return await request.app.state.service.set_card_enabled(card_id, body.enabled, request_user(request))

    @app.delete("/api/cards/{card_id}", dependencies=[need("persone_modifica")])
    async def delete_card(card_id: int, request: Request) -> dict:
        person = await request.app.state.service.delete_card(card_id, request_user(request))
        return {"ok": True, "person": person}

    @app.get("/api/unknown-cards", dependencies=[need("persone")])
    async def unknown_cards(request: Request) -> list[dict]:
        return request.app.state.db.list_unknown_cards()

    @app.delete("/api/unknown-cards/{number}", dependencies=[need("persone_modifica")])
    async def ignore_unknown_card(number: str, request: Request) -> dict:
        request.app.state.db.forget_unknown_card(number)
        return {"ok": True}

    # Registro

    @app.get("/api/events", dependencies=[need("registro")])
    async def events(
        request: Request,
        kind: str = "",
        q: str = "",
        before_id: int | None = None,
        before_ts: str = "",
        limit: int = 100,
        since: str = "",
        until: str = "",
        person_id: int | None = None,
        door: str = "",
    ) -> list[dict]:
        return request.app.state.db.list_events(
            kind=kind,
            search=q,
            before_id=before_id,
            before_ts=_utc(before_ts),
            limit=min(limit, 500),
            since=_utc(since),
            until=_utc(until),
            person_id=person_id,
            door=door,
        )

    @app.get("/api/events/count", dependencies=[need("registro")])
    async def count_events(
        request: Request, kind: str = "", q: str = "", since: str = "", until: str = "", door: str = ""
    ) -> dict:
        """Quanti eventi corrispondono ai filtri: serve per gli accessi nascosti dalle viste del registro."""
        count = request.app.state.db.count_matching_events(
            kind=kind, search=q, since=_utc(since), until=_utc(until), door=door
        )
        return {"count": count}

    @app.get("/api/events/{event_id}", dependencies=[need("registro")])
    async def event(event_id: int, request: Request) -> dict:
        event = request.app.state.db.event_row(event_id)
        if not event:
            raise HTTPException(404, "evento non trovato")
        return event

    @app.get("/api/events/{event_id}/snapshot", dependencies=[need("registro")])
    async def snapshot(event_id: int, request: Request) -> Response:
        event = request.app.state.db.get_event(event_id)
        if not event or not event.get("snapshot"):
            return JSONResponse({"detail": "nessuna istantanea"}, status_code=404)
        path = request.app.state.ha.snapshot_dir / Path(event["snapshot"]).name
        if not path.exists():
            return JSONResponse({"detail": "istantanea cancellata"}, status_code=404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/export/events.csv", dependencies=[need("registro", "import_export")])
    async def export_events(
        request: Request,
        kind: str = "",
        q: str = "",
        since: str = "",
        until: str = "",
        person_id: int | None = None,
        door: str = "",
    ) -> Response:
        state = request.app.state
        rows = state.db.list_events(
            kind=kind,
            search=q,
            since=_utc(since),
            until=_utc(until),
            person_id=person_id,
            door=door,
            limit=EXPORT_LIMIT,
        )
        names = {d["door_token"]: d["name"] for d in state.doors}
        return _csv_response(events_csv(rows, names), "registro_accessi")

    @app.get("/api/export/persons.csv", dependencies=[need("persone", "import_export")])
    async def export_persons(request: Request) -> Response:
        state = request.app.state
        names = await _group_names(state)
        categories = {c["id"]: c["name"] for c in state.db.list_categories()}
        text = persons_csv(state.db.list_persons(), names, state.db.last_accesses(), categories)
        return _csv_response(text, "anagrafica")

    @app.post("/api/import/persons", dependencies=[need("persone_modifica", "import_export")])
    async def import_csv(body: ImportIn, request: Request) -> dict:
        state = request.app.state
        try:
            groups = (await state.rules.list_groups())["groups"]
        except VapixError as err:
            _LOGGER.warning("Gruppi non leggibili per l'importazione: %s", err)
            groups = []
        if body.apply:
            # Un'importazione cambia molte persone insieme: il backup si fa sempre, non una volta ogni tanto
            await state.backups.before_change(
                "prima_importazione",
                request_user(request),
                _backup_clients(state),
                _offline_controllers(state),
                window=False,
            )
        return await import_persons(state.service, body.csv, groups, request_user(request), body.apply)

    @app.get("/api/audit", dependencies=[need("utenti")])
    async def audit(request: Request, before_id: int | None = None, limit: int = 100) -> list[dict]:
        return request.app.state.db.list_audit(limit=min(limit, 500), before_id=before_id)

    # Regole

    @app.get("/api/groups", dependencies=[need("persone", "regole", any_of=True)])
    async def groups(request: Request) -> dict:
        return await request.app.state.rules.list_groups()

    @app.post("/api/groups", dependencies=[need("regole_modifica"), before_change()])
    async def create_group(body: GroupIn, request: Request) -> dict:
        return await request.app.state.rules.create_group(
            body.name, body.description, body.schedule, request_user(request)
        )

    @app.put("/api/groups/{token:path}", dependencies=[need("regole_modifica"), before_change()])
    async def update_group(token: str, body: GroupIn, request: Request) -> dict:
        return await request.app.state.rules.update_group(
            token, body.name, body.description, body.schedule, request_user(request)
        )

    @app.delete("/api/groups/{token:path}", dependencies=[need("regole_modifica"), before_change()])
    async def delete_group(token: str, request: Request) -> dict:
        await request.app.state.rules.delete_group(token, request_user(request))
        return {"ok": True}

    @app.get("/api/schedules", dependencies=[need("persone", "regole", any_of=True)])
    async def schedules(request: Request) -> list[dict]:
        return await request.app.state.rules.list_schedules()

    @app.post("/api/schedules", dependencies=[need("regole_modifica"), before_change()])
    async def create_schedule(body: ScheduleIn, request: Request) -> dict:
        return await request.app.state.rules.create_schedule(
            body.name, body.description, [r.model_dump() for r in body.rules], body.holidays, request_user(request)
        )

    @app.put("/api/schedules/{token:path}", dependencies=[need("regole_modifica"), before_change()])
    async def update_schedule(token: str, body: ScheduleIn, request: Request) -> dict:
        return await request.app.state.rules.update_schedule(
            token,
            body.name,
            body.description,
            [r.model_dump() for r in body.rules],
            body.holidays,
            request_user(request),
        )

    @app.delete("/api/schedules/{token:path}", dependencies=[need("regole_modifica"), before_change()])
    async def delete_schedule(token: str, request: Request) -> dict:
        await request.app.state.rules.delete_schedule(token, request_user(request))
        return {"ok": True}

    @app.get("/api/holidays", dependencies=[need("persone", "regole", any_of=True)])
    async def holidays(request: Request) -> list[dict]:
        return request.app.state.db.list_holidays()

    @app.post("/api/holidays", dependencies=[need("regole_modifica"), before_change()])
    async def add_holiday(body: HolidayIn, request: Request) -> dict:
        await request.app.state.rules.add_holidays([body.model_dump()], request_user(request))
        return {"ok": True}

    @app.post("/api/holidays/italia", dependencies=[need("regole_modifica"), before_change()])
    async def add_italian_holidays(body: ItalianHolidaysIn, request: Request) -> dict:
        await request.app.state.rules.add_italian_holidays(body.year, request_user(request))
        return {"ok": True}

    @app.delete("/api/holidays/{day}", dependencies=[need("regole_modifica"), before_change()])
    async def delete_holiday(day: str, request: Request) -> dict:
        await request.app.state.rules.delete_holiday(day, request_user(request))
        return {"ok": True}

    @app.put("/api/hidden", dependencies=[need("regole_modifica")])
    async def set_hidden(body: HiddenIn, request: Request) -> dict:
        request.app.state.rules.set_hidden(body.kind, body.token, body.hidden, request_user(request))
        return {"ok": True}

    @app.put("/api/default-group", dependencies=[need("regole_modifica")])
    async def set_default_group(body: DefaultGroupIn, request: Request) -> dict:
        await request.app.state.rules.set_default_group(body.token, request_user(request))
        return {"ok": True}

    @app.get("/api/categories", dependencies=[need("persone", "regole", any_of=True)])
    async def categories(request: Request) -> list[dict]:
        return request.app.state.rules.list_categories()

    @app.post("/api/categories", dependencies=[need("regole_modifica"), before_change()])
    async def create_category(body: CategoryIn, request: Request) -> dict:
        return await request.app.state.rules.save_category(None, body.model_dump(), request_user(request))

    @app.put("/api/categories/{category_id}", dependencies=[need("regole_modifica"), before_change()])
    async def update_category(category_id: int, body: CategoryIn, request: Request) -> dict:
        return await request.app.state.rules.save_category(category_id, body.model_dump(), request_user(request))

    @app.delete("/api/categories/{category_id}", dependencies=[need("regole_modifica"), before_change()])
    async def delete_category(category_id: int, request: Request) -> dict:
        await request.app.state.rules.delete_category(category_id, request_user(request))
        return {"ok": True}

    @app.put("/api/default-category", dependencies=[need("regole_modifica")])
    async def set_default_category(body: DefaultCategoryIn, request: Request) -> dict:
        request.app.state.rules.set_default_category(body.category_id, request_user(request))
        return {"ok": True}

    @app.post("/api/persons/{person_id}/align", dependencies=[need("persone_modifica")])
    async def align_person(person_id: int, request: Request) -> dict:
        return await request.app.state.rules.align_person(person_id, request_user(request))

    # Porte: dove sono, come stanno, cosa è successo

    @app.get("/api/locations", dependencies=[need("stato", "persone", "regole", "impianti", any_of=True)])
    async def locations(request: Request) -> list[dict]:
        return request.app.state.installation.locations()

    @app.get("/api/door-overview", dependencies=[need("stato")])
    async def door_overview(request: Request) -> list[dict]:
        """Porte con struttura, stato e numeri di oggi; l'ultimo passaggio solo a chi vede il registro."""
        state = request.app.state
        user = current_user(request)
        counts = state.db.door_event_counts(local_day_start())
        last = state.db.last_door_events(("concesso", "negato", "sconosciuta", "apertura_remota", "citofono"))
        result = []
        for door in _doors_status(state):
            today = counts.get(door["token"], {})
            if len(state.doors) == 1:
                # Eventi registrati prima della 0.5 senza porta: con una porta sola sono suoi
                for kind, n in counts.get("", {}).items():
                    today[kind] = today.get(kind, 0) + n
            event = last.get(door["token"])
            if len(state.doors) == 1 and last.get("") and (not event or last[""]["ts"] > event["ts"]):
                event = last[""]
            result.append(
                {
                    **door,
                    "today": {
                        "granted": sum(today.get(k, 0) for k in ACCESS_COUNT_KINDS),
                        "denied": today.get("negato", 0),
                        "alarms": today.get("allarme", 0) + today.get("manomissione", 0),
                    },
                    "last": (
                        {"ts": event["ts"], "kind": event["kind"], "person": event["person_name"] or event["card"]}
                        if event and user.can("registro")
                        else None
                    ),
                }
            )
        return result

    @app.get("/api/door-detail/{door_id}", dependencies=[need("stato")])
    async def door_detail(door_id: int, request: Request) -> dict:
        """Tutto su una porta: dispositivi, chi può entrare e ultimi eventi, secondo i permessi."""
        state = request.app.state
        user = current_user(request)
        door = next((d for d in await door_overview(request) if d["id"] == door_id), None)
        config = state.db.get_door(door_id)
        if door is None or config is None:
            raise HTTPException(status_code=404, detail="porta non trovata")
        controller = state.db.get_controller(config["controller_id"]) or {}
        readers, points, errors = [], [], []
        # Pulsanti e relè collegati agli ingressi del controller: anche loro sono punti di identificazione
        inputs = {
            row["idpoint_token"]: row
            for row in state.db.controller_inputs(config["controller_id"]).values()
            if row["idpoint_token"]
        }
        try:
            all_points = await state.controller.list_access_points()
            idpoints = {r.get("token"): r for r in await state.controller.list_idpoints()}
            for point in all_points:
                if point.get("Entity") != config["door_token"]:
                    continue
                points.append(point.get("token"))
                direction = next(
                    (a.get("Value") for a in point.get("Attribute") or [] if a.get("Name") == "Direction"), ""
                )
                for device in point.get("IdPointDevice") or []:
                    reader = idpoints.get(device.get("IdPoint"), {})
                    wired = inputs.get(device.get("IdPoint"))
                    readers.append(
                        {
                            "name": (wired or {}).get("name") or reader.get("Name") or device.get("IdPoint") or "",
                            "description": reader.get("Description") or "",
                            "direction": {"in": "Entrata", "out": "Uscita"}.get(direction, ""),
                            # Ingresso del controller: ruolo e morsetto al posto del lato della porta
                            "role": INPUT_ROLES.get(wired["role"], "") if wired else "",
                            "role_key": wired["role"] if wired else "",
                            "terminal": INPUT_TERMINALS.get(wired["io"], wired["io"]) if wired else "",
                            "access_point": point.get("Name") or point.get("token") or "",
                            "enabled": point.get("Enabled", True) is not False,
                        }
                    )
        except VapixError as err:
            errors.append(f"lettori: {err}")
        groups = None
        if user.can("regole") or user.can("persone"):
            try:
                listed = (await state.rules.list_groups())["groups"]
                opening = {
                    g["token"]: g
                    for g in listed
                    if not g["access_points"] or set(g["access_points"]) & set(points) or not points
                }
                # Chi entra da questa porta: le tipologie il cui profilo la comprende
                groups = [
                    {
                        "name": c["name"],
                        "color": c["color"],
                        "schedule_names": opening[c["group_token"]]["schedule_names"],
                        "persons": c["persons"],
                        "default": c["default"],
                        "enabled": opening[c["group_token"]]["enabled"],
                    }
                    for c in state.rules.list_categories()
                    if c["group_token"] in opening
                ]
            except VapixError as err:
                errors.append(f"tipologie: {err}")
        events = None
        if user.can("registro"):
            # Con una porta sola valgono anche gli eventi registrati prima che avessero la porta
            door_filter = config["door_token"] + ("," if len(state.doors) == 1 else "")
            events = state.db.list_events(door=door_filter, limit=15)
        return {
            **door,
            "controller": {
                "id": controller.get("id"),
                "name": controller.get("name", ""),
                "host": controller.get("host", ""),
            },
            "intercom_relay": config["intercom_relay"],
            "intercom_open_door": config["intercom_open_door"],
            "readers": readers,
            "groups": groups,
            "events": events,
            "errors": errors,
        }

    @app.post("/api/sites", dependencies=[need("impianti")])
    async def create_site(body: SiteIn, request: Request) -> dict:
        return request.app.state.installation.save_site(None, body.model_dump(), request_user(request))

    @app.put("/api/sites/{site_id}", dependencies=[need("impianti")])
    async def update_site(site_id: int, body: SiteIn, request: Request) -> dict:
        return request.app.state.installation.save_site(site_id, body.model_dump(), request_user(request))

    @app.delete("/api/sites/{site_id}", dependencies=[need("impianti")])
    async def delete_site(site_id: int, request: Request) -> dict:
        request.app.state.rules.check_place_unused("site", site_id)
        request.app.state.installation.delete_site(site_id, request_user(request))
        return {"ok": True}

    @app.post("/api/sites/{site_id}/areas", dependencies=[need("impianti")])
    async def create_area(site_id: int, body: AreaIn, request: Request) -> dict:
        return request.app.state.installation.save_area(None, site_id, body.model_dump(), request_user(request))

    @app.put("/api/areas/{area_id}", dependencies=[need("impianti")])
    async def update_area(area_id: int, body: AreaIn, request: Request) -> dict:
        return request.app.state.installation.save_area(area_id, 0, body.model_dump(), request_user(request))

    @app.delete("/api/areas/{area_id}", dependencies=[need("impianti")])
    async def delete_area(area_id: int, request: Request) -> dict:
        request.app.state.rules.check_place_unused("area", area_id)
        request.app.state.installation.delete_area(area_id, request_user(request))
        await _refresh_categories(request.app)
        return {"ok": True}

    # Impianti

    @app.get("/api/installation", dependencies=[need("impianti")])
    async def installation(request: Request) -> dict:
        service = request.app.state.installation
        places = service.controller_places()
        health = {h["id"]: h for h in _controllers_health(request.app.state)}
        controllers = [
            {
                **c,
                "place": places.get(c["id"], {"site": "", "area": "", "site_id": 0, "area_id": 0}),
                "health": health.get(c["id"]),
            }
            for c in service.controllers()
        ]
        return {"controllers": controllers, "max_controllers": MAX_CONTROLLERS}

    @app.post("/api/controllers/test", dependencies=[need("impianti")])
    async def test_controller(body: ControllerTestIn, request: Request) -> dict:
        """Prova indirizzo e credenziali senza salvarli; elenca le porte trovate."""
        password = body.password
        if not password and body.controller_id:
            existing = request.app.state.db.get_controller(body.controller_id)
            password = existing["password"] if existing else ""
        host = normalize_host(body.host)
        if not host:
            raise ServiceError("manca l'indirizzo del controller")
        client = request.app.state.injected_controller or A1210Client(host, body.username, password, body.verify_ssl)
        try:
            info = await client.device_info()
            doors = await client.door_info_list()
        finally:
            if request.app.state.injected_controller is None:
                await client.aclose()
        return {
            "model": info.get("ProdNbr") or info.get("ProdShortName", ""),
            "firmware": info.get("Version", ""),
            "serial": info.get("SerialNumber", ""),
            "doors": [{"token": d.get("token", ""), "name": d.get("Name") or d.get("token", "")} for d in doors],
        }

    @app.post("/api/controllers", dependencies=[need("impianti")])
    async def create_controller(body: ControllerIn, request: Request) -> dict:
        result = request.app.state.installation.save_controller(None, body.model_dump(), request_user(request))
        await reload_runtime(request.app)
        return result

    @app.put("/api/controllers/{controller_id}", dependencies=[need("impianti")])
    async def update_controller(controller_id: int, body: ControllerIn, request: Request) -> dict:
        result = request.app.state.installation.save_controller(controller_id, body.model_dump(), request_user(request))
        await reload_runtime(request.app)
        return result

    @app.put("/api/controllers/{controller_id}/camera", dependencies=[need("impianti")])
    async def set_controller_camera(controller_id: int, body: ControllerCameraIn, request: Request) -> dict:
        """Telecamera del coperchio: si cambia senza riaprire la connessione al controller."""
        state = request.app.state
        result = state.installation.set_controller_camera(controller_id, body.camera, request_user(request))
        primary = state.installation.primary
        state.controller_camera = (primary or {}).get("snapshot_camera") or ""
        return result

    @app.post("/api/controllers/{controller_id}/identity/accept")
    async def accept_controller_identity(controller_id: int, request: Request, user: Admin) -> dict:
        """Un amministratore conferma il controller trovato all'indirizzo: le scritture ripartono."""
        state = request.app.state
        try:
            state.health.accept_identity(controller_id, user.label)
        except LookupError as err:
            raise HTTPException(404, str(err)) from err
        _apply_pause(state)
        with contextlib.suppress(VapixError, ServiceError):
            if state.settings.a1210_host:
                result = await state.service.sync_from_controller()
                state.last_sync = {"ok": True, "error": "", "result": result, "at": _now()}
        return {"ok": True}

    @app.delete("/api/controllers/{controller_id}", dependencies=[need("impianti"), before_change()])
    async def delete_controller(controller_id: int, request: Request) -> dict:
        request.app.state.installation.delete_controller(controller_id, request_user(request))
        await reload_runtime(request.app)
        return {"ok": True}

    @app.get("/api/controllers/{controller_id}/doors-available", dependencies=[need("impianti")])
    async def available_doors(controller_id: int, request: Request) -> list[dict]:
        """Porte presenti sul controller in uso, da associare alle porte dell'app."""
        state = request.app.state
        primary = state.installation.primary
        if not primary or primary["id"] != controller_id:
            raise ServiceError("controller non in uso")
        doors = await state.controller.door_info_list()
        return [{"token": d.get("token", ""), "name": d.get("Name") or d.get("token", "")} for d in doors]

    @app.post("/api/controllers/{controller_id}/doors", dependencies=[need("impianti")])
    async def create_door(controller_id: int, body: DoorConfigIn, request: Request) -> dict:
        result = request.app.state.installation.save_door(None, controller_id, body.model_dump(), request_user(request))
        await reload_runtime(request.app)
        return result

    @app.put("/api/doors/{door_id}", dependencies=[need("impianti")])
    async def update_door(door_id: int, body: DoorConfigIn, request: Request) -> dict:
        door = request.app.state.db.get_door(door_id)
        if door is None:
            raise ServiceError("porta non trovata")
        result = request.app.state.installation.save_door(
            door_id, door["controller_id"], body.model_dump(), request_user(request)
        )
        await reload_runtime(request.app)
        return result

    @app.delete("/api/doors/{door_id}", dependencies=[need("impianti"), before_change()])
    async def delete_door(door_id: int, request: Request) -> dict:
        request.app.state.rules.check_place_unused("door", door_id)
        request.app.state.installation.delete_door(door_id, request_user(request))
        await reload_runtime(request.app)
        return {"ok": True}

    # Programmazione del controller

    @app.get("/api/programming", dependencies=[need("impianti")])
    async def programming(request: Request) -> dict:
        data = await request.app.state.programming.read()
        # Stato in tempo reale degli ingressi che mandano gli eventi Device/IO/Port
        ports = request.app.state.monitor.io_ports
        for item in data.get("inputs", []):
            item["active"] = ports.get(item["port"]) if item["port"] is not None else None
        return data

    @app.put("/api/programming/doors/{token:path}", dependencies=[need("impianti"), before_change()])
    async def program_door(token: str, body: HardwareDoorIn, request: Request) -> dict:
        await request.app.state.programming.update_door(token, body.model_dump(), request_user(request))
        return {"ok": True}

    @app.put("/api/programming/readers/{token:path}", dependencies=[need("impianti"), before_change()])
    async def program_reader(token: str, body: HardwareReaderIn, request: Request) -> dict:
        await request.app.state.programming.update_reader(token, body.model_dump(), request_user(request))
        return {"ok": True}

    @app.put("/api/programming/inputs/{io}", dependencies=[need("impianti"), before_change()])
    async def program_input(io: str, body: ControllerInputIn, request: Request) -> dict:
        saved = await request.app.state.programming.update_input(io, body.model_dump(), request_user(request))
        # I nomi degli ingressi sono anche quelli delle entità di Home Assistant
        await request.app.state.bridge.publish_input_names()
        return saved

    @app.put("/api/programming/access-points/{token:path}", dependencies=[need("impianti"), before_change()])
    async def program_access_point(token: str, body: HardwareAccessPointIn, request: Request) -> dict:
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        await request.app.state.programming.update_access_point(token, data, request_user(request))
        return {"ok": True}

    @app.post("/api/programming/manage", dependencies=[need("impianti"), before_change()])
    async def program_manage(request: Request) -> dict:
        state = request.app.state
        managed = await state.programming.manage_access_points(request_user(request))
        await state.health.recheck_managed(state.installation.primary)
        return {"managed": managed}

    @app.put("/api/programming/links", dependencies=[need("impianti"), before_change()])
    async def program_links(body: LinksIn, request: Request) -> dict:
        links = [link.model_dump() for link in body.links]
        changed = await request.app.state.programming.set_links(links, request_user(request))
        return {"changed": changed}

    @app.post("/api/programming/changes/{change_id}/undo", dependencies=[need("impianti"), before_change()])
    async def undo_change(change_id: int, request: Request) -> dict:
        if await request.app.state.programming.undo(change_id, request_user(request)) == "input":
            await request.app.state.bridge.publish_input_names()
        return {"ok": True}

    @app.get("/api/cameras", dependencies=[need("impianti")])
    async def cameras(request: Request) -> list[dict]:
        """Telecamere di Home Assistant con l'ultima istantanea non riuscita, se c'è."""
        state = request.app.state
        cameras = await state.ha.states()
        if cameras:
            state.camera_names = {c["entity_id"]: c["name"] for c in cameras}
            state.camera_names_at = time.monotonic()
        return [{**c, "last_error": state.snapshot_errors.get(c["entity_id"])} for c in cameras]

    # Utenti dell'app

    # Backup: Dati Comuni e configurazione di ogni controller

    @app.get("/api/backups", dependencies=[need("backup")])
    async def list_backups(request: Request) -> dict:
        state = request.app.state
        listing = await asyncio.to_thread(state.backups.listing)
        return {**listing, "can_restore": current_user(request).can("utenti"), "sections": RESTORE_SECTIONS}

    @app.post("/api/backups", dependencies=[need("backup")])
    async def create_backup(body: BackupIn, request: Request) -> dict:
        state = request.app.state
        return await state.backups.create(
            "manuale", body.name, request_user(request), _backup_clients(state), offline=_offline_controllers(state)
        )

    @app.put("/api/backups/policy")
    async def backup_policy(body: BackupPolicyIn, request: Request, user: Admin) -> dict:
        """Backup automatico e ritenzione. Regole che eliminano backup: prima la risposta dice quanti."""
        values = body.model_dump(exclude_none=True, exclude={"confirm"})
        return await request.app.state.backups.set_policy(values, user.label, body.confirm)

    @app.post("/api/backups/verify", dependencies=[need("backup")])
    async def verify_backups(request: Request) -> dict:
        return await request.app.state.backups.verify_now()

    @app.get("/api/backups/{backup_id}/compare", dependencies=[need("backup")])
    async def compare_backup(request: Request, backup_id: str, other: str = "precedente", part: str = "") -> dict:
        """Differenze con il backup precedente, con lo stato attuale ("attuale") o con un altro backup."""
        state = request.app.state
        return await state.backups.compare(backup_id, other, _backup_clients(state), part or None)

    @app.put("/api/backups/{backup_id}/lock", dependencies=[need("backup")])
    async def lock_backup(backup_id: str, body: BackupLockIn, request: Request) -> dict:
        return await request.app.state.backups.set_locked(backup_id, body.locked, request_user(request))

    @app.delete("/api/backups/{backup_id}")
    async def delete_backup(backup_id: str, request: Request, user: Admin) -> dict:
        await request.app.state.backups.delete(backup_id, user.label)
        return {"ok": True}

    @app.post("/api/backups/{backup_id}/preview")
    async def preview_restore(backup_id: str, body: RestorePreviewIn, request: Request, user: Admin) -> dict:
        """Cosa cambierebbe ripristinando le sezioni scelte: una riga da spuntare per ogni oggetto diverso."""
        state = request.app.state
        _, saved = await asyncio.to_thread(state.backups.load, backup_id)
        # Un controller che non risponde non ha la sua parte nello stato attuale: porte e lettori restano bloccati
        current, _ = await state.backups.snapshot(_backup_clients(state))
        items = _restore_plan(state, saved, current, body)
        return {"items": public(items), "sections": ALL_SECTIONS, "paused": state.controller.paused}

    @app.post("/api/backups/{backup_id}/restore")
    async def restore_backup(backup_id: str, body: RestoreIn, request: Request, user: Admin) -> dict:
        """Ripristina le righe scelte. Prima si fa un backup dello stato attuale: anche il ripristino si annulla."""
        state = request.app.state
        if state.controller.paused:
            raise ServiceError(f"scritture sospese: {state.controller.paused}")
        manifest, saved = await asyncio.to_thread(state.backups.load, backup_id)
        if not body.keys:
            raise ServiceError("scegli almeno una riga da ripristinare")
        label = manifest.get("name") or _local_text(manifest["created_at"])
        # Con la data e non con il nome: "Prima del ripristino di Prima del ripristino di …" non si legge
        name = f"Prima del ripristino del backup del {_local_text(manifest['created_at'])}"
        before = await state.backups.create(
            "prima_ripristino", name, user.label, _backup_clients(state), offline=_offline_controllers(state)
        )
        _, current = await asyncio.to_thread(state.backups.load, before["id"])
        stale = {k for k, info in before["parts"].items() if info.get("stale")}
        current = {k: v for k, v in current.items() if k not in stale}
        items = _restore_plan(state, saved, current, body)
        runtime = Runtime(
            state.db,
            state.service,
            state.rules,
            state.programming,
            (state.installation.primary or {}).get("id"),
            mqtt_password=body.mqtt_password,
            mqtt_known=_mqtt_known(state),
        )
        result = await apply(items, body.keys, runtime, user.label)
        full = any(s in FULL_SECTIONS for s in body.sections)
        state.db.add_audit(
            user.label,
            "ripristino completo del controller da backup" if full else "ripristino da backup",
            f"{label}: {plural(len(result['applied']), 'modifica', 'modifiche')}"
            + (f", {plural(len(result['failed']), 'non riuscita', 'non riuscite')}" if result["failed"] else ""),
        )
        if result.pop("reload"):
            await reload_runtime(request.app)
        if full:
            await state.health.recheck_managed(state.installation.primary)
        return {**result, "backup_before": before["id"]}

    @app.get("/api/users", dependencies=[need("utenti")])
    async def users(request: Request) -> dict:
        return {
            "users": request.app.state.users.list(),
            "permissions": PERMISSIONS,
            "roles": {key: {"label": label, "perms": perms} for key, (label, perms) in ROLES.items()},
        }

    @app.post("/api/users")
    async def create_user(body: AppUserIn, request: Request, user: Admin) -> dict:
        return request.app.state.users.save(body.username, body.name, body.perms, user, create=True)

    @app.put("/api/users/{username}")
    async def update_user(username: str, body: AppUserIn, request: Request, user: Admin) -> dict:
        return request.app.state.users.save(username, body.name, body.perms, user, create=False)

    @app.delete("/api/users/{username}")
    async def delete_user(username: str, request: Request, user: Admin) -> dict:
        request.app.state.users.delete(username, user)
        return {"ok": True}

    return app


async def _start_runtime(app: FastAPI) -> None:
    """Crea client del controller, ascolto MQTT e servizi con la configurazione degli impianti."""
    state = app.state
    cfg = state.installation.effective_settings(state.options)
    state.settings = cfg
    state.raw_controller = state.injected_controller or A1210Client(
        cfg.a1210_host, cfg.a1210_username, cfg.a1210_password, cfg.a1210_verify_ssl
    )
    # Le scritture si sospendono se all'indirizzo risponde un controller diverso da quello memorizzato
    state.controller = ControllerGuard(state.raw_controller)
    _apply_pause(state)
    state.doors = state.installation.doors()
    state.controller_camera = (state.installation.primary or {}).get("snapshot_camera") or ""
    state.monitor = EventMonitor(prefix=cfg.mqtt_topic_prefix, door_token=cfg.door_token)
    # Eventi persi da recuperare dal registro del controller: all'avvio e dopo ogni interruzione
    state.recover_pending = True
    state.recover_outages = 0
    state.service = AccessService(state.db, state.controller, cfg, state.monitor)
    state.service.clock_offset = lambda: _controller_clock_offset(state)
    if translated := state.db.translate_details("negato", DENIED_REASONS):
        _LOGGER.info("Motivi di %s dinieghi tradotti in italiano", translated)
    state.db.rename_controller_events("alloggiamento controller aperto", CASING_OPEN, state.service.controller_name())
    state.rules = RulesService(state.service)
    primary = state.installation.primary
    state.programming = ProgrammingService(
        state.db, state.controller, primary["id"] if primary else None, state.service.lock
    )
    state.bridge = HaBridge(cfg, state.monitor, state.db, state.doors)
    _wire(app)
    state.mqtt_configured = bool(state.mqtt)
    state.monitor_task = (
        asyncio.create_task(state.monitor.run(state.mqtt)) if state.mqtt and cfg.mqtt_topic_prefix else None
    )


async def _stop_runtime(app: FastAPI) -> None:
    state = app.state
    task = getattr(state, "monitor_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if state.injected_controller is None:
        await state.raw_controller.aclose()


async def reload_runtime(app: FastAPI) -> None:
    """Riparte con la nuova configurazione dopo una modifica a controller o porte."""
    async with app.state.reload_lock:
        await _stop_runtime(app)
        await _start_runtime(app)
    # Indirizzo cambiato: prima di leggere o scrivere si controlla chi risponde
    try:
        await app.state.health.check_all()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Controllo dei controller non riuscito")
    _apply_pause(app.state)
    with contextlib.suppress(VapixError, ServiceError):
        if app.state.settings.a1210_host:
            result = await app.state.service.sync_from_controller()
            app.state.last_sync = {"ok": True, "error": "", "result": result, "at": _now()}
    await _refresh_categories(app)


async def _refresh_categories(app: FastAPI) -> None:
    """Porte spostate tra aree e strutture: le tipologie che le comprendono aggiornano il loro profilo."""
    if not app.state.settings.a1210_host:
        return
    try:
        if changed := await app.state.rules.refresh_category_profiles():
            _LOGGER.info("Profili di %s tipologie aggiornati dopo una modifica alle porte", changed)
    except VapixError as err:
        _LOGGER.warning("Profili delle tipologie non aggiornati: %s", err)


def _wire(app: FastAPI) -> None:
    """Collega eventi, istantanee e Home Assistant."""
    state = app.state
    cfg: Settings = state.settings
    monitor: EventMonitor = state.monitor
    service: AccessService = state.service
    bridge: HaBridge = state.bridge
    monitor.on_event = service.record_event
    monitor.on_status = service.notify_change

    doors = {d["door_token"]: d for d in state.doors}

    async def after_event(row: dict) -> None:
        if row.get("recovered"):
            # Recuperato dal registro del controller: la foto di adesso non c'entra, e le automazioni
            # di Home Assistant non devono scattare in ritardo
            return
        door = doors.get(row.get("door") or cfg.door_token) or {}
        camera = door.get("snapshot_camera") or ""
        if row.get("kind") == "manomissione" and not row.get("door"):
            # Coperchio del controller: la telecamera del controller, mai quella di una porta
            camera = state.controller_camera
        kinds = SNAPSHOT_KINDS | (GRANTED_KINDS if door.get("snapshot_on_granted") else set())
        if camera and row.get("kind") in kinds:
            filename, error = await state.ha.save_snapshot(camera, f"{row['id']}_{_file_stamp(row.get('ts') or '')}")
            if filename or error:
                state.db.set_event_snapshot(row["id"], filename, error)
            # Ultimo esito per telecamera, mostrato nella scheda Telecamere del controller
            state.snapshot_errors.pop(camera, None)
            if error:
                state.snapshot_errors[camera] = {"ts": row.get("ts") or _now(), "error": error}
        await bridge.publish_event(row)

    async def on_change() -> None:
        await bridge.publish_state()

    service.after_event = after_event
    service.on_change = on_change
    state.intercoms = []
    for door in state.doors:
        if not door["intercom_name"]:
            continue
        intercom = IntercomBridge(
            IntercomSettings(
                door["intercom_name"], door["intercom_open_door"], door["intercom_relay"], door["intercom_mqtt_prefix"]
            ),
            # Con l'ora del controller, come gli altri eventi della porta (vedi AccessService.event_now)
            log=lambda door_token=door["door_token"], **fields: service.log_event(
                **(fields | {"ts": service.event_now()}), door=door_token
            ),
            open_door=lambda source, note, door_token=door["door_token"]: service.open_door(
                False, source, door_token, note
            ),
        )
        intercom.door = door
        state.intercoms.append(intercom)
        for topic in intercom.topics:
            monitor.extra_subscriptions[topic] = intercom.handle
    if cfg.ha_integration:
        monitor.will = bridge.will()
        monitor.on_connect = bridge.on_connect
        if bridge.has_buttons:
            monitor.command_topic = bridge.command_topic
            allowed = {d["door_token"] for d in state.doors if d["ha_buttons"]}

            async def on_command(command: str) -> None:
                parsed = parse_command(command)
                door = (parsed[1] or cfg.door_token) if parsed else ""
                if parsed is None or door not in allowed:
                    _LOGGER.warning("Comando sconosciuto da Home Assistant: %s", command)
                    return
                # Chi ha premuto il pulsante, se Home Assistant lo sa: "Luca (Home Assistant)"
                # Al massimo 2 secondi, per non ritardare l'apertura se Home Assistant è lento
                try:
                    who = await asyncio.wait_for(app.state.ha.button_presser(), 2)
                except TimeoutError:
                    who = None
                source = (
                    f"{who} (Home Assistant)"
                    if who
                    else "Automazione (Home Assistant)"
                    if who == ""
                    else "Home Assistant"
                )
                try:
                    await service.open_door(parsed[0], source, door)
                except (ServiceError, VapixError) as err:
                    _LOGGER.warning("Apertura da Home Assistant non eseguita: %s", err)

            monitor.on_command = on_command
    else:
        # Rimuove le entità pubblicate in precedenza
        monitor.on_connect = bridge.on_connect


def _doors_status(state) -> list[dict]:
    places = state.installation.door_places()
    return [
        {
            "id": d["id"],
            "name": d["name"],
            "token": d["door_token"],
            "site": places.get(d["id"], {}).get("site", ""),
            "area": places.get(d["id"], {}).get("area", ""),
            "site_id": places.get(d["id"], {}).get("site_id", 0),
            "area_id": places.get(d["id"], {}).get("area_id", 0),
            "mode": state.monitor.door_modes.get(d["door_token"]),
            "camera": d["snapshot_camera"],
            "snapshot_on_granted": d["snapshot_on_granted"],
            "ha_buttons": d["ha_buttons"],
            "intercom": d["intercom_name"],
        }
        for d in state.doors
    ]


def _brief(item: dict) -> dict:
    return {k: item[k] for k in ("id", "type", "severity", "title", "text", "door", "ts", "count", "seen")}


async def _camera_names(state) -> dict[str, str]:
    """Nomi delle telecamere, dalla cache se recente: il riepilogo si legge spesso."""
    # Mai letti finora: None, non 0 (l'orologio monotono può partire da pochi secondi dopo l'avvio)
    stale = state.camera_names_at is None or time.monotonic() - state.camera_names_at > CAMERA_NAMES_TTL
    if state.ha.available and stale:
        state.camera_names_at = time.monotonic()
        cameras = await state.ha.states()
        if cameras:
            state.camera_names = {c["entity_id"]: c["name"] for c in cameras}
    return state.camera_names


def _dashboard_key(request: Request) -> str:
    """Un cruscotto per utente dell'app; l'accesso locale senza utente ha il suo."""
    return SETTING_DASHBOARD + (current_user(request).username or "-")


def _current_notifications(state) -> list[dict]:
    return state.notifications.collect(
        mqtt_configured=bool(state.mqtt),
        mqtt_connected=state.monitor.connected,
        controller_configured=bool(state.settings.a1210_host),
        controller_online=state.monitor.controller_online,
        sync=state.last_sync,
        door_names={d["door_token"]: d["name"] for d in state.doors},
        cameras=[
            {
                "camera": camera,
                "name": state.camera_names.get(camera) or camera,
                "error": problem["error"],
                "ts": problem["ts"],
                "doors": [d["name"] for d in state.doors if d["snapshot_camera"] == camera]
                + ([state.service.controller_name()] if camera == state.controller_camera else []),
            }
            for camera, problem in state.snapshot_errors.items()
            if camera == state.controller_camera or any(d["snapshot_camera"] == camera for d in state.doors)
        ],
        controllers=_controllers_health(state),
        missing=_missing_summary(state.db),
        backup=state.backups.last_error(),
        backup_verify=state.backups.last_verify(),
    )


def _notification_listing(request: Request) -> dict:
    user = current_user(request)
    state = request.app.state
    return state.notifications.listing(_current_notifications(state), None if user.can("utenti") else user.perms)


def _intercom_status(intercom: IntercomBridge) -> dict:
    return {
        "configured": True,
        "door": intercom.door["name"],
        "name": intercom.settings.name,
        "open_door": intercom.settings.open_door,
        "topics": intercom.topics,
        "call_state": intercom.call_state,
        "last_message_at": intercom.last_message_at,
    }


def _file_stamp(ts: str) -> str:
    """Ora dell'evento nel nome del file: quella del controller, non l'orologio di Home Assistant."""
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        moment = datetime.now()
    return moment.strftime("%Y%m%d_%H%M%S")


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
        if row["kind"] in ACCESS_COUNT_KINDS:
            bucket["granted"] += 1
        elif row["kind"] == "negato":
            bucket["denied"] += 1
        elif row["kind"] == "sconosciuta":
            bucket["unknown"] += 1
    return [{"day": day.isoformat(), **counts} for day, counts in days.items()]


def _sync_text(result: dict) -> str:
    text = f"{plural(result['cards'], 'tessera', 'tessere')}, {plural(result['added'], 'nuova', 'nuove')}"
    return text + (f", {result['missing']} non presenti sul controller" if result.get("missing") else "")


def _health_client(app: FastAPI, row: dict):
    """Client per il controllo periodico: uno per controller, separato da quello delle scritture."""
    if app.state.injected_controller is not None:
        return app.state.injected_controller
    return A1210Client(row["host"], row["username"], row["password"], bool(row["verify_ssl"]))


def _apply_pause(state) -> None:
    """Scritture verso il controller in uso sospese se è cambiato e nessuno ha confermato."""
    state.controller.paused = paused_reason(state.installation.primary)


async def _health_loop(app: FastAPI) -> None:
    """Ogni HEALTH_INTERVAL: tutti i controller in parallelo (raggiungibile, identità, orologio)."""
    state = app.state
    while True:
        try:
            before = state.controller.paused
            primary = state.installation.primary
            was = state.health.states.get(primary["id"]) if primary else None
            was_down = bool(was and was.failures)
            await state.health.check_all()
            now = state.health.states.get(primary["id"]) if primary else None
            if was_down and now is not None and now.online:
                _LOGGER.info("Controller di nuovo raggiungibile: rilettura delle tessere")
                state.sync_wake.set()
            _apply_pause(state)
            if before != state.controller.paused:
                state.service.notify_change()
            await _recover_if_needed(state)
        except Exception:  # noqa: BLE001 - il controllo riparte al giro successivo
            _LOGGER.exception("Controllo dei controller non riuscito")
        state.health_ready.set()
        await asyncio.sleep(HEALTH_INTERVAL)


async def _recover_if_needed(state) -> None:
    """Dopo l'avvio o un'interruzione (broker perso, controller offline), appena gli eventi tornano ad arrivare
    si recuperano quelli persi dal registro interno del controller."""
    monitor = state.monitor
    pending = state.recover_pending or monitor.outages != state.recover_outages
    primary = state.installation.primary
    health = state.health.states.get(primary["id"]) if primary else None
    if not pending or not health or not health.online or state.controller.paused:
        return
    if state.mqtt and (not monitor.connected or monitor.controller_online is False):
        return
    outages = monitor.outages
    last = state.db.last_controller_event(CONTROLLER_KINDS)
    if not last:
        # Primo avvio: niente da recuperare, gli eventi arrivano da ora
        state.recover_pending, state.recover_outages = False, outages
        return
    since = max(datetime.fromisoformat(last) - timedelta(seconds=RECOVER_OVERLAP), datetime.now(tz=UTC) - RECOVER_LIMIT)
    try:
        recovered = await state.service.recover_events(since)
    except VapixError as err:
        _LOGGER.warning("Eventi persi non recuperati dal controller: %s", err)
        return
    state.recover_pending, state.recover_outages = False, outages
    state.last_recovery = {"at": _now(), "since": since.isoformat(timespec="seconds"), "events": recovered}
    if recovered:
        _LOGGER.info("Recuperati %s eventi dal registro del controller", recovered)
        state.db.add_audit(
            "sistema",
            "eventi recuperati dal controller",
            f"{plural(recovered, 'evento', 'eventi')} dal {since:%d/%m %H:%M}",
        )


def _controllers_health(state) -> list[dict]:
    """Stato di ogni controller con quanto serve a notifiche e pagina Impianti."""
    rows = {c["id"]: c for c in state.db.list_controllers()}
    result = []
    for controller_id, health in state.health.states.items():
        row = rows.get(controller_id)
        if row is None:
            continue
        result.append(
            {
                **health.as_dict(),
                "identity_change": paused_reason(row),
                "clock_problem": clock_problem(health),
                "clock_fault": clock_fault(health),
                "clock_synced": controller_synced(health),
                "known": {k: row.get(k) or "" for k in ("serial", "model", "firmware", "cert_fingerprint")},
            }
        )
    return result


def _controller_clock_offset(state) -> float | None:
    """Scarto dell'orologio del controller principale rispetto a Home Assistant, solo se è sincronizzato via NTP."""
    primary = state.installation.primary
    health = state.health.states.get(primary["id"]) if primary else None
    if not health or health.clock_offset is None or not controller_synced(health):
        return None
    return health.clock_offset


def _offline_controllers(state) -> dict[int, str]:
    """Controller da non leggere per il backup: irraggiungibili per il controllo di stato (inutile aspettarli)
    o cambiati e non ancora confermati (la loro configurazione non va mescolata con quella del vecchio)."""
    offline: dict[int, str] = {}
    for row in state.db.list_controllers():
        health = state.health.states.get(row["id"])
        if row.get("identity_change"):
            offline[row["id"]] = "controller cambiato, in attesa di conferma"
        elif health and health.unreachable:
            offline[row["id"]] = f"non raggiungibile ({health.error or 'nessuna risposta'})"
    return offline


async def _backup_loop(app: FastAPI) -> None:
    """Backup all'aggiornamento dell'add-on, backup pianificati e verifica giornaliera dei file."""
    state = app.state
    with contextlib.suppress(TimeoutError):
        # Prima il controllo di stato: dice quali controller non rispondono o sono cambiati
        await asyncio.wait_for(state.health_ready.wait(), HEALTH_INTERVAL)
    try:
        if made := await state.backups.after_update(_backup_clients(state), _offline_controllers(state)):
            _LOGGER.info("Backup dopo l'aggiornamento: %s", made["summary"])
    except BackupError as err:
        _LOGGER.warning("Backup dopo l'aggiornamento non riuscito: %s", err)
    while True:
        try:
            if made := await state.backups.run_scheduled(_backup_clients(state), _offline_controllers(state)):
                _LOGGER.debug("Backup %s: %s", made["kind"], made["summary"])
            if state.backups.verify_due():
                result = await state.backups.verify_now()
                if not result["ok"]:
                    _LOGGER.error("Verifica dei backup: %s", "; ".join(result["errors"]))
        except BackupError as err:
            _LOGGER.warning("Backup pianificato non riuscito: %s", err)
        except Exception:  # noqa: BLE001 - si riprova al giro successivo
            _LOGGER.exception("Backup pianificato non riuscito")
        await asyncio.sleep(BACKUP_TICK)


def _backup_clients(state) -> dict[int, object]:
    """Client con cui leggere ogni controller per il backup: quello in uso, e per gli altri quello del controllo."""
    primary = state.installation.primary
    clients: dict[int, object] = {}
    for row in state.db.list_controllers():
        if not row["host"]:
            continue
        clients[row["id"]] = (
            state.controller if primary and row["id"] == primary["id"] else state.health.client_for(row)
        )
    return clients


def _restore_plan(state, saved: dict, current: dict, body: RestorePreviewIn) -> list[dict]:
    sections = [s for s in body.sections if s in ALL_SECTIONS]
    if not sections:
        raise BackupError("scegli cosa ripristinare")
    primary = (state.installation.primary or {}).get("id")
    if body.part and body.part != f"controller:{primary}":
        raise BackupError("per ora si ripristinano porte e lettori solo del controller in uso")
    if any(s in FULL_SECTIONS for s in sections) and body.part != f"controller:{primary}":
        raise BackupError("il ripristino completo si fa dalla scheda Backup del controller")
    return plan(saved, current, sections, primary, set(_mqtt_known(state)))


def _mqtt_known(state) -> dict[str, str]:
    """Credenziali MQTT dell'add-on: se il controller usa le stesse, la password non va chiesta."""
    mqtt = getattr(state, "mqtt", None)
    return {mqtt.username: mqtt.password} if mqtt and mqtt.username and mqtt.password else {}


def _local_text(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def _missing_summary(db: Database) -> dict:
    cards = db.missing_cards()
    if not cards:
        return {"count": 0}
    return {
        "count": len(cards),
        "persons": len({c["person_id"] for c in cards}),
        "since": min(c["missing_since"] for c in cards),
        "mass": mass_missing(len(db.all_cards()), len(cards)),
    }


def _file_time(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


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
    groups_checked = False
    loop = asyncio.get_running_loop()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(state.health_ready.wait(), HEALTH_INTERVAL)
    while True:
        delay = SYNC_INTERVAL
        if state.controller.paused:
            # Controller cambiato: niente sincronizzazione finché un amministratore non conferma
            state.last_sync = {"ok": None, "error": "", "result": None, "at": _now(), "paused": state.controller.paused}
            delay = SYNC_RETRY
        elif state.settings.a1210_host:
            try:
                result = await state.service.sync_from_controller()
                state.last_sync = {"ok": True, "error": "", "result": result, "at": _now()}
                _LOGGER.debug("Tessere sincronizzate dal controller: %s", result)
                if not groups_checked:
                    # Una volta per avvio: gruppi delle versioni precedenti con la fascia solo nelle regole
                    groups_checked = True
                    if repaired := await state.rules.repair_group_schedules():
                        _LOGGER.info("Fascia oraria aggiunta al profilo di %s gruppi", repaired)
                    # Festività che le versioni precedenti non riuscivano a scrivere (BYMONTHDAY rifiutato)
                    if rewritten := await state.rules.reapply_holidays(only_changed=True):
                        _LOGGER.info("Festività riscritte in %s fasce orarie", rewritten)
                    # Dalla 0.10 i gruppi sono dentro le tipologie
                    if adopted := await state.rules.adopt_groups():
                        _LOGGER.info("Gruppi passati alle tipologie: %s modifiche", adopted)
            except (VapixError, ServiceError) as err:
                state.last_sync = {"ok": False, "error": str(err), "result": None, "at": _now()}
                _LOGGER.warning("Sincronizzazione non riuscita: %s", err)
                delay = SYNC_RETRY
        if loop.time() - last_prune >= PRUNE_INTERVAL or last_prune == 0.0:
            state.ha.delete_snapshots(state.db.prune_events(state.settings.event_retention_days))
            last_prune = loop.time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(state.sync_wake.wait(), delay)
        state.sync_wake.clear()


async def _controller_status(controller: A1210Client, settings: Settings, inputs: list[dict]) -> dict:
    if not settings.a1210_host:
        return {"ok": False, "error": "indirizzo del controller non configurato"}
    try:
        info = await controller.device_info()
        doors = await controller.door_info_list()
        credentials = await controller.list_credentials()
    except VapixError as err:
        return {"ok": False, "error": str(err)}
    tokens = [door.get("token") for door in doors]
    # Pulsanti e relè sugli ingressi sono punti di identificazione come i lettori: si contano a parte
    wired = {row["idpoint_token"] for row in inputs if row["idpoint_token"]}
    try:
        readers = sum(1 for p in await controller.list_idpoints() if p.get("token") not in wired)
    except VapixError:
        readers = None
    return {
        "ok": True,
        "model": info.get("ProdNbr") or info.get("ProdShortName", ""),
        "firmware": info.get("Version", ""),
        "serial": info.get("SerialNumber", ""),
        "doors": tokens,
        "door_names": [door.get("Name") or door.get("token") for door in doors],
        "door_found": settings.door_token in tokens,
        "credentials": len(credentials),
        "readers": readers,
        "inputs": sum(1 for row in inputs if row["role"]),
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
