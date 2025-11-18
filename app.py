# app.py
"""
Optimized Pomodoro Bot with Card UI (Zoho Cliq)
- Server-driven timers stored in MongoDB
- Worker threads fire at timer end and send completion/break messages (cards)
- Routes accept Deluge/Incoming webhook payloads and return replies (and optionally send cards)
"""

import os
import time
import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, send_file
import requests
from pymongo import MongoClient

# optional PDF export
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ---------------------------
# Config (env)
# ---------------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "pomodoro_db")

# Zoho bot API message endpoint (message)
ZOHO_BOT_API = os.getenv("ZOHO_BOT_API", "")  # e.g. https://cliq.zoho.com/api/v2/bots/<bot>/message
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN", "")  # 'Zoho-oauthtoken <token>'

# OAuth refresh (optional)
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
col_timers = db.get_collection("timers")      # active timers: { user, type, ends_at, task, duration, paused_pomodoro }
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
# Utilities
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
# OAuth / Zoho messaging
# ---------------------------
ZOHO_LOCK = threading.Lock()

def refresh_access_token():
    """Use refresh_token to update ZOHO_OAUTH_TOKEN in memory."""
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        print("No CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN configured.")
        return False
    url = f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
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

def send_zoho_message(text: str = "", card: dict = None, buttons: list = None):
    """
    Sends message/card to Zoho Cliq bot message endpoint.
    Payload can include text, card and buttons keys.
    """
    if not ZOHO_BOT_API:
        print("ZOHO_BOT_API not set; skipping send_zoho_message.")
        return None

    headers = {
        "Authorization": ZOHO_OAUTH_TOKEN,
        "Content-Type": "application/json"
    }

    payload = {}
    if text:
        payload["text"] = text
    # Avoid putting invalid/unknown theme values; use minimal card fields accepted by Zoho
    if card:
        payload["card"] = card
    if buttons:
        payload["buttons"] = buttons

    # debug
    try:
        print("📨 PAYLOAD TO ZOHO:", json.dumps(payload, indent=2, default=str))
    except Exception:
        print("📨 PAYLOAD TO ZOHO (no-json)")

    try:
        r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        # token expired -> refresh + retry once
        if getattr(r, "status_code", None) == 401:
            print("Zoho 401 — refreshing token and retrying.")
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        print("Zoho send status:", getattr(r, "status_code", None), getattr(r, "text", None))
        return r
    except Exception as e:
        print("send_zoho_message exception:", e)
        return None

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
timer_threads = {}   # user -> Thread
timer_threads_lock = threading.Lock()

def _timer_worker(user: str):
    """
    Worker that monitors DB entry for user's active timer and acts when ends_at reached.
    Only the worker sends completion/break notifications to avoid duplicates.
    """
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
                # record history
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

                # update streak & xp
                current_streak, longest = update_streak_for_user(user)
                gained, total_xp, level = update_score(user, int(t.get("duration", 25)), current_streak)

                # Build completion card and send (worker sole sender of completion)
                card = build_summary_card(user, hist_item, current_streak, longest, gained, total_xp, level)
                send_zoho_message(text=f"Pomodoro completed: {hist_item['task']}", card=card, buttons=None)

                # auto-start break
                completed_today = count_pomodoros_today(user)
                auto_break_min = 15 if (completed_today % 4 == 0) else 5
                break_ends_at = now_ts() + auto_break_min * 60
                set_active_timer(user, {"type": "break", "ends_at": break_ends_at, "task": f"Auto Break ({auto_break_min} min)", "duration": auto_break_min})
                # send auto-break card
                br_card = build_start_card(user, f"Auto Break ({auto_break_min} min)", auto_break_min, typ="break")
                send_zoho_message(text=f"Auto-break started ({auto_break_min} min)", card=br_card, buttons=None)
                # continue worker loop to handle break
                continue

            elif typ == "break":
                # record break
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
                    # notify resuming
                    send_zoho_message(text=f"Break over — resuming {paused.get('task')}")
                    continue
                else:
                    remove_active_timer(user)
                    send_zoho_message(text="☕ Break over! Ready to get back to work.")
                    q = load_tasks_for_user(user)
                    if q:
                        next_task = q[0]
                        send_zoho_message(text=f"⏭ Next task in queue: {next_task['task']} ({next_task['duration']} min). Type `start next` to continue.")
                    break
            else:
                # unknown type - cleanup
                remove_active_timer(user)
                break
        else:
            # sleep short to allow responsive cancel
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
    """
    Store timer in DB and start worker thread.
    Note: schedule_timer does NOT send any Zoho message itself.
    Route handlers are responsible for sending start/status cards.
    """
    ends_at = now_ts() + int(duration_min * 60)
    doc = {"type": typ, "ends_at": ends_at, "task": task, "duration": duration_min}
    if paused_pomodoro:
        doc["paused_pomodoro"] = paused_pomodoro
    set_active_timer(user, doc)
    start_timer_thread_if_needed(user)

