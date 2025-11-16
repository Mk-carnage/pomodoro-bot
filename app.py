# app.py
"""
Integrated Pomodoro Bot (Zoho callback-driven) with MongoDB persistence,
tasks queue, streaks, XP, PDF export and smart suggestions.

How it works:
- POST /pomodoro with a payload (Deluge/Zoho style) to issue commands.
- When starting a pomodoro, we call Zoho API with bot_callback that points to:
  {HOST}/timer-callback?user=<USER>&type=pomodoro
- Zoho will POST to /timer-callback when the timer expires; we process completion there.
- Auto-breaks are scheduled similarly (Zoho callback with type=break).
"""

import os
import time
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from flask import Flask, request, jsonify, send_file, abort
import requests
from pymongo import MongoClient, ReturnDocument

# Optional: PDF export
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ---------------------------
# Configuration / env
# ---------------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "pomodoro_db")

# Zoho message endpoint (bot incoming)
# Example: https://cliq.zoho.in/api/v2/bots/yourbotname/message
ZOHO_BOT_API = os.getenv("ZOHO_BOT_API", "")
# OAuth token in format "Zoho-oauthtoken xxxxx"
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN", "")
# For refresh token flow:
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")
# Zoho accounts base domain (https://accounts.zoho.com or https://accounts.zoho.in)
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
    # We allow running locally without Zoho configured for testing, but warn.
    print("WARNING: ZOHO_BOT_API not set. Bot messages will fail until configured.")

# ---------------------------
# Mongo setup
# ---------------------------
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
col_timers = db.get_collection("timers")      # persistent active timer per user
col_history = db.get_collection("history")    # completed sessions (pomodoro/break)
col_tasks = db.get_collection("tasks")        # queued tasks per user
col_users = db.get_collection("users")        # user stats: xp, streaks, etc

# ensure unique active timer per user
try:
    col_timers.create_index("user", unique=True)
except Exception:
    pass

# ---------------------------
# Flask app
# ---------------------------
app = Flask(__name__)

# ---------------------------
# Helpers: time / iso
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
# Mongo helpers
# ---------------------------
def set_active_timer(user: str, timer_obj: dict):
    """timer_obj: { type, ends_at (int), task, duration, paused_pomodoro (opt) }"""
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
# Zoho token refresh + send message (with callback)
# ---------------------------

def refresh_access_token():
    """Use refresh_token to update ZOHO_OAUTH_TOKEN (in-memory only)."""
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return False
    url = f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
    }
    try:
        r = requests.post(url, params=params, timeout=10).json()
        if "access_token" in r:
            ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + r["access_token"]
            print("🔄 Refreshed Zoho Access Token.")
            return True
        else:
            print("refresh token error:", r)
    except Exception as e:
        print("refresh_access_token exception:", e)
    return False

def send_zoho_message(text: str, callback_seconds: int = None, user: str = None, host_url: str = None):
    if not ZOHO_BOT_API:
        print("ZOHO_BOT_API not set; skipping send_zoho_message.")
        return None

    headers = {
        "Authorization": ZOHO_OAUTH_TOKEN,
        "Content-Type": "application/json"
    }

    payload = {
        "message": {
            "text": text
        },
        "fallback": False   # 🔥 REQUIRED FOR CALLBACKS
    }

    if callback_seconds and host_url:
        params = {}
        if user:
            params["user"] = user

        callback_uri = f"{host_url.rstrip('/')}/timer-callback"
        if params:
            callback_uri += "?" + urlencode(params)

        payload["bot_callback"] = {
            "time": int(callback_seconds),
            "uri": callback_uri
        }

    print("📨 PAYLOAD SENT TO ZOHO:", payload)

    try:
        r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        if r.status_code == 401:
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)

        print("Zoho send status:", r.status_code, r.text)
        return r
    except Exception as e:
        print("send_zoho_message error:", e)
        return None



