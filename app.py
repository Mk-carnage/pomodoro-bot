# app.py
import os
import threading
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

# ---------------------------
# Environment / config
# ---------------------------
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# JSON files
HISTORY_FILE = "history.json"
SUMMARY_STATE_FILE = "summary_state.json"
STREAK_FILE = "streaks.json"
SCORE_FILE = "scores.json"
TASK_FILE = "tasks.json"

app = Flask(__name__)

# timers: mapping user_id -> timer dict
# timer dict structure example:
# {
#   "type": "pomodoro" | "break",
#   "end": datetime,
#   "task": "Write report",
#   "duration": 25,              # minutes for pomodoro OR break
#   "paused_pomodoro": {         # present when a pomodoro was paused due to break
#       "task": "...",
#       "remaining_seconds": 300
#   } or None
# }
timers = {}
timers_lock = threading.Lock()


# ---------------------------
# File helpers
# ---------------------------
def load_file(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


def load_history():
    return load_file(HISTORY_FILE, [])


def save_history(data):
    save_file(HISTORY_FILE, data)


def load_summary_state():
    return load_file(SUMMARY_STATE_FILE, {})


def save_summary_state(data):
    save_file(SUMMARY_STATE_FILE, data)


def load_streaks():
    return load_file(STREAK_FILE, {})


def save_streaks(data):
    save_file(STREAK_FILE, data)


def load_scores():
    return load_file(SCORE_FILE, {})


def save_scores(data):
    save_file(SCORE_FILE, data)


def load_tasks():
    return load_file(TASK_FILE, {})


def save_tasks(data):
    save_file(TASK_FILE, data)


# ---------------------------
# Zoho send + token refresh
# ---------------------------
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return False
    url = "https://accounts.zoho.in/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    try:
        r = requests.post(url, params=params, timeout=10).json()
        if "access_token" in r:
            ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + r["access_token"]
            print("🔄 Access token refreshed.")
            return True
        print("Failed refresh:", r)
    except Exception as e:
        print("Exception refreshing token:", e)
    return False


def send_message(text):
    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}
    try:
        r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers, timeout=10)
        if r.status_code == 401:
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers, timeout=10)
        print("Sent ->", r.status_code)
        return r
    except Exception as e:
        print("Send error:", e)
        return None


# ---------------------------
# Parse start command
# ---------------------------
def parse_start_command(message):
    parts = message.split()
    duration = 25
    task_name = "Untitled Task"
    if len(parts) >= 3 and parts[1].isdigit():
        duration = int(parts[1])
        task_name = " ".join(parts[2:]).strip()
    elif len(parts) >= 2:
        task_name = " ".join(parts[1:]).strip()
    return duration, task_name


# ---------------------------
# Streak functions
# ---------------------------
def update_streak_for_user(user_id):
    streaks = load_streaks()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    user = streaks.get(user_id, {"current_streak": 0, "longest_streak": 0, "last_completed_date": None})
    last = user.get("last_completed_date")

    if last == yesterday:
        user["current_streak"] += 1
    elif last != today:
        user["current_streak"] = 1

    user["longest_streak"] = max(user["longest_streak"], user["current_streak"])
    user["last_completed_date"] = today

    streaks[user_id] = user
    save_streaks(streaks)
    return user["current_streak"], user["longest_streak"]


# ---------------------------
# Score functions
# ---------------------------
def calculate_level(xp):
    if xp < 100:
        return 1
    elif xp < 250:
        return 2
    elif xp < 500:
        return 3
    elif xp < 1000:
        return 4
    else:
        return xp // 500 + 4


def update_score(user_id, duration, streak):
    scores = load_scores()
    user = scores.get(user_id, {"xp": 0, "level": 1})

    base_xp = duration  # 1 XP per minute
    streak_bonus = streak * 5
    long_bonus = 10 if duration >= 30 else 0

    gained = base_xp + streak_bonus + long_bonus
    user["xp"] += gained
    user["level"] = calculate_level(user["xp"])

    scores[user_id] = user
    save_scores(scores)
    return gained, user["xp"], user["level"]


# ---------------------------
# Helper: count pomodoros today for a user
# ---------------------------
def count_pomodoros_today(user_id):
    history = load_history()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # Consider entries where "type" == "pomodoro" or absent (legacy)
    return sum(1 for h in history if h.get("user") == user_id and h.get("date") == today and h.get("type", "pomodoro") == "pomodoro")


