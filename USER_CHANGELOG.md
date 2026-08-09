# Fliphone Update Log

## August 2026 Privacy And Commands Update

* Slash commands are Fliphone's main controls, and `f.` prefix commands are available again for members who prefer them.

* Fliphone's status now explains that the service is rebuilding after database loss and apologizes for the disruption.

* Restricted moderation commands check the caller against the trusted staff user ID list. Unauthorized users cannot use the commands or their review panels.

* `/teardown` now uses Confirm and Cancel buttons instead of asking administrators to type a confirmation message.

* Message processing is limited to administrator configured Fliphone channels while a call or room is active. Messages in unrelated or inactive channels are ignored.

* `/privacy optout` prevents your future messages from being relayed or included in new report context. `/privacy optin` enables participation again.

* Reports no longer depend on a stored conversation buffer. When `/report` is submitted, Fliphone fetches the full available context for that call or room from Discord, sends it to the private moderation channel, and discards the context locally.

* Long report transcripts are automatically split into multiple Discord attachments so moderators do not lose the beginning of busy conversations.

* Report previews are easier to scan, with each speaker, side, timestamp, and message shown as a separate conversation block.

* New reports include an Open Conversation button for trusted moderators. It opens the full conversation privately with page controls and shows each speaker's profile picture when Discord provides one.

* Private report evidence is scheduled for deletion when the report is resolved or after 30 days.

* GIF and custom emoji submissions now use slash command fields. `/addemoji` and `/addemojis` both accept up to five custom emojis at once.

* Existing server setup, call history, profiles, moderation records, GIF reviews, emoji reviews, and other database records are preserved.

## Historical Update And Testing Notes

The sections below are retained from the earlier update cycle for project history. Some command names shown there use the former `f.` format and have since moved to slash commands.

## User-Facing Update Post

Fliphone QOL update.

What's changed:

1. Added `f.check` so server admins can quickly check if Fliphone is set up correctly.
2. Added a repair button inside `f.check`, plus `f.repair`, for fixing broken setup without doing everything manually again.
3. Added `f.dbstatus` for server admins to check whether the database is reachable.
4. Added `f.profile` so users can see their own Fliphone settings, including whether call notifications are enabled.
5. Improved waiting in the call queue. If someone is waiting too long, Fliphone now suggests `f.notify` so they can get a DM when calls are available.
6. Improved call queue messages with a clearer wait estimate.
7. Reworked `f.help` so normal users and server admins see the commands they actually need.
8. Started moving Fliphone away from local SQLite files and toward PostgreSQL for better hosting reliability.

The goal of this update is to aim for fewer broken setups, easier admin fixes, cleaner moderation, and less waiting around when no calls are active.

## Feature And Testing Checklist

Use this list after starting the bot.

### `f.check`

What it does:
Checks the server setup, permissions, configured channel, webhook, queue state, active calls, and rooms.

How to test:

1. Run `f.check` in a server where Fliphone is already set up.
2. Confirm it shows the setup status and database status.
3. Delete or break the Fliphone webhook, then run `f.check` again.
4. Confirm it shows an issue and offers the repair button.

### Repair Button And `f.repair`

What it does:
Repairs broken setup after `f.check`, especially missing channel or webhook problems.

How to test:

1. Break setup by deleting the stored webhook or the setup channel.
2. Run `f.check`.
3. Press the repair button, or run `f.repair`.
4. Run `f.check` again.
5. Confirm the setup now shows as healthy.

### `f.dbstatus`

What it does:
Shows whether the bot can reach the database, what backend it is using, and safe table counts.

How to test:

1. Run `f.dbstatus` as a server admin.
2. Confirm it shows `PostgreSQL` when `DATABASE_URL` is enabled.
3. Confirm it shows healthy/online status.
4. Confirm it does not show private row data or webhook URLs.

### `f.profile`

What it does:
Shows a user their Fliphone status and notification settings.

How to test:

1. Run `f.profile`.
2. Run `f.notify`.
3. Run `f.profile` again.
4. Confirm the notification status changed.

Aliases to test:

- `f.settings`
- `f.me`

