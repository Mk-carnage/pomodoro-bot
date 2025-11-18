"""
Pomodoro Bot (server-driven timers) with Zoho Card UI
- MongoDB persistence
- Server-driven timers (threads) that send card messages to Zoho when timers expire
- Card UI builders (start/status/summary/streak/score/chart-poll fallback)
- OAuth refresh support

Environment variables required:
- MONGO_URI
- MONGO_DB (optional, default pomodoro_db)
- ZOHO_BOT_API (https://cliq.zoho.com/api/v2/bots/<BOT_UNIQUE_NAME>/message)
- ZOHO_OAUTH_TOKEN (initial: 'Zoho-oauthtoken <token>')
- CLIENT_ID (optional, for refresh)
- CLIENT_SECRET (optional, for refresh)
- REFRESH_TOKEN (optional, for refresh)
- ZOHO_ACCOUNTS_BASE (optional, default https://accounts.zoho.com)
- LOCAL_TZ (optional, Asia/Kolkata)
- PORT (optional)

Notes:
- Buttons on cards that perform actions will send regular message payloads to your /pomodoro route when clicked (Cliq invokes bot webhook). Implement Deluge or map clicks in your bot UI to send appropriate text commands if needed.
- Chart support in Zoho cards is limited; this code falls back to poll-style card for visualization.
"""

import os
import time
import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from flask import Flask, request, jsonify, send_file, abort
import requests
from pymongo import MongoClient

# ---------------------------
# Optional PDF export
# ---------------------------
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ---------------------------
# Config / env
# ---------------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "pomodoro_db")

ZOHO_BOT_API = os.getenv("ZOHO_BOT_API", "")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN", "")
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")
ZOHO_ACCOUNTS_BASE = os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com")

LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Asia/Kolkata")
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc

REPORT_DIR = "reports"
os.makedirs(REPORT_DIR, exist_ok=True)

if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable must be set.")

if not ZOHO_BOT_API:
    print("WARNING: ZOHO_BOT_API not set. Messages to Zoho will fail until configured.")

# ---------------------------
# Mongo
# ---------------------------
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
col_timers = db.get_collection("timers")
col_history = db.get_collection("history")
col_tasks = db.get_collection("tasks")
col_users = db.get_collection("users")

try:
    col_timers.create_index("user", unique=True)
except Exception:
    pass

# ---------------------------
# Flask
# ---------------------------
app = Flask(__name__)

# ---------------------------
# Time helpers
# ---------------------------

def now_ts() -> int:
    return int(time.time())


def ts_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def iso_to_dt(iso: str) -> datetime:
    try:
        s = str(iso).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc)

# ---------------------------
# DB helpers
# ---------------------------

def set_active_timer(user: str, timer_obj: dict):
    doc = timer_obj.copy()
    doc["user"] = user
    col_timers.find_one_and_update({"user": user}, {"$set": doc}, upsert=True)


def get_active_timer(user: str):
    return col_timers.find_one({"user": user})


def remove_active_timer(user: str):
    col_timers.delete_one({"user": user})


def append_history(item: dict):
    col_history.insert_one(item)


def load_tasks_for_user(user: str):
    doc = col_tasks.find_one({"user": user})
    return doc.get("queue", []) if doc else []


def save_tasks_for_user(user: str, queue):
    col_tasks.find_one_and_update({"user": user}, {"$set": {"queue": queue}}, upsert=True)


def load_user_stats(user: str):
    doc = col_users.find_one({"user": user})
    if not doc:
        return {"xp": 0, "level": 1, "current_streak": 0, "longest_streak": 0, "last_completed_date": None}
    return {
        "xp": doc.get("xp", 0),
        "level": doc.get("level", 1),
        "current_streak": doc.get("current_streak", 0),
        "longest_streak": doc.get("longest_streak", 0),
        "last_completed_date": doc.get("last_completed_date")
    }


def save_user_stats(user: str, stats: dict):
    stats = stats.copy()
    stats["user"] = user
    col_users.find_one_and_update({"user": user}, {"$set": stats}, upsert=True)

# ---------------------------
# Analytics & suggestions (must be above routes)
# ---------------------------

