#!/usr/bin/env python3
"""Prove al banco sull'A1210: T1-T12, T16 e T18 del passaggio «l'app è l'unica fonte di verità».

Usa il client VAPIX dell'add-on (axis_access_manager/app/vapix.py). Crea solo oggetti con token e nome aam_prova_ e
li toglie tutti alla fine, anche dopo un errore. Non manda mai comandi alle porte e non scrive porte o lettori che
non ha creato, configurazioni di porte e lettori, morsetti, controllo accessi, fuso, NTP, MQTT, rete o account: lo
impedisce il trasporto HTTP stesso (Registratore), che lascia partire solo i metodi elencati qui sotto.
Istruzioni in LEGGIMI.md.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import getpass
import itertools
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

ADDON = Path(__file__).resolve().parents[2] / "axis_access_manager"
if str(ADDON) not in sys.path:
    sys.path.insert(0, str(ADDON))

from app import vapix  # noqa: E402
from app.rules import DAYS, holidays_ical, schedule_ical  # noqa: E402

PREFISSO = "aam_prova_"
# Utenti e tessere che un A1210 ha dopo il reset di fabbrica: nessuno finché T12 non dice il contrario
DI_FABBRICA: set[str] = set()
# T11 vuole il controller appena resettato; T4 e T8 chiedono un lettore configurato; T12 è il reset
ORDINE = ["T11", "T1", "T2", "T3", "T5", "T6", "T7", "T9", "T10", "T16", "T18", "T4", "T8", "T12"]
TITOLI = {
    "T1": "Token scelti dall'app alla creazione",
    "T2": "Stessa scrittura due volte, con il cavo staccato in mezzo",
    "T3": "Scrittura con campi omessi su un oggetto esistente",
    "T4": "Tessera sospesa con SetCredential o con DisableCredential",
    "T5": "Due tessere con lo stesso numero",
    "T6": "Riferimenti a oggetti che non ci sono e rimozione di oggetti in uso",
    "T7": "Rimozione di oggetti che non ci sono",
    "T8": "Tessera presente ma non abilitata",
    "T9": "Festività, testo delle fasce e lunghezza di nome e descrizione",
    "T10": "Come il controller restituisce i valori scritti",
    "T11": "Elenchi vuoti: chiave vuota o assente",
    "T12": "Oggetti di serie dopo il reset di fabbrica (il reset lo fa l'operatore)",
    "T16": "Utente sentinella senza tessere",
    "T18": "Tempi con molte tessere; limiti e pulsante durante la scrittura con --limiti",
}
# T18 con --limiti: tetto di ogni ricerca (si cambia con --tetto nome=numero), prudente per il tempo al banco e per la
# memoria del controller; oltre il tetto il kit non scrive anche senza CapabilityViolated
TETTI = {"utenti": 2000, "tessere": 2000, "profili": 200, "fasce": 200, "eccezioni": 500, "regole": 100}
# Oggetti (o voci) scritti tra un controllo e l'altro, al massimo; con tetti piccoli i blocchi si accorciano
BLOCCHI = {"utenti": 100, "tessere": 100, "profili": 20, "fasce": 20, "eccezioni": 25, "regole": 10, "punti": 10}

# Metodi VAPIX che possono partire. Tutto il resto lo ferma il Registratore prima che esca: comandi alle porte
# (AccessDoor, UnlockDoor, LockDoor, DoorSchedule...), SetDoorConfiguration, SetIdPointConfiguration, SetIoAssignment,
# SetAccessController, SetAuthenticationProfile, fuso, NTP, MQTT, rete e account
CGI = {("/axis-cgi/basicdeviceinfo.cgi", "getAllProperties"), ("/axis-cgi/time.cgi", "getDateTimeInfo")}
LETTURE_HTTP = set(
    """axudb:GetUserList axudb:GetUser pacsaxis:GetCredentialList pacsaxis:GetCredential pacsaxis:GetAccessPointList
    pacsaxis:GetAccessPoint pacsaxis:GetAccessProfileList pacsaxis:GetAccessProfile
    pacsaxis:GetAuthenticationProfileList pacsaxis:GetAccessControllerList axsch:GetScheduleList axtdc:GetDoorList
    axtdc:GetDoor axtdc:GetDoorConfigurationList axtid:GetIdPointList axtid:GetIdPoint
    axtid:GetIdPointConfigurationList axisio:GetIoInfoList axisio:GetIoAssignmentList axlog:FetchEvents""".split()
)
# Scritture, solo su oggetti del kit: metodo → chiave dell'elenco di oggetti nella richiesta
SCRITTURE_HTTP = {
    "axudb:SetUser": "User",
    "pacsaxis:SetCredential": "Credential",
    "pacsaxis:SetAccessProfile": "AccessProfile",
    "pacsaxis:SetAccessPoint": "AccessPoint",
    "axsch:SetSchedule": "Schedule",
    "axtdc:SetDoor": "Door",
    "axtid:SetIdPoint": "IdPoint",
}
# Con i token nella richiesta, solo del kit (le configurazioni le toglie vapix insieme alla porta e al lettore del kit)
RIMOZIONI_HTTP = set(
    """axudb:RemoveUser pacsaxis:RemoveCredential pacsaxis:RemoveAccessProfile pacsaxis:RemoveAccessPoint
    axsch:RemoveSchedule axtdc:RemoveDoor axtdc:RemoveDoorConfiguration axtid:RemoveIdPoint
    axtid:RemoveIdPointConfiguration pacsaxis:EnableCredential pacsaxis:DisableCredential""".split()
)
# Metodi del client che il kit usa: letture, scritture di un oggetto e scritture con il solo token
LETTURE = set(
    """device_info date_time_info certificate_fingerprint list_users get_users list_credentials get_credentials
    list_access_points get_access_points list_access_profiles get_access_profiles list_authentication_profiles
    list_access_controllers list_schedules list_doors get_doors list_door_configurations list_idpoints get_idpoints
    list_idpoint_configurations list_io latest_events""".split()
)
SCRITTURE_OGGETTO = set(
    "set_user set_credential set_schedule set_access_profile set_door create_idpoint create_access_point".split()
)
SCRITTURE_TOKEN = set(
    """remove_user remove_credential remove_schedule remove_access_profile remove_door remove_idpoint
    remove_access_point enable_credential disable_credential""".split()
)


@dataclass(frozen=True)
class Tipo:
    elenco: str
    leggi: str
    scrivi: str
    togli: str
    percorso: str
    rimozione: str


TIPI = {
    "utente": Tipo("list_users", "get_users", "set_user", "remove_user", "/vapix/pacs", "axudb:RemoveUser"),
    "tessera": Tipo(
        "list_credentials",
        "get_credentials",
        "set_credential",
        "remove_credential",
        "/vapix/pacs",
        "pacsaxis:RemoveCredential",
    ),
    "fascia": Tipo("list_schedules", "", "set_schedule", "remove_schedule", "/vapix/schedule", "axsch:RemoveSchedule"),
    "profilo": Tipo(
        "list_access_profiles",
        "get_access_profiles",
        "set_access_profile",
        "remove_access_profile",
        "/vapix/pacs",
        "pacsaxis:RemoveAccessProfile",
    ),
    "porta": Tipo("list_doors", "get_doors", "set_door", "remove_door", "/vapix/doorcontrol", "axtdc:RemoveDoor"),
    "lettore": Tipo(
        "list_idpoints", "get_idpoints", "create_idpoint", "remove_idpoint", "/vapix/idpoint", "axtid:RemoveIdPoint"
    ),
    "punto": Tipo(
        "list_access_points",
        "get_access_points",
        "create_access_point",
        "remove_access_point",
        "/vapix/pacs",
        "pacsaxis:RemoveAccessPoint",
    ),
}
# Prima chi cita, poi chi è citato; per creare, al contrario
ORDINE_RIMOZIONE = ["tessera", "utente", "profilo", "punto", "lettore", "porta", "fascia"]
ORDINE_CREAZIONE = ["utente", "fascia", "tessera", "profilo", "porta", "lettore", "punto"]
FOTO = {
    "utenti": "list_users",
    "tessere": "list_credentials",
    "fasce": "list_schedules",
    "profili": "list_access_profiles",
    "porte": "list_doors",
    "lettori": "list_idpoints",
    "punti": "list_access_points",
    "configurazioni_porte": "list_door_configurations",
    "configurazioni_lettori": "list_idpoint_configurations",
    "modi_di_identificazione": "list_authentication_profiles",
    "controllo_accessi": "list_access_controllers",
    "morsetti": "list_io",
}
TIPO_DELLA_FOTO = {
    "utenti": "utente",
    "tessere": "tessera",
    "fasce": "fascia",
    "profili": "profilo",
    "porte": "porta",
    "lettori": "lettore",
    "punti": "punto",
}
ELENCHI_T11 = [
    ("utenti", "/vapix/pacs", "axudb:GetUserList", "User"),
    ("tessere", "/vapix/pacs", "pacsaxis:GetCredentialList", "Credential"),
    ("punti di accesso", "/vapix/pacs", "pacsaxis:GetAccessPointList", "AccessPoint"),
    ("profili", "/vapix/pacs", "pacsaxis:GetAccessProfileList", "AccessProfile"),
    ("modi di identificazione", "/vapix/pacs", "pacsaxis:GetAuthenticationProfileList", "AuthenticationProfile"),
    ("controllo accessi", "/vapix/pacs", "pacsaxis:GetAccessControllerList", "AccessController"),
    ("fasce", "/vapix/schedule", "axsch:GetScheduleList", "Schedule"),
    ("porte", "/vapix/doorcontrol", "axtdc:GetDoorList", "Door"),
    ("configurazioni delle porte", "/vapix/doorcontrol", "axtdc:GetDoorConfigurationList", "DoorConfiguration"),
    ("lettori", "/vapix/idpoint", "axtid:GetIdPointList", "IdPoint"),
    ("configurazioni dei lettori", "/vapix/idpoint", "axtid:GetIdPointConfigurationList", "IdPointConfiguration"),
]
SETTIMANA = schedule_ical([{"days": DAYS[:5], "start": "08:00", "end": "18:00"}])
SEMPRE = schedule_ical([{"days": DAYS, "start": "00:00", "end": "24:00"}])
FESTE = holidays_ical([{"day": "2026-12-25", "yearly": True}, {"day": "2027-04-05", "yearly": False}])


class Vietata(Exception):
    """Richiesta che il kit non deve mai mandare: si ferma tutto, prima che parta."""


class Rifiuto(Exception):
    """Il kit non parte (o si ferma): controller sbagliato o che sembra in servizio."""


def nostro(tipo: str, oggetto: dict) -> bool:
    """Oggetto creato dal kit: token o nome aam_prova_ (per le tessere anche descrizione o utente)."""
    campi = ["token", "Name"] + (["Description", "UserToken"] if tipo == "tessera" else [])
    return any(str(oggetto.get(c) or "").startswith(PREFISSO) for c in campi)


def serie_pulita(valore: str) -> str:
    return "".join(c for c in str(valore).upper() if c.isalnum())


def lungo(inizio: str, lunghezza: int) -> str:
    return (inizio + "x" * lunghezza)[:lunghezza]


def riassunto_evento(riga: dict) -> dict:
    valori = {str(kv.get("Key")): kv.get("Value") for kv in riga.get("KeyValues") or [] if kv.get("Key")}
    argomento = "/".join(str(valori[f"topic{i}"]) for i in range(8) if valori.get(f"topic{i}"))
    resto = {k: v for k, v in valori.items() if not k.startswith("topic")}
    return {"riga": riga.get("rowid"), "ora": riga.get("UtcTime"), "argomento": argomento, **resto}


def confronta(mandato: dict, letto: dict | None) -> dict:
    """Chiavi aggiunte dal controller, chiavi mandate e non restituite, valori cambiati."""
    if letto is None:
        return {"letto": None}
    return {
        "aggiunte": {k: v for k, v in letto.items() if k not in mandato},
        "mancanti": [k for k in mandato if k not in letto],
        "diverse": {k: {"mandato": v, "letto": letto[k]} for k, v in mandato.items() if k in letto and letto[k] != v},
    }


def statistiche(durate: list[float]) -> dict:
    if not durate:
        return {"quante": 0}
    ordinate = sorted(durate)
    return {
        "quante": len(durate),
        "totale_s": round(sum(durate), 2),
        "media_ms": round(1000 * sum(durate) / len(durate)),
        "p95_ms": round(1000 * ordinate[min(len(ordinate) - 1, int(len(ordinate) * 0.95))]),
        "massimo_ms": round(1000 * ordinate[-1]),
    }


def sospesa(tessera: dict | None) -> bool:
    """Tessera riletta e sospesa: Enabled false. Non letta, senza Enabled o con un altro valore non conta come
    sospesa."""
    return isinstance(tessera, dict) and tessera.get("Enabled") is False


def stato_tessera(tessera: dict | None) -> str:
    if not isinstance(tessera, dict):
        return "non letta"
    return f"Enabled {tessera.get('Enabled')!r}, Status {tessera.get('Status')!r}"


def con_numero(tessera: dict, numero: str) -> bool:
    """La tessera ha quel numero (maiuscole o minuscole non contano)."""
    numeri = [str(d.get("Value", "")).lower() for d in tessera.get("IdData") or [] if isinstance(d, dict)]
    return numero.lower() in numeri


def profili_legati(tessera: dict) -> list[str]:
    return [
        str(c.get("AccessProfile") if isinstance(c, dict) else c) for c in tessera.get("CredentialAccessProfile") or []
    ]


def blocco_di(nome: str, tetto: int) -> int:
    """Blocco della ricerca di un limite: almeno dieci blocchi fino al tetto, al massimo BLOCCHI[nome]."""
    return max(1, min(BLOCCHI[nome], tetto // 10))


def _json(contenuto: bytes):
    try:
        return json.loads(contenuto)
    except ValueError:
        return contenuto.decode("utf-8", "replace")[:4000]


class Permessi:
    """Token che il kit può scrivere o togliere: i suoi (aam_prova_) e quelli che il controller ha dato a oggetti
    del kit (trovati con il nome aam_prova_)."""

    def __init__(self) -> None:
        self.extra: set[str] = set()

    def ammesso(self, token) -> bool:
        return isinstance(token, str) and (token.startswith(PREFISSO) or token in self.extra)


def controlla(request: httpx.Request, permessi: Permessi) -> str:
    """Metodo VAPIX della richiesta se il kit lo può mandare, altrimenti Vietata."""
    try:
        corpo = json.loads(request.content or b"null")
    except ValueError as err:
        raise Vietata(f"richiesta non riconosciuta per {request.url.path}") from err
    percorso = request.url.path
    if request.method != "POST" or not isinstance(corpo, dict):
        raise Vietata(f"{request.method} {percorso}")
    if percorso.startswith("/axis-cgi/"):
        metodo = str(corpo.get("method"))
        if (percorso, metodo) in CGI:
            return metodo
        raise Vietata(f"{percorso} {metodo}")
    if len(corpo) != 1:
        raise Vietata(f"{percorso}: più metodi in una richiesta")
    metodo, argomenti = next(iter(corpo.items()))
    argomenti = argomenti if isinstance(argomenti, dict) else {}
    if metodo in LETTURE_HTTP:
        return metodo
    if metodo in SCRITTURE_HTTP:
        oggetti = argomenti.get(SCRITTURE_HTTP[metodo])
        if isinstance(oggetti, list) and oggetti and all(isinstance(o, dict) for o in oggetti):
            nomi = [o["Name"] for o in oggetti if "Name" in o]
            if all(permessi.ammesso(o.get("token")) for o in oggetti) and all(
                str(n).startswith(PREFISSO) for n in nomi
            ):
                return metodo
    elif metodo in RIMOZIONI_HTTP:
        token = argomenti.get("Token")
        tokens = token if isinstance(token, list) else [token]
        if tokens and all(permessi.ammesso(t) for t in tokens):
            return metodo
    raise Vietata(f"{metodo} non permesso al kit: {json.dumps(argomenti, ensure_ascii=False)[:300]}")


class Registratore(httpx.AsyncBaseTransport):
    """Trasporto del client: ferma le richieste vietate prima che partano e tiene richieste e risposte grezze, senza
    intestazioni (quindi senza credenziali)."""

    def __init__(self, interno: httpx.AsyncBaseTransport, permessi: Permessi | None = None) -> None:
        self.interno = interno
        self.permessi = permessi or Permessi()
        self.scambi: list[dict] = []
        # Tutti i metodi partiti, nell'ordine
        self.metodi: list[str] = []

    def prendi(self) -> list[dict]:
        scambi, self.scambi = self.scambi, []
        return scambi

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        metodo = controlla(request, self.permessi)
        self.metodi.append(metodo)
        voce: dict = {"metodo": metodo, "percorso": request.url.path, "richiesta": _json(request.content)}
        self.scambi.append(voce)
        inizio = time.monotonic()
        try:
            risposta = await self.interno.handle_async_request(request)
            # Letta qui per intero: il client poi ritrova il contenuto già letto
            contenuto = await risposta.aread()
        except Exception as err:
            voce.update(errore=repr(err), durata_s=round(time.monotonic() - inizio, 3))
            raise
        voce.update(stato=risposta.status_code, risposta=_json(contenuto), durata_s=round(time.monotonic() - inizio, 3))
        return risposta

    async def aclose(self) -> None:
        await self.interno.aclose()


def cliente(
    host: str,
    utente: str,
    password: str,
    verifica: bool = False,
    trasporto: httpx.AsyncBaseTransport | None = None,
) -> tuple[vapix.A1210Client, Registratore]:
    """Il client dell'add-on con il Registratore davanti al trasporto vero."""
    registro = Registratore(trasporto or httpx.AsyncHTTPTransport(verify=verifica))
    return vapix.A1210Client(host, utente, password, verify_ssl=verifica, transport=registro), registro


