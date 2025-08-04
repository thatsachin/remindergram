import os, datetime, asyncio, zoneinfo
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

from database import init_db, SessionLocal
from models import Reminder, Event, Response
from gpt_parser import parse
from scheduler import start as start_scheduler

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = {5478021276, 98765432}            # two user IDs

# ---------- helpers ----------
def user_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USERS:
            await update.message.reply_text("⛔️ Unauthorized user")
            return
        return await func(update, context)
    return wrapped

async def send_due_reminder(reminder_id: int):
    from sqlalchemy.orm import Session
    db: Session = SessionLocal()
    r = db.get(Reminder, reminder_id)
    if not r or r.status != "pending":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Did ✅", callback_data=f"did:{r.id}"),
         InlineKeyboardButton("Didn’t ❌", callback_data=f"didnt:{r.id}")],
        [InlineKeyboardButton("Snooze 💤", callback_data=f"snooze:{r.id}")]
    ])
    await application.bot.send_message(
        chat_id=r.user_id,
        text=f"🔔 *Reminder*: {r.text}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )
    # log event
    ev = Event(reminder_id=r.id)
    db.add(ev); db.commit()
    db.refresh(ev)
    # update recurring?
    if r.is_recurring and r.recur_rule:
        r.next_trigger_utc = next_trigger_from_rule(r.next_trigger_utc, r.recur_rule)
    else:
        r.status = "completed"
    db.commit()
    db.close()

def next_trigger_from_rule(dt: datetime.datetime, rule: str) -> datetime.datetime:
    # simplified examples
    if rule == "daily":
        return dt + datetime.timedelta(days=1)
    if rule == "weekly":
        return dt + datetime.timedelta(weeks=1)
    # TODO: parse full rrule if needed
    return dt + datetime.timedelta(days=1)

# ---------- command handlers ----------
@user_only
async def start_cmd(update: Update, context):
    await update.message.reply_text(
        "Hi! Send me a reminder in natural language, e.g.:\n"
        "• Remind me tomorrow at 9 pm to stretch\n"
        "• Remind me every Monday to file the report\n"
        "Use /list to see active reminders."
    )

@user_only
async def list_cmd(update: Update, context):
    db = SessionLocal()
    rs = db.query(Reminder).filter(
        Reminder.user_id==update.effective_user.id,
        Reminder.status=="pending"
    ).all()
    if not rs:
        await update.message.reply_text("No active reminders.")
    else:
        lines = [
            f"• #{r.id} — {r.text} at {r.next_trigger_utc.isoformat()} UTC"
            + (" (recurring)" if r.is_recurring else "")
            for r in rs
        ]
        await update.message.reply_text("\n".join(lines))
    db.close()

@user_only
async def natlang_handler(update: Update, context):
    text = update.message.text.strip()
    parse_result = parse(text)
    if "error" in parse_result:
        if parse_result["error"] == "no_time":
            await update.message.reply_text("Please include a date/time 😊")
        elif parse_result["error"] == "not_reminder":
            await update.message.reply_text("I only handle reminders.")
        else:
            await update.message.reply_text("Sorry, couldn’t understand that.")
        return

    # create reminder
    db = SessionLocal()
    r = Reminder(
        user_id=update.effective_user.id,
        text=parse_result["task"],
        next_trigger_utc=parse_result["datetime_iso"],
        is_recurring=bool(parse_result.get("recurrence")),
        recur_rule=parse_result.get("recurrence")
    )
    db.add(r); db.commit()
    await update.message.reply_text(
        f"✅ Reminder set (ID {r.id}) for {r.next_trigger_utc} UTC"
        + (f", recurring {r.recur_rule}" if r.is_recurring else "")
    )
    db.close()

# ---------- callback for inline buttons ----------
async def button_cb(update: Update, context):
    query = update.callback_query
    await query.answer()
    action, rid = query.data.split(":")
    rid = int(rid)
    user_id = query.from_user.id
    db = SessionLocal()
    r = db.get(Reminder, rid)
    if not r or r.user_id != user_id:
        await query.edit_message_text("Not found or no permission.")
        db.close(); return

    ev = db.query(Event).filter(Event.reminder_id==rid).order_by(Event.id.desc()).first()
    resp = Response(event_id=ev.id if ev else None,
                    user_id=user_id, response=action)
    db.add(resp)
    if action == "snooze":
        r.next_trigger_utc = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
        await query.edit_message_text("Snoozed for 10 min.")
    else:
        r.status = "completed"
        await query.edit_message_text("Logged, thank you!")
    db.commit(); db.close()

# ---------- assemble app ----------
init_db()
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("list", list_cmd))
application.add_handler(CallbackQueryHandler(button_cb))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natlang_handler))

async def on_start(app):
    from scheduler import start as start_scheduler, send_due_reminder
    # Make sure send_due_reminder refers to the bot.py implementation, if needed
    # Or, if it's in scheduler anyway, ignore.
    start_scheduler(app)            # Only call AFTER async loop is running!

if __name__ == "__main__":
    application.post_init = on_start   # PTB calls this after the loop starts
    application.run_polling()
