"""Accesso diretto all'app, fuori da Home Assistant: per il personale (reception, pulizie, manutenzione) da un PC della
rete interna, con nome utente e password dell'app.

Un secondo server nello stesso processo, sulla porta DIRECT_PORT del container (in Home Assistant la si assegna nella
scheda Rete dell'add-on), serve la stessa app. Le sue richieste sono segnate (scope DIRECT_SCOPE): per loro non vale
l'utente di Home Assistant (le intestazioni di Ingress si tolgono) ma solo la sessione aperta con il login.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import h11
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .db import Database

_LOGGER = logging.getLogger(__name__)

DIRECT_PORT = 8100
DIRECT_SCOPE = "aam_direct"
SETTING = "direct_access"
# Solo HTTP, sulla rete interna: niente certificati da gestire né avvisi del browser
DEFAULTS = {"enabled": False}
# Intestazione che la pagina manda con ogni modifica: un altro sito non può aggiungerla senza un preflight CORS, a cui
# l'app non risponde
REQUEST_HEADER = "X-AAM-Request"
# Intestazioni di Ingress: sull'accesso diretto non devono mai arrivare all'app
INGRESS_HEADERS = (b"x-remote-user-name", b"x-remote-user-display-name", b"x-remote-user-id", b"x-ingress-path")
# Rete interna di Home Assistant: un client con questo indirizzo è il gateway Docker, non il PC vero
HASSIO_NETWORK = ipaddress.ip_network("172.30.32.0/23")

# Sessione: resta aperta su quel computer finché non si esce o la chiude un amministratore. Si chiude da sola solo se
# non viene usata per SESSION_UNUSED. Il cookie scade dopo COOKIE_MAX_AGE (Chrome non accetta di più) e si rinnova
SESSION_UNUSED = timedelta(days=90)
COOKIE_MAX_AGE = 400 * 24 * 3600
TOUCH_EVERY = timedelta(hours=1)
# Con la password provvisoria si può solo cambiarla, entro un'ora dal login
MUST_CHANGE_SESSION = timedelta(hours=1)
# La password provvisoria vale una settimana: poi l'amministratore ne crea un'altra
TEMPORARY_VALID = timedelta(days=7)
COOKIE = "aam_session"

# Password: scrypt con N=2^14, r=8, p=5 (16 MiB, costo pari ai parametri minimi OWASP), sale di 16 byte
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 5
SCRYPT_MAXMEM = 64 * 1024 * 1024
PASSWORD_MIN = 10
PASSWORD_MAX = 128
COMMON_PASSWORDS = {
    "1234567890",
    "0123456789",
    "1234512345",
    "1111111111",
    "0000000000",
    "qwertyuiop",
    "password12",
    "password123",
    "password1234",
    "passwordpassword",
    "abcdefghij",
    "abcd123456",
    "abc1234567",
    "iloveyou12",
    "qwerty1234",
    "1q2w3e4r5t",
    "q1w2e3r4t5",
    "asdfghjkl1",
    "hotel12345",
    "reception1",
    "reception123",
    "benvenuto1",
    "benvenuto123",
    "cambiami123",
}
# Password provvisorie: 12 caratteri senza quelli che si confondono (0/o, 1/l/i), a gruppi di 4
TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

# Tentativi sbagliati: (indirizzo, utente) 5 in 15 minuti; indirizzo 20 in 15 minuti (i PC dietro il gateway Docker
# contano come un indirizzo solo); utente da qualunque indirizzo 30 in un'ora. Poi 15 minuti di blocco, e ogni
# tentativo sbagliato finché il conto resta sopra il limite ne fa scattare un altro. Il tentativo conta prima del
# controllo della password: anche quelli in parallelo
LIMITS = (("pair", 5, 15 * 60), ("ip", 20, 15 * 60), ("user", 30, 3600))
# PC dietro il gateway Docker: un conto solo per tutti, con un limite più alto
HIDDEN_IP_LIMIT = 40
BLOCK_SECONDS = 15 * 60
# Conti tenuti in memoria al massimo: oltre si dimenticano i più vecchi
LIMITER_KEYS = 20000
# Calcoli della password in parallelo e in coda: oltre, la richiesta si rifiuta subito
HASH_WORKERS = 2
HASH_QUEUE = 8

# Connessioni al server dell'accesso diretto: HEADER_TIMEOUT secondi per mandare le intestazioni, BODY_TIMEOUT per il
# corpo, al massimo MAX_CONNECTIONS_PER_IP aperte da un indirizzo (il gateway Docker è uno solo) e MAX_CONNECTIONS in
# tutto
HEADER_TIMEOUT = 10
BODY_TIMEOUT = 30
MAX_CONNECTIONS_PER_IP = 100
MAX_CONNECTIONS = 400
# Corpo delle richieste: login e password piccoli, il resto (importazione CSV) fino a BODY_LIMIT
BODY_LIMIT = 5 * 1024 * 1024
SMALL_BODY_LIMIT = 16 * 1024
SMALL_BODY_PATHS = ("/api/login", "/api/logout", "/api/password")
# Nome nell'indirizzo: solo un IP o un nome della rete interna (contro il DNS rebinding)
_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?::[0-9]{1,5})?$")
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".intranet", ".private")


class DirectError(Exception):
    """Richiesta dell'accesso diretto non valida: messaggio per l'utente e codice HTTP."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------- Password ----------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str, username: str) -> str:
    """Perché la nuova password non va bene; vuoto se va bene."""
    if len(password) < PASSWORD_MIN:
        return f"la password deve avere almeno {PASSWORD_MIN} caratteri"
    if len(password) > PASSWORD_MAX:
        return f"la password può avere al massimo {PASSWORD_MAX} caratteri"
    simple = password.lower()
    if username and username.lower() in simple:
        return "la password non può contenere il nome utente"
    if simple in COMMON_PASSWORDS or len(set(simple)) < 4:
        return "password troppo facile da indovinare"
    return ""


