"""Testi senza Home Assistant per chi entra dall'accesso diretto senza i permessi tecnici.

Il personale che usa l'accesso diretto (reception, pulizie, manutenzione) non deve nemmeno sapere che dietro l'app c'è
Home Assistant: senza i permessi Impianti o Backup non legge mai «Home Assistant», «MQTT», «broker», «add-on» e simili,
né nelle pagine né nelle notifiche né negli errori. Resta solo il registro, dove Home Assistant è chi ha aperto la porta
a distanza. Lo decide il server per ogni richiesta (main.hides_home_assistant), mai il browser; da Home Assistant e per
chi ha i permessi tecnici i testi restano quelli di sempre.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

# Permessi tecnici: chi ne ha uno configura l'impianto o i backup, e legge i testi di sempre
TECHNICAL = ("impianti", "backup")
# Parole che chi entra così non deve leggere, solo come parole intere: «Supervisore» e «ingresso» sono altro. «HA»
# solo maiuscolo («ha» è il verbo)
FORBIDDEN = re.compile(
    r"\bhome[ -]?assistant\b|(?-i:\bHA\b)|\bmqtt\b|\bbroker\b|\bmosquitto\b|\badd-?on\b|\bsupervisor\b|\bingress\b"
    r"|\bintegrazion[ei]\b",
    re.IGNORECASE,
)
# Testi scritti dagli utenti (nomi di persone, ditte, tipologie, porte…) che possono comparire in un messaggio: una
# lista, o una funzione che la legge solo quando serve
Names = Iterable[str] | Callable[[], Iterable[str]]
# Soluzione tecnica: per loro c'è l'amministratore
ASK_ADMIN = "avvisa l'amministratore dell'impianto"
# Errore che anche così parlerebbe di Home Assistant (un testo nuovo, non previsto qui sotto)
NEUTRAL_ERROR = f"operazione non riuscita: {ASK_ADMIN}"
# Frasi dei messaggi del server che possono arrivare anche a loro, riscritte: errori imprevisti, telecamere (le foto
# passano da Home Assistant)
PHRASES = (
    (re.compile(r"\b([Ee])rrore imprevisto[,:] dettagli nel log dell'add-on"), rf"\1rrore imprevisto: {ASK_ADMIN}"),
    (re.compile(r"Home Assistant non risponde"), "telecamera non raggiungibile"),
    (re.compile(r" \(risposta (\d+) di Home Assistant\)"), r" (risposta \1)"),
)


def technical(user) -> bool:
    """L'utente ha un permesso tecnico (l'amministratore li ha tutti)."""
    return any(user.can(perm) for perm in TECHNICAL)


def forbidden(text: str, names: Names = ()) -> bool:
    """Il testo nomina Home Assistant e simili. Una parola vietata dentro un testo scritto dagli utenti (names) non
    conta: «la tessera è già registrata a nome VAN NAM HA», «esiste già la tipologia Broker Assicurazioni». I nomi si
    leggono solo se nel testo c'è una parola vietata."""
    text = text or ""
    if not FORBIDDEN.search(text):
        return False
    for name in _masks(names() if callable(names) else names):
        text = text.replace(name, " ")
    return bool(FORBIDDEN.search(text))


def _masks(names: Iterable[str]) -> list[str]:
    """I nomi da non contare, dal più lungo: solo quelli con una parola vietata e anche altro. Un nome fatto solo di
    una parola vietata («MQTT», «HA») non si distingue dalla parola tecnica, e resta vietato."""
    found = {n for n in names if n and FORBIDDEN.search(n) and re.search(r"\w", FORBIDDEN.sub("", n))}
    return sorted(found, key=len, reverse=True)


def once(read: Callable[[], Iterable[str]]) -> Callable[[], list[str]]:
    """I nomi scritti dagli utenti letti al primo bisogno e poi tenuti, per una risposta; se non si leggono, nessuno
    (il testo con una parola vietata resta sostituito)."""
    kept: list[list[str]] = []

    def names() -> list[str]:
        if not kept:
            try:
                kept.append([n for n in read() if n])
            except Exception:  # noqa: BLE001 - senza nomi il testo si sostituisce come prima
                kept.append([])
        return kept[0]

    return names


def plain_text(text: str, fallback: str = NEUTRAL_ERROR, names: Names = ()) -> str:
    """Il testo senza Home Assistant: le frasi note riscritte; se ne resta una parola (fuori dai nomi scritti dagli
    utenti), fallback al posto di tutto."""
    for pattern, replacement in PHRASES:
        text = pattern.sub(replacement, text)
    return fallback if forbidden(text, names) else text


def plain_source(source: str) -> str:
    """Chi ha provato ad aprire a distanza («Luca (Home Assistant)»), per le notifiche: nel registro resta com'è."""
    rest = source.replace("(Home Assistant)", "(apertura remota)")
    return "Apertura Remota" if forbidden(rest) else rest
