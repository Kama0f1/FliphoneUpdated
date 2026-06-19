"""Create a verified encrypted SQLite snapshot of the configured PostgreSQL database."""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile

import asyncpg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import SQLITE_SCHEMA, TABLE_ORDER


DEFAULT_BACKUP_DIR = Path(r"F:\Backups\Fliphone")
DEFAULT_KEY_PATH = Path(r"F:\Secrets\fliphone-backup.key")
KEEP_BACKUPS = 4
INTERVAL = timedelta(days=7)


def _key_path() -> Path:
    return Path(os.getenv("BACKUP_KEY_PATH", str(DEFAULT_KEY_PATH)))


def _backup_dir() -> Path:
    return Path(os.getenv("BACKUP_DIR", str(DEFAULT_BACKUP_DIR)))


def initialize_key() -> Path:
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"), encoding="ascii")
    return path


def load_key() -> bytes:
    path = _key_path()
    raw = base64.urlsafe_b64decode(path.read_text(encoding="ascii").strip())
    if len(raw) != 32:
        raise RuntimeError("Backup key must decode to exactly 32 bytes")
    return raw


def due(state_path: Path) -> bool:
    if not state_path.exists():
        return True
    try:
        last = datetime.fromisoformat(json.loads(state_path.read_text(encoding="utf-8"))["last_success"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return True
    return datetime.now(timezone.utc) - last >= INTERVAL


async def export_snapshot(database_url: str, target: Path) -> dict[str, int]:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2, statement_cache_size=0)
    counts: dict[str, int] = {}
    try:
        sqlite = sqlite3.connect(target)
        try:
            sqlite.executescript(SQLITE_SCHEMA)
            for table in TABLE_ORDER:
                rows = await pool.fetch(f'SELECT * FROM "{table}"')
                counts[table] = len(rows)
                if not rows:
                    continue
                columns = list(rows[0].keys())
                placeholders = ", ".join("?" for _ in columns)
                quoted = ", ".join(f'"{column}"' for column in columns)
                sqlite.executemany(
                    f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({placeholders})',
                    [tuple(row[column] for column in columns) for row in rows],
                )
            sqlite.commit()
        finally:
            sqlite.close()
    finally:
        await pool.close()
    return counts


def verify_sqlite(path: Path, expected: dict[str, int]) -> None:
    connection = sqlite3.connect(path)
    try:
        for table, count in expected.items():
            actual = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if actual != count:
                raise RuntimeError(f"Verification failed for {table}: expected {count}, got {actual}")
    finally:
        connection.close()


async def run(force: bool) -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    state_path = backup_dir / "backup-state.json"
    if not force and not due(state_path):
        print("Backup is not due yet.")
        return

    key = load_key()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"fliphone-{timestamp}.sqlite3.enc"
    with tempfile.TemporaryDirectory() as temp_dir:
        sqlite_path = Path(temp_dir) / "snapshot.sqlite3"
        verify_path = Path(temp_dir) / "verify.sqlite3"
        expected = await export_snapshot(database_url, sqlite_path)
        verify_sqlite(sqlite_path, expected)

        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, sqlite_path.read_bytes(), b"Fliphone SQLite backup v1")
        destination.write_bytes(b"FLIPBK1" + nonce + ciphertext)

        encrypted = destination.read_bytes()
        plaintext = AESGCM(key).decrypt(encrypted[7:19], encrypted[19:], b"Fliphone SQLite backup v1")
        verify_path.write_bytes(plaintext)
        verify_sqlite(verify_path, expected)

    backups = sorted(backup_dir.glob("fliphone-*.sqlite3.enc"), reverse=True)
    for old in backups[KEEP_BACKUPS:]:
        old.unlink()
    state_path.write_text(
        json.dumps({"last_success": datetime.now(timezone.utc).isoformat(), "file": destination.name}),
        encoding="utf-8",
    )
    print(f"Verified encrypted backup created: {destination}")
    print("Rows:", ", ".join(f"{table}={count}" for table, count in expected.items()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run even when the weekly backup is not due")
    parser.add_argument("--init-key", action="store_true", help="Create the external encryption key if missing")
    args = parser.parse_args()
    if args.init_key:
        print(f"Backup key ready: {initialize_key()}")
        return
    asyncio.run(run(args.force))


if __name__ == "__main__":
    main()
