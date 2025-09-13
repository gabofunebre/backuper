from sqlalchemy import Column, Integer, String

from .database import Base


class App(Base):
    __tablename__ = "apps"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    url = Column(String, nullable=False)
    token = Column(String, nullable=False)
    # Cron-style schedule for backup tasks
    schedule = Column(String, nullable=True)
    # Google Drive folder where backups will be stored
    drive_folder_id = Column(String, nullable=True)
    # Retention policy in days/weeks
    retention_daily = Column(Integer, nullable=True)
    retention_weekly = Column(Integer, nullable=True)
