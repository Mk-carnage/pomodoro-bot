# app.py
import os
import threading
import time
import json
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

# -----------------------------
# Environment Variables (Render-safe)
# -----------------------------
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

# Optional test mode: if "1", scheduler will send every minute for quick testing
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

# Work hours schedule in local timezone (Asia/Kolkata)
MORNING_HOUR = int(os.getenv("SUMMARY_MORNING_HOUR", "9"))   # 09:00
EVENING_HOUR = int(os.getenv("SUMMARY_EVENING_HOUR", "17"))  # 17:00

# Files
HISTORY_FILE = "history.json"
SUMMARY_STATE_FILE = "summary_state.json"

# Timezone
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

app = Flask(__name__)

# -----------------------------
# In-memory timers and lock
# -----------------------------
timers = {}
timers_lock = threading.Lock()


# -----------------------------
# File helpers
# -----------------------------
def load_json_file(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


# -----------------------------
# History helpers
# -----------------------------
def load_history():
    return load_json_file(HISTORY_FILE, [])


def save_history(data):
    save_json_file(HISTORY_FILE, data)


# -----------------------------
# Summary state helpers (persist last-sent dates)
# Structure example:
# {
#   "user_id_1": {"morning": "2025-11-14", "evening": "2025-11-14"},
#   "user_id_2": {"morning": "2025-11-13"}
# }
# -----------------------------
def load_summary_state():
    return load_json_file(SUMMARY_STATE_FILE, {})


def save_summary_state(state):
    save_json_file(SUMMARY_STATE_FILE, state)


# -----------------------------
# Zoho token refresh (optional)
# -----------------------------
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        print("No refresh credentials configured.")
        return False
    url = "https://accounts.zoho.in/oauth/v2/token"
    data = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    try:
        r = requests.post(url, data=data, timeout=10)
        j = r.json()
        if r.status_code == 200 and j.get("access_token"):
            ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + j["access_token"]
            print("Access token refreshed.")
            return True
        print("Failed to refresh token:", j)
    except Exception as e:
        print("Exception refreshing token:", e)
    return False


# -----------------------------
# Send message helper
# -----------------------------
def send_message(text):
    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}
    try:
        r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers, timeout=10)
        if r.status_code == 401:
            # try refresh once
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers, timeout=10)
        print("Sent message ->", r.status_code, r.text if r is not None else "no response")
        return r
    except Exception as e:
        print("Error sending message:", e)
        return None


# -----------------------------
# Parse start command (duration + task)
# -----------------------------
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


# -----------------------------
# Timer watcher: completes timers and saves history
# -----------------------------
def timer_watcher():
    print("Timer watcher started.")
    while True:
        now = datetime.utcnow()
        finished = []
        with timers_lock:
            for user_id, info in list(timers.items()):
                if now >= info["end"]:
                    finished.append((user_id, info))
        # process outside lock
        for user_id, info in finished:
            task = info.get("task", "Untitled")
            duration = info.get("duration", 25)
            # save history
            history = load_history()
            history.append({
                "user": user_id,
                "task": task,
                "duration": duration,
                "completed_at": datetime.utcnow().isoformat(),
                "date": datetime.utcnow().strftime("%Y-%m-%d")
            })
            save_history(history)
            # notify (sends to bot incoming webhook)
            send_message(f"⏰ Pomodoro completed!\n✔ Finished: **{task}**\nType `break` to start your rest.")
            # remove timer
            with timers_lock:
                timers.pop(user_id, None)
        time.sleep(1)


# -----------------------------
# Summary scheduler: sends summary at 09:00 and 17:00 Asia/Kolkata
# -----------------------------
def should_send_for_user(state, user_id, kind, local_date_str):
    # kind: "morning" or "evening"
    user_state = state.get(user_id, {})
    return user_state.get(kind) != local_date_str


def mark_sent_for_user(state, user_id, kind, local_date_str):
    if user_id not in state:
        state[user_id] = {}
    state[user_id][kind] = local_date_str


def build_daily_summary_for_user(user_id, date_str):
    history = load_history()
    # filter for that user and date
    user_tasks = [h for h in history if h["user"] == user_id and h["date"] == date_str]
    if not user_tasks:
        return None
    total_minutes = sum(int(h.get("duration", 0)) for h in user_tasks)
    out = f"📅 DAILY SUMMARY for {date_str}\n\n"
    out += f"Total completed Pomodoros: {len(user_tasks)}\n"
    out += f"Total focus time: {total_minutes} minutes\n\n"
    out += "Tasks:\n"
    for h in user_tasks:
        out += f"• {h['task']} — {h['duration']}m\n"
    return out


