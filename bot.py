import os
import logging
import json
import sqlite3
import datetime
import pytz
import re
import asyncio
from functools import wraps

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import aiohttp

# --- Load environment variables ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DB_PATH = os.getenv("DB_PATH", "reminders.db")

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup ---
def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        next_trigger_utc TIMESTAMP NOT NULL,
        is_recurring BOOLEAN NOT NULL,
        recur_rule TEXT,
        status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'deleted')),
        created_at TIMESTAMP NOT NULL
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id INTEGER NOT NULL,
        fired_at_utc TIMESTAMP NOT NULL,
        FOREIGN KEY(reminder_id) REFERENCES reminders(id)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        response TEXT NOT NULL CHECK(response IN ('did', 'didnt', 'snoozed')),
        responded_at_utc TIMESTAMP NOT NULL,
        FOREIGN KEY(event_id) REFERENCES events(id)
    )""")
    conn.commit()
    conn.close()

init_db()

# --- OpenAI LLM Parsing ---
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
SYSTEM_PROMPT = (
    "You are a reminder-creation assistant. "
    "If the text is a valid reminder (specific task and a specific time or recurrence), return JSON: "
    '{"task": "...", "datetime_iso": "...", "recurrence": "..."} '
    'If you can\'t find a valid time, return {"error": "no_time"} '
    'If the text isn\'t a reminder, return {"error": "not_reminder"}'
)

async def parse_reminder(text):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ],
        "max_tokens": 128,
        "temperature": 0
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OPENAI_API_URL, headers=headers, json=data) as resp:
                if resp.status != 200:
                    logger.error(f"OpenAI API error: {resp.status}")
                    return {"error": "llm_error"}
                result = await resp.json()
                try:
                    content = result["choices"][0]["message"]["content"]
                    # Extract JSON from response
                    match = re.search(r'\{.*\}', content, re.DOTALL)
                    if match:
                        return json.loads(match.group(0))
                    else:
                        return {"error": "parse_error"}
                except Exception as e:
                    logger.error(f"OpenAI parse error: {e}")
                    return {"error": "parse_error"}
    except Exception as e:
        logger.error(f"OpenAI request error: {e}")
        return {"error": "request_error"}

# --- APScheduler Setup ---
scheduler = AsyncIOScheduler(timezone="UTC")
application = None  # Will be set in main()

def schedule_all_reminders():
    conn = get_db()
    c = conn.cursor()
    now = datetime.datetime.utcnow()
    c.execute("""
        SELECT * FROM reminders
        WHERE status='pending' AND next_trigger_utc > ?
    """, (now,))
    for row in c.fetchall():
        schedule_reminder(row)
    conn.close()

def schedule_reminder(reminder_row):
    reminder_id = reminder_row["id"]
    trigger_time = reminder_row["next_trigger_utc"]
    if trigger_time < datetime.datetime.utcnow():
        return
    scheduler.add_job(
        send_reminder_job,
        trigger=DateTrigger(run_date=trigger_time),
        args=[reminder_id],
        id=f"reminder_{reminder_id}",
        replace_existing=True
    )

async def send_reminder_job(reminder_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM reminders WHERE id=? AND status='pending'", (reminder_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return
        user_id = row["user_id"]
        text = row["text"]
        # Insert event
        fired_at = datetime.datetime.utcnow()
        c.execute("INSERT INTO events (reminder_id, fired_at_utc) VALUES (?, ?)", (reminder_id, fired_at))
        event_id = c.lastrowid
        conn.commit()
        conn.close()
        # Send Telegram message with inline buttons
        keyboard = [
            [
                InlineKeyboardButton("Did ✅", callback_data=f"did|{event_id}|{reminder_id}"),
                InlineKeyboardButton("Didn't ❌", callback_data=f"didnt|{event_id}|{reminder_id}"),
                InlineKeyboardButton("Snooze 💤", callback_data=f"snooze|{event_id}|{reminder_id}")
            ]
        ]
        if application and application.bot:
            await application.bot.send_message(
                chat_id=user_id,
                text=f"⏰ Reminder: {text}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            logger.error(f"Application not available for reminder {reminder_id}")
    except Exception as e:
        logger.error(f"Error sending reminder {reminder_id}: {e}")

# --- Telegram Bot Handlers ---

def user_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None:
            return
        return await func(update, context)
    return wrapper

@user_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your reminder bot. "
        "Send me a message like 'Remind me to call Alice at 9pm tomorrow' or 'Show my reminders'."
    )

@user_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send reminders in natural language (e.g. 'Remind me to call Alice at 9pm tomorrow').\n"
        "Commands:\n"
        "- Show my reminders\n"
        "- Delete the dentist reminder\n"
        "- Change the dentist reminder to next Thursday at 2pm\n"
        "You'll get notified when it's time, with buttons to mark as done, not done, or snooze."
    )

@user_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    # Check for list/show reminders
    if re.search(r"\b(show|list)\b.*\breminders\b", text, re.I):
        await list_reminders(update, context)
        return
    # Check for delete/cancel
    if re.search(r"\b(delete|cancel)\b", text, re.I):
        await delete_reminder(update, context, text)
        return
    # Check for change/update
    if re.search(r"\b(change|update|edit|modify)\b", text, re.I):
        await update_reminder(update, context, text)
        return
    # Check for mark as done/completed
    if re.search(r"\b(mark)\b.*\b(done|completed)\b", text, re.I):
        await mark_reminder_done(update, context, text)
        return
    # Otherwise, try to parse as new reminder
    await create_reminder(update, context, text)

async def create_reminder(update, context, text):
    user_id = update.effective_user.id
    await update.message.reply_chat_action("typing")
    parsed = await parse_reminder(text)
    if "error" in parsed:
        if parsed["error"] == "no_time":
            await update.message.reply_text("Sorry, I couldn't find a time in your reminder. Please specify when.")
        elif parsed["error"] == "not_reminder":
            await update.message.reply_text("That doesn't look like a reminder. Please try again.")
        else:
            await update.message.reply_text("Sorry, I couldn't understand. Please try again.")
        return
    task = parsed["task"]
    dt_iso = parsed["datetime_iso"]
    recurrence = parsed.get("recurrence")
    try:
        dt_utc = datetime.datetime.fromisoformat(dt_iso)
        if dt_utc.tzinfo is not None:
            dt_utc = dt_utc.astimezone(pytz.UTC).replace(tzinfo=None)
    except Exception:
        await update.message.reply_text("Sorry, I couldn't parse the time. Please try again.")
        return
    is_recurring = bool(recurrence and recurrence.lower() not in ("", "none", "null"))
    now = datetime.datetime.utcnow()
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO reminders (user_id, text, next_trigger_utc, is_recurring, recur_rule, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
    """, (user_id, task, dt_utc, is_recurring, recurrence, now))
    reminder_id = c.lastrowid
    conn.commit()
    c.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,))
    row = c.fetchone()
    conn.close()
    schedule_reminder(row)
    await update.message.reply_text(f"Reminder set: {task} at {dt_utc} UTC" + (f" (recurring: {recurrence})" if is_recurring else ""))