# ---------------------------
# Utility: parse incoming request (robust)
# ---------------------------
def parse_incoming_request(data):
    """
    Accept different keys used by Deluge/Zoho
    Return (user, raw_text)
    """
    if not isinstance(data, dict):
        return None, ""
    # try raw fields
    for key in ("raw", "message", "msg", "text", "raw_message", "raw_msg"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return data.get("user") or data.get("user_id") or data.get("sender") or data.get("userid") or "unknown", v.strip()
    # fallback to nested shapes
    if "message_details" in data and isinstance(data["message_details"], dict):
        md = data["message_details"]
        for key in ("raw_message", "message"):
            v = md.get(key)
            if isinstance(v, str) and v.strip():
                return data.get("user") or md.get("from") or "unknown", v.strip()
    # last fallback
    return data.get("user") or data.get("user_id") or "unknown", ""

# ---------------------------
# Core: streaks & xp
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
# Routes: main command endpoint (/pomodoro)
# ---------------------------
@app.route("/", methods=["GET", "HEAD"])
def home():
    return "OK", 200

@app.route("/pomodoro", methods=["POST"])
def pomodoro_route():
    data = request.json or {}
    user, raw = parse_incoming_request(data)
    user = str(user)
    raw_lower = (raw or "").strip()

    # handle different commands (start, break, status, tasks, etc.)
    cmd = raw_lower.lower()

    # ---- Add task: add task Task name 25 ----
    if cmd.startswith("add task"):
        parts = raw.strip().split()
        if len(parts) < 4:
            return jsonify({"reply": "Usage: add task <task name> <duration> (minutes)."})
        duration = parts[-1]
        if not duration.isdigit():
            return jsonify({"reply": "Duration must be a number (minutes)."})
        duration = int(duration)
        task_name = " ".join(parts[2:-1])
        q = load_tasks_for_user(user)
        q.append({"task": task_name, "duration": duration})
        save_tasks_for_user(user, q)
        return jsonify({"reply": f"📝 Added task: **{task_name}** ({duration} min)"})

    # ---- tasks ----
    if cmd == "tasks":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in queue."})
        out = "📋 Task Queue:\n"
        for i, t in enumerate(q, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out})

    # ---- start next ----
    if cmd == "start next":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in the queue."})
        next_task = q.pop(0)
        save_tasks_for_user(user, q)
        duration = next_task["duration"]
        task_name = next_task["task"]
        ends_at = now_ts() + duration * 60
        set_active_timer(user, {"type": "pomodoro", "ends_at": ends_at, "task": task_name, "duration": duration})
        # schedule callback via Zoho (include user)
        send_zoho_message(f"🍅 Started **{task_name}** ({duration} min)",
                          callback_seconds=duration * 60, user=user, host_url=request.host_url)
        return jsonify({"reply": f"▶️ Started next task: **{task_name}** ({duration} min)"})

    # ---- done i.e. remove from queue done 1 ----
    if cmd.startswith("done"):
        parts = cmd.split()
        if len(parts) != 2 or not parts[1].isdigit():
            return jsonify({"reply": "Usage: done <task_number>"})
        idx = int(parts[1]) - 1
        q = load_tasks_for_user(user)
        if idx < 0 or idx >= len(q):
            return jsonify({"reply": "❌ Invalid task number."})
        removed = q.pop(idx)
        save_tasks_for_user(user, q)
        return jsonify({"reply": f"✔ Removed task: **{removed['task']}**"})

    # ---- clear tasks ----
    if cmd == "clear tasks":
        save_tasks_for_user(user, [])
        return jsonify({"reply": "🗑 Cleared all tasks."})

    # ---- break manual: break or break 10 ----
    if cmd.startswith("break"):
        parts = raw.strip().split()
        if len(parts) == 1:
            minutes = 5
        elif len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
        else:
            return jsonify({"reply": "Usage: break OR break <minutes>"})
        # pause existing pomodoro if any
        cur = get_active_timer(user)
        paused = None
        if cur and cur.get("type") == "pomodoro":
            remaining = max(0, cur.get("ends_at", 0) - now_ts())
            paused = {"task": cur.get("task"), "remaining_seconds": remaining}
            remove_active_timer(user)
        ends_at = now_ts() + minutes * 60
        set_active_timer(user, {"type": "break", "ends_at": ends_at, "task": f"Manual Break ({minutes} min)", "duration": minutes, "paused_pomodoro": paused})
        # schedule callback
        send_zoho_message(f"☕ Break started for {minutes} minutes.",
                          callback_seconds=minutes * 60, user=user, host_url=request.host_url)
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."})

    # ---- stop break ----
    if cmd in ("stop break", "stopbreak", "break stop"):
        cur = get_active_timer(user)
        if not cur or cur.get("type") != "break":
            return jsonify({"reply": "❌ No active break to stop."})
        paused = cur.get("paused_pomodoro")
        remove_active_timer(user)
        if paused:
            remaining = int(paused.get("remaining_seconds", 0))
            ends_at = now_ts() + remaining
            set_active_timer(user, {"type": "pomodoro", "ends_at": ends_at, "task": paused.get("task"), "duration": round(remaining/60, 2)})
            send_zoho_message(f"▶️ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.",
                              callback_seconds=remaining, user=user, host_url=request.host_url)
            return jsonify({"reply": f"▶️ Break stopped. Resumed **{paused.get('task')}**."})
        else:
            return jsonify({"reply": "🛑 Break stopped."})

    # ---- start <minutes> <task...> or start <task...> ----
    if cmd.startswith("start"):
        # parse: start 25 Task name OR start Task name
        parts = raw.strip().split()
        duration = 25
        task_name = "Untitled Task"
        if len(parts) >= 2 and parts[0].lower() == "start":
            if len(parts) >= 3 and parts[1].isdigit():
                duration = int(parts[1])
                task_name = " ".join(parts[2:])
            else:
                # start <task...>
                if parts[1].isdigit():
                    duration = int(parts[1])
                    task_name = " ".join(parts[2:]) or "Untitled Task"
                else:
                    task_name = " ".join(parts[1:]) or "Untitled Task"
        ends_at = now_ts() + duration * 60
        # cancel any existing break if user starts
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        set_active_timer(user, {"type": "pomodoro", "ends_at": ends_at, "task": task_name, "duration": duration})
        # schedule callback via Zoho with user query
        send_zoho_message(f"🍅 Started **{task_name}** ({duration} min)", callback_seconds=duration * 60, user=user, host_url=request.host_url)
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"})

    # ---- status ----
    if cmd in ("status", "time", "progress"):
        cur = get_active_timer(user)
        if not cur:
            return jsonify({"reply": "❌ No active session."})
        rem = max(0, cur.get("ends_at", 0) - now_ts())
        typ = cur.get("type", "pomodoro")
        if typ == "pomodoro":
            return jsonify({"reply": f"🍅 {cur.get('task')} — {rem//60}m {rem%60}s left"})
        else:
            return jsonify({"reply": f"☕ Break — {rem//60}m {rem%60}s left"})

    # ---- stop / cancel ----
    if cmd in ("stop", "end", "cancel"):
        cur = get_active_timer(user)
        if cur:
            remove_active_timer(user)
            return jsonify({"reply": "🛑 Stopped."})
        return jsonify({"reply": "❌ No active session."})

    # ---- resume (for paused) - use stop break/resume above to implement) ----
    if cmd == "resume":
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
            paused = cur.get("paused_pomodoro")
            remaining = paused.get("remaining_seconds", 0)
            ends_at = now_ts() + remaining
            set_active_timer(user, {"type": "pomodoro", "ends_at": ends_at, "task": paused.get("task"), "duration": round(remaining/60, 2)})
            send_zoho_message(f"⏯ Resumed **{paused.get('task')}**", callback_seconds=remaining, user=user, host_url=request.host_url)
            return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
        return jsonify({"reply": "❌ Nothing to resume."})

    # ---- today summary ----
    if cmd == "today":
        return jsonify({"reply": build_daily_summary(user)})

    # ---- week summary ----
    if cmd == "week":
        entries = list(col_history.find({"user": user}).sort("completed_at", -1).limit(21))
        if not entries:
            return jsonify({"reply": "📭 No history"})
        summary = {}
        for h in entries:
            summary[h.get("date")] = summary.get(h.get("date"), 0) + 1
        out = "📊 Weekly Summary\n"
        for d, c in summary.items():
            out += f"{d}: {'🍅'*c} ({c})\n"
        return jsonify({"reply": out})

    # ---- analytics / chart ----
    if cmd in ("chart", "weekly chart", "analytics", "weekly analytics"):
        return jsonify({"reply": get_weekly_chart(user)})

    # ---- streak ----
    if cmd == "streak":
        s = load_user_stats(user)
        return jsonify({"reply": f"🔥 Streak: {s.get('current_streak', 0)} days\n🏆 Longest: {s.get('longest_streak', 0)} days"})

    # ---- score ----
    if cmd == "score":
        s = load_user_stats(user)
        return jsonify({"reply": f"🎯 XP: {s.get('xp', 0)}\n⭐ Level: {s.get('level', 1)}"})

    # ---- export ----
    if cmd.startswith("export"):
        return handle_export_command(user, cmd)

    # ---- suggestions ----
    if cmd in ("suggest", "ai suggest", "suggestions"):
        items = smart_suggestions(user)
        reply = "🤖 *Here are some suggestions:* \n"
        for i in items:
            reply += f"• {i}\n"
        return jsonify({"reply": reply})

    # ---- fallback/help ----
    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"})

