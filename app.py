#!/usr/bin/env python3
"""
Pomodoro Bot - full rebuilt app.py
- Active timers stored in timers.json for fast read/writes.
- MongoDB for persistent history, tasks and user stats.
- Background watcher processes expirations every second.
- /tick endpoint available for external cron if needed (Render free plan).
- Zoho Cliq incoming webhook integration with OAuth refresh support.
"""

import os
import time
import json
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
from typing import Optional, Tuple, Dict, Any

import requests
from flask import Flask, request, jsonify, send_file, abort
from pymongo import MongoClient

# Optional PDF export (keep original behavior)
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pomodoro_bot")

# ---------------------------
# Config / env (set in Render)
# ---------------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "pomodoro_db")

# Default Zoho incoming URL you provided; still allow override by env
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL", "https://cliq.zoho.com/api/v2/bots/pomodorobot/incoming")
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")
# Optional static header token (set to "Zoho-oauthtoken <token>")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN", "")

# Timezone
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Asia/Kolkata")
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc

REPORT_DIR = "reports"
os.makedirs(REPORT_DIR, exist_ok=True)

# timers.json path (local)
TIMERS_JSON = Path("timers.json")
if not TIMERS_JSON.exists():
    TIMERS_JSON.write_text("{}")

# ---------------------------
# App & in-memory
# ---------------------------
app = Flask(__name__)
timers_lock = threading.Lock()
# timers: dict user -> { type, end (epoch int), task, duration, paused_pomodoro }
timers: Dict[str, Dict[str, Any]] = {}

# OAuth token state
ACCESS_TOKEN_ISSUED_AT = 0.0
TOKEN_LIFETIME = 3600
TOKEN_REFRESH_THRESHOLD = 50 * 60  # refresh when <=10 minutes left
token_refresh_lock = threading.Lock()

# ---------------------------
# Mongo setup
# ---------------------------
if not MONGO_URI:
    log.error("MONGO_URI environment variable not set. Exiting.")
    raise RuntimeError("MONGO_URI environment variable not set.")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
col_history = db.get_collection("history")
col_tasks = db.get_collection("tasks")
col_users = db.get_collection("users")
col_timers_debug = db.get_collection("timers_debug")

# ---------------------------
# Time helpers
# ---------------------------
def now_utc() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)

def dt_to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

def epoch_to_dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)

def iso_to_dt(iso) -> Optional[datetime]:
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        try:
            return epoch_to_dt(int(float(iso)))
        except Exception:
            return None

# ---------------------------
# timers.json file helpers
# ---------------------------
def load_timers_file() -> Dict[str, Dict[str, Any]]:
    try:
        txt = TIMERS_JSON.read_text()
        raw = json.loads(txt or "{}")
    except Exception as e:
        log.warning("Failed reading timers.json: %s", e)
        raw = {}
    normalized = {}
    for uid, info in raw.items():
        try:
            end = info.get("end")
            if isinstance(end, str):
                dt = iso_to_dt(end)
                if dt:
                    end_epoch = dt_to_epoch(dt)
                else:
                    end_epoch = int(float(end))
            else:
                end_epoch = int(end)
            normalized[uid] = {
                "type": info.get("type", "pomodoro"),
                "end": int(end_epoch),
                "task": info.get("task", "Untitled Task"),
                "duration": int(info.get("duration", 25)),
                "paused_pomodoro": info.get("paused_pomodoro", None)
            }
        except Exception:
            log.exception("Skipping malformed timer for user %s", uid)
            continue
    return normalized

def save_timers_file(data: dict):
    tmp = TIMERS_JSON.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(TIMERS_JSON)
    except Exception as e:
        log.error("Failed to write timers.json: %s", e)

def load_timers_into_memory():
    global timers
    with timers_lock:
        timers = load_timers_file()
    log.info("Loaded %d timer(s) into memory.", len(timers))

def save_timers_from_memory():
    with timers_lock:
        save_timers_file(timers)

# ---------------------------
# DB mirror helpers
# ---------------------------
def set_active_timer_in_db(user_id: str, timer_obj: dict):
    try:
        doc = timer_obj.copy()
        end = doc.get("end")
        if isinstance(end, int):
            doc["end"] = epoch_to_dt(end).isoformat()
        col_timers_debug.find_one_and_update({"user": user_id}, {"$set": {**doc, "user": user_id}}, upsert=True)
    except Exception:
        log.exception("DB mirror write error")