# ---------------------------
# Timer watcher - handles pomodoro and break completion
# ---------------------------
def timer_watcher():
    print("⏳ Timer watcher started.")
    while True:
        now = datetime.utcnow()
        to_process = []

        with timers_lock:
            for uid, info in list(timers.items()):
                if now >= info["end"]:
                    to_process.append((uid, info))

        for uid, info in to_process:
            try:
                ttype = info.get("type", "pomodoro")
                if ttype == "pomodoro":
                    # COMPLETE POMODORO
                    task = info.get("task", "Untitled Task")
                    duration = info.get("duration", 25)

                    # Save history entry (pomodoro)
                    history = load_history()
                    history.append({
                        "user": uid,
                        "task": task,
                        "duration": duration,
                        "completed_at": datetime.utcnow().isoformat(),
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "type": "pomodoro"
                    })
                    save_history(history)

                    # streak & score
                    current_streak, longest = update_streak_for_user(uid)
                    gained, total_xp, level = update_score(uid, duration, current_streak)

                    # Send completion message
                    send_message(
                        f"⏰ Pomodoro completed!\n✔ Finished: **{task}**\n\n"
                        f"🔥 Streak: {current_streak} days\n"
                        f"🏆 Longest streak: {longest} days\n\n"
                        f"🎯 XP earned: +{gained}\n"
                        f"💠 Total XP: {total_xp}\n"
                        f"⭐ Level: {level}"
                    )

                    # Intelligent auto-break logic:
                    # short break 5 min, long break 15 min after every 4th pomodoro completed today
                    completed_today = count_pomodoros_today(uid)
                    # If newly completed made it a multiple of 4, give long break
                    if completed_today % 4 == 0:
                        auto_break_min = 15
                    else:
                        auto_break_min = 5

                    # Start auto-break only if user hasn't manually requested to skip (we assume auto-break is default)
                    # Start break (no paused_pomodoro since pomodoro finished)
                    break_end = datetime.utcnow() + timedelta(minutes=auto_break_min)
                    with timers_lock:
                        timers[uid] = {
                            "type": "break",
                            "end": break_end,
                            "task": f"Auto-break ({auto_break_min} min)",
                            "duration": auto_break_min,
                            "paused_pomodoro": None
                        }
                    send_message(f"☕ Auto break started for {auto_break_min} minutes. Relax! (Type `stop break` to cancel or `break <mins>` to override.)")

                elif ttype == "break":
                    # COMPLETE BREAK
                    br_task = info.get("task", "Break")
                    br_duration = info.get("duration", 5)

                    # Save break to history (so analytics include breaks)
                    history = load_history()
                    history.append({
                        "user": uid,
                        "task": br_task,
                        "duration": br_duration,
                        "completed_at": datetime.utcnow().isoformat(),
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "type": "break"
                    })
                    save_history(history)

                    # If there was a paused pomodoro resume it
                    paused = info.get("paused_pomodoro")
                    if paused:
                        remaining = paused.get("remaining_seconds", 0)
                        # resume pomodoro
                        end = datetime.utcnow() + timedelta(seconds=remaining)
                        with timers_lock:
                            timers[uid] = {
                                "type": "pomodoro",
                                "end": end,
                                "task": paused.get("task", "Resumed Task"),
                                "duration": round(remaining / 60, 2),
                                "paused_pomodoro": None
                            }
                        send_message(f"⏰ Break over — resuming **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.")
                    else:
                        # no paused pomodoro - just notify and suggest next task
                        send_message("☕ Break over! Ready to get back to work.")
                        # suggest next task if exists
                        tasks = load_tasks()
                        user_tasks = tasks.get(uid, [])
                        if user_tasks:
                            next_task = user_tasks[0]
                            send_message(f"⏭ Next task in queue: **{next_task['task']}** ({next_task['duration']} min). Type `start next` to begin.")
                    # end of break processing
                # finally, if processed, ensure we remove if not replaced (if no resume happened)
                with timers_lock:
                    # If timers[uid] still exists and its end was this info, remove it.
                    cur = timers.get(uid)
                    # we check identity by type and approximate end-time (string compare)
                    if cur and cur.get("end") == info.get("end"):
                        timers.pop(uid, None)
            except Exception as e:
                print("Error processing timer:", e)
        time.sleep(1)


