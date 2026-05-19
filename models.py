from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Payment(Base):
    __tablename__ = 'payments'
    
    id = Column(Integer, primary_key=True)
    payment_id = Column(String(100), unique=True, nullable=False)
    order_id = Column(String(100), nullable=False)
    minecraft_name = Column(String(100), nullable=False)
    discord_tag = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    rank = Column(String(50), nullable=False)
    rank_key = Column(String(20), nullable=False)
    billing = Column(String(20), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default='INR')
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)