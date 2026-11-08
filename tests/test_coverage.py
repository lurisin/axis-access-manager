from app.coverage import category_doors, door_schedules, places
from app.rules_service import RulesService
from app.service import AccessService

DOOR = {
    "snapshot_camera": "",
    "snapshot_on_granted": 0,
    "ha_buttons": 1,
    "intercom_name": "",
    "intercom_open_door": 0,
    "intercom_relay": 1,
    "intercom_mqtt_prefix": "hmd",
}


def _place(db) -> dict:
    """Via Consulta: Varco Scala nell'area Camere (dal controller), Cantina nei Locali Tecnici."""
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    site = db.save_site(None, "Via Consulta", "")
    rooms = db.save_area(None, site, "Camere")
    tech = db.save_area(None, site, "Locali Tecnici")
    db.set_controller_area(controller_id, rooms)
    scala = db.save_door(None, controller_id, DOOR | {"door_token": "porta", "name": "Varco Scala"})
    cantina = db.save_door(None, controller_id, DOOR | {"door_token": "porta2", "name": "Cantina", "area_id": tech})
    return {"controller": controller_id, "site": site, "rooms": rooms, "tech": tech, "scala": scala, "cantina": cantina}


def _category(db, name: str, rules: list[dict]) -> int:
    values = {"name": name, "color": "blue", "group_token": f"profilo_{name}", "valid_days": 0, "description": ""}
    category_id = db.save_category(None, values)
    db.set_category_rules(category_id, rules)
    return category_id


def row(target, target_id, schedule="standard_always"):
    return {"target": target, "target_id": target_id, "schedule": schedule}


def test_category_doors_from_doors_areas_and_sites(db):
    ids = _place(db)
    maint = _category(db, "Manutentore", [row("door", ids["cantina"])])
    clean = _category(db, "Pulizie", [row("area", ids["rooms"], "standard_office_hours")])
    tech = _category(db, "Tecnico", [row("site", ids["site"]), row("door", ids["scala"], "standard_office_hours")])
    empty = _category(db, "Vuota", [])

    assert category_doors(db, maint) == {"porta2"}
    # L'area vale anche per la porta che la prende dal controller
    assert category_doors(db, clean) == {"porta"}
    assert category_doors(db, tech) == {"porta", "porta2"}
    # Senza righe, senza tipologia o con una tipologia che non c'è più: nessuna porta
    assert category_doors(db, empty) == set()
    assert category_doors(db, 0) == set()
    assert category_doors(db, 999) == set()
    assert door_schedules(db, db.category_rules(tech)) == {
        "porta": ["standard_always", "standard_office_hours"],
        "porta2": ["standard_always"],
    }

    # Una porta aggiunta dopo nell'area entra da sola; con l'area eliminata Pulizie non apre più niente
    db.set_door_area(ids["cantina"], ids["rooms"])
    assert category_doors(db, clean) == {"porta", "porta2"}
    layout = places(db, deleted_areas={ids["rooms"]})
    assert category_doors(db, clean, layout) == set()
    assert category_doors(db, tech, layout) == {"porta"}


async def test_profile_and_coverage_come_from_the_same_rows(db, controller, settings):
    """Il profilo che l'app scrive per la tipologia apre le porte che il registro considera della tipologia."""
    ids = _place(db)
    controller.access_points.append(
        {"token": "ap-cantina", "Entity": "porta2", "IdPointDevice": [{"IdPoint": "lettore-2", "DeviceUUID": ""}]}
    )
    rules = RulesService(AccessService(db, controller, settings))
    for target, target_id in (("door", ids["cantina"]), ("area", ids["rooms"]), ("site", ids["site"])):
        saved = await rules.save_category(None, {"name": f"T {target}", "rules": [row(target, target_id)]}, "admin")
        points = {p.get("token"): p.get("Entity") for p in controller.access_points}
        opened = {points[p["AccessPoint"]] for p in controller.profiles[saved["token"]]["AccessPolicy"]}
        assert opened == category_doors(db, saved["id"])
