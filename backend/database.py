from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

engine = create_engine("sqlite:///dhvanyartha.db")
Base = declarative_base()


class ScanRecord(Base):
    __tablename__ = "scans"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, nullable=True)
    source = Column(String, default="manual")  # "manual" (web app) or "extension" (browser)
    content_type = Column(String)       # text/image/audio/video/website
    input_summary = Column(String)      # short preview of what was scanned
    moderation_decision = Column(String)  # allow/flag/block
    reason = Column(String)
    confidence = Column(Float)
    full_result = Column(String)        # entire JSON result, as text
    created_at = Column(DateTime, default=datetime.utcnow)


class ParentSettings(Base):
    __tablename__ = "parent_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, unique=True, nullable=False)
    child_age = Column(Integer, default=18)
    blocked_categories = Column(String, default="[]")  # JSON-encoded list of strings
    guard_enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


Base.metadata.create_all(bind=engine)

SessionLocal = sessionmaker(bind=engine)