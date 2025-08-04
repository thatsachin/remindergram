# models.py
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.sql import func
from database import Base

class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    text = Column(String, nullable=False)
    next_trigger_utc = Column(DateTime, nullable=False)
    is_recurring = Column(Boolean, default=False)
    recur_rule = Column(String)          # “daily”, “RRULE:FREQ=WEEKLY;…”
    status = Column(String, default="pending")   # pending, completed, deleted
    created_at = Column(DateTime, server_default=func.now())

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    reminder_id = Column(Integer, ForeignKey("reminders.id"))
    fired_at_utc = Column(DateTime, default=func.now())

class Response(Base):
    __tablename__ = "responses"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    user_id = Column(Integer)
    response = Column(String)            # did / didnt / snoozed
    responded_at_utc = Column(DateTime, default=func.now())