@dataclass
class Esito:
    ok: bool
    valore: object = None
    errore: str = ""
    fault: str = ""
    irraggiungibile: bool = False
    durata: float = 0.0
    http: list = field(default_factory=list)

    def token_risposta(self) -> str | None:
        """Token nella risposta grezza della scrittura, None se la risposta non ne ha."""
        for scambio in reversed(self.http):
            risposta = scambio.get("risposta")
            if scambio.get("stato") == 200 and isinstance(risposta, dict):
                token = risposta.get("Token")
                if isinstance(token, list):
                    return str(token[0]) if token else ""
                return None if token is None else str(token)
        return None

    def spiegazione(self) -> str:
        if self.ok:
            return "riuscita"
        return f"{self.errore}" + (f" [{self.fault}]" if self.fault else "")

    def limite(self) -> bool:
        """Rifiutata perché il controller è pieno (CapabilityViolated)."""
        return not self.ok and "CapabilityViolated" in self.fault


class Operatore:
    """Chi sta al banco: legge le istruzioni e risponde da tastiera."""

    interattivo = True

    def __init__(self, scrivi=print) -> None:
        self.scrivi = scrivi

    def chiedi(self, chiave: str, testo: str) -> str:
        self.scrivi(f"\n>>> {testo}")
        try:
            return input("    > ").strip()
        except EOFError:
            return ""


class SenzaOperatore(Operatore):
    """Nessuno al banco: le parti guidate si saltano."""

    interattivo = False

    def chiedi(self, chiave: str, testo: str) -> str:
        return ""