# Rehydrate timers on boot
def rehydrate_timers():
    docs = list(col_timers.find({}))
    for d in docs:
        user = d.get("user")
        if not user:
            continue
        start_timer_thread_if_needed(user)
    print(f"Rehydrated {len(docs)} timers from DB.")

# ---------------------------
# Card builders
# ---------------------------
def build_start_card(user: str, task: str, duration: int, typ: str = "pomodoro"):
    """
    Build a minimal Zoho card accepted by API.
    Avoids 'theme' values that caused earlier input_pattern_mismatch errors.
    """
    subtitle = f"{duration} min • {typ.capitalize()}"
    card = {
        "title": "Pomodoro Bot",
        "subtitle": subtitle,
        "thumbnail": "https://img.icons8.com/emoji/96/tomato-emoji.png",
        "sections": [
            {
                "widgets": [
                    {"type": "label", "text": f"🍅 Started **{task}** ({duration} min)"} if typ == "pomodoro" else {"type": "label", "text": f"☕ Break: **{task}** ({duration} min)"},
                ]
            }
        ]
    }
    # simple action buttons: stop/resume/next
    buttons = [
        {"label": "Stop", "type": "-", "action": {"type": "invoke.function", "data": {"cmd": "stop"}}},
        {"label": "Status", "type": "+", "action": {"type": "invoke.function", "data": {"cmd": "status"}}}
    ]
    return card

def build_status_card(user: str):
    cur = get_active_timer(user)
    if not cur:
        card = {
            "title": "Pomodoro Status",
            "subtitle": "No active session",
            "sections": [{"widgets": [{"type": "label", "text": "No Pomodoro or Break is active."}]}]
        }
        return {"text": "No active session.", "card": card, "buttons": []}
    rem = max(0, cur.get("ends_at", 0) - now_ts())
    m, s = divmod(rem, 60)
    task = cur.get("task", "Untitled Task")
    typ = cur.get("type", "pomodoro")
    card = {
        "title": "Session Status",
        "subtitle": f"{typ.capitalize()} • {m}m {s}s remaining",
        "sections": [
            {"widgets": [
                {"type": "label", "text": f"Task: {task}"},
                {"type": "label", "text": f"Type: {typ}"},
                {"type": "label", "text": f"Remaining: {m}m {s}s"},
            ]}
        ]
    }
    buttons = [
        {"label": "Stop", "type": "-", "action": {"type": "invoke.function", "data": {"cmd": "stop"}}},
        {"label": "Break", "type": "+", "action": {"type": "invoke.function", "data": {"cmd": "break"}}}
    ]
    return {"text": f"{typ} status", "card": card, "buttons": buttons}

def build_summary_card(user: str, hist_item: dict, current_streak: int, longest: int, gained: int, total_xp: int, level: int):
    # A rich summary card for completion event
    card = {
        "title": "Pomodoro completed!",
        "subtitle": f"Task: {hist_item.get('task')} — {hist_item.get('duration')} min",
        "sections": [
            {"widgets": [
                {"type": "label", "text": f"✔ Task: **{hist_item.get('task')}** ({hist_item.get('duration')} min)"},
                {"type": "label", "text": f"🔥 Streak: {current_streak} days  •  🏆 Longest: {longest} days"},
                {"type": "label", "text": f"🎯 XP earned: +{gained}  •  💠 Total XP: {total_xp}  •  ⭐ Level: {level}"}
            ]}
        ]
    }
    return card

