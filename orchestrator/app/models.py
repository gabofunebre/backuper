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