def temporary_password() -> str:
    chars = "".join(secrets.choice(TEMP_ALPHABET) for _ in range(12))
    return "-".join(chars[i : i + 4] for i in range(0, 12, 4))


def normalize_username(value: str) -> str:
    """Come users.py: senza spazi ai lati e in minuscolo. Vuoto se non è un nome utente valido."""
    username = value.strip().lower()
    if not username or len(username) > 100 or any(c.isspace() or ord(c) < 32 for c in username):
        return ""
    return username


BUSY = "troppe richieste di accesso in questo momento: riprova tra poco"


class PasswordHasher:
    """Calcoli scrypt su thread propri, al massimo HASH_WORKERS insieme e HASH_QUEUE in attesa: i tentativi in massa non
    occupano i thread dell'app (database, controller) e oltre la coda si rifiutano subito."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="password")
        self._waiting = 0
        # Hash di una password a caso: chi non esiste costa quanto chi sbaglia la password
        self._dummy = ""

    def full(self) -> bool:
        return self._waiting >= HASH_WORKERS + HASH_QUEUE

    async def _run(self, func, *args):
        if self.full():
            raise DirectError(BUSY, 429)
        self._waiting += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(self._pool, func, *args)
        finally:
            self._waiting -= 1

    async def verify(self, password: str, stored: str | None) -> bool:
        if stored is None:
            if not self._dummy:
                self._dummy = await self._run(hash_password, secrets.token_hex(16))
            await self._run(verify_password, password, self._dummy)
            return False
        return await self._run(verify_password, password, stored)

    async def hash(self, password: str) -> str:
        return await self._run(hash_password, password)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------- Tentativi sbagliati ----------


@dataclass
class LoginLimiter:
    """Tentativi sbagliati in memoria (all'avvio dell'add-on si riparte da zero)."""

    # In ordine di ultimo tentativo e di fine del blocco: i più vecchi si tolgono dall'inizio
    failures: OrderedDict[tuple, deque] = field(default_factory=OrderedDict)
    blocked_until: OrderedDict[tuple, float] = field(default_factory=OrderedDict)
    clock: Callable[[], float] = time.monotonic
    max_keys: int = LIMITER_KEYS

    @staticmethod
    def _keys(ip: str, username: str) -> list[tuple[tuple, int, int]]:
        # ip vuoto: PC dietro il gateway Docker, contati insieme
        keys = []
        for kind, limit, window in LIMITS:
            if kind == "pair":
                keys.append(((kind, ip, username), limit, window))
            elif kind == "ip":
                keys.append(((kind, ip), limit if ip else HIDDEN_IP_LIMIT, window))
            elif kind == "user":
                keys.append(((kind, username), limit, window))
        return keys

    def blocked(self, ip: str, username: str) -> int:
        """Secondi di blocco che restano, 0 se si può provare."""
        now = self.clock()
        left = 0
        for key, _, _ in self._keys(ip, username):
            until = self.blocked_until.get(key, 0)
            if until > now:
                left = max(left, int(until - now) + 1)
            elif key in self.blocked_until:
                del self.blocked_until[key]
        return left

    def attempt(self, ip: str, username: str) -> list[tuple]:
        """Un tentativo, contato prima del controllo della password. Restituisce i blocchi appena scattati."""
        now = self.clock()
        started = []
        for key, limit, window in self._keys(ip, username):
            times = self.failures.pop(key, None) or deque()
            self.failures[key] = times
            while times and times[0] <= now - window:
                times.popleft()
            times.append(now)
            # Il conto non si azzera col blocco: finito il blocco, un altro sbaglio sopra il limite blocca di nuovo
            # (per l'utente al massimo 30 tentativi in un'ora, più uno ogni blocco)
            if len(times) >= limit and self.blocked_until.get(key, 0) <= now:
                self.blocked_until.pop(key, None)
                self.blocked_until[key] = now + BLOCK_SECONDS
                started.append(key)
        self._prune(now)
        return started

    def success(self, ip: str, username: str, started: list[tuple] | tuple = ()) -> None:
        """Password giusta: il tentativo non conta, né quelli sbagliati prima da questo indirizzo. started: i blocchi
        fatti scattare da questo stesso tentativo (contato prima del controllo), che non valgono."""
        for key in started:
            self.blocked_until.pop(key, None)
        for key, _, _ in self._keys(ip, username):
            times = self.failures.get(key)
            if times:
                if key[0] == "pair":
                    times.clear()
                else:
                    times.pop()

    def unblock(self, username: str) -> None:
        """L'amministratore ha dato una password nuova: l'utente può riprovare subito, anche se il blocco è quello di
        tutti i PC dietro il gateway Docker."""
        for store in (self.failures, self.blocked_until):
            for key in [
                k
                for k in store
                if (k[0] == "user" and k[1] == username) or (k[0] == "pair" and k[2] == username) or k == ("ip", "")
            ]:
                del store[key]

    def _prune(self, now: float) -> None:
        longest = max(window for _, _, window in LIMITS)
        while self.failures:
            key, times = next(iter(self.failures.items()))
            if len(self.failures) <= self.max_keys and times and times[-1] > now - longest:
                break
            del self.failures[key]
        while self.blocked_until:
            key, until = next(iter(self.blocked_until.items()))
            if len(self.blocked_until) <= self.max_keys and until > now:
                break
            del self.blocked_until[key]


def client_ip(request) -> str:
    """Indirizzo del PC; vuoto se è il gateway Docker di Home Assistant (tutti i PC sembrerebbero lo stesso)."""
    host = request.client.host if request.client else ""
    try:
        if ipaddress.ip_address(host) in HASSIO_NETWORK:
            return ""
    except ValueError:
        return ""
    return host


def host_problem(host: str) -> str:
    """Perché il nome nell'indirizzo (intestazione Host) non va bene; vuoto se va bene."""
    if not host or not _HOST.match(host):
        return "indirizzo non valido"
    name = host[1 : host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(name)
        return ""
    except ValueError:
        pass
    if "." not in name or name.lower().endswith(LOCAL_SUFFIXES):
        return ""
    # Si legge prima del login, da chiunque: senza Home Assistant (vedi plain.py)
    return "indirizzo non accettato: apri l'app con l'indirizzo IP del server"


# ---------- Sessioni ----------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def _parse(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def session_valid(session: dict, password: dict | None, now: datetime) -> bool:
    """Sessione ancora buona: l'utente ha la password con cui è entrato, la sessione è stata usata negli ultimi
    SESSION_UNUSED, e con la password provvisoria (da cambiare) non è più vecchia di MUST_CHANGE_SESSION."""
    if password is None or session.get("password_set_at") != password.get("set_at"):
        return False
    last = _parse(session.get("last_seen") or "")
    created = _parse(session.get("created_at") or "")
    if last is None or created is None or now - last > SESSION_UNUSED:
        return False
    return not (password.get("must_change") and now - created > MUST_CHANGE_SESSION)


def temporary_expired(password: dict, now: datetime) -> bool:
    if not password.get("must_change"):
        return False
    set_at = _parse(password.get("set_at") or "")
    return set_at is None or now - set_at > TEMPORARY_VALID


def short_agent(agent: str) -> str:
    """Browser e sistema in poche parole, per riconoscere il computer nella pagina Utenti."""
    browser = next(
        (
            name
            for key, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if key in agent
        ),
        "",
    )
    system = next(
        (
            name
            for key, name in (
                ("Windows", "Windows"),
                ("Android", "Android"),
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Mac OS", "Mac"),
                ("Linux", "Linux"),
                ("CrOS", "ChromeOS"),
            )
            if key in agent
        ),
        "",
    )
    return " su ".join(x for x in (browser, system) if x) or "Browser"


# ---------- Impostazioni ----------


def load_settings(db: Database) -> dict:
    try:
        saved = json.loads(db.get_setting(SETTING) or "{}")
    except ValueError:
        saved = {}
    settings = dict(DEFAULTS)
    if isinstance(saved, dict):
        settings.update({k: saved[k] for k in DEFAULTS if k in saved and type(saved[k]) is type(DEFAULTS[k])})
    return settings


# ---------- Server ----------


def mark_direct(app):
    """L'app vista dal server dell'accesso diretto: richieste segnate, senza le intestazioni di Ingress, con il corpo
    limitato (BODY_LIMIT, SMALL_BODY_LIMIT per login e password)."""

    async def direct_app(scope, receive, send):
        if scope["type"] == "websocket":
            # L'app non ha WebSocket: dall'accesso diretto si rifiutano tutti
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http":
            scope = dict(scope)
            scope[DIRECT_SCOPE] = True
            scope["headers"] = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in INGRESS_HEADERS]
            limit = SMALL_BODY_LIMIT if scope.get("path") in SMALL_BODY_PATHS else BODY_LIMIT
            length = next((v for k, v in scope["headers"] if k.lower() == b"content-length"), None)
            if length is not None and (not length.strip().isdigit() or int(length) > limit):
                await _too_large(send)
                return
            receive, send = _limit_body(receive, send, limit)
        await app(scope, receive, send)

    return direct_app