# ---------------------------
# Utility: start a manual break (can pause pomodoro)
# ---------------------------
def start_manual_break(user_id, minutes):
    with timers_lock:
        cur = timers.get(user_id)
        # if currently in pomodoro, pause it
        paused_pomodoro = None
        if cur and cur.get("type") == "pomodoro":
            remaining = int(max((cur["end"] - datetime.utcnow()).total_seconds(), 0))
            paused_pomodoro = {"task": cur.get("task"), "remaining_seconds": remaining}
            # replace current with break (paused_pomodoro stored)
        # If currently in break, override with new break duration
        break_end = datetime.utcnow() + timedelta(minutes=minutes)
        timers[user_id] = {
            "type": "break",
            "end": break_end,
            "task": f"Manual break ({minutes} min)",
            "duration": minutes,
            "paused_pomodoro": paused_pomodoro
        }
    send_message(f"☕ Break started for {minutes} minutes. (Type `stop break` to cancel and resume.)")
    return True


# ---------------------------
# Utility: stop/cancel break (and optionally resume paused pomodoro)
# ---------------------------
def stop_break(user_id):
    with timers_lock:
        cur = timers.get(user_id)
        if not cur or cur.get("type") != "break":
            return False, "No active break to stop."
        paused = cur.get("paused_pomodoro")
        timers.pop(user_id, None)
        if paused:
            # resume paused pomodoro
            remaining = paused.get("remaining_seconds", 0)
            end = datetime.utcnow() + timedelta(seconds=remaining)
            timers[user_id] = {
                "type": "pomodoro",
                "end": end,
                "task": paused.get("task"),
                "duration": round(remaining / 60, 2),
                "paused_pomodoro": None
            }
            return True, f"Break stopped. Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."
        else:
            return True, "Break stopped."


# ---------------------------
# Start background threads (Flask 3 compatible)
# ---------------------------
@app.before_request
def start_background_threads():
    if not getattr(app, "threads_started", False):
        threading.Thread(target=timer_watcher, daemon=True).start()
        app.threads_started = True
        print("Background thread(s) started.")


# ---------------------------
# Daily summary builder (kept simple)
# ---------------------------
def build_daily_summary(uid):
    history = load_history()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    items = [h for h in history if h.get("user") == uid and h.get("date") == today]
    if not items:
        return "📅 No tasks completed today."
    total_minutes = sum(int(h.get("duration", 0)) for h in items)
    out = f"📅 DAILY SUMMARY ({today})\nTotal completed: {len(items)}\nTotal focus time: {total_minutes} minutes\n\nTasks:\n"
    for h in items:
        kind = h.get("type", "pomodoro")
        out += f"• [{kind}] {h.get('task')} — {h.get('duration')}m\n"
    return out


