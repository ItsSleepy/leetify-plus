import discord
from discord.ext import commands
import asyncio
import logging
import sys
import os
from config import Config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("LeetifyBot")


class LeetifyBot(commands.Bot):
    """Leetify CS2 stats bot"""

    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(
            command_prefix="/",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        os.makedirs("data", exist_ok=True)
        await self.load_extension("cogs.leetify")
        logger.info("Loaded cogs.leetify")
        synced = await self.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s)")

    async def on_ready(self):
        logger.info(f"Online: {self.user}  |  Guilds: {len(self.guilds)}")
        await self.change_presence(activity=discord.Game(name="CS2 Stats  /leetify"))


async def main():
    if not Config.DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set in .env")
        return
    async with LeetifyBot() as bot:
        await bot.start(Config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
