from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Enum, Text, Index, Boolean
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Payment(Base):
    __tablename__ = 'payments'

    id               = Column(Integer, primary_key=True, autoincrement=True)
    payment_id       = Column(String(100), nullable=True,  unique=True)
    order_id         = Column(String(100), nullable=False, unique=True)
    minecraft_name   = Column(String(100), nullable=False)
    discord_tag      = Column(String(100), nullable=False)
    email            = Column(String(255), nullable=False)
    rank             = Column(String(50),  nullable=False)
    rank_key         = Column(String(50),  nullable=False)
    billing          = Column(Enum('monthly', 'lifetime', name='billing_type'), nullable=False)
    amount           = Column(Integer,     nullable=False)
    currency         = Column(String(10),  nullable=False, default='INR')
    status           = Column(
                        Enum('pending', 'completed', 'failed', 'expired', name='payment_status'),
                        nullable=False,
                        default='pending',
                     )
    is_lifetime      = Column(Boolean, nullable=False, default=False)
    is_expired       = Column(Boolean, nullable=False, default=False)
    subscription_start = Column(DateTime, nullable=True)
    subscription_end   = Column(DateTime, nullable=True)
    last_checked_at  = Column(DateTime, nullable=True)
    original_order_id = Column(String(100), nullable=True)
    upgrade_from_monthly = Column(Boolean, nullable=False, default=False)
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)
    verified_at      = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_payments_order_id',   'order_id'),
        Index('ix_payments_payment_id', 'payment_id'),
        Index('ix_payments_email',      'email'),
        Index('ix_payments_status',     'status'),
        Index('ix_payments_minecraft_name', 'minecraft_name'),
        Index('ix_payments_discord_tag',    'discord_tag'),
        Index('ix_payments_is_expired',    'is_expired'),
        Index('ix_payments_is_lifetime',   'is_lifetime'),
        Index('ix_payments_rank_key',      'rank_key'),
    )

    def __repr__(self):
        return f'<Payment order_id={self.order_id} status={self.status}>'


class WebhookEvent(Base):
    """
    Stores every raw Razorpay webhook payload.
    Lets you replay events and audit exactly what Razorpay sent.
    """
    __tablename__ = 'webhook_events'

    id           = Column(Integer, primary_key=True, autoincrement=True)
    event_id     = Column(String(100), nullable=True,  unique=True)  # Razorpay X-Razorpay-Event-Id
    event_type   = Column(String(100), nullable=False)               # e.g. payment.captured
    payload      = Column(Text,        nullable=False)               # raw JSON string
    processed    = Column(String(10),  nullable=False, default='no') # yes / no / error
    received_at  = Column(DateTime,    nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_webhook_event_id',   'event_id'),
        Index('ix_webhook_event_type', 'event_type'),
        Index('ix_webhook_processed',  'processed'),
    )

    def __repr__(self):
        return f'<WebhookEvent {self.event_type} processed={self.processed}>'