# ---------------------------
# Main bot endpoint
# ---------------------------
@app.route("/pomodoro", methods=["POST"])
def pomodoro_endpoint():
    data = request.json or {}
    raw = data.get("raw", "").strip()
    msg = raw.lower()
    user = data.get("user")
    if not user:
        return jsonify({"reply": "❌ No user id provided."}), 400

    # ---------- add task ----------
    if msg.startswith("add task"):
        parts = raw.split()
        if len(parts) < 4:
            return jsonify({"reply": "Usage: add task <task name> <duration>"})
        duration = parts[-1]
        if not duration.isdigit():
            return jsonify({"reply": "Duration must be a number (minutes)."})
        duration = int(duration)
        task_name = " ".join(parts[2:-1])
        tasks = load_tasks()
        user_tasks = tasks.get(user, [])
        user_tasks.append({"task": task_name, "duration": duration})
        tasks[user] = user_tasks
        save_tasks(tasks)
        return jsonify({"reply": f"📝 Added task: **{task_name}** ({duration} min)"})

    # ---------- tasks ----------
    if msg == "tasks":
        tasks = load_tasks()
        user_tasks = tasks.get(user, [])
        if not user_tasks:
            return jsonify({"reply": "📭 No tasks in queue."})
        out = "📋 Task Queue:\n"
        for i, t in enumerate(user_tasks, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out})

    # ---------- start next ----------
    if msg == "start next":
        tasks = load_tasks()
        user_tasks = tasks.get(user, [])
        if not user_tasks:
            return jsonify({"reply": "📭 No tasks in the queue."})
        next_task = user_tasks.pop(0)
        tasks[user] = user_tasks
        save_tasks(tasks)
        duration = next_task["duration"]
        task_name = next_task["task"]
        end = datetime.utcnow() + timedelta(minutes=duration)
        with timers_lock:
            timers[user] = {"type": "pomodoro", "end": end, "task": task_name, "duration": duration, "paused_pomodoro": None}
        return jsonify({"reply": f"▶️ Started next task: **{task_name}** ({duration} min)"})

    # ---------- done <n> ----------
    if msg.startswith("done"):
        parts = msg.split()
        if len(parts) != 2 or not parts[1].isdigit():
            return jsonify({"reply": "Usage: done <task_number>"})
        index = int(parts[1]) - 1
        tasks = load_tasks()
        user_tasks = tasks.get(user, [])
        if index < 0 or index >= len(user_tasks):
            return jsonify({"reply": "❌ Invalid task number."})
        removed = user_tasks.pop(index)
        tasks[user] = user_tasks
        save_tasks(tasks)
        return jsonify({"reply": f"✔ Removed task: **{removed['task']}**"})

    # ---------- clear tasks ----------
    if msg == "clear tasks":
        tasks = load_tasks()
        tasks[user] = []
        save_tasks(tasks)
        return jsonify({"reply": "🗑 Cleared all tasks."})

    # ---------- manual break ----------
    if msg.startswith("break"):
        parts = raw.split()
        if len(parts) == 1:
            minutes = 5
        elif len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
        else:
            return jsonify({"reply": "Usage: break OR break <minutes>"})
        # start manual break (pauses any running pomodoro)
        start_manual_break(user, minutes)
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."})

    # ---------- stop break ----------
    if msg in ("stop break", "stopbreak", "break stop"):
        ok, message = stop_break(user)
        return jsonify({"reply": message})

    # ---------- start (pomodoro) ----------
    if msg.startswith("start"):
        # If user issues a start while a break is running, we'll cancel break and start new pomodoro
        duration, task_name = parse_start_command(raw)
        end = datetime.utcnow() + timedelta(minutes=duration)
        with timers_lock:
            cur = timers.get(user)
            if cur and cur.get("type") == "break":
                # discard break and resume new pomodoro
                timers.pop(user, None)
            timers[user] = {"type": "pomodoro", "end": end, "task": task_name, "duration": duration, "paused_pomodoro": None}
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"})

    # ---------- status ----------
    if msg in ("status", "time", "progress"):
        with timers_lock:
            cur = timers.get(user)
            if not cur:
                return jsonify({"reply": "❌ No active session."})
            remaining = int(max((cur["end"] - datetime.utcnow()).total_seconds(), 0))
            ttype = cur.get("type", "pomodoro")
            if ttype == "pomodoro":
                return jsonify({"reply": f"🍅 {cur['task']} — {remaining//60}m {remaining%60}s left"})
            else:
                return jsonify({"reply": f"☕ Break — {remaining//60}m {remaining%60}s left"})

    # ---------- stop ----------
    if msg in ("stop", "end", "cancel"):
        with timers_lock:
            if user in timers:
                timers.pop(user, None)
                return jsonify({"reply": "🛑 Stopped."})
        return jsonify({"reply": "❌ No active session."})

    # ---------- resume (resume paused pomodoro if any) ----------
    if msg == "resume":
        with timers_lock:
            cur = timers.get(user)
            # if in break with paused_pomodoro: resume it
            if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
                paused = cur.get("paused_pomodoro")
                remaining = paused.get("remaining_seconds", 0)
                end = datetime.utcnow() + timedelta(seconds=remaining)
                timers[user] = {"type": "pomodoro", "end": end, "task": paused.get("task"), "duration": round(remaining / 60, 2), "paused_pomodoro": None}
                return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
        return jsonify({"reply": "❌ Nothing to resume."})

    # ---------- today ----------
    if msg == "today":
        return jsonify({"reply": build_daily_summary(user)})

    # ---------- week ----------
    if msg == "week":
        history = load_history()
        user_hist = [h for h in history if h.get("user") == user]
        if not user_hist:
            return jsonify({"reply": "📭 No history"})
        summary = {}
        for h in user_hist:
            summary[h["date"]] = summary.get(h["date"], 0) + 1
        out = "📊 Weekly Summary\n"
        for d, c in summary.items():
            out += f"{d}: {'🍅'*c} ({c})\n"
        return jsonify({"reply": out})

    # ---------- streak ----------
    if msg == "streak":
        streaks = load_streaks()
        s = streaks.get(user)
        if not s:
            return jsonify({"reply": "🔥 No streak yet"})
        return jsonify({"reply": f"🔥 Streak: {s['current_streak']} days\n🏆 Longest: {s['longest_streak']} days"})

    # ---------- score ----------
    if msg == "score":
        scores = load_scores()
        s = scores.get(user)
        if not s:
            return jsonify({"reply": "🎯 No XP yet"})
        return jsonify({"reply": f"🎯 XP: {s['xp']}\n⭐ Level: {s['level']}"})

    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | streak | score | add task | tasks | start next | done | clear tasks"})


# ---------------------------
# Root
# ---------------------------
@app.route("/")
def home():
    return "Pomodoro Bot Running"


# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