def build_streak_card(user: str):
    s = load_user_stats(user)
    card = {
        "title": "Streak",
        "subtitle": f"{s.get('current_streak',0)} days (longest {s.get('longest_streak',0)})",
        "sections": [{"widgets":[{"type":"label","text":f"🔥 Current streak: {s.get('current_streak',0)} days"},{"type":"label","text":f"🏆 Longest streak: {s.get('longest_streak',0)} days"}]}]
    }
    return {"text":"Streak info", "card":card, "buttons":[]}

def build_score_card(user: str):
    s = load_user_stats(user)
    card = {
        "title": "Score",
        "subtitle": f"XP: {s.get('xp',0)}  •  Level: {s.get('level',1)}",
        "sections":[{"widgets":[{"type":"label","text":f"🎯 XP: {s.get('xp',0)}"},{"type":"label","text":f"⭐ Level: {s.get('level',1)}"}]}]
    }
    return {"text":"Score", "card":card, "buttons":[]}

# ---------------------------
# Request parsing helper
# ---------------------------
def parse_incoming_request(data):
    """
    Flexible parsing for incoming webhook / Deluge payloads:
    Look for keys commonly used (raw, message, text, msg, raw_message).
    Return (user_id, message_text)
    """
    if not isinstance(data, dict):
        return "unknown", ""
    # If bot-framework uses 'raw' with plain string (your earlier deluge example)
    # we also accept top-level message_details.message etc.
    for key in ("raw", "message", "msg", "text", "raw_message", "raw_msg"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            uid = data.get("user") or data.get("user_id") or data.get("sender") or data.get("userid") or "unknown"
            return str(uid), v.strip()
    if "message_details" in data and isinstance(data["message_details"], dict):
        md = data["message_details"]
        for key in ("raw_message", "message", "text"):
            v = md.get(key)
            if isinstance(v, str) and v.strip():
                uid = data.get("user") or md.get("from") or "unknown"
                return str(uid), v.strip()
    # fallback
    uid = data.get("user") or data.get("user_id") or "unknown"
    return str(uid), ""

# ---------------------------
# Main routes
# ---------------------------
@app.route("/", methods=["GET", "HEAD"])
def home():
    return "OK", 200

@app.route("/pomodoro", methods=["POST"])
def pomodoro_route():
    data = request.get_json(silent=True) or {}
    user, raw = parse_incoming_request(data)
    user = str(user)
    raw = (raw or "").strip()
    cmd_lower = raw.lower()

    # ---------- add task ----------
    if cmd_lower.startswith("add task"):
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

    # ---------- list tasks ----------
    if cmd_lower == "tasks":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in queue."}), 200
        out = "📋 Task Queue:\n"
        for i, t in enumerate(q, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out}), 200

    # ---------- start next ----------
    if cmd_lower == "start next":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in the queue."}), 200
        next_task = q.pop(0)
        save_tasks_for_user(user, q)
        duration = int(next_task["duration"])
        task_name = next_task["task"]
        # cancel break if running
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        # send start card (route responsible for sending start)
        card = build_start_card(user, task_name, duration, typ="pomodoro")
        send_zoho_message(text=f"Started next task: {task_name}", card=card)
        return jsonify({"reply": f"▶️ Started next task: **{task_name}** ({duration} min)"}), 200

    # ---------- done n ----------
    if cmd_lower.startswith("done"):
        parts = cmd_lower.split()
        if len(parts) != 2 or not parts[1].isdigit():
            return jsonify({"reply": "Usage: done <task_number>"}), 200
        idx = int(parts[1]) - 1
        q = load_tasks_for_user(user)
        if idx < 0 or idx >= len(q):
            return jsonify({"reply": "❌ Invalid task number."}), 200
        removed = q.pop(idx)
        save_tasks_for_user(user, q)
        return jsonify({"reply": f"✔ Removed task: **{removed['task']}**"}), 200

    # ---------- clear tasks ----------
    if cmd_lower == "clear tasks":
        save_tasks_for_user(user, [])
        return jsonify({"reply": "🗑 Cleared all tasks."}), 200

    # ---------- break ----------
    if cmd_lower.startswith("break"):
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
        card = build_start_card(user, f"Manual Break ({minutes} min)", minutes, typ="break")
        send_zoho_message(text=f"Break started for {minutes} minutes.", card=card)
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."}), 200

    # ---------- stop break ----------
    if cmd_lower in ("stop break", "stopbreak", "break stop"):
        cur = get_active_timer(user)
        if not cur or cur.get("type") != "break":
            return jsonify({"reply": "❌ No active break to stop."}), 200
        paused = cur.get("paused_pomodoro")
        remove_active_timer(user)
        if paused:
            remaining = int(paused.get("remaining_seconds", 0))
            schedule_timer(user, "pomodoro", remaining / 60.0, paused.get("task"))
            # send resume card
            card = build_start_card(user, paused.get("task"), int(remaining/60) or 1, typ="pomodoro")
            send_zoho_message(text=f"Resumed {paused.get('task')}", card=card)
            return jsonify({"reply": f"▶️ Break stopped. Resumed **{paused.get('task')}**."}), 200
        else:
            return jsonify({"reply": "🛑 Break stopped."}), 200

    # ---------- start ----------
    if cmd_lower.startswith("start"):
        # parsing rules:
        # start <minutes> <task...> -> if second token is digit -> minutes
        # start <task...> -> default 25
        # start <minutes> -> start with default name "Untitled Task"
        parts = raw.split()
        duration = 25
        task_name = "Untitled Task"
        if len(parts) >= 2:
            # parts[1] could be number or part of task
            second = parts[1]
            if second.isdigit():
                duration = int(second)
                if len(parts) >= 3:
                    task_name = " ".join(parts[2:]).strip() or task_name
                else:
                    task_name = "Untitled Task"
            else:
                # treat rest as task name
                task_name = " ".join(parts[1:]).strip() or task_name
        # cancel any break
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        # send start card (route sends the start card; worker will handle completion)
        card = build_start_card(user, task_name, duration, typ="pomodoro")
        send_zoho_message(text=f"🍅 Started {task_name} ({duration} min)", card=card)
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"}), 200

    # ---------- status/time/progress (show card) ----------
    if cmd_lower in ("status", "time", "progress"):
        payload = build_status_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"), buttons=payload.get("buttons"))
        return jsonify({"reply": "Status card sent."}), 200

    # ---------- stop / cancel ----------
    if cmd_lower in ("stop", "end", "cancel"):
        cur = get_active_timer(user)
        if cur:
            remove_active_timer(user)
            return jsonify({"reply": "🛑 Stopped."}), 200
        return jsonify({"reply": "❌ No active session."}), 200

    # ---------- resume ----------
    if cmd_lower == "resume":
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
            paused = cur.get("paused_pomodoro")
            remaining = paused.get("remaining_seconds", 0)
            schedule_timer(user, "pomodoro", remaining / 60.0, paused.get("task"))
            card = build_start_card(user, paused.get("task"), int(remaining/60) or 1, typ="pomodoro")
            send_zoho_message(text=f"⏯ Resumed {paused.get('task')}", card=card)
            return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."}), 200
        return jsonify({"reply": "❌ Nothing to resume."}), 200

    # ---------- today summary ----------
    if cmd_lower == "today":
        s = build_daily_summary(user)
        card = {
            "title": "Daily Summary",
            "sections": [{"widgets": [{"type":"label","text": line} for line in s.split("\n")]}]
        }
        send_zoho_message(text="Today's Summary", card=card)
        return jsonify({"reply": "Daily summary sent."}), 200

    # ---------- week ----------
    if cmd_lower == "week":
        out = get_weekly_chart(user)
        send_zoho_message(text="Weekly Summary", card={"title":"Weekly Summary", "sections":[{"widgets":[{"type":"label","text":out}]}]})
        return jsonify({"reply": "Weekly summary sent."}), 200

    if cmd_lower in ("chart", "weekly chart", "analytics", "weekly analytics"):
        out = get_weekly_chart(user)
        return jsonify({"reply": out}), 200

    if cmd_lower == "streak":
        payload = build_streak_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"))
        return jsonify({"reply": "Streak card sent."}), 200

    if cmd_lower == "score":
        payload = build_score_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"))
        return jsonify({"reply": "Score card sent."}), 200

    # ---------- export ----------
    if cmd_lower.startswith("export"):
        return handle_export_command(user, cmd_lower)

    # ---------- suggestions ----------
    if cmd_lower in ("suggest", "ai suggest", "suggestions"):
        items = smart_suggestions(user)
        reply = "🤖 *Here are some suggestions:* \n"
        for i in items:
            reply += f"• {i}\n"
        send_zoho_message(text="Suggestions", card={"title":"AI Suggestions","sections":[{"widgets":[{"type":"label","text":reply}]}]})
        return jsonify({"reply": "Suggestions sent."}), 200

    # fallback
    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"}), 200