def summary_scheduler():
    print("Summary scheduler started.")
    # load state
    state = load_summary_state()
    while True:
        now_local = datetime.now(LOCAL_TZ)
        date_str = now_local.strftime("%Y-%m-%d")

        # Testing mode: send every minute to all users (if TEST_MODE)
        if TEST_MODE:
            # send to users who have any history or active timers
            users = set()
            users.update([h["user"] for h in load_history()])
            with timers_lock:
                users.update(list(timers.keys()))
            for user in users:
                # send a quick test summary
                s = build_daily_summary_for_user(user, date_str)
                if s:
                    send_message(f"[TEST] {s}")
            time.sleep(60)
            continue

        # Normal mode: check morning and evening
        current_local_time = now_local.time()
        # create window to avoid missing exact second: check hour and minute
        if current_local_time.hour == MORNING_HOUR and current_local_time.minute == 0:
            # morning summary
            state = load_summary_state()  # always reload to avoid staleness
            # send to all users who have any history or timers
            users = set([h["user"] for h in load_history()])
            with timers_lock:
                users.update(list(timers.keys()))
            for user in users:
                if should_send_for_user(state, user, "morning", date_str):
                    s = build_daily_summary_for_user(user, date_str)
                    if s:
                        send_message(s)
                    else:
                        # optional: send short no-activity message
                        send_message(f"📅 Daily summary — no completed Pomodoros for {date_str}. Keep going!")
                    mark_sent_for_user(state, user, "morning", date_str)
            save_summary_state(state)
            # sleep 61 seconds to avoid re-sending within the same minute
            time.sleep(61)
            continue

        if current_local_time.hour == EVENING_HOUR and current_local_time.minute == 0:
            # evening summary
            state = load_summary_state()
            users = set([h["user"] for h in load_history()])
            with timers_lock:
                users.update(list(timers.keys()))
            for user in users:
                if should_send_for_user(state, user, "evening", date_str):
                    s = build_daily_summary_for_user(user, date_str)
                    if s:
                        send_message(s)
                    else:
                        send_message(f"📅 Evening summary — no completed Pomodoros for {date_str}.")
                    mark_sent_for_user(state, user, "evening", date_str)
            save_summary_state(state)
            time.sleep(61)
            continue

        # Sleep short while waiting for scheduled times
        time.sleep(20)


# -----------------------------
# Start background threads (Flask 3 compatible)
# -----------------------------
@app.before_request
def start_background_threads():
    if not getattr(app, "threads_started", False):
        t1 = threading.Thread(target=timer_watcher, daemon=True)
        t1.start()
        t2 = threading.Thread(target=summary_scheduler, daemon=True)
        t2.start()
        app.threads_started = True
        print("Background threads (timer + scheduler) started.")


# -----------------------------
# Main endpoint for bot commands
# -----------------------------
@app.route("/pomodoro", methods=["POST"])
def pomodoro():
    data = request.json or {}
    raw = data.get("raw", "")
    msg = raw.lower().strip()
    user_id = data.get("user")
    if not user_id:
        return jsonify({"reply": "❌ No user id provided."}), 400

    if msg.startswith("start"):
        duration, task = parse_start_command(raw)
        end = datetime.utcnow() + timedelta(minutes=duration)
        with timers_lock:
            timers[user_id] = {"end": end, "task": task, "duration": duration}
        return jsonify({"reply": f"🍅 Started **{task}** for {duration} minutes."})

    if msg.startswith("status"):
        with timers_lock:
            if user_id not in timers:
                return jsonify({"reply": "❌ No active session."})
            info = timers[user_id]
            remaining = info["end"] - datetime.utcnow()
            sec = max(int(remaining.total_seconds()), 0)
            return jsonify({"reply": f"🍅 {info['task']} — {sec//60}m {sec%60}s left"})

    if msg.startswith("stop"):
        with timers_lock:
            if user_id in timers:
                timers.pop(user_id, None)
                return jsonify({"reply": "🛑 Stopped."})
        return jsonify({"reply": "❌ No session to stop."})

    if msg.startswith("today"):
        history = load_history()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        user_tasks = [h for h in history if h["user"] == user_id and h["date"] == today]
        if not user_tasks:
            return jsonify({"reply": "📭 No tasks completed today."})
        out = "📅 Today's completed Pomodoros:\n"
        for h in user_tasks:
            out += f"• {h['task']} — {h['duration']}m\n"
        return jsonify({"reply": out})

    if msg.startswith("week"):
        history = load_history()
        user_tasks = [h for h in history if h["user"] == user_id]
        if not user_tasks:
            return jsonify({"reply": "📭 No history."})
        summary = {}
        for h in user_tasks:
            summary[h['date']] = summary.get(h['date'], 0) + 1
        out = "📊 Weekly summary:\n"
        for d, c in sorted(summary.items()):
            out += f"{d}: {'🍅'*c} ({c})\n"
        return jsonify({"reply": out})

    return jsonify({"reply": "🤖 Commands: start [mins] <task> | stop | status | today | week"})


# -----------------------------
# Root
# -----------------------------
@app.route("/")
def home():
    return "Pomodoro Bot Running"


# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