def remove_active_timer_from_db(user_id: str):
    try:
        col_timers_debug.delete_one({"user": user_id})
    except Exception:
        log.exception("DB mirror remove error")

# ---------------------------
# Mongo helpers: history, tasks, users
# ---------------------------
def append_history(item: dict):
    try:
        col_history.insert_one(item)
    except Exception:
        log.exception("append_history error")

def load_tasks_for_user(user_id: str):
    try:
        doc = col_tasks.find_one({"user": user_id})
        return doc.get("queue", []) if doc else []
    except Exception:
        log.exception("load_tasks_for_user error")
        return []

def save_tasks_for_user(user_id: str, queue):
    try:
        col_tasks.find_one_and_update({"user": user_id}, {"$set": {"queue": queue}}, upsert=True)
    except Exception:
        log.exception("save_tasks_for_user error")

def load_user_stats(user_id: str):
    try:
        doc = col_users.find_one({"user": user_id})
        if not doc:
            return {"xp": 0, "level": 1, "current_streak": 0, "longest_streak": 0, "last_completed_date": None}
        return {
            "xp": doc.get("xp", 0),
            "level": doc.get("level", 1),
            "current_streak": doc.get("current_streak", 0),
            "longest_streak": doc.get("longest_streak", 0),
            "last_completed_date": doc.get("last_completed_date")
        }
    except Exception:
        log.exception("load_user_stats error")
        return {"xp": 0, "level": 1, "current_streak": 0, "longest_streak": 0, "last_completed_date": None}

def save_user_stats(user_id: str, stats: dict):
    try:
        col_users.find_one_and_update({"user": user_id}, {"$set": stats}, upsert=True)
    except Exception:
        log.exception("save_user_stats error")

# ---------------------------
# Streaks & XP
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
    try:
        return col_history.count_documents({"user": user_id, "date": today, "type": "pomodoro"})
    except Exception:
        log.exception("count_pomodoros_today error")
        return 0

# ---------------------------
# Zoho OAuth + send (robust)
# ---------------------------
def _now_ts() -> float:
    return time.time()

def refresh_access_token(force: bool = False) -> bool:
    """
    Refreshes ZOHO_OAUTH_TOKEN using CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN.
    If ZOHO_OAUTH_TOKEN is provided in env, we accept that as a static token.
    Returns True on success (token present), False otherwise.
    """
    global ZOHO_OAUTH_TOKEN, ACCESS_TOKEN_ISSUED_AT
    now_ts = _now_ts()
    if not force and ACCESS_TOKEN_ISSUED_AT and (now_ts - ACCESS_TOKEN_ISSUED_AT) < TOKEN_REFRESH_THRESHOLD:
        return True

    with token_refresh_lock:
        now_ts = _now_ts()
        if not force and ACCESS_TOKEN_ISSUED_AT and (now_ts - ACCESS_TOKEN_ISSUED_AT) < TOKEN_REFRESH_THRESHOLD:
            return True

        if not ZOHO_INCOMING_URL:
            log.warning("ZOHO_INCOMING_URL not set; will skip outgoing messages.")
            return False

        # If environment provided static token, use it
        if ZOHO_OAUTH_TOKEN:
            # ensure prefix
            if not ZOHO_OAUTH_TOKEN.lower().startswith("zoho-oauthtoken"):
                ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + ZOHO_OAUTH_TOKEN
            log.info("Using static ZOHO_OAUTH_TOKEN from environment.")
            return True

        if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
            log.error("CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN missing and no static token provided.")
            return False

        url = "https://accounts.zoho.com/oauth/v2/token"
        payload = {
            "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token"
        }
        try:
            r = requests.post(url, data=payload, timeout=10)
            try:
                data = r.json()
            except Exception:
                log.error("Failed parsing token response JSON: %s", r.text)
                return False
            if "access_token" in data:
                ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + data["access_token"]
                ACCESS_TOKEN_ISSUED_AT = _now_ts()
                log.info("🔄 Access token refreshed at %s", datetime.utcnow().isoformat())
                return True
            else:
                log.error("Refresh token error: %s", data)
                return False
        except Exception:
            log.exception("Exception refreshing token")
            return False