def build_daily_summary(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    entries = list(col_history.find({"user": user_id, "date": today}))
    if not entries:
        return "📭 No activity today."
    total = 0
    completed_tasks = []
    for h in entries:
        completed_tasks.append(f"- {h.get('task')} ({h.get('duration')} min, {h.get('type')})")
        if h.get("type") == "pomodoro":
            total += int(h.get("duration", 0))
    s = f"📊 YOUR DAILY SUMMARY ({today})\n\nCompleted: {len(entries)} tasks\nTotal focus time: {total} min\n\nTasks:\n" + "\n".join(completed_tasks)
    return s


def get_weekly_chart(user_id):
    docs = list(col_history.find({"user": user_id}))
    if not docs:
        return "📭 No history available."
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekly = {d: {"count": 0, "minutes": 0} for d in days}
    for h in docs:
        try:
            dt = datetime.fromisoformat(h.get("completed_at"))
        except Exception:
            try:
                dt = datetime.strptime(h.get("date"), "%Y-%m-%d")
            except Exception:
                dt = datetime.utcnow()
        weekday = days[dt.weekday()]
        weekly[weekday]["count"] += 1
        weekly[weekday]["minutes"] += int(h.get("duration", 0))
    out = "📊 WEEKLY ANALYTICS\n\n"
    for day in days:
        count = weekly[day]["count"]
        minutes = weekly[day]["minutes"]
        bar = ("█" * count) if count > 0 else "-"
        out += f"{day}: {bar} {count} ({minutes} min)\n"
    return out


def smart_suggestions(user_id):
    history = list(col_history.find({"user": user_id}).sort("completed_at", -1).limit(200))
    tasks = load_tasks_for_user(user_id)
    s = load_user_stats(user_id)
    suggestions = []
    now = datetime.now(LOCAL_TZ)
    hour = now.hour
    level = s.get("level", 1)
    streak = s.get("current_streak", 0)
    if hour < 12:
        suggestions.append("A fresh morning — try a focused 25 min session.")
    elif 12 <= hour < 17:
        suggestions.append("Good afternoon! Deep work suits this slot.")
    elif 17 <= hour < 21:
        suggestions.append("Evening — prefer light review or creative tasks.")
    else:
        suggestions.append("It's late — plan tomorrow's tasks.")
    if streak >= 5:
        suggestions.append(f"You're on a {streak}-day streak — keep it up with a short session.")
    elif 1 <= streak <= 4:
        suggestions.append("You're building consistency — one Pomodoro will help.")
    if level <= 3:
        suggestions.append("Start with a 15–20 min session to earn XP quickly.")
    elif level >= 8:
        suggestions.append("Try a long deep-work 50–90 min block if possible.")
    if history:
        try:
            last = datetime.fromisoformat(history[0].get("completed_at"))
            idle_hours = (datetime.utcnow().replace(tzinfo=timezone.utc) - last).total_seconds() / 3600
            if idle_hours >= 3:
                suggestions.append("It's been a while — try a quick 10 min focus to restart.")
        except Exception:
            pass
    if tasks:
        suggestions.append(f"Next task: **{tasks[0]['task']}** ({tasks[0]['duration']} min)")
    freq = {}
    for h in history:
        if h.get("type") == "pomodoro":
            t = h.get("task")
            freq[t] = freq.get(t, 0) + 1
    if freq:
        top = max(freq, key=freq.get)
        suggestions.append(f"You've worked often on **{top}** — consider continuing it.")
    if not suggestions:
        suggestions = ["Try a small 15-minute session to start the streak."]
    return suggestions

# ---------------------------
# OAuth + Zoho send helpers
# ---------------------------
ZOHO_LOCK = threading.Lock()


def refresh_access_token():
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return False
    url = f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    try:
        r = requests.post(url, params=params, timeout=10)
        j = r.json()
        if "access_token" in j:
            with ZOHO_LOCK:
                ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + j["access_token"]
            print("🔄 Refreshed Zoho Access Token.")
            return True
        else:
            print("refresh token error:", j)
    except Exception as e:
        print("refresh_access_token exception:", e)
    return False


def send_zoho_message(text: str = None, card: dict = None, buttons: list = None):
    """Send Zoho message. Must include top-level 'text'. Optionally include 'card' and 'buttons'.
    Follows Zoho v2 message format: https://www.zoho.com/cliq/help/restapi/v2/#Message
    """
    if not ZOHO_BOT_API:
        print("ZOHO_BOT_API not configured; skipping send.")
        return None

    if not text and not card:
        raise ValueError("send_zoho_message requires text or card")

    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}
    payload = {}
    # Zoho requires top-level 'text' key even when card present
    payload["text"] = text or ""
    if card:
        payload["card"] = card
    if buttons:
        # top-level buttons array
        payload["buttons"] = buttons

    # Debug
    print("📨 PAYLOAD -> ZOHO:", json.dumps(payload))

    try:
        r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        if r.status_code == 401:
            # try refresh once
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        print("Zoho send status:", getattr(r, "status_code", None), getattr(r, "text", None))
        return r
    except Exception as e:
        print("send_zoho_message error:", e)
        return None

