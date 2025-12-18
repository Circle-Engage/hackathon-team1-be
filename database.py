"""Database for storing leads and chat history. Supports PostgreSQL (RDS) and SQLite."""

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

# DATABASE_URL format for PostgreSQL RDS:
# postgresql://username:password@hostname:5432/database_name
# For SQLite (local dev): sqlite:///./leads.db
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./leads.db")

# Configure engine based on database type
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # PostgreSQL or other databases
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Lead(Base):
    """Lead captured from chatbot interaction."""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String(100))
    last_name = Column(String(100))
    email = Column(String(255))
    phone = Column(String(20), nullable=True)
    zip_code = Column(String(10), nullable=True)
    state = Column(String(2), nullable=True)
    insurance_interest = Column(String(255))  # Medicare, ACA, Medicaid, etc.
    source = Column(String(100), default="Hackathon Chatbot")
    created_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text, nullable=True)  # Summary from chat


class ChatSession(Base):
    """Chat session for tracking conversations."""
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    lead_id = Column(Integer, nullable=True)  # Link to lead if captured
    messages = Column(JSON, default=list)  # Store conversation history
    insurance_topics = Column(JSON, default=list)  # Topics discussed
    ip_address = Column(String(45), nullable=True)  # Client IP for logging (supports IPv6)


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
