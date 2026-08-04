# 📞 Fliphone

> Cross-server chat roulette for Discord. Connect your server to a random stranger's server — anonymously.

[![Discord](https://img.shields.io/badge/Support_Server-5865F2?style=flat&logo=discord&logoColor=white)](https://discord.gg/t3KHGqPuEP)
[![top.gg](https://img.shields.io/badge/Vote_on_top.gg-FF3366?style=flat)](https://top.gg/bot/1489342959974486158/vote)
[![Privacy Policy](https://img.shields.io/badge/Privacy_Policy-30363d?style=flat)](https://gist.github.com/Kama0f1/431f01bbbf1ae6243505778376ba0fb3)

---

## What is Fliphone?

Fliphone connects Discord servers for real-time conversations. It supports 1:1 calls and **group rooms** with up to 5 servers chatting at once.

Messages are relayed in real time. Conversation text is not retained in Fliphone's database or in a post-call cache; report context is fetched from Discord only when a user submits a report.

---

## Features

- **1-on-1 calls** — matched instantly when someone is waiting, or queued for up to 10 minutes
- **Group rooms** — up to 5 servers in one room, identified by station names
- **Webhook relay** — messages appear with the sender's filtered display name and global Discord avatar
- **Personal anon mode** — anon users appear as tarot card identities
- **GIF moderation** — built-in report system with blacklist/whitelist management
- **Content filter** — automatic censorship of slurs and harmful language
- **Block list** — prevent specific servers from ever matching with yours again
- **Queue notifications** — opt-in DMs when someone is waiting with no match
- **Vote reminders** — opt-in top.gg vote prompts (max once per 12 hours)
- **Rate limiting** — lax flood protection with progressive warnings
- **Inactivity auto-hangup** — calls end automatically after 10 minutes of silence

---

## Quick Start

### 1. Invite the bot

[**➕ Add Fliphone to your server**](https://discord.com/api/oauth2/authorize?client_id=1489342959974486158&permissions=536988736&scope=bot+applications.commands)

**Required permissions:**
| Permission | Used for |
|---|---|
| View Channel | Reading messages in the phonebooth channel |
| Send Messages | Sending relay messages and status embeds |
| Manage Webhooks | Required for all cross-server message relay |
| Embed Links | All status and help embeds |
| Attach Files | Relaying allowed media and profile assets |
| Read Message History | Webhook lookup and reply context |
| Add Reactions | Relaying standard emoji reactions |

Discord may let someone with **Manage Server** authorize the app while omitting requested permissions that person
cannot grant. If the authorization screen places **Manage Webhooks** under "you can't grant them," cancel and have
the server owner or an admin with Manage Webhooks use the invite. Channel and category overrides can also deny
permissions afterward. If Fliphone appears offline or cannot see a channel, allow its role in that channel/category;
an owner or moderator with **Manage Roles** may be required to edit the overwrite.

### 2. Set up your channel

In the channel you want to use as the phonebooth, run:

```
/setup
```

Use `/check` for an exact permission diagnosis and `/repair` after correcting permissions.

`/setup` is also the reset button: it clears stale call/queue/room state, removes old Fliphone webhooks, creates a fresh webhook, saves the channel, and tests avatar delivery.

If setup says the server role is missing permissions, use the re-invite link. If it says a channel/category is
overriding permissions, fix that overwrite instead. Use `@Fliphone teardown` only when you want to remove Fliphone completely.

### 3. Start a call

```
/call
```

If another server is waiting, you connect instantly. Otherwise you join the queue (auto-cancels after 10 minutes).

---

## Command Reference

Public commands use Discord application commands. Restricted maintenance commands use `@Fliphone command`.

### 📞 Calling

| Command | Permission | Description |
|---|---|---|
| `/call` | Everyone | Join the queue or connect instantly |
| `/hangup` | Everyone | End the active call or leave the queue |
| `/skip` | Everyone | Hang up and immediately search for a new call |
| `/status` | Everyone | Show current status |
| `/block` | Everyone | Block the connected server or room station |
| `/anon` | Everyone | Toggle your personal tarot anon identity |
| `/friendrequest` | Everyone | Share your username as a friend-request card |
| `/privacy` | Everyone | View or change your message relay opt-out |
| `/notify` | Everyone | Toggle queue DM notifications |
| `/profile` | Everyone | Show your settings, XP, ranks, and banner |
| `/report` | Everyone | Report your active or most recent call |
| `/addgif` | Everyone | Submit a GIF URL for review |
| `/addemoji` or `/addemojis` | Everyone | Submit up to five custom emojis in one command |
| `/stats` | Everyone | Show global call statistics |
| `/invite` | Everyone | Get the bot invite link |

### 📡 Group Rooms

| Command | Permission | Description |
|---|---|---|
| `/room` | Everyone | Join a group room |
| `/roomcreate` | Everyone | Create a fresh group room |
| `/roomleave` | Everyone | Leave your current room |
| `/roomskip` | Everyone | Leave and immediately search for a new room |
| `/roomstatus` | Everyone | Show current room info |
| `/roomkick` | Everyone | Start a vote to kick a station |

### ⚙️ Server Setup

| Command | Permission | Description |
|---|---|---|
| `/setup` | Manage Channels | Reset and fully configure this channel in one step |
| `/check` | Manage Channels | Diagnose setup, permissions, webhook, and call state |
| `/repair` | Manage Channels | Reset and rebuild the configured channel |
| `@Fliphone dbstatus` | Trusted staff | Show database health and safe row counts |
| `@Fliphone teardown` | Manage Channels | Remove Fliphone from this server |
| `/blocklist` | Manage Channels | List servers your server has blocked |
| `/unblock` | Manage Channels | Unblock a server by its ID |
| `/kick` | Manage Channels | Force-disconnect the active call |

### Restricted Tools

Owner and trusted moderator commands are intentionally hidden from normal help.
Use `@Fliphone sudohelp` to view moderation panels, GIF review tools, global bans,
censor controls, and guild audit tools.

---

## How Calls Work

```
Server A  ──/call──►  Queue  ◄──/call──  Server B
                         │
                    [matched!]
                         │
Server A  ◄──relay──   Bot  ──relay──►  Server B
```

1. Admin runs `/setup` in the chosen channel — registers it and creates a webhook.
2. A user runs `/call` — bot looks for a match from a *different* server (respecting block lists).
   - **Match found** → both channels get a *Connected!* message and relay begins.
   - **No match** → channel enters the queue (10-minute timeout, then auto-cancels).
3. Every non-command message sent in a connected channel is relayed to the partner.
4. Either side runs `/hangup` → call ends, stats logged, both channels notified.

### Relay priority

1. **Webhook relay** — message appears with the sender's filtered display name and avatar.
2. Fliphone saves each channel's webhook and can keep using a valid existing webhook if Manage Webhooks is later removed.
3. If a webhook itself breaks, Fliphone repairs it when Manage Webhooks is available or safely stops the call instead of exposing messages through a plain bot fallback.

### What gets relayed

- ✅ Text messages (censored through the content filter)
- ✅ GIFs from Tenor, Giphy, and Klipy (with per-server mode controls)
- ✅ Replies with context (shows who you're replying to)
- ❌ Stickers and all non-GIF attachments
- ❌ External links (stripped — only GIF links are allowed)
- ❌ File, image, video, and audio attachments (blocked for safety)
- ❌ Custom emoji (they won't render in other servers)

---

## How Group Rooms Work

Group rooms work like a conference call for up to 6 servers. Each server is assigned a **NATO station name** (Alpha, Bravo, Charlie, Delta, Echo, Foxtrot) — your real server name is never revealed.

- Run `/room` to join. If an active room has space and you haven't been in it, you slot straight in.
- If no room is available, a new room is created and waits for others.
- A room becomes **active** once 2+ servers have joined.
- New servers can join active rooms up to the maximum size of 6.
- If fewer than 2 servers remain after someone leaves, the room closes automatically.

**Vote kick:** Any server can run `/roomkick` to start a majority vote to remove a misbehaving station (requires 3+ servers in the room). The vote lasts 60 seconds.

---

## GIF Mode

Server admins can control how GIFs are handled in their server's calls:

| Mode | Behaviour |
|---|---|
| `enabled` | All GIFs from Tenor, Giphy, Klipy, and `.gif` links are relayed (default) |
| `limited` | Only Tenor, Giphy, and Klipy links are relayed — direct `.gif` URLs blocked |
| `disabled` | All GIFs are blocked — none sent or received |

When one side has restrictions, both sides are notified at the start of the call.

---

## Configuration (`.env`)

```env
# Required
DISCORD_TOKEN=your_bot_token_here

# Optional
DATABASE_URL=                  # PostgreSQL URL. If empty, SQLite is used.
DB_PATH=phonebooth.db          # SQLite file path for fallback/migration
DATABASE_POOL_SIZE=5           # PostgreSQL connection pool size
PG_STATEMENT_CACHE_SIZE=0      # Keep 0 for managed DB pooler compatibility
TRUSTED_MOD_IDS=               # Comma-separated trusted moderator user IDs
QUEUE_TIMEOUT=10               # Minutes before queue auto-cancels
REPORT_LOG_CHANNEL_ID=0        # Channel ID for GIF report logs (0 = disabled)
LOG_CHANNEL_ID=0               # Channel ID for console logs posted by the bot
TOPGG_TOKEN=                   # top.gg API token for server count posting
```

---

## SQLite to PostgreSQL Migration

1. Stop the bot on every host, including Heavencloud and your PC.
2. Download `phonebooth.db`, `phonebooth.db-wal`, and `phonebooth.db-shm` into the same folder.
3. Create a managed PostgreSQL database and put its connection string in `DATABASE_URL`.
4. Install dependencies, then run:

```bash
pip install -r requirements.txt
python scripts/migrate_sqlite_to_postgres.py --sqlite-path phonebooth.db
```

The migration script creates a clean SQLite snapshot first, copies all tracked tables into PostgreSQL, resets identity sequences, and prints row counts only. It will refuse to copy into a non-empty PostgreSQL database unless you pass `--clear-postgres`.

After the migration succeeds, keep `DATABASE_URL` set and start the bot with `python main.py`. Leave the old Heavencloud bot offline so new data does not split across two databases again.

---

## Self-Hosting

### Requirements

- Python 3.11+
- Dependencies in `requirements.txt`

### Setup

```bash
git clone https://github.com/YOUR_USERNAME/fliphone
cd fliphone
pip install -r requirements.txt
cp env.example .env
# Edit .env and add your DISCORD_TOKEN
python main.py
```

### Docker

```bash
docker build -t fliphone .
docker run -e DISCORD_TOKEN=your_token fliphone
```

### Required Gateway Intents

Enable these in the [Discord Developer Portal](https://discord.com/developers/applications) under your bot's settings:

- ✅ **Message Content Intent** — required to read and relay message text

---

## Database Schema

| Table | Purpose |
|---|---|
| `guild_config` | One row per server that has run `/setup` |
| `queue` | Channels currently waiting for a match |
| `connections` | Active, live 1:1 calls |
| `call_history` | Completed calls (used for stats) |
| `blocked_guilds` | Server-level block list |
| `banned_users` | Bot-wide user ban list |
| `custom_words` | Custom censor words added with the restricted `@Fliphone censor` command |
| `gif_reports` | GIF URLs reported by users, pending review |
| `profile_banners` | Saved user profile banner choices |
| `user_chat_stats` | Global user XP, chat count, and ranking data |
| `server_chat_stats` | Per-server user XP and leaderboard data |
| `gif_url_list` | Blacklisted and whitelisted GIF URLs |
| `rooms` | Group room sessions |
| `room_members` | Servers currently in a group room |
| `notify_subscribers` | Users opted into queue notifications |
| `call_reports` | User-submitted call reports awaiting moderation |

---

## Privacy & Terms

- [Privacy Policy](https://gist.github.com/Kama0f1/431f01bbbf1ae6243505778376ba0fb3)
- [Terms of Service](https://gist.github.com/Kama0f1/085bf38fb03e3c84d99f2ff9afc410af)

Ordinary message content is not stored in PostgreSQL or weekly backups. Report context is fetched from Discord only when a user submits `/report`, posted to a private Discord moderation channel, and discarded locally. Users can disable relay processing with `/privacy optout`.

---

## Support

- **Support server:** [discord.gg/t3KHGqPuEP](https://discord.gg/t3KHGqPuEP)
- **Vote:** [top.gg/bot/1489342959974486158/vote](https://top.gg/bot/1489342959974486158/vote)

---