### Call Queue Notify Reminder

What it does:
If someone waits in the call queue for around 90 seconds, Fliphone suggests using `f.notify`.

How to test:

1. Use a server/channel where nobody else is waiting for a call.
2. Run `f.call`.
3. Wait about 90 seconds.
4. Confirm the bot suggests `f.notify`.
5. Enable `f.notify`.
6. Try again and confirm the reminder does not repeat for that user.

### Call Queue Wait Estimate

What it does:
Queue messages now give a clearer idea that the user is waiting for another server.

How to test:

1. Run `f.call` when there is no match available.
2. Check the bot's response.
3. Confirm the message clearly says the user is waiting and includes the notification suggestion flow.

### GIF Report Panel

What it does:
Trusted moderators can review GIF reports with buttons/select controls instead of only reading a plain list.

How to test:

1. Create or submit a GIF report.
2. Have a trusted moderator run `f.gifreports`.
3. Select a report from the panel.
4. Test blacklist, whitelist, and refresh.
5. Confirm the panel updates correctly.

### User Report Panel

What it does:
Trusted moderators can review and resolve user reports with a cleaner panel.

How to test:

1. During or after a call, submit a report with `f.report`.
2. Have a trusted moderator run `f.userreports`.
3. Select a report.
4. Press resolve.
5. Refresh and confirm the report is marked resolved or removed from the active list.

### Updated `f.help`

What it does:
Shows regular user commands and server admin commands without mixing in owner-only tools.

How to test:

1. Run `f.help`.
2. Confirm user commands and server admin commands are shown.
3. Run `f.help check`.
4. Confirm command-specific help works.
5. Run as a normal user and confirm restricted owner tools are not shown.

### `f.sudohelp`

What it does:
Shows restricted owner/trusted moderator commands separately.

How to test:

1. Run `f.sudohelp` from the bot owner account.
2. Run `f.sudohelp` from one of the trusted moderator accounts.
3. Confirm restricted tools are listed.
4. Run it from a normal user account and confirm access is denied.

### Trusted Moderator IDs

What it does:
Allows selected trusted moderators to use moderation tools without making them the bot owner.

How to test:

1. Use one of the trusted moderator accounts.
2. Run `f.gifreports`.
3. Run `f.userreports`.
4. Confirm both commands work.
5. Try from a normal account and confirm they are blocked.

### PostgreSQL Support

What it does:
Lets Fliphone use Railway/PostgreSQL instead of relying only on local SQLite files.

How to test:

1. Keep `DATABASE_URL` set in `.env`.
2. Start the bot.
3. Run `f.dbstatus`.
4. Confirm it says PostgreSQL is being used.
5. Do not treat this as the final production database until the Heavencloud SQLite data has been migrated.

## When Heavencloud Comes Back

Do this before switching fully to Railway/PostgreSQL.

1. Stop the bot on Heavencloud.
2. Stop the bot anywhere else it is running.
3. Download these three files together:
   - `phonebooth.db`
   - `phonebooth.db-wal`
   - `phonebooth.db-shm`
4. Put all three files in the project folder.
5. Keep a backup copy of those three files somewhere safe.
6. Make sure `.env` has the Railway `DATABASE_URL`.
7. Run the migration:

```powershell
python scripts\migrate_sqlite_to_postgres.py --sqlite-path phonebooth.db --overwrite-snapshot
```

If the Railway database has test data that can be deleted, run this instead:

```powershell
python scripts\migrate_sqlite_to_postgres.py --sqlite-path phonebooth.db --overwrite-snapshot --clear-postgres
```

8. Check the migration output and make sure every table count matches.
9. Start the bot on your PC or Railway.
10. Run `f.dbstatus`.
11. Run `f.check` in the main servers.
12. Test `f.profile`, `f.notify`, `f.call`, `f.hangup`, reports, and GIF moderation.
13. Keep the old SQLite files as rollback backups.
14. Do not start the old Heavencloud bot again unless it is also using the same PostgreSQL database.

The most important part is step 14. If the old SQLite bot and the new PostgreSQL bot both run at the same time, the data can split again.
