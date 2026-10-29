"""Notifiche dell'app: cosa va gestito, ordinato per gravità, con il numero sulla campanella.

Le notifiche non si salvano: si ricavano ogni volta da eventi recenti e stato dei collegamenti.
Eventi uguali (stessa persona o tessera, stessa porta, stesso motivo) a pochi minuti l'uno dall'altro
diventano una sola notifica con il numero dei tentativi.
Si salvano solo i tipi scelti nelle impostazioni e quelle già viste.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta

from .backup_copy import RESTORED
from .db import Database
from .service import ServiceError, plural

# Tipo → nome, descrizione, livello predefinito
TYPES = {
    "door_alarm": (
        "Allarmi delle Porte",
        "Porta forzata, aperta troppo a lungo, manomissione del lettore o del controller.",
        "urgent",
    ),
    "controller": (
        "Controller Non Raggiungibile",
        "Il controller non è raggiungibile o non risponde all'app.",
        "urgent",
    ),
    "programming": (
        "Porte Che Non Si Aprono",
        "Punti di accesso che il controller non gestisce, pulsanti senza il permesso di apertura o ingressi rimasti "
        "programmati a metà, per esempio dopo un reset del controller: la porta non si apre. Segnala anche "
        "le tipologie non ancora aggiornate sul controller dopo una modifica a porte e aree.",
        "urgent",
    ),
    "mqtt": (
        "Collegamento MQTT Perso",
        "L'app non riceve più gli eventi dal broker MQTT, oppure non c'è nessun broker MQTT.",
        "urgent",
    ),
    "unknown_card": ("Tessere Sconosciute", "Tessere non registrate passate sul lettore.", "urgent"),
    "denied": (
        "Accessi Negati",
        "Accessi negati a persone registrate: fuori fascia, porta non autorizzata, tessera scaduta.",
        "urgent",
    ),
    "expiring": ("Tessere in Scadenza", "Persone con tessere che scadono nei prossimi 7 giorni.", "urgent"),
    "camera": ("Telecamere Non Disponibili", "L'ultima foto di una telecamera delle porte non è riuscita.", "urgent"),
    "controller_changed": (
        "Controller Cambiato",
        "All'indirizzo del controller risponde un apparecchio diverso: scritture sospese finché non si conferma.",
        "urgent",
    ),
    "clock": (
        "Orologio Sbagliato",
        "L'ora del controller o di Home Assistant è sbagliata di oltre un minuto, oppure il controller ha un fuso "
        "orario diverso da Home Assistant. Se a sbagliare è il controller le fasce orarie scattano all'ora sbagliata.",
        "urgent",
    ),
    "backup": (
        "Backup Non Riuscito",
        "L'ultimo backup non è riuscito o è incompleto, per esempio perché un controller non rispondeva, oppure la "
        "verifica giornaliera ha trovato file dei backup rovinati.",
        "urgent",
    ),
    "missing_cards": (
        "Tessere Non Presenti sul Controller",
        "Tessere che l'app conosce ma che sul controller non ci sono più: da riscrivere o togliere dall'app.",
        "urgent",
    ),
    "database": (
        "Database dell'Add-on",
        "Database trovato danneggiato o mancante all'avvio, oppure copia del database per il backup di Home Assistant "
        "non aggiornata.",
        "urgent",
    ),
}
# Guasti dell'impianto: restano finché non si risolvono, anche dopo la presa visione
FAULT_TYPES = (
    "controller",
    "programming",
    "mqtt",
    "camera",
    "controller_changed",
    "clock",
    "missing_cards",
    "backup",
    "database",
)
# Tipi con nomi: permesso necessario per vederli. Gli allarmi delle porte li vede chi vede lo stato, eventi e
# istantanee solo chi vede il registro.
VISIBILITY = {
    "denied": "registro",
    "unknown_card": "persone",
    "expiring": "persone",
    "missing_cards": "persone",
    "backup": "backup",
    "database": "backup",
}
# Attiva: compare in Da Gestire e conta sulla campanella. "info" (fino alla 0.13) vale come attiva.
LEVELS = {"urgent": "Attiva", "off": "Disattivata"}
OLD_LEVELS = {"info": "urgent"}
RECENT = timedelta(hours=24)
# Come il riquadro Tessere in Scadenza del cruscotto e l'etichetta In Scadenza in Persone
EXPIRING = timedelta(days=7)
GROUP_WINDOW = timedelta(minutes=5)
# Un guasto di collegamento preso visione torna da gestire ogni tanto, finché dura
REMIND_AFTER = timedelta(hours=4)
MAX_EVENTS = 500
SEVERITY_ORDER = {"crit": 0, "warn": 1, "low": 2}
SETTING_LEVELS = "notifications"
SETTING_SEEN = "notifications_seen"
# Tipologie con il profilo sul controller ancora da aggiornare dopo una modifica a porte o aree ({"at", "error"}):
# la scrive main._refresh_categories, che riprova dopo ogni sincronizzazione
SETTING_CATEGORIES_PENDING = "categories_refresh_pending"
# Dopo l'avvio i guasti tornano man mano (controllo dei controller, stato MQTT): fino ad allora le viste restano
SETTLE = timedelta(minutes=5)
# Id dei gruppi di eventi nelle versioni precedenti: g<primo evento>-<ultimo>
OLD_GROUP_ID = re.compile(r"^g\d+-(\d+)$")


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


class NotificationService:
    def __init__(self, db: Database, settle: timedelta = SETTLE) -> None:
        self.db = db
        self.settled_at = time.monotonic() + settle.total_seconds()
        # Avvio dell'app: ora del controllo del database
        self.started_at = datetime.now(tz=UTC).isoformat(timespec="seconds")

    # Impostazioni

    def levels(self) -> dict[str, str]:
        try:
            saved = json.loads(self.db.get_setting(SETTING_LEVELS) or "{}")
        except ValueError:
            saved = {}
        saved = {k: OLD_LEVELS.get(v, v) for k, v in saved.items()}
        return {k: saved.get(k) if saved.get(k) in LEVELS else default for k, (_, _, default) in TYPES.items()}

    def set_levels(self, data: dict, user: str) -> dict[str, str]:
        levels = self.levels()
        for key, level in data.items():
            if key not in TYPES:
                raise ServiceError(f"tipo di notifica sconosciuto: {key}")
            if level not in LEVELS:
                raise ServiceError(f"livello non valido: {level}")
            levels[key] = level
        self.db.set_setting(SETTING_LEVELS, json.dumps(levels))
        self.db.add_audit(
            user, "notifiche modificate", ", ".join(f"{TYPES[k][0]}: {LEVELS[v]}" for k, v in levels.items())
        )
        return levels

    def types(self) -> list[dict]:
        levels = self.levels()
        return [
            {"key": k, "name": name, "description": desc, "default": default, "level": levels[k]}
            for k, (name, desc, default) in TYPES.items()
        ]

    # Notifiche attive

    def collect(
        self,
        *,
        mqtt_configured: bool,
        mqtt_connected: bool,
        controller_configured: bool,
        controller_online: bool | None,
        sync: dict,
        primary_id: int | None = None,
        controller_offline_since: str = "",
        mqtt_disconnected_since: str = "",
        door_names: dict[str, str],
        cameras: list[dict] | None = None,
        controllers: list[dict] | None = None,
        missing: dict | None = None,
        backup: dict | None = None,
        backup_verify: dict | None = None,
        db_recovery: str = "",
        ha_copy_error: str = "",
        now: datetime | None = None,
    ) -> list[dict]:
        """Notifiche dei tipi attivi, dalla più grave e, a parità, dalla più recente.

        db_recovery: cosa ha fatto all'avvio il controllo del database (backup_copy.recover), "" se era sano.
        ha_copy_error: perché l'ultima copia per il backup di Home Assistant non è stata aggiornata."""
        now = now or datetime.now(tz=UTC)
        levels = self.levels()
        since = (now - RECENT).astimezone(UTC).isoformat(timespec="seconds")
        items: list[dict] = []

        def add(kind: str, item_id: str, severity: str, title: str, text: str, ts: str, **extra) -> None:
            if levels[kind] != "off":
                items.append(
                    {
                        "id": item_id,
                        "type": kind,
                        "level": levels[kind],
                        "severity": severity,
                        "title": title,
                        "text": text,
                        "ts": ts,
                        "first_ts": ts,
                        "count": 1,
                        "door": "",
                        "door_token": "",
                        "events": [],
                        "event": None,
                        "snapshot": None,
                        "snapshot_error": "",
                        "link": "",
                        # Quanto è grave il guasto: se cresce dopo la presa visione la notifica torna da gestire
                        "weight": 0,
                        # Guasto in corso: dopo la presa visione resta nella barra in alto finché non si risolve
                        "fault": kind in FAULT_TYPES,
                        **extra,
                    }
                )

        def door_of(event: dict) -> str:
            name = door_names.get(event.get("door") or "")
            if not name and event.get("kind") == "manomissione" and not event.get("door"):
                # Coperchio del controller: non riguarda una porta
                return event.get("source") or "Controller"
            if not name and len(door_names) == 1:
                name = next(iter(door_names.values()))
            return name or "porta non indicata"

        def add_group(kind: str, severity: str, title: str, text: str, group: list[dict], link: str = "") -> None:
            last = group[-1]
            shot = next((e for e in reversed(group) if e.get("snapshot")), None)
            # Id dall'ultimo evento: resta lo stesso quando i primi escono dalle 24 ore, cambia con un nuovo tentativo
            add(
                kind,
                f"g{last['id']}",
                severity,
                f"{title} ×{len(group)}" if len(group) > 1 else title,
                text,
                last["ts"],
                first_ts=group[0]["ts"],
                count=len(group),
                door=door_of(last),
                door_token=last.get("door") or "",
                events=[e["id"] for e in group],
                event=last["id"],
                snapshot=shot["id"] if shot else None,
                snapshot_error="" if shot else last.get("snapshot_error") or "",
                person_id=last.get("person_id"),
                card=last.get("card") or "",
                link=link,
            )

        if controller_configured:
            if not mqtt_configured:
                # Il broker si cerca di nuovo ogni minuto (main._wait_for_broker): la notifica sparisce da sola
                add(
                    "mqtt",
                    "mqtt:none",
                    "crit",
                    "Nessun Broker MQTT",
                    "Gli eventi del controller non arrivano all'app: registro e stato porta non si aggiornano. "
                    "Installa l'add-on Mosquitto o imposta il broker nelle opzioni dell'add-on: l'app lo cerca di "
                    "nuovo ogni minuto, senza riavvio.",
                    now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                )
            elif not mqtt_connected:
                add(
                    "mqtt",
                    _episode_id("mqtt", mqtt_disconnected_since),
                    "crit",
                    "Collegamento MQTT Perso",
                    "Gli eventi del controller non arrivano all'app: registro e stato porta non si aggiornano.",
                    mqtt_disconnected_since or now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                    weight=_reminder_weight(mqtt_disconnected_since, now),
                )
            controllers = controllers or []
            unreachable = [c for c in controllers if c.get("unreachable")]
            for c in unreachable:
                # Controllo periodico di ogni controller: non risponde da almeno un minuto
                add(
                    "controller",
                    f"controller:{c['id']}",
                    "crit",
                    "Controller Non Raggiungibile",
                    f"{c['name']} · {c.get('error') or 'non risponde'}",
                    c.get("checked_at") or now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                )
            # Solo con l'ultimo controllo riuscito: dopo il primo errore il controller potrebbe non rispondere. Conta
            # il controller principale, l'unico che manda gli eventi all'app: al collaudo con un altro controller
            # non raggiungibile il guasto non compariva, e il principale è rimasto scollegato dal broker per ore
            primary = next((c for c in controllers if c.get("id") == primary_id), None) if primary_id else None
            checked = [primary] if primary else controllers
            reachable = bool(checked) and all(c.get("online") is True for c in checked)
            if mqtt_connected and controller_online is False and reachable:
                # Il controller risponde all'app ma non pubblica sul broker
                name = f"{primary['name']} risponde" if primary and len(controllers) > 1 else "Il controller risponde"
                add(
                    "mqtt",
                    _episode_id("mqtt:controller", controller_offline_since),
                    "crit",
                    "Eventi del Controller Non Arrivano",
                    f"{name} ma non è collegato al broker MQTT: passaggi e allarmi non arrivano in diretta. "
                    "Lo stato della porta si rilegge dal controller ogni 30 secondi, e i passaggi persi si "
                    "recuperano quando il controller torna collegato.",
                    controller_offline_since or now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                    weight=_reminder_weight(controller_offline_since, now),
                )
            elif not unreachable and (sync.get("ok") is False or (controller_online is False and not controllers)):
                detail = sync.get("error") if sync.get("ok") is False else "il controller non è raggiungibile"
                add(
                    "controller",
                    "controller",
                    "crit",
                    "Controller Non Raggiungibile",
                    _sentence(detail or ""),
                    sync.get("at") or now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                )
            ha_clock = False
            for c in controllers:
                if c.get("identity_change"):
                    add(
                        "controller_changed",
                        f"changed:{c['id']}:{c.get('serial') or ''}",
                        "crit",
                        "Controller Cambiato",
                        _capital(c["identity_change"]) + ". Fino ad allora le modifiche non vengono scritte.",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link="#impianti",
                    )
                if c.get("unmanaged") and not c.get("unreachable"):
                    names = c["unmanaged"]
                    add(
                        "programming",
                        f"unmanaged:{c['id']}",
                        "crit",
                        "Punto di Accesso Non Gestito" if len(names) == 1 else "Punti di Accesso Non Gestiti",
                        f"{c['name']} · {_names(names)}: il controller legge tessere e pulsanti ma non decide, quindi "
                        "la porta non si apre. Rimedia con Fai Gestire al Controller nella pagina del controller.",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link=f"#impianti/c{c['id']}",
                        weight=len(names),
                    )
                if c.get("rex_missing") and not c.get("unreachable"):
                    names = c["rex_missing"]
                    add(
                        "programming",
                        f"rex:{c['id']}",
                        "crit",
                        "Pulsante Senza Permesso di Apertura"
                        if len(names) == 1
                        else "Pulsanti Senza Permesso di Apertura",
                        f"{c['name']} · {_names(names)}: il controller nega l'apertura, quindi la porta non si apre. "
                        "Rimedia con Dai il Permesso nella pagina del controller.",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link=f"#impianti/c{c['id']}",
                        weight=len(names),
                    )
                if c.get("input_problems") and not c.get("unreachable"):
                    # Scrittura rimasta a metà o oggetti tolti fuori dall'app: si ripara scrivendo di nuovo l'ingresso
                    names = c["input_problems"]
                    add(
                        "programming",
                        f"inputs:{c['id']}",
                        "warn",
                        "Ingresso da Riscrivere" if len(names) == 1 else "Ingressi da Riscrivere",
                        f"{c['name']} · {_names(names)}: sul controller la programmazione non è quella salvata "
                        "nell'app. Rimedia aprendo l'ingresso nella pagina del controller e premendo Scrivi sul "
                        "Controller, anche con le stesse impostazioni.",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link=f"#impianti/c{c['id']}",
                        weight=len(names),
                    )
                if c.get("zone_differs") and not c.get("unreachable"):
                    # Fasce orarie e festività sono scritte in ora locale del controller
                    add(
                        "clock",
                        f"zone:{c['id']}",
                        "warn",
                        "Fuso Orario del Controller",
                        f"{c['name']} · fuso orario {c.get('time_zone') or 'sconosciuto'}, diverso da quello di "
                        f"Home Assistant ({c.get('ha_time_zone')}): le fasce orarie scattano all'ora sbagliata.",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link=f"#impianti/c{c['id']}/stato",
                    )
                offset = c.get("clock_offset")
                if c.get("clock_fault") == "controller":
                    add(
                        "clock",
                        f"clock:{c['id']}",
                        "warn",
                        "Orologio del Controller",
                        f"{c['name']} · {_offset_text(offset)} rispetto a Home Assistant, NTP non sincronizzato",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link="#impianti",
                        weight=_offset_weight(offset),
                    )
                elif c.get("clock_fault") == "home_assistant" and not ha_clock:
                    # Il controller è sincronizzato via NTP: è l'ora di Home Assistant a essere sbagliata
                    ha_clock = True
                    add(
                        "clock",
                        "clock:home_assistant",
                        "low",
                        "Orologio di Home Assistant",
                        f"Home Assistant è {_offset_text(-offset)} rispetto a {c['name']}, "
                        "che è sincronizzato via NTP: controlla il server NTP di Home Assistant.",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link="#impianti",
                        weight=_offset_weight(offset),
                    )
            pending = _json_dict(self.db.get_setting(SETTING_CATEGORIES_PENDING))
            if pending is not None:
                # Il controller applica ancora le righe di prima: chi doveva perdere una porta la apre ancora
                error = str(pending.get("error") or "")
                add(
                    "programming",
                    "categories",
                    "crit",
                    "Tipologie Non Aggiornate sul Controller",
                    "Le ultime modifiche a porte e aree non sono ancora sul controller, che applica le tipologie "
                    "com'erano prima. "
                    + (f"{_sentence(error)} " if error else "")
                    + "L'app riprova dopo ogni sincronizzazione con il controller.",
                    str(pending.get("at") or "") or now.isoformat(timespec="seconds"),
                    link="#persone/tipologie",
                )
        if missing and missing.get("count"):
            add(
                "missing_cards",
                f"missing:{missing['since']}",
                "crit" if missing.get("mass") else "warn",
                "Tessera Non Presente sul Controller"
                if missing["count"] == 1
                else "Tessere Non Presenti sul Controller",
                (
                    f"{plural(missing['count'], 'tessera', 'tessere')} di "
                    f"{plural(missing['persons'], 'persona', 'persone')} "
                    + (
                        "non è più sul controller: riscrivila o toglila dall'app."
                        if missing["count"] == 1
                        else "non sono più sul controller: riscrivile o toglile dall'app."
                    )
                ),
                missing["since"],
                link="#persone/mancanti",
                # Altre tessere sparite dopo la presa visione (per esempio un reset): torna da gestire
                weight=missing["count"],
            )
        if backup and backup.get("error"):
            # Con l'indice il backup c'è ma è incompleto; senza, non è stato salvato
            add(
                "backup",
                f"backup:{backup.get('at') or ''}",
                "warn",
                "Backup Incompleto" if backup.get("id") else "Backup Non Riuscito",
                _sentence(backup["error"]),
                backup.get("at") or now.isoformat(timespec="seconds"),
                link="#impianti/backup",
            )
        if backup_verify and not backup_verify.get("ok", True):
            # Resta finché una verifica successiva non trova tutto a posto (per esempio dopo aver eliminato i rovinati)
            damaged = backup_verify.get("damaged") or len(backup_verify.get("errors") or [])
            add(
                "backup",
                f"backup-verify:{backup_verify.get('at') or ''}",
                "warn",
                "Backup Rovinato" if damaged == 1 else "Backup Rovinati",
                (
                    f"{damaged} backup non si possono ripristinare: file mancanti o rovinati."
                    if damaged != 1
                    else "Un backup non si può ripristinare: file mancanti o rovinati."
                ),
                backup_verify.get("at") or now.isoformat(timespec="seconds"),
                link="#impianti/backup",
            )
        if db_recovery:
            restored = RESTORED in db_recovery
            add(
                "database",
                # Un id per avvio: il database ripristinato di nuovo a un avvio successivo non risulta già visto
                f"database:avvio:{self.started_at}",
                "crit",
                "Database Ripristinato dalla Copia" if restored else "Database Non Ripristinato",
                _sentence(db_recovery),
                self.started_at,
                link="#impostazioni/generale",
                # Ripristinato: è successo all'avvio e non c'è altro da fare, dopo la presa visione non resta
                fault=not restored,
            )
        if ha_copy_error:
            add(
                "database",
                "database:copia",
                "warn",
                "Copia per il Backup Non Aggiornata",
                _sentence(ha_copy_error),
                now.isoformat(timespec="seconds"),
                link="#impostazioni/generale",
            )
        titles = {"allarme": "Allarme Porta", "manomissione": "Manomissione"}
        if levels["door_alarm"] != "off":
            alarms = self.db.list_events(kind="allarme,manomissione", since=since, limit=MAX_EVENTS)
            for group in _groups(alarms, lambda e: (e["kind"], e.get("door") or "", e.get("detail") or "")):
                last = group[-1]
                add_group("door_alarm", "crit", titles[last["kind"]], _capital(last.get("detail") or ""), group)
        if levels["denied"] != "off" or levels["unknown_card"] != "off":
            denied = self.db.list_events(kind="negato", since=since, limit=MAX_EVENTS)

            def who(e: dict) -> tuple:
                return e.get("person_id") or e.get("person_name") or e.get("card"), e.get("door"), e.get("detail")

            for group in _groups(denied, who):
                last = group[-1]
                reason = last.get("detail") or ""
                if last.get("person_id") or last.get("person_name"):
                    # Persona registrata: un tentativo è poco, più tentativi di fila meritano attenzione
                    text = " · ".join(x for x in (last.get("person_name") or "", reason) if x)
                    add_group("denied", "warn" if len(group) > 1 else "low", "Accesso Negato", text, group)
                elif not last.get("card"):
                    # Senza tessera: un pulsante o il relè del citofono che il controller non fa aprire
                    source = last.get("source") or ""
                    text = " · ".join(x for x in ("" if source == "controller" else source, reason) if x)
                    add_group("denied", "low", "Accesso Negato", text or "Accesso negato senza tessera", group)
                else:
                    # Tessera non registrata: ripetuta è un tentativo insistito
                    text = f"Tessera {last['card']}"
                    severity = "crit" if len(group) > 1 else "warn"
                    add_group("unknown_card", severity, "Tessera Sconosciuta", text, group, "#da-registrare")
        for cam in cameras or []:
            # Telecamera di una porta con l'ultima foto non riuscita: sparisce alla prima foto buona
            add(
                "camera",
                f"cam:{cam['camera']}",
                "warn",
                "Telecamera Non Disponibile",
                f"{cam['name']} · {cam['error']}",
                cam["ts"],
                door=", ".join(cam.get("doors") or []),
                link="#impianti",
            )
        if levels["expiring"] != "off":
            for person in self.db.list_persons():
                ends = []
                for card in person["cards"]:
                    start, end = _parse(card.get("valid_from") or ""), _parse(card.get("valid_to") or "")
                    # Come il cruscotto e In Scadenza in Persone: una tessera non ancora valida non è in scadenza
                    if end and now < end <= now + EXPIRING and not (start and start > now):
                        ends.append(end)
                end = min(ends) if ends else None
                if end:
                    add(
                        "expiring",
                        f"x{person['id']}:{end.isoformat()}",
                        "low",
                        "Tessera in Scadenza",
                        f"{person['name']} · scade il {end.astimezone():%d/%m/%Y %H:%M}",
                        (end - EXPIRING).isoformat(timespec="seconds"),
                        person_id=person["id"],
                        link=f"#persone/{person['id']}",
                    )
        items.sort(key=lambda i: (_parse(i["ts"]) or now).timestamp(), reverse=True)
        items.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
        return items

    # Già viste

    def seen(self) -> dict[str, int | None]:
        """Notifiche viste, con il peso che avevano alla presa visione (None: viste quando il peso non si salvava)."""
        try:
            saved = json.loads(self.db.get_setting(SETTING_SEEN) or "{}")
        except ValueError:
            return {}
        if isinstance(saved, list):
            saved = dict.fromkeys(saved)
        if not isinstance(saved, dict):
            return {}
        return {OLD_GROUP_ID.sub(r"g\1", str(k)): v if isinstance(v, int) else None for k, v in saved.items()}

    def _still_seen(self, seen: dict[str, int | None], active: dict[str, dict]) -> dict[str, int | None]:
        """Dimentica le viste che non ci sono più, così se il problema torna si rivede.

        Appena avviato no: i guasti tornano solo dopo i primi controlli, e la presa visione andrebbe persa.
        """
        if time.monotonic() >= self.settled_at:
            seen = {k: v for k, v in seen.items() if k in active}
        # Viste senza peso: vale quello di adesso
        return {k: active[k].get("weight", 0) if v is None and k in active else v for k, v in seen.items()}

    def _save_seen(self, seen: dict[str, int | None]) -> None:
        self.db.set_setting(SETTING_SEEN, json.dumps(dict(sorted(seen.items()))))

    def mark_seen(self, ids: list[str], current: list[dict]) -> None:
        """Segna come viste con il peso di adesso: se poi il guasto peggiora, la notifica torna da gestire."""
        active = {i["id"]: i for i in current}
        seen = self._still_seen(self.seen(), active)
        for item_id in ids:
            if item_id in active:
                seen[item_id] = max(seen.get(item_id) or 0, active[item_id].get("weight", 0))
        self._save_seen(seen)

    def listing(self, current: list[dict], perms: set[str] | None = None) -> dict:
        """Notifiche per un utente: nomi, tessere ed eventi solo a chi ha il permesso di vederli."""
        saved = self.seen()
        active = {i["id"]: i for i in current}
        seen = self._still_seen(saved, active)
        if seen != saved:
            self._save_seen(seen)
        hidden = {k for k, perm in VISIBILITY.items() if perms is not None and perm not in perms}
        no_log = perms is not None and "registro" not in perms
        items = []
        for i in current:
            if i["type"] in hidden:
                continue
            item = {
                **i,
                "seen": i["id"] in seen and i.get("weight", 0) <= (seen[i["id"]] or 0),
                "fault": i.get("fault", i["type"] in FAULT_TYPES),
            }
            if no_log:
                item.update(events=[], event=None, snapshot=None)
            items.append(item)
        return {
            "items": items,
            "open": sum(1 for i in items if not i["seen"]),
            "types": self.types(),
            "levels": LEVELS,
        }


