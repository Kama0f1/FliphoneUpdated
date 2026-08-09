from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cogs.report import Report
from database import Database


class _HistoryChannel:
    def __init__(self, messages):
        self.messages = messages
        self.history_kwargs = None

    async def history(self, **kwargs):
        self.history_kwargs = kwargs
        messages = self.messages if kwargs.get("oldest_first") else reversed(self.messages)
        for message in messages:
            yield message


class _PrivacyDatabase:
    def __init__(self, opted_out_ids: set[int]):
        self.opted_out_ids = opted_out_ids

    async def is_content_opted_out(self, user_id: int) -> bool:
        return user_id in self.opted_out_ids


class _EvidenceMessage:
    def __init__(self, message_id: int, deleted: list[int]):
        self.message_id = message_id
        self.deleted = deleted

    async def delete(self):
        self.deleted.append(self.message_id)


class _EvidenceChannel:
    def __init__(self):
        self.deleted: list[int] = []

    def get_partial_message(self, message_id: int):
        return _EvidenceMessage(message_id, self.deleted)


class _ReportAttachment:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.data = data
        self.read_modes: list[bool] = []

    async def read(self, *, use_cached: bool = False):
        self.read_modes.append(use_cached)
        return self.data


class _ReportEvidenceChannel:
    def __init__(self, attachments):
        self.attachments = attachments

    async def fetch_message(self, _message_id: int):
        return SimpleNamespace(attachments=self.attachments)


