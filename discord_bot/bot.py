import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import discord
from discord import Embed
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')
GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', 0))
ANNOUNCEMENT_CHANNEL_ID = int(os.getenv('DISCORD_ANNOUNCEMENT_CHANNEL_ID', 0))
JAVA_IP = os.getenv('JAVA_IP', '')
BEDROCK_IP = os.getenv('BEDROCK_IP', '')
BEDROCK_PORT = os.getenv('BEDROCK_PORT', '')
DATABASE_URL = os.getenv('DATABASE_URL', '')
NEW_MEMBER_DAYS = int(os.getenv('NEW_MEMBER_DAYS', '30'))
OG_MEMBER_DAYS = int(os.getenv('OG_MEMBER_DAYS', '365'))

ROLE_IDS = {
    'default': int(os.getenv('ROLE_ID_DEFAULT', 0)),
    'new-member': int(os.getenv('ROLE_ID_NEW_MEMBER', 0)),
    'og-member': int(os.getenv('ROLE_ID_OG_MEMBER', 0)),
    'iron': int(os.getenv('ROLE_ID_IRON', 0)),
    'gold': int(os.getenv('ROLE_ID_GOLD', 0)),
    'diamond': int(os.getenv('ROLE_ID_DIAMOND', 0)),
    'nether': int(os.getenv('ROLE_ID_NETHER', 0)),
    'god': int(os.getenv('ROLE_ID_GOD', 0)),
}


class GamePodraBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents)
        self.db: Optional[asyncpg.Pool] = None
        self.guild: Optional[discord.Guild] = None
        self.announcement_channel: Optional[discord.TextChannel] = None

    async def setup_hook(self):
        self.db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
        await self._init_db()
        self.check_purchases.start()
        self.auto_assign_roles.start()
        await self.tree.sync()

    async def _init_db(self):
        async with self.db.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS rank_assignments (
                    id SERIAL PRIMARY KEY,
                    payment_order_id VARCHAR(100),
                    discord_user_id BIGINT NOT NULL,
                    minecraft_name VARCHAR(100),
                    rank VARCHAR(50) NOT NULL,
                    action VARCHAR(10) NOT NULL,
                    assigned_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    removed_at TIMESTAMP
                )
            """)
        logger.info("Bot database table rank_assignments initialized")

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        self.guild = self.get_guild(GUILD_ID)
        if self.guild:
            self.announcement_channel = self.guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
            logger.info("Guild: %s, Announcement channel: %s", self.guild.name, self.announcement_channel)

    # ── Background: check new purchases every 5 min ──────────────

    @tasks.loop(minutes=5)
    async def check_purchases(self):
        if not self.db or not self.announcement_channel:
            return

        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.order_id, p.minecraft_name, p.rank, p.discord_tag
                FROM payments p
                WHERE p.status = 'completed'
                AND p.verified_at IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM rank_assignments ra
                    WHERE ra.payment_order_id = p.order_id AND ra.action = 'given'
                )
            """)

            for row in rows:
                order_id = row['order_id']
                mc_name = row['minecraft_name']
                rank_name = row['rank']
                user_id = int(row['discord_tag'])

                await conn.execute(
                    "INSERT INTO rank_assignments (payment_order_id, discord_user_id, minecraft_name, rank, action) "
                    "VALUES ($1, $2, $3, $4, 'given')",
                    order_id, user_id, mc_name, rank_name
                )

                announcement = f"🎉 **{mc_name}** just purchased **{rank_name}** rank!"
                await self.announcement_channel.send(announcement)

                if user_id:
                    try:
                        user = await self.fetch_user(user_id)
                        if user:
                            await user.send(
                                f"Thank you for purchasing **{rank_name}** rank "
                                f"for Minecraft user **{mc_name}**! 🎉"
                            )
                    except discord.HTTPException:
                        logger.warning("Could not DM user %s", user_id)

    # ── Background: auto-assign roles every 10 min ──────────────

    @tasks.loop(minutes=10)
    async def auto_assign_roles(self):
        if not self.db or not self.guild:
            return

        rank_role_map = {
            'iron': ROLE_IDS['iron'],
            'gold': ROLE_IDS['gold'],
            'diamond': ROLE_IDS['diamond'],
            'netherite': ROLE_IDS['nether'],
            'god': ROLE_IDS['god'],
        }
        all_role_ids = {v for v in ROLE_IDS.values() if v}

        for member in self.guild.members:
            account_days = (datetime.now(timezone.utc) - member.created_at).days

            sub = None
            async with self.db.acquire() as conn:
                sub = await conn.fetchrow("""
                    SELECT rank_key FROM payments
                    WHERE discord_tag = $1
                    AND status = 'completed'
                    AND NOT is_expired
                    AND (subscription_end IS NULL OR subscription_end > NOW())
                    ORDER BY created_at DESC
                    LIMIT 1
                """, str(member.id))

            target_role_ids = {ROLE_IDS['default']} if ROLE_IDS['default'] else set()

            if account_days < NEW_MEMBER_DAYS:
                target_role_ids.add(ROLE_IDS['new-member'])
            else:
                target_role_ids.discard(ROLE_IDS['new-member'])

            if account_days >= OG_MEMBER_DAYS:
                target_role_ids.add(ROLE_IDS['og-member'])

            if sub and sub['rank_key'] in rank_role_map:
                target_role_ids.add(rank_role_map[sub['rank_key']])

            current_role_ids = {r.id for r in member.roles if r.id in all_role_ids}
            to_add = target_role_ids - current_role_ids
            to_remove = current_role_ids - target_role_ids

            for rid in to_add:
                if rid:
                    try:
                        await member.add_roles(discord.Object(id=rid), reason="Auto-assign by GamePodra bot")
                        await discord.utils.sleep_until(datetime.now(timezone.utc) + timedelta(seconds=0.5))
                    except discord.HTTPException as e:
                        logger.error("Failed to add role %s to %s: %s", rid, member.id, e)

            for rid in to_remove:
                if rid:
                    try:
                        await member.remove_roles(discord.Object(id=rid), reason="Auto-remove by GamePodra bot")
                        await discord.utils.sleep_until(datetime.now(timezone.utc) + timedelta(seconds=0.5))
                    except discord.HTTPException as e:
                        logger.error("Failed to remove role %s from %s: %s", rid, member.id, e)

            await discord.utils.sleep_until(datetime.now(timezone.utc) + timedelta(seconds=1.5))