# ---------------------------
# Card builders (Zoho v2 format)
# ---------------------------

def build_start_card(task: str, duration: int):
    card = {
        "title": f"Started: {task}",
        "theme": "standard",
        "thumbnail": "https://img.icons8.com/color/96/tomato.png",
        "sections": [
            {
                "widgets": [
                    {"type": "label", "text": f"🍅 {task} — {duration} min"},
                    {"type": "label", "text": "Timer started. Relax and focus!"}
                ]
            }
        ]
    }
    # Buttons: stop / status
    buttons = [
        {
            "label": "Stop",
            "type": "-",
            "action": {"type": "invoke.function", "data": {"name": "stop_cmd"}}
        },
        {
            "label": "Status",
            "type": "+",
            "action": {"type": "invoke.function", "data": {"name": "status_cmd"}}
        }
    ]
    return card, buttons


def build_status_card(user: str):
    cur = get_active_timer(user)
    if not cur:
        card = {"title": "Status", "theme": "standard", "sections": [{"widgets": [{"type": "label", "text": "No active session."}]}]}
        return card, None
    rem = max(0, cur.get("ends_at", 0) - now_ts())
    typ = cur.get("type", "pomodoro")
    lines = [f"Task: {cur.get('task')}", f"Type: {typ}", f"Remaining: {rem//60}m {rem%60}s"]
    card = {
        "title": "Session Status",
        "theme": "standard",
        "sections": [{"widgets": [{"type": "label", "text": l} for l in lines]}]
    }
    buttons = [
        {"label": "Stop", "type": "-", "action": {"type": "invoke.function", "data": {"name": "stop_cmd"}}},
        {"label": "Break", "type": "+", "action": {"type": "invoke.function", "data": {"name": "break_cmd"}}}
    ]
    return card, buttons


def build_summary_card(user: str):
    summary = build_daily_summary(user)
    card = {
        "title": "Today's Summary",
        "theme": "standard",
        "sections": [{"widgets": [{"type": "label", "text": summary}]}]
    }
    return card, None


def build_streak_card(user: str):
    s = load_user_stats(user)
    card = {
        "title": "Streak & Score",
        "theme": "standard",
        "sections": [
            {"widgets": [{"type": "label", "text": f"🔥 Current streak: {s.get('current_streak',0)} days"},
                         {"type": "label", "text": f"🏆 Longest: {s.get('longest_streak',0)} days"},
                         {"type": "label", "text": f"🎯 XP: {s.get('xp',0)} | ⭐ Level: {s.get('level',1)}"}]}
        ]
    }
    return card, None


def build_chart_poll_card(user: str):
    # Zoho doesn't support rich charting in card easily via API; produce a poll-style representation
    docs = list(col_history.find({"user": user}))
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    weekly = {d:0 for d in days}
    for h in docs:
        try:
            dt = datetime.fromisoformat(h.get("completed_at"))
        except Exception:
            try:
                dt = datetime.strptime(h.get("date"), "%Y-%m-%d")
            except Exception:
                dt = datetime.utcnow()
        weekly[days[dt.weekday()]] += 1
    # Build poll options
    buttons = []
    for day in days:
        buttons.append({
            "label": f"{day}: {weekly[day]}",
            "type": "+",
            "action": {"type": "invoke.function", "data": {"name": "noop"}}
        })
    card = {"title": "Weekly Activity (poll view)", "theme": "poll", "thumbnail": "https://www.zoho.com/cliq/help/restapi/images/poll_icon.png"}
    return card, buttons

# ---------------------------
# Scoring / streaks
# ---------------------------

