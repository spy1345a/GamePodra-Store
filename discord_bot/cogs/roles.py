"""
cogs/roles.py — all role-assignment and membership-role logic.

Role hierarchy (rank roles):
    iron < gold < diamond < nether < god

Membership roles (time-based, no purchase needed):
    NEW_MEMBER  — after NEW_MEMBER_DAYS days in guild
    OG_MEMBER   — after OG_MEMBER_DAYS days in guild
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import db

log = logging.getLogger("roles")

# ---------------------------------------------------------------------------
# Rank key → env var mapping
# ---------------------------------------------------------------------------

RANK_ROLE_ENV: dict[str, str] = {
    "iron":    "ROLE_ID_IRON",
    "gold":    "ROLE_ID_GOLD",
    "diamond": "ROLE_ID_DIAMOND",
    "nether":  "ROLE_ID_NETHER",
    "god":     "ROLE_ID_GOD",
}

ALL_RANK_KEYS = list(RANK_ROLE_ENV.keys())


def _role_id(env_key: str) -> Optional[int]:
    val = os.environ.get(env_key, "0").strip()
    return int(val) if val and val != "0" else None


def get_rank_role(guild: discord.Guild, rank_key: str) -> Optional[discord.Role]:
    env = RANK_ROLE_ENV.get(rank_key.lower())
    if not env:
        return None
    rid = _role_id(env)
    return guild.get_role(rid) if rid else None


def get_all_rank_roles(guild: discord.Guild) -> list[discord.Role]:
    roles = []
    for env in RANK_ROLE_ENV.values():
        rid = _role_id(env)
        if rid:
            r = guild.get_role(rid)
            if r:
                roles.append(r)
    return roles


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

async def assign_membership_role(bot: commands.Bot, member: discord.Member):
    """
    Assign NEW_MEMBER or OG_MEMBER based on how long ago they joined.
    Safe to call repeatedly — only adds, never removes the other tier.
    """
    joined = member.joined_at
    if not joined:
        return

    days_in_server = (datetime.now(timezone.utc) - joined).days
    new_days = int(os.environ.get("NEW_MEMBER_DAYS", 1))
    og_days  = int(os.environ.get("OG_MEMBER_DAYS", 365))

    new_rid = _role_id("ROLE_ID_NEW_MEMBER")
    og_rid  = _role_id("ROLE_ID_OG_MEMBER")

    try:
        if og_rid and days_in_server >= og_days:
            og_role = member.guild.get_role(og_rid)
            if og_role and og_role not in member.roles:
                await member.add_roles(og_role, reason="OG member threshold reached")
                log.info("Assigned OG_MEMBER to %s", member)

        elif new_rid and days_in_server >= new_days:
            new_role = member.guild.get_role(new_rid)
            if new_role and new_role not in member.roles:
                await member.add_roles(new_role, reason="New member threshold reached")
                log.info("Assigned NEW_MEMBER to %s", member)

    except discord.Forbidden:
        log.warning("Missing permissions to assign membership role to %s", member)
    except discord.HTTPException as e:
        log.error("HTTP error assigning membership role to %s: %s", member, e)


async def sync_member_rank(
    bot: commands.Bot,
    discord_user_id: int,
    discord_tag: str,
    *,
    announce_channel: Optional[discord.TextChannel] = None,
    new_purchase: bool = False,
) -> Optional[str]:
    """
    Pull the active rank from the DB and reconcile Discord roles.
    Returns the rank_key that was applied, or None.

    - Strips all other rank roles before applying the correct one.
    - DMs the user on grant or expiry.
    - Optionally posts to announce_channel on new_purchase.
    """
    guild_id = int(os.environ["DISCORD_GUILD_ID"])
    guild = bot.get_guild(guild_id)
    if not guild:
        log.error("Guild %s not found", guild_id)
        return None

    try:
        member = guild.get_member(discord_user_id) or await guild.fetch_member(discord_user_id)
    except discord.NotFound:
        log.warning("Member %s not in guild", discord_user_id)
        return None
    except discord.HTTPException as e:
        log.error("Failed to fetch member %s: %s", discord_user_id, e)
        return None

    active = await db.get_active_rank(discord_tag)
    all_rank_roles = get_all_rank_roles(guild)

    if active:
        target_role = get_rank_role(guild, active["rank_key"])
        if not target_role:
            log.warning("Role for rank_key '%s' not configured", active["rank_key"])
            return None

        # Strip all other rank roles
        to_remove = [r for r in all_rank_roles if r != target_role and r in member.roles]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank sync — removing stale ranks")

        # Add the correct rank role
        if target_role not in member.roles:
            await member.add_roles(target_role, reason=f"Rank sync — {active['rank_key']}")
            log.info("Granted %s rank '%s' to %s", active['billing'], active['rank_key'], member)

            # DM the user
            await _dm_rank_granted(member, active, new_purchase=new_purchase)

            # Announcement
            if new_purchase and announce_channel:
                await _announce_purchase(announce_channel, member, active)

        return active["rank_key"]

    else:
        # No active rank — strip all rank roles
        to_remove = [r for r in all_rank_roles if r in member.roles]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank sync — no active rank in DB")
            log.info("Stripped all rank roles from %s (no active rank)", member)
            await _dm_rank_expired(member)

        return None


async def expire_member_rank(
    bot: commands.Bot,
    discord_tag: str,
    rank_key: str,
    order_id: str,
):
    """Mark the DB row expired and strip the Discord role."""
    await db.mark_expired(order_id)
    log.info("Marked order %s as expired", order_id)

    guild_id = int(os.environ["DISCORD_GUILD_ID"])
    guild = bot.get_guild(guild_id)
    if not guild:
        return

    # discord_tag stores the snowflake ID as a string
    try:
        member = guild.get_member(int(discord_tag)) or await guild.fetch_member(int(discord_tag))
    except (discord.NotFound, ValueError):
        log.warning("Could not find member for discord_tag '%s' during expiry", discord_tag)
        return

    role = get_rank_role(guild, rank_key)
    if role and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"Monthly rank expired: {rank_key}")
            log.info("Removed expired rank '%s' from %s", rank_key, member)
        except discord.Forbidden:
            log.warning("No permission to remove rank role from %s", member)

    await _dm_rank_expired(member, rank_key=rank_key)


# ---------------------------------------------------------------------------
# DM & announcement helpers
# ---------------------------------------------------------------------------

RANK_EMOJI = {
    "iron":    "⚔️",
    "gold":    "🥇",
    "diamond": "💎",
    "nether":  "🔥",
    "god":     "👑",
}

RANK_COLOR = {
    "iron":    discord.Color.light_grey(),
    "gold":    discord.Color.gold(),
    "diamond": discord.Color.blue(),
    "nether":  discord.Color.dark_red(),
    "god":     discord.Color.purple(),
}


async def _dm_rank_granted(member: discord.Member, row: dict, *, new_purchase: bool):
    emoji = RANK_EMOJI.get(row["rank_key"], "🎖️")
    color = RANK_COLOR.get(row["rank_key"], discord.Color.green())
    billing_str = "Lifetime" if row["is_lifetime"] else "Monthly"

    if row["subscription_end"]:
        end_ts = int(row["subscription_end"].replace(tzinfo=timezone.utc).timestamp())
        expires_str = f"<t:{end_ts}:R>"
    else:
        expires_str = "Never (lifetime)"

    embed = discord.Embed(
        title=f"{emoji} {row['rank'].upper()} rank activated!",
        description=(
            f"Your **{row['rank'].upper()}** rank has been applied on the GamePodra server.\n\n"
            f"**Type:** {billing_str}\n"
            f"**Expires:** {expires_str}"
        ),
        color=color,
    )
    embed.set_footer(text="GamePodra • Thank you for your support!")
    try:
        await member.send(embed=embed)
    except discord.Forbidden:
        log.debug("DMs closed for %s — skipping rank grant DM", member)


async def _dm_rank_expired(member: discord.Member, *, rank_key: str = "your"):
    embed = discord.Embed(
        title="⏰ Rank expired",
        description=(
            f"Your **{rank_key.upper()}** rank on GamePodra has expired.\n\n"
            "Renew anytime at our store to get it back instantly!"
        ),
        color=discord.Color.red(),
    )
    embed.set_footer(text="GamePodra • We'd love to have you back!")
    try:
        await member.send(embed=embed)
    except discord.Forbidden:
        log.debug("DMs closed for %s — skipping expiry DM", member)


async def _announce_purchase(
    channel: discord.TextChannel,
    member: discord.Member,
    row: dict,
):
    emoji = RANK_EMOJI.get(row["rank_key"], "🎖️")
    color = RANK_COLOR.get(row["rank_key"], discord.Color.green())
    billing_str = "lifetime" if row["is_lifetime"] else "monthly"
    amount_inr  = row["amount"] / 100

    embed = discord.Embed(
        title=f"🎉 New rank purchase!",
        description=(
            f"{member.mention} just grabbed the **{row['rank'].upper()}** {emoji} rank "
            f"({billing_str}) for ₹{amount_inr:.0f}!\n\n"
            "Support the server — check out our store!"
        ),
        color=color,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="GamePodra Store")
    await channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class RolesCog(commands.Cog, name="Roles"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))