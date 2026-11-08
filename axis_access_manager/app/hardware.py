"""Catalogo dell'Axis A1210: ingressi, bus dei lettori, relè, porte e lettori, oggetti di fabbrica, limiti e tipi di
oggetto che accettano il token scelto dall'app.

Sono costanti scritte a mano dalle prove al banco (tools/banco, T1-T20) e mai imparate mentre l'app gira. Ogni valore
cita la prova che lo decide: finché la prova non c'è vale la variante prudente, quella che fa fare meno all'app.
"""

from __future__ import annotations

MODEL = "A1210"

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
# Ruoli possibili per ogni ingresso: il contatto porta e il REX sono del connettore Door, gli AUX sono liberi. Il
# Contatto Porta solo sugli ingressi che mandano gli eventi Device/IO/Port
ALLOWED_ROLES = {
    "DPS": {"", "door_monitor"},
    "REX": {"", "rex", "intercom"},
    "IO1": {"", "rex", "intercom", "door_contact", "alarm"},
    "IO2": {"", "rex", "intercom", "door_contact", "alarm"},
    "EXTERNAL_TAMPER": {"", "alarm"},
    "ALARM": {"", "door_contact", "alarm"},
}
# Bus dei lettori (morsetti con più lettori, un IoUser per lettore): RS-485 per OSDP e Wiegand. Da confermare con le
# prove al banco (T12, T13): Ser0hd è provato al banco, Wiegand1 viene dalla documentazione Axis
READER_BUSES = {"Ser0hd": "Lettori RS-485", "Wiegand1": "Lettore Wiegand"}
# Relè della serratura, comandato dalla porta. Da confermare con le prove al banco (T12, T14)
RELAYS = {"relay": "Relè Serratura"}
# Porte e lettori che l'A1210 comanda. Da confermare con le prove al banco (T12); solo informativi, nessun controllo li
# usa: l'impianto in servizio ha già le sue porte
DOORS = 1
READERS = 2

# Oggetti di fabbrica: l'app non li scrive, non li toglie e non li confronta, salvo la fascia Sempre, che si controlla
# soltanto (deve valere sempre, anche nei festivi). Da confermare con le prove al banco (T12, reset di fabbrica): ogni
# oggetto di serie che la prova trova in più va aggiunto qui.
# Fasce orarie di serie, con il nome italiano che l'app mostra; anche ogni altra fascia standard_* è di fabbrica
FACTORY_SCHEDULES = {
    "standard_always": "Sempre",
    "standard_office_hours": "Orario d'Ufficio",
    "standard_after_hours": "Fuori Orario",
    "standard_weekends": "Fine Settimana",
}
FACTORY_SCHEDULE_PREFIX = "standard_"
ALWAYS = "standard_always"
# Modi di identificazione dei punti di accesso: quelli che il codice conosce. Anche quelli fatti a mano restano come
# sono: l'app non scrive né toglie nessun modo di identificazione
FACTORY_AUTHENTICATION = frozenset({"CardOnly", "REXOnly"})
# Controllo accessi di serie («Axis-<serie> AccessController»): non si toglie mai, se ne cambia solo l'elenco dei
# punti di accesso gestiti
FACTORY_ACCESS_CONTROLLER = "Axis-{serial} AccessController"
# Assegnazioni dei morsetti di serie: dopo il reset al banco Ser0hd resta senza lettori, quindi nessuna (T12). Un
# morsetto senza IoUser non conta mai
FACTORY_IO: frozenset[str] = frozenset()
FACTORY = {
    "fascia": frozenset(FACTORY_SCHEDULES),
    "modo": FACTORY_AUTHENTICATION,
    "gestiti": frozenset({FACTORY_ACCESS_CONTROLLER}),
    "morsetto": FACTORY_IO,
}

# Limiti del controller per tipo (numero massimo), controllati al salvataggio: utenti, tessere, profili, fasce,
# eccezioni per fascia, punti di accesso e regole per profilo. Da confermare con le prove al banco (T18 con --limiti,
# la riga «limiti per hardware.LIMITS» del rapporto). None: limite non ancora misurato, nessun controllo
LIMITS: dict[str, int | None] = {
    "utenti": None,
    "tessere": None,
    "profili": None,
    "fasce": None,
    "eccezioni": None,
    "punti": None,
    "regole": None,
}

# Tipi di oggetto che il controller crea con il token scelto dall'app (T1). Provato solo SetUser: gli altri tipi
# passano dal ripiego (token dato dal controller), finché T1 non dice il contrario
APP_TOKEN_KINDS = frozenset({"utente"})


def _plain_mode(io: str) -> str:
    """Modo dell'ingresso senza linea supervisionata: gli AUX sono I/O configurabili, gli altri solo ingressi."""
    return "inout:pu/gnd" if io.startswith("IO") else "in:pu/gnd"


def is_factory(kind: str, token: str, serial: str = "") -> bool:
    """Oggetto di fabbrica, che l'app non scrive né toglie mai. Il controllo accessi si riconosce dal numero di serie
    nel token; senza numero di serie ogni controllo accessi conta come di fabbrica."""
    token = str(token or "")
    if kind == "fascia":
        return token in FACTORY["fascia"] or token.startswith(FACTORY_SCHEDULE_PREFIX)
    if kind == "modo":
        return True
    if kind == "gestiti":
        return not serial or token.lower() == FACTORY_ACCESS_CONTROLLER.format(serial=serial).lower()
    return token in FACTORY.get(kind, ())
