- from bot_helpers import send_due_reminder     # will be defined in bot.py
# scheduler.py
import datetime, zoneinfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Reminder, Event

scheduler = AsyncIOScheduler(timezone=zoneinfo.ZoneInfo("UTC"))

def scan_and_queue():
    db: Session = SessionLocal()
    now = datetime.datetime.utcnow()
    due = db.query(Reminder).filter(
        Reminder.next_trigger_utc <= now,
        Reminder.status == "pending"
    ).all()
    for r in due:
        scheduler.add_job(send_due_reminder, args=[r.id])
    db.close()

def start():
    scheduler.add_job(scan_and_queue, "interval", minutes=1, id="scanner")
    scheduler.start()
