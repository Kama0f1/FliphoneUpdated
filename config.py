import os
from dotenv import load_dotenv

load_dotenv()


def _parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for item in raw.replace(" ", "").split(","):
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            continue
    return values

# ── Bot ───────────────────────────────────────────────────────────────────────
TOKEN: str          = os.getenv("DISCORD_TOKEN", "")
PREFIX: str         = os.getenv("COMMAND_PREFIX", "f.")
# Both lowercase and uppercase prefix work (f.call and F.call)
PREFIXES: list[str] = [PREFIX, PREFIX[0].upper() + PREFIX[1:]]
DB_PATH: str        = os.getenv("DB_PATH", "phonebooth.db")
DATABASE_URL: str   = os.getenv("DATABASE_URL", "")
DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
# Disable asyncpg's prepared statement cache by default for PgBouncer/pooler compatibility.
PG_STATEMENT_CACHE_SIZE: int = int(os.getenv("PG_STATEMENT_CACHE_SIZE", "0"))

# ── Queue ─────────────────────────────────────────────────────────────────────
QUEUE_TIMEOUT: int  = int(os.getenv("QUEUE_TIMEOUT", "10"))   # minutes

# ── Logging ───────────────────────────────────────────────────────────────────
# Channel ID where console logs are posted.
# Leave blank / 0 to disable forwarding to Discord.
LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "1511060418083291268"))

# Channel ID where GIF reports are sent for owner review.
# Leave blank / 0 to disable (reports still get stored in the DB).
REPORT_LOG_CHANNEL_ID: int = int(os.getenv("REPORT_LOG_CHANNEL_ID", "0"))

# ── Display ───────────────────────────────────────────────────────────────────
ANON_NAMES: list[str] = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot",
    "Golf",  "Hotel", "India",   "Juliet","Kilo", "Lima",
    "Mike",  "November","Oscar", "Papa",  "Quebec","Romeo",
    "Sierra","Tango","Uniform",  "Victor","Whiskey","X-ray",
    "Yankee","Zulu",
]

ANON_COLORS: list[int] = [
    0xFF6B6B, 0xFFE66D, 0x4ECDC4, 0x95E1D3, 0xF38181,
    0xFCE38A, 0x08D9D6, 0xFF2E63, 0xA29BFE, 0x6C5CE7,
    0xFD79A8, 0x00CEC9, 0xE17055, 0x74B9FF, 0x55EFC4,
]

COLOR_OK     = 0x57F287
COLOR_WAIT   = 0x5865F2
COLOR_WARN   = 0xFFA500
COLOR_ERR    = 0xFF6B6B

FOOTER       = "Fliphone • Cross-server chat roulette"

# ── Trusted moderators ───────────────────────────────────────────────────────
# These user IDs can run f.gifbl, f.gifwl, f.gifcheck, and f.reports
# even without being the bot owner.
TRUSTED_MOD_IDS: set[int] = {
    1129160384956342273,
    944227083117297674,
    1423635237505859689,
    *_parse_int_set(os.getenv("TRUSTED_MOD_IDS", "")),
}

# These guild IDs grant all members with administrator permission the same
# GIF moderation rights as TRUSTED_MOD_IDS.
TRUSTED_GUILD_IDS: set[int] = {
    1489521749115801651,  # admin server
}

# ── Top.gg ───────────────────────────────────────────────────────────────────
# Your top.gg API token — find it at top.gg/bot/<id>/edit under "Webhooks"
TOPGG_TOKEN: str = os.getenv("TOPGG_TOKEN", "")

# ── Notify ignore list ────────────────────────────────────────────────────────
# User IDs whose queue entries will NOT trigger notify DMs (e.g. you testing).
NOTIFY_IGNORE_IDS: set[int] = set()

# ── Invite ────────────────────────────────────────────────────────────────────
# Permissions: View Channel + Send Messages + Manage Webhooks +
#              Embed Links + Attach Files + Read Message History + Add Reactions
BOT_PERMISSIONS: int = 536988736

# Optional: number of gateway shards to use. Set to 0 or leave unset to let
# Discord/discord.py auto-determine the shard count.
SHARD_COUNT: int = int(os.getenv("SHARD_COUNT", "0"))
