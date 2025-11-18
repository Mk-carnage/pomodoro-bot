# app.py
"""
Pomodoro bot (server-driven timers)
- Uses MongoDB for persistence
- Sends auto notifications via Zoho Cliq
- Manual command replies return JSON for Deluge to wrap into cards
"""

import os
import time
import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from flask import Flask, request, jsonify, send_file
import requests
from pymongo import MongoClient

# Optional PDF export (unchanged)
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
    print("WARNING: ZOHO_BOT_API not set. Auto-notifications may fail until configured.")

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
    except:
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
# OAuth / Zoho messaging (only for auto notifications)
# ---------------------------
ZOHO_LOCK = threading.Lock()

def refresh_access_token():
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

def send_zoho_message(text: str):
    """Used ONLY by timer completion events for auto-notification"""
    if not ZOHO_BOT_API:
        print("ZOHO_BOT_API not set; skipping send_zoho_message.")
        return None
    headers = {
        "Authorization": ZOHO_OAUTH_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {"text": text}
    print("📨 PAYLOAD SENT TO ZOHO:", payload)
    try:
        r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        if r.status_code == 401:
            print("Zoho 401 — attempting refresh.")
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        print("Zoho send status:", getattr(r, "status_code", None), getattr(r, "text", None))
        return r
    except Exception as e:
        print("send_zoho_message error:", e)
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

                # Auto-notification for completion
                send_zoho_message(
                    f"⏰ Pomodoro completed!\n✔ Task: **{hist_item['task']}** ({hist_item['duration']} min)\n\n"
                    f"🔥 Streak: {current_streak} days\n🏆 Longest streak: {longest} days\n\n"
                    f"🎯 XP earned: +{gained}\n💠 Total XP: {total_xp}\n⭐ Level: {level}"
                )

                # auto-start break
                completed_today = count_pomodoros_today(user)
                auto_break_min = 15 if (completed_today % 4 == 0) else 5
                break_ends_at = now_ts() + auto_break_min * 60
                set_active_timer(user, {"type": "break", "ends_at": break_ends_at, "task": f"Auto Break ({auto_break_min} min)", "duration": auto_break_min})
                send_zoho_message(f"☕ Auto-break started for {auto_break_min} minutes. (Type `stop break` to cancel.)")

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
                    send_zoho_message(f"⏰ Break over — resuming **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.")
                    continue
                else:
                    remove_active_timer(user)
                    send_zoho_message("☕ Break over! Ready to get back to work.")
                    q = load_tasks_for_user(user)
                    if q:
                        next_task = q[0]
                        send_zoho_message(f"⏭ Next task in queue: **{next_task['task']}** ({next_task['duration']} min). Type `start next` to continue.")
                    break
            else:
                remove_active_timer(user)
                break
        else:
            time.sleep(min(1.0, max(0.1, remaining)))
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
# Main routes
# ---------------------------
@app.route("/", methods=["GET","HEAD"])
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

    if cmd == "tasks":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in queue."})
        out = "📋 Task Queue:\n"
        for i, t in enumerate(q, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out})

    if cmd == "start next":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in the queue."})
        next_task = q.pop(0)
        save_tasks_for_user(user, q)
        duration = next_task["duration"]
        task_name = next_task["task"]
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        return jsonify({"reply": f"▶️ Started next task: **{task_name}** ({duration} min)"})

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

    if cmd == "clear tasks":
        save_tasks_for_user(user, [])
        return jsonify({"reply": "🗑 Cleared all tasks."})

    if cmd.startswith("break"):
        parts = raw.split()
        if len(parts) == 1:
            minutes = 5
        elif len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
        else:
            return jsonify({"reply": "Usage: break OR break <minutes>"})
        cur = get_active_timer(user)
        paused = None
        if cur and cur.get("type") == "pomodoro":
            remaining = max(0, cur.get("ends_at", 0) - now_ts())
            paused = {"task": cur.get("task"), "remaining_seconds": remaining}
            remove_active_timer(user)
        schedule_timer(user, "break", minutes, f"Manual Break ({minutes} min)", paused_pomodoro=paused)
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."})

    if cmd in ("stop break", "stopbreak", "break stop"):
        cur = get_active_timer(user)
        if not cur or cur.get("type") != "break":
            return jsonify({"reply": "❌ No active break to stop."})
        paused = cur.get("paused_pomodoro")
        remove_active_timer(user)
        if paused:
            remaining = int(paused.get("remaining_seconds", 0))
            schedule_timer(user, "pomodoro", remaining/60.0, paused.get("task"))
            return jsonify({"reply": f"▶️ Break stopped. Resumed **{paused.get('task')}**."})
        else:
            return jsonify({"reply": "🛑 Break stopped."})

    if cmd.startswith("start"):
        parts = raw.split()
        duration = 25
        task_name = "Untitled Task"
        if len(parts) >= 2:
            if parts[1].isdigit():
                duration = int(parts[1])
                task_name = " ".join(parts[2:]) or "Untitled Task"
            else:
                task_name = " ".join(parts[1:]) or "Untitled Task"
        # fixed: now respects duration even when user says start 1
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"})

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

    if cmd in ("stop", "end", "cancel"):
        cur = get_active_timer(user)
        if cur:
            remove_active_timer(user)
            return jsonify({"reply": "🛑 Stopped."})
        return jsonify({"reply": "❌ No active session."})

    if cmd == "resume":
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
            paused = cur.get("paused_pomodoro")
            remaining = paused.get("remaining_seconds", 0)
            schedule_timer(user, "pomodoro", remaining/60.0, paused.get("task"))
            return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
        return jsonify({"reply": "❌ Nothing to resume."})

    if cmd == "today":
        return jsonify({"reply": build_daily_summary(user)})

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

    if cmd in ("chart", "weekly chart", "analytics", "weekly analytics"):
        return jsonify({"reply": get_weekly_chart(user)})

    if cmd == "streak":
        s = load_user_stats(user)
        return jsonify({"reply": f"🔥 Streak: {s.get('current_streak', 0)} days\n🏆 Longest: {s.get('longest_streak', 0)} days"})

    if cmd == "score":
        s = load_user_stats(user)
        return jsonify({"reply": f"🎯 XP: {s.get('xp', 0)}\n⭐ Level: {s.get('level', 1)}"})

    if cmd.startswith("export"):
        return handle_export_command(user, cmd)

    if cmd in ("suggest", "ai suggest", "suggestions"):
        items = smart_suggestions(user)
        reply = "🤖 *Here are some suggestions:* \n"
        for i in items:
            reply += f"• {i}\n"
        return jsonify({"reply": reply})

    # fallback
    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"})

# ---------------------------
# Export / PDF helpers etc. (unchanged)
# ---------------------------

# Boot
if __name__ == "__main__":
    print("Starting server-driven Pomodoro bot.")
    rehydrate_timers()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
