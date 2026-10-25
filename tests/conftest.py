import copy
import itertools

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
        self.fail_set_credential = False
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

    async def device_info(self):
        return {"ProdNbr": "A1210", "Version": "10.12.288.1", "SerialNumber": "X"}

    async def door_info_list(self):
        return [{"token": t, "Name": d["Name"]} for t, d in self.doors.items()]

    async def list_doors(self):
        return [copy.deepcopy(d) for d in self.doors.values()]

    async def get_doors(self, tokens):
        return [copy.deepcopy(self.doors[t]) for t in tokens if t in self.doors]

    async def set_door(self, door):
        self.calls.append(("set_door", door["token"]))
        self.doors[door["token"]] = copy.deepcopy(door)

    async def list_door_configurations(self):
        return [{"token": "porta", "Configuration": [{"Name": "Lock.Type", "Value": "FailSecure"}]}]

    async def list_idpoints(self):
        return [copy.deepcopy(i) for i in self.idpoints.values()]

    async def get_idpoints(self, tokens):
        return [copy.deepcopy(self.idpoints[t]) for t in tokens if t in self.idpoints]

    async def set_idpoint(self, idpoint):
        self.calls.append(("set_idpoint", idpoint["token"]))
        self.idpoints[idpoint["token"]] = copy.deepcopy(idpoint)

    async def list_idpoint_configurations(self):
        raise VapixError("non supportato")

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

    async def access_door(self, door_token, extended=False):
        self.calls.append(("access_door", door_token, extended))

    async def list_access_points(self):
        return [copy.deepcopy(p) for p in self.access_points]

    async def list_access_profiles(self):
        return [copy.deepcopy(p) for p in self.profiles.values()]

    async def get_access_profiles(self, tokens):
        return [copy.deepcopy(self.profiles[t]) for t in tokens if t in self.profiles]

    async def set_access_profile(self, profile):
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