# ---------------------------
# Timer callback receiver: Zoho POSTs here when callback expires
# ---------------------------
@app.route("/timer-callback", methods=["POST"])
def timer_callback():
    """
    Zoho POSTs here when a bot_callback expires.
    Callback URL includes:  /timer-callback?user=<userid>
    """

    # --------- Extract user safely ---------
    body = request.json or {}

    user = (
        request.args.get("user")
        or body.get("user")
        or body.get("sender")
        or body.get("user_id")
        or "unknown"
    )
    user = str(user)

    # Type of callback (pomodoro/break)
    cb_type = (
        request.args.get("type")
        or body.get("type")
        or "pomodoro"
    )

    print("🔥 CALLBACK RECEIVED → user:", user, "| type:", cb_type)

    # --------- Load active timer ---------
    t = get_active_timer(user)
    if not t:
        print("⚠ No active timer in DB for this user.")
        return "no active timer", 200

    ends_at = int(t.get("ends_at", 0))
    now = now_ts()

    # --------- Guard against early calls ---------
    if now < ends_at - 2:
        print("⏳ Callback arrived EARLY → ignoring.")
        return "too early", 200

    typ = t.get("type", "pomodoro")

    # ======================================================
    # 🎯 POMODORO FINISHED
    # ======================================================
    if typ == "pomodoro":

        completed_at_iso = ts_to_iso(now)
        hist_item = {
            "user": user,
            "task": t.get("task", "Untitled Task"),
            "duration": int(t.get("duration", 25)),
            "completed_at": completed_at_iso,
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "type": "pomodoro"
        }
        append_history(hist_item)

        # Update streak and XP
        current_streak, longest = update_streak_for_user(user)
        gained, total_xp, level = update_score(user, int(t.get("duration", 25)), current_streak)

        # Notify completion (Zoho-compliant message)
        send_zoho_message(
            (
                f"⏰ *Pomodoro completed!*\n"
                f"✔ Task: **{hist_item['task']}** ({hist_item['duration']} min)\n\n"
                f"🔥 Streak: {current_streak} days\n"
                f"🏆 Longest streak: {longest} days\n\n"
                f"🎯 XP earned: +{gained}\n"
                f"💠 Total XP: {total_xp}\n"
                f"⭐ Level: {level}"
            ),
            user=user,
            host_url=request.host_url
        )

        # ======================================================
        # auto-break scheduling
        # ======================================================
        completed_today = count_pomodoros_today(user)
        auto_break_min = 15 if (completed_today % 4 == 0) else 5

        break_ends_at = now + auto_break_min * 60
        set_active_timer(
            user,
            {
                "type": "break",
                "ends_at": break_ends_at,
                "task": f"Auto Break ({auto_break_min} min)",
                "duration": auto_break_min
            }
        )

        send_zoho_message(
            f"☕ Auto-break started for {auto_break_min} minutes.\n(Type `stop break` to cancel.)",
            callback_seconds=auto_break_min * 60,
            user=user,
            host_url=request.host_url
        )

        return "ok", 200

    # ======================================================
    # 🎯 BREAK FINISHED
    # ======================================================
    elif typ == "break":

        completed_at_iso = ts_to_iso(now)
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

        # --------- Resume paused pomodoro ---------
        if paused:
            remaining = int(paused.get("remaining_seconds", 0))
            ends_at = now + remaining

            set_active_timer(
                user,
                {
                    "type": "pomodoro",
                    "ends_at": ends_at,
                    "task": paused.get("task"),
                    "duration": round(remaining/60, 2)
                }
            )

            send_zoho_message(
                f"⏰ Break over — resuming **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.",
                callback_seconds=remaining,
                user=user,
                host_url=request.host_url
            )

            return "resumed", 200

        # --------- Normal break end (no paused task) ---------
        remove_active_timer(user)

        send_zoho_message(
            "☕ Break over! Ready to get back to work.",
            user=user,
            host_url=request.host_url
        )

        # Suggest queued task
        q = load_tasks_for_user(user)
        if q:
            next_task = q[0]
            send_zoho_message(
                f"⏭ Next task in queue: **{next_task['task']}** ({next_task['duration']} min).\nType `start next` to continue.",
                user=user,
                host_url=request.host_url
            )

        return "ok", 200

    # ======================================================
    return "ignored", 200


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
    # idle detection
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
    # top frequent task
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
# Boot / main
# ---------------------------
if __name__ == "__main__":
    print("Starting Pomodoro bot (callback-driven).")
    # No threads — safe on Render/Heroku
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