def _offset_text(seconds: float) -> str:
    """Scarto dell'orologio: "3 min avanti", "45 s indietro"."""
    way = "avanti" if seconds > 0 else "indietro"
    value = abs(seconds)
    if value >= 3600:
        return f"{value / 3600:.1f} ore {way}".replace(".", ",")
    if value >= 120:
        return f"{round(value / 60)} min {way}"
    return f"{round(value)} s {way}"


def _reminder_weight(since: str, now: datetime) -> int:
    """Peso di un guasto che dura: cresce ogni REMIND_AFTER, così dopo la presa visione torna da gestire. Al
    collaudo un controller scollegato dal broker, preso visione alle 18:20, è rimasto nascosto per ore."""
    start = _parse(since) if since else None
    if start is None or now <= start:
        return 0
    return int((now - start) / REMIND_AFTER)


def _episode_id(item_id: str, since: str) -> str:
    """Id di un guasto con l'ora di inizio: se riparte è un'altra notifica, da gestire anche se la precedente era
    stata presa visione. Al collaudo il guasto ripartito con l'aggiornamento dell'add-on restava visto dalle 18:20."""
    return f"{item_id}@{since}" if since else item_id


def _offset_weight(seconds: float | None) -> int:
    """Peso dello scarto dell'orologio: oltre 10 minuti e oltre un'ora la notifica vista torna da gestire."""
    value = abs(seconds or 0)
    return 2 if value >= 3600 else 1 if value >= 600 else 0


def _json_dict(value: str | None) -> dict | None:
    """Impostazione salvata come oggetto JSON; None se è vuota, {} se non si legge."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _names(names: list[str]) -> str:
    return f"{', '.join(names[:3])}{' e altri' if len(names) > 3 else ''}"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _sentence(text: str) -> str:
    """Messaggio del server come frase a sé: maiuscola iniziale e punto finale."""
    text = _capital(text.strip())
    return text if not text or text.endswith((".", "!", "?")) else text + "."


def _groups(events: list[dict], key) -> list[list[dict]]:
    """Eventi uguali per la chiave, ciascuno entro GROUP_WINDOW dal precedente: dal più vecchio al più recente."""
    open_groups: dict = {}
    groups: list[list[dict]] = []
    dated = [(_parse(e.get("ts") or ""), e) for e in events]
    for at, event in sorted(dated, key=lambda d: (d[0].timestamp() if d[0] else 0, d[1]["id"])):
        k = key(event)
        group = open_groups.get(k)
        last = _parse(group[-1].get("ts") or "") if group else None
        if group and at and last and at - last <= GROUP_WINDOW:
            group.append(event)
        else:
            group = [event]
            open_groups[k] = group
            groups.append(group)
    return groups
