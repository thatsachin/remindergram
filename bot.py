"""
Cloud-hosted Telegram Reminder Bot
‒ Two-user ready, but scales to many.
‒ Runs on Render.com free tier (background worker).
‒ Python 3.11+, only open-source libs.
Environment variables (set in Render dashboard):
    TELEGRAM_BOT_TOKEN
    OPENAI_API_KEY
"""
import os
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta

import openai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from python_dotenv import dotenv_values          # for local dev only
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ---------------------------------------------------------------------
# 0.  Configuration & logging
# ---------------------------------------------------------------------
LOCAL_ENV = dotenv_values(".env")               # ignored in production
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", LOCAL_ENV.get("TELEGRAM_BOT_TOKEN"))
openai.api_key = os.getenv("OPENAI_API_KEY", LOCAL_ENV.get("OPENAI_API_KEY"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("reminder-bot")

# ---------------------------------------------------------------------
# 1.  Database layer (SQLite, file in container)
# ---------------------------------------------------------------------
DB_PATH = "reminders.sqlite"
conn = sqlite3.connect(DB_PATH, check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
conn.row_factory = sqlite3.Row

def init_db():
    cur = conn.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS reminders(
        id              INTEGER PRIMARY KEY,
        user_id         INTEGER NOT NULL,
        text            TEXT NOT NULL,
        next_trigger_utc TIMESTAMP NOT NULL,
        is_recurring    INTEGER NOT NULL DEFAULT 0,
        recur_rule      TEXT,
        status          TEXT NOT NULL DEFAULT 'pending',
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS events(
        id          INTEGER PRIMARY KEY,
        reminder_id INTEGER NOT NULL,
        fired_at_utc TIMESTAMP NOT NULL,
        FOREIGN KEY(reminder_id) REFERENCES reminders(id)
    );

    CREATE TABLE IF NOT EXISTS responses(
        id              INTEGER PRIMARY KEY,
        event_id        INTEGER NOT NULL,
        user_id         INTEGER NOT NULL,
        response        TEXT NOT NULL,
        responded_at_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(event_id) REFERENCES events(id)
    );
    """)
    conn.commit()

init_db()

# ---------------------------------------------------------------------
# 2.  Natural-language parsing via GPT function-style prompt
# ---------------------------------------------------------------------
SYSTEM_PROMPT = """You are a reminder-creation assistant.
If the user message describes a reminder with both task and time, return JSON:
 {"task": "<string>",
  "datetime_iso": "<ISO-8601 UTC or with offset>",
  "recurrence": "<null|daily|weekly|monthly|custom rrule>"}
If the message lacks a clear time: {"error": "no_time"}
If the text is not a reminder request: {"error": "not_reminder"}"""

async def parse_reminder(text: str) -> dict:
    completion = await openai.ChatCompletion.acreate(
        model="gpt-3.5-turbo-1106",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ],
        temperature=0,
        response_format={"type": "json_object"}
    )
    return json.loads(completion.choices[0].message.content)

# ---------------------------------------------------------------------
# 3.  APScheduler for delivery (in-process, survives restarts via DB)
# ---------------------------------------------------------------------
scheduler = AsyncIOScheduler()

def schedule_job(reminder_id: int, when_utc: datetime):
    """
    Register a one-shot job to fire at given UTC time.
    """
    scheduler.add_job(
        func=deliver_reminder,
        trigger=DateTrigger(run_date=when_utc),
        args=[reminder_id],
        id=f"reminder-{reminder_id}",
        replace_existing=True,
        misfire_grace_time=30  # seconds
    )

def rebuild_jobs_from_db():
    cur = conn.cursor()
    cur.execute(
        "SELECT id, next_trigger_utc FROM reminders "
        "WHERE status='pending' AND next_trigger_utc > ?",
        (datetime.now(timezone.utc) - timedelta(seconds=5),)
    )
    for row in cur.fetchall():
        schedule_job(row["id"], row["next_trigger_utc"])
    log.info("Re-scheduled %d pending reminders from DB", cur.rowcount)

# ---------------------------------------------------------------------
# 4.  Telegram command / message handlers
# ---------------------------------------------------------------------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Just tell me what to remind you.\n"
        "Examples:\n"
        "• Remind me tomorrow at 9 pm to call Alice\n"
        "• Remind me every Monday to send the report\n"
        "Send /list to see active reminders."
    )

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cur = conn.cursor()
    cur.execute(
        "SELECT id, text, next_trigger_utc, is_recurring "
        "FROM reminders WHERE user_id=? AND status='pending' "
        "ORDER BY next_trigger_utc", (uid,))
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("You have no active reminders.")
        return
    lines = []
    for r in rows:
        due_local = r["next_trigger_utc"].astimezone() \
            .strftime('%Y-%m-%d %H:%M')
        recurring = "🔁" if r["is_recurring"] else "🕑"
        lines.append(f"{recurring}  #{r['id']}: {r['text']}  — {due_local}")
    await update.message.reply_text("\n".join(lines))

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    text = update.message.text

    # Basic moderating: limit to two authorised users if needed
    # AUTH_USERS = {12345678, 98765432}
    # if uid not in AUTH_USERS:
    #     await update.message.reply_text("Sorry, not authorised.")
    #     return

    try:
        parsed = await parse_reminder(text)
    except Exception as e:
        log.exception("OpenAI parse error")
        await update.message.reply_text("❌ Error understanding that.")
        return

    if parsed.get("error"):
        await update.message.reply_text(
            "⚠️ " +
            ("I couldn’t find a time in your reminder."
             if parsed["error"] == "no_time"
             else "That doesn’t look like a reminder."))
        return

    # Normalise datetime
    try:
        when = datetime.fromisoformat(parsed["datetime_iso"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when_utc = when.astimezone(timezone.utc)
    except Exception:
        await update.message.reply_text("❌ Can’t read the time you gave.")
        return
    if when_utc < datetime.now(timezone.utc):
        await update.message.reply_text("⏱️ That time is in the past.")
        return

    recur = parsed.get("recurrence")
    is_recurring = bool(recur and recur.lower() != "null")

    # Store in DB
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders(user_id,text,next_trigger_utc,is_recurring,recur_rule)"
        "VALUES (?,?,?,?,?)",
        (uid, parsed["task"], when_utc, int(is_recurring), recur)
    )
    rid = cur.lastrowid
    conn.commit()

    # Schedule job
    schedule_job(rid, when_utc)

    await update.message.reply_text(
        f"✅ Reminder set (id #{rid}) for {when.astimezone().strftime('%Y-%m-%d %H:%M')}"
        + (" and will repeat." if is_recurring else "")
    )

# ---------------------------------------------------------------------
# 5.  Delivery & callback-query handling
# ---------------------------------------------------------------------
async def deliver_reminder(reminder_id: int):
    cur = conn.cursor()
    cur.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,))
    r = cur.fetchone()
    if not r or r["status"] != "pending":
        return

    # Write event row
    cur.execute(
        "INSERT INTO events(reminder_id,fired_at_utc) VALUES(?,?)",
        (reminder_id, datetime.now(timezone.utc))
    )
    event_id = cur.lastrowid
    conn.commit()

    # Build inline buttons
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Did ✅", callback_data=f"{event_id}:did"),
        InlineKeyboardButton("Didn’t ❌", callback_data=f"{event_id}:didnt"),
        InlineKeyboardButton("Snooze 💤", callback_data=f"{event_id}:snooze")
    ]])

    # Send message
    app = Application.get_instance()
    await app.bot.send_message(
        chat_id=r["user_id"],
        text=f"⏰ *Reminder*: {r['text']}",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=kb
    )

async def button_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    payload = query.data  # format "event_id:action"
    try:
        event_id, action = payload.split(":")
        event_id = int(event_id)
    except ValueError:
        return

    uid = query.from_user.id
    cur = conn.cursor()
    # Ensure event belongs to user
    cur.execute("""
        SELECT e.id, r.id AS rid, r.user_id, r.is_recurring, r.recur_rule
        FROM events e JOIN reminders r ON e.reminder_id=r.id
        WHERE e.id=? AND r.user_id=?""", (event_id, uid))
    row = cur.fetchone()
    if not row:
        await query.edit_message_text("⚠️ Not your reminder.")
        return

    # Log response
    cur.execute(
        "INSERT INTO responses(event_id,user_id,response) VALUES(?,?,?)",
        (event_id, uid, action)
    )

    # Handle snooze
    if action == "snooze":
        await query.edit_message_text("How many minutes to snooze?")
        ctx.user_data["awaiting_snooze"] = row["rid"]
        return

    # Mark completed/non-completed
    if not row["is_recurring"]:
        cur.execute("UPDATE reminders SET status='completed' WHERE id=?", (row["rid"],))
    conn.commit()
    await query.edit_message_text("Noted, thanks! ✅" if action == "did" else "Got it. ❌")

async def snooze_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "awaiting_snooze" not in ctx.user_data:
        return
    rid = ctx.user_data.pop("awaiting_snooze")
    try:
        mins = int(update.message.text.strip())
        if not 1 <= mins <= 1440:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a snooze time in minutes (1-1440).")
        ctx.user_data["awaiting_snooze"] = rid
        return
    new_time = datetime.now(timezone.utc) + timedelta(minutes=mins)
    cur = conn.cursor()
    cur.execute(
        "UPDATE reminders SET next_trigger_utc=? WHERE id=?",
        (new_time, rid)
    )
    conn.commit()
    schedule_job(rid, new_time)
    await update.message.reply_text(f"Snoozed for {mins} minutes. 💤")

# ---------------------------------------------------------------------
# 6.  Build & run application
# ---------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, snooze_reply))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Start scheduler & rebuild jobs
    scheduler.start()
    rebuild_jobs_from_db()

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
