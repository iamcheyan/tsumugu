"""Database layer — reuses the existing SQLAlchemy models.

Provides a synchronous session factory and table-creation helper.
The models (Config, DownloadHistory, FileMetadata, SyncFolder) are
defined here so the whole TUI package is self-contained.
"""
from __future__ import annotations

import os
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func

DATABASE_URL = os.environ.get("TSUMUGU_DB", "sqlite:///./data.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Config(Base):
    __tablename__ = "config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, index=True)
    value = Column(Text)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class DownloadHistory(Base):
    __tablename__ = "download_history"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(512), nullable=False)
    title = Column(String(512))
    artist = Column(String(512))
    duration = Column(Integer)
    format = Column(String(50))  # mp3, m4a, flac
    file_path = Column(String(1024))
    status = Column(String(50))  # downloading, completed, failed, split
    split_mode = Column(String(50))  # chapter_info, silence_detection
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))


class FileMetadata(Base):
    __tablename__ = "file_metadata"

    id = Column(Integer, primary_key=True, index=True)
    file_path = Column(String(1024), unique=True, index=True)
    file_name = Column(String(512))
    file_size = Column(Integer)
    file_type = Column(String(50))  # folder, audio, video, generic
    tag = Column(String(50), nullable=True)  # music, podcast, or None
    modified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    download_id = Column(Integer, nullable=True)


class SyncFolder(Base):
    __tablename__ = "sync_folders"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String(1024), unique=True, index=True)
    name = Column(String(255))
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)
    # Migration: add 'tag' column to file_metadata if missing
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(
                text("PRAGMA table_info(file_metadata)")
            ).fetchall()]
            if "tag" not in cols:
                conn.execute(
                    text("ALTER TABLE file_metadata ADD COLUMN tag VARCHAR(50)")
                )
                conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[DB] Migration note: {exc}")


def get_session():
    """Context-managed session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()