def send_message(text: str, max_retries: int = 1) -> bool:
    """
    Send a plain text message to ZOHO_INCOMING_URL.
    Returns True on success (HTTP 2xx), False otherwise.
    """
    if not ZOHO_INCOMING_URL:
        log.error("ZOHO_INCOMING_URL not configured; cannot send message.")
        return False

    if not refresh_access_token():
        log.error("Could not ensure access token for sending message.")
        return False

    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}
    payload = {"text": text}
    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        try:
            r = requests.post(ZOHO_INCOMING_URL, json=payload, headers=headers, timeout=10)
        except Exception as e:
            log.warning("send_message request exception (attempt %d): %s", attempt, e)
            r = None
        if r is None:
            time.sleep(0.5)
            continue
        # Log response for debugging
        log.info("send_message response status=%s body=%s", r.status_code, (r.text or "")[:800])
        if r.status_code in (200, 201, 204):
            return True
        text_lower = (r.text or "").lower()
        if r.status_code == 401 or "invalid_oauth" in text_lower or "invalid token" in text_lower:
            log.warning("Unauthorized response from Zoho - attempting forced refresh.")
            if refresh_access_token(force=True):
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                time.sleep(0.5)
                continue
            else:
                log.error("Forced refresh failed after unauthorized response.")
                return False
        # For other statuses, don't retry repeatedly
        return False
    return False

# ---------------------------
# Timer start helpers
# ---------------------------
def start_pomodoro(user_id: str, duration: int, task: str):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    end = now + timedelta(minutes=int(duration))
    with timers_lock:
        timers[user_id] = {
            "type": "pomodoro",
            "task": task,
            "duration": int(duration),
            "end": int(end.timestamp()),
            "paused_pomodoro": None
        }
        save_timers_from_memory()
    set_active_timer_in_db(user_id, timers[user_id])
    log.info("[TIMER] Started pomodoro for %s: %s (%dm) -> %s", user_id, task, duration, end.isoformat())
    return timers[user_id]

def start_break(user_id: str, minutes: int, paused_pomodoro: Optional[dict] = None, label: Optional[str] = None):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    end = now + timedelta(minutes=int(minutes))
    with timers_lock:
        timers[user_id] = {
            "type": "break",
            "task": label or f"Manual Break ({minutes} min)",
            "duration": int(minutes),
            "end": int(end.timestamp()),
            "paused_pomodoro": paused_pomodoro
        }
        save_timers_from_memory()
    set_active_timer_in_db(user_id, timers[user_id])
    log.info("[TIMER] Started break for %s: %dm -> %s", user_id, minutes, end.isoformat())
    return timers[user_id]

