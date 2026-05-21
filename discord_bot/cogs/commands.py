"""
cogs/commands.py — slash commands + plain-message triggers for GamePodra bot.

/ip          — show Java + Bedrock connection info (slash)
ip           — same embed, fires when any message contains the word "ip"
               (case-insensitive, any channel, no prefix needed)
/rank        — check your own active rank
/syncranks   — admin: force a full rank sync for a member or all members
"""

import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands

import db
from cogs.roles import (
    RANK_COLOR,
    RANK_EMOJI,
    assign_membership_role,
    sync_member_rank,
)

log = logging.getLogger("commands")

# Words that trigger the IP embed when they appear standalone in a message.
# Matched case-insensitively as a whole word so "zip" or "trip" won't fire.
_IP_TRIGGERS = re.compile(r"\bip\b", re.IGNORECASE)


def _build_ip_embed() -> discord.Embed:
    java_ip      = os.environ.get("JAVA_IP", "Not set")
    bedrock_ip   = os.environ.get("BEDROCK_IP", "Not set")
    bedrock_port = os.environ.get("BEDROCK_PORT", "19132")

    embed = discord.Embed(title="🎮 GamePodra Server", color=discord.Color.green())
    embed.add_field(name="☕ Java Edition",   value=f"```{java_ip}```",                          inline=False)
    embed.add_field(name="📱 Bedrock Edition", value=f"```IP:   {bedrock_ip}\nPort: {bedrock_port}```", inline=False)
    embed.set_footer(text="GamePodra • See you in-game!")
    return embed


class CommandsCog(commands.Cog, name="Commands"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /ip — server connection info (slash command)
    # ------------------------------------------------------------------

    @app_commands.command(name="ip", description="Get the GamePodra server IP and connection details")
    async def ip_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_build_ip_embed())

    # ------------------------------------------------------------------
    # "ip" plain-message trigger — fires in any channel, no prefix needed
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots (including ourselves)
        if message.author.bot:
            return

        if _IP_TRIGGERS.search(message.content):
            try:
                await message.channel.send(embed=_build_ip_embed())
            except discord.Forbidden:
                log.warning("No permission to send in channel %s", message.channel)

    # ------------------------------------------------------------------
    # /rank — check your own rank
    # ------------------------------------------------------------------

    @app_commands.command(name="rank", description="Check your active rank on GamePodra")
    async def rank_command(self, interaction: discord.Interaction):
        discord_tag = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        row = await db.get_active_rank(discord_tag)

        if not row:
            embed = discord.Embed(
                title="No active rank",
                description=(
                    "You don't have an active rank on GamePodra.\n\n"
                    "Visit our store to grab one!"
                ),
                color=discord.Color.greyple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        emoji  = RANK_EMOJI.get(row["rank_key"], "🎖️")
        color  = RANK_COLOR.get(row["rank_key"], discord.Color.green())
        billing_str = "Lifetime" if row["is_lifetime"] else "Monthly"

        if row["subscription_end"]:
            from datetime import timezone
            end_ts = int(row["subscription_end"].replace(tzinfo=timezone.utc).timestamp())
            expires_str = f"<t:{end_ts}:R> (<t:{end_ts}:D>)"
        else:
            expires_str = "Never (lifetime)"

        embed = discord.Embed(
            title=f"{emoji} {row['rank'].upper()} rank",
            color=color,
        )
        embed.add_field(name="Type",    value=billing_str,   inline=True)
        embed.add_field(name="Expires", value=expires_str,   inline=True)
        if row.get("verified_at"):
            from datetime import timezone as tz
            ts = int(row["verified_at"].replace(tzinfo=tz.utc).timestamp())
            embed.add_field(name="Activated", value=f"<t:{ts}:D>", inline=True)
        embed.set_footer(text="GamePodra • Thank you for your support!")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /syncranks — admin command
    # ------------------------------------------------------------------

    @app_commands.command(name="syncranks", description="[Admin] Sync rank roles from the database")
    @app_commands.describe(member="Specific member to sync (leave blank to sync everyone)")
    @app_commands.checks.has_permissions(administrator=True)
    async def syncranks_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if member:
            # Single member sync
            discord_tag = str(member.id)
            rank_key = await sync_member_rank(
                self.bot,
                discord_user_id=member.id,
                discord_tag=discord_tag,
            )
            await assign_membership_role(self.bot, member)
            if rank_key:
                msg = f"✅ Synced **{member.display_name}** → **{rank_key.upper()}**"
            else:
                msg = f"✅ Synced **{member.display_name}** — no active rank (roles stripped if any)"
            await interaction.followup.send(msg, ephemeral=True)

        else:
            # Full guild sweep
            count = 0
            errors = 0
            async for m in guild.fetch_members(limit=None):
                if m.bot:
                    continue
                try:
                    await sync_member_rank(
                        self.bot,
                        discord_user_id=m.id,
                        discord_tag=str(m.id),
                    )
                    await assign_membership_role(self.bot, m)
                    count += 1
                except Exception as e:
                    log.error("syncranks: error on %s: %s", m, e)
                    errors += 1

            msg = f"✅ Synced **{count}** members."
            if errors:
                msg += f" ⚠️ {errors} error(s) — check logs."
            await interaction.followup.send(msg, ephemeral=True)

    @syncranks_command.error
    async def syncranks_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Administrator** permission to use this command.",
                ephemeral=True,
            )
        else:
            log.error("syncranks error: %s", error)
            await interaction.response.send_message("❌ An unexpected error occurred.", ephemeral=True)

    # ------------------------------------------------------------------
    # /id — DM your Discord user ID
    # ------------------------------------------------------------------

    @app_commands.command(name="id", description="Get your Discord user ID in DMs")
    async def id_command(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True)

        try:
            await interaction.user.send(
                f"🆔 Your Discord User ID is:\n```{interaction.user.id}```"
            )

            await interaction.followup.send(
                "✅ I sent your Discord ID in DMs.",
                ephemeral=True,
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't DM you. Please enable DMs from server members.",
                ephemeral=True,
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))