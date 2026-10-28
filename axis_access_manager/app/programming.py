"""Programmazione del controller dall'app: nomi e tempi delle porte, lettori, collegamento lettore-porta.

Ogni scrittura legge prima l'oggetto dal controller, cambia solo i campi scelti e salva la versione
precedente: dall'app si può annullare la modifica. Nessun comando di sblocco passa da qui.
"""

from __future__ import annotations

import copy
import logging
import re

from .db import Database
from .service import ServiceError, normalize_name, normalize_text
from .vapix import A1210Client, ControllerUnreachable, VapixError

_LOGGER = logging.getLogger(__name__)

DURATION_PATTERN = re.compile(r"^P(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$")

# Tempi della porta: campo Axis → (chiave nell'app, nome nell'interfaccia, minimo, massimo in secondi).
# I tempi di sblocco hanno un massimo prudente: la serratura non deve restare alimentata a lungo.
DOOR_TIMES = {
    "AccessTime": ("access_time", "Tempo di Apertura", 1, 30),
    "ExtendedAccessTime": ("extended_access_time", "Apertura Estesa", 1, 60),
    "OpenTooLongTime": ("open_too_long_time", "Allarme Aperta Troppo a Lungo", 1, 3600),
    "ExtendedOpenTooLongTime": ("extended_open_too_long_time", "Allarme con Apertura Estesa", 1, 7200),
    "PreAlarmTime": ("pre_alarm_time", "Preallarme", 0, 3600),
}
DIRECTIONS = {"in": "Ingresso", "out": "Uscita"}
# Ingressi fisici dell'A1210 (nomi del servizio axisio) → nome di serie e porta I/O degli eventi, se c'è.
# Contatto porta e REX sono il connettore Door (pin 1-2 e 3-4); gli altri danno anche gli eventi Device/IO/Port.
CONTROLLER_INPUTS = {
    "DPS": ("Ingresso Contatto Porta", None),
    "REX": ("Ingresso REX", None),
    "IO1": ("Ingresso AUX 1", "0"),
    "IO2": ("Ingresso AUX 2", "1"),
    "EXTERNAL_TAMPER": ("Ingresso Manomissione", "2"),
    "ALARM": ("Ingresso Allarme", "3"),
}
INPUT_ROLES = {
    "": "Non Usato",
    "rex": "Pulsante Uscita",
    "intercom": "Relè Citofono",
    "door_monitor": "Sensore Porta",
    "alarm": "Ingresso di Allarme",
}
# Ruoli che aprono o sorvegliano una porta: vanno collegati a una porta del controller
DOOR_ROLES = {"rex", "intercom", "door_monitor"}
# Ruoli possibili per ogni ingresso: il contatto porta e il REX sono del connettore Door, gli AUX sono liberi
ALLOWED_ROLES = {
    "DPS": {"", "door_monitor"},
    "REX": {"", "rex", "intercom"},
    "IO1": {"", "rex", "intercom", "alarm"},
    "IO2": {"", "rex", "intercom", "alarm"},
    "EXTERNAL_TAMPER": {"", "alarm"},
    "ALARM": {"", "alarm"},
}
CONTACTS = {"no": "Normalmente Aperto", "nc": "Normalmente Chiuso"}
# Sensore Porta (DoorMonitor della porta, provato al banco): stato del contatto con la porta aperta e chiusa.
# Normalmente Aperto: con la porta chiusa il contatto è aperto, aprendo la porta si chiude verso massa
DOOR_MONITOR_VALUES = {
    "no": {"DoorMonitor.ValueWhenOpen": "Input Ground", "DoorMonitor.ValueWhenClosed": "Input Open"},
    "nc": {"DoorMonitor.ValueWhenOpen": "Input Open", "DoorMonitor.ValueWhenClosed": "Input Ground"},
}
DOOR_MONITOR_OFF = {
    "DoorMonitor.Type": "None",
    "DoorMonitor.ValueWhenOpen": "None",
    "DoorMonitor.ValueWhenClosed": "None",
}
DOOR_MONITOR_USAGES = ("DoorOpenMonitor", "DoorClosedMonitor")
# Ruoli che il controller gestisce come richiesta di apertura (REX): un punto di identificazione senza lettore
# con l'ingresso assegnato e un punto di accesso in uscita con il profilo REXOnly di fabbrica
REX_ROLES = {"rex", "intercom"}
INPUT_SETTINGS = ("name", "role", "door_token", "contact", "supervised", "schedule")
# Il permesso di aprire senza tessera viene da un profilo di accesso con il modo REXOnly e una regola per il punto
# di accesso del pulsante (esempio Axis con REX): senza, il controller nega con InvalidAccessProfile
REX_AUTHENTICATION = "REXOnly"
REX_PROFILE_NAME = "Uscite e Citofono"
REX_PROFILE_DESCRIPTION = "Pulsanti di uscita e relè del citofono (Axis Access Manager)"
ALWAYS = "standard_always"


def _plain_mode(io: str) -> str:
    """Modo dell'ingresso senza linea supervisionata: gli AUX sono I/O configurabili, gli altri solo ingressi."""
    return "inout:pu/gnd" if io.startswith("IO") else "in:pu/gnd"


