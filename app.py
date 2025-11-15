# app.py
import os
import threading
import time
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, send_file, abort
import requests
from pymongo import MongoClient, ReturnDocument

# Optional import for PDF generation
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import inch
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ---------------------------
# Config / env
# ---------------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "pomodoro_db")

ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL", "")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN", "")  # format: "Zoho-oauthtoken xxxxx"
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Asia/Kolkata")
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc

REPORT_DIR = "reports"
os.makedirs(REPORT_DIR, exist_ok=True)

# ---------------------------
# Mongo setup
# ---------------------------
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable not set.")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
col_timers = db.get_collection("timers")      # persistent active timers
col_history = db.get_collection("history")    # all completed sessions
col_tasks = db.get_collection("tasks")        # queued tasks per user
col_users = db.get_collection("users")        # streaks, scores, meta

# Create indexes (optional)
col_timers.create_index("user", unique=True)

# ---------------------------
# In-memory structures
# ---------------------------
app = Flask(__name__)
timers = {}  # user_id -> timer dict (end as datetime aware UTC)
timers_lock = threading.Lock()

# ---------------------------
# Helpers: datetime <-> iso (UTC)
# ---------------------------
def iso_to_dt(iso):
    if iso is None:
        return None
    if isinstance(iso, datetime):
        dt = iso
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    try:
        s = str(iso)
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc)

def dt_to_iso(dt):
    if not isinstance(dt, datetime):
        return dt
    dt_utc = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.isoformat()

# ---------------------------
# Persistence helpers (timers)
# ---------------------------
def set_active_timer(user_id, timer_obj):
    """
    upsert active timer into MongoDB. timer_obj expects 'end' as ISO string or datetime.
    """
    doc = timer_obj.copy()
    end = doc.get("end")
    if isinstance(end, datetime):
        doc["end"] = dt_to_iso(end)
    # store entire doc as-is
    col_timers.find_one_and_update(
        {"user": user_id},
        {"$set": {**doc, "user": user_id}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

def remove_active_timer(user_id):
    col_timers.delete_one({"user": user_id})

def get_active_timer_from_db(user_id):
    return col_timers.find_one({"user": user_id})

def rehydrate_timers():
    """
    Load timers from DB into in-memory timers (convert end to datetime).
    """
    docs = list(col_timers.find({}))
    with timers_lock:
        timers.clear()
        for d in docs:
            try:
                uid = d.get("user")
                end = d.get("end")
                end_dt = iso_to_dt(end)
                timers[uid] = {
                    "type": d.get("type", "pomodoro"),
                    "end": end_dt,
                    "task": d.get("task", "Untitled Task"),
                    "duration": d.get("duration", 25),
                    "paused_pomodoro": d.get("paused_pomodoro", None)
                }
            except Exception as e:
                print("rehydrate error for doc:", d, "->", e)

# ---------------------------
# Other persistence helpers: history, tasks, users
# ---------------------------
def append_history(item):
    # item should contain: user, task, duration, completed_at (ISO), date, type
    col_history.insert_one(item)

def load_tasks_for_user(user_id):
    doc = col_tasks.find_one({"user": user_id})
    return doc.get("queue", []) if doc else []

def save_tasks_for_user(user_id, queue):
    col_tasks.find_one_and_update({"user": user_id}, {"$set": {"queue": queue}}, upsert=True)

def load_user_stats(user_id):
    doc = col_users.find_one({"user": user_id})
    if not doc:
        return {"xp": 0, "level": 1, "current_streak": 0, "longest_streak": 0, "last_completed_date": None}
    # ensure keys exist
    return {
        "xp": doc.get("xp", 0),
        "level": doc.get("level", 1),
        "current_streak": doc.get("current_streak", 0),
        "longest_streak": doc.get("longest_streak", 0),
        "last_completed_date": doc.get("last_completed_date")
    }

def save_user_stats(user_id, stats):
    col_users.find_one_and_update({"user": user_id}, {"$set": stats}, upsert=True)

# ---------------------------
# Zoho token refresh + send message
# ---------------------------
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return False
    url = "https://accounts.zoho.com/oauth/v2/token"
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

def send_message(text):
    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}
    try:
        r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers, timeout=10)
        if r.status_code == 401:
            # try refresh once
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers, timeout=10)
        return r
    except Exception as e:
        print("send_message error:", e)
        return None

