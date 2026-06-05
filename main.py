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

from aiohttp import web

import config
from bot import PhoneboothBot


async def start_health_server() -> None:
    """Serve a tiny health endpoint for container hosts such as Railway."""
    port = int(os.getenv("PORT", "8080"))

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    while True:
        await asyncio.sleep(3600)


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
        asyncio.create_task(start_health_server())

    try:
        async with bot:
            await bot.start(config.TOKEN)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
