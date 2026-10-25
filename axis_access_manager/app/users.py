"""Utenti dell'app e permessi: chi può vedere e chi può modificare."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .db import Database

# Permesso → descrizione mostrata nella pagina Utenti
PERMISSIONS = {
    "stato": "Vedere cruscotto, porte e notifiche",
    "persone": "Vedere persone, tessere e tipologie",
    "persone_modifica": "Registrare, modificare, sospendere ed eliminare persone e tessere",
    "registro": "Vedere il registro accessi e le istantanee",
    "regole": "Vedere fasce orarie e festività",
    "regole_modifica": "Modificare tipologie, fasce orarie e festività",
    "import_export": "Esportare e importare file CSV",
    "impianti": "Configurare controller, porte, citofoni e telecamere e programmare il controller",
    "utenti": "Gestire gli utenti dell'app e vedere le attività (amministratore)",
}

# Permessi che ne richiedono altri per avere senso
IMPLIED = {
    "persone_modifica": ["persone"],
    "regole_modifica": ["regole"],
}

# Ogni permesso mostra le sue pagine nel menu: senza "stato" spariscono cruscotto, porte e notifiche
ROLES = {
    "amministratore": ("Amministratore", list(PERMISSIONS)),
    "operatore": ("Operatore", ["stato", "persone", "persone_modifica", "registro", "regole", "import_export"]),
    "tessere": ("Tessere", ["persone", "persone_modifica", "regole"]),
    "consultazione": ("Consultazione", ["stato", "persone", "registro", "regole"]),
    "solo_registro": ("Solo Registro", ["registro"]),
    "solo_stato": ("Solo Stato", ["stato"]),
    "nessun_accesso": ("Nessun Accesso", []),
}

ADMIN = "utenti"
LOCAL_USER = "locale"
SETTING_STATUS_ADDED = "perm_stato_added"


class UserError(Exception):
    """Operazione sugli utenti non consentita."""


@dataclass
class AppUser:
    username: str
    name: str
    perms: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.name or self.username

    def can(self, *perms: str) -> bool:
        return ADMIN in self.perms or all(p in self.perms for p in perms)


def normalize_perms(perms: list[str]) -> list[str]:
    result = {p for p in perms if p in PERMISSIONS}
    if ADMIN in result:
        return list(PERMISSIONS)
    for perm in list(result):
        result.update(IMPLIED.get(perm, []))
    return [p for p in PERMISSIONS if p in result]


def role_of(perms: list[str]) -> str:
    current = set(normalize_perms(perms))
    for key, (_, role_perms) in ROLES.items():
        if set(role_perms) == current:
            return key
    return "personalizzato"


class UserDirectory:
    """Riconosce l'utente di Home Assistant che apre la pagina e ne restituisce i permessi."""

    def __init__(self, db: Database, readonly_users: tuple[str, ...] = ()) -> None:
        self.db = db
        self.readonly_users = readonly_users
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def resolve(self, username: str, display_name: str) -> AppUser:
        """Utente registrato; chi entra per la prima volta viene aggiunto.

        Il primo utente in assoluto diventa amministratore, gli altri partono con "Solo Stato"
        (o "Consultazione" se elencati nell'opzione readonly_users).
        """
        username = username.strip().lower()
        if not username:
            # Senza intestazioni di Ingress (sviluppo, vecchio Supervisor): accesso completo come prima della 0.4
            return AppUser(LOCAL_USER, "Accesso locale", set(PERMISSIONS))
        with self._lock:
            row = self.db.get_app_user(username)
            if row is None:
                if not self._has_admin():
                    perms = ROLES["amministratore"][1]
                elif username in self.readonly_users:
                    perms = ROLES["consultazione"][1]
                else:
                    perms = ROLES["solo_stato"][1]
                self.db.save_app_user(username, display_name, perms)
                self.db.add_audit(display_name or username, "utente aggiunto al primo accesso", role_label(perms))
                row = self.db.get_app_user(username)
            self._touch(username, display_name, row)
        return AppUser(row["username"], row["name"] or display_name, set(row["perms"]))

    def add_status_permission(self) -> int:
        """Una volta sola: "stato" agli utenti creati prima della 0.11, che vedevano sempre cruscotto e porte."""
        if self.db.get_setting(SETTING_STATUS_ADDED):
            return 0
        changed = 0
        with self._lock:
            for row in self.db.list_app_users():
                if "stato" not in row["perms"]:
                    self.db.save_app_user(row["username"], row["name"], normalize_perms(row["perms"] + ["stato"]))
                    changed += 1
            self.db.set_setting(SETTING_STATUS_ADDED, "1")
        if changed:
            self.db.add_audit("sistema", "permesso Stato aggiunto agli utenti esistenti", f"{changed} utenti")
        return changed

    def _touch(self, username: str, display_name: str, row: dict) -> None:
        # Al massimo una scrittura al minuto per utente
        now = time.monotonic()
        if now - self._seen.get(username, -120) < 60 and row["name"]:
            return
        self._seen[username] = now
        self.db.touch_app_user(username, display_name)

    def _has_admin(self) -> bool:
        return any(ADMIN in u["perms"] for u in self.db.list_app_users())

    def list(self) -> list[dict]:
        result = []
        for row in self.db.list_app_users():
            perms = normalize_perms(row["perms"])
            result.append({**row, "perms": perms, "role": role_of(perms), "admin": ADMIN in perms})
        return result

    def save(self, username: str, name: str, perms: list[str], by: AppUser, create: bool) -> dict:
        username = username.strip().lower()
        if not username or len(username) > 100 or any(c.isspace() for c in username):
            raise UserError("nome utente non valido: usa il nome utente di Home Assistant, senza spazi")
        name = " ".join(name.split())[:100]
        perms = normalize_perms(perms)
        with self._lock:
            existing = self.db.get_app_user(username)
            if create and existing:
                raise UserError(f"l'utente {username} esiste già")
            if not create and not existing:
                raise UserError("utente non trovato")
            if existing and ADMIN in existing["perms"] and ADMIN not in perms and self._admins() <= 1:
                raise UserError("serve almeno un amministratore: dai prima i permessi completi a un altro utente")
            self.db.save_app_user(username, name or (existing or {}).get("name", ""), perms)
        action = "utente creato" if create else "utente modificato"
        saved = self.db.get_app_user(username)
        self.db.add_audit(by.label, action, f"{saved['name'] or username}: {role_label(perms)}")
        return saved

    def delete(self, username: str, by: AppUser) -> None:
        username = username.strip().lower()
        with self._lock:
            existing = self.db.get_app_user(username)
            if not existing:
                raise UserError("utente non trovato")
            if username == by.username:
                raise UserError("non puoi eliminare il tuo utente")
            if ADMIN in existing["perms"] and self._admins() <= 1:
                raise UserError("è l'unico amministratore")
            self.db.delete_app_user(username)
        self.db.add_audit(by.label, "utente eliminato", existing["name"] or username)

    def _admins(self) -> int:
        return sum(1 for u in self.db.list_app_users() if ADMIN in u["perms"])


def role_label(perms: list[str]) -> str:
    role = role_of(perms)
    if role in ROLES:
        return ROLES[role][0]
    return "Personalizzato: " + ", ".join(perms)
