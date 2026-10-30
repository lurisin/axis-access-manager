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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .backup import BACKUP_DIR, BackupError, BackupService
from .backup_copy import ha_copy_error, ha_copy_path, update_copies
from .config import Settings, load_settings, resolve_mqtt, supervisor_token
from .controller_state import POLL_LIVE, POLL_WITHOUT_EVENTS, StateKeeper
from .db import DB_PATH, Database, local_day_start
from .events import EventMonitor
from .full_restore import FULL_SECTIONS, renamed_tokens
from .ha import HomeAssistant
from .ha_mqtt import DOOR_PHYSICAL, HaBridge, parse_command, topic_base
from .health import (
    HEALTH_INTERVAL,
    HealthMonitor,
    clock_fault,
    clock_problem,
    controller_synced,
    ha_time_zone,
    paused_reason,
)
from .installation import MAX_CONTROLLERS, InstallationService, check_host, same_controller
from .intercom import IntercomBridge, IntercomSettings
from .notifications import EXPIRING, SETTING_CATEGORIES_PENDING, NotificationService
from .programming import DIRECTIONS, INPUT_ROLES, REX_ROLES, ProgrammingService, is_rex_profile
from .restore import ALL_SECTIONS, RESTORE_SECTIONS, Runtime, apply, plan, public
from .rules_service import ProfileNotWritten, RulesService
from .service import (
    CASING_OPEN,
    CONTROLLER_KINDS,
    DENIED_REASONS,
    INPUT_TERMINALS,
    AccessService,
    ServiceError,
    input_detail,
    mass_missing,
    plural,
)
from .transfer import events_csv, import_persons, persons_csv, read_import
from .users import PERMISSIONS, ROLES, AppUser, UserDirectory, UserError, role_of
from .vapix import A1210Client, ControllerGuard, ControllerUnreachable, VapixError

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Con Ingress le richieste arrivano solo dal Supervisor
INGRESS_PROXY = "172.30.32.2"

# Eventi per cui salvare un'istantanea della telecamera
# Istantanea sempre: rifiuti, citofono, allarmi, aperture a distanza e dai pulsanti (chi passa senza tessera).
# Per una tessera una sola foto, sull'esito: la lettura e la tessera sconosciuta arrivano insieme al negato.
SNAPSHOT_KINDS = {"negato", "citofono", "allarme", "manomissione", "apertura_remota", "uscita"}
GRANTED_KINDS = {"concesso"}
# Passaggi contati come accessi nei numeri del giorno: la lettura della tessera precede il concesso (sarebbe doppia)
# e le chiamate al citofono non sono accessi; le aperture dal citofono arrivano come apertura remota.
ACCESS_COUNT_KINDS = ("concesso", "apertura_remota", "uscita")
# Il CSV del registro si scrive a pagine: tutto il registro, senza tenerlo in memoria
EXPORT_PAGE = 5000

# Recupero degli eventi persi: da poco prima dell'ultimo registrato, al massimo degli ultimi 7 giorni
RECOVER_OVERLAP = 10
RECOVER_LIMIT = timedelta(days=7)

SYNC_INTERVAL = 15 * 60
# Senza broker MQTT all'avvio: ogni quanto chiederlo di nuovo al Supervisor
MQTT_RETRY = 60
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
    year: int = Field(default_factory=lambda: date.today().year, ge=2000, le=2100)


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
            request.headers.get("X-Remote-User-Id") or "",
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
            # Le descrizioni contengono virgole: ognuna tra virgolette, separate dal punto e virgola
            missing = [f"«{PERMISSIONS[p]}»" for p in perms if not user.can(p)]
            if len(missing) == 1:
                detail = f"il tuo utente non ha il permesso {missing[0]}"
            elif any_of:
                detail = f"al tuo utente serve almeno uno di questi permessi: {'; '.join(missing)}"
            else:
                detail = f"il tuo utente non ha questi permessi: {'; '.join(missing)}"
            raise HTTPException(status_code=403, detail=detail)
        return user

    return Depends(check)


Admin = Annotated[AppUser, need("utenti")]


def before_change(kind: str = "prima_modifica") -> Any:
    """Dipendenza: backup prima di una modifica rischiosa, se è cambiato qualcosa. Va dopo il controllo dei
    permessi; una serie di modifiche di seguito fa un solo backup."""

    async def backup(request: Request) -> None:
        state = request.app.state
        async with _reading(state):
            await state.backups.before_change(
                kind, request_user(request), _backup_clients(state), _offline_controllers(state)
            )

    return Depends(backup)


# Dati rifiutati dalla validazione: nome del campo e motivo in parole per l'utente
FIELD_LABELS = {
    "name": "nome",
    "card": "tessera",
    "camera": "telecamera",
    "widgets": "riquadri",
    "size": "larghezza del riquadro",
    "height": "altezza del riquadro",
    "frequency": "frequenza dei backup",
    "night": "ora del backup notturno",
    "keep_last": "backup da tenere",
    "keep_daily": "backup da tenere",
    "keep_weekly": "backup da tenere",
    "keep_monthly": "backup da tenere",
    "keep_yearly": "backup da tenere",
    "keys": "elementi da ripristinare",
    "mqtt_password": "password del broker MQTT",
    "year": "anno",
    "limit": "numero di righe",
    "schedule": "fascia oraria",
    "host": "indirizzo del controller",
    "category_id": "tipologia",
}
VALIDATION_REASONS = {
    "missing": "valore mancante",
    "string_too_long": "al massimo {max_length} caratteri",
    "string_too_short": "almeno {min_length} caratteri",
    "too_long": "al massimo {max_length} elementi",
    "too_short": "almeno {min_length} elementi",
    "greater_than_equal": "il minimo è {ge}",
    "greater_than": "deve essere più di {gt}",
    "less_than_equal": "il massimo è {le}",
    "less_than": "deve essere meno di {lt}",
    "string_pattern_mismatch": "formato non valido",
    "literal_error": "valore non ammesso",
    "int_parsing": "serve un numero intero",
    "int_type": "serve un numero intero",
    "float_parsing": "serve un numero",
    "bool_parsing": "serve sì o no",
    "json_invalid": "testo non leggibile",
}


def _validation_text(errors: list[dict]) -> str:
    """Primo errore di validazione in parole; quello tecnico (campo, tipo, valore) resta nel log."""
    _LOGGER.info("Dati non validi: %s", errors)
    first = errors[0] if errors else {}
    names = [str(p) for p in first.get("loc") or () if not isinstance(p, int) and p not in ("body", "query", "path")]
    field = FIELD_LABELS.get(names[-1], "") if names else ""
    try:
        reason = VALIDATION_REASONS.get(first.get("type", ""), "valore non valido").format(**(first.get("ctx") or {}))
    except (KeyError, IndexError):
        reason = "valore non valido"
    return f"dati non validi: {field}, {reason}" if field else f"dati non validi: {reason}"


