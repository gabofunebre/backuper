import datetime

from sqlalchemy import Column, DateTime, Integer, String

from .database import Base


class App(Base):
    __tablename__ = "apps"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    url = Column(String, nullable=False)
    token = Column(String, nullable=False)
    # Cron-style schedule for backup tasks
    schedule = Column(String, nullable=True)
    # Google Drive folder ID where backups will be stored
    drive_folder_id = Column(String, nullable=True)
    # Optional rclone remote override
    rclone_remote = Column(String, nullable=True)
    # Number of backups to retain
    retention = Column(Integer, nullable=True)


class RcloneRemote(Base):
    __tablename__ = "rclone_remotes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    type = Column(String, nullable=True)
    route = Column(String, nullable=True)
    share_url = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=True, default=datetime.datetime.utcnow)