class Kit:
    def __init__(
        self,
        client,
        registro: Registratore,
        serie: str,
        operatore: Operatore,
        scrivi=print,
        quante: int = 500,
        tessera: str = "",
        pausa=asyncio.sleep,
        limiti: bool = False,
        tetti: dict[str, int] | None = None,
    ) -> None:
        self.client = client
        self.registro = registro
        self.permessi = registro.permessi
        self.serie = serie_pulita(serie)
        self.operatore = operatore
        self.scrivi = scrivi
        self.quante = quante
        self.tessera = tessera
        self.pausa = pausa
        # T18: ricerca dei limiti, con i tetti di TETTI cambiati da --tetto
        self.limiti = limiti
        self.tetti = {**TETTI, **(tetti or {})}
        # Migliaia di scritture (ricerca dei limiti): delle operazioni riuscite il rapporto tiene solo metodo e durata
        self.leggero = False
        self.file: Path | None = None
        self._numeri = itertools.count(1)
        self.dati: dict = {
            "kit": "prove_a1210",
            "inizio": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "serie_attesa": self.serie,
            "controller": {},
            "avvio": {"operazioni": [], "osservazioni": []},
            "prove": {},
            "fine": {"operazioni": [], "osservazioni": []},
        }
        self.sezione = self.dati["avvio"]

    # Chiamate al controller, tutte registrate

    def _controlla(self, metodo: str, args: tuple) -> None:
        if metodo in LETTURE:
            return
        if metodo in SCRITTURE_OGGETTO:
            oggetto = args[0]
            token, nome = oggetto.get("token"), oggetto.get("Name", PREFISSO)
        elif metodo in SCRITTURE_TOKEN:
            token, nome = args[0], PREFISSO
        else:
            raise Vietata(f"{metodo}: il kit non usa questo metodo")
        if not self.permessi.ammesso(token) or not str(nome).startswith(PREFISSO):
            raise Vietata(f"{metodo} su {token!r}: non è un oggetto del kit")

    def _registra(self, metodo: str, args: list, esito: Esito) -> None:
        valore = esito.valore
        if metodo.startswith("list_") and isinstance(valore, list):
            valore = {"quanti": len(valore)}
        if self.leggero and esito.ok:
            # Gli errori, e le risposte grezze di un CapabilityViolated, restano per intero
            voce = {"metodo": metodo, "esito": "riuscita", "durata_s": esito.durata}
            self.sezione["operazioni"].append(voce | ({"valore": valore} if metodo.startswith("list_") else {}))
            return
        self.sezione["operazioni"].append(
            {
                "metodo": metodo,
                "argomenti": copy.deepcopy(args),
                "esito": "riuscita" if esito.ok else ("irraggiungibile" if esito.irraggiungibile else "errore"),
                "valore": valore,
                "errore": esito.errore,
                "fault": esito.fault,
                "durata_s": esito.durata,
                "http": esito.http,
            }
        )

    async def tenta(self, metodo: str, *args) -> Esito:
        self._controlla(metodo, args)
        self.registro.prendi()
        inizio = time.monotonic()
        try:
            esito = Esito(ok=True, valore=await getattr(self.client, metodo)(*args))
        except vapix.VapixError as err:
            esito = Esito(
                ok=False,
                errore=str(err),
                fault=err.fault,
                irraggiungibile=isinstance(err, vapix.ControllerUnreachable),
            )
        esito.durata = round(time.monotonic() - inizio, 3)
        esito.http = self.registro.prendi()
        self._registra(metodo, list(args), esito)
        return esito

    async def grezza(self, percorso: str, metodo: str, argomenti: dict) -> Esito:
        """Richiesta VAPIX così com'è, per vedere la risposta senza le correzioni del client (passa dal
        Registratore come tutte le altre)."""
        if metodo not in LETTURE_HTTP | RIMOZIONI_HTTP:
            raise Vietata(f"{metodo}: il kit non lo manda grezzo")
        self.registro.prendi()
        inizio = time.monotonic()
        try:
            esito = Esito(ok=True, valore=await self.client._post(percorso, {metodo: argomenti}))
        except vapix.VapixError as err:
            esito = Esito(
                ok=False, errore=str(err), fault=err.fault, irraggiungibile=isinstance(err, vapix.ControllerUnreachable)
            )
        esito.durata = round(time.monotonic() - inizio, 3)
        esito.http = self.registro.prendi()
        self._registra(f"grezza {metodo}", [argomenti], esito)
        return esito

    async def crea(self, tipo: str, oggetto: dict) -> Esito:
        return await self.tenta(TIPI[tipo].scrivi, oggetto)

    async def elenco(self, tipo: str) -> list[dict] | None:
        esito = await self.tenta(TIPI[tipo].elenco)
        return esito.valore if esito.ok else None

    async def leggi(self, tipo: str, token: str) -> dict | None:
        if TIPI[tipo].leggi:
            esito = await self.tenta(TIPI[tipo].leggi, [token])
            oggetti = esito.valore if esito.ok else []
        else:
            oggetti = await self.elenco(tipo) or []
        return next((o for o in oggetti or [] if o.get("token") == token), None)

    # Rapporto

    def nota(self, testo: str) -> None:
        self.sezione["osservazioni"].append(testo)
        self.scrivi(f"  - {testo}")

    def salta(self, perche: str) -> None:
        self.sezione["esito"] = "saltata"
        self.nota(f"saltata: {perche}")

    def salva(self) -> None:
        if self.file is None:
            return
        self.dati["salvato"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
        temporaneo = self.file.with_suffix(".tmp")
        temporaneo.write_text(json.dumps(self.dati, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        temporaneo.replace(self.file)

    # Oggetti del kit

    def numero(self) -> str:
        """Numero di tessera del kit, diverso a ogni chiamata."""
        return f"{0xA1A00000 + next(self._numeri):08x}"

    def modello(self, tipo: str, token: str, rif: dict | None = None, **campi) -> dict:
        """Oggetto completo come lo scrive l'app; rif dice quali oggetti cita (per tipo)."""
        rif = {k: v for k, v in (rif or {}).items() if v}
        if tipo == "utente":
            oggetto = {"token": token, "Name": token, "Description": "", "Attribute": []}
        elif tipo == "tessera":
            profili = [rif["profilo"]] if rif.get("profilo") else []
            oggetto = {
                "token": token,
                "UserToken": rif.get("utente", ""),
                "Description": token,
                "Enabled": True,
                "Status": "Enabled",
                "ValidFrom": "",
                "ValidTo": "",
                "IdData": [{"Name": "Card", "Value": self.numero()}],
                "Attribute": [],
                "AuthenticationProfile": [],
                "CredentialAccessProfile": [{"AccessProfile": p, "ValidFrom": "", "ValidTo": ""} for p in profili],
            }
        elif tipo == "fascia":
            oggetto = {
                "token": token,
                "Name": token,
                "Description": "",
                "ScheduleDefinition": SETTIMANA,
                "ExceptionScheduleDefinition": None,
                "Attribute": [],
            }
        elif tipo == "profilo":
            fascia = rif.get("fascia") or "standard_always"
            oggetto = {
                "token": token,
                "Name": token,
                "Description": "",
                "Enabled": True,
                "Schedule": [fascia],
                "ValidFrom": "",
                "ValidTo": "",
                "AuthenticationProfile": [],
                "Attribute": [],
                "AccessPolicy": [
                    {"AccessPoint": p, "AuthorizationProfile": [], "Attribute": [], "Schedule": [fascia]}
                    for p in rif.get("punti") or []
                    if p
                ],
            }
        elif tipo == "porta":
            oggetto = {
                "token": token,
                "Name": token,
                "Description": "",
                "AccessTime": "PT7S",
                "OpenTooLongTime": "PT15S",
                "PreAlarmTime": "PT5S",
                "ExtendedAccessTime": "PT30S",
                "ExtendedOpenTooLongTime": "PT1M",
            }
        elif tipo == "lettore":
            oggetto = {
                "token": token,
                "Name": token,
                "Description": "",
                "Location": "",
                "Action": "Access",
                "Timeout": "PT10S",
                "MinPINSize": 4,
                "MaxPINSize": 4,
                "EndOfPIN": "#",
            }
        else:
            lettori = [rif["lettore"]] if rif.get("lettore") else []
            oggetto = {
                "token": token,
                "Name": token,
                "Description": "",
                "Entity": rif.get("porta", ""),
                "EntityType": "axtdc:Door",
                "Enabled": True,
                "DoorDeviceUUID": "",
                "IdPointDevice": [{"IdPoint": t, "DeviceUUID": ""} for t in lettori],
                "AuthenticationProfile": ["CardOnly"],
                "Attribute": [{"Name": "Direction", "Value": "in"}],
            }
        oggetto.update(campi)
        return oggetto

    async def catena(self, sigla: str) -> dict[str, str]:
        """Un oggetto per tipo, ognuno che cita i precedenti: tipo → token di quelli creati."""
        rif: dict = {}
        for tipo in ORDINE_CREAZIONE:
            if tipo == "tessera":
                continue
            token = f"{PREFISSO}{sigla}_{tipo}"
            if tipo == "profilo":
                riferimenti = {**rif, "punti": []}
            else:
                riferimenti = rif
            if (await self.crea(tipo, self.modello(tipo, token, riferimenti))).ok:
                rif[tipo] = token
        if rif.get("punto") and rif.get("profilo"):
            profilo = self.modello("profilo", rif["profilo"], {**rif, "punti": [rif["punto"]]})
            await self.crea("profilo", profilo)
        token = f"{PREFISSO}{sigla}_tessera"
        if (await self.crea("tessera", self.modello("tessera", token, rif))).ok:
            rif["tessera"] = token
        return rif

    async def nostri_sul_controller(self) -> tuple[dict[str, list[str]], list[str]]:
        """Oggetti del kit sul controller (tipo → token) ed elenchi che non si leggono."""
        trovati: dict[str, list[str]] = {}
        illeggibili = []
        for tipo in ORDINE_RIMOZIONE:
            oggetti = await self.elenco(tipo)
            if oggetti is None:
                illeggibili.append(tipo)
                continue
            trovati[tipo] = [str(o.get("token")) for o in oggetti if nostro(tipo, o) and o.get("token")]
        return trovati, illeggibili

    async def pulisci(self) -> dict:
        """Toglie tutti gli oggetti del kit, in ordine; dice cosa resta."""
        risultato: dict = {"tolti": [], "non_tolti": []}
        if not await self.identita():
            risultato["non_tolti"].append("controller non raggiungibile: non ho tolto niente")
            return risultato
        errori: dict[str, str] = {}
        # Più giri: un oggetto ancora citato si toglie dopo chi lo cita
        for _ in range(3):
            trovati, illeggibili = await self.nostri_sul_controller()
            da_togliere = [(tipo, token) for tipo in ORDINE_RIMOZIONE for token in trovati.get(tipo, [])]
            if not da_togliere:
                break
            spento = False
            for tipo, token in da_togliere:
                if not token.startswith(PREFISSO):
                    # Oggetto del kit con un token dato dal controller
                    self.permessi.extra.add(token)
                esito = await self.tenta(TIPI[tipo].togli, token)
                if esito.ok or "NotFound" in esito.fault:
                    risultato["tolti"].append(f"{tipo} {token}")
                    errori.pop(f"{tipo} {token}", None)
                else:
                    errori[f"{tipo} {token}"] = esito.spiegazione()
                if esito.irraggiungibile:
                    spento = True
                    break
            if spento:
                break
        trovati, illeggibili = await self.nostri_sul_controller()
        for tipo, tokens in trovati.items():
            for token in tokens:
                chiave = f"{tipo} {token}"
                risultato["non_tolti"].append(f"{chiave}: {errori.get(chiave, 'ancora presente')}")
        risultato["non_tolti"] += [f"elenco {t} non leggibile: non so se è rimasto qualcosa" for t in illeggibili]
        return risultato

    async def fotografia(self) -> dict:
        """Tutto quello che il kit sa leggere, solo lettura."""
        foto: dict = {}
        for nome, metodo in FOTO.items():
            esito = await self.tenta(metodo)
            valore = list(esito.valore) if isinstance(esito.valore, tuple) else esito.valore
            foto[nome] = valore if esito.ok else {"errore": esito.errore, "fault": esito.fault}
        return foto

    @staticmethod
    def estranei(foto: dict) -> dict:
        """La fotografia senza gli oggetti del kit."""
        rimasti = {}
        for nome, valore in foto.items():
            tipo = TIPO_DELLA_FOTO.get(nome)
            if isinstance(valore, list) and nome != "morsetti":
                valore = [
                    o
                    for o in valore
                    if not isinstance(o, dict)
                    or not (nostro(tipo, o) if tipo else str(o.get("token") or "").startswith(PREFISSO))
                ]
            rimasti[nome] = valore
        return rimasti

    async def identita(self) -> bool:
        """Il controller risponde ed è quello di banco. Uno diverso ferma tutto."""
        esito = await self.tenta("device_info")
        if not esito.ok:
            return False
        serie = serie_pulita((esito.valore or {}).get("SerialNumber", ""))
        if serie != self.serie:
            raise Rifiuto(f"il controller ha il numero di serie {serie or '(nessuno)'}, non {self.serie}")
        return True

    async def aspetta_controller(self, secondi: float) -> bool:
        limite = time.monotonic() + secondi
        while True:
            if await self.identita():
                return True
            if time.monotonic() > limite:
                return False
            await self.pausa(5)

    async def impronta(self) -> str:
        esito = await self.tenta("certificate_fingerprint")
        return str(esito.valore) if esito.ok else f"non letta: {esito.errore}"

    async def ultimo_evento(self) -> int:
        esito = await self.tenta("latest_events", 1)
        return max((int(r.get("rowid") or 0) for r in esito.valore or []), default=0) if esito.ok else 0

    async def eventi_dopo(self, riga: int) -> list[dict]:
        """Eventi del registro arrivati dopo la riga, in ordine (al massimo gli ultimi 100)."""
        esito = await self.tenta("latest_events", 100)
        righe = [r for r in (esito.valore or []) if int(r.get("rowid") or 0) > riga] if esito.ok else []
        return [riassunto_evento(r) for r in sorted(righe, key=lambda r: int(r.get("rowid") or 0))]

    async def passaggio(self, caso: str, testo: str, numero: str, punto: str) -> list[dict] | None:
        """L'operatore passa la tessera numero sul lettore del punto; restituisce gli eventi arrivati dopo, None se il
        passaggio non c'è stato. Prima rilegge tessere e profili e non lo chiede se una tessera con quel numero
        potrebbe aprire."""
        if not await self.non_apre(caso, numero, punto):
            return None
        riga = await self.ultimo_evento()
        risposta = self.operatore.chiedi(caso, f"{testo} Poi premi Invio (scrivi salta per non farlo).")
        if risposta.lower() == "salta":
            return None
        await self.pausa(3)
        eventi = await self.eventi_dopo(riga)
        self.sezione.setdefault("eventi", []).append({"caso": caso, "eventi": eventi})
        return eventi

    async def tessere_che_aprono(self, numero: str, punto: str) -> list[dict] | None:
        """Tessere con quel numero che potrebbero aprire il punto: non sospese (Enabled non false) e con un profilo che
        ha una regola per il punto, o che non si ritrova. None se qualcosa non si legge."""
        tessere = await self.elenco("tessera")
        if tessere is None:
            return None
        # Gli elenchi potrebbero non avere tutti i campi: tessere e profili si rileggono interi
        candidate = [str(t.get("token")) for t in tessere if "IdData" not in t or con_numero(t, numero)]
        lette: list[dict] = []
        if candidate:
            esito = await self.tenta("get_credentials", candidate)
            lette = list(esito.valore or []) if esito.ok else []
            if not esito.ok or {str(t.get("token")) for t in lette} != set(candidate):
                return None
        attive = [t for t in lette if con_numero(t, numero) and not sospesa(t)]
        legati = sorted({p for t in attive for p in profili_legati(t)})
        regole: dict[str, list] = {}
        if legati:
            esito = await self.tenta("get_access_profiles", legati)
            if not esito.ok:
                return None
            regole = {
                str(p.get("token")): [r.get("AccessPoint") for r in p.get("AccessPolicy") or [] if isinstance(r, dict)]
                for p in esito.valore or []
            }
        return [t for t in attive if any(p not in regole or punto in regole[p] for p in profili_legati(t))]

    async def non_apre(self, caso: str, numero: str, punto: str) -> bool:
        """Nessuna tessera con quel numero può aprire il punto. Se una del kit potrebbe, la sospende o la toglie e
        dice che il passaggio non si fa; se resta qualcosa che apre ferma la prova."""
        aprono = await self.tessere_che_aprono(numero, punto)
        if aprono is None:
            self.nota(f"{caso}: tessere o profili non letti, non so se la tessera {numero} apre: passaggio non fatto")
            return False
        if not aprono:
            return True
        for tessera in aprono:
            token = str(tessera.get("token"))
            come = await self.metti_al_sicuro(token) if nostro("tessera", tessera) else "non è del kit, non la tocco"
            self.nota(
                f"{caso}: ATTENZIONE, la tessera {token} con il numero {numero} poteva aprire "
                f"({stato_tessera(tessera)}, profili {', '.join(profili_legati(tessera))}): "
                f"passaggio non fatto ({come})"
            )
        if await self.tessere_che_aprono(numero, punto) != []:
            raise RuntimeError(f"una tessera con il numero {numero} può ancora aprire {punto}")
        return False

    async def metti_al_sicuro(self, token: str) -> str:
        """Tessera del kit che potrebbe aprire: DisableCredential, controllato rileggendola; se non resta sospesa la
        toglie. Dice cosa ha fatto. Se non riesce né a sospenderla né a toglierla ferma la prova (poi riprova la
        pulizia)."""
        if not token.startswith(PREFISSO):
            # Tessera del kit con un token dato dal controller (come in pulisci)
            self.permessi.extra.add(token)
        esito = await self.tenta("disable_credential", token)
        if esito.ok and sospesa(await self.leggi("tessera", token)):
            return "sospesa con DisableCredential"
        perche = esito.spiegazione() if not esito.ok else "riuscita, ma riletta non è sospesa"
        tolta = await self.tenta("remove_credential", token)
        if tolta.ok:
            return f"DisableCredential {perche}: tessera tolta"
        self.nota(f"ATTENZIONE: la tessera {token} può aprire e non si toglie ({tolta.spiegazione()})")
        raise RuntimeError(f"tessera {token} né sospesa né tolta: {tolta.spiegazione()}")

    async def set_sospende(self, sigla: str, utente: str, profilo: str) -> tuple[bool, str]:
        """SetCredential con Enabled false scrive davvero una tessera nuova sospesa, anche con un profilo che apre?
        Lo prova con una tessera del kit, che ha un numero che nessuno ha in mano, e la toglie subito; se no, dice
        perché."""
        prova = f"{PREFISSO}{sigla}_prova_sospesa"
        tessera = self.modello(
            "tessera", prova, {"utente": utente, "profilo": profilo}, Enabled=False, Status="Disabled"
        )
        esito = await self.crea("tessera", tessera)
        letta = await self.leggi("tessera", prova)
        await self.tenta("remove_credential", prova)
        if not esito.ok:
            return False, f"tessera di prova {prova} non scritta: {esito.spiegazione()}"
        if not sospesa(letta):
            return False, f"tessera di prova {prova} scritta sospesa e riletta con {stato_tessera(letta)}"
        return True, ""

    def descrivi_passaggio(self, caso: str, eventi: list[dict] | None) -> None:
        if eventi is None:
            self.nota(f"{caso}: tessera non passata")
            return
        richieste = [e for e in eventi if e["argomento"].startswith("IdPoint/Request/IdData")]
        negati = [e for e in eventi if e["argomento"].startswith("AccessControl/Denied")]
        concessi = [e for e in eventi if "Granted" in e["argomento"]]
        motivi = ", ".join(e["argomento"] + (f" Reason={e['Reason']}" if e.get("Reason") else "") for e in negati)
        self.nota(
            f"{caso}: IdData {'arrivato' if richieste else 'non arrivato'}; negato: {motivi or 'nessuno'}; "
            f"CredentialToken {'sì' if any('CredentialToken' in e for e in negati) else 'no'}; "
            f"CredentialHolderName {'sì' if any('CredentialHolderName' in e for e in negati) else 'no'}"
        )
        if concessi:
            self.nota(f"{caso}: ATTENZIONE, accesso concesso ({concessi[0]['argomento']})")

    async def lettore_vero(self, testo: str = "") -> dict | None:
        """Punto di accesso con un lettore, gestito dal controller e non del kit, per i passaggi di tessera e per il
        pulsante di uscita della sua porta; testo: la richiesta all'operatore se non c'è."""
        for tentativo in range(2):
            punti = await self.elenco("punto") or []
            controllori = (await self.tenta("list_access_controllers")).valore or []
            gestiti = {t for c in controllori for t in c.get("AccessPoint") or []}
            adatti = [
                p for p in punti if not nostro("punto", p) and p.get("token") in gestiti and p.get("IdPointDevice")
            ]
            adatti.sort(key=lambda p: {"Name": "Direction", "Value": "in"} not in (p.get("Attribute") or []))
            if adatti:
                return adatti[0]
            if tentativo or not self.operatore.interattivo:
                return None
            risposta = self.operatore.chiedi(
                "configura_lettore",
                testo
                or "Per questa prova serve un lettore di tessere configurato sul controller (porta e lettore, con la "
                "procedura guidata della pagina del controller). Configuralo e premi Invio, oppure scrivi salta.",
            )
            if risposta.lower() == "salta":
                return None
            self.sezione["lettore_configurato"] = True
        return None

    def numero_tessera(self) -> str:
        if not self.tessera and self.operatore.interattivo:
            self.tessera = self.operatore.chiedi(
                "numero_tessera",
                "Scrivi il numero della tessera che passerai sul lettore, come lo legge il controller (per esempio "
                "c0110b18); Invio per non passare tessere.",
            )
        return self.tessera

    # Svolgimento

    async def avvio(self) -> None:
        """Controlli prima di scrivere qualsiasi cosa: controller giusto e non in servizio."""
        self.scrivi("== Controlli prima di partire ==")
        esito = await self.tenta("device_info")
        if not esito.ok:
            raise Rifiuto(esito.spiegazione())
        info = esito.valore or {}
        self.dati["controller"] = {
            "modello": info.get("ProdNbr"),
            "firmware": info.get("Version"),
            "serie": info.get("SerialNumber"),
            "impronta_certificato": await self.impronta(),
        }
        self.nota(
            f"controller {info.get('ProdNbr')} firmware {info.get('Version')} serie {info.get('SerialNumber')}, "
            f"impronta del certificato {self.dati['controller']['impronta_certificato']}"
        )
        if serie_pulita(info.get("SerialNumber", "")) != self.serie:
            raise Rifiuto(
                f"il numero di serie del controller è {info.get('SerialNumber')!r}, non {self.serie!r}: "
                "non è il controller di banco indicato"
            )
        estranei = []
        for tipo in ("utente", "tessera"):
            oggetti = await self.elenco(tipo)
            if oggetti is None:
                raise Rifiuto(f"elenco {tipo} non leggibile: senza sapere cosa c'è il kit non scrive niente")
            estranei += [
                f"{tipo} {o.get('token')} ({o.get('Name') or o.get('Description') or ''})"
                for o in oggetti
                if not nostro(tipo, o) and o.get("token") not in DI_FABBRICA
            ]
        if estranei:
            raise Rifiuto(
                f"sul controller ci sono {len(estranei)} utenti o tessere che non sono del kit, come in un "
                f"controller in servizio: {', '.join(estranei[:10])}. Il kit va usato solo su un controller di "
                "banco resettato"
            )
        self.nota("nessun utente e nessuna tessera estranei: si può partire")
        pulizia = await self.pulisci()
        self.sezione["pulizia"] = pulizia
        if pulizia["tolti"]:
            self.nota(f"tolti {len(pulizia['tolti'])} oggetti aam_prova_ rimasti da una volta precedente")
        if pulizia["non_tolti"]:
            raise Rifiuto(f"restano oggetti del kit da una volta precedente: {', '.join(pulizia['non_tolti'])}")

    async def esegui_prova(self, codice: str) -> None:
        self.sezione = self.dati["prove"][codice] = {
            "titolo": TITOLI[codice],
            "esito": "fatta",
            "inizio": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "osservazioni": [],
            "operazioni": [],
        }
        self.scrivi(f"\n== {codice} · {TITOLI[codice]} ==")
        prima = None
        try:
            if not await self.identita():
                self.sezione["esito"] = "saltata"
                self.nota("saltata: il controller non risponde")
                return
            if codice != "T12":
                prima = self.estranei(await self.fotografia())
            await getattr(self, codice.lower())()
        except (Vietata, Rifiuto, asyncio.CancelledError, KeyboardInterrupt):
            self.sezione["esito"] = "interrotta"
            raise
        except Exception as err:
            self.sezione["esito"] = "errore"
            self.sezione["errore"] = traceback.format_exc()
            self.nota(f"errore del kit: {err!r} (dettagli nel file JSON)")
        finally:
            try:
                await self.dopo_la_prova(prima)
            finally:
                self.sezione["fine"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
                self.salva()

    async def dopo_la_prova(self, prima: dict | None) -> None:
        """Toglie gli oggetti della prova e controlla che gli altri siano rimasti com'erano."""
        try:
            self.sezione["pulizia"] = pulizia = await self.pulisci()
        except Rifiuto as err:
            self.sezione["pulizia"] = {"tolti": [], "non_tolti": [f"non ho tolto niente: {err}"]}
            raise
        if pulizia["non_tolti"]:
            self.nota(f"NON TOLTI: {'; '.join(pulizia['non_tolti'])}")
        if prima is None or not await self.identita():
            return
        dopo = self.estranei(await self.fotografia())
        cambiati = [k for k in prima if json.dumps(prima[k], default=str) != json.dumps(dopo.get(k), default=str)]
        self.sezione["estranei_cambiati"] = {k: {"prima": prima[k], "dopo": dopo.get(k)} for k in cambiati}
        if cambiati:
            perche = " (il lettore configurato dall'operatore: controllare che sia solo quello)"
            self.nota(
                f"ATTENZIONE: cambiati oggetti che non sono del kit: {', '.join(cambiati)}"
                + (perche if self.sezione.get("lettore_configurato") else "")
            )

    async def chiusura(self) -> None:
        self.sezione = self.dati["fine"]
        self.scrivi("\n== Pulizia finale ==")
        try:
            self.sezione["pulizia"] = pulizia = await self.pulisci()
        except Rifiuto as err:
            pulizia = {"tolti": [], "non_tolti": [f"non ho tolto niente: {err}"]}
            self.sezione["pulizia"] = pulizia
        if pulizia["non_tolti"]:
            self.nota("NON SONO RIUSCITO A TOGLIERE:")
            for riga in pulizia["non_tolti"]:
                self.nota(riga)
        else:
            self.nota("sul controller non resta niente del kit")
        self.scrivi("\n== Riepilogo ==")
        for codice, prova in self.dati["prove"].items():
            self.scrivi(f"{codice:<4} {prova['esito']:<10} {prova['titolo']}")
        if self.file:
            self.scrivi(f"\nRapporto completo: {self.file}")
        self.dati["fine"]["ora"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
        self.salva()

    # Le prove

    async def t1(self) -> None:
        """Il token scelto dall'app: l'oggetto nasce con quello e gli elenchi lo restituiscono uguale."""
        rif: dict = {}
        tabella: dict = {}
        for tipo in ORDINE_CREAZIONE:
            esiti = tabella[tipo] = {}
            for variante in ("corto", "40", "64"):
                if variante == "corto":
                    token = f"{PREFISSO}{tipo}_1_ab12"
                else:
                    token = lungo(f"{PREFISSO}{tipo}_{variante}_", int(variante))
                riferimenti = {**rif, "punti": []}
                mancano = {"tessera": ["utente"], "profilo": ["fascia"], "punto": ["porta", "lettore"]}.get(tipo, [])
                if any(m not in rif for m in mancano):
                    esiti[variante] = f"non provato: manca {', '.join(m for m in mancano if m not in rif)}"
                    continue
                oggetto = self.modello(tipo, token, riferimenti)
                esito = await self.crea(tipo, oggetto)
                if not esito.ok:
                    esiti[variante] = f"rifiutato: {esito.spiegazione()}"
                    continue
                risposta = esito.token_risposta()
                letto = await self.leggi(tipo, token)
                elenco = await self.elenco(tipo) or []
                nell_elenco = [o for o in elenco if o.get("token") == token]
                altri = [o.get("token") for o in elenco if o.get("token") != token and self._stesso(tipo, o, oggetto)]
                if letto and nell_elenco:
                    rif.setdefault(tipo, token)
                    esiti[variante] = "accettato: nasce con quel token e GetXList lo restituisce uguale"
                elif altri:
                    esiti[variante] = f"creato con un altro token: {', '.join(map(str, altri))}"
                else:
                    esiti[variante] = "accettato ma non ritrovato negli elenchi"
                if risposta not in (None, token):
                    esiti[variante] += f"; la risposta dà il token {risposta!r}"
                elif risposta is None:
                    esiti[variante] += "; la risposta non contiene il token"
                self.nota(f"{tipo}, token di {len(token)} caratteri: {esiti[variante]}")
        self.sezione["tabella"] = tabella
        buoni = [t for t, esiti in tabella.items() if all(e.startswith("accettato: ") for e in esiti.values())]
        self.nota(f"tipi che accettano il token dell'app fino a 64 caratteri: {', '.join(buoni) or 'nessuno'}")
        # Una configurazione creata da sola legherebbe la porta o il lettore del kit a relè e morsetti
        for tipo, metodo in (("porta", "list_door_configurations"), ("lettore", "list_idpoint_configurations")):
            esito = await self.tenta(metodo)
            if esito.ok:
                proprie = [str(c.get("token")) for c in esito.valore or [] if str(c.get("token")).startswith(PREFISSO)]
                self.nota(
                    f"configurazioni create dal controller per {tipo} aam_prova_: {', '.join(proprie) or 'nessuna'}"
                )
            else:
                self.nota(f"configurazioni di {tipo} non lette: {esito.spiegazione()}")
        self.nota("non provato il token Axis-<serie di un altro apparecchio>: il kit scrive solo token aam_prova_")

    @staticmethod
    def _stesso(tipo: str, letto: dict, mandato: dict) -> bool:
        if tipo == "tessera":
            return letto.get("Description") == mandato.get("Description") or letto.get("IdData") == mandato.get(
                "IdData"
            )
        return letto.get("Name") == mandato.get("Name")

    async def t2(self) -> None:
        """Lo stesso Set mandato due volte, con una scrittura in tempo scaduto in mezzo."""
        if not self.operatore.interattivo:
            return self.salta("serve l'operatore per staccare e riattaccare il cavo")
        rif: dict = {}
        oggetti: dict[str, dict] = {}
        for tipo in ORDINE_CREAZIONE:
            token = f"{PREFISSO}t2_{tipo}"
            oggetto = self.modello(tipo, token, {**rif, "punti": [rif["punto"]] if "punto" in rif else []})
            esito = await self.crea(tipo, oggetto)
            if esito.ok:
                rif[tipo], oggetti[tipo] = token, oggetto
            else:
                self.nota(f"{tipo}: prima scrittura rifiutata ({esito.spiegazione()}), lo salto")
        if not oggetti:
            return self.salta("nessun oggetto creato")
        risposta = self.operatore.chiedi(
            "stacca_cavo",
            "Stacca ora il cavo di rete dal controller (non dal PC) e premi Invio. Il kit rimanda le stesse scritture: "
            "devono andare in tempo scaduto, fino a 10 secondi l'una. Scrivi salta per non farlo.",
        )
        if risposta.lower() == "salta":
            return self.salta("cavo non staccato")
        for tipo, oggetto in oggetti.items():
            esito = await self.crea(tipo, oggetto)
            if esito.ok:
                self.nota(f"{tipo}: con il cavo staccato la scrittura è riuscita (il cavo era ancora collegato?)")
            else:
                self.nota(f"{tipo}: con il cavo staccato {esito.spiegazione()} dopo {esito.durata} s")
        self.operatore.chiedi(
            "riattacca_cavo", "Riattacca il cavo e premi Invio: il kit aspetta che il controller risponda di nuovo."
        )
        if not await self.aspetta_controller(180):
            raise RuntimeError("il controller non risponde 3 minuti dopo aver riattaccato il cavo")
        for tipo, oggetto in oggetti.items():
            esito = await self.crea(tipo, oggetto)
            elenco = await self.elenco(tipo) or []
            stessi = [o for o in elenco if o.get("token") == oggetto["token"]]
            simili = [o for o in elenco if self._stesso(tipo, o, oggetto)]
            letto = stessi[0] if stessi else None
            self.nota(
                f"{tipo}: seconda scrittura {esito.spiegazione()}; con quel token {len(stessi)}, uguali in tutto "
                f"{len(simili)}; differenze dal mandato: {json.dumps(confronta(oggetto, letto), ensure_ascii=False)}"
            )

    async def t3(self) -> None:
        """Set su un token esistente con campi omessi: sostituisce, unisce o rimette i valori di serie?"""
        rif: dict = {}
        omessi = {
            "utente": {"Description": "aam_prova descrizione", "Attribute": [{"Name": "aam_prova", "Value": "1"}]},
            "fascia": {"Description": "aam_prova descrizione", "ExceptionScheduleDefinition": FESTE},
            "tessera": {"Attribute": [{"Name": "aam_prova", "Value": "1"}], "ValidTo": "2030-12-31T23:59:59Z"},
            "profilo": {"Description": "aam_prova descrizione", "ValidTo": "2030-12-31T23:59:59Z"},
            "porta": {"ExtendedAccessTime": "PT30S", "ExtendedOpenTooLongTime": "PT1M"},
            "lettore": {"Location": "aam_prova banco", "Timeout": "PT7S"},
            "punto": {"Attribute": [{"Name": "Direction", "Value": "in"}], "AuthenticationProfile": ["CardOnly"]},
        }
        for tipo in ORDINE_CREAZIONE:
            token = f"{PREFISSO}t3_{tipo}"
            completo = self.modello(tipo, token, {**rif, "punti": []}, **omessi[tipo])
            esito = await self.crea(tipo, completo)
            if not esito.ok:
                self.nota(f"{tipo}: oggetto completo rifiutato ({esito.spiegazione()})")
                continue
            rif[tipo] = token
            primo = await self.leggi(tipo, token)
            ridotto = {k: v for k, v in completo.items() if k not in omessi[tipo]}
            esito = await self.crea(tipo, ridotto)
            letto = await self.leggi(tipo, token)
            if not esito.ok or letto is None:
                self.nota(f"{tipo}: scrittura senza {', '.join(omessi[tipo])} {esito.spiegazione()}")
                continue
            for chiave, valore in omessi[tipo].items():
                if chiave not in letto:
                    come = "tolta (il controller sostituisce l'oggetto)"
                elif letto[chiave] == valore:
                    come = "tenuta com'era (il controller unisce)"
                else:
                    come = f"rimessa a {letto[chiave]!r} (valore di serie?)"
                self.nota(f"{tipo} senza {chiave}: {come}")
            self.sezione.setdefault("letti", {})[tipo] = {"dopo_il_completo": primo, "dopo_il_ridotto": letto}
        self.nota(
            "non fatti DoorConfiguration e IdPointConfiguration parziali: il kit non scrive le configurazioni di "
            "porte e lettori (vanno con T13 e T14)"
        )

    async def t4(self) -> None:
        """SetCredential con Enabled false e Status Disabled equivale a DisableCredential?"""
        utente = f"{PREFISSO}t4_utente"
        await self.crea("utente", self.modello("utente", utente))
        a, b = f"{PREFISSO}t4_tessera_a", f"{PREFISSO}t4_tessera_b"
        tessera_a = self.modello("tessera", a, {"utente": utente})
        tessera_b = self.modello("tessera", b, {"utente": utente})
        for tessera in (tessera_a, tessera_b):
            esito = await self.crea("tessera", tessera)
            if not esito.ok:
                raise RuntimeError(f"tessera {tessera['token']} non creata: {esito.spiegazione()}")
        prima = await self.leggi("tessera", a)
        await self.tenta("disable_credential", a)
        spenta_a = await self.leggi("tessera", a)
        await self.tenta("enable_credential", a)
        accesa_a = await self.leggi("tessera", a)
        await self.crea("tessera", {**tessera_b, "Enabled": False, "Status": "Disabled"})
        spenta_b = await self.leggi("tessera", b)
        await self.crea("tessera", {**tessera_b, "Enabled": True, "Status": "Enabled"})
        accesa_b = await self.leggi("tessera", b)
        stato = ("Enabled", "Status", "Reason")
        for nome, x, y in (("sospesa", spenta_a, spenta_b), ("riattivata", accesa_a, accesa_b)):
            vx = {k: (x or {}).get(k) for k in stato}
            vy = {k: (y or {}).get(k) for k in stato}
            uguale = "uguali" if vx == vy else "DIVERSE"
            self.nota(f"{nome}: con Disable/EnableCredential {vx}, con SetCredential {vy}: {uguale}")
        self.sezione["letti"] = {
            "prima": prima,
            "disable": spenta_a,
            "enable": accesa_a,
            "set_spenta": spenta_b,
            "set_accesa": accesa_b,
        }
        self.nota(
            f"campi cambiati da DisableCredential: {json.dumps(confronta(prima or {}, spenta_a), ensure_ascii=False)}"
        )
        if not self.operatore.interattivo:
            return
        punto = await self.lettore_vero()
        numero = self.numero_tessera() if punto else ""
        if not punto or not numero:
            self.nota("passaggi di tessera non fatti: servono un lettore configurato e il numero di una tessera")
            return
        # La tessera vera non apre mai: il passaggio si chiede solo con la tessera riletta sospesa (e passaggio()
        # ricontrolla tessere e profili subito prima)
        await self.t4_sospesa_con_set(utente, punto, numero)
        await self.t4_sospesa_con_disable(utente, punto, numero)

    async def t4_sospesa_con_set(self, utente: str, punto: dict, numero: str) -> None:
        """Primo passaggio: la tessera vera scritta sospesa con SetCredential e con un profilo che aprirebbe la porta
        del banco, così il Denied parla solo della sospensione."""
        caso = "sospesa con SetCredential"
        fasce = [s.get("token") for s in await self.elenco("fascia") or []]
        fascia = "standard_always" if "standard_always" in fasce else f"{PREFISSO}t4_sempre"
        if fascia != "standard_always":
            await self.crea("fascia", self.modello("fascia", fascia, ScheduleDefinition=SEMPRE))
        profilo = f"{PREFISSO}t4_profilo"
        esito = await self.crea(
            "profilo", self.modello("profilo", profilo, {"fascia": fascia, "punti": [punto["token"]]})
        )
        if not esito.ok:
            self.nota(f"{caso}: profilo {profilo} non creato ({esito.spiegazione()}): passaggio non fatto")
            return
        # Se SetCredential non sospende una tessera di prova scritta allo stesso modo, la tessera vera con il profilo
        # non si scrive nemmeno: non c'è un momento in cui apre
        sospende, perche = await self.set_sospende("t4", utente, profilo)
        if not sospende:
            self.nota(f"{caso}: SetCredential non sospende: passaggio non fatto ({perche})")
            return
        vera = f"{PREFISSO}t4_tessera_vera"
        tessera = self.modello(
            "tessera",
            vera,
            {"utente": utente, "profilo": profilo},
            IdData=[{"Name": "Card", "Value": numero}],
            Enabled=False,
            Status="Disabled",
        )
        esito = await self.crea("tessera", tessera)
        letta = await self.leggi("tessera", vera)
        if not esito.ok or not sospesa(letta):
            sicura = await self.metti_al_sicuro(vera)
            if esito.ok:
                self.nota(
                    f"{caso}: SetCredential non sospende: passaggio non fatto (tessera vera riletta con "
                    f"{stato_tessera(letta)}; {sicura})"
                )
            else:
                self.nota(f"{caso}: tessera vera non scritta ({esito.spiegazione()}): passaggio non fatto ({sicura})")
            return
        lettore = punto.get("Name") or punto["token"]
        invito = f"Passa la tessera {numero} sul lettore di {lettore}."
        self.descrivi_passaggio(caso, await self.passaggio("t4_set_disabled", invito, numero, punto["token"]))

    async def t4_sospesa_con_disable(self, utente: str, punto: dict, numero: str) -> None:
        """Secondo passaggio: una tessera nuova con il numero vero, nata abilitata ma senza nessun profilo, poi sospesa
        con DisableCredential. Non si riscrive quella del primo passaggio: SetCredential potrebbe tenerle il profilo
        (T3). Quella va tolta prima: con due tessere con lo stesso numero (T5) non si sa quale legge il controller."""
        caso = "sospesa con DisableCredential, senza profilo (confronta con T8)"
        tolta = await self.tenta("remove_credential", f"{PREFISSO}t4_tessera_vera")
        altre = await self.tessere_con_numero(numero)
        if not tolta.ok or altre is None or altre:
            if not tolta.ok:
                perche = f"la tessera del primo passaggio non è tolta ({tolta.spiegazione()})"
            elif altre is None:
                perche = "tessere non lette"
            else:
                perche = f"sul controller c'è ancora una tessera con il numero {numero}: {', '.join(altre)}"
            self.nota(f"{caso}: {perche}: passaggio non fatto")
            return
        seconda = f"{PREFISSO}t4_vera_senza_profilo"
        esito = await self.crea(
            "tessera", self.modello("tessera", seconda, {"utente": utente}, IdData=[{"Name": "Card", "Value": numero}])
        )
        if not esito.ok:
            self.nota(f"{caso}: tessera non scritta ({esito.spiegazione()}): passaggio non fatto")
            return
        esito = await self.tenta("disable_credential", seconda)
        letta = await self.leggi("tessera", seconda)
        if not esito.ok or not sospesa(letta):
            perche = esito.spiegazione() if not esito.ok else f"riuscita, ma riletta con {stato_tessera(letta)}"
            self.nota(f"{caso}: DisableCredential {perche}: passaggio non fatto")
            return
        lettore = punto.get("Name") or punto["token"]
        invito = f"Passa di nuovo la tessera {numero} sul lettore di {lettore}."
        self.descrivi_passaggio(caso, await self.passaggio("t4_disable", invito, numero, punto["token"]))

    async def tessere_con_numero(self, numero: str) -> list[str] | None:
        """Token delle tessere con quel numero sul controller; None se l'elenco non si legge."""
        tessere = await self.elenco("tessera")
        if tessere is None:
            return None
        return [str(t.get("token")) for t in tessere if con_numero(t, numero)]

    async def t5(self) -> None:
        """Una tessera con un numero già usato da un'altra credenziale: rifiuto o doppione?"""
        utenti = [f"{PREFISSO}t5_utente_1", f"{PREFISSO}t5_utente_2"]
        for utente in utenti:
            await self.crea("utente", self.modello("utente", utente))
        numero = self.numero()
        casi = [
            ("prima tessera", "a", utenti[0], numero),
            ("stesso numero, stesso utente", "b", utenti[0], numero),
            ("stesso numero, altro utente", "c", utenti[1], numero),
            ("stesso numero in maiuscolo", "d", utenti[1], numero.upper()),
        ]
        for caso, sigla, utente, valore in casi:
            tessera = self.modello(
                "tessera",
                f"{PREFISSO}t5_tessera_{sigla}",
                {"utente": utente},
                IdData=[{"Name": "Card", "Value": valore}],
            )
            esito = await self.crea("tessera", tessera)
            self.nota(f"{caso}: {'accettata' if esito.ok else 'rifiutata: ' + esito.spiegazione()}")
        tessere = await self.elenco("tessera") or []
        stesse = [
            t.get("token")
            for t in tessere
            if any(str(d.get("Value", "")).lower() == numero for d in t.get("IdData") or [])
        ]
        self.nota(f"tessere con il numero {numero} sul controller: {len(stesse)} ({', '.join(map(str, stesse))})")

    async def t6(self) -> None:
        """Riferimenti a oggetti che non ci sono, e rimozione di oggetti ancora citati."""
        assente = f"{PREFISSO}t6_assente"
        base = await self.catena("t6_base")
        creazioni = [
            ("tessera con un utente che non c'è", "tessera", {"utente": assente}),
            ("tessera con un profilo che non c'è", "tessera", {"utente": base.get("utente"), "profilo": assente}),
            ("profilo con un punto che non c'è", "profilo", {"fascia": base.get("fascia"), "punti": [assente]}),
            ("profilo con una fascia che non c'è", "profilo", {"fascia": assente, "punti": []}),
            ("punto con una porta che non c'è", "punto", {"porta": assente, "lettore": base.get("lettore")}),
            ("punto con un lettore che non c'è", "punto", {"porta": base.get("porta"), "lettore": assente}),
        ]
        for numero, (caso, tipo, rif) in enumerate(creazioni, 1):
            token = f"{PREFISSO}t6_rif_{numero}_{tipo}"
            esito = await self.crea(tipo, self.modello(tipo, token, rif))
            letto = await self.leggi(tipo, token) if esito.ok else None
            if esito.ok:
                self.nota(f"{caso}: accettato; letto: {json.dumps(letto, ensure_ascii=False)}")
            else:
                self.nota(f"{caso}: rifiutato ({esito.spiegazione()})")
        rimozioni = [
            ("fascia in uso da un profilo", "fascia", "profilo"),
            ("profilo in uso da una tessera", "profilo", "tessera"),
            ("punto in uso da un profilo", "punto", "profilo"),
            ("porta in uso da un punto", "porta", "punto"),
            ("lettore in uso da un punto", "lettore", "punto"),
            ("utente in uso da una tessera", "utente", "tessera"),
        ]
        for numero, (caso, tipo, chi_cita) in enumerate(rimozioni, 1):
            rif = await self.catena(f"t6_rim_{numero}")
            if tipo not in rif or chi_cita not in rif:
                self.nota(f"{caso}: non provato, la catena di oggetti non si è creata")
                continue
            esito = await self.tenta(TIPI[tipo].togli, rif[tipo])
            dopo = await self.leggi(chi_cita, rif[chi_cita])
            if esito.ok:
                self.nota(f"{caso}: tolto; {chi_cita} che lo citava ora è {json.dumps(dopo, ensure_ascii=False)}")
            else:
                self.nota(f"{caso}: rifiutato ({esito.spiegazione()})")

    async def t7(self) -> None:
        """Remove di oggetti che non ci sono: che codice risponde il controller?"""
        nomi = {
            "utente": "un utente",
            "tessera": "una tessera",
            "fascia": "una fascia",
            "profilo": "un profilo",
            "porta": "una porta",
            "lettore": "un lettore",
            "punto": "un punto di accesso",
        }
        richieste = [(tipo, nomi[tipo], t.percorso, t.rimozione) for tipo, t in TIPI.items()]
        richieste += [
            ("porta", "una configurazione di porta", "/vapix/doorcontrol", "axtdc:RemoveDoorConfiguration"),
            ("lettore", "una configurazione di lettore", "/vapix/idpoint", "axtid:RemoveIdPointConfiguration"),
        ]
        for tipo, nome, percorso, metodo in richieste:
            esito = await self.grezza(percorso, metodo, {"Token": [f"{PREFISSO}t7_assente_{tipo}"]})
            stato = next((s.get("stato") for s in esito.http if s.get("stato") != 401), None)
            self.nota(f"{metodo} di {nome} che non c'è: {esito.spiegazione()} (HTTP {stato})")
        for metodo in ("enable_credential", "disable_credential"):
            esito = await self.tenta(metodo, f"{PREFISSO}t7_assente_tessera")
            self.nota(f"{metodo} di una tessera che non c'è: {esito.spiegazione()}")

    async def t8(self) -> None:
        """Tessera presente ma non abilitata: arriva l'IdData? Con quale Reason il Denied?"""
        if not self.operatore.interattivo:
            return self.salta("serve l'operatore per passare la tessera")
        punto = await self.lettore_vero()
        if punto is None:
            return self.salta("nessun lettore configurato")
        numero = self.numero_tessera()
        if not numero:
            return self.salta("nessun numero di tessera")
        lettore = punto.get("Name") or punto["token"]
        self.nota(f"lettore del punto {lettore} ({punto['token']}), tessera {numero}")
        altro = await self.catena("t8_altro")
        utente = f"{PREFISSO}t8_titolare"
        await self.crea("utente", self.modello("utente", utente))
        fasce = [s.get("token") for s in await self.elenco("fascia") or []]
        fascia = "standard_always" if "standard_always" in fasce else altro.get("fascia")
        profili = {
            "spento": self.modello("profilo", f"{PREFISSO}t8_spento", {"fascia": fascia}, Enabled=False),
            "altro_punto": self.modello(
                "profilo", f"{PREFISSO}t8_altro_punto", {"fascia": fascia, "punti": [altro.get("punto")]}
            ),
            "giusto": self.modello("profilo", f"{PREFISSO}t8_giusto", {"fascia": fascia, "punti": [punto["token"]]}),
        }
        for profilo in profili.values():
            await self.crea("profilo", profilo)
        token = f"{PREFISSO}t8_tessera"
        dati = {"IdData": [{"Name": "Card", "Value": numero}]}
        invito = f"Passa la tessera {numero} sul lettore di {lettore}."
        casi = [
            ("profilo spento senza regole", profili["spento"]["token"], True),
            ("profilo senza regola per quel punto", profili["altro_punto"]["token"], True),
            ("CredentialAccessProfile vuoto", "", True),
            ("credenziale disabilitata", profili["giusto"]["token"], False),
        ]
        for caso, profilo, abilitata in casi:
            tessera = self.modello("tessera", token, {"utente": utente, "profilo": profilo}, **dati)
            if not abilitata:
                # Profilo giusto: la tessera si scrive da capo già sospesa, e solo se SetCredential sospende davvero
                # una tessera di prova scritta allo stesso modo, così non c'è un momento in cui apre
                sospende, perche = await self.set_sospende("t8", utente, profilo)
                if not sospende:
                    self.nota(f"{caso}: SetCredential non sospende: passaggio non fatto ({perche})")
                    continue
                tolta = await self.tenta("remove_credential", token)
                if not tolta.ok:
                    self.nota(f"{caso}: tessera del caso prima non tolta ({tolta.spiegazione()}): passaggio non fatto")
                    continue
                tessera.update(Enabled=False, Status="Disabled")
            esito = await self.crea("tessera", tessera)
            if not esito.ok:
                sicura = f" ({await self.metti_al_sicuro(token)})" if not abilitata else ""
                self.nota(f"{caso}: tessera non scritta ({esito.spiegazione()}){sicura}")
                continue
            if not abilitata:
                esito = await self.tenta("disable_credential", token)
                if not esito.ok:
                    sicura = await self.metti_al_sicuro(token)
                    self.nota(f"{caso}: DisableCredential {esito.spiegazione()}: passaggio non fatto ({sicura})")
                    continue
            self.descrivi_passaggio(caso, await self.passaggio(caso, invito, numero, punto["token"]))
        self.nota("nessun caso con la tessera abilitata e un profilo che apre: il kit non fa aprire porte")

    async def t9(self) -> None:
        """Festività tolte con null o stringa vuota, testo delle fasce restituito uguale, lunghezze massime."""
        token = f"{PREFISSO}t9_fascia"
        fascia = self.modello("fascia", token, ExceptionScheduleDefinition=FESTE)
        esito = await self.crea("fascia", fascia)
        if not esito.ok:
            raise RuntimeError(f"fascia non creata: {esito.spiegazione()}")
        letta = await self.leggi("fascia", token) or {}
        for chiave in ("ScheduleDefinition", "ExceptionScheduleDefinition"):
            if letta.get(chiave) == fascia[chiave]:
                self.nota(f"{chiave}: restituita identica")
            else:
                self.nota(f"{chiave}: DIVERSA, scritta {fascia[chiave]!r}, letta {letta.get(chiave)!r}")
        for nome, valore in (("stringa vuota", ""), ("null", None), ("chiave omessa", ...)):
            await self.crea("fascia", fascia)
            ridotta = {k: v for k, v in fascia.items() if k != "ExceptionScheduleDefinition"}
            if valore is not ...:
                ridotta["ExceptionScheduleDefinition"] = valore
            esito = await self.crea("fascia", ridotta)
            letta = await self.leggi("fascia", token) or {}
            if not esito.ok:
                self.nota(f"festività con {nome}: rifiutata ({esito.spiegazione()})")
            elif "ExceptionScheduleDefinition" not in letta:
                self.nota(f"festività con {nome}: tolte, chiave assente nella lettura")
            elif letta["ExceptionScheduleDefinition"] == FESTE:
                self.nota(f"festività con {nome}: RESTANO")
            else:
                self.nota(f"festività con {nome}: letta {letta['ExceptionScheduleDefinition']!r}")
        for campo in ("Name", "Description"):
            massimo = None
            for lunghezza in (64, 65, 128, 129, 255, 256, 257, 1024):
                prova = f"{PREFISSO}t9_{campo.lower()}_{lunghezza}"
                valore = lungo(f"{PREFISSO}{campo.lower()}_", lunghezza)
                esito = await self.crea("fascia", self.modello("fascia", prova, **{campo: valore}))
                letta = await self.leggi("fascia", prova) if esito.ok else None
                if esito.ok and letta is not None and letta.get(campo) == valore:
                    massimo = lunghezza
                    risultato = "accettato e letto intero"
                elif esito.ok:
                    risultato = f"accettato ma letto di {len(str((letta or {}).get(campo) or ''))} caratteri"
                else:
                    risultato = f"rifiutato ({esito.spiegazione()})"
                self.nota(f"{campo} di {lunghezza} caratteri: {risultato}")
            self.nota(f"{campo}: lunghezza più grande letta intera {massimo}")

    async def t10(self) -> None:
        """Valori scritti e come tornano: date, numeri di tessera, durate, ordine, campi vuoti, chiavi aggiunte."""
        utente = f"{PREFISSO}t10_utente"
        mandato_utente = self.modello(
            "utente", utente, Attribute=[{"Name": "b_aam", "Value": "2"}, {"Name": "a_aam", "Value": "1"}]
        )
        await self.crea("utente", mandato_utente)
        self.nota(
            f"utente: {json.dumps(confronta(mandato_utente, await self.leggi('utente', utente)), ensure_ascii=False)}"
        )
        date = {
            "z": ("2026-01-01T00:00:00Z", "2030-12-31T23:59:59Z"),
            "piu_zero": ("2026-01-01T00:00:00+00:00", "2030-12-31T23:59:59+00:00"),
            "piu_uno": ("2026-01-01T01:00:00+01:00", "2031-01-01T00:59:59+01:00"),
        }
        for sigla, (inizio, fine) in date.items():
            token = f"{PREFISSO}t10_validita_{sigla}"
            tessera = self.modello("tessera", token, {"utente": utente}, ValidFrom=inizio, ValidTo=fine)
            esito = await self.crea("tessera", tessera)
            letta = await self.leggi("tessera", token) if esito.ok else None
            if letta is None:
                self.nota(f"validità {inizio} - {fine}: {esito.spiegazione()}")
                continue
            self.nota(
                f"validità scritta {inizio} - {fine}, letta {letta.get('ValidFrom')!r} - {letta.get('ValidTo')!r}"
            )
        for sigla, numero in (("maiuscolo", self.numero().upper()), ("minuscolo", self.numero())):
            token = f"{PREFISSO}t10_numero_{sigla}"
            tessera = self.modello("tessera", token, {"utente": utente}, IdData=[{"Name": "Card", "Value": numero}])
            esito = await self.crea("tessera", tessera)
            letta = await self.leggi("tessera", token) if esito.ok else None
            self.nota(
                f"numero di tessera {numero}: letto {(letta or {}).get('IdData')!r}"
                if letta
                else f"numero di tessera {numero}: {esito.spiegazione()}"
            )
            if letta:
                self.nota(f"tessera: {json.dumps(confronta(tessera, letta), ensure_ascii=False)}")
        durate = {
            "AccessTime": "PT7S",
            "ExtendedAccessTime": "PT7.000S",
            "OpenTooLongTime": "PT1M",
            "PreAlarmTime": "PT0.5S",
        }
        porta = f"{PREFISSO}t10_porta"
        mandata = self.modello("porta", porta, **durate)
        esito = await self.crea("porta", mandata)
        letta = await self.leggi("porta", porta) if esito.ok else None
        for chiave, valore in durate.items():
            self.nota(f"durata {chiave} scritta {valore}, letta {(letta or {}).get(chiave)!r}")
        self.nota(f"porta: {json.dumps(confronta(mandata, letta), ensure_ascii=False)}")
        rif = {"porta": porta} if letta else {}
        altri = [
            ("lettore", {"Timeout": "PT3.000S"}),
            ("fascia", {}),
            ("profilo", {}),
            ("punto", {"Attribute": [{"Name": "Direction", "Value": "in"}, {"Name": "a_aam", "Value": "1"}]}),
        ]
        for tipo, campi in altri:
            if tipo == "punto" and not ("porta" in rif and "lettore" in rif):
                self.nota("punto: non provato, mancano porta o lettore")
                continue
            token = f"{PREFISSO}t10_{tipo}"
            mandato = self.modello(tipo, token, rif, **campi)
            esito = await self.crea(tipo, mandato)
            if not esito.ok:
                self.nota(f"{tipo}: {esito.spiegazione()}")
                continue
            rif[tipo] = token
            self.nota(f"{tipo}: {json.dumps(confronta(mandato, await self.leggi(tipo, token)), ensure_ascii=False)}")
        self.nota("non provato l'ordine di Configuration: il kit non scrive le configurazioni di porte e lettori")

    async def t11(self) -> None:
        """Elenchi senza oggetti: la chiave c'è vuota o manca?"""
        for nome, percorso, metodo, chiave in ELENCHI_T11:
            letture = [await self.grezza(percorso, metodo, {"Limit": vapix.CREDENTIAL_PAGE_SIZE}) for _ in range(2)]
            prima = letture[0]
            if not prima.ok:
                self.nota(f"{nome} ({metodo}): {prima.spiegazione()}")
                continue
            risposta = prima.valore if isinstance(prima.valore, dict) else {}
            uguali = json.dumps(prima.valore, sort_keys=True) == json.dumps(letture[1].valore, sort_keys=True)
            if chiave not in risposta:
                come = "chiave ASSENTE"
            elif risposta[chiave] in ([], None):
                come = f"chiave presente con {json.dumps(risposta[chiave])}"
            else:
                come = f"non vuoto, oggetti: {len(risposta[chiave])}; per questo elenco la prova non dice niente"
            altre = sorted(k for k in risposta if k != chiave)
            self.nota(
                f"{nome}: {come}; altre chiavi {altre or 'nessuna'}; "
                f"seconda lettura {'uguale' if uguali else 'DIVERSA'}"
            )

    async def t12(self) -> None:
        """Reset di fabbrica fatto dall'operatore, poi fotografia (solo lettura) degli oggetti di serie."""
        if not self.operatore.interattivo:
            return self.salta("il reset lo fa l'operatore dalla pagina del controller")
        prima = {"impronta": await self.impronta(), "ora": (await self.tenta("date_time_info")).valore}
        risposta = self.operatore.chiedi(
            "reset",
            "Fai ora il reset di fabbrica dalla pagina web del controller: il kit non lo fa. Dopo il reset crea di "
            "nuovo l'account con lo stesso nome utente e la stessa password e rimetti lo stesso indirizzo, poi premi "
            "Invio. Il kit dopo legge soltanto. Scrivi salta per non fare il reset.",
        )
        if risposta.lower() == "salta":
            return self.salta("reset non fatto")
        if not await self.aspetta_controller(900):
            raise RuntimeError("il controller non risponde 15 minuti dopo il reset (stessa password?)")
        dopo = {"impronta": await self.impronta(), "ora": (await self.tenta("date_time_info")).valore}
        foto = await self.fotografia()
        self.sezione["prima_del_reset"], self.sezione["dopo_il_reset"] = prima, dopo
        self.sezione["oggetti_di_serie"] = foto
        cambiata = "CAMBIATA" if prima["impronta"] != dopo["impronta"] else "uguale"
        self.nota(f"impronta del certificato {cambiata}: prima {prima['impronta']}, dopo {dopo['impronta']}")
        self.nota(f"ora e fuso dopo il reset: {json.dumps(dopo['ora'], ensure_ascii=False)}")
        for nome, valore in foto.items():
            if isinstance(valore, list) and nome != "morsetti":
                tokens = [str(o.get("token")) for o in valore if isinstance(o, dict)]
                self.nota(f"{nome}: {len(tokens)} ({', '.join(tokens) or 'nessuno'})")
            elif nome == "morsetti" and isinstance(valore, list) and len(valore) == 2:
                assegnati = [f"{a.get('IoName')}={a.get('IoMode')}" for a in valore[1] or []]
                self.nota(f"morsetti assegnati: {', '.join(assegnati) or 'nessuno'}")
            else:
                self.nota(f"{nome}: non letto ({valore})")
        sempre = next(
            (s for s in foto.get("fasce") or [] if isinstance(s, dict) and s.get("token") == "standard_always"), None
        )
        self.nota(f"definizione di standard_always: {json.dumps(sempre, ensure_ascii=False)}")

    async def t16(self) -> None:
        """Utente sentinella senza tessere con una Description di circa 90 caratteri."""
        token = f"{PREFISSO}impianto"
        # Come la scriverà l'app: versione, installazione, ultima scrittura
        descrizione = "aam:1 install=6f1c2e4a-9b7d-4c3e-8a21-3f5d7c9e1b20 write=000000123 ora=2026-11-07T10:00:00Z"
        riga = await self.ultimo_evento()
        esito = await self.crea("utente", self.modello("utente", token, Description=descrizione))
        if not esito.ok:
            raise RuntimeError(f"sentinella non creata: {esito.spiegazione()}")
        letto = await self.leggi("utente", token) or {}
        elenco = next((u for u in await self.elenco("utente") or [] if u.get("token") == token), {})
        for dove, utente in (("GetUser", letto), ("GetUserList", elenco)):
            valore = str(utente.get("Description") or "")
            intera = "intera" if valore == descrizione else f"DIVERSA ({len(valore)} caratteri: {valore!r})"
            self.nota(f"{dove}: Description di {len(descrizione)} caratteri {intera}")
        await self.pausa(2)
        esito = await self.tenta("latest_events", 100)
        nuovi = [riassunto_evento(r) for r in esito.valore or [] if int(r.get("rowid") or 0) > riga] if esito.ok else []
        citano = [e for e in nuovi if token in json.dumps(e) or descrizione[:20] in json.dumps(e)]
        self.sezione["eventi"] = nuovi
        self.nota(f"eventi dopo la creazione: {len(nuovi)}, che citano la sentinella: {len(citano)}")
        for lunghezza in (128, 256, 1024):
            prova = f"{PREFISSO}t16_descrizione_{lunghezza}"
            valore = lungo(descrizione, lunghezza)
            esito = await self.crea("utente", self.modello("utente", prova, Description=valore))
            letto = await self.leggi("utente", prova) if esito.ok else None
            if letto is not None:
                lettura = str(letto.get("Description") or "")
                self.nota(f"Description di {lunghezza} caratteri: letta di {len(lettura)}")
            else:
                self.nota(f"Description di {lunghezza} caratteri: {esito.spiegazione()}")
        risposta = self.operatore.chiedi(
            "sentinella_sito",
            f"Apri la pagina del controller, elenco degli utenti: come compare {token}? Scrivi in breve cosa vedi "
            "(Invio per saltare).",
        )
        if risposta:
            self.nota(f"sul sito Axis: {risposta}")
        ancora = "c'è ancora" if await self.leggi("utente", token) else "NON C'È PIÙ"
        self.nota(f"la sentinella senza tessere {ancora} alla fine della prova")
        self.nota(
            "il peso sui limiti non si misura qui: la sentinella è un utente, il limite degli utenti lo cerca T18 con "
            "--limiti"
        )

    async def t18(self) -> None:
        """Molte tessere: tempi di scrittura, di lettura completa e di rimozione. Con --limiti anche i limiti del
        controller e il pulsante di uscita premuto durante la scrittura."""
        n = self.quante
        self.scrivi(f"  scrivo {n} utenti e {n} tessere aam_prova_: ci vuole qualche minuto")
        profilo = f"{PREFISSO}t18_profilo"
        await self.crea("profilo", self.modello("profilo", profilo))
        tempi: dict[str, list[float]] = {
            "set_user": [],
            "set_credential": [],
            "remove_credential": [],
            "remove_user": [],
        }
        creati: list[tuple[str, str]] = []
        fermo = ""
        inizio = time.monotonic()
        for i in range(1, n + 1):
            utente, tessera = f"{PREFISSO}t18_u{i:04d}", f"{PREFISSO}t18_t{i:04d}"
            esito = await self.crea("utente", self.modello("utente", utente))
            tempi["set_user"].append(esito.durata)
            if esito.ok:
                esito = await self.crea(
                    "tessera", self.modello("tessera", tessera, {"utente": utente, "profilo": profilo})
                )
                tempi["set_credential"].append(esito.durata)
            if not esito.ok:
                fermo = f"fermo a {i}: {esito.spiegazione()}"
                break
            creati.append((utente, tessera))
            if i % 100 == 0:
                self.scrivi(f"  {i} di {n}")
        scrittura = round(time.monotonic() - inizio, 1)
        self.nota(f"scritte {len(creati)} tessere con i loro utenti in {scrittura} s" + (f"; {fermo}" if fermo else ""))
        letture = {}
        for metodo in ("list_credentials", "list_users"):
            esito = await self.tenta(metodo)
            pagine = [round(s.get("durata_s", 0), 3) for s in esito.http if s.get("stato") == 200]
            letture[metodo] = {"esito": esito.spiegazione(), "durata_s": esito.durata, "pagine_s": pagine}
            quanti = len(esito.valore or []) if esito.ok else 0
            self.nota(f"{metodo}: {quanti} in {esito.durata} s, pagine da {vapix.CREDENTIAL_PAGE_SIZE}: {len(pagine)}")
        inizio = time.monotonic()
        for utente, tessera in creati:
            for metodo, token in (("remove_credential", tessera), ("remove_user", utente)):
                esito = await self.tenta(metodo, token)
                tempi[metodo].append(esito.durata)
                if esito.irraggiungibile:
                    raise RuntimeError(f"il controller non risponde durante la rimozione: {esito.spiegazione()}")
        self.nota(f"tolte in {round(time.monotonic() - inizio, 1)} s")
        self.sezione["tempi"] = {"letture": letture, **{k: statistiche(v) for k, v in tempi.items()}}
        for metodo, durate in tempi.items():
            s = statistiche(durate)
            if s["quante"]:
                self.nota(
                    f"{metodo}: media {s['media_ms']} ms, 95% entro {s['p95_ms']} ms, massimo {s['massimo_ms']} ms"
                )
        if self.limiti:
            await self.t18_limiti()
        else:
            self.sezione["limiti"] = {"provati": False}
            self.nota(
                "T18 parziale: solo i tempi. NON provati i limiti con CapabilityViolated (tessere, utenti, profili, "
                "fasce, eccezioni per fascia, regole per profilo) né le aperture con il pulsante durante la "
                "scrittura: si provano con --limiti"
            )
        lente = [o for o in self.sezione["operazioni"] if o["durata_s"] > 5]
        if lente:
            self.nota(f"ATTENZIONE: {len(lente)} chiamate oltre 5 s (il client aspetta al massimo 10 s)")
        risposta = self.operatore.chiedi(
            "nota_t18", "Se durante la prova la pagina del controller è rallentata, scrivilo qui (Invio per niente)."
        )
        if risposta:
            self.nota(f"dall'operatore: {risposta}")

    async def t18_limiti(self) -> None:
        """Limiti del controller: oggetti aam_prova_ scritti uno alla volta, a blocchi, fino a CapabilityViolated o al
        tetto, e tolti dopo ogni ricerca con la pulizia di sempre. Mentre scrive le tessere l'operatore preme il
        pulsante di uscita della porta del banco: nessuna tessera da passare, e nessun oggetto del kit apre qualcosa
        (i profili non hanno regole, o le hanno solo per punti di accesso del kit, senza serratura)."""
        limiti: dict = {"provati": True, "tetti": dict(self.tetti), "risultati": {}, "rimozioni": []}
        self.sezione["limiti"] = limiti
        risultati = limiti["risultati"]
        self.scrivi(f"  ricerca dei limiti, tetti: {', '.join(f'{k} {v}' for k, v in self.tetti.items())}")
        self.leggero = True
        try:
            await self.pulisci_fase("i tempi")
            porta = await self.porta_del_pulsante()

            utenti: list[str] = []

            async def utente(i: int) -> Esito:
                token = f"{PREFISSO}t18_lim_u{i:05d}"
                esito = await self.crea("utente", self.modello("utente", token))
                if esito.ok:
                    utenti.append(token)
                return esito

            await self.limite_a_oggetti("utenti", "utente", utente)
            if utenti:
                # Tessere con gli utenti appena scritti, a giro, e con un profilo senza regole: non aprono niente
                profilo = f"{PREFISSO}t18_lim_profilo"
                con_profilo = (await self.crea("profilo", self.modello("profilo", profilo))).ok

                async def tessera(i: int) -> Esito:
                    rif = {"utente": utenti[(i - 1) % len(utenti)], "profilo": profilo if con_profilo else ""}
                    return await self.crea("tessera", self.modello("tessera", f"{PREFISSO}t18_lim_t{i:05d}", rif))

                r = await self.limite_a_oggetti("tessere", "tessera", tessera, pulsante=porta)
                r["utenti"], r["con_profilo"] = len(utenti), con_profilo
            else:
                self.non_provato("tessere", "nessun utente scritto")
            if porta is not None and "pulsante" not in limiti:
                self.nota("aperture con il pulsante durante la scrittura non provate: le tessere non si sono scritte")
            await self.pulisci_fase("utenti e tessere")

            async def profilo_n(i: int) -> Esito:
                return await self.crea("profilo", self.modello("profilo", f"{PREFISSO}t18_lim_p{i:04d}"))

            await self.limite_a_oggetti("profili", "profilo", profilo_n)
            await self.pulisci_fase("profili")

            async def fascia_n(i: int) -> Esito:
                return await self.crea("fascia", self.modello("fascia", f"{PREFISSO}t18_lim_f{i:04d}"))

            await self.limite_a_oggetti("fasce", "fascia", fascia_n)
            await self.pulisci_fase("fasce")
            await self.limite_eccezioni()
            await self.pulisci_fase("eccezioni")
            await self.limite_regole()
            await self.pulisci_fase("regole")
        finally:
            self.leggero = False
        trovati = []
        for nome, r in risultati.items():
            if r["esito"] == "CapabilityViolated":
                trovati.append(f"{nome} {r['limite']}")
            elif r["esito"] in ("tetto", "punti finiti"):
                trovati.append(f"{nome} almeno {r['limite']}")
            else:
                trovati.append(f"{nome} non trovato ({r['esito']})")
        self.nota(f"limiti per hardware.LIMITS: {', '.join(trovati)}")

    def non_provato(self, nome: str, perche: str) -> None:
        self.nota(f"{nome}: limite non provato, {perche}")
        self.sezione["limiti"]["risultati"][nome] = {"esito": "non provato", "perche": perche}

    def fine_ricerca(self, nome: str, risultato: dict, testo: str, spento: bool) -> dict:
        """Il risultato di una ricerca va nel rapporto; se il controller non risponde più ferma la prova (la pulizia
        dopo la prova riprova a togliere tutto)."""
        self.sezione["limiti"]["risultati"][nome] = risultato
        self.nota(f"{nome}: {testo}")
        if spento:
            raise RuntimeError(f"il controller non risponde durante la ricerca del limite di {nome}")
        return risultato

    async def pulisci_fase(self, dopo: str) -> None:
        """Toglie tutti gli oggetti del kit tra una ricerca e l'altra; se resta qualcosa ferma la prova."""
        inizio = time.monotonic()
        pulizia = await self.pulisci()
        durata = round(time.monotonic() - inizio, 1)
        voce = {"dopo": dopo, "tolti": len(pulizia["tolti"]), "durata_s": durata, "non_tolti": pulizia["non_tolti"]}
        self.sezione["limiti"]["rimozioni"].append(voce)
        if pulizia["tolti"]:
            self.nota(f"pulizia dopo {dopo}: {len(pulizia['tolti'])} oggetti tolti in {durata} s")
        if pulizia["non_tolti"]:
            raise RuntimeError(f"dopo {dopo} restano oggetti del kit: {'; '.join(pulizia['non_tolti'][:5])}")

    async def porta_del_pulsante(self) -> dict | None:
        """Punto di accesso della porta del banco, per il pulsante di uscita; None se non si prova."""
        if not self.operatore.interattivo:
            self.nota("aperture con il pulsante durante la scrittura non provate: serve l'operatore")
            return None
        punto = await self.lettore_vero(
            "Per provare il pulsante di uscita mentre il kit scrive serve la porta del banco configurata sul "
            "controller, con lettore e pulsante di uscita (procedura guidata della pagina del controller). "
            "Configurala e premi Invio, oppure scrivi salta."
        )
        if punto is None:
            self.nota("aperture con il pulsante durante la scrittura non provate: nessuna porta configurata")
        return punto

    async def pulsante_prima(self, punto: dict, quanti: int, nome: str) -> dict | None:
        """Prima di un blocco: l'operatore si prepara a premere il pulsante di uscita mentre il kit scrive."""
        porta = str(punto.get("Entity") or "")
        nome_porta = (await self.leggi("porta", porta) or {}).get("Name") or porta
        risposta = self.operatore.chiedi(
            "pulsante_pronto",
            f"Ora il kit scrive {quanti} {nome} di fila. Premi Invio e, mentre scrive, premi il PULSANTE DI USCITA "
            f"della porta {nome_porta} una o due volte: guarda se la porta si apre subito. NON passare tessere. "
            "Scrivi salta per non farlo.",
        )
        if risposta.lower() == "salta":
            self.sezione["limiti"]["pulsante"] = {"risposta": "salta"}
            self.nota("aperture con il pulsante durante la scrittura non provate: saltato dall'operatore")
            return None
        stato = {"riga": await self.ultimo_evento(), "inizio": datetime.now(tz=UTC).isoformat(timespec="seconds")}
        self.scrivi("  scrivo: premi ora il pulsante di uscita")
        return stato

    async def pulsante_dopo(self, stato: dict, quanti: int, nome: str, durata: float) -> None:
        risposta = self.operatore.chiedi(
            "pulsante_esito",
            f"Scrittura finita ({quanti} {nome} in {durata} s). Con il pulsante di uscita la porta si è aperta subito? "
            "Scrivi sì, in ritardo (di quanti secondi), no, oppure non premuto.",
        )
        eventi = await self.eventi_dopo(stato["riga"])
        self.sezione["limiti"]["pulsante"] = {
            **stato,
            "durante": f"{quanti} {nome}",
            "durata_s": durata,
            "risposta": risposta,
            "eventi": eventi,
        }
        argomenti = sorted({e["argomento"] for e in eventi})
        self.nota(
            f"pulsante di uscita durante la scrittura di {quanti} {nome} ({durata} s): "
            f"«{risposta or 'nessuna risposta'}»; eventi intanto: {len(eventi)} ({', '.join(argomenti) or 'nessuno'})"
        )

    async def limite_a_oggetti(
        self, nome: str, tipo: str, crea_uno, pulsante: dict | None = None, tetto: int | None = None
    ) -> dict:
        """Un oggetto nuovo alla volta (crea_uno(i) scrive l'i-esimo), a blocchi, fino a CapabilityViolated, a un altro
        errore o al tetto; tra un blocco e l'altro ricontrolla il controller. Con pulsante, durante il primo blocco
        l'operatore preme il pulsante di uscita."""
        tetto = tetto or self.tetti[nome]
        blocco = blocco_di(nome, tetto)
        elenco = await self.elenco(tipo)
        presenti = None if elenco is None else len([o for o in elenco if not nostro(tipo, o)])
        scritti, durate, blocchi = 0, [], []
        fermo: Esito | None = None
        spento = False
        while scritti < tetto and fermo is None:
            quanti = min(blocco, tetto - scritti)
            attesa = await self.pulsante_prima(pulsante, quanti, nome) if pulsante and not blocchi else None
            inizio = time.monotonic()
            for _ in range(quanti):
                esito = await crea_uno(scritti + 1)
                durate.append(esito.durata)
                if not esito.ok:
                    fermo = esito
                    break
                scritti += 1
            blocchi.append({"fino_a": scritti, "durata_s": round(time.monotonic() - inizio, 2)})
            if attesa is not None:
                await self.pulsante_dopo(attesa, quanti, nome, blocchi[-1]["durata_s"])
            self.scrivi(f"  {nome}: {scritti}")
            if (fermo is not None and fermo.irraggiungibile) or (fermo is None and not await self.identita()):
                spento = True
                break
        lettura = await self.tenta(TIPI[tipo].elenco)
        nostri = len([o for o in lettura.valore or [] if nostro(tipo, o)]) if lettura.ok else None
        risultato: dict = {
            "accettati": scritti,
            "gia_presenti": presenti,
            "tetto": tetto,
            "blocco": blocco,
            "scrittura": statistiche(durate),
            "blocchi": blocchi,
            "lettura": {"esito": lettura.spiegazione(), "durata_s": lettura.durata, "del_kit": nostri},
        }
        altri = f" più {presenti} già presenti" if presenti else ("" if presenti == 0 else " più quelli già presenti")
        if fermo is not None and fermo.limite():
            risultato.update(esito="CapabilityViolated", limite=scritti + (presenti or 0), fault=fermo.fault)
            testo = f"CapabilityViolated al {scritti + 1}°: entrano {scritti} aam_prova_{altri}"
        elif spento:
            risultato.update(esito="controller non risponde", limite=None)
            testo = f"il controller non risponde dopo {scritti}: limite non trovato"
        elif fermo is not None:
            risultato.update(esito="altro errore", limite=None, errore=fermo.errore, fault=fermo.fault)
            testo = f"fermo al {scritti + 1}° per un altro errore ({fermo.spiegazione()}): limite non trovato"
        else:
            risultato.update(esito="tetto", limite=scritti + (presenti or 0))
            testo = f"tetto di {tetto} raggiunto senza CapabilityViolated: entrano almeno {scritti} aam_prova_{altri}"
        s = risultato["scrittura"]
        if s["quante"]:
            testo += f"; scrittura media {s['media_ms']} ms, 95% entro {s['p95_ms']} ms"
        testo += f"; lettura completa in {lettura.durata} s ({lettura.spiegazione()})"
        self.fine_ricerca(nome, risultato, testo, spento)
        if nostri is not None and nostri != scritti:
            self.nota(f"{nome}: ATTENZIONE, il controller ne elenca {nostri} del kit, non {scritti}")
        return risultato

    async def limite_in_un_oggetto(self, nome: str, scrivi_con, tetto: int | None = None) -> dict:
        """Lo stesso oggetto riscritto con sempre più voci (scrivi_con(n) lo scrive con n), a blocchi, fino a
        CapabilityViolated, a un altro errore o al tetto; dopo un CapabilityViolated il numero esatto si trova
        dimezzando l'intervallo."""
        tetto = tetto or self.tetti[nome]
        blocco = blocco_di(nome, tetto)
        buono, cattivo = 0, None
        prove: list[dict] = []
        fermo: Esito | None = None

        async def prova(n: int) -> Esito:
            esito = await scrivi_con(n)
            prove.append({"voci": n, "esito": esito.spiegazione(), "durata_s": esito.durata})
            return esito

        n = min(blocco, tetto)
        while True:
            esito = await prova(n)
            if not esito.ok:
                fermo, cattivo = esito, n
                break
            buono = n
            if n >= tetto:
                break
            n = min(n + blocco, tetto)
        limite_visto = fermo is not None and fermo.limite()
        interrotta: Esito | None = None
        while limite_visto and cattivo - buono > 1:
            mezzo = (buono + cattivo) // 2
            esito = await prova(mezzo)
            if esito.ok:
                buono = mezzo
            elif esito.limite():
                cattivo = mezzo
            else:
                # Un altro errore a metà della ricerca: il limite resta tra buono e cattivo
                interrotta = esito
                break
        risultato: dict = {"accettate": buono, "tetto": tetto, "blocco": blocco, "prove": prove}
        if limite_visto:
            esatto = cattivo - buono == 1
            risultato.update(esito="CapabilityViolated", limite=buono, rifiutate=cattivo, esatto=esatto)
            testo = f"CapabilityViolated con {cattivo}: entrano {buono}"
            if interrotta is not None:
                testo += f" (il limite è tra {buono} e {cattivo}: ricerca fermata da {interrotta.spiegazione()})"
        elif fermo is not None:
            risultato.update(esito="altro errore", limite=None, errore=fermo.errore, fault=fermo.fault)
            testo = f"fermo con {cattivo} per un altro errore ({fermo.spiegazione()}): limite non trovato"
        else:
            risultato.update(esito="tetto", limite=buono)
            testo = f"tetto di {tetto} raggiunto senza CapabilityViolated: entrano almeno {buono}"
        spento = any(e is not None and e.irraggiungibile for e in (fermo, interrotta))
        return self.fine_ricerca(nome, risultato, testo, spento)

    async def limite_eccezioni(self) -> None:
        """Eccezioni (giornate intere) in una fascia sola, sempre più."""
        fascia = f"{PREFISSO}t18_lim_eccezioni"
        primo = date(2027, 1, 1)

        def eccezioni(n: int) -> str | None:
            return holidays_ical([{"day": (primo + timedelta(days=i)).isoformat()} for i in range(n)])

        async def scrivi_con(n: int) -> Esito:
            return await self.crea("fascia", self.modello("fascia", fascia, ExceptionScheduleDefinition=eccezioni(n)))

        r = await self.limite_in_un_oggetto("eccezioni", scrivi_con)
        r["caratteri"] = len(eccezioni(r["accettate"]) or "")
        self.nota(f"eccezioni: con {r['accettate']} il testo delle eccezioni è di {r['caratteri']} caratteri")

    async def limite_regole(self) -> None:
        """Regole in un profilo solo, una per punto di accesso come le scrive l'app: prima i punti di accesso del kit,
        tutti sulla stessa porta e sullo stesso lettore del kit (senza configurazione: niente serratura, niente
        morsetti), poi il profilo con sempre più regole. Nessuna tessera usa il profilo."""
        porta, lettore = f"{PREFISSO}t18_lim_porta", f"{PREFISSO}t18_lim_lettore"
        for tipo, token in (("porta", porta), ("lettore", lettore)):
            esito = await self.crea(tipo, self.modello(tipo, token))
            if not esito.ok:
                return self.non_provato("regole", f"{tipo} del kit non creato ({esito.spiegazione()})")
        punti: list[str] = []

        async def punto(i: int) -> Esito:
            token = f"{PREFISSO}t18_lim_ap{i:04d}"
            esito = await self.crea("punto", self.modello("punto", token, {"porta": porta, "lettore": lettore}))
            if esito.ok:
                punti.append(token)
            return esito

        await self.limite_a_oggetti("punti", "punto", punto, tetto=self.tetti["regole"])
        if not punti:
            return self.non_provato("regole", "nessun punto di accesso del kit")
        profilo = f"{PREFISSO}t18_lim_regole"

        async def scrivi_con(n: int) -> Esito:
            return await self.crea("profilo", self.modello("profilo", profilo, {"punti": punti[:n]}))

        r = await self.limite_in_un_oggetto("regole", scrivi_con, tetto=len(punti))
        if r["esito"] == "tetto" and len(punti) < self.tetti["regole"]:
            r["esito"] = "punti finiti"
            self.nota(f"regole: non si va oltre {len(punti)}, i punti di accesso del kit finiscono lì")


async def esegui(kit: Kit, prove: list[str]) -> dict:
    """Controlli, prove scelte nell'ordine giusto, pulizia finale. Rifiuto se il kit non deve partire."""
    try:
        await kit.avvio()
        for codice in ORDINE:
            if codice in prove:
                await kit.esegui_prova(codice)
    except Vietata as err:
        kit.dati["vietata"] = str(err)
        kit.scrivi(f"\nFERMO: il kit stava per mandare una richiesta vietata, non è partita: {err}")
    finally:
        if kit.dati["prove"] or kit.dati["avvio"].get("pulizia"):
            await kit.chiusura()
        else:
            kit.salva()
        await kit.client.aclose()
    return kit.dati


def tetto(valore: str) -> tuple[str, int]:
    """--tetto nome=numero: tetto di una ricerca dei limiti di T18."""
    nome, _, numero = valore.partition("=")
    if nome not in TETTI or not numero.isdigit() or int(numero) < 1:
        raise argparse.ArgumentTypeError(f"scrivi nome=numero con nome tra {', '.join(TETTI)}, non {valore!r}")
    return nome, int(numero)


def argomenti(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prove al banco sull'A1210 (T1-T12, T16, T18). Solo su un controller di banco resettato."
    )
    parser.add_argument("--host", required=True, help="indirizzo del controller, anche con :porta")
    parser.add_argument("--utente", default="root", help="utente del controller (la password la chiede il kit)")
    parser.add_argument("--serie", required=True, help="numero di serie dall'etichetta del controller di banco")
    parser.add_argument("--prova", action="append", choices=ORDINE, help="solo questa prova; si può ripetere")
    parser.add_argument("--tessera", default="", help="numero della tessera da passare sul lettore (T4 e T8)")
    parser.add_argument("--quante", type=int, default=500, help="tessere da scrivere in T18 (di serie 500)")
    parser.add_argument(
        "--limiti",
        action="store_true",
        help="in T18 cerca anche i limiti del controller (CapabilityViolated) e prova il pulsante di uscita durante "
        "la scrittura",
    )
    parser.add_argument(
        "--tetto",
        type=tetto,
        action="append",
        default=[],
        metavar="NOME=NUMERO",
        help="tetto di una ricerca dei limiti, si può ripetere; di serie "
        + ", ".join(f"{k}={v}" for k, v in TETTI.items()),
    )
    parser.add_argument("--uscita", help="file JSON del rapporto (di serie prove_a1210_<serie>_<ora>.json)")
    parser.add_argument("--non-interattivo", action="store_true", help="salta le parti che chiedono all'operatore")
    parser.add_argument("--verifica-certificato", action="store_true", help="verifica il certificato HTTPS")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    scelte = argomenti(argv)
    # Console di Windows: un nome con un carattere che non sa scrivere non deve fermare il kit a metà
    sys.stdout.reconfigure(errors="replace")
    password = os.environ.get("AAM_BANCO_PASSWORD") or getpass.getpass(f"Password di {scelte.utente} sul controller: ")
    client, registro = cliente(scelte.host, scelte.utente, password, scelte.verifica_certificato)
    operatore = SenzaOperatore() if scelte.non_interattivo else Operatore()
    kit = Kit(
        client,
        registro,
        scelte.serie,
        operatore,
        quante=scelte.quante,
        tessera=scelte.tessera,
        limiti=scelte.limiti,
        tetti=dict(scelte.tetto),
    )
    ora = datetime.now().strftime("%Y%m%d_%H%M%S")
    kit.file = Path(scelte.uscita or f"prove_a1210_{serie_pulita(scelte.serie)}_{ora}.json")
    try:
        dati = asyncio.run(esegui(kit, scelte.prova or ORDINE))
    except Rifiuto as err:
        kit.dati["rifiuto"] = str(err)
        kit.salva()
        print(f"\n{'MI SONO FERMATO' if kit.dati['prove'] else 'NON PARTO'}: {err}")
        return 2
    except KeyboardInterrupt:
        print(f"\nInterrotto. Quello che il kit ha potuto togliere è nel rapporto: {kit.file}")
        return 130
    rimasti = (dati.get("fine", {}).get("pulizia") or {}).get("non_tolti")
    return 1 if rimasti or dati.get("vietata") else 0


if __name__ == "__main__":
    sys.exit(main())
