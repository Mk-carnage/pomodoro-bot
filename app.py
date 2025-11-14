import os
import threading
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

# -----------------------------
# ENVIRONMENT VARIABLES
# -----------------------------
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

# Summary times - India Work Hours (modifiable)
MORNING_HOUR = 9
EVENING_HOUR = 17

# Optional test mode
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

# Timezone
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# Files
HISTORY_FILE = "history.json"
SUMMARY_STATE_FILE = "summary_state.json"
STREAK_FILE = "streaks.json"

app = Flask(__name__)

# -----------------------------
# TIMER STORAGE
# -----------------------------
timers = {}
timers_lock = threading.Lock()


# -----------------------------
# FILE HELPERS
# -----------------------------
def load_file(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default


def save_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


# HISTORY HELPERS
def load_history():
    return load_file(HISTORY_FILE, [])


def save_history(data):
    save_file(HISTORY_FILE, data)


# SUMMARY STATE HELPERS
def load_summary_state():
    return load_file(SUMMARY_STATE_FILE, {})


def save_summary_state(data):
    save_file(SUMMARY_STATE_FILE, data)


# STREAK HELPERS
def load_streaks():
    return load_file(STREAK_FILE, {})


def save_streaks(data):
    save_file(STREAK_FILE, data)


# -----------------------------
# SEND MESSAGE TO ZOHO
# -----------------------------
def send_message(text):
    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}
    payload = {"text": text}

    try:
        r = requests.post(ZOHO_INCOMING_URL, json=payload, headers=headers)

        if r.status_code == 401:
            refresh_access_token()
            headers["Authorization"] = ZOHO_OAUTH_TOKEN
            r = requests.post(ZOHO_INCOMING_URL, json=payload, headers=headers)

        print("Sent:", r.text)
        return r.text

    except Exception as e:
        print("Send error:", e)
        return None


# -----------------------------
# REFRESH TOKEN
# -----------------------------
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN

    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return

    url = "https://accounts.zoho.in/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
    }

    r = requests.post(url, params=params)
    data = r.json()

    if "access_token" in data:
        ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + data["access_token"]


# -----------------------------
# PARSE START COMMAND
# -----------------------------
def parse_start_command(msg):
    parts = msg.split()

    duration = 25
    task = "Untitled Task"

    if len(parts) >= 3 and parts[1].isdigit():
        duration = int(parts[1])
        task = " ".join(parts[2:])
    elif len(parts) >= 2:
        task = " ".join(parts[1:])

    return duration, task


# -----------------------------
# STREAK UPDATE
# -----------------------------
def update_streak_for_user(user_id):
    streaks = load_streaks()

    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    user_data = streaks.get(user_id, {
        "current_streak": 0,
        "longest_streak": 0,
        "last_completed_date": None
    })

    last = user_data["last_completed_date"]

    if last == yesterday:
        user_data["current_streak"] += 1
    elif last != today:
        user_data["current_streak"] = 1

    user_data["longest_streak"] = max(
        user_data["longest_streak"],
        user_data["current_streak"]
    )

    user_data["last_completed_date"] = today

    streaks[user_id] = user_data
    save_streaks(streaks)

    return user_data["current_streak"], user_data["longest_streak"]


# -----------------------------
# TIMER WATCHER (Pomodoro complete)
# -----------------------------
def timer_watcher():
    print("Timer watcher started…")

    while True:
        now = datetime.utcnow()
        to_finish = []

        with timers_lock:
            for uid, info in timers.items():
                if now >= info["end"]:
                    to_finish.append((uid, info))

        for uid, info in to_finish:
            task = info["task"]
            duration = info["duration"]

            # Save to history
            history = load_history()
            history.append({
                "user": uid,
                "task": task,
                "duration": duration,
                "completed_at": datetime.utcnow().isoformat(),
                "date": datetime.utcnow().strftime("%Y-%m-%d")
            })
            save_history(history)

            # Update streak
            current, longest = update_streak_for_user(uid)

            # Send message
            send_message(
                f"⏰ Pomodoro Completed!\n"
                f"✔ Task: **{task}**\n\n"
                f"🔥 Current Streak: {current} days\n"
                f"🏆 Longest Streak: {longest} days\n"
                f"Type `break` to start resting."
            )

            with timers_lock:
                timers.pop(uid, None)

        time.sleep(1)


