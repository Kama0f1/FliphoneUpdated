# Privacy Policy - Fliphone

**Last updated: August 4, 2026**

## 1. Overview

Fliphone is a Discord bot that connects channels from different servers for real time 1:1 conversations and group rooms.

This policy explains what Fliphone processes, what it stores, why it uses that information, and what choices users and server administrators have.

By using Fliphone, you acknowledge the practices described in this policy.

## 2. Message Content

Fliphone processes ordinary server message content only after confirming that the message was sent in an administrator configured Fliphone channel during an active 1:1 call or group room.

Messages in unrelated channels and messages sent outside an active Fliphone conversation are ignored before their content is inspected. Public commands and restricted staff tools use Discord application commands and do not require Fliphone to read normal channel messages.

During an active conversation, Fliphone may process text, links, GIF references, custom emoji markup, attachment metadata, reply references, and safety filter results. Permitted content is relayed to participating Discord channels through Discord webhooks.

Fliphone does not write ordinary conversation text to PostgreSQL, SQLite, log files, or backup files. It does not maintain a post call transcript cache.

Relayed messages remain inside Discord and may remain visible in participating channels according to Discord settings and deletion behavior.

Messages beginning with `x ` or `X ` remain in the original channel and are not relayed or included in new report context.

## 3. Information Stored by Fliphone

1. **Server and channel IDs.** These are used for setup, routing, matchmaking, rooms, blocks, and moderation.

2. **User IDs.** These are used for preferences, bans, notifications, reports, profiles, experience points, rankings, and moderation history.

3. **Setup administrator IDs.** These record who configured Fliphone in a server.

4. **Webhook URLs.** These allow Fliphone to deliver relayed messages. Webhook URLs are treated as sensitive credentials.

5. **Queue, call, and room metadata.** This includes active routing state, start and end times, message counts, station assignments, and call statistics. It does not include ordinary conversation text.

6. **User preferences.** This includes notification settings, anonymous identity settings, profile banner selections, and message relay privacy choices.

7. **Moderation records.** This includes user bans, server bans, server block lists, custom censor words, report metadata, review actions, and resolution status.

8. **Profile and usage records.** This includes experience points, levels, message counts, rankings, and server statistics.

9. **GIF review records.** This includes submitted or reported URLs, submitter and source IDs, review status, and moderator actions.

10. **Custom emoji review records.** This includes the original emoji ID, name, animation status, submitter and source IDs, review status, mirrored Fliphone application emoji ID, usage count, last use time, and moderator actions.

11. **Scheduled task records.** These prevent maintenance jobs and announcements from running more often than intended.

Production records are stored in managed PostgreSQL hosted through Railway. SQLite is used for local development, verified backups, and migration work.

## 4. Reports and Evidence

Users may submit a conversation report with `/report`. The report form collects a written reason and may collect a voluntarily supplied media link.

When a report is submitted, Fliphone identifies the active or most recent relevant conversation and fetches recent context from the reporting Discord channel. Fliphone includes no more than 50 relevant messages from the selected conversation. Messages from users who enabled `/privacy optout` and messages beginning with `x ` are excluded.

The fetched context exists in application memory only while the report is assembled. It is sent to a private Discord moderation channel as a readable review message and, when context is available, a text attachment. It is then discarded from Fliphone's local memory.

Ordinary conversation context is not stored in PostgreSQL, SQLite, application logs, or backup files. The private Discord report message is scheduled for deletion when the report is resolved or after 30 days, whichever occurs first.

The database stores report metadata, including the reporter user and server IDs, the reported server ID, the written report reason, call timestamps, report status, private review message ID, and private review channel ID. This metadata allows authorized moderators to manage the report and remove its Discord evidence.

Only the bot owner and users listed as trusted Fliphone moderators may use report review commands or report panel controls.

## 5. Temporary Application Data

Fliphone temporarily keeps routing caches, rate limit counters, webhook caches, reply mappings, reaction mappings, and short lived permission results in application memory.

This information supports active calls, rooms, replies, reactions, abuse prevention, and reliability. It is cleared when no longer needed, when the relevant conversation ends, or when the bot process restarts.

## 6. Information Fliphone Does Not Request

Fliphone does not request passwords, email addresses, payment information, government identity documents, real names, physical addresses, IP addresses, device identifiers, voice recordings, or microphone data.

Fliphone does not sell personal information, create advertising profiles, or use message content to train machine learning or artificial intelligence models.

Users should not send sensitive personal information through calls, rooms, reports, or support requests.

## 7. Services Used by Fliphone

1. **Discord.** Discord hosts servers, messages, webhooks, application commands, application emojis, and private moderation channels.

2. **Railway.** Railway hosts the Fliphone service and its PostgreSQL database.

3. **top.gg.** Fliphone may send its aggregate server count to top.gg for its public listing.

4. **Tenor, Giphy, and Klipy.** These services provide or host supported GIF content.

5. **GitHub.** GitHub hosts Fliphone policies and source code.

These services process information under their own terms and privacy policies.

## 8. Retention

1. Server configuration and webhook records remain until setup is replaced, removed with `/teardown`, manually deleted, or no longer required.

2. Queue and active conversation state remains until the queue entry, call, or room ends.

3. Notification and privacy preferences remain until changed or deleted.

4. Call history, profile statistics, and rankings may remain while those service features operate.

5. Ban and block records remain until removed or no longer needed for safety.

6. Report metadata and written reasons may remain for safety, moderation history, and abuse prevention.

7. Report evidence in the private Discord moderation channel is scheduled for deletion when resolved or after 30 days.

8. GIF and custom emoji review records may remain to prevent duplicate submissions, enforce review decisions, and preserve moderation history.

9. Encrypted SQLite backups are created from production data, verified after creation, and rotated so only the four newest backup files are retained. The encryption key is stored separately.

Running `/teardown` removes active server configuration, webhook association, queue entries, and live conversation state. It does not automatically remove every historical statistic, moderation record, report, ban, or ranking entry.

## 9. User and Administrator Controls

1. Begin a message with `x ` to keep it local.

2. Use `/privacy optout` to prevent future messages from being relayed or included in new report context.

3. Use `/privacy optin` to participate in message relay again.

4. Use `/hangup` or `/roomleave` to leave an active conversation.

5. Use `/notify` to change queue notification preferences.

6. Use `/teardown` with Manage Channels permission to remove active Fliphone setup from a server.

7. Contact the support server to request access, correction, or deletion of associated stored information.

## 10. Data Requests

To request access, correction, or deletion of information associated with your Discord user ID, contact the Fliphone team through the official support server:

https://discord.gg/t3KHGqPuEP

Include your Discord user ID so relevant records can be located. Do not provide passwords, identity documents, or unrelated sensitive information.

Some information may be retained when reasonably necessary for safety, abuse prevention, unresolved reports, security, or legal obligations.

## 11. Security

Database access is restricted to the bot operator and authorized service infrastructure. Moderation commands and review panels verify the Discord user ID of the caller against the trusted staff list.

Webhook URLs and backup encryption keys are treated as sensitive credentials. No online service can guarantee absolute security, but reasonable technical and access controls are used to protect stored information.

## 12. Age Requirements

Users must meet Discord's minimum age requirements for their country. Fliphone is not intended for anyone who is not legally permitted to use Discord.

## 13. Changes and Contact

This policy may be updated when Fliphone features, hosting, retention practices, or legal obligations change. The latest revision date appears at the top of this policy.

For privacy questions, data requests, or safety concerns, join the official Fliphone support server:

https://discord.gg/t3KHGqPuEP
