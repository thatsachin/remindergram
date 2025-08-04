# gpt_parser.py
import os, json, datetime, openai
openai.api_key = os.getenv("OPENAI_API_KEY")
openai.api_base = "https://api.studio.nebius.com/v1"
MODEL = "meta-llama/Meta-Llama-3.1-8B-Instruct"

SYSTEM_PROMPT = """You are a reminder-creation assistant.
If the text is a valid reminder (specific task AND a specific time or clear recurrence)
return a JSON exactly like:
{"task":"...","datetime_iso":"...","recurrence":"..."}
Use ISO 8601 UTC for datetime_iso.
If time missing: {"error":"no_time"}
If not a reminder: {"error":"not_reminder"}"""

def parse(text: str) -> dict:
    chat = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text}
    ]
    resp = openai.ChatCompletion.create(model=MODEL, messages=chat, temperature=0)
    raw = resp.choices[0].message.content.strip()
    # guard-rail: must be valid JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "parse_failed"}
    # datetime sanity
    if "datetime_iso" in data:
        try:
            datetime.datetime.fromisoformat(data["datetime_iso"].replace("Z",""))
        except ValueError:
            return {"error": "bad_time"}
    return data
