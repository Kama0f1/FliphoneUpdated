"""
database.py - All persistence for Phonebooth V2.

The bot keeps a stable async Database API for cogs while supporting either:
- SQLite, selected when DATABASE_URL is empty.
- PostgreSQL, selected when DATABASE_URL is set.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from typing import Any, Optional, Sequence

import aiosqlite

try:
    import asyncpg
except ImportError:  # pragma: no cover - handled at runtime with a clear error.
    asyncpg = None

import config


TABLE_ORDER = [
    "guild_config",
    "queue",
    "connections",
    "custom_words",
    "banned_users",
    "call_history",
    "blocked_guilds",
    "gif_reports",
    "notify_subscribers",
    "gif_url_list",
    "gif_mode_settings",
    "rooms",
    "room_members",
    "call_reports",
]

IDENTITY_TABLES = {
    "connections": "id",
    "call_history": "id",
    "gif_reports": "id",
    "rooms": "id",
    "room_members": "id",
    "call_reports": "id",
}

SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id    INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    webhook_url TEXT,
    anonymous   INTEGER NOT NULL DEFAULT 0,
    setup_by    INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue (
    channel_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    webhook_url TEXT,
    joined_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_a   INTEGER NOT NULL,
    guild_a     INTEGER NOT NULL,
    webhook_a   TEXT,
    channel_b   INTEGER NOT NULL,
    guild_b     INTEGER NOT NULL,
    webhook_b   TEXT,
    started_at  TEXT NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conn_a ON connections (channel_a);
CREATE INDEX IF NOT EXISTS idx_conn_b ON connections (channel_b);

CREATE TABLE IF NOT EXISTS custom_words (
    word       TEXT PRIMARY KEY,
    added_by   INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS banned_users (
    user_id    INTEGER PRIMARY KEY,
    banned_by  INTEGER,
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_a     INTEGER NOT NULL,
    guild_b     INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    ended_by    INTEGER
);

CREATE TABLE IF NOT EXISTS blocked_guilds (
    guild_id         INTEGER NOT NULL,
    blocked_guild_id INTEGER NOT NULL,
    blocked_by       INTEGER,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (guild_id, blocked_guild_id)
);

CREATE TABLE IF NOT EXISTS gif_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    norm_url    TEXT NOT NULL,
    msg_id      INTEGER,
    channel_id  INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    reporter_id INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    reported_at TEXT
);

CREATE TABLE IF NOT EXISTS notify_subscribers (
    user_id    INTEGER PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gif_url_list (
    norm_url    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,
    added_by    INTEGER,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gif_mode_settings (
    guild_id    INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL DEFAULT 'enabled',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL DEFAULT 'waiting',
    max_size    INTEGER NOT NULL DEFAULT 6,
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    msg_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS room_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    webhook_url TEXT,
    station     TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(channel_id)
);

CREATE INDEX IF NOT EXISTS idx_room_member_channel ON room_members (channel_id);
CREATE INDEX IF NOT EXISTS idx_room_member_room ON room_members (room_id);

CREATE TABLE IF NOT EXISTS call_reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_guild_id   INTEGER NOT NULL,
    reporter_user_id    INTEGER NOT NULL,
    reported_guild_id   INTEGER NOT NULL,
    reason              TEXT NOT NULL,
    call_started_at     TEXT,
    call_ended_at       TEXT,
    status              TEXT NOT NULL DEFAULT 'open',
    created_at          TEXT NOT NULL
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id    BIGINT PRIMARY KEY,
    channel_id  BIGINT NOT NULL,
    webhook_url TEXT,
    anonymous   INTEGER NOT NULL DEFAULT 0,
    setup_by    BIGINT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue (
    channel_id  BIGINT PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    webhook_url TEXT,
    joined_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    channel_a   BIGINT NOT NULL,
    guild_a     BIGINT NOT NULL,
    webhook_a   TEXT,
    channel_b   BIGINT NOT NULL,
    guild_b     BIGINT NOT NULL,
    webhook_b   TEXT,
    started_at  TEXT NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conn_a ON connections (channel_a);
CREATE INDEX IF NOT EXISTS idx_conn_b ON connections (channel_b);

CREATE TABLE IF NOT EXISTS custom_words (
    word       TEXT PRIMARY KEY,
    added_by   BIGINT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS banned_users (
    user_id    BIGINT PRIMARY KEY,
    banned_by  BIGINT,
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_history (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    guild_a     BIGINT NOT NULL,
    guild_b     BIGINT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    ended_by    BIGINT
);

CREATE TABLE IF NOT EXISTS blocked_guilds (
    guild_id         BIGINT NOT NULL,
    blocked_guild_id BIGINT NOT NULL,
    blocked_by       BIGINT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (guild_id, blocked_guild_id)
);

CREATE TABLE IF NOT EXISTS gif_reports (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    url         TEXT NOT NULL,
    norm_url    TEXT NOT NULL,
    msg_id      BIGINT,
    channel_id  BIGINT NOT NULL,
    guild_id    BIGINT NOT NULL,
    reporter_id BIGINT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    reported_at TEXT
);

CREATE TABLE IF NOT EXISTS notify_subscribers (
    user_id    BIGINT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gif_url_list (
    norm_url    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,
    added_by    BIGINT,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gif_mode_settings (
    guild_id    BIGINT PRIMARY KEY,
    mode        TEXT NOT NULL DEFAULT 'enabled',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'waiting',
    max_size    INTEGER NOT NULL DEFAULT 6,
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    msg_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS room_members (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    room_id     BIGINT NOT NULL,
    channel_id  BIGINT NOT NULL,
    guild_id    BIGINT NOT NULL,
    webhook_url TEXT,
    station     TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(channel_id)
);

CREATE INDEX IF NOT EXISTS idx_room_member_channel ON room_members (channel_id);
CREATE INDEX IF NOT EXISTS idx_room_member_room ON room_members (room_id);

CREATE TABLE IF NOT EXISTS call_reports (
    id                  BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    reporter_guild_id   BIGINT NOT NULL,
    reporter_user_id    BIGINT NOT NULL,
    reported_guild_id   BIGINT NOT NULL,
    reason              TEXT NOT NULL,
    call_started_at     TEXT,
    call_ended_at       TEXT,
    status              TEXT NOT NULL DEFAULT 'open',
    created_at          TEXT NOT NULL
);
"""


