# Privacy Policy for Fliphone

**Last updated: June 23, 2026**

Fliphone is a Discord bot that connects users across servers for 1:1 calls and group rooms. This policy explains what data the bot handles and why.

## Data We Handle

| Data | Purpose | Retention |
|---|---|---|
| Server, channel, and user IDs | Setup, routing, moderation, preferences, XP, and rankings | Until no longer needed or deletion is requested |
| Relay webhook URLs | Deliver messages with the sender's chosen relay identity | Until setup is removed or repaired |
| Call and room metadata | Queue state, start/end times, message counts, statistics, and reliability | Stored in the bot database |
| User preferences | Notification choice, personal mask setting, and profile banner | Stored until changed or deleted |
| Moderation data | Bot-wide bans, server blocks, reports, and resolution status | Stored for safety and abuse prevention |
| GIF URLs | Safety checks, reports, whitelist submissions, and blacklists | Stored when submitted or reported |
| Recent message excerpts | Allow users to report short or active conversations | See the section below |

## Message Content and Reports

Fliphone relays message content in real time. Ordinary live conversation text is not stored as long-term database data and is not written to PostgreSQL or weekly database backups.

For safety reports, the bot keeps a rolling in-memory buffer of up to the last 50 relayed messages per conversation. Each entry is limited to 2,000 characters. The buffer is removed 7 days after a call or room ends, when the process restarts, or when it is otherwise cleared.

If a user submits a report, a short preview and a text file containing the captured conversation excerpt may be posted to a private Discord moderation channel for review. The excerpt contains at most the last 50 relayed messages. Report context is limited and retained for 7 days or less in Fliphone's own systems; report messages in Discord can be deleted after review or upon a valid deletion request. Report metadata and the report reason are stored in the database.

Messages beginning with `x ` or `X ` stay in the local Discord channel and are not relayed or added to the report buffer.

## Data We Do Not Request

Fliphone does not request passwords, email addresses, payment information, or real-world identity information. Users should not send sensitive personal information through calls or rooms.

## Storage and Security

Production data is stored in managed PostgreSQL. Verified SQLite backup snapshots are created weekly, encrypted with AES-GCM, and rotated so only the four newest backups are kept. The encryption key is stored separately from the backups.

Database and moderation access is restricted to the bot owner and trusted moderators where required. Fliphone does not sell personal data.

## User Choices

- Server administrators can remove their server setup with `f.teardown`.
- Users can toggle queue notification DMs with `f.notify`.
- Users can keep a message local by beginning it with `x `.
- Users can request deletion of associated stored data through the support server, subject to safety and legal retention needs.

## Contact

Questions and deletion requests can be submitted through the official Fliphone support server linked in `f.help`.
