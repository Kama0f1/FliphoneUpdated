from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from cogs.report import Report
from database import Database


class _HistoryChannel:
    def __init__(self, messages):
        self.messages = messages

    async def history(self, **_kwargs):
        for message in reversed(self.messages):
            yield message


class _PrivacyDatabase:
    def __init__(self, opted_out_ids: set[int]):
        self.opted_out_ids = opted_out_ids

    async def is_content_opted_out(self, user_id: int) -> bool:
        return user_id in self.opted_out_ids


def _message(
    message_id: int,
    user_id: int,
    content: str,
    *,
    webhook: bool = False,
    bot: bool = False,
):
    author = SimpleNamespace(
        id=user_id,
        bot=bot,
        display_name=f"user-{user_id}",
        __str__=lambda self: self.display_name,
    )
    return SimpleNamespace(
        id=message_id,
        author=author,
        webhook_id=999 if webhook else None,
        content=content,
        attachments=[],
        created_at=datetime(2026, 8, 4, 12, message_id, tzinfo=timezone.utc),
    )


class ReportContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_excludes_opted_out_local_and_local_only_messages(self):
        messages = [
            _message(1, 10, "local message"),
            _message(2, 20, "opted out message"),
            _message(3, 10, "x keep this local"),
            _message(4, 999, "relayed message", webhook=True, bot=True),
            _message(5, 42, "bot notice", bot=True),
        ]
        channel = _HistoryChannel(messages)
        privacy_db = _PrivacyDatabase({20})
        bot = SimpleNamespace(
            user=SimpleNamespace(id=42),
            db=privacy_db,
            get_channel=lambda _channel_id: channel,
        )
        report = Report(bot)

        entries = await report._fetch_report_context(
            123,
            {
                "other_guild_id": 2,
                "started_at": "2026-08-04T11:00:00+00:00",
                "ended_at": "2026-08-04T13:00:00+00:00",
            },
            reporting_guild_id=1,
        )

        self.assertEqual([entry["content"] for entry in entries], ["local message", "relayed message"])
        self.assertEqual([entry["guild_id"] for entry in entries], [1, 2])


class PrivacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_user_preferences_are_preserved(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    anonymous INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO user_preferences (user_id, anonymous, updated_at) VALUES (1, 1, 'old')"
            )
            conn.commit()
            conn.close()

            database = Database(path=path, database_url="")
            await database.init()
            row = await database._fetchrow(
                "SELECT user_id, anonymous, content_opt_out FROM user_preferences WHERE user_id = ?",
                (1,),
            )
            await database.close()

            self.assertEqual(row["user_id"], 1)
            self.assertEqual(row["anonymous"], 1)
            self.assertEqual(row["content_opt_out"], 0)
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