# ---------------------------
# Timer expiry processing
# ---------------------------
def process_timer_expiry(user_id: str, info: dict):
    """
    Called when timer expired. Handles history, XP/streak update, notifications, and auto-break/resume.
    """
    try:
        ttype = info.get("type", "pomodoro")
        if ttype == "pomodoro":
            task = info.get("task", "Untitled Task")
            duration = int(info.get("duration", 25))
            completed_at = now_utc()
            hist_item = {
                "user": user_id,
                "task": task,
                "duration": duration,
                "completed_at": completed_at.isoformat(),
                "date": completed_at.strftime("%Y-%m-%d"),
                "type": "pomodoro"
            }
            append_history(hist_item)

            current_streak, longest = update_streak_for_user(user_id)
            gained, total_xp, level = update_score(user_id, duration, current_streak)

            # Notifications
            send_message(
                f"⏰ Pomodoro completed!\n"
                f"✔ Task: **{task}** ({duration} min)\n\n"
                f"🔥 Streak: {current_streak} days\n"
                f"🏆 Longest streak: {longest} days\n\n"
                f"🎯 XP earned: +{gained}\n"
                f"💠 Total XP: {total_xp}\n"
                f"⭐ Level: {level}"
            )

            # auto-break (5 or 15)
            completed_today = count_pomodoros_today(user_id)
            auto_break_min = 15 if (completed_today % 4 == 0) else 5
            start_break(user_id, auto_break_min, paused_pomodoro=None, label=f"Auto Break ({auto_break_min} min)")
            send_message(f"☕ Auto-break started for {auto_break_min} minutes. (Type `stop break` to cancel.)")

        elif ttype == "break":
            br_task = info.get("task", "Break")
            br_duration = int(info.get("duration", 5))
            paused = info.get("paused_pomodoro")

            completed_at = now_utc()
            hist_item = {
                "user": user_id,
                "task": br_task,
                "duration": br_duration,
                "completed_at": completed_at.isoformat(),
                "date": completed_at.strftime("%Y-%m-%d"),
                "type": "break"
            }
            append_history(hist_item)

            if paused:
                remaining = int(paused.get("remaining_seconds", 0))
                new_end = now_utc() + timedelta(seconds=remaining)
                with timers_lock:
                    timers[user_id] = {
                        "type": "pomodoro",
                        "end": int(new_end.timestamp()),
                        "task": paused.get("task"),
                        "duration": round(remaining / 60, 2),
                        "paused_pomodoro": None
                    }
                    save_timers_from_memory()
                set_active_timer_in_db(user_id, timers[user_id])
                send_message(f"⏰ Break over — resuming **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.")
            else:
                send_message("☕ Break over! Ready to get back to work.")
                queue = load_tasks_for_user(user_id)
                if queue:
                    next_task = queue[0]
                    send_message(f"⏭ Next task in queue: **{next_task['task']}** ({next_task['duration']} min). Type `start next` to continue.")
                # remove timer if any
                with timers_lock:
                    if user_id in timers:
                        timers.pop(user_id, None)
                        save_timers_from_memory()
                remove_active_timer_from_db(user_id)
    except Exception:
        log.exception("Error in process_timer_expiry for user %s", user_id)

# ---------------------------
# Background watcher (robust)
# ---------------------------
def watcher_loop():
    log.info("▶️ JSON timers watcher started (1s).")
    last_reload = 0.0
    while True:
        try:
            now_epoch = int(time.time())
            # periodically reload timers.json to pick up external changes
            if time.time() - last_reload > 10:
                try:
                    loaded = load_timers_file()
                    with timers_lock:
                        for k, v in loaded.items():
                            if k not in timers:
                                timers[k] = v
                    last_reload = time.time()
                except Exception:
                    log.exception("Error reloading timers file")

            to_process = []
            with timers_lock:
                for uid, info in list(timers.items()):
                    try:
                        if int(info.get("end", 0)) <= now_epoch:
                            to_process.append((uid, dict(info)))
                    except Exception:
                        log.exception("Malformed timer for %s, removing", uid)
                        timers.pop(uid, None)
                        save_timers_from_memory()

            if to_process:
                log.info("Found %d expired timer(s) to process.", len(to_process))

            for uid, info in to_process:
                try:
                    with timers_lock:
                        if uid in timers:
                            timers.pop(uid, None)
                            save_timers_from_memory()
                    remove_active_timer_from_db(uid)
                    process_timer_expiry(uid, info)
                except Exception:
                    log.exception("Error processing expired timer for %s", uid)
        except Exception:
            log.exception("Watcher loop top-level exception")
        time.sleep(1)