# ── Slash Commands ────────────────────────────────────────────

bot = GamePodraBot()


@bot.tree.command(name="ip", description="Send server connection info to the announcement channel")
async def cmd_ip(interaction: discord.Interaction):
    if not JAVA_IP:
        await interaction.response.send_message("Server IP is not configured.", ephemeral=True)
        return

    msg = (
        f"**☕ Java Edition**\n"
        f"`{JAVA_IP}`\n\n"
        f"**🧊 Bedrock Edition**\n"
        f"IP: `{BEDROCK_IP or JAVA_IP}`\n"
        f"Port: `{BEDROCK_PORT or '19132'}`"
    )

    if bot.announcement_channel:
        await bot.announcement_channel.send(msg)
        await interaction.response.send_message("Server info sent to the announcement channel!", ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="status", description="Check your subscription status")
async def cmd_status(interaction: discord.Interaction):
    if not bot.db:
        await interaction.response.send_message("Database is not connected.", ephemeral=True)
        return

    async with bot.db.acquire() as conn:
        sub = await conn.fetchrow("""
            SELECT minecraft_name, rank, billing, is_expired, subscription_end
            FROM payments
            WHERE discord_tag = $1 AND status = 'completed'
            ORDER BY created_at DESC LIMIT 1
        """, str(interaction.user.id))

        if not sub:
            await interaction.response.send_message(
                "No purchase found for your Discord account.", ephemeral=True
            )
            return

        mc_name = sub['minecraft_name']
        expired = sub['is_expired'] or (sub['subscription_end'] and sub['subscription_end'] < datetime.now(timezone.utc))
        embed = Embed(
            title="Your Subscription",
            color=discord.Color.green() if not expired else discord.Color.red()
        )
        embed.add_field(name="Minecraft", value=mc_name, inline=False)
        embed.add_field(name="Rank", value=sub['rank'], inline=True)
        embed.add_field(name="Type", value=sub['billing'].title(), inline=True)
        embed.add_field(name="Status", value="Expired" if expired else "Active", inline=True)
        if sub['subscription_end']:
            embed.add_field(name="Expires", value=sub['subscription_end'].strftime('%d %b %Y'), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Run ──────────────────────────────────────────────────────

_REQUIRED_ENV = [
    ('DISCORD_BOT_TOKEN',              'Discord bot token'),
    ('DISCORD_GUILD_ID',               'Discord server (guild) ID'),
    ('DISCORD_ANNOUNCEMENT_CHANNEL_ID', 'Announcement channel ID'),
    ('DATABASE_URL',                   'PostgreSQL connection string'),
]

if __name__ == "__main__":
    missing = [label for key, label in _REQUIRED_ENV if not os.getenv(key)]
    if missing:
        logger.error("Missing required environment variables:\n  - " + "\n  - ".join(missing))
        raise SystemExit(1)

    if not JAVA_IP:
        logger.warning("JAVA_IP not set — /ip command will not work")

    if all(rid == 0 for rid in ROLE_IDS.values()):
        logger.warning("No role IDs configured — role assignment will be skipped")

    bot.run(TOKEN)
