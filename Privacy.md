# Privacy Policy for Fliphone

**Last updated: August 4, 2026**

Fliphone is a Discord bot that connects users across servers for 1:1 calls and group rooms. This policy explains what data the bot handles and why.

## Data We Handle

| Data | Purpose | Retention |
|---|---|---|
| Server, channel, and user IDs | Setup, routing, moderation, preferences, XP, and rankings | Until no longer needed or deletion is requested |
| Relay webhook URLs | Deliver messages with the sender's chosen relay identity | Until setup is removed or repaired |
| Call and room metadata | Queue state, start/end times, message counts, statistics, and reliability | Stored in the bot database |
| User preferences | Notification choice, personal mask setting, content opt-out, and profile banner | Stored until changed or deleted |
| Moderation data | Bot-wide bans, server blocks, reports, and resolution status | Stored for safety and abuse prevention |
| GIF URLs | Safety checks, reports, whitelist submissions, and blacklists | Stored when submitted or reported |
| Report evidence | Review a conversation only after a user submits `/report` | Stored in a private Discord moderation channel until resolved or for no more than 30 days |

## Message Content and Reports

Fliphone processes message content only in an administrator-configured Fliphone channel during an active 1:1 call or group room. Messages in unrelated channels and messages sent outside an active conversation are ignored by the relay. Ordinary live conversation text is not written to PostgreSQL, SQLite, or weekly database backups.

Fliphone does not maintain a post-call transcript cache. When a user submits `/report`, the bot fetches up to 50 recent messages for that call directly from the reporting Discord channel. This context exists in application memory only while the report is assembled, is sent to a private Discord moderation channel, and is then discarded locally.

Report evidence is deleted from the private moderation channel when the report is resolved or after 30 days, whichever happens first. Report metadata and the reporter's written reason may remain in the database for safety and abuse prevention. Access to the moderation channel is limited to authorized Fliphone moderators.

Messages beginning with `x ` or `X ` stay in the local Discord channel and are not relayed or included in report context. Users can run `/privacy optout` to prevent their future messages from being relayed or included in new report context. They can run `/privacy optin` to participate again.

## Data We Do Not Request

Fliphone does not request passwords, email addresses, payment information, or real-world identity information. Users should not send sensitive personal information through calls or rooms.

## Storage and Security

Production data is stored in managed PostgreSQL. Verified SQLite backup snapshots are created weekly, encrypted with AES-GCM, and rotated so only the four newest backups are kept. The encryption key is stored separately from the backups.

Database and moderation access is restricted to the bot owner and trusted moderators where required. Fliphone does not sell personal data.

## User Choices

- Server administrators can remove their server setup with `@Fliphone teardown`.
- Users can toggle queue notification DMs with `/notify`.
- Users can keep a message local by beginning it with `x `.
- Users can disable or re-enable message relay with `/privacy optout` and `/privacy optin`.
- Users can request deletion of associated stored data through the support server, subject to safety and legal retention needs.

## Contact

Questions and deletion requests can be submitted through the official Fliphone support server linked in `/help`.