def parse_duration(value: str | None) -> float | None:
    """Durata xs:duration (PT10S, PT1M30S) in secondi; None se assente o non riconosciuta."""
    if not value:
        return None
    match = DURATION_PATTERN.match(str(value).strip())
    if not match:
        return None
    hours, minutes, seconds = (float(x) if x else 0.0 for x in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return int(total) if total == int(total) else total


def format_duration(seconds: float) -> str:
    return f"PT{int(seconds)}S" if seconds == int(seconds) else f"PT{seconds}S"


def _seconds(value: str | None) -> str:
    """Durata del controller in secondi da mostrare (7, 0,5); "—" se non si legge."""
    seconds = parse_duration(value)
    return "—" if seconds is None else f"{seconds:g}".replace(".", ",")


def _configuration(items: list[dict], token: str) -> list[dict]:
    for item in items:
        if item.get("token") == token:
            return [
                {"name": str(c.get("Name", "")), "value": str(c.get("Value", ""))}
                for c in item.get("Configuration") or []
            ]
    return []


def _terminals(pins: list[dict]) -> list[str]:
    """Morsetti come li stampa il controller ("Door 1:3", "Door 1:4") raccolti per connettore: "Door 1:3+4"."""
    grouped: dict[str, list[str]] = {}
    for pin in pins:
        connector, _, number = str(pin.get("Name", "")).rpartition(":")
        grouped.setdefault(connector, []).append(number)
    return [f"{name}:{'+'.join(numbers)}" if name else "+".join(numbers) for name, numbers in grouped.items()]


def door_monitor_of(io: str, assigned: list[dict], door_configs: list[dict]) -> dict | None:
    """Sensore Porta impostato sul controller senza l'app (a mano o da un ripristino): porta e tipo di contatto."""
    users = next((a.get("IoUser") or [] for a in assigned if a.get("IoName") == io), [])
    door = next((u.get("token") for u in users if u.get("Usage") == DOOR_MONITOR_USAGES[0]), "")
    if not door:
        return None
    config = {c.get("Name"): c.get("Value") for c in _door_config(door_configs, door).get("Configuration") or []}
    contact = next((k for k, v in DOOR_MONITOR_VALUES.items() if all(config.get(n) == x for n, x in v.items())), "no")
    return {"role": "door_monitor", "door_token": door, "contact": contact, "supervised": False}


def _door_config(configs: list[dict], door: str) -> dict:
    return next((c for c in configs if c.get("token") == door), {})


def input_problem(
    io: str,
    row: dict,
    points: list[dict] | None = None,
    idpoints: set[str] | None = None,
    assigned: list[dict] | None = None,
) -> str:
    """Perché un ingresso salvato nell'app non è così sul controller, "" se è a posto o non si sa (dati non letti):
    una scrittura rimasta a metà o oggetti tolti fuori dall'app. Si ripara scrivendo di nuovo l'ingresso."""
    role = row.get("role") or ""
    point, idpoint = row.get("access_point_token") or "", row.get("idpoint_token") or ""
    if role in REX_ROLES:
        found = next((p for p in points or [] if p.get("token") == point), None)
        if not point or (points is not None and found is None):
            return "manca il suo punto di accesso sul controller"
        if found is not None and found.get("Entity") != row.get("door_token"):
            return "sul controller apre un'altra porta"
        if not idpoint or (idpoints is not None and idpoint not in idpoints):
            return "manca il suo punto di identificazione sul controller"
    elif role == "door_monitor":
        users = next((a.get("IoUser") or [] for a in assigned or [] if a.get("IoName") == io), [])
        if assigned is not None and not any(
            u.get("Usage") == DOOR_MONITOR_USAGES[0] and u.get("token") == row.get("door_token") for u in users
        ):
            return "sul controller non è collegato alla porta"
    elif point or idpoint:
        return "la programmazione di prima non è stata tolta del tutto dal controller"
    return ""


def _inputs(
    info: list[dict],
    assigned: list[dict],
    saved: dict[str, dict] | None = None,
    door_configs: list[dict] | None = None,
    problems: dict[str, str] | None = None,
) -> list[dict]:
    """Ingressi fisici del controller: morsetti, supervisione, a chi sono assegnati ora e cosa ne fa l'app."""
    saved = saved or {}
    users = {a.get("IoName"): a.get("IoUser") or [] for a in assigned}
    found = {i.get("IoName"): i for i in info}
    result = []
    for io, (name, port) in CONTROLLER_INPUTS.items():
        item = found.get(io)
        if item is None:
            continue
        mine = saved.get(io, {})
        if not mine.get("role"):
            mine = {**mine, **(door_monitor_of(io, assigned, door_configs or []) or {})}
        result.append(
            {
                "io": io,
                "name": mine.get("name") or name,
                "default_name": name,
                "role": mine.get("role", ""),
                "door": mine.get("door_token", ""),
                "contact": mine.get("contact", "no"),
                "supervised": bool(mine.get("supervised")),
                # Anche il ruolo trovato sul controller (contatto porta messo a mano su un AUX), per mostrarlo
                "roles": [r for r in INPUT_ROLES if r in ALLOWED_ROLES.get(io, {""}) or r == mine.get("role")],
                "pins": _terminals(item.get("PinInfo") or []),
                "can_supervise": "supervised" in (item.get("IoMode") or []),
                "port": port,
                "assigned": [
                    {"type": u.get("Type", ""), "token": u.get("token", ""), "usage": u.get("Usage", "")}
                    for u in users.get(io, [])
                ],
                # Ingresso da scrivere di nuovo: sul controller non è com'è salvato nell'app
                "problem": (problems or {}).get(io, ""),
            }
        )
    return result


def _attribute(item: dict, name: str) -> str:
    for attribute in item.get("Attribute") or []:
        if attribute.get("Name") == name:
            return str(attribute.get("Value") or "")
    return ""


def unlinked_points(points: list[dict], controllers: list[dict]) -> list[dict]:
    """Punti di accesso che nessun controllo accessi (AccessController) gestisce: lì tessere e ingressi non aprono,
    il controller legge la tessera ma non decide niente."""
    listed = {t for c in controllers for t in c.get("AccessPoint") or []}
    return [p for p in points if p.get("token") and p["token"] not in listed]


async def link_access_points(controller, add=(), remove=()) -> int:
    """Mette nel controllo accessi del controller i punti di accesso in add che nessuno gestisce ancora e toglie da
    tutti quelli in remove. Restituisce quanti controlli accessi ha riscritto."""
    controllers = await controller.list_access_controllers()
    listed = {t for c in controllers for t in c.get("AccessPoint") or []}
    missing = [t for t in dict.fromkeys(add) if t and t not in listed]
    target = await _own_access_controller(controller, controllers) if missing else None
    written = 0
    for item in controllers:
        before = list(item.get("AccessPoint") or [])
        after = [t for t in before if t not in remove] + (missing if item is target else [])
        if after != before:
            await controller.set_access_controller({**item, "AccessPoint": after})
            written += 1
    return written


def is_rex_profile(profile: dict) -> bool:
    """Profilo per le aperture senza tessera (pulsanti, relè): non è un gruppo di persone."""
    return REX_AUTHENTICATION in (profile.get("AuthenticationProfile") or [])


def rex_policies(rows) -> dict[str, str]:
    """Punto di accesso → fascia oraria per ogni ingresso che apre la porta. Senza fascia scelta: sempre."""
    return {
        r["access_point_token"]: r.get("schedule") or ALWAYS
        for r in rows
        if r.get("role") in REX_ROLES and r.get("access_point_token")
    }


def rex_uncovered(profiles: list[dict], points: list[str]) -> list[str]:
    """Punti di accesso di pulsanti e relè su cui nessun profilo per le aperture senza tessera dà il permesso."""
    covered = {
        policy.get("AccessPoint")
        for profile in profiles
        if is_rex_profile(profile) and profile.get("Enabled") is not False
        for policy in profile.get("AccessPolicy") or []
    }
    return [t for t in points if t not in covered]


def _own_rex_profile(profiles: list[dict]) -> dict | None:
    """Il profilo dei pulsanti scritto dall'app: dalla descrizione, o dal nome se l'hanno cambiata dal controller."""
    rex = [p for p in profiles if is_rex_profile(p)]
    return next((p for p in rex if p.get("Description") == REX_PROFILE_DESCRIPTION), None) or next(
        (p for p in rex if p.get("Name") == REX_PROFILE_NAME), None
    )


def _rex_sig(profile: dict) -> tuple:
    return (
        profile.get("Name"),
        profile.get("Description"),
        profile.get("Enabled") is not False,
        tuple(profile.get("Schedule") or []),
        tuple(profile.get("AuthenticationProfile") or []),
        tuple(
            sorted((p.get("AccessPoint"), tuple(p.get("Schedule") or [])) for p in profile.get("AccessPolicy") or [])
        ),
    )


async def sync_rex_profile(controller, wanted: dict[str, str], drop: set[str] | frozenset[str] = frozenset()) -> str:
    """Porta il profilo dei pulsanti alle regole volute (punto di accesso → fascia), solo sui punti che ci sono.

    Le altre regole del profilo restano finché il loro punto di accesso c'è e non è in drop: un pulsante che l'app
    non conosce più (controller rimosso e riaggiunto, reinstallazione) non perde il permesso di aprire. Senza più
    regole il profilo si toglie. Restituisce "creato", "aggiornato" o "tolto", oppure "" se era già a posto."""
    existing = {p.get("token") for p in await controller.list_access_points()}
    wanted = {t: s for t, s in wanted.items() if t in existing}
    mine = _own_rex_profile(await controller.list_access_profiles())
    kept = [
        policy
        for policy in (mine or {}).get("AccessPolicy") or []
        if policy.get("AccessPoint") in existing and policy.get("AccessPoint") not in {*drop, *wanted}
    ]
    if not wanted and not kept:
        if mine is None:
            return ""
        await controller.remove_access_profile(mine["token"])
        return "tolto"
    profile = copy.deepcopy(mine) if mine else {"token": "", "ValidFrom": "", "ValidTo": "", "Attribute": []}
    profile.update(
        {
            "Name": REX_PROFILE_NAME,
            "Description": REX_PROFILE_DESCRIPTION,
            "Enabled": True,
            # L'A1210 valuta la fascia del profilo insieme a quella della regola: decide quella di ogni ingresso
            "Schedule": [ALWAYS],
            "AuthenticationProfile": [REX_AUTHENTICATION],
            "AccessPolicy": kept
            + [
                {"AccessPoint": point, "AuthorizationProfile": [], "Attribute": [], "Schedule": [schedule]}
                for point, schedule in wanted.items()
            ],
        }
    )
    if mine is not None and _rex_sig(mine) == _rex_sig(profile):
        return ""
    await controller.set_access_profile(profile)
    return "aggiornato" if mine else "creato"


async def _own_access_controller(controller, controllers: list[dict]) -> dict:
    """Il controllo accessi di questo controller: quello di fabbrica ha il numero di serie nel token."""
    if len(controllers) == 1:
        return controllers[0]
    serial = str((await controller.device_info()).get("SerialNumber") or "").lower()
    mine = [c for c in controllers if serial and serial in str(c.get("token") or "").lower()]
    if len(mine) != 1:
        raise VapixError("non trovo il controllo accessi (AccessController) di questo controller")
    return mine[0]


class ProgrammingService:
    def __init__(self, db: Database, controller: A1210Client, controller_id: int | None, lock) -> None:
        self.db = db
        self.controller = controller
        self.controller_id = controller_id
        self.lock = lock

    def _check(self) -> int:
        if not self.controller_id:
            raise ServiceError("configura prima il controller in Impianti")
        return self.controller_id

    async def read(self) -> dict:
        """Porte, lettori e punti di accesso così come sono programmati sul controller."""
        self._check()
        errors: list[str] = []
        failed: set[str] = set()
        offline = False

        async def safe(label: str, call, default=None):
            nonlocal offline
            if not offline:
                try:
                    return await call()
                except ControllerUnreachable as err:
                    # Le altre letture aspetterebbero lo stesso tempo: lo storico e il resto arrivano subito
                    offline = True
                    errors.append(str(err))
                except VapixError as err:
                    errors.append(f"{label}: {err}")
            failed.add(label)
            return default if default is not None else []

        doors = await safe("porte", self.controller.list_doors)
        if not doors:
            # Firmware che non permette GetDoorList: almeno nomi e token
            doors = await safe("porte", self.controller.door_info_list)
        door_configs = await safe("configurazione porte", self.controller.list_door_configurations)
        readers = await safe("lettori", self.controller.list_idpoints)
        reader_configs = await safe("configurazione lettori", self.controller.list_idpoint_configurations)
        points = await safe("punti di accesso", self.controller.list_access_points)
        access_controllers = await safe("controllo accessi", self.controller.list_access_controllers)
        unmanaged = unlinked_points(points, access_controllers) if access_controllers else []
        # Come il controllo di stato: un punto di accesso disattivato non apre comunque
        unmanaged = [p for p in unmanaged if p.get("Enabled") is not False]
        rex_missing = [] if offline else await self._rex_missing(points, errors)
        io_info, io_assigned = await safe("ingressi", self.controller.list_io, ([], []))
        saved = self.db.controller_inputs(self.controller_id)
        problems = {
            io: input_problem(
                io,
                row,
                None if "punti di accesso" in failed else points,
                None if "lettori" in failed else {r.get("token") for r in readers},
                None if "ingressi" in failed else io_assigned,
            )
            for io, row in saved.items()
        }
        owned_points, owned_idpoints = self._input_tokens()
        readers = [r for r in readers if r.get("token") not in owned_idpoints]
        points = [p for p in points if p.get("token") not in owned_points]
        reader_names = {r.get("token"): r.get("Name") or r.get("token") for r in readers}
        door_names = {d.get("token"): d.get("Name") or d.get("token") for d in doors}
        return {
            "doors": [
                {
                    "token": d.get("token", ""),
                    "name": d.get("Name") or "",
                    "description": d.get("Description") or "",
                    "editable": "AccessTime" in d,
                    **{key: parse_duration(d.get(field)) for field, (key, *_) in DOOR_TIMES.items()},
                    "configuration": _configuration(door_configs, d.get("token", "")),
                }
                for d in doors
            ],
            "readers": [
                {
                    "token": r.get("token", ""),
                    "name": r.get("Name") or "",
                    "description": r.get("Description") or "",
                    "location": r.get("Location") or "",
                    "timeout": parse_duration(r.get("Timeout")),
                    "configuration": _configuration(reader_configs, r.get("token", "")),
                }
                for r in readers
            ],
            "access_points": [
                {
                    "token": a.get("token", ""),
                    "name": a.get("Name") or "",
                    "description": a.get("Description") or "",
                    "door": a.get("Entity") or "",
                    "door_name": door_names.get(a.get("Entity"), a.get("Entity") or ""),
                    "readers": [i.get("IdPoint") for i in a.get("IdPointDevice") or [] if i.get("IdPoint")],
                    "reader_names": [
                        reader_names.get(i.get("IdPoint"), i.get("IdPoint"))
                        for i in a.get("IdPointDevice") or []
                        if i.get("IdPoint")
                    ],
                    "direction": _attribute(a, "Direction"),
                    "enabled": a.get("Enabled", True) is not False,
                }
                for a in points
            ],
            # Punti di accesso per cui il controller non decide (dopo un reset): tessere e pulsanti non aprono
            "unmanaged": [{"token": a["token"], "name": a.get("Name") or a["token"]} for a in unmanaged],
            # Pulsanti e relè senza il permesso di aprire: il controller li nega (InvalidAccessProfile)
            "rex_missing": rex_missing,
            "inputs": _inputs(io_info, io_assigned, saved, door_configs, problems),
            "input_roles": INPUT_ROLES,
            "contacts": CONTACTS,
            "directions": DIRECTIONS,
            "changes": self.db.list_controller_changes(self.controller_id),
            # Stessa lettura tentata due volte (porte) con lo stesso errore: una riga sola
            "errors": list(dict.fromkeys(errors)),
        }

    async def _rex_missing(self, points: list[dict], errors: list[str]) -> list[dict]:
        """Ingressi che aprono la porta ma su cui nessun profilo dà il permesso."""
        rows = self.db.controller_inputs(self.controller_id)
        existing = {p.get("token") for p in points}
        wanted = [t for t in rex_policies(rows.values()) if t in existing]
        if not wanted:
            return []
        try:
            missing = set(rex_uncovered(await self.controller.list_access_profiles(), wanted))
        except VapixError as err:
            errors.append(f"profili di accesso: {err}")
            return []
        return [
            {"io": io, "name": rows[io]["name"]}
            for io in CONTROLLER_INPUTS
            if io in rows and rows[io]["access_point_token"] in missing
        ]

    async def _sync_rex(self, io: str, row: dict, drop: set[str] | frozenset[str] = frozenset()) -> str:
        """Profilo dei pulsanti con l'ingresso io nello stato row (non ancora salvato) e gli altri come sono;
        drop sono i punti di accesso dell'ingresso che stanno per essere tolti."""
        rows = {**self.db.controller_inputs(self._check()), io: row}
        return await sync_rex_profile(self.controller, rex_policies(rows.values()), drop)

    async def repair_rex_profile(self, user: str = "sistema") -> int:
        """Ingressi programmati prima della 0.21.3, o profilo tolto dal sito del controller: rimette il permesso
        di aprire. Non toglie mai il permesso a un pulsante che l'app non conosce. Restituisce quanti ingressi ne
        erano senza."""
        if not self.controller_id:
            return 0
        async with self.lock:
            rows = self.db.controller_inputs(self.controller_id)
            missing = {i["io"] for i in await self._rex_missing(await self.controller.list_access_points(), [])}
            done = await sync_rex_profile(self.controller, rex_policies(rows.values()))
            if done and missing:
                names = ", ".join(rows[io]["name"] for io in CONTROLLER_INPUTS if io in missing)
                self.db.add_audit(user, "programmazione controller", f"permesso di apertura senza tessera: {names}")
            elif done == "tolto":
                self.db.add_audit(
                    user,
                    "programmazione controller",
                    f"profilo {REX_PROFILE_NAME} tolto: i suoi punti di accesso non ci sono più",
                )
        return len(missing) if done else 0

    async def _save(self, kind: str, token: str, before: dict, after: dict, summary: str, user: str) -> None:
        if before == after:
            return
        setter = {"door": self.controller.set_door, "reader": self.controller.set_idpoint}.get(
            kind, self.controller.set_access_point
        )
        await setter(after)
        self.db.add_controller_change(user, self.controller_id, kind, token, summary, before, after)
        self.db.add_audit(user, "programmazione controller", summary)

    def _input_tokens(self) -> tuple[set[str], set[str]]:
        """Punti di accesso e punti di identificazione creati per gli ingressi di questo controller."""
        rows = self.db.controller_inputs(self.controller_id).values() if self.controller_id else []
        rows = list(rows)
        return (
            {r["access_point_token"] for r in rows if r["access_point_token"]},
            {r["idpoint_token"] for r in rows if r["idpoint_token"]},
        )

    async def _device_uuids(self, points: list[dict]) -> tuple[dict[str, str], str]:
        """Dispositivo (DeviceUUID) di ogni lettore: dalla configurazione del lettore, dai punti di accesso
        o, se il lettore ora è scollegato da tutto, dallo storico delle modifiche.
        Il secondo valore è per i lettori senza nessuna di queste fonti: il dispositivo della porta.
        """
        try:
            configs = await self.controller.list_idpoint_configurations()
        except VapixError:
            configs = []
        objects = [{"IdPointDevice": [{"IdPoint": c.get("token"), "DeviceUUID": c.get("DeviceUUID")}]} for c in configs]
        objects += points + self.db.list_controller_change_objects(self._check(), "access_point")
        known: dict[str, str] = {}
        for item in objects:
            for device in item.get("IdPointDevice") or []:
                if device.get("IdPoint") and device.get("DeviceUUID"):
                    known.setdefault(device["IdPoint"], device["DeviceUUID"])
        # Lettori e porte sono del controller stesso: stesso dispositivo
        doors = (p.get("DoorDeviceUUID") for p in points)
        return known, next(iter(known.values()), "") or next((d for d in doors if d), "")

    @staticmethod
    def _devices(readers: list[str], current: list[dict], known: dict[str, str], fallback: str) -> list[dict]:
        """Voci IdPointDevice per i lettori scelti; chi non ha il dispositivo lo riprende da known."""
        by_reader = {d.get("IdPoint"): d for d in current}
        return [
            by_reader[r]
            if (by_reader.get(r) or {}).get("DeviceUUID")
            else {"IdPoint": r, "DeviceUUID": known.get(r) or fallback}
            for r in readers
        ]

    async def update_door(self, token: str, data: dict, user: str) -> None:
        self._check()
        async with self.lock:
            found = await self.controller.get_doors([token])
            if not found:
                raise ServiceError("porta non trovata sul controller")
            before = found[0]
            after = copy.deepcopy(before)
            after["Name"] = normalize_name(data.get("name", before.get("Name", "")))[:64]
            after["Description"] = normalize_text(
                data.get("description", before.get("Description")), 1024, "descrizione"
            )
            for field, (key, label, low, high) in DOOR_TIMES.items():
                if data.get(key) is None or field not in before:
                    continue
                try:
                    seconds = float(data[key])
                except (TypeError, ValueError) as err:
                    raise ServiceError(f"tempo non valido: {data[key]}") from err
                if seconds == parse_duration(before[field]):
                    continue  # invariato: un valore già sul controller oltre i limiti non blocca le altre modifiche
                if not low <= seconds <= high:
                    raise ServiceError(f"{label}: tra {low} e {high} secondi")
                after[field] = format_duration(seconds)
            # Nello storico si vede cosa è cambiato: due modifiche della stessa porta non hanno lo stesso riassunto
            changed = [f"rinominata da {before.get('Name') or '—'}"] if after["Name"] != before.get("Name") else []
            if after["Description"] != (before.get("Description") or ""):
                changed.append("descrizione")
            changed += [
                f"{label} {_seconds(before[field])} → {_seconds(after[field])} s"
                for field, (_, label, *_) in DOOR_TIMES.items()
                if field in before and after[field] != before[field]
            ]
            summary = f"porta {after['Name']}" + (f": {', '.join(changed)}" if changed else "")
            await self._save("door", token, before, after, summary, user)

    async def update_reader(self, token: str, data: dict, user: str) -> None:
        self._check()
        async with self.lock:
            found = await self.controller.get_idpoints([token])
            if not found:
                raise ServiceError("lettore non trovato sul controller")
            before = found[0]
            after = copy.deepcopy(before)
            after["Name"] = normalize_name(data.get("name", before.get("Name", "")))[:64]
            after["Description"] = normalize_text(
                data.get("description", before.get("Description")), 1024, "descrizione"
            )
            await self._save("reader", token, before, after, f"lettore {after['Name']}", user)

    async def update_input(self, io: str, data: dict, user: str) -> dict:
        """Nome, ruolo, porta e cablaggio di un ingresso fisico. Restituisce i valori salvati."""
        controller_id = self._check()
        if io not in CONTROLLER_INPUTS:
            raise ServiceError("ingresso non trovato")
        role = data.get("role") or ""
        if role not in INPUT_ROLES:
            raise ServiceError("ruolo non valido")
        if role not in ALLOWED_ROLES[io]:
            raise ServiceError(f"{INPUT_ROLES[role]}: non disponibile su questo ingresso")
        door = (data.get("door") or "") if role in DOOR_ROLES else ""
        if role in DOOR_ROLES:
            if not door:
                raise ServiceError(f"{INPUT_ROLES[role]}: scegli la porta")
            if door not in {d.get("token") for d in await self.controller.door_info_list()}:
                raise ServiceError("porta non trovata sul controller")
        contact = data.get("contact") or "no"
        if contact not in CONTACTS:
            raise ServiceError("tipo di contatto non valido")
        supervised = bool(data.get("supervised"))
        if role == "door_monitor" and supervised:
            raise ServiceError("Sensore Porta: la linea supervisionata non è ancora disponibile")
        if role == "alarm" and supervised:
            raise ServiceError("Ingresso di Allarme: la linea supervisionata non è disponibile")
        if role not in REX_ROLES and role != "door_monitor":
            # Per Non Usato e Ingresso di Allarme l'app non scrive niente sul controller: contatto e linea
            # supervisionata non avrebbero effetto, e Home Assistant mostra l'ingresso com'è
            contact, supervised = "no", False
        name = " ".join(normalize_text(data.get("name"), 64, "nome").split())
        values = {
            "name": name or (INPUT_ROLES[role] if role else CONTROLLER_INPUTS[io][0]),
            "role": role,
            "door_token": door,
            "contact": contact,
            "supervised": supervised,
            "schedule": "",
        }
        async with self.lock:
            current = self.db.controller_inputs(controller_id).get(io) or {
                "name": CONTROLLER_INPUTS[io][0],
                "role": "",
                "door_token": "",
                "contact": "no",
                "supervised": False,
                "schedule": "",
            }
            if not current.get("role"):
                # Contatto porta impostato a mano sul controller: l'app lo riconosce e lo può togliere, come la pagina
                current = {**current, **(await self._door_monitor_on_controller(io) or {})}
            before = {k: current.get(k) for k in INPUT_SETTINGS}
            # Stesse impostazioni: si riscrive solo se sul controller l'ingresso non è com'è salvato nell'app
            if before == values and not await self._input_problem(io, current):
                return values
            await self._apply_input(controller_id, io, current, values)
            default = CONTROLLER_INPUTS[io][0]
            label = default if values["name"] in (default, *INPUT_ROLES.values()) else f"{values['name']} ({default})"
            summary = f"{label}: {INPUT_ROLES[role]}"
            self.db.add_controller_change(user, controller_id, "input", io, summary, before, values)
            self.db.add_audit(user, "programmazione controller", summary)
        return values

    async def _input_problem(self, io: str, row: dict) -> str:
        """Come input_problem, con i dati letti ora dal controller (solo quelli che servono al ruolo)."""
        try:
            if row.get("role") in REX_ROLES:
                points = await self.controller.list_access_points()
                return input_problem(io, row, points, {i.get("token") for i in await self.controller.list_idpoints()})
            if row.get("role") == "door_monitor":
                return input_problem(io, row, assigned=(await self.controller.list_io())[1])
        except VapixError:
            pass
        return input_problem(io, row)

    async def _apply_input(self, controller_id: int, io: str, current: dict, values: dict) -> None:
        """Porta il controller allo stato chiesto per l'ingresso e salva le impostazioni con i token creati.
        Se il controller rifiuta a metà, i token già creati restano salvati: il prossimo salvataggio li riusa."""
        tokens = {
            "idpoint_token": current.get("idpoint_token", ""),
            "access_point_token": current.get("access_point_token", ""),
        }
        wanted = values["role"] in REX_ROLES
        try:
            monitor = current.get("role") == "door_monitor"
            if monitor and (values["role"] != "door_monitor" or values["door_token"] != current.get("door_token")):
                await self._clear_door_monitor(io, current.get("door_token") or "")
            point = tokens["access_point_token"]
            if point and (not wanted or values["door_token"] != current.get("door_token")):
                # Prima la regola che lo nomina, poi il punto di accesso
                await self._sync_rex(io, {**current, "access_point_token": ""}, {point})
                await link_access_points(self.controller, remove={point})
                await self.controller.remove_access_point(point)
                tokens["access_point_token"] = ""
            if not wanted:
                if tokens["idpoint_token"]:
                    await self._release_io(io, tokens["idpoint_token"], current)
                    await self.controller.remove_idpoint(tokens["idpoint_token"])
                    tokens["idpoint_token"] = ""
            else:
                await self._write_rex(io, values, tokens)
                await self._sync_rex(io, {**values, **tokens})
            if values["role"] == "door_monitor":
                await self._write_door_monitor(io, values)
        except VapixError as err:
            # Resta il ruolo di prima con i token di adesso: la pagina segnala l'ingresso (input_problem) e un nuovo
            # salvataggio, anche con le stesse impostazioni, completa il lavoro
            self.db.save_controller_input(controller_id, io, {**current, **tokens})
            raise ServiceError(f"configurazione dell'ingresso non completata: {err}") from err
        self.db.save_controller_input(controller_id, io, {**values, **tokens})

    async def _door_monitor_on_controller(self, io: str) -> dict | None:
        try:
            _, assigned = await self.controller.list_io()
            return door_monitor_of(io, assigned, await self.controller.list_door_configurations())
        except VapixError:
            return None

    async def _set_door_monitor(self, door: str, changes: dict[str, str], required: bool = True) -> None:
        """Cambia solo le voci DoorMonitor della configurazione della porta; il resto (serratura, relè) resta.
        Con required False una porta che sul controller non c'è più non è un errore: non c'è niente da cambiare."""
        current = _door_config(await self.controller.list_door_configurations(), door)
        if not current:
            if not required:
                return
            raise VapixError("configurazione della porta non trovata sul controller")
        items = [dict(c) for c in current.get("Configuration") or []]
        for item in items:
            if item.get("Name") in changes:
                item["Value"] = changes[item["Name"]]
        names = {item.get("Name") for item in items}
        items += [{"Name": k, "Value": v} for k, v in changes.items() if k not in names]
        await self.controller.set_door_configuration(
            {"token": door, "DeviceUUID": current.get("DeviceUUID") or "", "Configuration": items}
        )

    async def _write_door_monitor(self, io: str, values: dict) -> None:
        """Contatto porta come al banco: DoorMonitor Standard sulla porta e l'ingresso ai due controlli della porta."""
        door = values["door_token"]
        await self._set_door_monitor(door, {"DoorMonitor.Type": "Standard", **DOOR_MONITOR_VALUES[values["contact"]]})
        users = [
            {"Type": "doorcontrol", "token": door, "Usage": usage, "MultiIo": False} for usage in DOOR_MONITOR_USAGES
        ]
        await self.controller.set_io_assignment(io, "in:pu/gnd", users)

    async def _clear_door_monitor(self, io: str, door: str) -> None:
        if door:
            await self._set_door_monitor(door, DOOR_MONITOR_OFF, required=False)
        await self.controller.set_io_assignment(io, "in:pu/gnd", [])

    async def _release_io(self, io: str, idpoint: str, current: dict) -> None:
        """Libera l'I/O tornando al modo semplice. Senza utenti il firmware accetta il cambio di modo ma lo ignora:
        da supervisionato si passa prima al modo semplice con l'IdPoint come REX normale, poi si toglie l'utente."""
        if current.get("supervised"):
            await self.controller.set_idpoint_configuration(
                idpoint,
                "",
                {"IdPoint.Reader.Type": "None", "IdPoint.REX.Type": "ActiveLow", "IdPoint.Keypress.Type": "Auto"},
            )
            user = {"Type": "idpoint", "token": idpoint, "Usage": "REX", "MultiIo": False}
            await self.controller.set_io_assignment(io, _plain_mode(io), [user])
        await self.controller.set_io_assignment(io, _plain_mode(io), [])

    async def _write_rex(self, io: str, values: dict, tokens: dict) -> None:
        supervised = values["supervised"]
        description = f"Ingresso {io} (Axis Access Manager)"
        idpoints = await self.controller.list_idpoints()
        if tokens["idpoint_token"] not in {i.get("token") for i in idpoints}:
            # Token che sul controller non c'è più (reset, ripristino): si crea un punto nuovo. Senza token si
            # riprende quello che l'app aveva scritto per questo ingresso (controller rimosso e riaggiunto)
            tokens["idpoint_token"] = next(
                (i.get("token") or "" for i in idpoints if i.get("Description") == description), ""
            )
        idpoint = await self.controller.create_idpoint(
            {
                **({"token": tokens["idpoint_token"]} if tokens["idpoint_token"] else {}),
                "Name": values["name"][:64],
                "Description": description,
                "Location": "",
                "Action": "Access",
                "Timeout": "PT10S",
                "MinPINSize": 4,
                "MaxPINSize": 4,
                "EndOfPIN": "#",
            }
        )
        tokens["idpoint_token"] = idpoint
        points = await self.controller.list_access_points()
        _, device = await self._device_uuids(points)
        level = "High" if values["contact"] == "nc" else "Low"
        # Senza dispositivo: il controller ci mette il suo e lega la configurazione a sé (come dice VAPIX)
        await self.controller.set_idpoint_configuration(
            idpoint,
            "",
            {
                "IdPoint.Reader.Type": "None",
                "IdPoint.REX.Type": f"{'Supervised' if supervised else ''}Active{level}",
                "IdPoint.Keypress.Type": "Auto",
            },
        )
        await self.controller.set_io_assignment(
            io,
            "supervised" if supervised else _plain_mode(io),
            [
                {
                    "Type": "idpoint",
                    "token": idpoint,
                    "Usage": "REXSupervised" if supervised else "REX",
                    "MultiIo": False,
                }
            ],
        )
        door = values["door_token"]
        existing = next((p for p in points if p.get("token") == tokens["access_point_token"]), None) or next(
            (p for p in points if p.get("Description") == description), None
        )
        # Un punto di accesso già sulla porta fa da modello per i campi che il controller si aspetta
        template = existing or next((p for p in points if p.get("Entity") == door), {})
        point = copy.deepcopy(template)
        if existing is None:
            point.pop("token", None)
        attributes = [a for a in point.get("Attribute") or [] if a.get("Name") != "Direction"]
        point.update(
            {
                "Name": values["name"][:64],
                "Description": description,
                "Entity": door,
                "EntityType": "axtdc:Door",
                "Enabled": True,
                "IdPointDevice": [{"IdPoint": idpoint, "DeviceUUID": device}],
                "AuthenticationProfile": ["REXOnly"],
                "Attribute": [*attributes, {"Name": "Direction", "Value": "out"}],
            }
        )
        if not point.get("DoorDeviceUUID"):
            point["DoorDeviceUUID"] = device
        tokens["access_point_token"] = await self.controller.create_access_point(point)
        # Senza questo il controller vede l'ingresso ma non decide niente
        await link_access_points(self.controller, add=[tokens["access_point_token"]])

    async def manage_access_points(self, user: str) -> int:
        """Fa gestire al controller tutti i punti di accesso che non gestisce. Restituisce quanti erano."""
        self._check()
        async with self.lock:
            points = await self.controller.list_access_points()
            missing = unlinked_points(points, await self.controller.list_access_controllers())
            if missing:
                await link_access_points(self.controller, add=[p["token"] for p in missing])
                names = ", ".join(p.get("Name") or p["token"] for p in missing)
                self.db.add_audit(
                    user, "programmazione controller", f"punti di accesso gestiti dal controller: {names}"
                )
            return len(missing)

    async def update_access_point(self, token: str, data: dict, user: str) -> None:
        """Nome, porta comandata, lettori collegati, direzione e attivazione di un punto di accesso."""
        self._check()
        async with self.lock:
            found = await self.controller.get_access_points([token])
            if not found:
                raise ServiceError("punto di accesso non trovato sul controller")
            before = found[0]
            after = copy.deepcopy(before)
            if "name" in data:
                after["Name"] = normalize_name(data["name"])[:64]
            if "enabled" in data:
                after["Enabled"] = bool(data["enabled"])
            if data.get("door"):
                doors = {d.get("token") for d in await self.controller.door_info_list()}
                if data["door"] not in doors:
                    raise ServiceError("porta non trovata sul controller")
                after["Entity"] = data["door"]
                after["EntityType"] = before.get("EntityType") or "axtdc:Door"
            if "readers" in data:
                readers = {r.get("token") for r in await self.controller.list_idpoints()}
                chosen = [r for r in data["readers"] if r]
                if any(r not in readers for r in chosen):
                    raise ServiceError("lettore non trovato sul controller")
                known, fallback = await self._device_uuids(await self.controller.list_access_points())
                after["IdPointDevice"] = self._devices(chosen, before.get("IdPointDevice") or [], known, fallback)
            if "direction" in data:
                direction = data["direction"] or ""
                if direction and direction not in DIRECTIONS:
                    raise ServiceError("direzione non valida")
                attributes = [a for a in after.get("Attribute") or [] if a.get("Name") != "Direction"]
                if direction:
                    attributes.append({"Name": "Direction", "Value": direction})
                after["Attribute"] = attributes
            await self._save(
                "access_point", token, before, after, f"punto di accesso {after.get('Name') or token}", user
            )

    async def set_links(self, links: list[dict], user: str) -> int:
        """Collegamenti lettore → porta dallo schema del controller, salvati tutti insieme.

        Sull'Axis un collegamento è un lettore dentro un punto di accesso esistente della porta, con la sua
        direzione: qui si spostano solo i lettori tra i punti di accesso, senza crearne o eliminarne.
        Restituisce quanti punti di accesso sono cambiati.
        """
        self._check()
        async with self.lock:
            points = await self.controller.list_access_points()
            reader_names = {
                r.get("token"): r.get("Name") or r.get("token") for r in await self.controller.list_idpoints()
            }
            readers = set(reader_names)
            doors = {d.get("token"): d.get("Name") or d.get("token") for d in await self.controller.door_info_list()}
            # I punti degli ingressi (pulsante, relè del citofono) sono gestiti dalle loro schede, non dallo schema
            owned_points, owned_idpoints = self._input_tokens()
            readers -= owned_idpoints
            # Collegamenti già sul controller verso una porta o un lettore che non c'è più (tolti fuori dall'app):
            # lo schema li rimanda com'erano, restano così e non bloccano gli altri
            dangling = {
                (device.get("IdPoint"), p.get("Entity"))
                for p in points
                if p.get("token") not in owned_points
                for device in p.get("IdPointDevice") or []
                if p.get("Entity") not in doors or device.get("IdPoint") not in readers
            }
            points = [p for p in points if p.get("Entity") in doors and p.get("token") not in owned_points]
            known, fallback = await self._device_uuids(points)
            wanted: dict[str, list[str]] = {p["token"]: [] for p in points}
            enabled: dict[str, bool] = {}
            for link in links:
                reader, door = link.get("reader") or "", link.get("door") or ""
                direction = link.get("direction") or ""
                if (reader, door) in dangling:
                    for point in points:
                        on_point = [i.get("IdPoint") for i in point.get("IdPointDevice") or []]
                        if point.get("Entity") == door and reader in on_point and reader not in wanted[point["token"]]:
                            wanted[point["token"]].append(reader)
                    continue
                if reader not in readers:
                    raise ServiceError(f"lettore non trovato sul controller: {reader}")
                if door not in doors:
                    raise ServiceError(f"porta non trovata sul controller: {door}")
                if direction and direction not in DIRECTIONS:
                    raise ServiceError("direzione non valida")
                choices = [p for p in points if p.get("Entity") == door and _attribute(p, "Direction") == direction]
                if not choices:
                    where = f"in {DIRECTIONS[direction].lower()}" if direction else "senza direzione"
                    raise ServiceError(
                        f"sul controller la porta {doors[door]} non ha un punto di accesso {where}: "
                        "scegli una direzione che la porta ha già"
                    )
                point = next((p for p in choices if reader in wanted[p["token"]]), None) or next(
                    (p for p in choices if any(i.get("IdPoint") == reader for i in p.get("IdPointDevice") or [])),
                    choices[0],
                )
                if reader not in wanted[point["token"]]:
                    wanted[point["token"]].append(reader)
                enabled[point["token"]] = enabled.get(point["token"], True) and link.get("enabled", True) is not False
            updates = []
            for point in points:
                token = point["token"]
                before_readers = [i.get("IdPoint") for i in point.get("IdPointDevice") or []]
                after = copy.deepcopy(point)
                current = point.get("IdPointDevice") or []
                # Stessi lettori: si salva comunque se a uno manca il dispositivo e lo si conosce
                order = before_readers if set(before_readers) == set(wanted[token]) else wanted[token]
                devices = self._devices(order, current, known, fallback)
                if devices != current:
                    after["IdPointDevice"] = devices
                if token in enabled and (point.get("Enabled", True) is not False) != enabled[token]:
                    after["Enabled"] = enabled[token]
                if after != point:
                    gains = bool(set(wanted[token]) - set(before_readers))
                    updates.append((not gains, token, point, after))
            # Prima i punti che ricevono lettori, poi quelli che li perdono: un errore a metà non lascia
            # un lettore scollegato da tutto
            for _, token, before, after in sorted(updates, key=lambda u: u[0]):
                devices = after.get("IdPointDevice") or []
                names = ", ".join(reader_names.get(i.get("IdPoint")) or i.get("IdPoint") for i in devices)
                direction = _attribute(after, "Direction")
                where = f" in {DIRECTIONS[direction].lower()}" if direction in DIRECTIONS else ""
                summary = f"collegamenti di {doors.get(after.get('Entity'))}{where}: {names or 'nessun lettore'}"
                if (before.get("Enabled", True) is not False) != (after.get("Enabled", True) is not False):
                    summary += " · attivati" if after.get("Enabled", True) is not False else " · disattivati"
                await self._save("access_point", token, before, after, summary, user)
            return len(updates)

    async def undo(self, change_id: int, user: str) -> str:
        """Riporta l'oggetto com'era prima della modifica. Restituisce il tipo di oggetto."""
        self._check()
        change = self.db.get_controller_change(change_id)
        if change is None or change["controller_id"] != self.controller_id:
            raise ServiceError("modifica non trovata")
        if change["undone"]:
            raise ServiceError("modifica già annullata")
        # Si riscrive l'oggetto intero com'era: le modifiche fatte dopo sullo stesso oggetto andrebbero perse
        later = [
            c["summary"]
            for c in reversed(self.db.list_controller_changes(self.controller_id, limit=1000))
            if c["id"] > change_id and c["kind"] == change["kind"] and c["token"] == change["token"] and not c["undone"]
        ]
        if later:
            what = "la modifica successiva" if len(later) == 1 else "le modifiche successive"
            raise ServiceError(f"annulla prima {what} dello stesso oggetto: {'; '.join(later)}")
        async with self.lock:
            if change["kind"] == "input":
                current = self.db.controller_inputs(self.controller_id).get(change["token"]) or {}
                await self._apply_input(self.controller_id, change["token"], current, change["before"])
            else:
                setter = {"door": self.controller.set_door, "reader": self.controller.set_idpoint}.get(
                    change["kind"], self.controller.set_access_point
                )
                await setter(change["before"])
            self.db.mark_change_undone(change_id)
            self.db.add_audit(user, "programmazione controller annullata", change["summary"])
        return change["kind"]