# ---------------------------
# Export / PDF helpers
# (unchanged from earlier — kept for completeness)
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
        return jsonify({"reply": "❌ Use: export today | export week | export month"}), 200
    link = request.host_url.rstrip("/") + "/download/" + os.path.basename(fp)
    return jsonify({"reply": f"📄 Report ready! Download: {link}"}), 200

# ---------------------------
# Analytics & suggestions
# ---------------------------
def get_weekly_chart(user_id):
    docs = list(col_history.find({"user": user_id}))
    if not docs:
        return "📭 No history available."
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    weekly = {d: {"count":0, "minutes":0} for d in days}
    for h in docs:
        try:
            dt = datetime.fromisoformat(h.get("completed_at"))
        except:
            try:
                dt = datetime.strptime(h.get("date"), "%Y-%m-%d")
            except:
                dt = datetime.utcnow()
        weekday = days[dt.weekday()]
        weekly[weekday]["count"] += 1
        weekly[weekday]["minutes"] += int(h.get("duration",0))
    out = "📊 WEEKLY ANALYTICS\n\n"
    total_sessions = 0
    total_minutes = 0
    most_day = None
    most_count = 0
    for day in days:
        count = weekly[day]["count"]
        minutes = weekly[day]["minutes"]
        bar = ("█"*count) if count>0 else "-"
        out += f"{day}: {bar} {count} ({minutes} min)\n"
        total_sessions += count
        total_minutes += minutes
        if count > most_count:
            most_count = count
            most_day = day
    out += f"\nTotal Sessions: {total_sessions}\nTotal Focus Time: {total_minutes} min\n"
    if most_day:
        out += f"🔥 Most productive day: {most_day} ({most_count} sessions)"
    return out

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
            total += int(h.get("duration",0))
    s = f"📊 YOUR DAILY SUMMARY ({today})\n\nCompleted: {len(entries)} tasks\nTotal focus time: {total} min\n\nTasks:\n" + "\n".join(completed_tasks)
    return s

def smart_suggestions(user_id):
    history = list(col_history.find({"user": user_id}).sort("completed_at", -1).limit(200))
    tasks = load_tasks_for_user(user_id)
    s = load_user_stats(user_id)
    suggestions = []
    now = datetime.now(LOCAL_TZ)
    hour = now.hour
    level = s.get("level",1)
    streak = s.get("current_streak",0)
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
        except:
            pass
    if tasks:
        suggestions.append(f"Next task: **{tasks[0]['task']}** ({tasks[0]['duration']} min)")
    freq = {}
    for h in history:
        if h.get("type") == "pomodoro":
            t = h.get("task")
            freq[t] = freq.get(t,0) + 1
    if freq:
        top = max(freq, key=freq.get)
        suggestions.append(f"You've worked often on **{top}** — consider continuing it.")
    if not suggestions:
        suggestions = ["Try a small 15-minute session to start the streak."]
    return suggestions

# ---------------------------
# Boot
# ---------------------------
if __name__ == "__main__":
    print("Starting optimized Pomodoro bot with Card UI.")
    rehydrate_timers()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
