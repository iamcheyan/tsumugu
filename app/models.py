from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean
from sqlalchemy.sql import func
from .database import Base

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
    download_id = Column(Integer, nullable=True)  # Link to download_history if file was downloaded


class SyncFolder(Base):
    __tablename__ = "sync_folders"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String(1024), unique=True, index=True)  # Relative to NAS root, e.g. "/Music"
    name = Column(String(255))  # Display name
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())