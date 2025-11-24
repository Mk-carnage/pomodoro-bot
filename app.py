# app.py
"""
Pomodoro bot (server-driven timers)
- Uses MongoDB for persistence
- Sends messages to Zoho via POST { "text": "..." } to the bot message endpoint
- No bot_callback usage — server schedules and sends messages when timers expire
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

# Zoho bot API (message endpoint)
# e.g. https://cliq.zoho.com/api/v2/bots/<botunique>/message
ZOHO_BOT_API = os.getenv("ZOHO_BOT_API", "")
# OAuth token string in the form 'Zoho-oauthtoken <token>'
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN", "")
# refresh token flow params (optional, for server to refresh)
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
col_timers = db.get_collection("timers")      # { user, type, ends_at, task, duration, paused_pomodoro }
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
ZOHO_LOCK = threading.Lock()  # protect ZOHO_OAUTH_TOKEN update

def refresh_access_token():
    """Use refresh_token to update ZOHO_OAUTH_TOKEN (in-memory only)."""
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

def send_zoho_message(text: str, user_hint: str = None):
    """
    Sends a simple message to Zoho bot message API.
    Payload: { "text": "..." }  (Zoho requires 'text' key)
    """
    if not ZOHO_BOT_API:
        print("ZOHO_BOT_API not set; skipping send_zoho_message.")
        return None

    headers = {
        "Authorization": ZOHO_OAUTH_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {"text": text}

    # helpful debug
    print("📨 PAYLOAD SENT TO ZOHO:", payload)

    try:
        r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        if r.status_code == 401:
            # try refresh once
            print("Zoho 401 — attempting refresh.")
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        print("Zoho send status:", getattr(r, "status_code", None), getattr(r, "text", None))
        return r
    except Exception as e:
        print("send_zoho_message error:", e)
        return None



# Real AI code 

HF_TOKEN = os.getenv("HF_TOKEN")
MODEL_ID = os.getenv("HF_MODEL_ID", "meta-llama/Meta-Llama-3-8B-Instruct")

def hf_ai_suggestions(user_id):
    history = list(col_history.find({"user": user_id}).sort("completed_at", -1).limit(50))
    tasks = load_tasks_for_user(user_id)
    stats = load_user_stats(user_id)

    hist_text = "\n".join(f"{h.get('date')} – {h.get('task')} ({h.get('duration')} min)" for h in history)
    task_text = "\n".join(f"{t['task']} ({t['duration']} min)" for t in tasks)

    prompt = (
        f"You are a productivity assistant.\n\n"
        f"User history:\n{hist_text or 'No history'}\n\n"
        f"Pending tasks:\n{task_text or 'No tasks'}\n\n"
        f"Stats: XP={stats.get('xp')} Level={stats.get('level')} Streak={stats.get('current_streak')}\n\n"
        "Give 5 short actionable suggestions to help the user be productive now."
    )

    api_url = f"https://router.huggingface.co/models/{MODEL_ID}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": prompt, "parameters": {"max_new_tokens": 150}}

    resp = requests.post(api_url, headers=headers, json=payload, timeout=60)
    j = resp.json()

    suggestion_text = (
        j.get("generated_text")
        or (j[0].get("generated_text") if isinstance(j, list) else None)
        or j.get("choices", [{}])[0].get("text")
        or str(j)
    )

    return suggestion_text.strip()

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
    Worker that monitors the DB entry for user's active timer and fires actions when ends_at reached.
    Allows cancellation by removing DB entry.
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
            # timer complete — process based on type
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

                # notify completion
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

                # loop to continue worker for break
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
                # unknown type - clean up
                remove_active_timer(user)
                break

        else:
            # sleep for a short time but also allow quick reaction to cancel/update
            sleep_for = min(1.0, max(0.1, remaining))
            time.sleep(sleep_for)

    # remove thread from registry
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

def schedule_timer(user: str, typ: str, duration_min: int, task: str, paused_pomodoro=None):
    """
    Store timer in DB and start worker thread for that user.
    typ: "pomodoro" or "break"
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
        # If timer already passed, let worker handle immediate execution by starting thread
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
        # cancel any break
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        send_zoho_message(f"🍅 Started **{task_name}** ({duration} min)")
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
        send_zoho_message(f"☕ Break started for {minutes} minutes.")
        #return jsonify({"reply": f"☕ Break started for {minutes} minutes."})
        

    if cmd in ("stop break", "stopbreak", "break stop"):
        cur = get_active_timer(user)
        if not cur or cur.get("type") != "break":
            return jsonify({"reply": "❌ No active break to stop."})
        paused = cur.get("paused_pomodoro")
        remove_active_timer(user)
        if paused:
            remaining = int(paused.get("remaining_seconds", 0))
            schedule_timer(user, "pomodoro", remaining/60.0, paused.get("task"))
            send_zoho_message(f"▶️ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.")
            return jsonify({"reply": f"▶️ Break stopped. Resumed **{paused.get('task')}**."})
        else:
            return jsonify({"reply": "🛑 Break stopped."})

    if cmd.startswith("start"):
        parts = raw.split()
        
        # Reject input like "start 1"
        if len(parts) == 2 and parts[1].isdigit():
            return jsonify({"reply": "❌ Invalid format.\nUse:\n• start <task>\n• start <minutes> <task>"}), 200
    
        duration = 25
        task_name = "Untitled Task"
    
        # start 25 Task Name
        if len(parts) >= 3 and parts[1].isdigit():
            duration = int(parts[1])
            task_name = " ".join(parts[2:]) or "Untitled Task"
    
        # start Task Name
        elif len(parts) >= 2:
            task_name = " ".join(parts[1:]) or "Untitled Task"
    
        # Cancel break if running
        cur = get_active_timer(user)
        if cur and cur.get("type") == "break":
            remove_active_timer(user)
    
        schedule_timer(user, "pomodoro", duration, task_name)
        #send_zoho_message(f"🍅 Started **{task_name}** ({duration} min)")
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
            send_zoho_message(f"⏯ Resumed **{paused.get('task')}**")
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
        reply = hf_ai_suggestions(user)
        return jsonify({"reply": "🤖 *AI Suggestions:*\n" + reply})

    # fallback
    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"})

# ---------------------------
# Export / PDF helpers (same as before)
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
# Analytics & suggestions (same as before)
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



# ---------------------------
# Boot
# ---------------------------
if __name__ == "__main__":
    print("Starting server-driven Pomodoro bot.")
    rehydrate_timers()
    # Run Flask
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
