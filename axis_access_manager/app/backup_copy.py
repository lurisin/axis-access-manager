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
# Copie prima degli aggiornamenti dello schema o dell'add-on
UPDATE_COPY_DIR = "copie_database"
UPDATE_COPIES_KEPT = 3
# Copia a blocchi di pagine: tra un blocco e l'altro l'add-on può scrivere (in un passo solo aspettava fino a
# 5 s e poi la sua scrittura falliva). Ogni scrittura però fa ripartire la copia: dopo COPY_STEPS_FOR secondi
# la si finisce in un passo solo, e dopo COPY_GIVE_UP secondi si rinuncia.
COPY_STEP_PAGES = 1024
COPY_STEPS_FOR = 30
COPY_GIVE_UP = 120
# Errori con cui SQLite dice che il file è rovinato; gli altri (bloccato, permessi, disco) non lo dicono
CORRUPT_CODES = (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


class DamagedError(Exception):
    """Database rovinato secondo SQLite."""


class _Slow(Exception):
    """Copia a blocchi che non finisce: la si rifà in un passo solo."""


def _sync_dir(folder: Path) -> None:
    """Rende definitivi su disco i nomi appena cambiati nella cartella."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _corrupt(err: sqlite3.Error) -> bool:
    return (getattr(err, "sqlite_errorcode", 0) & 0xFF) in CORRUPT_CODES


def _backup(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    start = time.monotonic()

    def in_steps(status: int, remaining: int, pages: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() - start > COPY_STEPS_FOR:
            raise _Slow

    def at_once(status: int, remaining: int, pages: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() - start > COPY_GIVE_UP:
            raise TimeoutError("copia del database troppo lenta")

    try:
        src.backup(dst, pages=COPY_STEP_PAGES, progress=in_steps)
    except _Slow:
        src.backup(dst, progress=at_once)


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
            except sqlite3.DatabaseError as err:
                if check and _corrupt(err):
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
        return (db_path.parent / HA_COPY_DIR / HA_COPY_ERROR).read_text().strip()
    except OSError:
        return ""


def damage(path: Path) -> str:
    """Perché il database è rovinato secondo PRAGMA quick_check ("" se è sano).
    Si apre in scrittura: il journal lasciato da un'interruzione SQLite lo riporta indietro da solo, come
    all'apertura normale (in sola lettura non può, e un database sano sembrava rovinato). Gli errori che non
    dicono "rovinato" (file bloccato, permessi, disco) si sollevano: non vogliono dire danneggiato."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as err:
        if _corrupt(err):
            return str(err) or "file rovinato"
        raise
    return "" if result == "ok" else str(result)


def is_healthy(path: Path) -> bool:
    """Sano e leggibile: con un errore qualsiasi non lo si usa come copia."""
    try:
        return not damage(path)
    except sqlite3.Error:
        return False


def _reason(err: Exception) -> str:
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
        return "ripristinata la copia del backup: il ripristino si era interrotto all'avvio precedente"
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
        return f"{problem} e nessuna copia sana del backup da cui ripartire"
    # Prima la copia accanto al database: se non riesce (disco pieno) il database resta dov'è e si riprova
    try:
        consistent_copy(copy, restoring)
    except (OSError, sqlite3.Error) as err:
        _LOGGER.error("Copia del backup non ripristinata: %s", err)
        return f"{problem}, copia del backup non ripristinata ({_reason(err)}): si riprova al prossimo avvio"
    when = datetime.fromtimestamp(copy.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    if not _has_data(db_path):
        # Journal senza database: sulla copia la rovinerebbe
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(restoring, db_path)
        _sync_dir(db_path.parent)
        return f"{problem}, ripristinata la copia del backup del {when}"
    broken = db_path.with_name(f"{db_path.stem}_danneggiato_{datetime.now():%Y%m%d_%H%M%S}{db_path.suffix}")
    # Il journal va con il file danneggiato: lì serve a chi vuole recuperarne qualcosa, sulla copia la rovinerebbe
    for suffix in ("-journal", "-wal"):
        side = Path(f"{db_path}{suffix}")
        if side.exists():
            os.replace(side, Path(f"{broken}{suffix}"))
    Path(f"{db_path}-shm").unlink(missing_ok=True)
    os.replace(db_path, broken)
    os.replace(restoring, db_path)
    _sync_dir(db_path.parent)
    return f"{problem} spostato in {broken.name}, ripristinata la copia del backup del {when}"


def update_copy(db_path: Path, reason: str) -> Path:
    """Copia prima di un aggiornamento; si tengono le ultime UPDATE_COPIES_KEPT."""
    folder = db_path.parent / UPDATE_COPY_DIR
    safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in reason)[:40]
    target = folder / f"{db_path.stem}_{datetime.now():%Y%m%d_%H%M%S}_{safe}{db_path.suffix}"
    consistent_copy(db_path, target)
    copies = sorted(folder.glob(f"{db_path.stem}_*{db_path.suffix}"), key=lambda p: p.stat().st_mtime)
    for old in copies[:-UPDATE_COPIES_KEPT]:
        old.unlink(missing_ok=True)
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
        error = f"copia non aggiornata ({_reason(err)})"
        print(f"Copia del database non riuscita: {err}", file=sys.stderr)
    else:
        error = ""
        print(f"Copia coerente del database pronta in {target}")
    with contextlib.suppress(OSError):
        if error:
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text(error)
        else:
            status.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
