"""Copie coerenti del database: prima di ogni backup di Home Assistant e prima degli aggiornamenti.

Home Assistant copia /data mentre l'add-on gira: un database preso a metà scrittura può risultare danneggiato.
Subito prima del backup (backup_pre nel config.yaml) si prepara una copia con l'API di backup di SQLite,
che è sempre coerente; all'avvio, se il database è danneggiato, si riparte da quella copia.

Il file gira anche da solo (python3 app/backup_copy.py): usa solo la libreria standard.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

DB_PATH = Path("/data/axis_access_manager.db")
# Copia per il backup di Home Assistant
HA_COPY_DIR = "copia_backup"
# Perché l'ultimo backup_pre non ha aggiornato la copia (il file c'è solo in quel caso)
HA_COPY_ERROR = "errore.txt"
# Inizio dei messaggi di recover() quando il database è tornato quello della copia
RESTORED = "ripristinata la copia del backup"
# Nei messaggi di recover() e replace_unusable() quando l'app riparte da un database nuovo, vuoto
STARTED_EMPTY = "l'app è ripartita con un database vuoto"
# Copie prima degli aggiornamenti dello schema o dell'add-on
UPDATE_COPY_DIR = "copie_database"
UPDATE_COPIES_KEPT = 3
# Copia a blocchi di pagine: tra un blocco e l'altro l'add-on può scrivere (in un passo solo aspettava fino a
# 5 s e poi la sua scrittura falliva). Ogni scrittura però fa ripartire la copia: dopo COPY_STEPS_FOR secondi
# la si finisce tenendo ferma la lettura del database, sempre a blocchi, e dopo COPY_GIVE_UP secondi si rinuncia.
COPY_STEP_PAGES = 1024
COPY_STEPS_FOR = 30
COPY_GIVE_UP = 120
# Errori con cui SQLite dice che il file è rovinato; gli altri (bloccato, permessi, disco) non lo dicono
CORRUPT_CODES = (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


class DamagedError(Exception):
    """Database rovinato secondo SQLite."""


class _Slow(Exception):
    """Copia a blocchi che non finisce: la si rifà tenendo ferma la lettura del database."""


def _sync_dir(folder: Path) -> None:
    """Rende definitivi su disco i nomi appena cambiati nella cartella."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def corrupt_error(err: sqlite3.Error) -> bool:
    return (getattr(err, "sqlite_errorcode", 0) & 0xFF) in CORRUPT_CODES


