import asyncio
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

# .env lives at project root (one level above discord_bot/)
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


class GamePodraBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True  # needed to read message text for "ip" trigger
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.load_extension("cogs.roles")
        await self.load_extension("cogs.tasks")
        await self.load_extension("cogs.commands")
        guild = discord.Object(id=int(os.environ["DISCORD_GUILD_ID"]))
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", os.environ["DISCORD_GUILD_ID"])

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="GamePodra ranks 🎮",
            )
        )

    async def on_member_join(self, member: discord.Member):
        """Assign NEW_MEMBER role on join and check if they have an active rank."""
        from cogs.roles import assign_membership_role, sync_member_rank
        await assign_membership_role(self, member)
        await sync_member_rank(self, member.id, str(member.id))


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    bot = GamePodraBot()
    asyncio.run(bot.start(token))


if __name__ == "__main__":
    main()