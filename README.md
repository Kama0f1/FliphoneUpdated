# 📞 Fliphone

> Cross-server chat roulette for Discord. Connect your server to a random stranger's server — anonymously.

[![Discord](https://img.shields.io/badge/Support_Server-5865F2?style=flat&logo=discord&logoColor=white)](https://discord.gg/t3KHGqPuEP)
[![top.gg](https://img.shields.io/badge/Vote_on_top.gg-FF3366?style=flat)](https://top.gg/bot/1489342959974486158/vote)
[![Privacy Policy](https://img.shields.io/badge/Privacy_Policy-30363d?style=flat)](https://YOUR_DOMAIN/privacy.html)

---

## What is Fliphone?

Fliphone connects your Discord server's designated channel with a random stranger's server for a real-time, anonymous conversation — like a phone call between two servers. It also supports **group rooms** with up to 6 servers chatting at once.

Messages are relayed in real time. Fliphone **never stores message content.**

---

## Features

- **1-on-1 calls** — matched instantly when someone is waiting, or queued for up to 10 minutes
- **Group rooms** — up to 6 servers in one room, identified by NATO station names
- **Webhook relay** — messages appear with the sender's username and avatar (when bot has Manage Webhooks)
- **Anonymous mode** — senders appear as *Stranger [NATO word]* with a robot avatar
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
| Manage Webhooks | ⭐ Seamless relay with real avatars/names |
| Embed Links | All status and help embeds |
| Read Message History | Webhook lookup and reply context |
| Add Reactions | Future feature support |

### 2. Set up your channel

In the channel you want to use as the phonebooth, run:

```
f.setup
```

The bot creates a webhook automatically (if it has Manage Webhooks permission) and registers that channel. You only need to do this once.

### 3. Start a call

```
f.call
```

If another server is waiting, you connect instantly. Otherwise you join the queue (auto-cancels after 10 minutes).

---

## Command Reference

**Prefix:** `f.` (also `F.` — case-insensitive)

### 📞 Calling

| Command | Aliases | Permission | Description |
|---|---|---|---|
| `f.call` | `f.c`, `f.dial`, `f.connect` | Everyone | Join the queue or connect instantly |
| `f.hangup` | `f.h`, `f.disconnect`, `f.bye` | Everyone | End the active call or leave the queue |
| `f.skip` | `f.s`, `f.next` | Everyone | Hang up and immediately search for a new call |
| `f.status` | `f.pbstatus` | Everyone | Show current status (idle / queued / in call) |
| `f.block` | — | Everyone | Block the server you're currently talking to |
| `f.anon` | `f.mask`, `f.anonymous` | Everyone | Toggle anonymous mode for your server |
| `f.fr` | `f.friendrequest` | Everyone | Share your username as a friend-request card |
| `f.notify` | `f.notifications` | Everyone | Toggle queue DM notifications (opt-in) |
| `f.profile` | `f.settings`, `f.me` | Everyone | Show your settings and current server/channel state |
| `f.report` | — | Everyone | Report your active or most recent call |
| `f.gifmode` | — | Server Admin | Set GIF policy: `enabled`, `limited`, or `disabled` |
| `f.stats` | — | Everyone | Show global call statistics |
| `f.invite` | — | Everyone | Get the bot invite link |

### 📡 Group Rooms

| Command | Aliases | Permission | Description |
|---|---|---|---|
| `f.room` | `f.r` | Everyone | Join a group room (up to 6 servers) |
| `f.roomleave` | `f.rl` | Everyone | Leave your current room |
| `f.roomskip` | `f.rs` | Everyone | Leave and immediately search for a new room |
| `f.roomstatus` | `f.rst` | Everyone | Show current room info |
| `f.roomkick` | `f.rk` | Everyone | Start a vote to kick a station (need 3+ servers) |

### ⚙️ Server Setup

| Command | Aliases | Permission | Description |
|---|---|---|---|
| `f.setup` | — | Manage Channels | Register this channel as the Fliphone channel |
| `f.check` | `f.setupcheck`, `f.doctor` | Manage Channels | Diagnose setup, permissions, webhook, and call state |
| `f.repair` | `f.fixsetup`, `f.fix` | Manage Channels | Repair the configured channel/webhook after `f.check` |
| `f.dbstatus` | `f.database`, `f.db` | Manage Channels | Show database health and safe row counts |
| `f.teardown` | `f.remove` | Manage Channels | Remove Fliphone from this server (clears all data) |
| `f.blocklist` | `f.blocked` | Manage Channels | List servers your server has blocked |
| `f.unblock` | — | Manage Channels | Unblock a server by its ID |
| `f.kick` | — | Manage Channels | Force-disconnect the active call |

### Restricted Tools

Owner and trusted moderator commands are intentionally hidden from normal help.
Use `f.sudohelp` to view moderation panels, GIF review tools, global bans,
censor controls, and guild audit tools.

---

## How Calls Work

```
Server A  ──f.call──►  Queue  ◄──f.call──  Server B
                         │
                    [matched!]
                         │
Server A  ◄──relay──   Bot  ──relay──►  Server B
```

1. Admin runs `f.setup` in the chosen channel — registers it and creates a webhook.
2. A user runs `f.call` — bot looks for a match from a *different* server (respecting block lists).
   - **Match found** → both channels get a *Connected!* message and relay begins.
   - **No match** → channel enters the queue (10-minute timeout, then auto-cancels).
3. Every non-command message sent in a connected channel is relayed to the partner.
4. Either side runs `f.hangup` → call ends, stats logged, both channels notified.

### Relay priority

1. **Webhook relay** (if bot has Manage Webhooks) — message appears with the sender's username and avatar, no bot prefix clutter.
2. **Fallback plain message** — if webhooks aren't available, the bot sends `**Name**\nmessage`.

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

- Run `f.room` to join. If an active room has space and you haven't been in it, you slot straight in.
- If no room is available, a new room is created and waits for others.
- A room becomes **active** once 2+ servers have joined.
- New servers can join active rooms up to the maximum size of 6.
- If fewer than 2 servers remain after someone leaves, the room closes automatically.

**Vote kick:** Any server can run `f.roomkick <Station>` to start a majority vote to remove a misbehaving station (requires 3+ servers in the room). The vote lasts 60 seconds.

---

## GIF Mode

Server admins can control how GIFs are handled in their server's calls:

| Mode | Behaviour |
|---|---|
| `enabled` | All GIFs from Tenor, Giphy, Klipy, and `.gif` links are relayed (default) |
| `limited` | Only Tenor, Giphy, and Klipy links are relayed — direct `.gif` URLs blocked |
| `disabled` | All GIFs are blocked — none sent or received |

Set with: `f.gifmode enabled` / `f.gifmode limited` / `f.gifmode disabled`

When one side has restrictions, both sides are notified at the start of the call.

---

## Configuration (`.env`)

```env
# Required
DISCORD_TOKEN=your_bot_token_here

# Optional
COMMAND_PREFIX=f.              # Default: f.
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

The migration script creates a clean SQLite snapshot first, copies all 14 tables into PostgreSQL, resets identity sequences, and prints row counts only. It will refuse to copy into a non-empty PostgreSQL database unless you pass `--clear-postgres`.

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
| `guild_config` | One row per server that has run `f.setup` |
| `queue` | Channels currently waiting for a match |
| `connections` | Active, live 1:1 calls |
| `call_history` | Completed calls (used for stats) |
| `blocked_guilds` | Server-level block list |
| `banned_users` | Bot-wide user ban list |
| `custom_words` | Custom censor words added via `f.censor` |
| `gif_reports` | GIF URLs reported by users, pending review |
| `gif_url_list` | Blacklisted and whitelisted GIF URLs |
| `gif_mode_settings` | Per-server GIF mode (enabled/limited/disabled) |
| `rooms` | Group room sessions |
| `room_members` | Servers currently in a group room |
| `notify_subscribers` | Users opted into queue notifications |
| `call_reports` | User-submitted call reports awaiting moderation |

---

## Privacy & Terms

- [Privacy Policy](https://YOUR_DOMAIN/privacy.html)
- [Terms of Service](https://YOUR_DOMAIN/tos.html)

Fliphone **does not store message content.** Only the metadata listed in the Privacy Policy is retained.

---

## Support

- **Support server:** [discord.gg/t3KHGqPuEP](https://discord.gg/t3KHGqPuEP)
- **Vote:** [top.gg/bot/1489342959974486158/vote](https://top.gg/bot/1489342959974486158/vote)

---
