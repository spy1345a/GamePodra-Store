"""
db.py — thin async-friendly wrapper around the payments DB.
Uses SQLAlchemy Core (synchronous) via asyncio.to_thread so we
never block the event loop.  Adjust DATABASE_URL in .env to match
your setup (e.g. mysql+pymysql:// or sqlite:///payments.db for dev).
"""

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, text

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = os.environ["DATABASE_URL"]
        _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


# ---------------------------------------------------------------------------
# Query helpers (all run in a thread via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _fetch_active_rank(discord_tag: str) -> Optional[dict]:
    """
    Return the most-recently-verified completed row for this Discord user
    that is NOT expired, or None.
    discord_tag stores the Discord snowflake (user ID as string).
    """
    sql = text("""
        SELECT
            rank, rank_key, billing, is_lifetime,
            is_expired, subscription_start, subscription_end,
            verified_at, order_id
        FROM payments
        WHERE discord_tag = :tag
          AND status      = 'completed'
          AND is_expired  = FALSE
        ORDER BY verified_at DESC
        LIMIT 1
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"tag": discord_tag}).mappings().first()
        return dict(row) if row else None


def _fetch_expired_uncleaned(discord_tag: str) -> list[dict]:
    """Rows that are expired but bot hasn't stripped the role yet."""
    sql = text("""
        SELECT rank, rank_key, order_id
        FROM payments
        WHERE discord_tag = :tag
          AND status      = 'completed'
          AND is_expired  = TRUE
          AND billing     = 'monthly'
        ORDER BY subscription_end DESC
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"tag": discord_tag}).mappings().all()
        return [dict(r) for r in rows]


def _fetch_all_expiring() -> list[dict]:
    """Monthly subs whose subscription_end has passed but is_expired is still FALSE."""
    now = datetime.now(timezone.utc)
    sql = text("""
        SELECT discord_tag, rank, rank_key, subscription_end, order_id
        FROM payments
        WHERE status      = 'completed'
          AND billing     = 'monthly'
          AND is_expired  = FALSE
          AND subscription_end IS NOT NULL
          AND subscription_end <= :now
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"now": now}).mappings().all()
        return [dict(r) for r in rows]


def _mark_expired(order_id: str):
    sql = text("""
        UPDATE payments
        SET is_expired = TRUE,
            status     = 'expired'
        WHERE order_id = :oid
    """)
    with get_engine().connect() as conn:
        conn.execute(sql, {"oid": order_id})
        conn.commit()


def _fetch_recent_completed(since_seconds: int = 120) -> list[dict]:
    """Payments completed in the last `since_seconds` seconds (for announcement polling)."""
    sql = text("""
        SELECT
            discord_tag, minecraft_name, rank, rank_key,
            billing, amount, currency, verified_at, order_id
        FROM payments
        WHERE status     = 'completed'
          AND verified_at >= NOW() - INTERVAL :secs SECOND
        ORDER BY verified_at DESC
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"secs": since_seconds}).mappings().all()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

import asyncio


async def get_active_rank(discord_tag: str) -> Optional[dict]:
    return await asyncio.to_thread(_fetch_active_rank, discord_tag)


async def get_expired_uncleaned(discord_tag: str) -> list[dict]:
    return await asyncio.to_thread(_fetch_expired_uncleaned, discord_tag)


async def get_all_expiring() -> list[dict]:
    return await asyncio.to_thread(_fetch_all_expiring)


async def mark_expired(order_id: str):
    await asyncio.to_thread(_mark_expired, order_id)


async def get_recent_completed(since_seconds: int = 120) -> list[dict]:
    return await asyncio.to_thread(_fetch_recent_completed, since_seconds)