def _message(
    message_id: int,
    user_id: int,
    content: str,
    *,
    webhook: bool = False,
    bot: bool = False,
    display_name: str | None = None,
):
    author = SimpleNamespace(
        id=user_id,
        bot=bot,
        display_name=display_name or f"user-{user_id}",
        display_avatar=SimpleNamespace(url=f"https://cdn.example/avatar-{user_id}.png"),
        __str__=lambda self: self.display_name,
    )
    return SimpleNamespace(
        id=message_id,
        author=author,
        webhook_id=999 if webhook else None,
        content=content,
        attachments=[],
        created_at=datetime(2026, 8, 4, 12, tzinfo=timezone.utc) + timedelta(seconds=message_id),
        stickers=[],
        embeds=[],
        reactions=[],
        reference=None,
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
        self.assertEqual(entries[0]["avatar_url"], "https://cdn.example/avatar-10.png")

    async def test_context_fetches_every_message_in_the_call_window(self):
        messages = [_message(index, 10, f"message {index}") for index in range(1, 151)]
        channel = _HistoryChannel(messages)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=42),
            db=_PrivacyDatabase(set()),
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

        self.assertEqual(len(entries), 150)
        self.assertEqual(entries[0]["content"], "message 1")
        self.assertEqual(entries[-1]["content"], "message 150")
        self.assertIsNone(channel.history_kwargs["limit"])
        self.assertTrue(channel.history_kwargs["oldest_first"])

    async def test_room_webhooks_are_attributed_to_their_station(self):
        messages = [
            _message(1, 999, "alpha message", webhook=True, bot=True, display_name="Station Alpha · one"),
            _message(2, 999, "bravo message", webhook=True, bot=True, display_name="Station Bravo · two"),
            _message(3, 999, "unknown message", webhook=True, bot=True, display_name="another webhook"),
        ]
        channel = _HistoryChannel(messages)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=42),
            db=_PrivacyDatabase(set()),
            get_channel=lambda _channel_id: channel,
        )
        report = Report(bot)

        entries = await report._fetch_report_context(
            123,
            {
                "other_guild_id": 20,
                "started_at": "2026-08-04T11:00:00+00:00",
                "ended_at": "2026-08-04T13:00:00+00:00",
                "station_guild_ids": {"Alpha": 10, "Bravo": 20},
            },
            reporting_guild_id=30,
        )

        self.assertEqual([entry["guild_id"] for entry in entries], [10, 20, 0])

    def test_transcript_chunks_preserve_utf8_content(self):
        transcript = "first line\n" + ("animated emoji 😭\n" * 20) + "last line"
        chunks = Report._split_transcript(transcript, max_bytes=64)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 64 for chunk in chunks))
        self.assertEqual(b"".join(chunks).decode("utf-8"), transcript)

    def test_readable_transcript_has_one_header_and_round_trips_messages(self):
        bot = SimpleNamespace(db=_PrivacyDatabase(set()))
        report = Report(bot)
        transcript = report._build_transcript(
            report_id=7,
            reason="A test report",
            reported_name="Reported Server",
            reported_guild_id=2,
            reporting_name="Reporting Server",
            reporting_guild_id=1,
            entries=[
                {
                    "guild_id": 2,
                    "username": "First Speaker",
                    "user_id": None,
                    "avatar_url": "https://cdn.example/first.png",
                    "timestamp": "2026-08-04T12:00:01+00:00",
                    "content": (
                        "first line\n"
                        "----- FLIPHONE MESSAGE 999999 -----\n"
                        "second line"
                    ),
                },
                {
                    "guild_id": 1,
                    "username": "Second Speaker",
                    "user_id": 22,
                    "avatar_url": "https://cdn.example/second.png",
                    "timestamp": "2026-08-04T12:00:02+00:00",
                    "content": "reply",
                },
            ],
        )

        self.assertEqual(transcript.count("FLIPHONE CALL REPORT #7"), 1)
        parsed = report._parse_transcript(transcript)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["side"], "REPORTED")
        self.assertEqual(
            parsed[0]["content"],
            "first line\n----- FLIPHONE MESSAGE 999999 -----\nsecond line",
        )
        self.assertEqual(parsed[0]["avatar_url"], "")
        self.assertEqual(parsed[1]["side"], "REPORTING")
        self.assertEqual(parsed[1]["user_id"], "22")
        self.assertNotIn("Avatar URL", transcript)

        viewer_data = report._build_viewer_data(
            [
                {
                    "guild_id": 2,
                    "username": "First Speaker",
                    "user_id": None,
                    "avatar_url": "https://cdn.example/first.png",
                    "timestamp": "2026-08-04T12:00:01+00:00",
                    "content": "message",
                }
            ],
            reported_guild_id=2,
            reporting_guild_id=1,
        )
        parsed_viewer_data = report._parse_viewer_data(viewer_data)
        self.assertEqual(parsed_viewer_data[0]["side"], "REPORTED")
        self.assertEqual(
            parsed_viewer_data[0]["avatar_url"],
            "https://cdn.example/first.png",
        )

    def test_report_excerpt_uses_readable_message_blocks(self):
        report = Report(SimpleNamespace(db=_PrivacyDatabase(set())))
        excerpt = report._build_readable_excerpt(
            [
                {
                    "guild_id": 2,
                    "username": "Speaker",
                    "timestamp": "2026-08-04T12:00:01+00:00",
                    "content": "A readable message",
                }
            ],
            reported_guild_id=2,
            reporting_guild_id=1,
        )

        self.assertEqual(
            excerpt,
            "**Speaker** | `REPORTED` | `12:00:01`\n> A readable message",
        )

    async def test_report_viewer_loads_spoiler_data_from_the_direct_attachment_url(self):
        viewer_data = (
            b'{"timestamp":"2026-08-04T12:00:01+00:00","side":"REPORTED",'
            b'"username":"Speaker","user_id":null,"avatar_url":"https://cdn.example/a.png",'
            b'"content":"message"}'
        )
        attachment = _ReportAttachment(
            "SPOILER_report-7-viewer-data.jsonl",
            viewer_data,
        )
        channel = _ReportEvidenceChannel([attachment])
        bot = SimpleNamespace(
            db=_PrivacyDatabase(set()),
            get_channel=lambda _channel_id: channel,
        )
        report = Report(bot)

        entries = await report._load_report_transcript(
            {
                "id": 7,
                "review_channel_id": 123,
                "review_msg_id": 456,
                "review_message_ids": "456",
            }
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["content"], "message")
        self.assertEqual(entries[0]["avatar_url"], "https://cdn.example/a.png")
        self.assertEqual(attachment.read_modes, [False])

    async def test_deleting_report_removes_every_evidence_part(self):
        channel = _EvidenceChannel()
        bot = SimpleNamespace(db=_PrivacyDatabase(set()), get_channel=lambda _channel_id: channel)
        report = Report(bot)

        await report._delete_call_report_review_message(
            {
                "review_channel_id": 123,
                "review_msg_id": 11,
                "review_message_ids": "11,12,13",
            }
        )

        self.assertEqual(channel.deleted, [11, 12, 13])


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
            conn.execute(
                """
                CREATE TABLE call_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_guild_id INTEGER NOT NULL,
                    reporter_user_id INTEGER NOT NULL,
                    reported_guild_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    call_started_at TEXT,
                    call_ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    review_msg_id INTEGER,
                    review_channel_id INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO call_reports
                    (reporter_guild_id, reporter_user_id, reported_guild_id, reason, created_at)
                VALUES (10, 20, 30, 'existing report', 'old')
                """
            )
            conn.commit()
            conn.close()

            database = Database(path=path, database_url="")
            await database.init()
            row = await database._fetchrow(
                "SELECT user_id, anonymous, content_opt_out FROM user_preferences WHERE user_id = ?",
                (1,),
            )
            has_review_message_ids = await database._has_column(
                "call_reports", "review_message_ids"
            )
            old_report = await database.get_call_report(1)
            await database.close()

            self.assertEqual(row["user_id"], 1)
            self.assertEqual(row["anonymous"], 1)
            self.assertEqual(row["content_opt_out"], 0)
            self.assertTrue(has_review_message_ids)
            self.assertEqual(old_report["reason"], "existing report")
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