class Database:
    def __init__(
        self,
        path: str = config.DB_PATH,
        database_url: str = config.DATABASE_URL,
    ):
        self.path = path
        self.database_url = database_url.strip()
        self.backend = "postgres" if self.database_url else "sqlite"
        self._conn = None
        self._conn_lock = asyncio.Lock()

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name in {"init", "close", "__class__"}:
            return attr
        if inspect.iscoroutinefunction(attr):
            async def guarded(*args, **kwargs):
                await object.__getattribute__(self, "_ensure_connection")()
                return await attr(*args, **kwargs)

            return guarded
        return attr

    async def _ensure_connection(self) -> None:
        conn = object.__getattribute__(self, "_conn")
        if conn is not None:
            return
        async with object.__getattribute__(self, "_conn_lock"):
            conn = object.__getattribute__(self, "_conn")
            if conn is None:
                await object.__getattribute__(self, "init")()

    async def init(self) -> None:
        await self.close()

        if self.backend == "postgres":
            if asyncpg is None:
                raise RuntimeError(
                    "DATABASE_URL is set, but asyncpg is not installed. "
                    "Run: pip install -r requirements.txt"
                )
            self._conn = await asyncpg.create_pool(
                dsn=self.database_url,
                min_size=1,
                max_size=config.DATABASE_POOL_SIZE,
                statement_cache_size=config.PG_STATEMENT_CACHE_SIZE,
            )
            await self._execute_script(POSTGRES_SCHEMA)
            return

        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA synchronous = NORMAL;")
        await self._conn.execute("PRAGMA cache_size = -8000;")
        await self._conn.execute("PRAGMA temp_store = MEMORY;")
        await self._conn.execute("PRAGMA mmap_size = 134217728;")
        await self._conn.executescript(SQLITE_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        if self.backend == "postgres":
            await conn.close()
        else:
            await conn.close()

    @staticmethod
    def _row(row) -> Optional[dict]:
        return dict(row) if row else None

    @staticmethod
    def _normalize_url(url: str) -> str:
        return url.split("?")[0].rstrip("/").lower()

    @staticmethod
    def _pg_sql(sql: str) -> str:
        chunks = sql.split("?")
        if len(chunks) == 1:
            return sql
        converted = [chunks[0]]
        for index, chunk in enumerate(chunks[1:], start=1):
            converted.append(f"${index}")
            converted.append(chunk)
        return "".join(converted)

    @staticmethod
    def _pg_rowcount(status: str) -> int:
        parts = status.split()
        return int(parts[-1]) if parts and parts[-1].isdigit() else 0

    async def _execute_script(self, sql: str) -> None:
        if self.backend == "postgres":
            async with self._conn.acquire() as conn:
                await conn.execute(sql)
            return
        await self._conn.executescript(sql)
        await self._conn.commit()

    async def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.backend == "postgres":
            async with self._conn.acquire() as conn:
                status = await conn.execute(self._pg_sql(sql), *params)
                return self._pg_rowcount(status)

        cur = await self._conn.execute(sql, tuple(params))
        await self._conn.commit()
        return cur.rowcount

    async def _fetchrow(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict]:
        if self.backend == "postgres":
            async with self._conn.acquire() as conn:
                row = await conn.fetchrow(self._pg_sql(sql), *params)
                return self._row(row)

        async with self._conn.execute(sql, tuple(params)) as cur:
            return self._row(await cur.fetchone())

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        if self.backend == "postgres":
            async with self._conn.acquire() as conn:
                rows = await conn.fetch(self._pg_sql(sql), *params)
                return [dict(row) for row in rows]

        async with self._conn.execute(sql, tuple(params)) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def _fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.backend == "postgres":
            async with self._conn.acquire() as conn:
                return await conn.fetchval(self._pg_sql(sql), *params)

        async with self._conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def _insert_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.backend == "postgres":
            return int(await self._fetchval(f"{sql} RETURNING id", params))

        cur = await self._conn.execute(sql, tuple(params))
        await self._conn.commit()
        return int(cur.lastrowid)

    async def ping(self) -> bool:
        return int(await self._fetchval("SELECT 1") or 0) == 1

    async def count_table(self, table: str) -> int:
        if table not in TABLE_ORDER:
            raise ValueError(f"Unknown table: {table}")
        return int(await self._fetchval(f'SELECT COUNT(*) FROM "{table}"') or 0)

    async def table_counts(self) -> dict[str, int]:
        selects = [
            f"SELECT '{table}' AS table_name, COUNT(*) AS row_count FROM \"{table}\""
            for table in TABLE_ORDER
        ]
        rows = await self._fetchall(" UNION ALL ".join(selects))
        counts = {table: 0 for table in TABLE_ORDER}
        for row in rows:
            counts[str(row["table_name"])] = int(row["row_count"])
        return counts

    async def setup_guild(self, guild_id, channel_id, webhook_url, user_id) -> None:
        await self._execute(
            """
            INSERT INTO guild_config
                (guild_id, channel_id, webhook_url, setup_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                webhook_url = excluded.webhook_url,
                setup_by = excluded.setup_by,
                created_at = excluded.created_at
            """,
            (guild_id, channel_id, webhook_url, user_id, datetime.utcnow().isoformat()),
        )

    async def delete_guild(self, guild_id: int) -> None:
        await self._execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))

    async def get_guild_config(self, guild_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))

    async def get_config_by_channel(self, channel_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM guild_config WHERE channel_id = ?", (channel_id,))

    async def toggle_anonymous(self, guild_id: int) -> bool:
        row = await self._fetchrow(
            "SELECT anonymous FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        )
        if not row:
            return True
        new_val = 0 if row["anonymous"] else 1
        await self._execute(
            "UPDATE guild_config SET anonymous = ? WHERE guild_id = ?",
            (new_val, guild_id),
        )
        return bool(new_val)

    async def update_webhook(self, channel_id: int, webhook_url: Optional[str]) -> None:
        await self._execute(
            "UPDATE guild_config SET webhook_url = ? WHERE channel_id = ?",
            (webhook_url, channel_id),
        )

    async def add_to_queue(self, channel_id, guild_id, user_id, webhook_url) -> None:
        await self._execute(
            """
            INSERT INTO queue (channel_id, guild_id, user_id, webhook_url, joined_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                user_id = excluded.user_id,
                webhook_url = excluded.webhook_url,
                joined_at = excluded.joined_at
            """,
            (channel_id, guild_id, user_id, webhook_url, datetime.utcnow().isoformat()),
        )

    async def remove_from_queue(self, channel_id: int) -> None:
        await self._execute("DELETE FROM queue WHERE channel_id = ?", (channel_id,))

    async def get_queue_entry(self, channel_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM queue WHERE channel_id = ?", (channel_id,))

    async def get_queue_match(self, guild_id: int, channel_id: int) -> Optional[dict]:
        return await self._fetchrow(
            """
            SELECT q.*
            FROM queue q
            WHERE q.guild_id != ?
              AND q.channel_id != ?
              AND NOT EXISTS (
                  SELECT 1 FROM blocked_guilds b
                  WHERE (b.guild_id = ? AND b.blocked_guild_id = q.guild_id)
                     OR (b.guild_id = q.guild_id AND b.blocked_guild_id = ?)
              )
            ORDER BY q.joined_at ASC
            LIMIT 1
            """,
            (guild_id, channel_id, guild_id, guild_id),
        )

    async def get_queue_size(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM queue") or 0)

    async def create_connection(self, channel_a, guild_a, webhook_a, channel_b, guild_b, webhook_b) -> int:
        return await self._insert_returning_id(
            """
            INSERT INTO connections
                (channel_a, guild_a, webhook_a, channel_b, guild_b, webhook_b, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_a, guild_a, webhook_a, channel_b, guild_b, webhook_b, datetime.utcnow().isoformat()),
        )

    async def get_connection(self, channel_id: int) -> Optional[dict]:
        return await self._fetchrow(
            "SELECT * FROM connections WHERE channel_a = ? OR channel_b = ?",
            (channel_id, channel_id),
        )

    async def increment_message_count(self, connection_id: int) -> None:
        await self._execute(
            "UPDATE connections SET msg_count = msg_count + 1 WHERE id = ?",
            (connection_id,),
        )

    async def remove_connection(self, connection_id: int, ended_by: Optional[int] = None) -> Optional[dict]:
        if self.backend == "postgres":
            async with self._conn.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT * FROM connections WHERE id = $1",
                        connection_id,
                    )
                    if not row:
                        return None
                    conn_row = dict(row)
                    await conn.execute(
                        """
                        INSERT INTO call_history
                            (guild_a, guild_b, started_at, ended_at, msg_count, ended_by)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        conn_row["guild_a"],
                        conn_row["guild_b"],
                        conn_row["started_at"],
                        datetime.utcnow().isoformat(),
                        conn_row["msg_count"],
                        ended_by,
                    )
                    await conn.execute("DELETE FROM connections WHERE id = $1", connection_id)
                    return conn_row

        async with self._conn.execute(
            "SELECT * FROM connections WHERE id = ?",
            (connection_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            conn_row = dict(row)
        await self._conn.execute(
            """
            INSERT INTO call_history
                (guild_a, guild_b, started_at, ended_at, msg_count, ended_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                conn_row["guild_a"],
                conn_row["guild_b"],
                conn_row["started_at"],
                datetime.utcnow().isoformat(),
                conn_row["msg_count"],
                ended_by,
            ),
        )
        await self._conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
        await self._conn.commit()
        return conn_row

    async def get_active_connection_count(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM connections") or 0)

    async def get_total_calls(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM call_history") or 0)

    async def get_total_guilds(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM guild_config") or 0)

    async def block_guild(self, guild_id: int, blocked_id: int, user_id: int) -> None:
        await self._execute(
            """
            INSERT INTO blocked_guilds (guild_id, blocked_guild_id, blocked_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, blocked_guild_id) DO NOTHING
            """,
            (guild_id, blocked_id, user_id, datetime.utcnow().isoformat()),
        )

    async def unblock_guild(self, guild_id: int, blocked_id: int) -> int:
        return await self._execute(
            "DELETE FROM blocked_guilds WHERE guild_id = ? AND blocked_guild_id = ?",
            (guild_id, blocked_id),
        )

    async def get_blocked_guilds(self, guild_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM blocked_guilds WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )

    async def ban_user(self, user_id: int, banned_by: int, reason: str) -> None:
        await self._execute(
            """
            INSERT INTO banned_users (user_id, banned_by, reason, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                banned_by = excluded.banned_by,
                reason = excluded.reason,
                created_at = excluded.created_at
            """,
            (user_id, banned_by, reason, datetime.utcnow().isoformat()),
        )

    async def unban_user(self, user_id: int) -> int:
        return await self._execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))

    async def is_user_banned(self, user_id: int) -> bool:
        return await self._fetchval("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)) is not None

    async def add_gif_report(self, url: str, msg_id: Optional[int], channel_id: int, guild_id: int) -> int:
        norm = self._normalize_url(url)
        return await self._insert_returning_id(
            """
            INSERT INTO gif_reports
                (url, norm_url, msg_id, channel_id, guild_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (url, norm, msg_id, channel_id, guild_id, datetime.utcnow().isoformat()),
        )

    async def get_gif_report(self, report_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM gif_reports WHERE id = ?", (report_id,))

    async def mark_gif_reported(self, report_id: int, reporter_id: int) -> None:
        await self._execute(
            "UPDATE gif_reports SET status = 'reported', reporter_id = ?, reported_at = ? WHERE id = ?",
            (reporter_id, datetime.utcnow().isoformat(), report_id),
        )

    async def resolve_gif_report(self, report_id: int, resolution: str) -> None:
        await self._execute("UPDATE gif_reports SET status = ? WHERE id = ?", (resolution, report_id))

    async def get_pending_gif_reports(self) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM gif_reports WHERE status = 'reported' ORDER BY reported_at ASC"
        )

    async def check_gif_url(self, url: str) -> Optional[str]:
        norm = self._normalize_url(url)
        return await self._fetchval("SELECT status FROM gif_url_list WHERE norm_url = ?", (norm,))

    async def set_gif_url_status(self, url: str, status: str, added_by: int) -> None:
        norm = self._normalize_url(url)
        await self._execute(
            """
            INSERT INTO gif_url_list (norm_url, url, status, added_by, added_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(norm_url) DO UPDATE SET
                url = excluded.url,
                status = excluded.status,
                added_by = excluded.added_by,
                added_at = excluded.added_at
            """,
            (norm, url, status, added_by, datetime.utcnow().isoformat()),
        )

    async def remove_gif_url(self, url: str) -> int:
        norm = self._normalize_url(url)
        return await self._execute("DELETE FROM gif_url_list WHERE norm_url = ?", (norm,))

    async def get_gif_mode(self, guild_id: int) -> str:
        mode = await self._fetchval("SELECT mode FROM gif_mode_settings WHERE guild_id = ?", (guild_id,))
        mode = (mode or "enabled").lower()
        return mode if mode in {"enabled", "limited", "disabled"} else "enabled"

    async def get_gif_modes_bulk(self, guild_id_a: int, guild_id_b: int) -> tuple[str, str]:
        rows = await self._fetchall(
            "SELECT guild_id, mode FROM gif_mode_settings WHERE guild_id IN (?, ?)",
            (guild_id_a, guild_id_b),
        )
        modes = {row["guild_id"]: row["mode"] for row in rows}

        def resolve(gid):
            mode = (modes.get(gid, "enabled") or "enabled").lower()
            return mode if mode in {"enabled", "limited", "disabled"} else "enabled"

        return resolve(guild_id_a), resolve(guild_id_b)

    async def set_gif_mode(self, guild_id: int, mode: str) -> None:
        new_mode = (mode or "enabled").lower().strip()
        if new_mode not in {"enabled", "limited", "disabled"}:
            raise ValueError("Invalid GIF mode")
        await self._execute(
            """
            INSERT INTO gif_mode_settings (guild_id, mode, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                mode = excluded.mode,
                updated_at = excluded.updated_at
            """,
            (guild_id, new_mode, datetime.utcnow().isoformat()),
        )

    async def add_custom_word(self, word: str, added_by: int) -> bool:
        rowcount = await self._execute(
            """
            INSERT INTO custom_words (word, added_by, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(word) DO NOTHING
            """,
            (word.lower().strip(), added_by, datetime.utcnow().isoformat()),
        )
        return rowcount > 0

    async def remove_custom_word(self, word: str) -> bool:
        rowcount = await self._execute(
            "DELETE FROM custom_words WHERE word = ?",
            (word.lower().strip(),),
        )
        return rowcount > 0

    async def get_custom_words(self) -> list[str]:
        rows = await self._fetchall("SELECT word FROM custom_words ORDER BY word")
        return [row["word"] for row in rows]

    async def create_room(self, max_size: int = 6) -> int:
        return await self._insert_returning_id(
            "INSERT INTO rooms (status, max_size, created_at) VALUES ('waiting', ?, ?)",
            (max_size, datetime.utcnow().isoformat()),
        )

    async def get_room_by_id(self, room_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM rooms WHERE id = ?", (room_id,))

    async def get_available_room(self, guild_id: int) -> Optional[dict]:
        return await self._fetchrow(
            """
            SELECT r.*
            FROM rooms r
            WHERE r.status IN ('waiting', 'active')
              AND (SELECT COUNT(*) FROM room_members rm WHERE rm.room_id = r.id) < r.max_size
              AND NOT EXISTS (
                  SELECT 1 FROM room_members rm
                  WHERE rm.room_id = r.id AND rm.guild_id = ?
              )
            ORDER BY
                CASE r.status WHEN 'active' THEN 0 ELSE 1 END ASC,
                (SELECT COUNT(*) FROM room_members rm WHERE rm.room_id = r.id) DESC
            LIMIT 1
            """,
            (guild_id,),
        )

    async def add_room_member(
        self,
        room_id: int,
        channel_id: int,
        guild_id: int,
        webhook_url: Optional[str],
        station: str,
    ) -> None:
        await self._execute(
            """
            INSERT INTO room_members
                (room_id, channel_id, guild_id, webhook_url, station, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO NOTHING
            """,
            (room_id, channel_id, guild_id, webhook_url, station, datetime.utcnow().isoformat()),
        )

    async def get_room_member(self, channel_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM room_members WHERE channel_id = ?", (channel_id,))

    async def get_room_members(self, room_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM room_members WHERE room_id = ? ORDER BY joined_at ASC",
            (room_id,),
        )

    async def get_room_member_count(self, room_id: int) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM room_members WHERE room_id = ?", (room_id,)) or 0)

    async def remove_room_member(self, channel_id: int) -> Optional[dict]:
        row = await self.get_room_member(channel_id)
        if not row:
            return None
        await self._execute("DELETE FROM room_members WHERE channel_id = ?", (channel_id,))
        return row

    async def activate_room(self, room_id: int) -> None:
        await self._execute("UPDATE rooms SET status = 'active' WHERE id = ?", (room_id,))

    async def close_room(self, room_id: int) -> None:
        await self._execute(
            "UPDATE rooms SET status = 'closed', closed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), room_id),
        )

    async def increment_room_msg_count(self, room_id: int) -> None:
        await self._execute("UPDATE rooms SET msg_count = msg_count + 1 WHERE id = ?", (room_id,))

    async def increment_room_member_msg_count(self, channel_id: int) -> None:
        await self._execute(
            "UPDATE room_members SET msg_count = msg_count + 1 WHERE channel_id = ?",
            (channel_id,),
        )

    async def get_used_stations(self, room_id: int) -> list[str]:
        rows = await self._fetchall("SELECT station FROM room_members WHERE room_id = ?", (room_id,))
        return [row["station"] for row in rows]

    async def toggle_notify(self, user_id: int) -> bool:
        row = await self._fetchrow(
            "SELECT enabled FROM notify_subscribers WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            await self._execute(
                "INSERT INTO notify_subscribers (user_id, enabled, created_at) VALUES (?, 1, ?)",
                (user_id, datetime.utcnow().isoformat()),
            )
            return True

        new_val = 0 if row["enabled"] else 1
        await self._execute(
            "UPDATE notify_subscribers SET enabled = ? WHERE user_id = ?",
            (new_val, user_id),
        )
        return bool(new_val)

    async def get_notify_subscribers(self) -> list[int]:
        rows = await self._fetchall("SELECT user_id FROM notify_subscribers WHERE enabled = 1")
        return [row["user_id"] for row in rows]

    async def get_notify_status(self, user_id: int) -> bool:
        row = await self._fetchrow(
            "SELECT enabled FROM notify_subscribers WHERE user_id = ?",
            (user_id,),
        )
        return bool(row["enabled"]) if row else False

    async def add_call_report(
        self,
        reporter_guild_id,
        reporter_user_id,
        reported_guild_id,
        reason,
        call_started_at,
        call_ended_at,
    ):
        return await self._insert_returning_id(
            """
            INSERT INTO call_reports
                (reporter_guild_id, reporter_user_id, reported_guild_id, reason,
                 call_started_at, call_ended_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reporter_guild_id,
                reporter_user_id,
                reported_guild_id,
                reason,
                call_started_at,
                call_ended_at,
                datetime.utcnow().isoformat(),
            ),
        )

    async def get_open_call_reports(self):
        return await self._fetchall(
            "SELECT * FROM call_reports WHERE status = 'open' ORDER BY created_at ASC"
        )

    async def resolve_call_report(self, report_id):
        rowcount = await self._execute(
            "UPDATE call_reports SET status = 'resolved' WHERE id = ? AND status = 'open'",
            (report_id,),
        )
        return rowcount > 0

    async def get_recent_call_for_guild(self, guild_id):
        return await self._fetchrow(
            """
            SELECT * FROM call_history
            WHERE guild_a = ? OR guild_b = ?
            ORDER BY ended_at DESC
            LIMIT 1
            """,
            (guild_id, guild_id),
        )
