"""
main.py – Phonebooth V2 entry point.

Usage (local)
-------------
    cp .env.example .env
    # Edit .env and add your DISCORD_TOKEN
    pip install -r requirements.txt
    python main.py

Usage (Docker / Cloud Run)
--------------------------
    Secrets are passed as env vars — do NOT bake .env into the image.
    When the PORT env var is set, a health-check HTTP server starts
    automatically on that port (required by Cloud Run).
"""

import asyncio
import os
import sys

import config
from bot import PhoneboothBot


async def main() -> None:
    if not config.TOKEN:
        print(
            "❌  DISCORD_TOKEN is not set.\n"
            "    Copy .env.example → .env and add your bot token."
        )
        sys.exit(1)

    bot = PhoneboothBot()

    # Start health-check HTTP server when running in a cloud container
    if os.environ.get("PORT"):
        from gcp.healthcheck import start_health_server
        asyncio.create_task(start_health_server())

    try:
        async with bot:
            await bot.start(config.TOKEN)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
