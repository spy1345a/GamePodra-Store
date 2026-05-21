"""
cogs/tasks.py — background loops that run independently of user actions.

Task 1: expiry_check   — every 5 min, finds monthly subs past their end date,
                          marks them expired in DB, strips Discord role, DMs user.

Task 2: new_purchase   — every 5 min, polls for recently-verified payments,
                          assigns role, announces to channel.

Task 3: membership_check — every 12 h, sweeps all guild members and assigns
                            NEW_MEMBER / OG_MEMBER roles based on join date.
"""

import logging
import os

import discord
from discord.ext import commands, tasks

import db
from cogs.roles import (
    assign_membership_role,
    expire_member_rank,
    sync_member_rank,
)

log = logging.getLogger("tasks")

# track which order_ids we've already announced this session
_announced: set[str] = set()


class TasksCog(commands.Cog, name="Tasks"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expiry_check.start()
        self.new_purchase_poll.start()
        self.membership_check.start()

    def cog_unload(self):
        self.expiry_check.cancel()
        self.new_purchase_poll.cancel()
        self.membership_check.cancel()

    # ------------------------------------------------------------------
    # Task 1: expiry check — every 5 minutes
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def expiry_check(self):
        log.info("[expiry_check] Running expiry sweep…")
        try:
            expiring = await db.get_all_expiring()
        except Exception as e:
            log.error("[expiry_check] DB error: %s", e)
            return

        if not expiring:
            log.info("[expiry_check] No expired subs found.")
            return

        log.info("[expiry_check] Found %d expired subscription(s).", len(expiring))
        for row in expiring:
            try:
                await expire_member_rank(
                    self.bot,
                    discord_tag=row["discord_tag"],
                    rank_key=row["rank_key"],
                    order_id=row["order_id"],
                )
            except Exception as e:
                log.error(
                    "[expiry_check] Failed to expire order %s: %s",
                    row["order_id"], e,
                )

    @expiry_check.before_loop
    async def before_expiry(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Task 2: new-purchase poll — every 5 minutes
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def new_purchase_poll(self):
        log.debug("[new_purchase_poll] Checking for recent purchases…")
        try:
            # look back 8 minutes — covers one full missed cycle with buffer
            rows = await db.get_recent_completed(since_seconds=480)
        except Exception as e:
            log.error("[new_purchase_poll] DB error: %s", e)
            return

        if not rows:
            return

        announce_ch = self._get_announce_channel()

        for row in rows:
            order_id = row["order_id"]
            if order_id in _announced:
                continue
            _announced.add(order_id)

            discord_tag = row["discord_tag"]
            try:
                discord_id = int(discord_tag)
            except (ValueError, TypeError):
                log.warning("[new_purchase_poll] Non-numeric discord_tag '%s', skipping", discord_tag)
                continue

            log.info(
                "[new_purchase_poll] New purchase: %s → %s (%s)",
                discord_tag, row["rank_key"], row["billing"],
            )
            try:
                await sync_member_rank(
                    self.bot,
                    discord_user_id=discord_id,
                    discord_tag=discord_tag,
                    announce_channel=announce_ch,
                    new_purchase=True,
                )
            except Exception as e:
                log.error("[new_purchase_poll] Failed to sync rank for %s: %s", discord_tag, e)

    @new_purchase_poll.before_loop
    async def before_purchase_poll(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Task 3: membership role sweep — every 12 hours
    # ------------------------------------------------------------------

    @tasks.loop(hours=12)
    async def membership_check(self):
        log.info("[membership_check] Sweeping guild members for membership roles…")
        guild_id = int(os.environ["DISCORD_GUILD_ID"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            log.error("[membership_check] Guild not found.")
            return

        count = 0
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            try:
                await assign_membership_role(self.bot, member)
                count += 1
            except Exception as e:
                log.error("[membership_check] Error processing %s: %s", member, e)

        log.info("[membership_check] Done — processed %d members.", count)

    @membership_check.before_loop
    async def before_membership_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _get_announce_channel(self) -> discord.TextChannel | None:
        ch_id = os.environ.get("DISCORD_ANNOUNCEMENT_CHANNEL_ID", "0").strip()
        if not ch_id or ch_id == "0":
            return None
        ch = self.bot.get_channel(int(ch_id))
        if isinstance(ch, discord.TextChannel):
            return ch
        return None


async def setup(bot: commands.Bot):
    await bot.add_cog(TasksCog(bot))