# ---------------------------
# Request parsing + endpoints
# ---------------------------
def parse_incoming_request(data: dict) -> Tuple[Optional[str], str]:
    if not isinstance(data, dict):
        return None, ""
    # find text
    text = ""
    for key in ("raw", "message", "msg", "text", "raw_message", "raw_msg", "message_details", "rawMessage"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    if not text and "message_details" in data and isinstance(data["message_details"], dict):
        for k in ("raw_message", "message", "msg"):
            vv = data["message_details"].get(k)
            if isinstance(vv, str) and vv.strip():
                text = vv.strip()
                break

    # find user id
    user = None
    if data.get("user") and isinstance(data.get("user"), dict):
        user = data["user"].get("id") or data["user"].get("user_id")
    if not user:
        user = data.get("user") or data.get("user_id") or data.get("userid") or data.get("sender")
    if isinstance(user, dict):
        user = user.get("id")
    return user, text

def parse_start_command(text: str):
    parts = text.strip().split()
    duration = 25
    task = "Untitled Task"
    if len(parts) >= 2 and parts[0].lower() == "start":
        if len(parts) >= 3 and parts[1].isdigit():
            duration = int(parts[1])
            task = " ".join(parts[2:])
        else:
            if parts[1].isdigit():
                duration = int(parts[1])
                task = " ".join(parts[2:]) or "Untitled Task"
            else:
                task = " ".join(parts[1:]) or "Untitled Task"
    return duration, task

@app.route("/pomodoro", methods=["POST"])
def pomodoro_route():
    data = request.json or {}
    user, raw = parse_incoming_request(data)
    if not user:
        return jsonify({"reply": "❌ Missing user id."}), 400
    raw_lower = (raw or "").strip().lower()

    # ADD TASK
    if raw_lower.startswith("add task"):
        parts = raw.strip().split()
        if len(parts) < 4:
            return jsonify({"reply": "Usage: add task <task name> <duration> (minutes)."})
        duration = parts[-1]
        if not duration.isdigit():
            return jsonify({"reply": "Duration must be a number (minutes)."})
        duration = int(duration)
        task_name = " ".join(parts[2:-1])
        queue = load_tasks_for_user(user)
        queue.append({"task": task_name, "duration": duration})
        save_tasks_for_user(user, queue)
        return jsonify({"reply": f"📝 Added task: **{task_name}** ({duration} min)"})

    # SHOW TASKS
    if raw_lower == "tasks":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in queue."})
        out = "📋 Task Queue:\n"
        for i, t in enumerate(q, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out})

    # START NEXT
    if raw_lower == "start next":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in the queue."})
        next_task = q.pop(0)
        save_tasks_for_user(user, q)
        duration = next_task["duration"]
        task_name = next_task["task"]
        start_pomodoro(user, duration, task_name)
        return jsonify({"reply": f"▶️ Started next task: **{task_name}** ({duration} min)"})

    # DONE (remove from queue)
    if raw_lower.startswith("done"):
        parts = raw_lower.split()
        if len(parts) != 2 or not parts[1].isdigit():
            return jsonify({"reply": "Usage: done <task_number>"})
        index = int(parts[1]) - 1
        q = load_tasks_for_user(user)
        if index < 0 or index >= len(q):
            return jsonify({"reply": "❌ Invalid task number."})
        removed = q.pop(index)
        save_tasks_for_user(user, q)
        return jsonify({"reply": f"✔ Removed task: **{removed['task']}**"})

    # CLEAR TASKS
    if raw_lower == "clear tasks":
        save_tasks_for_user(user, [])
        return jsonify({"reply": "🗑 Cleared all tasks."})

    # BREAK
    if raw_lower.startswith("break"):
        parts = raw.strip().split()
        if len(parts) == 1:
            minutes = 5
        elif len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
        else:
            return jsonify({"reply": "Usage: break OR break <minutes>"})
        with timers_lock:
            cur = timers.get(user)
            paused_pomodoro = None
            if cur and cur.get("type") == "pomodoro":
                remaining = max(int(cur["end"] - int(time.time())), 0)
                paused_pomodoro = {"task": cur.get("task"), "remaining_seconds": remaining}
                timers.pop(user, None)
                save_timers_from_memory()
            start_break(user, minutes, paused_pomodoro=paused_pomodoro, label=f"Manual Break ({minutes} min)")
        send_message(f"☕ Break started for {minutes} minutes. (Type `stop break` to cancel and resume.)")
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."})

    # STOP BREAK
    if raw_lower in ("stop break", "stopbreak", "break stop"):
        with timers_lock:
            cur = timers.get(user)
            if not cur or cur.get("type") != "break":
                return jsonify({"reply": "❌ No active break to stop."})
            paused = cur.get("paused_pomodoro")
            timers.pop(user, None)
            save_timers_from_memory()
            remove_active_timer_from_db(user)
            if paused:
                remaining = int(paused.get("remaining_seconds", 0))
                end_epoch = int((now_utc() + timedelta(seconds=remaining)).timestamp())
                timers[user] = {"type": "pomodoro", "end": end_epoch, "task": paused.get("task"), "duration": round(remaining/60,2), "paused_pomodoro": None}
                save_timers_from_memory()
                set_active_timer_in_db(user, timers[user])
                return jsonify({"reply": f"▶️ Break stopped. Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
            else:
                return jsonify({"reply": "🛑 Break stopped."})

    # START Pomodoro
    if raw_lower.startswith("start"):
        duration, task_name = parse_start_command(raw)
        start_pomodoro(user, duration, task_name)
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"})

    # STATUS
    if raw_lower in ("status", "time", "progress"):
        with timers_lock:
            cur = timers.get(user)
            if not cur:
                return jsonify({"reply": "❌ No active session."})
            remaining = max(int(cur["end"] - int(time.time())), 0)
            if cur.get("type") == "pomodoro":
                return jsonify({"reply": f"🍅 {cur['task']} — {remaining//60}m {remaining%60}s left"})
            else:
                return jsonify({"reply": f"☕ Break — {remaining//60}m {remaining%60}s left"})

    # STOP / CANCEL
    if raw_lower in ("stop", "end", "cancel"):
        with timers_lock:
            if user in timers:
                timers.pop(user, None)
                save_timers_from_memory()
                remove_active_timer_from_db(user)
                return jsonify({"reply": "🛑 Stopped."})
        return jsonify({"reply": "❌ No active session."})

    # RESUME (from paused break)
    if raw_lower == "resume":
        with timers_lock:
            cur = timers.get(user)
            if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
                paused = cur.get("paused_pomodoro")
                remaining = int(paused.get("remaining_seconds", 0))
                end_epoch = int((now_utc() + timedelta(seconds=remaining)).timestamp())
                timers[user] = {"type":"pomodoro","end":end_epoch,"task":paused.get("task"),"duration":round(remaining/60,2),"paused_pomodoro":None}
                save_timers_from_memory()
                set_active_timer_in_db(user, timers[user])
                return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
        return jsonify({"reply": "❌ Nothing to resume."})

    # TODAY SUMMARY
    if raw_lower == "today":
        return jsonify({"reply": build_daily_summary(user)})

    # WEEK
    if raw_lower == "week":
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

    # CHART / ANALYTICS
    if raw_lower in ("chart", "weekly chart", "analytics", "weekly analytics"):
        text = get_weekly_chart(user)
        return jsonify({"reply": text})

    # STREAK
    if raw_lower == "streak":
        s = load_user_stats(user)
        return jsonify({"reply": f"🔥 Streak: {s.get('current_streak',0)} days\n🏆 Longest: {s.get('longest_streak',0)} days"})

    # SCORE
    if raw_lower == "score":
        s = load_user_stats(user)
        return jsonify({"reply": f"🎯 XP: {s.get('xp',0)}\n⭐ Level: {s.get('level',1)}"})

    # EXPORT commands routed to /export for PDF generation
    if raw_lower.startswith("export"):
        return export_report_internal(user, raw_lower)

    # SUGGESTIONS
    if raw_lower in ("suggest", "ai suggest", "suggestions"):
        items = smart_suggestions(user)
        reply = "🤖 *Here are some suggestions:* \n"
        for i in items:
            reply += f"• {i}\n"
        return jsonify({"reply": reply})

    # fallback/help
    return jsonify({"reply":"Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"})

# ---------------------------
# Analytics / export helpers
# ---------------------------
def get_weekly_chart(user_id: str):
    docs = list(col_history.find({"user": user_id}))
    if not docs:
        return "📭 No history available."
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    weekly = {d: {"count":0, "minutes":0} for d in days}
    for h in docs:
        try:
            dt = datetime.fromisoformat(h.get("completed_at"))
        except Exception:
            try:
                dt = datetime.strptime(h.get("date"), "%Y-%m-%d")
            except:
                dt = datetime.utcnow()
        weekday = days[dt.weekday()]
        weekly[weekday]["count"] += 1
        weekly[weekday]["minutes"] += int(h.get("duration",0))
    out = "📊 **WEEKLY ANALYTICS**\n\n"
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

def build_daily_summary(user_id: str):
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

def smart_suggestions(user_id: str):
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
# PDF export
# ---------------------------
def create_pdf(filepath, title, lines):
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab not installed")
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, height - 80, title)
    c.setFont("Helvetica", 12)
    y = height - 120
    for line in lines:
        if y < 50:
            c.showPage()
            c.setFont("Helvetica", 12)
            y = height - 50
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
            t = h.get("task","")
            d = h.get("duration",0)
            tt = h.get("type","pomodoro")
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
                total += int(h.get("duration",0))
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
                total += int(h.get("duration",0))
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

def export_report_internal(user: str, raw_lower: str):
    if not REPORTLAB_AVAILABLE:
        return jsonify({"reply":"❌ PDF export not available (reportlab missing)."}), 500
    if raw_lower in ("export today", "export daily"):
        fp = generate_daily_report(user)
    elif raw_lower in ("export week", "export weekly"):
        fp = generate_weekly_report(user)
    elif raw_lower in ("export month", "export monthly"):
        fp = generate_monthly_report(user)
    else:
        return jsonify({"reply":"❌ Use: export today | export week | export month"})
    link = request.host_url.rstrip("/") + "/download/" + os.path.basename(fp)
    return jsonify({"reply": f"📄 Report ready! Download: {link}"})

@app.route("/export", methods=["POST"])
def export_route():
    data = request.json or {}
    user, raw = parse_incoming_request(data)
    if not user:
        return jsonify({"reply": "Missing user id."}), 400
    return export_report_internal(user, raw.lower().strip())

# ---------------------------
# Health, debug, tick endpoints
# ---------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()}), 200

@app.route("/_timers", methods=["GET"])
def dump_timers():
    with timers_lock:
        return jsonify(timers)

@app.route("/tick", methods=["GET", "POST"])
def tick():
    """
    External cron can call this (every 15-30s) as fallback in environments
    where background threads may be unreliable.
    """
    now_epoch = int(time.time())
    processed = 0
    to_process = []
    with timers_lock:
        for uid, info in list(timers.items()):
            try:
                if int(info.get("end", 0)) <= now_epoch:
                    to_process.append((uid, dict(info)))
            except Exception:
                timers.pop(uid, None)
                save_timers_from_memory()
    for uid, info in to_process:
        with timers_lock:
            if uid in timers:
                timers.pop(uid, None)
                save_timers_from_memory()
        remove_active_timer_from_db(uid)
        try:
            process_timer_expiry(uid, info)
            processed += 1
        except Exception:
            log.exception("tick processing error for %s", uid)
    return jsonify({"status": "ok", "processed": processed}), 200

# quick test helper: create a short timer (for testing only)
@app.route("/test-timer", methods=["POST"])
def test_timer():
    data = request.json or {}
    user = data.get("user") or "testuser"
    secs = int(data.get("seconds", 15))
    kind = data.get("type", "pomodoro")
    if kind == "pomodoro":
        minutes = max(1, int(secs / 60)) if secs >= 60 else (secs / 60)
        # For testing, allow sub-minute seconds by setting end directly
        end_epoch = int(time.time() + secs)
        with timers_lock:
            timers[user] = {"type": "pomodoro", "task": data.get("task", "Test Task"), "duration": round(secs/60,2), "end": end_epoch, "paused_pomodoro": None}
            save_timers_from_memory()
        set_active_timer_in_db(user, timers[user])
        return jsonify({"reply": f"Test pomodoro created for {secs}s for user {user}."})
    else:
        end_epoch = int(time.time() + secs)
        with timers_lock:
            timers[user] = {"type": "break", "task": data.get("task", f"Test Break {secs}s"), "duration": round(secs/60,2), "end": end_epoch, "paused_pomodoro": None}
            save_timers_from_memory()
        set_active_timer_in_db(user, timers[user])
        return jsonify({"reply": f"Test break created for {secs}s for user {user}."})

# ---------------------------
# Boot: load timers + start watcher
# ---------------------------
try:
    load_timers_into_memory()
except Exception:
    log.exception("load_timers_into_memory failed")

try:
    watcher_thread = threading.Thread(target=watcher_loop, daemon=True, name="timers-watcher")
    watcher_thread.start()
    log.info("▶️ Started background watcher thread.")
except Exception:
    log.exception("Could not start watcher thread")

# try a refresh at boot (best-effort)
try:
    refresh_access_token(force=True)
except Exception:
    log.warning("refresh token at boot failed (continuing)")

# ---------------------------
# Main - dev server fallback
# ---------------------------
if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.getenv("PORT", 5000))
    log.info("Starting Flask dev server on %s:%d", host, port)
    app.run(host=host, port=port)