def update_streak_for_user(user_id: str):
    s = load_user_stats(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    last = s.get("last_completed_date")
    if last == yesterday:
        s["current_streak"] = s.get("current_streak", 0) + 1
    elif last != today:
        s["current_streak"] = 1
    s["longest_streak"] = max(s.get("longest_streak", 0), s.get("current_streak", 0))
    s["last_completed_date"] = today
    save_user_stats(user_id, s)
    return s["current_streak"], s["longest_streak"]


def calculate_level(xp: int):
    if xp < 100: return 1
    if xp < 250: return 2
    if xp < 500: return 3
    if xp < 1000: return 4
    return xp // 500 + 4


def update_score(user_id: str, duration: int, streak: int):
    s = load_user_stats(user_id)
    base_xp = int(duration)
    streak_bonus = streak * 5
    long_bonus = 10 if duration >= 30 else 0
    gained = base_xp + streak_bonus + long_bonus
    s["xp"] = s.get("xp", 0) + gained
    s["level"] = calculate_level(s["xp"])
    save_user_stats(user_id, s)
    return gained, s["xp"], s["level"]


def count_pomodoros_today(user_id: str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return col_history.count_documents({"user": user_id, "date": today, "type": "pomodoro"})

# ---------------------------
# Timer engine (server-driven)
# ---------------------------

timer_threads = {}

timer_threads_lock = threading.Lock()


def _timer_worker(user: str):
    print(f"Timer worker started for user={user}")
    while True:
        t = get_active_timer(user)
        if not t:
            print(f"No timer entry for {user}; worker exiting.")
            break
        ends_at = int(t.get("ends_at", 0))
        remaining = ends_at - now_ts()
        if remaining <= 0:
            typ = t.get("type", "pomodoro")
            if typ == "pomodoro":
                completed_at_iso = ts_to_iso(now_ts())
                hist_item = {
                    "user": user,
                    "task": t.get("task", "Untitled Task"),
                    "duration": int(t.get("duration", 25)),
                    "completed_at": completed_at_iso,
                    "date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "type": "pomodoro"
                }
                append_history(hist_item)
                current_streak, longest = update_streak_for_user(user)
                gained, total_xp, level = update_score(user, int(t.get("duration", 25)), current_streak)

                # Send single completion card (avoid duplicates)
                card, buttons = build_start_card(hist_item["task"], hist_item["duration"])  # reuse start card style for completion
                # override text to completion message
                text = (f"⏰ Pomodoro completed!\n✔ Task: {hist_item['task']} ({hist_item['duration']} min)\n\n"
                        f"🔥 Streak: {current_streak} days | 🏆 Longest: {longest} days\n"
                        f"🎯 XP earned: +{gained} | 💠 Total XP: {total_xp} | ⭐ Level: {level}")
                send_zoho_message(text=text, card=card, buttons=None)

                # Auto-start break
                completed_today = count_pomodoros_today(user)
                auto_break_min = 15 if (completed_today % 4 == 0) else 5
                break_ends_at = now_ts() + auto_break_min * 60
                set_active_timer(user, {"type": "break", "ends_at": break_ends_at, "task": f"Auto Break ({auto_break_min} min)", "duration": auto_break_min})
                # send break-start card
                bcard = {"title": "Break started", "theme": "standard", "sections": [{"widgets": [{"type": "label", "text": f"☕ Auto-break started for {auto_break_min} minutes."}]}]}
                send_zoho_message(text=f"☕ Auto-break started for {auto_break_min} minutes.", card=bcard)

                # loop continue to handle break
                continue

            elif typ == "break":
                completed_at_iso = ts_to_iso(now_ts())
                hist_item = {
                    "user": user,
                    "task": t.get("task", "Break"),
                    "duration": int(t.get("duration", 5)),
                    "completed_at": completed_at_iso,
                    "date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "type": "break"
                }
                append_history(hist_item)
                paused = t.get("paused_pomodoro")
                if paused:
                    remaining = int(paused.get("remaining_seconds", 0))
                    ends_at = now_ts() + remaining
                    set_active_timer(user, {"type": "pomodoro", "ends_at": ends_at, "task": paused.get("task"), "duration": round(remaining/60, 2)})
                    send_zoho_message(text=f"⏰ Break over — resuming {paused.get('task')} with {remaining//60}m {remaining%60}s left.")
                    continue
                else:
                    remove_active_timer(user)
                    send_zoho_message(text="☕ Break over! Ready to get back to work.")
                    q = load_tasks_for_user(user)
                    if q:
                        next_task = q[0]
                        send_zoho_message(text=f"⏭ Next task in queue: {next_task['task']} ({next_task['duration']} min). Type 'start next' to continue.")
                    break
            else:
                remove_active_timer(user)
                break
        else:
            sleep_for = min(1.0, max(0.1, remaining))
            time.sleep(sleep_for)

    with timer_threads_lock:
        timer_threads.pop(user, None)
    print(f"Timer worker ended for user={user}")


def start_timer_thread_if_needed(user: str):
    with timer_threads_lock:
        if user in timer_threads:
            return
        t = threading.Thread(target=_timer_worker, args=(user,), daemon=True)
        timer_threads[user] = t
        t.start()


def schedule_timer(user: str, typ: str, duration_min: float, task: str, paused_pomodoro=None):
    ends_at = now_ts() + int(duration_min * 60)
    doc = {"type": typ, "ends_at": ends_at, "task": task, "duration": duration_min}
    if paused_pomodoro:
        doc["paused_pomodoro"] = paused_pomodoro
    set_active_timer(user, doc)
    start_timer_thread_if_needed(user)


def rehydrate_timers():
    docs = list(col_timers.find({}))
    for d in docs:
        user = d.get("user")
        if not user:
            continue
        start_timer_thread_if_needed(user)
    print(f"Rehydrated {len(docs)} timers from DB.")

# ---------------------------
# Request parsing helper
# ---------------------------

def parse_incoming_request(data):
    if not isinstance(data, dict):
        return None, ""
    for key in ("raw", "message", "msg", "text", "raw_message", "raw_msg"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return data.get("user") or data.get("user_id") or data.get("sender") or data.get("userid") or "unknown", v.strip()
    if "message_details" in data and isinstance(data["message_details"], dict):
        md = data["message_details"]
        for key in ("raw_message", "message"):
            v = md.get(key)
            if isinstance(v, str) and v.strip():
                return data.get("user") or md.get("from") or "unknown", v.strip()
    return data.get("user") or data.get("user_id") or "unknown", ""

# ---------------------------
# Routes
# ---------------------------
@app.route("/", methods=["GET", "HEAD"])
def home():
    return "OK", 200

@app.route("/pomodoro", methods=["POST"])
def pomodoro_route():
    data = request.json or {}
    user, raw = parse_incoming_request(data)
    user = str(user)
    raw = (raw or "").strip()
    cmd = raw.lower()

    # add task
    if cmd.startswith("add task"):
        parts = raw.split()
        if len(parts) < 4:
            return jsonify({"reply": "Usage: add task <task name> <duration> (minutes)."}), 200
        duration = parts[-1]
        if not duration.isdigit():
            return jsonify({"reply": "Duration must be a number (minutes)."}), 200
        duration = int(duration)
        task_name = " ".join(parts[2:-1])
        q = load_tasks_for_user(user)
        q.append({"task": task_name, "duration": duration})
        save_tasks_for_user(user, q)
        return jsonify({"reply": f"📝 Added task: **{task_name}** ({duration} min)"}), 200

    if cmd == "tasks":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in queue."}), 200
        out = "📋 Task Queue:\n"
        for i, t in enumerate(q, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out}), 200

    if cmd == "start next":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in the queue."}), 200
        next_task = q.pop(0)
        save_tasks_for_user(user, q)
        duration = next_task["duration"]
        task_name = next_task["task"]
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        card, buttons = build_start_card(task_name, duration)
        send_zoho_message(text=f"🍅 Started {task_name} ({duration} min)", card=card, buttons=None)
        return jsonify({"reply": f"▶️ Started next task: **{task_name}** ({duration} min)"}), 200

    if cmd.startswith("done"):
        parts = cmd.split()
        if len(parts) != 2 or not parts[1].isdigit():
            return jsonify({"reply": "Usage: done <task_number>"}), 200
        idx = int(parts[1]) - 1
        q = load_tasks_for_user(user)
        if idx < 0 or idx >= len(q):
            return jsonify({"reply": "❌ Invalid task number."}), 200
        removed = q.pop(idx)
        save_tasks_for_user(user, q)
        return jsonify({"reply": f"✔ Removed task: **{removed['task']}**"}), 200

    if cmd == "clear tasks":
        save_tasks_for_user(user, [])
        return jsonify({"reply": "🗑 Cleared all tasks."}), 200

    if cmd.startswith("break"):
        parts = raw.split()
        if len(parts) == 1:
            minutes = 5
        elif len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
        else:
            return jsonify({"reply": "Usage: break OR break <minutes>"}), 200
        cur = get_active_timer(user)
        paused = None
        if cur and cur.get("type") == "pomodoro":
            remaining = max(0, cur.get("ends_at", 0) - now_ts())
            paused = {"task": cur.get("task"), "remaining_seconds": remaining}
            remove_active_timer(user)
        schedule_timer(user, "break", minutes, f"Manual Break ({minutes} min)", paused_pomodoro=paused)
        send_zoho_message(text=f"☕ Break started for {minutes} minutes.")
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."}), 200

    if cmd in ("stop break", "stopbreak", "break stop"):
        cur = get_active_timer(user)
        if not cur or cur.get("type") != "break":
            return jsonify({"reply": "❌ No active break to stop."}), 200
        paused = cur.get("paused_pomodoro")
        remove_active_timer(user)
        if paused:
            remaining = int(paused.get("remaining_seconds", 0))
            schedule_timer(user, "pomodoro", remaining/60.0, paused.get("task"))
            send_zoho_message(text=f"▶️ Resumed {paused.get('task')} with {remaining//60}m {remaining%60}s left.")
            return jsonify({"reply": f"▶️ Break stopped. Resumed **{paused.get('task')}**."}), 200
        else:
            return jsonify({"reply": "🛑 Break stopped."}), 200

    # ---- start command fix: handles "start 1 Task" and "start Task" and "start 1" correctly
    if cmd.startswith("start"):
        tokens = raw.split()
        duration = 25
        task_name = "Untitled Task"
        if len(tokens) >= 2:
            if tokens[1].isdigit():
                duration = int(tokens[1])
                task_name = " ".join(tokens[2:]) or "Untitled Task"
            else:
                task_name = " ".join(tokens[1:]) or "Untitled Task"
        # cancel any break
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        card, buttons = build_start_card(task_name, duration)
        send_zoho_message(text=f"🍅 Started {task_name} ({duration} min)", card=card, buttons=None)
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"}), 200

    if cmd in ("status", "time", "progress"):
        card, buttons = build_status_card(user)
        send_zoho_message(text="Session status", card=card, buttons=None)
        return jsonify({"reply": "Status sent."}), 200

    if cmd in ("stop", "end", "cancel"):
        cur = get_active_timer(user)
        if cur:
            remove_active_timer(user)
            send_zoho_message(text="🛑 Stopped current session.")
            return jsonify({"reply": "🛑 Stopped."}), 200
        return jsonify({"reply": "❌ No active session."}), 200

    if cmd == "resume":
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
            paused = cur.get("paused_pomodoro")
            remaining = paused.get("remaining_seconds", 0)
            schedule_timer(user, "pomodoro", remaining/60.0, paused.get("task"))
            send_zoho_message(text=f"⏯ Resumed {paused.get('task')}")
            return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."}), 200
        return jsonify({"reply": "❌ Nothing to resume."}), 200

    if cmd == "today":
        card, _ = build_summary_card(user)
        send_zoho_message(text="Today's summary", card=card)
        return jsonify({"reply": "Summary sent."}), 200

    if cmd == "week":
        reply = get_weekly_chart(user)
        send_zoho_message(text=reply)
        return jsonify({"reply": "Weekly chart sent."}), 200

    if cmd in ("chart", "weekly chart", "analytics", "weekly analytics"):
        card, buttons = build_chart_poll_card(user)
        send_zoho_message(text="Weekly activity", card=card, buttons=buttons)
        return jsonify({"reply": "Chart (poll) sent."}), 200

    if cmd == "streak":
        card, _ = build_streak_card(user)
        send_zoho_message(text="Streak & score", card=card)
        return jsonify({"reply": "Streak sent."}), 200

    if cmd == "score":
        s = load_user_stats(user)
        send_zoho_message(text=f"🎯 XP: {s.get('xp',0)}\n⭐ Level: {s.get('level',1)}")
        return jsonify({"reply": "Score sent."}), 200

    if cmd.startswith("export"):
        return handle_export_command(user, cmd)

    if cmd in ("suggest", "ai suggest", "suggestions"):
        items = smart_suggestions(user)
        reply = "🤖 Here are some suggestions:\n"
        for i in items:
            reply += f"• {i}\n"
        send_zoho_message(text=reply)
        return jsonify({"reply": "Suggestions sent."}), 200

    # fallback
    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"}), 200

# ---------------------------
# Export / PDF helpers
# ---------------------------

def create_pdf(filepath, title, lines):
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab not installed")
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, height - 80, title)
    c.setFont("Helvetica", 12)
    y = height - 120
    for line in lines:
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 12)
            y = height - 60
        c.drawString(50, y, line)
        y -= 18
    c.save()