async def list_reminders(update, context):
    user_id = update.effective_user.id
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, text, next_trigger_utc, is_recurring, recur_rule
        FROM reminders
        WHERE user_id=? AND status='pending'
        ORDER BY next_trigger_utc ASC
    """, (user_id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("You have no active reminders.")
        return
    msg = "Your active reminders:\n"
    for row in rows:
        dt = row["next_trigger_utc"]
        msg += f"- [{row['id']}] {row['text']} at {dt} UTC"
        if row["is_recurring"]:
            msg += f" (recurring: {row['recur_rule']})"
        msg += "\n"
    await update.message.reply_text(msg)

async def delete_reminder(update, context, text):
    user_id = update.effective_user.id
    # Try to extract reminder id or description
    m = re.search(r"\b(\d+)\b", text)
    conn = get_db()
    c = conn.cursor()
    if m:
        reminder_id = int(m.group(1))
        c.execute("SELECT * FROM reminders WHERE id=? AND user_id=? AND status='pending'", (reminder_id, user_id))
        row = c.fetchone()
        if not row:
            await update.message.reply_text("Reminder not found.")
            conn.close()
            return
        c.execute("UPDATE reminders SET status='deleted' WHERE id=?", (reminder_id,))
        conn.commit()
        conn.close()
        await update.message.reply_text("Reminder deleted.")
        return
    # Otherwise, try to match by description
    c.execute("""
        SELECT id, text FROM reminders
        WHERE user_id=? AND status='pending'
    """, (user_id,))
    rows = c.fetchall()
    for row in rows:
        if row["text"].lower() in text.lower():
            c.execute("UPDATE reminders SET status='deleted' WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"Deleted reminder: {row['text']}")
            return
    conn.close()
    await update.message.reply_text("Could not find the reminder to delete.")

async def update_reminder(update, context, text):
    user_id = update.effective_user.id
    # Find which reminder to update
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, text FROM reminders
        WHERE user_id=? AND status='pending'
    """, (user_id,))
    rows = c.fetchall()
    for row in rows:
        if row["text"].lower() in text.lower():
            # Try to parse new time/text from message
            m = re.search(r"\bto (.+)", text, re.I)
            if not m:
                await update.message.reply_text("Please specify the new time or text after 'to ...'.")
                conn.close()
                return
            new_text = m.group(1)
            parsed = await parse_reminder(new_text)
            if "error" in parsed:
                await update.message.reply_text("Sorry, I couldn't parse the new reminder. Please try again.")
                conn.close()
                return
            task = parsed["task"]
            dt_iso = parsed["datetime_iso"]
            recurrence = parsed.get("recurrence")
            try:
                dt_utc = datetime.datetime.fromisoformat(dt_iso)
                if dt_utc.tzinfo is not None:
                    dt_utc = dt_utc.astimezone(pytz.UTC).replace(tzinfo=None)
            except Exception:
                await update.message.reply_text("Sorry, I couldn't parse the new time. Please try again.")
                conn.close()
                return
            is_recurring = bool(recurrence and recurrence.lower() not in ("", "none", "null"))
            c.execute("""
                UPDATE reminders
                SET text=?, next_trigger_utc=?, is_recurring=?, recur_rule=?
                WHERE id=?
            """, (task, dt_utc, is_recurring, recurrence, row["id"]))
            conn.commit()
            c.execute("SELECT * FROM reminders WHERE id=?", (row["id"],))
            updated_row = c.fetchone()
            conn.close()
            schedule_reminder(updated_row)
            await update.message.reply_text(f"Reminder updated: {task} at {dt_utc} UTC" + (f" (recurring: {recurrence})" if is_recurring else ""))
            return
    conn.close()
    await update.message.reply_text("Could not find the reminder to update.")