def _moment(value: str | None) -> datetime | None:
    """Data del controller o dell'app come datetime locale; None se manca o non è valida."""
    text = _iso(value or "")
    return datetime.fromisoformat(text) if text else None


async def _check_app_profile(state, token: str, categories: bool = False) -> None:
    """Gli endpoint dei gruppi delle versioni precedenti non toccano i profili che l'app gestisce da sé: quello di
    pulsanti e relè e, con categories, quelli delle tipologie (l'app li riscrive dalle righe di accesso)."""
    if categories:
        used_by = [c["name"] for c in state.db.list_categories() if c["group_token"] == token]
        if used_by:
            raise ServiceError(f"profilo gestito dall'app per la tipologia {used_by[0]}: si modifica da Tipologie")
    found = await state.controller.get_access_profiles([token])
    if found and is_rex_profile(found[0]):
        raise ServiceError(
            "profilo gestito dall'app per pulsanti e relè: si cambia dagli ingressi del controller in Impianti"
        )


async def _wait_for_broker(app: FastAPI) -> None:
    """Nessun broker MQTT all'avvio (Mosquitto installato dopo, Supervisor lento): lo si chiede di nuovo finché non
    c'è, poi l'ascolto degli eventi parte da solo, senza riavviare l'add-on."""
    while app.state.mqtt is None:
        await asyncio.sleep(MQTT_RETRY)
        broker = await resolve_mqtt(app.state.options, quiet=True)
        if broker is None:
            continue
        _LOGGER.info("Broker MQTT disponibile: parte l'ascolto degli eventi")
        app.state.mqtt = broker
        try:
            await reload_runtime(app)
        except Exception:  # noqa: BLE001 - l'app resta in piedi, l'errore va nel log
            _LOGGER.exception("Avvio dell'ascolto MQTT non riuscito")


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
        # Database rovinato o mancante all'avvio: nel log, in Attività e in una notifica finché l'add-on non riparte
        app.state.db_recovery = None
        if app.state.db.recovered:
            _LOGGER.error("All'avvio: %s", app.state.db.recovered)
            app.state.db.add_audit("sistema", "database controllato all'avvio", app.state.db.recovered)
            app.state.db_recovery = {"at": _now(), "text": app.state.db.recovered}
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
        # Stato del controller da rileggere subito: tornato a rispondere, ingresso programmato
        app.state.state_wake = asyncio.Event()
        await _start_runtime(app)
        maintenance = asyncio.create_task(_maintenance(app))
        health = asyncio.create_task(_health_loop(app))
        tasks = [maintenance, health, asyncio.create_task(_state_loop(app))]
        if scheduled_backups:
            tasks.append(asyncio.create_task(_backup_loop(app)))
        if not app.state.mqtt and supervisor_token():
            # Il broker del Supervisor può arrivare dopo (Mosquitto installato ora): lo si cerca di nuovo
            tasks.append(asyncio.create_task(_wait_for_broker(app)))
        yield
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - le chiusure qui sotto devono girare comunque
                _LOGGER.exception("Attività in background terminata con un errore")
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
        text = str(err)
        # "il controller non risponde", "credenziali del controller…": il controller è già nominato.
        # unreachable: nessuna risposta, l'interfaccia propone di riprovare
        detail = text if "controller" in text else f"errore del controller: {text}"
        return JSONResponse({"detail": detail, "unreachable": isinstance(err, ControllerUnreachable)}, 502)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, err: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation_text(list(err.errors()))}, status_code=422)

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
            # Fuso di Home Assistant (per esempio Europe/Rome, vuoto se sconosciuto): ore e giorni come nel server
            "time_zone": ha_time_zone(),
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
                # Quello in uso: se si sovrappone al prefisso del controller le entità vanno sotto un altro
                "topic_prefix": state.bridge.topic_prefix,
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
                # Perché l'ultima copia per il backup di Home Assistant non è stata aggiornata ("" se è andata bene)
                "ha_copy_error": ha_copy_error(state.db.path) if state.db.path else "",
                # Database ripristinato dalla copia (o non ripristinabile) all'avvio: {"at", "text"} o None
                "recovered": getattr(state, "db_recovery", None),
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
            "doors": doors if status else [{**d, "mode": None, "state": None} for d in doors],
            "configured": bool(state.settings.a1210_host),
            "mqtt_connected": state.monitor.connected if status else None,
            "unknown_cards": len(state.db.list_unknown_cards()) if user.can("persone") else 0,
            "sync": state.last_sync,
            "notifications": listing["open"] if listing else 0,
            # Barra in alto: guasti finché durano, allarmi finché non se ne prende visione
            "faults": [_brief(i) for i in listing["items"] if i["fault"]] if listing else [],
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
        # Solo quelle che l'utente vede: la presa visione vale per tutti, e ognuna resta nel registro attività
        visible = {i["id"] for i in listing["items"]}
        ids = [i for i in body.ids if i in visible] if body.ids is not None else list(visible)
        state = request.app.state
        state.notifications.mark_seen(ids, _current_notifications(state))
        # Chi ha preso visione di cosa: nel registro attività, come le altre modifiche
        chosen = set(ids)
        marked = [i for i in listing["items"] if i["id"] in chosen and not i["seen"]]
        if marked:
            titles = ", ".join(f"{i['title']} ({i['text']})" if i["text"] else i["title"] for i in marked[:3])
            more = f" e altre {len(marked) - 3}" if len(marked) > 3 else ""
            if len(marked) == 1:
                state.db.add_audit(request_user(request), "notifica segnata come vista", titles)
            else:
                detail = f"{len(marked)} notifiche: {titles}{more}"
                state.db.add_audit(request_user(request), "notifiche segnate come viste", detail)
        return {"open": _notification_listing(request)["open"]}

    @app.put("/api/notifications/settings", dependencies=[need("utenti")])
    async def notification_settings(body: dict[str, str], request: Request) -> dict:
        request.app.state.notifications.set_levels(body, request_user(request))
        return {"types": request.app.state.notifications.types()}

    @app.get("/api/dashboard", dependencies=[need("stato")])
    async def dashboard(request: Request) -> dict:
        user = current_user(request)
        state = request.app.state
        db = state.db
        today = db.count_events(local_day_start())
        week = _week_series(db.event_kinds_since(local_day_start(6)))
        persons = db.list_persons()
        cards = [c for p in persons for c in p["cards"]]
        now = datetime.now().astimezone()
        expiring = []
        for person in persons:
            ends = []
            for card in person["cards"]:
                start, end = _moment(card.get("valid_from")), _moment(card.get("valid_to"))
                # Come In Scadenza in Persone: una tessera non ancora valida non è in scadenza
                if end and now < end <= now + EXPIRING and not (start and start > now):
                    ends.append((end, card["valid_to"]))
            if ends:
                end, valid_to = min(ends)
                expiring.append((end, {"id": person["id"], "name": person["name"], "valid_to": valid_to}))
        expiring = [item for _, item in sorted(expiring, key=lambda e: e[0])]
        # Come le notifiche: raggiungibile secondo il controllo periodico (prima del primo controllo, secondo il
        # broker); eventi in arrivo solo con il broker collegato e il controller collegato al broker
        monitor = state.monitor
        primary = state.installation.primary
        health = state.health.states.get(primary["id"]) if primary else None
        if health is None or health.online is None:
            reachable = monitor.controller_online
        else:
            # Dopo un solo controllo non riuscito non è ancora irraggiungibile: stato sconosciuto
            reachable = False if health.unreachable else True if health.online else None
        return {
            "today": {
                "granted": today.get("concesso", 0),
                "denied": today.get("negato", 0),
                "unknown": today.get("sconosciuta", 0),
                "remote": today.get("apertura_remota", 0),
                "exits": today.get("uscita", 0),
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
            "expiring": expiring if user.can("persone") else [],
            "recent": db.list_events(limit=8) if user.can("registro") else [],
            "backup": await asyncio.to_thread(state.backups.overview) if user.can("backup") else None,
            "system": {
                # Controller raggiungibile dall'add-on (None: non ancora noto)
                "controller_online": reachable,
                # Eventi del controller in arrivo: senza broker no
                "mqtt_connected": bool(state.mqtt) and monitor.connected and monitor.controller_online is not False,
                "casing_open": monitor.casing_open,
                # Ultima lettura dello stato con le API del controller
                "state_read_at": monitor.state_read_at or None,
                "last_event": next(iter(db.list_events(limit=1)), {}).get("ts"),
                "configured": bool(state.settings.a1210_host),
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
        request.app.state.db.add_audit(
            request_user(request), "tessere sincronizzate dal controller", _sync_text(result)
        )
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
        # Solo le tessere contate nella conferma: senza elenco toglierebbe anche quelle sparite nel frattempo
        if body.ids is None:
            raise HTTPException(400, "indica le tessere da togliere")
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
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
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
            limit=limit,
            since=_utc(since),
            until=_utc(until),
            person_id=person_id,
            door=door,
        )

    @app.get("/api/events/count", dependencies=[need("registro")])
    async def count_events(
        request: Request,
        kind: str = "",
        q: str = "",
        since: str = "",
        until: str = "",
        person_id: int | None = None,
        door: str = "",
    ) -> dict:
        """Quanti eventi corrispondono ai filtri: serve per gli accessi nascosti dalle viste del registro."""
        count = request.app.state.db.count_matching_events(
            kind=kind, search=q, since=_utc(since), until=_utc(until), person_id=person_id, door=door
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
            return JSONResponse({"detail": "nessuna foto"}, status_code=404)
        path = request.app.state.ha.snapshot_dir / Path(event["snapshot"]).name
        if not path.exists():
            return JSONResponse({"detail": "foto cancellata"}, status_code=404)
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
        filters = {
            "kind": kind,
            "search": q,
            "since": _utc(since),
            "until": _utc(until),
            "person_id": person_id,
            "door": door,
        }
        names = {d["door_token"]: d["name"] for d in state.doors}
        header = events_csv([], names)

        async def pages():
            # Tutto il registro che corrisponde ai filtri, dal più recente, a pagine: nessun evento lasciato fuori
            # e senza tenerlo tutto in memoria. Ogni pagina riparte dall'ultimo evento scritto (ora e numero)
            yield header.encode("utf-8")
            before_id, before_ts = None, ""
            while rows := state.db.list_events(**filters, before_id=before_id, before_ts=before_ts, limit=EXPORT_PAGE):
                yield events_csv(rows, names)[len(header) :].encode("utf-8")
                before_id, before_ts = rows[-1]["id"], rows[-1]["ts"]
                await asyncio.sleep(0)

        filename = f"registro_accessi_{datetime.now():%Y%m%d_%H%M}.csv"
        return StreamingResponse(
            pages(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/export/persons.csv", dependencies=[need("persone", "import_export")])
    async def export_persons(request: Request) -> Response:
        state = request.app.state
        names = await _group_names(state)
        categories = {c["id"]: c["name"] for c in state.db.list_categories()}
        text = persons_csv(state.db.list_persons(), names, state.db.last_accesses(), categories)
        return _csv_response(text, "persone")

    @app.post("/api/import/persons", dependencies=[need("persone_modifica", "import_export")])
    async def import_csv(body: ImportIn, request: Request) -> dict:
        state = request.app.state
        try:
            groups = (await state.rules.list_groups())["groups"]
        except VapixError as err:
            _LOGGER.warning("Gruppi non leggibili per l'importazione: %s", err)
            # Ogni riga con un gruppo risulterebbe "gruppo sconosciuto": la causa vera vale per tutto il file
            if any(item.get("gruppo") for item in read_import(body.csv)):
                raise ServiceError(f"i gruppi del file non si possono controllare: {err}") from err
            groups = []
        if body.apply:
            # Un'importazione cambia molte persone insieme: il backup si fa sempre, non una volta ogni tanto
            async with _reading(state):
                await state.backups.before_change(
                    "prima_importazione",
                    request_user(request),
                    _backup_clients(state),
                    _offline_controllers(state),
                    window=False,
                )
        return await import_persons(state.service, body.csv, groups, request_user(request), body.apply)

    @app.get("/api/audit", dependencies=[need("utenti")])
    async def audit(
        request: Request, before_id: int | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[dict]:
        return request.app.state.db.list_audit(limit=limit, before_id=before_id)

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
        await _check_app_profile(request.app.state, token, categories=True)
        return await request.app.state.rules.update_group(
            token, body.name, body.description, body.schedule, request_user(request)
        )

    @app.delete("/api/groups/{token:path}", dependencies=[need("regole_modifica"), before_change()])
    async def delete_group(token: str, request: Request) -> dict:
        # Quelli delle tipologie li rifiuta già delete_group
        await _check_app_profile(request.app.state, token)
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
        # Il profilo di una tipologia sì: è quello che diventa predefinito con la tipologia predefinita
        await _check_app_profile(request.app.state, body.token)
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
        last = state.db.last_door_events(("concesso", "negato", "sconosciuta", "apertura_remota", "uscita", "citofono"))
        result = []
        for door in _doors_status(state):
            today = counts.get(door["token"], {})
            if len(state.doors) == 1:
                # Eventi registrati prima della 0.5 senza porta: con una porta sola sono suoi. Non la manomissione
                # senza porta (coperchio aperto, controller inclinato): riguarda il controller
                for kind, n in counts.get("", {}).items():
                    if kind != "manomissione":
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
                            "direction": DIRECTIONS.get(direction, ""),
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
        # Senza i punti di accesso della porta non si sa chi la apre: resta solo l'errore dei lettori
        if (user.can("regole") or user.can("persone")) and not errors:
            try:
                profiles = {p.get("token"): p for p in await state.controller.list_access_profiles()}
                names = {s["token"]: s["name"] for s in await state.rules.list_schedules(list(profiles.values()))}
                # Chi entra da questa porta: le tipologie con una regola sui suoi punti di accesso, con le fasce
                # di quelle regole
                opening = []
                for c in state.rules.list_categories():
                    profile = profiles.get(c["group_token"])
                    schedules = _policy_schedules(profile, points) if profile else None
                    if schedules is None:
                        continue
                    opening.append(
                        {
                            "name": c["name"],
                            "color": c["color"],
                            "schedule_names": [names.get(t, t) for t in schedules],
                            "persons": c["persons"],
                            "default": c["default"],
                            "enabled": profile.get("Enabled", True) is not False,
                        }
                    )
                groups = opening
            except VapixError as err:
                errors.append(f"tipologie: {err}")
        events = None
        if user.can("registro"):
            # Con una porta sola valgono anche gli eventi registrati prima che avessero la porta, ma non la
            # manomissione senza porta (coperchio aperto, controller inclinato): riguarda il controller
            door_filter = config["door_token"] + ("," if len(state.doors) == 1 else "")
            listed = state.db.list_events(door=door_filter, limit=50)
            events = [e for e in listed if e["door"] or e["kind"] != "manomissione"][:15]
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
        host = check_host(body.host)
        password = body.password
        existing = request.app.state.db.get_controller(body.controller_id) if body.controller_id else None
        if not password and existing:
            # La password salvata va solo all'indirizzo e all'utente salvati: mai a un indirizzo scritto qui
            if not same_controller(host, body.username, existing):
                raise ServiceError("indirizzo o utente cambiati: scrivi di nuovo la password del controller")
            password = existing["password"]
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

    @app.post("/api/controllers/{controller_id}/time-zone", dependencies=[need("impianti")])
    async def set_controller_time_zone(controller_id: int, request: Request) -> list[dict]:
        """Fuso orario di Home Assistant nel controller: fasce orarie e festività scattano all'ora giusta."""
        state = request.app.state
        row = state.db.get_controller(controller_id)
        if row is None:
            raise HTTPException(404, "controller non trovato")
        zone = ha_time_zone()
        if not zone:
            raise ServiceError("fuso orario di Home Assistant sconosciuto: impostalo a mano nel controller")
        primary = state.installation.primary
        if primary and primary["id"] == controller_id:
            client = state.controller
        else:
            # Un altro controller: il client del controllo, con le scritture sospese se è cambiato
            client = ControllerGuard(state.health.client_for(row))
            client.paused = paused_reason(row)
        health = state.health.states.get(controller_id)
        old = health.time_zone if health else ""
        await client.set_time_zone(zone)
        state.db.add_audit(
            request_user(request), "fuso orario del controller impostato", f"{row['name']}: {old or '—'} → {zone}"
        )
        # L'avviso sparisce subito, non alla rilettura dei 5 minuti
        await state.health.recheck_clock(row)
        return _controllers_health(state)

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
        # Profili delle tipologie rimasti indietro mentre le scritture erano sospese
        if state.db.get_setting(SETTING_CATEGORIES_PENDING):
            await _refresh_categories(request.app)
        return {"ok": True}

    @app.delete("/api/controllers/{controller_id}", dependencies=[need("impianti"), before_change()])
    async def delete_controller(controller_id: int, request: Request) -> dict:
        # Come per porte, aree e strutture: non si toglie finché le sue porte sono nelle righe di una tipologia
        controller = request.app.state.db.get_controller(controller_id)
        for door in (controller or {}).get("doors", []):
            try:
                request.app.state.rules.check_place_unused("door", door["id"])
            except ServiceError as err:
                raise ServiceError(f"la sua porta {door['name']} {err}") from err
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
        state = request.app.state
        data = await state.programming.read()
        # Stato in tempo reale degli ingressi che mandano gli eventi Device/IO/Port
        monitor = state.monitor
        for item in data.get("inputs", []):
            item["active"] = monitor.input_active(item["port"], item["role"] in REX_ROLES)
            door = item.get("door") or ""
            if item["role"] == "door_monitor" and door:
                # Il contatto porta non manda Device/IO/Port: vale lo stato della porta (DoorPhysicalState)
                item["door_open"] = DOOR_PHYSICAL.get(monitor.door_physical.get(door, ""))
            if item["role"] in REX_ROLES and door:
                # L'ingresso REX non manda Device/IO/Port: almeno l'ora dell'ultima pressione nel Registro
                item["last_press"] = state.db.last_input_press(door, input_detail(item["io"], item["role"]))
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
        state = request.app.state
        saved = await state.programming.update_input(io, body.model_dump(), request_user(request))
        await _input_changed(state)
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
        permitted = await state.programming.repair_rex_profile(request_user(request))
        await state.health.recheck_managed(state.installation.primary)
        return {"managed": managed, "permitted": permitted}

    @app.put("/api/programming/links", dependencies=[need("impianti"), before_change()])
    async def program_links(body: LinksIn, request: Request) -> dict:
        links = [link.model_dump() for link in body.links]
        changed = await request.app.state.programming.set_links(links, request_user(request))
        return {"changed": changed}

    @app.post("/api/programming/changes/{change_id}/undo", dependencies=[need("impianti"), before_change()])
    async def undo_change(change_id: int, request: Request) -> dict:
        if await request.app.state.programming.undo(change_id, request_user(request)) == "input":
            await _input_changed(request.app.state)
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
        async with _reading(state):
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
        # Un controller che non risponde non si aspetta e non risulta tolto: il confronto dice che non è letto
        async with _reading(state):
            return await state.backups.compare(
                backup_id, other, _backup_clients(state), part or None, offline=_offline_controllers(state)
            )

    @app.put("/api/backups/{backup_id}/lock", dependencies=[need("backup")])
    async def lock_backup(backup_id: str, body: BackupLockIn, request: Request) -> dict:
        # Sbloccato, il backup può scadere al backup successivo: sbloccare vale come eliminare, da amministratori
        if not body.locked and not current_user(request).can("utenti"):
            raise HTTPException(status_code=403, detail="sbloccare un backup è riservato agli amministratori")
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
        # Un controller che non risponde non ha la sua parte nello stato attuale: porte e lettori restano bloccati.
        # Quelli già irraggiungibili per il controllo di stato non si aspettano, come nel ripristino
        async with _reading(state):
            current, _ = await state.backups.snapshot(_backup_clients(state), offline=_offline_controllers(state))
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
        # Con la data e non con il nome: "Prima del Ripristino di Prima del Ripristino di …" non si legge
        name = f"Prima del Ripristino del Backup del {_local_text(manifest['created_at'])}"
        async with _reading(state):
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
            # Profili riscritti dal backup: se dopo le tipologie non si aggiornano, l'aggiornamento resta da fare
            state.places_key = None
            await reload_runtime(request.app)
        if full:
            # Backup di prima della 0.21.3: il permesso dei pulsanti non c'era, lo si rimette
            try:
                await state.programming.repair_rex_profile(user.label)
            except (VapixError, ServiceError) as err:
                _LOGGER.warning("Permesso dei pulsanti non rimesso dopo il ripristino: %s", err)
            await state.health.recheck_managed(state.installation.primary)
            # Le regole delle fasce tornano solo con le righe delle fasce spuntate (anche «Riporta le regole della
            # fascia nell'app»), che le scrivono già con le festività di oggi: le altre fasce non si toccano
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


# Operazioni ancora in corso sul client del controller (comandi di Home Assistant e del citofono, sincronizzazione,
# scritture a più passi): quanto si aspetta prima di chiuderlo allo spegnimento e con una nuova configurazione
STOP_GRACE = 5
RELOAD_GRACE = 30
# Stato del monitor che il controller manda solo quando cambia: con una nuova configurazione si tiene
MONITOR_STATE = (
    "controller_online",
    "door_modes",
    "door_physical",
    "door_contacts",
    "reachable",
    "state_read_at",
    "door_conditions",
    "casing_open",
    "system_ready",
    "io_ports",
    "io_changed",
    "categories",
    "last_card",
)


async def _start_runtime(app: FastAPI) -> None:
    """Crea client del controller, ascolto MQTT e servizi con la configurazione degli impianti."""
    state = app.state
    old_monitor = getattr(state, "monitor", None)
    old_service = getattr(state, "service", None)
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
    if old_monitor is not None and old_monitor.prefix == state.monitor.prefix:
        # Nuova configurazione (telecamera, nome di una porta…): allarmi, coperchio, ingressi e contatto porta
        # arrivano solo quando cambiano, e un monitor vuoto li mostrava sconosciuti in Home Assistant
        for name in MONITOR_STATE:
            setattr(state.monitor, name, getattr(old_monitor, name))
    # Eventi persi da recuperare dal registro del controller: all'avvio e dopo ogni interruzione
    state.recover_pending = True
    state.recover_outages = 0
    if getattr(state, "recover_from", None) is None:
        # Da dove recuperare: l'ultimo evento del registro quando era completo. Una nuova configurazione tiene
        # quello di prima, che può essere di prima di un'interruzione non ancora recuperata
        state.recover_from = _recover_mark(state)
    # Stato del controller letto con le API: per intero alla prima lettura, anche con una nuova configurazione
    state.state_keeper = StateKeeper(state.monitor, getattr(state, "state_wake", None))
    state.state_keeper.request_full()
    state.service = AccessService(state.db, state.controller, cfg, state.monitor)
    if old_service is not None:
        # Le operazioni ancora in corso con la configurazione di prima e le nuove non si accavallano
        state.service.lock = old_service.lock
        # Porta di ogni punto di accesso e lettore: dal controller, non dalla configurazione dell'app
        state.service.access_point_doors = old_service.access_point_doors
        state.service.idpoint_doors = old_service.idpoint_doors
    elif not state.controller.paused and cfg.a1210_host:
        # Eventi in diretta prima della prima sincronizzazione: senza la mappa finivano sulla porta principale
        state.points_task = asyncio.create_task(_load_access_points(state.service))
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
    # Letture in corso con il client di questa configurazione fuori dal lock del servizio (vedi _reading)
    state.readers = set()
    _wire(app)
    state.mqtt_configured = bool(state.mqtt)
    state.monitor_task = (
        asyncio.create_task(state.monitor.run(state.mqtt)) if state.mqtt and cfg.mqtt_topic_prefix else None
    )
    # Contatori di oggi in Home Assistant ripubblicati a mezzanotte
    state.bridge.start()
    if not hasattr(state, "places_key"):
        # All'avvio: porte e aree di cui i profili delle tipologie sul controller tengono già conto (un aggiornamento
        # rimasto da fare è in SETTING_CATEGORIES_PENDING). None: non si sa, vedi _refresh_categories
        state.places_key = _places_key(state)


async def _load_access_points(service: AccessService) -> None:
    try:
        await service.load_access_points()
    except VapixError as err:
        # Li caricano poi il recupero degli eventi e la sincronizzazione
        _LOGGER.debug("Punti di accesso non letti all'avvio: %s", err)


async def _stop_runtime(app: FastAPI) -> None:
    """Allo spegnimento: ascolto fermato, poi il client del controller chiuso quando nessuno lo usa più."""
    await _close_runtime(app, await _stop_listening(app), STOP_GRACE)


async def _stop_listening(app: FastAPI, offline: bool = True) -> tuple:
    """Ferma l'ascolto MQTT. Restituisce client, lock, sorgenti di comandi e letture in corso di questa
    configurazione, da chiudere con _close_runtime quando hanno finito. offline=False se la nuova configurazione
    ripubblica subito le stesse entità di Home Assistant (vedi _stays_online)."""
    state = app.state
    bridge = getattr(state, "bridge", None)
    if bridge:
        # Prima di chiudere la connessione: le entità di Home Assistant diventano non disponibili (con una chiusura
        # ordinata il broker scarta il messaggio di addio)
        await bridge.stop(offline)
    task = getattr(state, "monitor_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return state.raw_controller, state.service.lock, [state.monitor, *state.intercoms], state.readers


async def _close_runtime(app: FastAPI, old: tuple, grace: float) -> None:
    """Chiude il client del controller di una configurazione quando i comandi di Home Assistant e del citofono, le
    operazioni sotto il lock (sincronizzazione, scritture a più passi) e le letture di backup, confronti e recupero
    degli eventi hanno finito, al massimo dopo grace secondi. Chiuso sotto i loro piedi, la porta non si apriva e
    restava solo un errore nel log; un backup risultava incompleto."""
    client, lock, sources, readers = old
    tasks = {task for source in sources for task in source._tasks}
    try:
        async with asyncio.timeout(grace):
            if tasks:
                await asyncio.wait(tasks)
            async with lock:
                pass
            for done in list(readers):
                await done.wait()
    except TimeoutError:
        _LOGGER.warning("Operazioni sul controller ancora in corso dopo %s s: il collegamento si chiude", grace)
    if app.state.injected_controller is None:
        await client.aclose()


@contextlib.asynccontextmanager
async def _reading(state):
    """Letture del controller fuori dal lock del servizio (backup, confronti, anteprima del ripristino, recupero
    degli eventi): una nuova configurazione chiude il client di adesso solo quando hanno finito. Il client va
    preso dentro il blocco, e dentro il blocco non si ricarica la configurazione."""
    done = asyncio.Event()
    readers = getattr(state, "readers", set())
    readers.add(done)
    try:
        yield
    finally:
        readers.discard(done)
        done.set()


def _stays_online(app: FastAPI) -> bool:
    """La nuova configurazione si ricollega al broker e pubblica la disponibilità delle entità sullo stesso topic:
    le entità di Home Assistant non passano per «non disponibile» (le automazioni ripartirebbero da sole)."""
    state = app.state
    bridge = getattr(state, "bridge", None)
    if bridge is None or getattr(state, "monitor_task", None) is None or not state.mqtt:
        return False
    cfg = state.installation.effective_settings(state.options)
    if not cfg.ha_integration or not cfg.mqtt_topic_prefix:
        return False
    return f"{topic_base(cfg.ha_topic_prefix, cfg.mqtt_topic_prefix)}/disponibile" == bridge.availability_topic


async def reload_runtime(app: FastAPI) -> None:
    """Riparte con la nuova configurazione dopo una modifica a controller o porte."""
    async with app.state.reload_lock:
        old = await _stop_listening(app, offline=not _stays_online(app))
        await _start_runtime(app)
    # Il client di prima si chiude dopo le operazioni che lo stanno ancora usando; le nuove usano già quello nuovo
    await _close_runtime(app, old, RELOAD_GRACE)
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


def _places_key(state) -> tuple:
    """Controller in uso e porte con area e struttura: quello che decide le porte coperte dalle tipologie."""
    installation = state.installation
    area_site = {a["id"]: s["id"] for s in state.db.list_sites() for a in s["areas"]}
    doors = []
    for door in installation.doors():
        area = installation.door_area(door)
        doors.append((door["id"], door["door_token"], area, area_site.get(area, 0)))
    return state.settings.a1210_host, tuple(doors)


async def _refresh_categories(app: FastAPI) -> None:
    """Porte spostate tra aree e strutture: le tipologie che le comprendono aggiornano il loro profilo.
    Se il controller non risponde o le scritture sono sospese, il controller applicherebbe ancora le righe di prima:
    si ricorda e si riprova dopo ogni sincronizzazione riuscita e dopo la conferma di un controller cambiato."""
    state = app.state
    if not state.settings.a1210_host:
        return
    places = _places_key(state)
    try:
        if changed := await state.rules.refresh_category_profiles():
            _LOGGER.info("Profili di %s tipologie aggiornati dopo una modifica alle porte", changed)
    except (VapixError, ServiceError) as err:
        if (
            not isinstance(err, ProfileNotWritten)
            and places == state.places_key
            and not state.controller.paused
            and not state.db.get_setting(SETTING_CATEGORIES_PENDING)
        ):
            # Porte e aree come all'ultimo aggiornamento riuscito (cambiati solo telecamera, nome, videocitofono…) e
            # profili solo non letti: sul controller non manca niente e l'errore resta nel log. Un profilo diverso non
            # scritto invece si riprova, anche quando l'errore viene dalle scritture sospese
            _LOGGER.warning("Profili delle tipologie non controllati: %s", err)
            return
        _LOGGER.warning("Profili delle tipologie non aggiornati, si riprova: %s", err)
        state.db.set_setting(SETTING_CATEGORIES_PENDING, json.dumps({"at": _now(), "error": str(err)}))
        return
    state.places_key = places
    if state.db.get_setting(SETTING_CATEGORIES_PENDING):
        state.db.set_setting(SETTING_CATEGORIES_PENDING, "")


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
        # Prima Home Assistant: l'istantanea può aspettare la telecamera fino a 10 s, e anche se non si salva
        # l'evento (allarme, rifiuto, citofono) deve arrivare alle automazioni
        await bridge.publish_event(row)
        door = doors.get(row.get("door") or cfg.door_token) or {}
        camera = door.get("snapshot_camera") or ""
        if row.get("kind") == "manomissione" and not row.get("door"):
            # Coperchio del controller: la telecamera del controller, mai quella di una porta
            camera = state.controller_camera
        kinds = SNAPSHOT_KINDS | (GRANTED_KINDS if door.get("snapshot_on_granted") else set())
        if camera and row.get("kind") in kinds:
            await _save_snapshot(state, camera, row)

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
        # Stesso citofono di una porta precedente: la chiamata la segue quella (vedi sotto)
        intercom.call = next((i for i in state.intercoms if i.call_topic == intercom.call_topic), None)
        state.intercoms.append(intercom)
        for topic in intercom.topics:
            # Stesso citofono su due porte (relè 1 e 2): la chiamata è una e la tiene la prima porta, invece di
            # passare all'ultima; ogni relè resta della sua porta
            monitor.extra_subscriptions.setdefault(topic, intercom.handle)
    if cfg.ha_integration:
        monitor.will = bridge.will()
        monitor.on_connect = bridge.on_connect
        if bridge.has_buttons:
            monitor.command_topic = bridge.command_topic
            allowed = {d["door_token"] for d in state.doors if d["ha_buttons"]}

            async def on_command(command: str) -> None:
                parsed = parse_command(command)
                # Senza porta (automazioni delle versioni precedenti): la porta con gli identificativi di sempre
                door = (parsed[1] or bridge.legacy_door()) if parsed else ""
                if parsed is None or door not in allowed:
                    _LOGGER.warning("Comando sconosciuto da Home Assistant: %s", command)
                    return
                # Chi ha premuto il pulsante, se Home Assistant lo sa: "Luca (Home Assistant)"
                # Al massimo 2 secondi, per non ritardare l'apertura se Home Assistant è lento
                try:
                    who = await asyncio.wait_for(app.state.ha.button_presser(command), 2)
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
                    return
                except (ServiceError, VapixError) as err:
                    reason = str(err)
                except Exception:  # noqa: BLE001 - per esempio il collegamento chiuso da una nuova configurazione
                    _LOGGER.exception("Apertura da Home Assistant non eseguita")
                    reason = "errore imprevisto, dettagli nel log dell'add-on"
                _LOGGER.warning("Apertura da Home Assistant non eseguita: %s", reason)
                # Nel Registro e in Home Assistant, come un rifiuto con chi ha premuto: la porta è rimasta chiusa
                service.log_event(
                    ts=service.event_now(),
                    kind="negato",
                    detail=f"apertura non eseguita: {reason}",
                    source=source,
                    door=door,
                )

            monitor.on_command = on_command
    else:
        # Rimuove le entità pubblicate in precedenza
        monitor.on_connect = bridge.on_connect


async def _save_snapshot(state, camera: str, row: dict) -> None:
    """Istantanea di un evento. L'ultimo esito per telecamera va nella scheda Telecamere del controller."""
    try:
        filename, error = await state.ha.save_snapshot(camera, f"{row['id']}_{_file_stamp(row.get('ts') or '')}")
    except OSError:
        # Disco di Home Assistant pieno o cartella Media in sola lettura
        _LOGGER.exception("Istantanea da %s non salvata", camera)
        filename, error = "", "spazio esaurito o cartella Media non scrivibile"
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Istantanea da %s non salvata", camera)
        filename, error = "", "errore imprevisto, dettagli nel log dell'add-on"
    if filename or error:
        state.db.set_event_snapshot(row["id"], filename, error)
    state.snapshot_errors.pop(camera, None)
    if error:
        state.snapshot_errors[camera] = {"ts": row.get("ts") or _now(), "error": error}


def _policy_schedules(profile: dict, points: list[str]) -> list[str] | None:
    """Fasce con cui un profilo apre una porta, dalle sue regole sui punti di accesso della porta; None se non la
    apre. La fascia del profilo vale solo per le regole senza fascia: con fasce diverse per porta l'app ci mette
    Sempre, perché l'A1210 le valuta insieme a quelle delle regole."""
    found = None
    for policy in profile.get("AccessPolicy") or []:
        if policy.get("AccessPoint") not in points:
            continue
        found = found if found is not None else []
        for token in policy.get("Schedule") or profile.get("Schedule") or []:
            if token not in found:
                found.append(token)
    return found


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
            "state": _door_state(state.monitor, d["door_token"]),
            "camera": d["snapshot_camera"],
            "snapshot_on_granted": d["snapshot_on_granted"],
            "ha_buttons": d["ha_buttons"],
            "intercom": d["intercom_name"],
        }
        for d in state.doors
    ]


def _door_state(monitor: EventMonitor, token: str) -> dict:
    """Contatto ("open", "closed", "none" senza contatto porta, None se non si sa), allarme, preallarme e manomissione
    (stato del controller, None se non si sa). known: il controller risponde e lo stato è stato letto."""
    if monitor.reachable is not True:
        return {"known": False, "contact": None, "alarm": None, "warning": None, "tamper": None}
    if monitor.door_contacts.get(token) is False:
        contact = "none"
    else:
        contact = {True: "open", False: "closed"}.get(DOOR_PHYSICAL.get(monitor.door_physical.get(token, "")))
    conditions = monitor.door_conditions.get(token, {})
    return {
        "known": True,
        "contact": contact,
        **{
            name: (conditions.get(key) or {}).get("state")
            for name, key in (("alarm", "allarme"), ("warning", "preallarme"), ("tamper", "manomissione"))
        },
    }


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
        primary_id=(state.installation.primary or {}).get("id"),
        controller_offline_since=state.monitor.controller_offline_since,
        mqtt_disconnected_since=state.monitor.disconnected_since,
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
        # Database ripristinato all'avvio e copia per il backup di Home Assistant non aggiornata ("" se tutto bene)
        db_recovery=(getattr(state, "db_recovery", None) or {}).get("text", ""),
        ha_copy_error=ha_copy_error(state.db.path) if state.db.path else "",
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
        # I messaggi della chiamata arrivano alla prima porta dello stesso citofono
        "last_message_at": max(
            (i.last_message_at for i in (intercom, intercom.call) if i and i.last_message_at), default=None
        ),
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
    missing = result.get("missing") or 0
    return text + (f", {plural(missing, 'non presente', 'non presenti')} sul controller" if missing else "")


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
            responding = _responding(state)
            await state.health.check_all()
            now = state.health.states.get(primary["id"]) if primary else None
            if was_down and now is not None and now.online:
                _LOGGER.info("Controller di nuovo raggiungibile: rilettura delle tessere")
                state.sync_wake.set()
            _apply_pause(state)
            if before != state.controller.paused:
                state.service.notify_change()
            if _responding(state) != responding:
                # Non risponde più: stato sconosciuto subito. Torna a rispondere: lettura completa subito
                state.state_wake.set()
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
    if not pending:
        # Nessuna interruzione da recuperare: il registro è completo fin qui. Il punto da cui recuperare si fissa
        # adesso e non al recupero, quando gli eventi arrivati in diretta dopo l'interruzione lo avrebbero già
        # spostato oltre quelli persi
        state.recover_from = _recover_mark(state)
        return
    primary = state.installation.primary
    health = state.health.states.get(primary["id"]) if primary else None
    if not health or not health.online or state.controller.paused:
        return
    if state.mqtt and (not monitor.connected or monitor.controller_online is False):
        return
    outages = monitor.outages
    last, marked_at = state.recover_from
    if not last:
        # Primo avvio: niente da recuperare, gli eventi arrivano da ora
        state.recover_pending, state.recover_outages = False, outages
        return
    try:
        start = datetime.fromisoformat(last)
    except ValueError:
        start = marked_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    # Un evento con l'ora nel futuro (orologio del controller avanti, poi corretto) non porta l'inizio oltre il
    # momento in cui il punto è stato fissato: i recuperi successivi non leggerebbero più niente
    since = min(start, marked_at) - timedelta(seconds=RECOVER_OVERLAP)
    since = max(since, datetime.now(tz=UTC) - RECOVER_LIMIT)
    try:
        async with _reading(state):
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
            f"{plural(recovered, 'evento', 'eventi')} dal {since.astimezone():%d/%m/%Y %H:%M}",
        )


def _recover_mark(state) -> tuple[str, datetime]:
    """Ultimo evento del controller nel registro e ora di adesso: con il registro completo, il punto da cui
    recuperare dopo la prossima interruzione."""
    return state.db.last_controller_event(CONTROLLER_KINDS), datetime.now(tz=UTC)


async def _input_changed(state) -> None:
    """Dopo aver programmato un ingresso, o annullato la sua programmazione dallo Storico Modifiche."""
    # I nomi degli ingressi sono anche quelli delle entità di Home Assistant
    await state.bridge.publish_input_names()
    await state.health.recheck_managed(state.installation.primary)
    # Contatto porta appena collegato o tolto: Contatto Porta subito giusto in Home Assistant
    state.state_keeper.request_full()


def events_live(state) -> bool:
    """Gli eventi del controller principale arrivano in diretta: app collegata al broker e controller collegato."""
    monitor = state.monitor
    return bool(state.mqtt) and monitor.connected and monitor.controller_online is not False


def _responding(state) -> bool | None:
    """Il controller principale risponde all'app: True, False (non risponde da almeno un minuto, o all'indirizzo
    risponde un controller diverso), None se non si sa ancora (primo controllo non fatto, un solo controllo fallito)."""
    primary = state.installation.primary
    health = state.health.states.get(primary["id"]) if primary and primary.get("host") else None
    if health is None:
        return None
    if health.unreachable or state.controller.paused:
        return False
    return True if health.online else None


async def _state_loop(app: FastAPI) -> None:
    """Stato del controller con le API (vedi controller_state): ogni POLL_LIVE secondi con gli eventi in diretta,
    ogni POLL_WITHOUT_EVENTS senza; subito quando il controller smette o torna a rispondere."""
    state = app.state
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(state.health_ready.wait(), HEALTH_INTERVAL)
    while True:
        state.state_wake.clear()
        try:
            async with _reading(state):
                keeper = state.state_keeper
                doors = [d["door_token"] for d in state.installation.doors()]
                changed = await keeper.tick(state.controller, doors, _responding(state))
            if changed:
                state.service.notify_change()
        except Exception:  # noqa: BLE001 - la lettura riparte al giro successivo
            _LOGGER.exception("Lettura dello stato del controller non riuscita")
        interval = POLL_LIVE if events_live(state) else POLL_WITHOUT_EVENTS
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(state.state_wake.wait(), interval)


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
    # Orologio di Home Assistant spostato dopo la misura: lo scarto si corregge subito, non alla rilettura
    return health.clock_offset_now()


def _offline_controllers(state) -> dict[int, str]:
    """Controller da non leggere per il backup: irraggiungibili per il controllo di stato (inutile aspettarli)
    o cambiati e non ancora confermati (la loro configurazione non va mescolata con quella del vecchio)."""
    offline: dict[int, str] = {}
    for row in state.db.list_controllers():
        health = state.health.states.get(row["id"])
        if row.get("identity_change"):
            offline[row["id"]] = "controller cambiato, in attesa di conferma"
        elif health and health.unreachable:
            offline[row["id"]] = f"non raggiungibile, {health.error}" if health.error else "non raggiungibile"
    return offline


async def _backup_loop(app: FastAPI) -> None:
    """Backup all'aggiornamento dell'add-on, backup pianificati e verifica giornaliera dei file."""
    state = app.state
    with contextlib.suppress(TimeoutError):
        # Prima il controllo di stato: dice quali controller non rispondono o sono cambiati
        await asyncio.wait_for(state.health_ready.wait(), HEALTH_INTERVAL)
    try:
        async with _reading(state):
            made = await state.backups.after_update(_backup_clients(state), _offline_controllers(state))
        if made:
            _LOGGER.info("Backup dopo l'aggiornamento: %s", made["summary"])
    except BackupError as err:
        _LOGGER.warning("Backup dopo l'aggiornamento non riuscito: %s", err)
    except Exception:  # noqa: BLE001 - i backup pianificati partono lo stesso
        _LOGGER.exception("Backup dopo l'aggiornamento non riuscito")
    while True:
        try:
            async with _reading(state):
                made = await state.backups.run_scheduled(_backup_clients(state), _offline_controllers(state))
            if made:
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
    # Token già cambiati dai ripristini completi: il backup ha ancora quelli che il controller aveva rifiutato
    return plan(saved, current, sections, primary, set(_mqtt_known(state)), renamed_tokens(state.db))


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
    # Riparazioni di una volta per avvio già riuscite
    repaired: set[str] = set()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(state.health_ready.wait(), HEALTH_INTERVAL)
    while True:
        try:
            synced, delay = await _sync(state)
        except Exception:  # noqa: BLE001 - un errore imprevisto non deve fermare per sempre sincronizzazione e pulizia
            _LOGGER.exception("Sincronizzazione non riuscita")
            state.last_sync = {
                "ok": False,
                "error": "errore imprevisto: dettagli nel log dell'add-on",
                "result": None,
                "at": _now(),
            }
            synced, delay = False, SYNC_RETRY
        if synced:
            await _startup_repairs(state, repaired)
            try:
                # Profili delle tipologie rimasti indietro (controller che non rispondeva, scritture sospese)
                if state.db.get_setting(SETTING_CATEGORIES_PENDING):
                    await _refresh_categories(app)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Profili delle tipologie non aggiornati")
        if loop.time() - last_prune >= PRUNE_INTERVAL or last_prune == 0.0:
            try:
                state.ha.delete_snapshots(state.db.prune_events(state.settings.event_retention_days))
            except Exception:  # noqa: BLE001 - si riprova il giorno dopo
                _LOGGER.exception("Pulizia del registro non riuscita")
            last_prune = loop.time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(state.sync_wake.wait(), delay)
        state.sync_wake.clear()


async def _sync(state) -> tuple[bool, int]:
    """Un giro di sincronizzazione delle tessere: se è riuscita e fra quanti secondi il prossimo."""
    primary = state.installation.primary
    if state.settings.a1210_host and primary and primary["host"] and not state.controller.paused:
        # Chi risponde all'indirizzo, subito prima di leggere: dopo un'interruzione il nuovo tentativo può arrivare
        # prima del controllo di stato, e un controller sostituito non si legge finché non lo si conferma
        health = await state.health.check(primary)
        _apply_pause(state)
        if state.controller.paused:
            state.service.notify_change()
        if not health.online and not state.controller.paused:
            state.last_sync = {"ok": False, "error": health.error, "result": None, "at": _now()}
            _LOGGER.warning("Sincronizzazione non riuscita: %s", health.error)
            return False, SYNC_RETRY
    if state.controller.paused:
        # Controller cambiato: niente sincronizzazione finché un amministratore non conferma
        state.last_sync = {"ok": None, "error": "", "result": None, "at": _now(), "paused": state.controller.paused}
        return False, SYNC_RETRY
    if not state.settings.a1210_host:
        return False, SYNC_INTERVAL
    try:
        result = await state.service.sync_from_controller()
    except (VapixError, ServiceError) as err:
        state.last_sync = {"ok": False, "error": str(err), "result": None, "at": _now()}
        _LOGGER.warning("Sincronizzazione non riuscita: %s", err)
        return False, SYNC_RETRY
    state.last_sync = {"ok": True, "error": "", "result": result, "at": _now()}
    _LOGGER.debug("Tessere sincronizzate dal controller: %s", result)
    return True, SYNC_INTERVAL


async def _startup_repairs(state, done: set[str]) -> None:
    """Una volta per avvio, dopo una sincronizzazione riuscita. Ognuna per conto suo: una che non riesce (profilo
    rifiutato dal controller, rete) non ferma le altre, non tocca l'esito della sincronizzazione e si ritenta dopo
    la sincronizzazione successiva."""

    async def schedules() -> None:
        # Gruppi delle versioni precedenti con la fascia solo nelle regole
        if repaired := await state.rules.repair_group_schedules():
            _LOGGER.info("Fascia oraria aggiunta al profilo di %s gruppi", repaired)

    async def holidays() -> None:
        # Festività doppie o con i nomi di prima; poi quelle che le versioni precedenti non riuscivano a scrivere
        # (BYMONTHDAY rifiutato) e quelle appena cambiate
        if upgraded := state.rules.upgrade_holidays():
            _LOGGER.info("Festività aggiornate: %s righe", upgraded)
        if rewritten := await state.rules.reapply_holidays(only_changed=True):
            _LOGGER.info("Festività riscritte in %s fasce orarie", rewritten)

    async def groups() -> None:
        # Dalla 0.10 i gruppi sono dentro le tipologie
        if adopted := await state.rules.adopt_groups():
            _LOGGER.info("Gruppi passati alle tipologie: %s modifiche", adopted)

    async def rex() -> None:
        # Fino alla 0.21.2 pulsanti e relè non avevano il permesso di aprire: il controller li negava
        if permitted := await state.programming.repair_rex_profile():
            _LOGGER.info("Permesso di apertura dato a %s ingressi", permitted)
            await state.health.recheck_managed(state.installation.primary)

    for name, repair in (("fasce", schedules), ("festività", holidays), ("gruppi", groups), ("pulsanti", rex)):
        if name in done:
            continue
        try:
            await repair()
        except (VapixError, ServiceError) as err:
            _LOGGER.warning("Riparazione all'avvio (%s) non riuscita, si riprova: %s", name, err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Riparazione all'avvio (%s) non riuscita, si riprova", name)
        else:
            done.add(name)


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
