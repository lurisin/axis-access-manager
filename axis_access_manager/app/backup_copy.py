"""Copie coerenti del database: prima di ogni backup di Home Assistant e prima degli aggiornamenti.

Home Assistant copia /data mentre l'add-on gira: un database preso a metà scrittura può risultare danneggiato.
Subito prima del backup (backup_pre nel config.yaml) si prepara una copia con l'API di backup di SQLite,
che è sempre coerente; all'avvio, se il database è danneggiato, si riparte da quella copia.

Il file gira anche da solo (python3 app/backup_copy.py): usa solo la libreria standard.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/data/axis_access_manager.db")
# Copia per il backup di Home Assistant
HA_COPY_DIR = "copia_backup"
# Copie prima degli aggiornamenti dello schema o dell'add-on
UPDATE_COPY_DIR = "copie_database"
UPDATE_COPIES_KEPT = 3


def consistent_copy(source: Path, target: Path) -> None:
    """Copia coerente anche con il database in uso; il file finale compare solo a copia finita."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".parziale")
    partial.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(partial, target)


def ha_copy_path(db_path: Path) -> Path:
    return db_path.parent / HA_COPY_DIR / db_path.name


def is_healthy(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def recover(db_path: Path) -> str:
    """Database danneggiato e copia del backup sana: si mette da parte il danneggiato e si usa la copia."""
    if not db_path.exists() or is_healthy(db_path):
        return ""
    copy = ha_copy_path(db_path)
    if not copy.exists() or not is_healthy(copy):
        return ""
    broken = db_path.with_name(f"{db_path.stem}_danneggiato_{datetime.now():%Y%m%d_%H%M%S}{db_path.suffix}")
    os.replace(db_path, broken)
    for suffix in ("-journal", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    consistent_copy(copy, db_path)
    return f"database danneggiato spostato in {broken.name}, ripristinata la copia del backup"


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
    """Chiamato da Home Assistant prima di ogni backup."""
    path = Path(os.environ.get("AAM_DB_PATH") or DB_PATH)
    if not path.exists():
        print("Nessun database da copiare")
        return 0
    consistent_copy(path, ha_copy_path(path))
    print(f"Copia coerente del database pronta in {ha_copy_path(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
