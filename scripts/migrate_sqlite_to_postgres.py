"""
Copy Fliphone data from SQLite into PostgreSQL.

Usage:
    python scripts/migrate_sqlite_to_postgres.py --sqlite-path phonebooth.db

DATABASE_URL is read from the environment unless --database-url is provided.
The script prints table row counts only. It never prints webhook URLs or row data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

try:
    import asyncpg
except ImportError:  # pragma: no cover - displayed before migration starts.
    asyncpg = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import IDENTITY_TABLES, POSTGRES_SCHEMA, TABLE_ORDER  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Fliphone SQLite data into a managed PostgreSQL database."
    )
    parser.add_argument(
        "--sqlite-path",
        default=os.getenv("DB_PATH", "phonebooth.db"),
        help="Path to phonebooth.db. Keep the matching -wal and -shm files beside it.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="PostgreSQL connection URL. Defaults to DATABASE_URL.",
    )
    parser.add_argument(
        "--snapshot-path",
        default="",
        help="Clean SQLite snapshot output path. Defaults to <name>.snapshot.db.",
    )
    parser.add_argument(
        "--overwrite-snapshot",
        action="store_true",
        help="Replace an existing snapshot file.",
    )
    parser.add_argument(
        "--clear-postgres",
        action="store_true",
        help="TRUNCATE existing Postgres tables before copying data.",
    )
    return parser.parse_args()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def placeholder_list(count: int) -> str:
    return ", ".join(f"${index}" for index in range(1, count + 1))


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def create_sqlite_snapshot(source_path: Path, snapshot_path: Path, overwrite: bool) -> Path:
    if not source_path.exists():
        raise FileNotFoundError(f"SQLite file not found: {source_path}")

    wal_path = source_path.with_name(source_path.name + "-wal")
    shm_path = source_path.with_name(source_path.name + "-shm")
    if wal_path.exists():
        print(f"Detected WAL file: {wal_path.name}")
    else:
        print("Warning: no -wal file found beside the SQLite DB.")
    if shm_path.exists():
        print(f"Detected SHM file: {shm_path.name}")

    if snapshot_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"Snapshot already exists: {snapshot_path}. "
                "Pass --overwrite-snapshot to replace it."
            )
        snapshot_path.unlink()

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source_path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    try:
        target = sqlite3.connect(snapshot_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    print(f"Created clean SQLite snapshot: {snapshot_path}")
    return snapshot_path


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return [row[1] for row in rows]


def sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0])


async def postgres_counts(pg, tables: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(await pg.fetchval(f"SELECT COUNT(*) FROM {quote_ident(table)}"))
    return counts


async def ensure_postgres_is_ready(pg, clear_postgres: bool) -> None:
    await pg.execute(POSTGRES_SCHEMA)
    counts = await postgres_counts(pg, TABLE_ORDER)
    non_empty = {table: count for table, count in counts.items() if count}
    if not non_empty:
        return

    if not clear_postgres:
        details = ", ".join(f"{table}={count}" for table, count in non_empty.items())
        raise RuntimeError(
            "Postgres already contains Fliphone data. "
            f"Counts: {details}. Use --clear-postgres only if this target can be wiped."
        )

    table_list = ", ".join(quote_ident(table) for table in reversed(TABLE_ORDER))
    await pg.execute(f"TRUNCATE TABLE {table_list} RESTART IDENTITY")
    print("Cleared existing Postgres table data.")


async def copy_table(sqlite_conn: sqlite3.Connection, pg, table: str) -> tuple[int, int]:
    if not table_exists(sqlite_conn, table):
        return 0, 0

    columns = sqlite_columns(sqlite_conn, table)
    if not columns:
        return 0, 0

    source_count = sqlite_count(sqlite_conn, table)
    if source_count == 0:
        return 0, 0

    column_sql = ", ".join(quote_ident(column) for column in columns)
    insert_sql = (
        f"INSERT INTO {quote_ident(table)} ({column_sql}) "
        f"VALUES ({placeholder_list(len(columns))})"
    )
    sqlite_conn.row_factory = sqlite3.Row
    rows = sqlite_conn.execute(f"SELECT {column_sql} FROM {quote_ident(table)}").fetchall()
    values = [tuple(row[column] for column in columns) for row in rows]

    await pg.executemany(insert_sql, values)
    return source_count, len(values)


async def reset_identity_sequences(pg) -> None:
    for table, column in IDENTITY_TABLES.items():
        sequence = await pg.fetchval("SELECT pg_get_serial_sequence($1, $2)", table, column)
        if not sequence:
            continue
        max_id = await pg.fetchval(
            f"SELECT MAX({quote_ident(column)}) FROM {quote_ident(table)}"
        )
        if max_id is None:
            await pg.execute("SELECT setval($1::regclass, 1, false)", sequence)
        else:
            await pg.execute("SELECT setval($1::regclass, $2, true)", sequence, int(max_id))


async def migrate(args: argparse.Namespace) -> None:
    if asyncpg is None:
        raise RuntimeError("asyncpg is not installed. Run: pip install -r requirements.txt")
    if not args.database_url:
        raise RuntimeError("DATABASE_URL is required. Set it in .env or pass --database-url.")

    sqlite_path = Path(args.sqlite_path)
    if args.snapshot_path:
        snapshot_path = Path(args.snapshot_path)
    else:
        snapshot_path = sqlite_path.with_name(f"{sqlite_path.stem}.snapshot{sqlite_path.suffix}")
    snapshot = create_sqlite_snapshot(sqlite_path, snapshot_path, args.overwrite_snapshot)

    sqlite_conn = sqlite3.connect(snapshot)
    sqlite_conn.row_factory = sqlite3.Row
    pg = await asyncpg.connect(args.database_url, statement_cache_size=0)
    try:
        await ensure_postgres_is_ready(pg, args.clear_postgres)

        print("Copying tables:")
        for table in TABLE_ORDER:
            source_count, inserted_count = await copy_table(sqlite_conn, pg, table)
            print(f"  {table}: {inserted_count}/{source_count} rows")

        await reset_identity_sequences(pg)

        print("Verifying row counts:")
        destination_counts = await postgres_counts(pg, TABLE_ORDER)
        mismatches: list[str] = []
        for table in TABLE_ORDER:
            source_count = sqlite_count(sqlite_conn, table)
            destination_count = destination_counts[table]
            print(f"  {table}: sqlite={source_count}, postgres={destination_count}")
            if source_count != destination_count:
                mismatches.append(table)

        if mismatches:
            raise RuntimeError("Row-count mismatch after migration: " + ", ".join(mismatches))

        print("Migration completed successfully.")
    finally:
        await pg.close()
        sqlite_conn.close()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(migrate(args))
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