def generate_daily_report(user_id: str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    entries = list(col_history.find({"user": user_id, "date": today}))
    filepath = os.path.join(REPORT_DIR, f"daily_{user_id}.pdf")
    lines = []
    total = 0
    if not entries:
        lines.append("No activity today.")
    else:
        lines.append("Today's Pomodoro Activity:\n")
        for h in entries:
            t = h.get("task", "")
            d = h.get("duration", 0)
            tt = h.get("type", "pomodoro")
            lines.append(f"- {t} ({d} min, {tt})")
            if tt == "pomodoro":
                total += d
        lines.append("")
        lines.append(f"Total Focus Time: {total} min")
    create_pdf(filepath, "Daily Report", lines)
    return filepath


def generate_weekly_report(user_id: str):
    start = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=7)
    entries = [h for h in col_history.find({"user": user_id}) if iso_to_dt(h.get("completed_at")) >= start]
    filepath = os.path.join(REPORT_DIR, f"weekly_{user_id}.pdf")
    lines = []
    total = 0
    if not entries:
        lines.append("No activity in last 7 days.")
    else:
        lines.append("Weekly Pomodoro Activity:\n")
        for h in sorted(entries, key=lambda x: x.get("completed_at")):
            lines.append(f"{h.get('date')}: {h.get('task')} ({h.get('duration')} min, {h.get('type')})")
            if h.get("type") == "pomodoro":
                total += int(h.get("duration", 0))
        lines.append("")
        lines.append(f"Total Focus Time (7 days): {total} min")
    create_pdf(filepath, "Weekly Report", lines)
    return filepath


