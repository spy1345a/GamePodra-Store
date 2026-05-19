from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Enum
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Payment(Base):
    __tablename__ = 'payments'

    id          = Column(Integer, primary_key=True, autoincrement=True)
    payment_id  = Column(String(100), nullable=True, unique=True)   # null until user completes payment
    order_id    = Column(String(100), nullable=False, unique=True)
    minecraft_name = Column(String(100), nullable=False)
    discord_tag = Column(String(100), nullable=False)
    email       = Column(String(255), nullable=False)
    rank        = Column(String(50),  nullable=False)
    rank_key    = Column(String(50),  nullable=False)
    billing     = Column(Enum('monthly', 'lifetime', name='billing_type'), nullable=False)
    amount      = Column(Float, nullable=False)
    currency    = Column(String(10), nullable=False, default='INR')
    status      = Column(
                    Enum('pending', 'completed', 'failed', name='payment_status'),
                    nullable=False,
                    default='pending'
                  )
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f'<Payment order_id={self.order_id} status={self.status}>'