async def mark_reminder_done(update, context, text):
    user_id = update.effective_user.id
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, text FROM reminders
        WHERE user_id=? AND status='pending'
    """, (user_id,))
    rows = c.fetchall()
    for row in rows:
        if row["text"].lower() in text.lower():
            c.execute("UPDATE reminders SET status='completed' WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"Marked as done: {row['text']}")
            return
    conn.close()
    await update.message.reply_text("Could not find the reminder to mark as done.")

@user_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data.split("|")
    if len(data) != 3:
        await query.edit_message_text("Invalid action.")
        return
    action, event_id, reminder_id = data
    event_id = int(event_id)
    reminder_id = int(reminder_id)
    now = datetime.datetime.utcnow()
    conn = get_db()
    c = conn.cursor()
    # Check event belongs to this user
    c.execute("""
        SELECT r.user_id, r.is_recurring, r.recur_rule, r.text, r.next_trigger_utc
        FROM reminders r
        JOIN events e ON r.id = e.reminder_id
        WHERE e.id=? AND r.id=? AND r.status='pending'
    """, (event_id, reminder_id))
    row = c.fetchone()
    if not row or row["user_id"] != user_id:
        await query.edit_message_text("Not allowed.")
        conn.close()
        return
    # Log response
    c.execute("""
        INSERT INTO responses (event_id, user_id, response, responded_at_utc)
        VALUES (?, ?, ?, ?)
    """, (event_id, user_id, action, now))
    # Handle actions
    if action == "did" or action == "didnt":
        if row["is_recurring"]:
            # Compute next occurrence
            next_dt = compute_next_occurrence(row["next_trigger_utc"], row["recur_rule"])
            if next_dt:
                c.execute("""
                    UPDATE reminders SET next_trigger_utc=? WHERE id=?
                """, (next_dt, reminder_id))
                conn.commit()
                c.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,))
                updated_row = c.fetchone()
                conn.close()
                schedule_reminder(updated_row)
                await query.edit_message_text(f"Logged. Next reminder at {next_dt} UTC.")
                return
            else:
                c.execute("UPDATE reminders SET status='completed' WHERE id=?", (reminder_id,))
                conn.commit()
                conn.close()
                await query.edit_message_text("Logged. No more recurrences.")
                return
        else:
            c.execute("UPDATE reminders SET status='completed' WHERE id=?", (reminder_id,))
            conn.commit()
            conn.close()
            await query.edit_message_text("Logged. Reminder completed.")
            return
    elif action == "snooze":
        # Ask user for new time
        context.user_data["snooze_event_id"] = event_id
        context.user_data["snooze_reminder_id"] = reminder_id
        await query.edit_message_text("When should I remind you again? (e.g. 'in 10 minutes', 'tomorrow at 8am')")
        conn.close()
        return

@user_only
async def handle_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "snooze_event_id" not in context.user_data or "snooze_reminder_id" not in context.user_data:
        return False
    user_id = update.effective_user.id
    event_id = context.user_data.pop("snooze_event_id")
    reminder_id = context.user_data.pop("snooze_reminder_id")
    text = update.message.text.strip()
    parsed = await parse_reminder(text)
    if "error" in parsed or not parsed.get("datetime_iso"):
        await update.message.reply_text("Sorry, I couldn't parse the snooze time. Please try again.")
        return True
    dt_iso = parsed["datetime_iso"]
    try:
        dt_utc = datetime.datetime.fromisoformat(dt_iso)
        if dt_utc.tzinfo is not None:
            dt_utc = dt_utc.astimezone(pytz.UTC).replace(tzinfo=None)
    except Exception:
        await update.message.reply_text("Sorry, I couldn't parse the snooze time. Please try again.")
        return True
    now = datetime.datetime.utcnow()
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO responses (event_id, user_id, response, responded_at_utc)
        VALUES (?, ?, 'snoozed', ?)
    """, (event_id, user_id, now))
    c.execute("""
        UPDATE reminders SET next_trigger_utc=? WHERE id=?
    """, (dt_utc, reminder_id))
    conn.commit()
    c.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,))
    row = c.fetchone()
    conn.close()
    schedule_reminder(row)
    await update.message.reply_text(f"Snoozed! Next reminder at {dt_utc} UTC.")
    return True

def compute_next_occurrence(last_dt, recur_rule):
    # Simple rules: 'daily', 'weekly', 'monthly'
    if not recur_rule:
        return None
    if isinstance(last_dt, str):
        last_dt = datetime.datetime.fromisoformat(last_dt)
    if recur_rule.lower() == "daily":
        return last_dt + datetime.timedelta(days=1)
    if recur_rule.lower() == "weekly":
        return last_dt + datetime.timedelta(weeks=1)
    if recur_rule.lower() == "monthly":
        # Add 1 month (approx)
        return last_dt + datetime.timedelta(days=30)
    # TODO: Support ISO RRULEs if needed
    return None

# --- Main Application Setup ---
async def main():
    global application
    # Initialize the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Add message handlers with proper order
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start scheduler
    schedule_all_reminders()
    scheduler.start()
    
    logger.info("Bot started.")
    
    # Start the bot
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise 
