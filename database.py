"""
database.py - All persistence for Phonebooth V2.

The bot keeps a stable async Database API for cogs while supporting either:
- SQLite, selected when DATABASE_URL is empty.
- PostgreSQL, selected when DATABASE_URL is set.
"""

from __future__ import annotations

import asyncio
import inspect
import time
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
    "profile_banners",
    "user_chat_stats",
    "server_chat_stats",
    "scheduled_jobs",
    "gif_url_list",
    "gif_mode_settings",
    "user_preferences",
    "gif_submissions",
    "gif_submission_reviews",
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
    "gif_submissions": "id",
    "gif_submission_reviews": "id",
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
    last_activity_at TEXT,
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
    ,prompt_msg_id INTEGER
    ,prompt_channel_id INTEGER
    ,session_type TEXT
    ,session_id INTEGER
    ,expires_at TEXT
);

CREATE TABLE IF NOT EXISTS notify_subscribers (
    user_id    INTEGER PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_banners (
    user_id      INTEGER PRIMARY KEY,
    banner_index INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_chat_stats (
    user_id       INTEGER PRIMARY KEY,
    xp            INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_xp_at    TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_chat_stats_rank
    ON user_chat_stats (xp DESC, message_count DESC);

CREATE TABLE IF NOT EXISTS server_chat_stats (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    xp            INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_xp_at    TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_server_chat_stats_rank
    ON server_chat_stats (guild_id, xp DESC, message_count DESC);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_key     TEXT PRIMARY KEY,
    next_run_at REAL NOT NULL
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

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id    INTEGER PRIMARY KEY,
    anonymous  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gif_submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    norm_url     TEXT NOT NULL,
    submitter_id INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    review_msg_id INTEGER,
    created_at   TEXT NOT NULL,
    reviewed_at  TEXT,
    UNIQUE(norm_url, status)
);
CREATE INDEX IF NOT EXISTS idx_gif_submissions_user_time
    ON gif_submissions (submitter_id, created_at);

CREATE TABLE IF NOT EXISTS gif_submission_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    reviewer_id   INTEGER NOT NULL,
    action        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL DEFAULT 'waiting',
    max_size    INTEGER NOT NULL DEFAULT 6,
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    msg_count   INTEGER NOT NULL DEFAULT 0
    ,last_activity_at TEXT
);

CREATE TABLE IF NOT EXISTS room_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    webhook_url TEXT,
    station     TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    last_activity_at TEXT,
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
    last_activity_at TEXT,
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
    ,prompt_msg_id BIGINT
    ,prompt_channel_id BIGINT
    ,session_type TEXT
    ,session_id BIGINT
    ,expires_at TEXT
);

CREATE TABLE IF NOT EXISTS notify_subscribers (
    user_id    BIGINT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_banners (
    user_id      BIGINT PRIMARY KEY,
    banner_index INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_chat_stats (
    user_id       BIGINT PRIMARY KEY,
    xp            INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_xp_at    TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_chat_stats_rank
    ON user_chat_stats (xp DESC, message_count DESC);

CREATE TABLE IF NOT EXISTS server_chat_stats (
    guild_id      BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    xp            INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_xp_at    TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_server_chat_stats_rank
    ON server_chat_stats (guild_id, xp DESC, message_count DESC);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_key     TEXT PRIMARY KEY,
    next_run_at DOUBLE PRECISION NOT NULL
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

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id    BIGINT PRIMARY KEY,
    anonymous  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gif_submissions (
    id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    url          TEXT NOT NULL,
    norm_url     TEXT NOT NULL,
    submitter_id BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    channel_id   BIGINT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    review_msg_id BIGINT,
    created_at   TEXT NOT NULL,
    reviewed_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gif_submissions_pending_url
    ON gif_submissions (norm_url) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_gif_submissions_user_time
    ON gif_submissions (submitter_id, created_at);

CREATE TABLE IF NOT EXISTS gif_submission_reviews (
    id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    submission_id BIGINT NOT NULL,
    reviewer_id   BIGINT NOT NULL,
    action        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'waiting',
    max_size    INTEGER NOT NULL DEFAULT 6,
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    msg_count   INTEGER NOT NULL DEFAULT 0
    ,last_activity_at TEXT
);

CREATE TABLE IF NOT EXISTS room_members (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    room_id     BIGINT NOT NULL,
    channel_id  BIGINT NOT NULL,
    guild_id    BIGINT NOT NULL,
    webhook_url TEXT,
    station     TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    last_activity_at TEXT,
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
        self._connection_claim_lock = asyncio.Lock()
        self._room_claim_lock = asyncio.Lock()
        self._gif_url_cache: dict[str, tuple[float, Optional[str]]] = {}

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
            await self._ensure_runtime_columns()
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
        await self._ensure_runtime_columns()

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

    async def _has_column(self, table: str, column: str) -> bool:
        if self.backend == "postgres":
            return bool(
                await self._fetchval(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = ? AND column_name = ?
                    """,
                    (table, column),
                )
            )

        async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
        return any(row[1] == column for row in rows)

    async def _ensure_runtime_columns(self) -> None:
        runtime_columns = (
            ("connections", "last_activity_at", "started_at"),
            ("room_members", "last_activity_at", "joined_at"),
            ("rooms", "last_activity_at", "created_at"),
        )
        for table, column, fallback_column in runtime_columns:
            if not await self._has_column(table, column):
                await self._execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            await self._execute(
                f"UPDATE {table} SET {column} = {fallback_column} WHERE {column} IS NULL"
            )
        optional_columns = (
            ("gif_reports", "prompt_msg_id", "BIGINT" if self.backend == "postgres" else "INTEGER"),
            ("gif_reports", "prompt_channel_id", "BIGINT" if self.backend == "postgres" else "INTEGER"),
            ("gif_reports", "session_type", "TEXT"),
            ("gif_reports", "session_id", "BIGINT" if self.backend == "postgres" else "INTEGER"),
            ("gif_reports", "expires_at", "TEXT"),
        )
        for table, column, sql_type in optional_columns:
            if not await self._has_column(table, column):
                await self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

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

    async def toggle_user_anonymous(self, user_id: int) -> bool:
        current = await self._fetchval(
            "SELECT anonymous FROM user_preferences WHERE user_id = ?", (user_id,)
        )
        new_value = 0 if current else 1
        await self._execute(
            """
            INSERT INTO user_preferences (user_id, anonymous, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                anonymous = excluded.anonymous,
                updated_at = excluded.updated_at
            """,
            (user_id, new_value, datetime.utcnow().isoformat()),
        )
        return bool(new_value)

    async def is_user_anonymous(self, user_id: int) -> bool:
        return bool(
            await self._fetchval(
                "SELECT anonymous FROM user_preferences WHERE user_id = ?", (user_id,)
            )
            or 0
        )

    async def update_webhook(self, channel_id: int, webhook_url: Optional[str]) -> None:
        await self._execute(
            "UPDATE guild_config SET webhook_url = ? WHERE channel_id = ?",
            (webhook_url, channel_id),
        )

    async def update_connection_webhook(self, channel_id: int, webhook_url: Optional[str]) -> None:
        await self._execute(
            """
            UPDATE connections
            SET
                webhook_a = CASE WHEN channel_a = ? THEN ? ELSE webhook_a END,
                webhook_b = CASE WHEN channel_b = ? THEN ? ELSE webhook_b END
            WHERE channel_a = ? OR channel_b = ?
            """,
            (channel_id, webhook_url, channel_id, webhook_url, channel_id, channel_id),
        )

    async def update_room_member_webhook(self, channel_id: int, webhook_url: Optional[str]) -> None:
        await self._execute(
            "UPDATE room_members SET webhook_url = ? WHERE channel_id = ?",
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

    async def get_guild_queue_entries(self, guild_id: int) -> list[dict]:
        return await self._fetchall("SELECT * FROM queue WHERE guild_id = ?", (guild_id,))

    async def get_all_queue_entries(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM queue ORDER BY joined_at ASC")

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

    async def create_connection(
        self,
        channel_a,
        guild_a,
        webhook_a,
        channel_b,
        guild_b,
        webhook_b,
        started_at: Optional[str] = None,
    ) -> int:
        started_at = started_at or datetime.utcnow().isoformat()
        return await self._insert_returning_id(
            """
            INSERT INTO connections
                (channel_a, guild_a, webhook_a, channel_b, guild_b, webhook_b, started_at, last_activity_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_a, guild_a, webhook_a, channel_b, guild_b, webhook_b, started_at, started_at),
        )

    async def claim_queue_connection(
        self,
        *,
        channel_a: int,
        guild_a: int,
        webhook_a: str,
        channel_b: int,
        webhook_b: str,
        started_at: Optional[str] = None,
    ) -> Optional[dict]:
        """Atomically consume a queued partner and connect both channels once."""
        started_at = started_at or datetime.utcnow().isoformat()
        async with self._connection_claim_lock:
            if self.backend == "postgres":
                async with self._conn.acquire() as conn:
                    connection = await conn.fetchrow(
                        """
                        WITH channel_locks AS MATERIALIZED (
                            SELECT pg_advisory_xact_lock(channel_id)
                            FROM (
                                SELECT $1::BIGINT AS channel_id
                                UNION ALL
                                SELECT $4::BIGINT AS channel_id
                            ) ids
                            ORDER BY channel_id
                        ),
                        candidate AS MATERIALIZED (
                            SELECT q.*
                            FROM queue q
                            WHERE q.channel_id = $4
                              AND EXISTS (SELECT 1 FROM channel_locks)
                              AND NOT EXISTS (
                                  SELECT 1 FROM connections c
                                  WHERE c.channel_a IN ($1, $4)
                                     OR c.channel_b IN ($1, $4)
                              )
                            FOR UPDATE
                        ),
                        removed AS (
                            DELETE FROM queue q
                            USING candidate c
                            WHERE q.channel_id = $1 OR q.channel_id = c.channel_id
                            RETURNING q.channel_id
                        )
                        INSERT INTO connections
                            (channel_a, guild_a, webhook_a, channel_b, guild_b, webhook_b,
                             started_at, last_activity_at)
                        SELECT $1, $2, $3, $4, c.guild_id, $5, $6, $6
                        FROM candidate c
                        WHERE EXISTS (
                            SELECT 1 FROM removed r WHERE r.channel_id = c.channel_id
                        )
                        RETURNING *
                        """,
                        channel_a,
                        guild_a,
                        webhook_a,
                        channel_b,
                        webhook_b,
                        started_at,
                    )
                    return dict(connection) if connection else None

            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                async with self._conn.execute(
                    """
                    SELECT 1 FROM connections
                    WHERE channel_a IN (?, ?) OR channel_b IN (?, ?)
                    LIMIT 1
                    """,
                    (channel_a, channel_b, channel_a, channel_b),
                ) as cur:
                    if await cur.fetchone():
                        await self._conn.rollback()
                        return None
                async with self._conn.execute(
                    "SELECT * FROM queue WHERE channel_id = ?", (channel_b,)
                ) as cur:
                    match_row = await cur.fetchone()
                if not match_row:
                    await self._conn.rollback()
                    return None
                match = dict(match_row)
                await self._conn.execute(
                    "DELETE FROM queue WHERE channel_id IN (?, ?)",
                    (channel_a, channel_b),
                )
                cur = await self._conn.execute(
                    """
                    INSERT INTO connections
                        (channel_a, guild_a, webhook_a, channel_b, guild_b, webhook_b,
                         started_at, last_activity_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_a,
                        guild_a,
                        webhook_a,
                        channel_b,
                        match["guild_id"],
                        webhook_b,
                        started_at,
                        started_at,
                    ),
                )
                connection_id = int(cur.lastrowid)
                async with self._conn.execute(
                    "SELECT * FROM connections WHERE id = ?", (connection_id,)
                ) as row_cur:
                    connection = dict(await row_cur.fetchone())
                await self._conn.commit()
                return connection
            except Exception:
                await self._conn.rollback()
                raise

    async def get_connection(self, channel_id: int) -> Optional[dict]:
        return await self._fetchrow(
            "SELECT * FROM connections WHERE channel_a = ? OR channel_b = ?",
            (channel_id, channel_id),
        )

    async def get_guild_connections(self, guild_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM connections WHERE guild_a = ? OR guild_b = ?",
            (guild_id, guild_id),
        )

    async def get_active_connections(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM connections ORDER BY started_at ASC")

    async def increment_message_count(self, connection_id: int) -> None:
        await self._execute(
            "UPDATE connections SET msg_count = msg_count + 1, last_activity_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), connection_id),
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

    async def get_configured_guild_ids(self) -> list[int]:
        rows = await self._fetchall("SELECT guild_id FROM guild_config")
        return [int(row["guild_id"]) for row in rows]

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

    async def add_gif_report(
        self,
        url: str,
        msg_id: Optional[int],
        channel_id: int,
        guild_id: int,
        *,
        session_type: Optional[str] = None,
        session_id: Optional[int] = None,
    ) -> int:
        norm = self._normalize_url(url)
        return await self._insert_returning_id(
            """
            INSERT INTO gif_reports
                (url, norm_url, msg_id, channel_id, guild_id, status, created_at,
                 session_type, session_id)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                url, norm, msg_id, channel_id, guild_id,
                datetime.utcnow().isoformat(), session_type, session_id,
            ),
        )

    async def get_gif_report(self, report_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM gif_reports WHERE id = ?", (report_id,))

    async def get_gif_report_by_prompt(self, prompt_msg_id: int) -> Optional[dict]:
        return await self._fetchrow(
            "SELECT * FROM gif_reports WHERE prompt_msg_id = ?", (prompt_msg_id,)
        )

    async def get_open_gif_report_prompts(self) -> list[dict]:
        return await self._fetchall(
            """
            SELECT * FROM gif_reports
            WHERE prompt_msg_id IS NOT NULL AND status = 'pending'
            """
        )

    async def set_gif_report_expiry(self, report_id: int, expires_at: str) -> None:
        await self._execute(
            "UPDATE gif_reports SET expires_at = ? WHERE id = ?",
            (expires_at, report_id),
        )

    async def set_gif_report_prompt(
        self, report_id: int, prompt_msg_id: int, prompt_channel_id: int
    ) -> None:
        await self._execute(
            "UPDATE gif_reports SET prompt_msg_id = ?, prompt_channel_id = ? WHERE id = ?",
            (prompt_msg_id, prompt_channel_id, report_id),
        )

    async def expire_session_gif_reports(
        self, session_type: str, session_id: int, expires_at: str
    ) -> None:
        await self._execute(
            """
            UPDATE gif_reports SET expires_at = ?
            WHERE session_type = ? AND session_id = ? AND expires_at IS NULL
            """,
            (expires_at, session_type, session_id),
        )

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
        cached = self._gif_url_cache.get(norm)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        status = await self._fetchval(
            "SELECT status FROM gif_url_list WHERE norm_url = ?", (norm,)
        )
        self._gif_url_cache[norm] = (time.monotonic() + 300.0, status)
        return status

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
        self._gif_url_cache[norm] = (time.monotonic() + 300.0, status)

    async def add_gif_submission(
        self, url: str, submitter_id: int, guild_id: int, channel_id: int
    ) -> int:
        return await self._insert_returning_id(
            """
            INSERT INTO gif_submissions
                (url, norm_url, submitter_id, guild_id, channel_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                url,
                self._normalize_url(url),
                submitter_id,
                guild_id,
                channel_id,
                datetime.utcnow().isoformat(),
            ),
        )

    async def get_gif_submission(self, submission_id: int) -> Optional[dict]:
        return await self._fetchrow(
            "SELECT * FROM gif_submissions WHERE id = ?", (submission_id,)
        )

    async def get_pending_gif_submission_by_url(self, url: str) -> Optional[dict]:
        return await self._fetchrow(
            "SELECT * FROM gif_submissions WHERE norm_url = ? AND status = 'pending'",
            (self._normalize_url(url),),
        )

    async def count_recent_gif_submissions(self, user_id: int, since: str) -> int:
        return int(
            await self._fetchval(
                "SELECT COUNT(*) FROM gif_submissions WHERE submitter_id = ? AND created_at >= ?",
                (user_id, since),
            )
            or 0
        )

    async def set_gif_submission_review_message(
        self, submission_id: int, message_id: int
    ) -> None:
        await self._execute(
            "UPDATE gif_submissions SET review_msg_id = ? WHERE id = ?",
            (message_id, submission_id),
        )

    async def review_gif_submission(
        self, submission_id: int, reviewer_id: int, action: str
    ) -> Optional[dict]:
        submission = await self.get_gif_submission(submission_id)
        if not submission or submission["status"] != "pending":
            return None
        updated = await self._execute(
            "UPDATE gif_submissions SET status = ?, reviewed_at = ? WHERE id = ? AND status = 'pending'",
            (action, datetime.utcnow().isoformat(), submission_id),
        )
        if not updated:
            return None
        await self._insert_returning_id(
            """
            INSERT INTO gif_submission_reviews
                (submission_id, reviewer_id, action, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (submission_id, reviewer_id, action, datetime.utcnow().isoformat()),
        )
        if action == "approved":
            await self.set_gif_url_status(submission["url"], "whitelist", reviewer_id)
        elif action == "blacklisted":
            await self.set_gif_url_status(submission["url"], "blacklist", reviewer_id)
        return submission

    async def remove_gif_url(self, url: str) -> int:
        norm = self._normalize_url(url)
        removed = await self._execute("DELETE FROM gif_url_list WHERE norm_url = ?", (norm,))
        self._gif_url_cache.pop(norm, None)
        return removed

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

    async def create_room(self, max_size: int = 5) -> int:
        now = datetime.utcnow().isoformat()
        return await self._insert_returning_id(
            """
            INSERT INTO rooms (status, max_size, created_at, last_activity_at)
            VALUES ('waiting', ?, ?, ?)
            """,
            (max_size, now, now),
        )

    async def get_room_by_id(self, room_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM rooms WHERE id = ?", (room_id,))

    async def get_connection_by_id(self, connection_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM connections WHERE id = ?", (connection_id,))

    async def get_open_rooms(self) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM rooms WHERE status IN ('waiting', 'active') ORDER BY created_at ASC"
        )

    async def get_available_room(
        self, guild_id: int, excluded_room_id: Optional[int] = None
    ) -> Optional[dict]:
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
              AND (CAST(? AS BIGINT) IS NULL OR r.id <> ?)
              AND NOT EXISTS (
                  SELECT 1
                  FROM room_members candidate
                  JOIN blocked_guilds b
                    ON (b.guild_id = ? AND b.blocked_guild_id = candidate.guild_id)
                    OR (b.guild_id = candidate.guild_id AND b.blocked_guild_id = ?)
                  WHERE candidate.room_id = r.id
              )
            ORDER BY
                CASE r.status WHEN 'active' THEN 0 ELSE 1 END ASC,
                (SELECT COUNT(*) FROM room_members rm WHERE rm.room_id = r.id) DESC
            LIMIT 1
            """,
            (guild_id, excluded_room_id, excluded_room_id, guild_id, guild_id),
        )

    async def add_room_member(
        self,
        room_id: int,
        channel_id: int,
        guild_id: int,
        webhook_url: Optional[str],
        station: str,
    ) -> bool:
        joined_at = datetime.utcnow().isoformat()
        inserted = await self._execute(
            """
            INSERT INTO room_members
                (room_id, channel_id, guild_id, webhook_url, station, joined_at, last_activity_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO NOTHING
            """,
            (
                room_id,
                channel_id,
                guild_id,
                webhook_url,
                station,
                joined_at,
                joined_at,
            ),
        )
        return inserted > 0

    async def claim_room_slot(
        self,
        *,
        channel_id: int,
        guild_id: int,
        webhook_url: Optional[str],
        station_names: Sequence[str],
        excluded_room_id: Optional[int] = None,
    ) -> Optional[dict]:
        """Claim one room slot atomically within this bot process."""
        async with self._room_claim_lock:
            room = await self.get_available_room(guild_id, excluded_room_id)
            if not room:
                return None
            used = set(await self.get_used_stations(room["id"]))
            station = next((name for name in station_names if name not in used), None)
            if station is None:
                return None
            if await self.get_room_member_count(room["id"]) >= int(room["max_size"]):
                return None
            inserted = await self.add_room_member(
                room["id"], channel_id, guild_id, webhook_url, station
            )
            if not inserted:
                return None
            return {**room, "station": station}

    async def get_room_member(self, channel_id: int) -> Optional[dict]:
        return await self._fetchrow("SELECT * FROM room_members WHERE channel_id = ?", (channel_id,))

    async def get_guild_room_members(self, guild_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM room_members WHERE guild_id = ? ORDER BY joined_at ASC",
            (guild_id,),
        )

    async def get_room_members(self, room_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM room_members WHERE room_id = ? ORDER BY joined_at ASC",
            (room_id,),
        )

    async def get_room_relay_context(
        self, channel_id: int
    ) -> tuple[Optional[dict], Optional[dict], list[dict]]:
        rows = await self._fetchall(
            """
            SELECT
                member.*,
                source.channel_id AS source_channel_id,
                r.status AS room_status,
                r.max_size AS room_max_size,
                r.created_at AS room_created_at,
                r.closed_at AS room_closed_at,
                r.msg_count AS room_msg_count,
                r.last_activity_at AS room_last_activity_at
            FROM room_members source
            JOIN rooms r ON r.id = source.room_id
            JOIN room_members member ON member.room_id = source.room_id
            WHERE source.channel_id = ?
            ORDER BY member.joined_at ASC
            """,
            (channel_id,),
        )
        if not rows:
            return None, None, []
        source = next(
            (row for row in rows if int(row["channel_id"]) == int(channel_id)),
            None,
        )
        first = rows[0]
        room = {
            "id": first["room_id"],
            "status": first["room_status"],
            "max_size": first["room_max_size"],
            "created_at": first["room_created_at"],
            "closed_at": first["room_closed_at"],
            "msg_count": first["room_msg_count"],
            "last_activity_at": first["room_last_activity_at"],
        }
        context_keys = {
            "source_channel_id",
            "room_status",
            "room_max_size",
            "room_created_at",
            "room_closed_at",
            "room_msg_count",
            "room_last_activity_at",
        }
        members = [
            {key: value for key, value in row.items() if key not in context_keys}
            for row in rows
        ]
        if source:
            source = {
                key: value
                for key, value in source.items()
                if key not in context_keys
            }
        return source, room, members

    async def get_room_member_count(self, room_id: int) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM room_members WHERE room_id = ?", (room_id,)) or 0)

    async def remove_room_member(self, channel_id: int) -> Optional[dict]:
        row = await self.get_room_member(channel_id)
        if not row:
            return None
        await self._execute("DELETE FROM room_members WHERE channel_id = ?", (channel_id,))
        return row

    async def activate_room(self, room_id: int) -> None:
        await self._execute(
            "UPDATE rooms SET status = 'active', last_activity_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), room_id),
        )

    async def close_room(self, room_id: int) -> None:
        await self._execute(
            "UPDATE rooms SET status = 'closed', closed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), room_id),
        )

    async def increment_room_msg_count(self, room_id: int) -> None:
        await self._execute(
            "UPDATE rooms SET msg_count = msg_count + 1, last_activity_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), room_id),
        )

    async def touch_room(self, room_id: int) -> None:
        await self._execute(
            "UPDATE rooms SET last_activity_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), room_id),
        )

    async def increment_room_member_msg_count(self, channel_id: int) -> None:
        await self._execute(
            "UPDATE room_members SET msg_count = msg_count + 1, last_activity_at = ? WHERE channel_id = ?",
            (datetime.utcnow().isoformat(), channel_id),
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

    async def get_profile_banner(self, user_id: int) -> Optional[int]:
        row = await self._fetchrow(
            "SELECT banner_index FROM profile_banners WHERE user_id = ?",
            (user_id,),
        )
        return int(row["banner_index"]) if row else None

    async def set_profile_banner(self, user_id: int, banner_index: int) -> None:
        await self._execute(
            """
            INSERT INTO profile_banners (user_id, banner_index, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                banner_index = excluded.banner_index,
                updated_at = excluded.updated_at
            """,
            (user_id, banner_index, datetime.utcnow().isoformat()),
        )

    async def reset_profile_banner(self, user_id: int) -> None:
        await self._execute("DELETE FROM profile_banners WHERE user_id = ?", (user_id,))

    @staticmethod
    def _seconds_since(timestamp: Optional[str]) -> float:
        if not timestamp:
            return 10**9
        try:
            return (datetime.utcnow() - datetime.fromisoformat(timestamp)).total_seconds()
        except (TypeError, ValueError):
            return 10**9

    async def add_chat_xp(
        self,
        user_id: int,
        guild_id: int,
        amount: int,
        cooldown_seconds: int,
    ) -> Optional[dict]:
        row = await self._fetchrow(
            "SELECT last_xp_at FROM user_chat_stats WHERE user_id = ?",
            (user_id,),
        )
        if row and self._seconds_since(row["last_xp_at"]) < cooldown_seconds:
            return None

        now = datetime.utcnow().isoformat()
        await self._execute(
            """
            INSERT INTO user_chat_stats (user_id, xp, message_count, last_xp_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                xp = user_chat_stats.xp + excluded.xp,
                message_count = user_chat_stats.message_count + 1,
                last_xp_at = excluded.last_xp_at,
                updated_at = excluded.updated_at
            """,
            (user_id, amount, now, now),
        )
        await self._execute(
            """
            INSERT INTO server_chat_stats (guild_id, user_id, xp, message_count, last_xp_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp = server_chat_stats.xp + excluded.xp,
                message_count = server_chat_stats.message_count + 1,
                last_xp_at = excluded.last_xp_at,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, amount, now, now),
        )
        return await self.get_chat_profile(user_id, guild_id)

    async def get_chat_profile(self, user_id: int, guild_id: Optional[int] = None) -> dict:
        row = await self._fetchrow(
            "SELECT xp, message_count FROM user_chat_stats WHERE user_id = ?",
            (user_id,),
        )
        xp = int(row["xp"]) if row else 0
        message_count = int(row["message_count"]) if row else 0
        global_rank = None
        if message_count:
            global_rank = int(
                await self._fetchval(
                    """
                    SELECT COUNT(*) + 1
                    FROM user_chat_stats
                    WHERE xp > ?
                       OR (xp = ? AND message_count > ?)
                       OR (xp = ? AND message_count = ? AND user_id < ?)
                    """,
                    (xp, xp, message_count, xp, message_count, user_id),
                )
                or 1
            )

        server_xp = 0
        server_messages = 0
        server_rank = None
        if guild_id is not None:
            server_row = await self._fetchrow(
                """
                SELECT xp, message_count
                FROM server_chat_stats
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            server_xp = int(server_row["xp"]) if server_row else 0
            server_messages = int(server_row["message_count"]) if server_row else 0
            if server_messages:
                server_rank = int(
                    await self._fetchval(
                        """
                        SELECT COUNT(*) + 1
                        FROM server_chat_stats
                        WHERE guild_id = ?
                          AND (
                              xp > ?
                              OR (xp = ? AND message_count > ?)
                              OR (xp = ? AND message_count = ? AND user_id < ?)
                          )
                        """,
                        (guild_id, server_xp, server_xp, server_messages, server_xp, server_messages, user_id),
                    )
                    or 1
                )

        return {
            "xp": xp,
            "message_count": message_count,
            "global_rank": global_rank,
            "server_xp": server_xp,
            "server_message_count": server_messages,
            "server_rank": server_rank,
        }

    async def get_chat_leaderboard(self, limit: int = 10, guild_id: Optional[int] = None) -> list[dict]:
        limit = max(1, min(int(limit), 25))
        if guild_id is None:
            return await self._fetchall(
                """
                SELECT user_id, xp, message_count
                FROM user_chat_stats
                WHERE message_count > 0
                ORDER BY xp DESC, message_count DESC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
        return await self._fetchall(
            """
            SELECT user_id, xp, message_count
            FROM server_chat_stats
            WHERE guild_id = ? AND message_count > 0
            ORDER BY xp DESC, message_count DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        )

    async def get_server_leaderboard(self, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 25))
        return await self._fetchall(
            """
            SELECT
                guild_id,
                SUM(xp) AS xp,
                SUM(message_count) AS message_count,
                COUNT(*) AS active_users
            FROM server_chat_stats
            WHERE message_count > 0
            GROUP BY guild_id
            ORDER BY SUM(xp) DESC, SUM(message_count) DESC, guild_id ASC
            LIMIT ?
            """,
            (limit,),
        )

    async def claim_scheduled_job(self, job_key: str, interval_seconds: float) -> bool:
        """
        Atomically claim a due recurring job.

        A new job is scheduled one full interval into the future, so deploying
        or restarting the bot never causes an immediate notification.
        """
        now = time.time()
        next_run_at = now + interval_seconds
        inserted = await self._execute(
            """
            INSERT INTO scheduled_jobs (job_key, next_run_at)
            VALUES (?, ?)
            ON CONFLICT(job_key) DO NOTHING
            """,
            (job_key, next_run_at),
        )
        if inserted:
            return False

        claimed = await self._execute(
            """
            UPDATE scheduled_jobs
            SET next_run_at = ?
            WHERE job_key = ? AND next_run_at <= ?
            """,
            (next_run_at, job_key, now),
        )
        return claimed > 0

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
