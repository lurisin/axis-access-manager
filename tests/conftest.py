import copy
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.config import parse_options
from app.db import Database
from app.vapix import VapixError


class FakeController:
    """A1210 finto: tiene utenti e tessere in memoria e registra i comandi ricevuti."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.credentials: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.door = "Locked"
        self.door_physical = "Closed"
        self.fail_set_credential = False
        self.fail_io_assignment = False
        self.idpoint_configs: dict[str, dict] = {}
        self.io_assignments: dict[str, dict] = {}
        self.door_configs: dict[str, dict] = {
            "porta": {"token": "porta", "Configuration": [{"Name": "Lock.Type", "Value": "FailSecure"}]}
        }
        self.authentication_profiles: dict[str, dict] = {
            t: {"token": t, "Name": t, "Description": "", "Schedule": ["standard_always"]}
            for t in ("CardOnly", "REXOnly")
        }
        self.time_zone = "Europe/Rome"
        # Dispositivo del controller: lo mette nelle configurazioni scritte senza
        self.device_uuid = "uuid-locale"
        self.mqtt: dict = {}
        self.mqtt_events: dict = {}
        # Come un controller che rifiuta i token generati da un altro (Axis-<serie>:...)
        self.refuse_axis_tokens = False
        self._ids = itertools.count(1)
        self.access_points = [
            {
                "token": "ap-in",
                "Entity": "porta",
                "EntityType": "axtdc:Door",
                "Name": "Ingresso",
                "Enabled": True,
                "IdPointDevice": [{"IdPoint": "lettore-1", "DeviceUUID": ""}],
                "Attribute": [{"Name": "Direction", "Value": "in"}],
            }
        ]
        # Controllo accessi di fabbrica: decide solo per i punti di accesso elencati
        self.access_controllers = {
            "Axis-x AccessController": {
                "token": "Axis-x AccessController",
                "Name": "Axis-x AccessController",
                "Description": "",
                "AccessPoint": ["ap-in"],
            }
        }
        self.doors = {
            "porta": {
                "token": "porta",
                "Name": "Ingresso scala",
                "Description": "",
                "AccessTime": "PT7S",
                "ExtendedAccessTime": "PT30S",
                "OpenTooLongTime": "PT15S",
                "ExtendedOpenTooLongTime": "PT1M",
                "PreAlarmTime": "PT5S",
            },
            "porta2": {"token": "porta2", "Name": "Cantina", "Description": "", "AccessTime": "PT5S"},
        }
        self.idpoints = {
            "lettore-1": {"token": "lettore-1", "Name": "Lettore esterno", "Location": "", "Timeout": "PT3S"},
            "lettore-2": {"token": "lettore-2", "Name": "Lettore interno", "Location": "", "Timeout": "PT3S"},
        }
        self.schedules: dict[str, dict] = {
            "standard_always": {"token": "standard_always", "Name": "", "Description": "", "ScheduleDefinition": ""},
            "standard_office_hours": {"token": "standard_office_hours", "Name": "Office hours", "Description": ""},
        }
        self.profiles: dict[str, dict] = {
            "profilo_sempre": {
                "token": "profilo_sempre",
                "Name": "Sempre",
                "Description": "",
                "Enabled": True,
                # Come il profilo creato a mano sull'A1210: fascia sia nel profilo sia nella regola
                "Schedule": ["standard_always"],
                "AuthenticationProfile": [],
                "AccessPolicy": [
                    {
                        "AccessPoint": "ap-in",
                        "AuthorizationProfile": [],
                        "Attribute": [],
                        "Schedule": ["standard_always"],
                    }
                ],
            }
        }

    async def aclose(self):
        pass

    serial = "X"
    firmware = "10.12.288.1"
    fingerprint = "AA:BB"
    # Secondi di scarto dell'orologio rispetto all'ora vera
    clock_skew = 0
    ntp_synced = True
    offline = False

    async def device_info(self):
        if self.offline:
            raise VapixError("il controller non risponde (tempo scaduto)")
        return {"ProdNbr": "A1210", "Version": self.firmware, "SerialNumber": self.serial}

    async def certificate_fingerprint(self):
        return self.fingerprint

    async def date_time_info(self):
        # Come l'A1210 del banco: dateTime è in UTC
        moment = datetime.now(tz=UTC) + timedelta(seconds=self.clock_skew)
        return {"dateTime": moment.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": self.time_zone, "dstEnabled": True}

    async def set_time_zone(self, time_zone):
        self.calls.append(("set_time_zone", time_zone))
        self.time_zone = time_zone

    async def set_ntp(self, enabled, source, servers):
        self.calls.append(("set_ntp", enabled, source, tuple(servers)))

    async def mqtt_client_config(self):
        return {"config": copy.deepcopy(self.mqtt), "status": {"state": "active"}} if self.mqtt else {}

    async def mqtt_event_config(self):
        return {"eventPublicationConfig": copy.deepcopy(self.mqtt_events)} if self.mqtt_events else {}

    async def set_mqtt_client(self, config):
        self.calls.append(("set_mqtt_client", config.get("password")))
        self.mqtt = copy.deepcopy(config)

    async def set_mqtt_events(self, config):
        self.calls.append(("set_mqtt_events",))
        self.mqtt_events = copy.deepcopy(config)

    def _check_token(self, item):
        if self.refuse_axis_tokens and str(item.get("token") or "").startswith("Axis-"):
            raise VapixError("il controller ha rifiutato la richiesta (valore non accettato)", "ter:InvalidArgVal")

    def factory_reset(self):
        """Come un reset di fabbrica: resta solo quello che il controller ha di serie."""
        self.doors, self.door_configs, self.idpoints, self.idpoint_configs = {}, {}, {}, {}
        self.access_points, self.profiles, self.users, self.credentials = [], {}, {}, {}
        self.schedules = {
            t: {"token": t, "Name": "", "Description": "", "ScheduleDefinition": ""}
            for t in ("standard_always", "standard_after_hours")
        }
        self.io_assignments = {"Ser0hd": {"IoMode": "", "IoUser": []}}
        self.time_zone, self.mqtt, self.mqtt_events = "UTC", {}, {}
        # Il controllo accessi resta, con l'elenco dei punti di accesso vuoto
        token = f"Axis-{self.serial.lower()} AccessController"
        self.access_controllers = {token: {"token": token, "Name": token, "Description": "", "AccessPoint": []}}

    async def ntp_info(self):
        return {
            "client": {
                "enabled": True,
                "serversSource": "static",
                "staticServers": ["it.pool.ntp.org"],
                "advertisedServers": ["172.27.202.1"],
                "synced": self.ntp_synced,
                "timeOffset": 0.36650896072387695,
            }
        }

    log_events: list = []

    async def fetch_events(self, start, limit=1000, row_id=None):
        rows = [
            e
            for e in self.log_events
            if e["UtcTime"] >= start.replace("Z", "") and (row_id is None or e["rowid"] > row_id)
        ]
        return rows[:limit], len(rows) > limit

    async def door_info_list(self):
        return [{"token": t, "Name": d["Name"]} for t, d in self.doors.items()]

    async def list_doors(self):
        return [copy.deepcopy(d) for d in self.doors.values()]

    async def get_doors(self, tokens):
        return [copy.deepcopy(self.doors[t]) for t in tokens if t in self.doors]

    async def set_door(self, door):
        self._check_token(door)
        token = door.get("token") or f"door-{next(self._ids)}"
        self.calls.append(("set_door", token))
        self.doors[token] = {**copy.deepcopy(door), "token": token}
        return token

    async def list_door_configurations(self):
        return [copy.deepcopy(c) for c in self.door_configs.values()]

    async def set_door_configuration(self, configuration):
        self.calls.append(("set_door_configuration", configuration["token"], configuration.get("DeviceUUID")))
        self.door_configs[configuration["token"]] = {
            **copy.deepcopy(configuration),
            "DeviceUUID": configuration.get("DeviceUUID") or self.device_uuid,
        }

    async def remove_door(self, token):
        self.calls.append(("remove_door", token))
        self.doors.pop(token, None)
        self.door_configs.pop(token, None)

    async def list_authentication_profiles(self):
        return [copy.deepcopy(p) for p in self.authentication_profiles.values()]

    async def set_authentication_profile(self, profile):
        self.calls.append(("set_authentication_profile", profile["token"]))
        self.authentication_profiles[profile["token"]] = copy.deepcopy(profile)

    async def list_idpoints(self):
        return [copy.deepcopy(i) for i in self.idpoints.values()]

    async def get_idpoints(self, tokens):
        return [copy.deepcopy(self.idpoints[t]) for t in tokens if t in self.idpoints]

    async def set_idpoint(self, idpoint):
        self.calls.append(("set_idpoint", idpoint["token"]))
        self.idpoints[idpoint["token"]] = copy.deepcopy(idpoint)

    async def list_idpoint_configurations(self):
        raise VapixError("non supportato")

    async def create_idpoint(self, idpoint):
        self._check_token(idpoint)
        token = idpoint.get("token") or f"idpoint-{next(self._ids)}"
        self.calls.append(("create_idpoint", token))
        self.idpoints[token] = {**copy.deepcopy(idpoint), "token": token}
        return token

    async def set_idpoint_configuration(self, token, device_uuid, configuration):
        self.calls.append(("set_idpoint_configuration", token, device_uuid))
        self.idpoint_configs[token] = {"DeviceUUID": device_uuid or self.device_uuid, **configuration}

    async def remove_idpoint(self, token):
        self.calls.append(("remove_idpoint", token))
        self.idpoints.pop(token, None)
        self.idpoint_configs.pop(token, None)

    async def set_io_assignment(self, io, mode, users):
        if self.fail_io_assignment:
            raise VapixError(
                "il controller ha rifiutato l'assegnazione dell'ingresso (modo non ammesso)", "ModeNotAllowedForIoUser"
            )
        self.calls.append(("set_io_assignment", io, mode))
        # Come il firmware: senza utenti il cambio di modo da supervisionato è accettato ma ignorato
        if not users and self.io_assignments.get(io, {}).get("IoMode") == "supervised":
            mode = "supervised"
        self.io_assignments[io] = {"IoMode": mode, "IoUser": copy.deepcopy(users)}

    async def create_access_point(self, access_point):
        self._check_token(access_point)
        token = access_point.get("token") or f"ap-{next(self._ids)}"
        self.calls.append(("create_access_point", token))
        self.access_points = [a for a in self.access_points if a["token"] != token]
        self.access_points.append({**copy.deepcopy(access_point), "token": token})
        return token

    async def remove_access_point(self, token):
        self.calls.append(("remove_access_point", token))
        self.access_points = [a for a in self.access_points if a["token"] != token]

    async def list_access_controllers(self):
        return [copy.deepcopy(c) for c in self.access_controllers.values()]

    async def set_access_controller(self, access_controller):
        missing = set(access_controller["AccessPoint"]) - {p["token"] for p in self.access_points}
        if missing:
            raise VapixError(f"punto di accesso inesistente: {sorted(missing)}")
        self.calls.append(("set_access_controller", access_controller["token"], list(access_controller["AccessPoint"])))
        self.access_controllers[access_controller["token"]] = copy.deepcopy(access_controller)

    def managed(self) -> set[str]:
        """Punti di accesso per cui il controller decide."""
        return {t for c in self.access_controllers.values() for t in c["AccessPoint"]}

    async def list_io(self):
        """Come risponde l'A1210 del banco: assegnati solo lettore e relè."""
        info = [
            {"IoName": "Ser0hd", "IoMode": ["rs485hd"], "PinInfo": [{"Name": "Reader 1:3", "Description": "A"}]},
            {
                "IoName": "DPS",
                "IoMode": ["in:pu/gnd", "supervised"],
                "PinInfo": [{"Name": "Door 1:1", "Description": "GND"}, {"Name": "Door 1:2", "Description": "I"}],
            },
            {
                "IoName": "REX",
                "IoMode": ["in:pu/gnd", "in:float/gnd", "supervised"],
                "PinInfo": [{"Name": "Door 1:3", "Description": "GND"}, {"Name": "Door 1:4", "Description": "I"}],
            },
            {
                "IoName": "IO1",
                "IoMode": ["inout:pu/gnd", "inout:float/gnd", "supervised"],
                "PinInfo": [{"Name": "Aux IO:3", "Description": "I/O"}],
            },
            {"IoName": "ALARM", "IoMode": ["in:pu/gnd"], "PinInfo": [{"Name": "External:4", "Description": "I"}]},
        ]
        assigned = [
            {
                "IoName": "Ser0hd",
                "IoMode": "rs485hd",
                "IoUser": [{"Type": "idpoint", "token": "lettore-1", "Usage": "RS-485HD"}],
            },
            {"IoName": "REX", "IoMode": "", "IoUser": []},
        ]
        # Quello che è stato scritto prende il posto dello stato di partenza
        merged = {a["IoName"]: a for a in assigned}
        for io, value in self.io_assignments.items():
            merged[io] = {"IoName": io, **copy.deepcopy(value)}
        return info, list(merged.values())

    async def get_access_points(self, tokens):
        return [copy.deepcopy(a) for a in self.access_points if a["token"] in tokens]

    async def set_access_point(self, access_point):
        self.calls.append(("set_access_point", access_point["token"]))
        self.access_points = [a for a in self.access_points if a["token"] != access_point["token"]]
        self.access_points.append(copy.deepcopy(access_point))

    async def list_users(self):
        return list(self.users.values())

    async def get_users(self, tokens):
        return [dict(self.users[t]) for t in tokens if t in self.users]

    async def set_user(self, user):
        self.calls.append(("set_user", user["token"]))
        self.users[user["token"]] = dict(user)

    async def remove_user(self, token):
        self.calls.append(("remove_user", token))
        self.users.pop(token, None)

    async def list_credentials(self):
        return list(self.credentials.values())

    async def get_credentials(self, tokens):
        return [dict(self.credentials[t]) for t in tokens if t in self.credentials]

    async def set_credential(self, credential):
        if self.fail_set_credential:
            raise VapixError("errore finto")
        token = credential.get("token") or f"cred-{next(self._ids)}"
        self.calls.append(("set_credential", token))
        self.credentials[token] = {**credential, "token": token}
        return token

    async def remove_credential(self, token):
        self.calls.append(("remove_credential", token))
        self.credentials.pop(token, None)

    async def enable_credential(self, token):
        self.calls.append(("enable_credential", token))
        self.credentials[token]["Enabled"] = True

    async def disable_credential(self, token):
        self.calls.append(("disable_credential", token))
        self.credentials[token]["Enabled"] = False

    async def door_mode(self, door_token):
        return self.door

    async def door_state(self, door_token):
        return {"DoorMode": self.door, "DoorPhysicalState": self.door_physical}

    async def access_door(self, door_token, extended=False):
        self.calls.append(("access_door", door_token, extended))

    async def list_access_points(self):
        return [copy.deepcopy(p) for p in self.access_points]

    async def list_access_profiles(self):
        return [copy.deepcopy(p) for p in self.profiles.values()]

    async def get_access_profiles(self, tokens):
        return [copy.deepcopy(self.profiles[t]) for t in tokens if t in self.profiles]

    async def set_access_profile(self, profile):
        self._check_token(profile)
        token = profile.get("token") or f"profile-{next(self._ids)}"
        self.calls.append(("set_access_profile", token))
        self.profiles[token] = {**copy.deepcopy(profile), "token": token}
        return token

    async def remove_access_profile(self, token):
        self.calls.append(("remove_access_profile", token))
        self.profiles.pop(token, None)

    async def list_schedules(self):
        return [dict(s) for s in self.schedules.values()]

    async def set_schedule(self, schedule):
        self._check_token(schedule)
        token = schedule.get("token") or f"schedule-{next(self._ids)}"
        self.calls.append(("set_schedule", token))
        self.schedules[token] = {**schedule, "token": token}
        return token

    async def remove_schedule(self, token):
        self.calls.append(("remove_schedule", token))
        self.schedules.pop(token, None)

    def add_existing(self, user_token, name, card, enabled=True):
        """Utente e tessera creati fuori dall'add-on (ad esempio dagli script di HA)."""
        self.users[user_token] = {"token": user_token, "Name": name, "Description": "", "Attribute": []}
        token = f"cred-{next(self._ids)}"
        self.credentials[token] = {
            "token": token,
            "UserToken": user_token,
            "Description": name,
            "Enabled": enabled,
            "IdData": [{"Name": "Card", "Value": card}],
            "CredentialAccessProfile": [{"AccessProfile": "profilo_sempre", "ValidFrom": "", "ValidTo": ""}],
        }
        return token


@pytest.fixture(autouse=True)
def no_scheduled_backups(monkeypatch):
    """I backup pianificati partirebbero da soli durante i test: li prova chi li chiede."""
    monkeypatch.setenv("AAM_SCHEDULED_BACKUPS", "0")


@pytest.fixture
def settings():
    return parse_options(
        {
            "a1210_host": "a1210.local",
            "door_token": "porta",
            "access_profile": "profilo_sempre",
            "mqtt_topic_prefix": "accessi/x",
        }
    )


@pytest.fixture
def controller():
    return FakeController()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()