async def _too_large(send) -> None:
    body = json.dumps({"detail": "richiesta troppo grande"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _limit_body(receive, send, limit: int):
    """Corpo letto al massimo fino a limit: oltre, risposta 413 (se l'app non ha già risposto) e per l'app il client
    se n'è andato."""
    state = {"read": 0, "started": False, "rejected": False}

    async def limited_receive():
        if state["read"] > limit:
            return {"type": "http.disconnect"}
        message = await receive()
        if message["type"] == "http.request":
            state["read"] += len(message.get("body", b""))
            if state["read"] > limit:
                if not state["started"]:
                    state["rejected"] = True
                    await _too_large(send)
                return {"type": "http.disconnect"}
        return message

    async def guarded_send(message):
        if state["rejected"]:
            return
        if message["type"] == "http.response.start":
            state["started"] = True
        await send(message)

    return limited_receive, guarded_send


def is_direct(request) -> bool:
    return request.scope.get(DIRECT_SCOPE) is True


class _GuardedH11(H11Protocol):
    """HTTP del server dell'accesso diretto: chiude le connessioni che non mandano la richiesta entro HEADER_TIMEOUT
    (uvicorn le terrebbe aperte per sempre) e rifiuta le connessioni oltre i limiti per indirizzo e in tutto."""

    _header_timer: asyncio.TimerHandle | None = None
    _ip = ""
    _deadline_cycle = None
    _body_deadline = 0.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Niente righe per ogni richiesta (senza toccare il log del server principale)
        self.access_log = False

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        counts = _connections_per_ip(self.server_state)
        self._ip = self.client[0] if self.client else ""
        counts[self._ip] = counts.get(self._ip, 0) + 1
        if counts[self._ip] > MAX_CONNECTIONS_PER_IP or sum(counts.values()) > MAX_CONNECTIONS:
            transport.close()
            return
        self._arm_header_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_header_timer()
        counts = _connections_per_ip(self.server_state)
        left = counts.get(self._ip, 1) - 1
        if left > 0:
            counts[self._ip] = left
        else:
            counts.pop(self._ip, None)
        super().connection_lost(exc)

    def on_response_complete(self) -> None:
        super().on_response_complete()
        # Pronta per la prossima richiesta: anche quella deve arrivare in tempo
        if not self.transport.is_closing():
            self._arm_header_timer()

    def _arm_header_timer(self) -> None:
        self._cancel_header_timer()
        self._header_timer = self.loop.call_later(HEADER_TIMEOUT, self._header_timeout)

    def _cancel_header_timer(self) -> None:
        if self._header_timer is not None:
            self._header_timer.cancel()
            self._header_timer = None

    def _header_timeout(self) -> None:
        self._header_timer = None
        if self.cycle is not None and not self.cycle.response_complete:
            # Corpo arrivato: la richiesta la sta gestendo l'app, anche se ci mette molto
            if self.conn.their_state is not h11.SEND_BODY:
                return
            # Corpo non ancora arrivato tutto: al massimo BODY_TIMEOUT
            now = self.loop.time()
            if self._deadline_cycle is not self.cycle:
                self._deadline_cycle = self.cycle
                self._body_deadline = now + BODY_TIMEOUT
            if now < self._body_deadline:
                wait = min(HEADER_TIMEOUT, self._body_deadline - now)
                self._header_timer = self.loop.call_later(wait, self._header_timeout)
                return
        if not self.transport.is_closing():
            self.transport.close()


def _connections_per_ip(server_state) -> dict[str, int]:
    counts = getattr(server_state, "aam_per_ip", None)
    if counts is None:
        counts = server_state.aam_per_ip = {}
    return counts


def _raise_open_files() -> None:
    """Più file aperti consentiti (fino al massimo del sistema): le connessioni dell'accesso diretto non tolgono posto
    a Ingress, al controller e al database."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, ValueError, OSError):
        _LOGGER.debug("Limite dei file aperti non cambiato", exc_info=True)


class _Server(uvicorn.Server):
    """Secondo server nello stesso processo: i segnali di arresto li gestisce il server principale."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class DirectServer:
    """Avvia, ferma e riavvia il server dell'accesso diretto secondo le impostazioni."""

    def __init__(self, app, port: int = DIRECT_PORT) -> None:
        self.app = app
        self.port = port
        self._server: _Server | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.running = False
        self.error = ""
        self.since = ""
        # Porta vera (con port=0 nei test la sceglie il sistema)
        self.bound_port = 0

    def status(self) -> dict:
        return {
            "running": self.running,
            "error": self.error,
            "since": self.since or None,
            "port": self.port,
        }

    async def apply(self, settings: dict) -> None:
        """Server acceso con le impostazioni date, o spento. Un errore (porta occupata) resta in error."""
        async with self._lock:
            await self._stop()
            self.error = ""
            if not settings.get("enabled"):
                return
            try:
                await self._start(settings)
            except Exception as err:  # noqa: BLE001 - l'add-on resta in piedi, l'errore si vede nelle impostazioni
                self.error = _error_text(err)
                _LOGGER.warning("Accesso diretto non avviato: %s", self.error)
                await self._stop()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _start(self, settings: dict) -> None:
        _raise_open_files()
        # log_config e log_level None: il logging di tutto il processo (e del server di Ingress) resta com'è
        config = uvicorn.Config(
            mark_direct(self.app),
            lifespan="off",
            http=_GuardedH11,
            ws="none",
            proxy_headers=False,
            server_header=False,
            log_config=None,
            log_level=None,
            timeout_graceful_shutdown=3,
        )
        config.load()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
        except OSError:
            sock.close()
            raise
        self.bound_port = sock.getsockname()[1]
        server = _Server(config)
        self._server = server
        self._task = asyncio.create_task(self._serve(server, sock), name="accesso-diretto")
        # Avviato davvero (o fallito subito)
        for _ in range(100):
            if server.started or self._task.done():
                break
            await asyncio.sleep(0.02)
        if self._task.done():
            error = self._task.exception() if not self._task.cancelled() else None
            raise error or DirectError("server dell'accesso diretto fermato")
        self.running = True
        self.since = datetime.now(tz=UTC).isoformat(timespec="seconds")
        _LOGGER.info("Accesso diretto in ascolto sulla porta %s", self.port)

    async def _serve(self, server: _Server, sock: socket.socket) -> None:
        try:
            await server.serve(sockets=[sock])
        except SystemExit as err:
            # uvicorn chiude il processo con sys.exit su alcuni errori: qui deve fermarsi solo questo server
            raise DirectError("server dell'accesso diretto fermato") from err
        finally:
            sock.close()
            if self._server is server:
                self.running = False

    async def _stop(self) -> None:
        server, task = self._server, self._task
        self._server, self._task = None, None
        self.running = False
        self.since = ""
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except TimeoutError:
            server.force_exit = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:  # noqa: BLE001 - già fermato con un errore
            _LOGGER.debug("Server dell'accesso diretto fermato con un errore", exc_info=True)


def _error_text(err: BaseException) -> str:
    if isinstance(err, DirectError):
        return str(err)
    if isinstance(err, OSError) and err.errno in (98, 48):
        return "porta già in uso"
    text = str(err) or type(err).__name__
    return f"errore: {text}"