# ---------------------------
# Utility: parse incoming payload (compat with Deluge/form)
# ---------------------------
def parse_incoming_request(data):
    """
    Accept many possible payload shapes from Deluge/Zoho.
    - raw text fields: 'raw', 'message', 'msg', 'text', 'raw_message', 'raw_msg', 'raw'
    - user id fields: 'user', 'user_id', or 'user' map with 'id'
    Returns (user_id, raw_text)
    """
    if not isinstance(data, dict):
        return None, ""

    # find text
    for key in ("raw", "message", "msg", "text", "raw_message", "raw_msg", "message_details", "rawMessage"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    else:
        # sometimes body passed as {"message": {"raw_message": "..."}}
        text = ""
        # deeper checks
        if "message_details" in data and isinstance(data["message_details"], dict):
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
    # if user is a map-like string, try parse
    if isinstance(user, dict):
        user = user.get("id")

    return user, text

# ---------------------------
# Core logic: streaks & XP
# ---------------------------
def update_streak_for_user(user_id):
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

def calculate_level(xp):
    if xp < 100: return 1
    if xp < 250: return 2
    if xp < 500: return 3
    if xp < 1000: return 4
    return xp // 500 + 4

def update_score(user_id, duration, streak):
    s = load_user_stats(user_id)
    base_xp = int(duration)
    streak_bonus = streak * 5
    long_bonus = 10 if duration >= 30 else 0
    gained = base_xp + streak_bonus + long_bonus
    s["xp"] = s.get("xp", 0) + gained
    s["level"] = calculate_level(s["xp"])
    save_user_stats(user_id, s)
    return gained, s["xp"], s["level"]

def count_pomodoros_today(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return col_history.count_documents({"user": user_id, "date": today, "type": "pomodoro"})

# ---------------------------
# Timer watcher (robust)
# ---------------------------
def timer_watcher():
    print("⏳ Timer watcher thread started.")
    while True:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        to_process = []

        # collect expired timers and normalize in-memory
        with timers_lock:
            for uid, info in list(timers.items()):
                try:
                    end = info.get("end")
                    if not isinstance(end, datetime):
                        end_dt = iso_to_dt(end)
                        timers[uid]["end"] = end_dt
                    else:
                        end_dt = end if end.tzinfo is not None else end.replace(tzinfo=timezone.utc)

                    if now >= end_dt:
                        to_process.append((uid, dict(timers[uid])))  # snapshot
                except Exception as ex:
                    print("⚠ timer malformed for", uid, "=> removing", ex)
                    timers.pop(uid, None)
                    try:
                        remove_active_timer(uid)
                    except Exception:
                        pass

        # process expirations outside lock
        for uid, info in to_process:
            try:
                info_end = info.get("end")
                if not isinstance(info_end, datetime):
                    info_end = iso_to_dt(info_end)

                ttype = info.get("type", "pomodoro")

                # ---------- Pomodoro Completed ----------
                if ttype == "pomodoro":
                    task = info.get("task", "Untitled Task")
                    duration = int(info.get("duration", 25))

                    # append history (store ISO)
                    completed_at = datetime.utcnow().replace(tzinfo=timezone.utc)
                    hist_item = {
                        "user": uid,
                        "task": task,
                        "duration": duration,
                        "completed_at": dt_to_iso(completed_at),
                        "date": completed_at.strftime("%Y-%m-%d"),
                        "type": "pomodoro"
                    }
                    append_history(hist_item)

                    # update streak & score
                    current_streak, longest = update_streak_for_user(uid)
                    gained, total_xp, level = update_score(uid, duration, current_streak)

                    # notify
                    send_message(
                        f"⏰ Pomodoro completed!\n"
                        f"✔ Task: **{task}** ({duration} min)\n\n"
                        f"🔥 Streak: {current_streak} days\n"
                        f"🏆 Longest streak: {longest} days\n\n"
                        f"🎯 XP earned: +{gained}\n"
                        f"💠 Total XP: {total_xp}\n"
                        f"⭐ Level: {level}"
                    )

                    # create auto-break
                    completed_today = count_pomodoros_today(uid)
                    auto_break_min = 15 if (completed_today % 4 == 0) else 5
                    new_break_end = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(minutes=auto_break_min)

                    with timers_lock:
                        timers[uid] = {
                            "type": "break",
                            "end": new_break_end,
                            "task": f"Auto Break ({auto_break_min} min)",
                            "duration": auto_break_min,
                            "paused_pomodoro": None
                        }

                    # persist break to DB
                    try:
                        set_active_timer(uid, {
                            "type": "break",
                            "end": dt_to_iso(new_break_end),
                            "task": f"Auto Break ({auto_break_min} min)",
                            "duration": auto_break_min,
                            "paused_pomodoro": None
                        })
                    except Exception as e:
                        print("DB write error (set_active_timer):", e)

                    send_message(f"☕ Auto-break started for {auto_break_min} minutes. (Type `stop break` to cancel.)")

                # ---------- Break Completed ----------
                elif ttype == "break":
                    br_task = info.get("task", "Break")
                    br_duration = int(info.get("duration", 5))
                    paused = info.get("paused_pomodoro")

                    # save break history
                    completed_at = datetime.utcnow().replace(tzinfo=timezone.utc)
                    hist_item = {
                        "user": uid,
                        "task": br_task,
                        "duration": br_duration,
                        "completed_at": dt_to_iso(completed_at),
                        "date": completed_at.strftime("%Y-%m-%d"),
                        "type": "break"
                    }
                    append_history(hist_item)

                    if paused:
                        remaining = int(paused.get("remaining_seconds", 0))
                        new_end = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=remaining)
                        with timers_lock:
                            timers[uid] = {
                                "type": "pomodoro",
                                "end": new_end,
                                "task": paused.get("task"),
                                "duration": round(remaining / 60, 2),
                                "paused_pomodoro": None
                            }
                        # persist resumed pomodoro
                        try:
                            set_active_timer(uid, {
                                "type": "pomodoro",
                                "end": dt_to_iso(new_end),
                                "task": paused.get("task"),
                                "duration": round(remaining / 60, 2),
                                "paused_pomodoro": None
                            })
                        except Exception as e:
                            print("DB write error (resume set_active_timer):", e)

                        send_message(f"⏰ Break over — resuming **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.")
                    else:
                        send_message("☕ Break over! Ready to get back to work.")
                        # suggest next queued task if available
                        queue = load_tasks_for_user(uid)
                        if queue:
                            next_task = queue[0]
                            send_message(f"⏭ Next task in queue: **{next_task['task']}** ({next_task['duration']} min). Type `start next` to continue.")
                        # cleanup DB timer
                        try:
                            remove_active_timer(uid)
                        except Exception as e:
                            print("DB remove error:", e)
                        with timers_lock:
                            timers.pop(uid, None)

                # CLEANUP: If the processed timer still matches the in-memory timer (within 1s), remove it
                with timers_lock:
                    cur = timers.get(uid)
                    if cur is None:
                        pass
                    else:
                        cur_end = cur.get("end")
                        if not isinstance(cur_end, datetime):
                            cur_end = iso_to_dt(cur_end)
                        # allow 1 second tolerance
                        if cur.get("type") == info.get("type") and abs((cur_end - info_end).total_seconds()) < 1:
                            timers.pop(uid, None)
                            try:
                                remove_active_timer(uid)
                            except Exception:
                                pass

            except Exception as e:
                print("⚠️ Error processing timer for user", uid, ":", e)

        time.sleep(1)


# ---------------------------
# Command parsing & routes
# ---------------------------
def parse_start_command(text):
    parts = text.strip().split()
    duration = 25
    task = "Untitled Task"
    if len(parts) >= 2 and parts[0].lower() == "start":
        # start <minutes> <task...>  OR start <task...>
        if len(parts) >= 3 and parts[1].isdigit():
            duration = int(parts[1])
            task = " ".join(parts[2:])
        else:
            # maybe "start 25 Task name" or "start Task name"
            if parts[1].isdigit():
                duration = int(parts[1])
                task = " ".join(parts[2:]) or "Untitled Task"
            else:
                task = " ".join(parts[1:]) or "Untitled Task"
    return duration, task

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "OK", 200

@app.route("/pomodoro", methods=["POST"])
def pomodoro_route():
    data = request.json or {}
    user, raw = parse_incoming_request(data)
    if not user:
        return jsonify({"reply": "❌ Missing user id."}), 400
    raw_lower = (raw or "").strip().lower()

    # --- ADD TASK ---
    if raw_lower.startswith("add task"):
        # expected: add task <task name> <duration>
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

    # --- SHOW TASKS ---
    if raw_lower == "tasks":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in queue."})
        out = "📋 Task Queue:\n"
        for i, t in enumerate(q, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out})

    # --- START NEXT ---
    if raw_lower == "start next":
        q = load_tasks_for_user(user)
        if not q:
            return jsonify({"reply": "📭 No tasks in the queue."})
        next_task = q.pop(0)
        save_tasks_for_user(user, q)
        duration = next_task["duration"]
        task_name = next_task["task"]
        end = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(minutes=duration)
        with timers_lock:
            timers[user] = {"type": "pomodoro", "end": end, "task": task_name, "duration": duration, "paused_pomodoro": None}
        set_active_timer(user, {"type": "pomodoro", "end": dt_to_iso(end), "task": task_name, "duration": duration})
        return jsonify({"reply": f"▶️ Started next task: **{task_name}** ({duration} min)"})

    # --- DONE (remove from queue) ---
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

    # --- CLEAR TASKS ---
    if raw_lower == "clear tasks":
        save_tasks_for_user(user, [])
        return jsonify({"reply": "🗑 Cleared all tasks."})

    # --- BREAK ---
    if raw_lower.startswith("break"):
        parts = raw.strip().split()
        if len(parts) == 1:
            minutes = 5
        elif len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
        else:
            return jsonify({"reply": "Usage: break OR break <minutes>"})
        # start_manual_break: pause pomodoro if running
        with timers_lock:
            cur = timers.get(user)
            paused_pomodoro = None
            if cur and cur.get("type") == "pomodoro":
                remaining = int(max((cur["end"] - datetime.utcnow().replace(tzinfo=timezone.utc)).total_seconds(), 0))
                paused_pomodoro = {"task": cur.get("task"), "remaining_seconds": remaining}
                # remove pomodoro from memory
                timers.pop(user, None)
            # start break
            end = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(minutes=minutes)
            timers[user] = {"type": "break", "end": end, "task": f"Manual Break ({minutes} min)", "duration": minutes, "paused_pomodoro": paused_pomodoro}
            set_active_timer(user, {"type": "break", "end": dt_to_iso(end), "task": f"Manual Break ({minutes} min)", "duration": minutes, "paused_pomodoro": paused_pomodoro})
        send_message(f"☕ Break started for {minutes} minutes. (Type `stop break` to cancel and resume.)")
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."})

    # --- STOP BREAK ---
    if raw_lower in ("stop break", "stopbreak", "break stop"):
        with timers_lock:
            cur = timers.get(user)
            if not cur or cur.get("type") != "break":
                return jsonify({"reply": "❌ No active break to stop."})
            paused = cur.get("paused_pomodoro")
            timers.pop(user, None)
            remove_active_timer(user)
            if paused:
                remaining = int(paused.get("remaining_seconds", 0))
                new_end = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=remaining)
                timers[user] = {"type": "pomodoro", "end": new_end, "task": paused.get("task"), "duration": round(remaining/60,2), "paused_pomodoro": None}
                set_active_timer(user, {"type":"pomodoro","end":dt_to_iso(new_end),"task":paused.get("task"),"duration":round(remaining/60,2)})
                return jsonify({"reply": f"▶️ Break stopped. Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
            else:
                return jsonify({"reply": "🛑 Break stopped."})

    # --- START Pomodoro ---
    if raw_lower.startswith("start"):
        duration, task_name = parse_start_command(raw)
        end = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(minutes=duration)
        with timers_lock:
            cur = timers.get(user)
            # if break was running, cancel it
            if cur and cur.get("type") == "break":
                timers.pop(user, None)
                remove_active_timer(user)
            timers[user] = {"type": "pomodoro", "end": end, "task": task_name, "duration": duration, "paused_pomodoro": None}
        set_active_timer(user, {"type": "pomodoro", "end": dt_to_iso(end), "task": task_name, "duration": duration, "paused_pomodoro": None})
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"})

    # --- STATUS ---
    if raw_lower in ("status", "time", "progress"):
        with timers_lock:
            cur = timers.get(user)
            if not cur:
                return jsonify({"reply": "❌ No active session."})
            remaining = int(max((cur["end"] - datetime.utcnow().replace(tzinfo=timezone.utc)).total_seconds(), 0))
            if cur.get("type") == "pomodoro":
                return jsonify({"reply": f"🍅 {cur['task']} — {remaining//60}m {remaining%60}s left"})
            else:
                return jsonify({"reply": f"☕ Break — {remaining//60}m {remaining%60}s left"})

    # --- STOP / CANCEL ---
    if raw_lower in ("stop", "end", "cancel"):
        with timers_lock:
            if user in timers:
                timers.pop(user, None)
                remove_active_timer(user)
                return jsonify({"reply": "🛑 Stopped."})
        return jsonify({"reply": "❌ No active session."})

    # --- RESUME (from paused break) ---
    if raw_lower == "resume":
        with timers_lock:
            cur = timers.get(user)
            if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
                paused = cur.get("paused_pomodoro")
                remaining = paused.get("remaining_seconds", 0)
                new_end = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=remaining)
                timers[user] = {"type":"pomodoro","end":new_end,"task":paused.get("task"),"duration":round(remaining/60,2),"paused_pomodoro":None}
                set_active_timer(user, {"type":"pomodoro","end":dt_to_iso(new_end),"task":paused.get("task"),"duration":round(remaining/60,2)})
                return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
        return jsonify({"reply": "❌ Nothing to resume."})

    # --- TODAY SUMMARY ---
    if raw_lower == "today":
        return jsonify({"reply": build_daily_summary(user)})

    # --- WEEK ---
    if raw_lower == "week":
        entries = list(col_history.find({"user": user}).sort("completed_at", -1).limit(21))
        if not entries:
            return jsonify({"reply": "📭 No history"})
        # simple week summary counts
        summary = {}
        for h in entries:
            summary[h.get("date")] = summary.get(h.get("date"), 0) + 1
        out = "📊 Weekly Summary\n"
        for d, c in summary.items():
            out += f"{d}: {'🍅'*c} ({c})\n"
        return jsonify({"reply": out})

    # --- CHART / ANALYTICS ---
    if raw_lower in ("chart", "weekly chart", "analytics", "weekly analytics"):
        text = get_weekly_chart(user)
        return jsonify({"reply": text})

    # --- STREAK ---
    if raw_lower == "streak":
        s = load_user_stats(user)
        return jsonify({"reply": f"🔥 Streak: {s.get('current_streak',0)} days\n🏆 Longest: {s.get('longest_streak',0)} days"})

    # --- SCORE ---
    if raw_lower == "score":
        s = load_user_stats(user)
        return jsonify({"reply": f"🎯 XP: {s.get('xp',0)}\n⭐ Level: {s.get('level',1)}"})

    # --- EXPORT commands routed to /export for PDF generation ---
    if raw_lower.startswith("export"):
        # forward to /export handler for consistent generation
        return export_report_internal(user, raw_lower)

    # --- SUGGESTIONS ---
    if raw_lower in ("suggest", "ai suggest", "suggestions"):
        items = smart_suggestions(user)
        reply = "🤖 *Here are some suggestions:* \n"
        for i in items:
            reply += f"• {i}\n"
        return jsonify({"reply": reply})

    # --- fallback/help ---
    return jsonify({"reply":"Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"})

# ---------------------------
# Daily / weekly analytics helpers
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
# Smart suggestions
# ---------------------------
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
# PDF generation / export routes
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

def generate_daily_report(user_id):
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

def generate_weekly_report(user_id):
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

def generate_monthly_report(user_id):
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

def export_report_internal(user, raw_lower):
    # call appropriate generator and reply with link
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
# Boot-time watcher start (runs even before first request)
# ---------------------------
try:
    rehydrate_timers()
except Exception as e:
    print("rehydrate failed at boot:", e)

try:
    threading.Thread(target=timer_watcher, daemon=True).start()
    app.threads_started = True
    print("🚀 Timer watcher started on boot")
except Exception as e:
    print("Boot watcher start failed:", e)



# ---------------------------
# main
# ---------------------------
if __name__ == "__main__":
    # (Optional for local development only)
    try:
        rehydrate_timers()
    except Exception as e:
        print("rehydrate_timers failed:", e)

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