def _backup(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    start = time.monotonic()

    def in_steps(status: int, remaining: int, pages: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() - start > COPY_STEPS_FOR:
            raise _Slow

    def held(status: int, remaining: int, pages: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() - start > COPY_GIVE_UP:
            raise TimeoutError("copia del database troppo lenta")

    try:
        src.backup(dst, pages=COPY_STEP_PAGES, progress=in_steps)
    except _Slow:
        # Una transazione di lettura aperta tiene ferma la versione del database: le scritture non fanno più
        # ripartire la copia e, con il WAL, non aspettano. In un passo solo il limite non scattava mai: SQLite
        # avvisa dell'avanzamento solo a copia finita
        src.execute("BEGIN")
        src.execute("SELECT count(*) FROM sqlite_master").fetchall()
        try:
            src.backup(dst, pages=COPY_STEP_PAGES, progress=held)
        finally:
            src.rollback()


def consistent_copy(source: Path, target: Path, check: bool = False) -> None:
    """Copia coerente anche con il database in uso; il file finale compare solo a copia finita.
    check: prima di sostituire target si controlla la copia (DamagedError se è rovinata)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".parziale")
    partial.unlink(missing_ok=True)
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(partial)
            # Niente fsync nell'ultimo passo, che tiene fermo l'add-on: si fa dopo, a database libero
            dst.execute("PRAGMA synchronous = OFF")
            try:
                _backup(src, dst)
                # La copia prende il WAL dal database: senza, è un file solo, e leggerla (anche in sola lettura) non
                # lascia accanto -wal e -shm che finivano nel backup di Home Assistant
                dst.execute("PRAGMA journal_mode = DELETE")
            except sqlite3.DatabaseError as err:
                if check and corrupt_error(err):
                    raise DamagedError(str(err)) from err
                raise
            finally:
                dst.close()
        finally:
            src.close()
        with open(partial, "rb") as handle:
            os.fsync(handle.fileno())
        if check and (problem := damage(partial)):
            raise DamagedError(problem)
        os.replace(partial, target)
        # -wal e -shm della copia di prima (letta in sola lettura dalle versioni precedenti): con questa non c'entrano
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
    except BaseException:
        # Un .parziale lasciato sul disco pieno faceva fallire anche il tentativo successivo
        for leftover in (partial, Path(f"{partial}-journal")):
            leftover.unlink(missing_ok=True)
        raise
    _sync_dir(target.parent)


def ha_copy_path(db_path: Path) -> Path:
    return db_path.parent / HA_COPY_DIR / db_path.name


def ha_copy_error(db_path: Path) -> str:
    """Perché l'ultima copia per il backup di Home Assistant non è stata aggiornata ("" se è andata bene)."""
    try:
        text = (db_path.parent / HA_COPY_DIR / HA_COPY_ERROR).read_text().strip()
    except OSError:
        return ""
    # Il file c'è solo dopo un errore. Vuoto: con il disco pieno si crea, ma il motivo non si scrive. Prima la copia
    # risultava preparata, in verde
    return text or "copia non aggiornata (spazio su disco esaurito)"


def damage(path: Path) -> str:
    """Perché il database è rovinato secondo PRAGMA quick_check ("" se è sano).
    Si apre in scrittura: il journal lasciato da un'interruzione SQLite lo riporta indietro da solo, come
    all'apertura normale (in sola lettura non può, e un database sano sembrava rovinato). Gli errori che non
    dicono "rovinato" (file bloccato, permessi, disco) si sollevano: non vogliono dire danneggiato.
    Con il WAL rimasto accanto (add-on non chiuso: spegnimento improvviso, o cartella rimessa da un backup di Home
    Assistant fatto con l'add-on acceso) il controllo è completo: database e WAL copiati in momenti diversi danno
    indici che non tornano con le tabelle, e quick_check non lo vede. Anche con il WAL vuoto: copiato subito dopo lo
    svuotamento del backup_pre, mentre il database cambiava ancora. La chiusura normale dell'add-on toglie il WAL."""
    check = "integrity_check" if Path(f"{path}-wal").exists() else "quick_check"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30)
        try:
            result = conn.execute(f"PRAGMA {check}").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as err:
        if corrupt_error(err):
            return str(err) or "file rovinato"
        raise
    return "" if result == "ok" else str(result)


def is_healthy(path: Path) -> bool:
    """Sano e leggibile: con un errore qualsiasi non lo si usa come copia."""
    try:
        return not damage(path)
    except sqlite3.Error:
        return False


def error_reason(err: Exception) -> str:
    if isinstance(err, TimeoutError):
        return "copia troppo lenta"
    full = getattr(err, "errno", None) == errno.ENOSPC or getattr(err, "sqlite_errorcode", 0) == sqlite3.SQLITE_FULL
    return "spazio su disco esaurito" if full else "errore di lettura o scrittura dei file"


def _has_data(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def recover(db_path: Path) -> str:
    """All'avvio, prima di aprire il database: se SQLite lo dice danneggiato (o manca) e la copia del backup è sana,
    si mette da parte il danneggiato e si riparte dalla copia. Restituisce cosa è successo ("" se è tutto a posto)."""
    restoring = db_path.with_name(db_path.name + ".ripristino")
    if restoring.exists() and not _has_data(db_path) and is_healthy(restoring):
        # Interrotto tra lo spostamento del danneggiato e l'arrivo della copia: si finisce
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(restoring, db_path)
        _sync_dir(db_path.parent)
        return f"{RESTORED}: il ripristino si era interrotto all'avvio precedente"
    # Resti di un ripristino interrotto prima della fine della copia
    for leftover in (restoring, Path(f"{restoring}.parziale"), Path(f"{restoring}.parziale-journal")):
        leftover.unlink(missing_ok=True)
    copy = ha_copy_path(db_path)
    if not _has_data(db_path):
        # Senza copia è la prima installazione; con la copia il database è sparito (ripristino interrotto)
        if not copy.exists():
            return ""
        problem = "database mancante"
    else:
        try:
            problem = damage(db_path)
        except sqlite3.Error as err:
            # Non si sa se è danneggiato: non si tocca, l'apertura dirà il resto
            _LOGGER.error("Controllo del database all'avvio non riuscito: %s", err)
            return "controllo del database all'avvio non riuscito: database aperto senza ripristino"
        if not problem:
            return ""
        _LOGGER.error("Database danneggiato: %s", problem)
        problem = "database danneggiato"
    if not copy.exists() or not is_healthy(copy):
        if not _has_data(db_path):
            # L'apertura crea un database nuovo
            return f"{problem} e nessuna copia sana del backup da cui ripartire: {STARTED_EMPTY}"
        # Il danneggiato resta: se SQLite lo apre l'app parte con quello, se no ci pensa replace_unusable()
        return f"{problem} e nessuna copia sana del backup da cui ripartire"
    when = datetime.fromtimestamp(copy.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    moved = ""
    if _has_data(db_path):
        # Il danneggiato si mette da parte subito, prima della copia (lunga). Se l'avvio si interrompe o manca lo
        # spazio, all'avvio dopo il database manca e si riparte dalla copia. Prima restava al suo posto: il controllo
        # completo, che vede il danno solo con il WAL accanto (e alla chiusura lo toglie), non lo vedeva più, l'app
        # partiva con il database rovinato e il backup dopo ne faceva la copia
        moved = f" spostato in {_set_aside(db_path).name}"
        _sync_dir(db_path.parent)
    try:
        consistent_copy(copy, restoring)
    except (OSError, sqlite3.Error) as err:
        _LOGGER.error("Copia del backup non ripristinata: %s", err)
        # Disco pieno: la copia sana si sposta al posto del database (non serve spazio), come in replace_unusable().
        # Prima, con il database mancante, l'apertura ne creava uno vuoto, all'avvio dopo non si riprovava più e il
        # backup successivo sostituiva la copia sana con quello
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(copy, db_path)
        _sync_dir(db_path.parent)
        _sync_dir(copy.parent)
        return (
            f"{problem}{moved}, {RESTORED} del {when}, spostata al posto del database: si rifà al prossimo backup di "
            "Home Assistant"
        )
    # Journal senza database: sulla copia la rovinerebbe
    for suffix in ("-journal", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    os.replace(restoring, db_path)
    _sync_dir(db_path.parent)
    return f"{problem}{moved}, {RESTORED} del {when}"


def _set_aside(db_path: Path) -> Path:
    """Database danneggiato messo da parte, con _danneggiato_ nel nome (fuori dai backup di Home Assistant)."""
    broken = db_path.with_name(f"{db_path.stem}_danneggiato_{datetime.now():%Y%m%d_%H%M%S}{db_path.suffix}")
    # Journal e WAL (con il suo indice -shm) vanno con il file danneggiato: lì servono a chi vuole recuperarne
    # le ultime modifiche, sulla copia la rovinerebbero
    for suffix in ("-journal", "-wal", "-shm"):
        side = Path(f"{db_path}{suffix}")
        if side.exists():
            os.replace(side, Path(f"{broken}{suffix}"))
    os.replace(db_path, broken)
    return broken


def replace_unusable(db_path: Path) -> str:
    """Database che SQLite non apre, rimasto al suo posto dopo recover(): nessuna copia sana, o copia che non si è
    potuta duplicare (disco pieno). Senza questo l'add-on non partiva più, e i backup dell'app non si raggiungevano.
    Il danneggiato si mette da parte; la copia sana si sposta al posto del database (non serve spazio) e si rifà al
    prossimo backup di Home Assistant. Mai un database vuoto con una copia sana: il backup successivo la
    sostituirebbe con quello. Restituisce cosa è successo."""
    copy = ha_copy_path(db_path)
    healthy = copy.exists() and is_healthy(copy)
    broken = _set_aside(db_path)
    if healthy:
        when = datetime.fromtimestamp(copy.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
        os.replace(copy, db_path)
        _sync_dir(db_path.parent)
        _sync_dir(copy.parent)
        return (
            f"database danneggiato spostato in {broken.name}, {RESTORED} del {when}, spostata al posto del database: "
            "si rifà al prossimo backup di Home Assistant"
        )
    _sync_dir(db_path.parent)
    return (
        f"database danneggiato spostato in {broken.name} e nessuna copia sana del backup da cui ripartire: "
        f"{STARTED_EMPTY}"
    )


def update_copy(db_path: Path, reason: str) -> Path:
    """Copia prima di un aggiornamento; si tengono le ultime UPDATE_COPIES_KEPT."""
    folder = db_path.parent / UPDATE_COPY_DIR
    safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in reason)[:40]
    target = folder / f"{db_path.stem}_{datetime.now():%Y%m%d_%H%M%S}_{safe}{db_path.suffix}"
    # Resti di copie interrotte (spegnimento, o arresto oltre il tempo concesso dal Supervisor): grandi fino al
    # database, non si vedevano nell'app e non si toglievano più
    for leftover in folder.glob(f"{db_path.stem}_*{db_path.suffix}.parziale*"):
        leftover.unlink(missing_ok=True)
    # Prima si tolgono le copie in più: con il disco quasi pieno la nuova trova il posto della più vecchia
    copies = sorted(folder.glob(f"{db_path.stem}_*{db_path.suffix}"), key=lambda p: p.stat().st_mtime)
    for old in copies[: max(len(copies) - UPDATE_COPIES_KEPT + 1, 0)]:
        old.unlink(missing_ok=True)
    consistent_copy(db_path, target)
    return target


def update_copies(db_path: Path) -> list[dict]:
    folder = db_path.parent / UPDATE_COPY_DIR
    copies = sorted(folder.glob(f"{db_path.stem}_*{db_path.suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "size": p.stat().st_size,
            "at": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        }
        for p in copies
    ]


def _checkpoint(path: Path) -> None:
    """Scritture del WAL riportate nel database e WAL svuotato, subito prima che Home Assistant copi la cartella con
    l'add-on acceso: finché il WAL non torna grande il file del database non cambia, e nel backup database e WAL
    restano coerenti. Le scritture dell'add-on aspettano al massimo il timeout."""
    conn = sqlite3.connect(path, timeout=2)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()


def main() -> int:
    """Chiamato da Home Assistant prima di ogni backup. Esce sempre con 0: con un errore Home Assistant lascerebbe
    fuori dal backup tutto l'add-on, mentre il database vivo e la copia precedente nel backup ci sono comunque."""
    path = Path(os.environ.get("AAM_DB_PATH") or DB_PATH)
    if not path.exists():
        print("Nessun database da copiare")
        return 0
    target = ha_copy_path(path)
    status = target.parent / HA_COPY_ERROR
    try:
        consistent_copy(path, target, check=True)
    except DamagedError as err:
        # La copia sana di prima resta: sostituirla con una rovinata toglieva l'unica via d'uscita
        error = "database danneggiato: resta la copia precedente"
        print(f"Database danneggiato ({err}): la copia precedente in {target} resta", file=sys.stderr)
    except Exception as err:  # noqa: BLE001 - il backup di Home Assistant deve andare avanti
        error = f"copia non aggiornata ({error_reason(err)})"
        print(f"Copia del database non riuscita: {err}", file=sys.stderr)
    else:
        error = ""
        print(f"Copia coerente del database pronta in {target}")
    try:
        _checkpoint(path)
    except sqlite3.Error as err:
        print(f"WAL del database non svuotato prima del backup: {err}", file=sys.stderr)
    with contextlib.suppress(OSError):
        if error:
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text(error)
        else:
            status.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