def generate_monthly_report(user_id: str):
    start = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=30)
    entries = [h for h in col_history.find({"user": user_id}) if iso_to_dt(h.get("completed_at")) >= start]
    filepath = os.path.join(REPORT_DIR, f"monthly_{user_id}.pdf")
    lines = []
    total = 0
    p_count = 0
    b_count = 0
    if not entries:
        lines.append("No activity in last 30 days.")
    else:
        lines.append("Monthly Pomodoro Activity:\n")
        for h in sorted(entries, key=lambda x: x.get("completed_at")):
            lines.append(f"{h.get('date')}: {h.get('task')} ({h.get('duration')} min, {h.get('type')})")
            if h.get("type") == "pomodoro":
                p_count += 1
                total += int(h.get("duration", 0))
            else:
                b_count += 1
        lines.append("")
        lines.append(f"Pomodoro Sessions: {p_count}")
        lines.append(f"Break Sessions: {b_count}")
        lines.append(f"Total Focus Time: {total} min")
    create_pdf(filepath, "Monthly Report", lines)
    return filepath

@app.route("/download/<path:filename>")
def download_report(filename):
    filepath = os.path.join(REPORT_DIR, filename)
    if not os.path.exists(filepath):
        return "❌ File not found.", 404
    return send_file(filepath, as_attachment=True)


def handle_export_command(user, raw_lower):
    if not REPORTLAB_AVAILABLE:
        return jsonify({"reply": "❌ PDF export not available (reportlab missing)."}), 500
    if raw_lower in ("export today", "export daily"):
        fp = generate_daily_report(user)
    elif raw_lower in ("export week", "export weekly"):
        fp = generate_weekly_report(user)
    elif raw_lower in ("export month", "export monthly"):
        fp = generate_monthly_report(user)
    else:
        return jsonify({"reply": "❌ Use: export today | export week | export month"})
    link = request.host_url.rstrip("/") + "/download/" + os.path.basename(fp)
    return jsonify({"reply": f"📄 Report ready! Download: {link}"})

# ---------------------------
# Boot
# ---------------------------
if __name__ == "__main__":
    print("Starting server-driven Pomodoro bot.")
    rehydrate_timers()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