# -----------------------------
# SUMMARY SCHEDULER (9AM + 5PM)
# -----------------------------
def build_daily_summary(user):
    history = load_history()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    items = [h for h in history if h["user"] == user and h["date"] == today]

    if not items:
        return f"📅 No tasks completed today."

    total = sum(h["duration"] for h in items)
    out = f"📅 **Daily Summary**\n\nTotal Pomodoros: {len(items)}\nTotal Focus: {total} min\n\nTasks:\n"

    for h in items:
        out += f"• {h['task']} — {h['duration']}m\n"

    return out


def summary_scheduler():
    print("Summary scheduler started…")

    while True:
        now = datetime.now(LOCAL_TZ)
        time_str = now.strftime("%H:%M")

        if time_str in ["09:00", "17:00"]:
            history = load_history()
            users = list(set([h["user"] for h in history]))

            for user in users:
                summary = build_daily_summary(user)
                send_message(summary)

            time.sleep(60)

        time.sleep(20)


# -----------------------------
# FLASK STARTUP HOOK
# -----------------------------
@app.before_request
def start_threads():
    if not getattr(app, "threads_started", False):
        threading.Thread(target=timer_watcher, daemon=True).start()
        threading.Thread(target=summary_scheduler, daemon=True).start()
        app.threads_started = True
        print("Background threads started.")


# -----------------------------
# BOT COMMANDS
# -----------------------------
@app.route("/pomodoro", methods=["POST"])
def pomodoro():
    data = request.json
    msg = data.get("raw", "").lower()
    user = data.get("user")

    if msg.startswith("start"):
        duration, task = parse_start_command(data["raw"])
        end = datetime.utcnow() + timedelta(minutes=duration)

        with timers_lock:
            timers[user] = {"end": end, "task": task, "duration": duration}

        return jsonify({"reply": f"🍅 Started **{task}** for {duration} minutes."})

    if msg.startswith("status"):
        with timers_lock:
            if user not in timers:
                return jsonify({"reply": "❌ No active session."})

            info = timers[user]
            remaining = info["end"] - datetime.utcnow()
            sec = int(max(remaining.total_seconds(), 0))
            return jsonify({"reply": f"🍅 {info['task']} — {sec//60}m {sec%60}s left"})

    if msg.startswith("stop"):
        with timers_lock:
            if user in timers:
                timers.pop(user, None)
                return jsonify({"reply": "🛑 Stopped."})

        return jsonify({"reply": "❌ No session."})

    if msg.startswith("today"):
        return jsonify({"reply": build_daily_summary(user)})

    if msg.startswith("week"):
        history = load_history()
        user_hist = [h for h in history if h["user"] == user]

        if not user_hist:
            return jsonify({"reply": "📭 No history."})

        summary = {}
        for h in user_hist:
            summary[h["date"]] = summary.get(h["date"], 0) + 1

        out = "📊 **Weekly Summary**\n\n"
        for d, c in summary.items():
            out += f"{d}: {'🍅' * c} ({c})\n"

        return jsonify({"reply": out})

    if msg.startswith("streak"):
        streaks = load_streaks()
        s = streaks.get(user)

        if not s:
            return jsonify({"reply": "🔥 No streak yet!"})

        return jsonify({
            "reply": (
                f"🔥 Current Streak: {s['current_streak']} days\n"
                f"🏆 Longest Streak: {s['longest_streak']} days"
            )
        })

    return jsonify({"reply": "🤖 Commands: start | status | stop | today | week | streak"})


# -----------------------------
# ROOT
# -----------------------------
@app.route("/")
def home():
    return "Pomodoro Bot Running"


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
