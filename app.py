import os
import threading
import time
import json
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
import requests

# -----------------------------
# Environment Variables
# -----------------------------
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")

HISTORY_FILE = "history.json"

app = Flask(__name__)

# -----------------------------
# GLOBAL Storage (RAM)
# -----------------------------
timers = {}   # active timers


# ---------------------------------------------------
# HISTORY HELPERS
# ---------------------------------------------------
def load_history():
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except:
        return []


def save_history(data):
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=4)


# ---------------------------------------------------
# TOKEN REFRESH
# ---------------------------------------------------
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN

    if not CLIENT_ID or not CLIENT_SECRET or not REFRESH_TOKEN:
        print("⚠ No refresh token configured")
        return

    url = "https://accounts.zoho.in/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
    }

    response = requests.post(url, params=params)
    data = response.json()

    if "access_token" in data:
        print("🔄 Access token refreshed")
        ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + data["access_token"]
    else:
        print("❌ Failed to refresh token:", data)


# ---------------------------------------------------
# SEND MESSAGE TO ZOHO
# ---------------------------------------------------
def send_message(user_id, text):
    headers = {
        "Authorization": ZOHO_OAUTH_TOKEN,
        "Content-Type": "application/json",
    }

    payload = {"text": text}

    r = requests.post(ZOHO_INCOMING_URL, json=payload, headers=headers)

    if r.status_code == 401:
        print("⚠ 401 Unauthorized — refreshing token")
        refresh_access_token()
        r = requests.post(ZOHO_INCOMING_URL, json=payload, headers=headers)

    print("📤 Sent:", r.text)
    return r.text


# ---------------------------------------------------
# Parse "start" Command
# ---------------------------------------------------
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


# ---------------------------------------------------
# BACKGROUND TIMER WATCHER
# ---------------------------------------------------
def timer_watcher():
    print("⏳ Timer watcher started...")

    while True:
        now = datetime.utcnow()
        finished_users = []

        # find completed timers
        for user_id, info in timers.items():
            if now >= info["end"]:
                finished_users.append(user_id)

        # process completed
        for user_id in finished_users:
            info = timers[user_id]
            task = info["task"]
            duration = info["duration"]

            # SAVE history entry
            history = load_history()
            history.append({
                "user": user_id,
                "task": task,
                "duration": duration,
                "completed_at": datetime.utcnow().isoformat(),
                "date": datetime.utcnow().strftime("%Y-%m-%d")
            })
            save_history(history)

            # notify
            send_message(
                user_id,
                f"⏰ Pomodoro completed!\n✔ Finished: **{task}**\nType `break` to start your rest."
            )

            del timers[user_id]

        time.sleep(1)


# ---------------------------------------------------
# FLASK 3 — start watcher only once
# ---------------------------------------------------
@app.before_request
def start_background_thread():
    if not getattr(app, "watcher_thread_started", False):
        print("🚀 Starting background thread...")
        t = threading.Thread(target=timer_watcher, daemon=True)
        t.start()
        app.watcher_thread_started = True


# ---------------------------------------------------
# MAIN BOT ENDPOINT
# ---------------------------------------------------
@app.route("/pomodoro", methods=["POST"])
def pomodoro():
    data = request.json
    raw_msg = data.get("raw", "")
    msg = raw_msg.lower().strip()
    user_id = data.get("user")

    if not user_id:
        return jsonify({"reply": "❌ No user ID"}), 400

    # ---------------------
    # START
    # ---------------------
    if msg.startswith("start"):
        duration, task_name = parse_start_command(raw_msg)

        end_time = datetime.utcnow() + timedelta(minutes=duration)

        timers[user_id] = {
            "end": end_time,
            "task": task_name,
            "duration": duration,
        }

        return jsonify({"reply": f"🍅 Started: **{task_name}** for {duration}m"})

    # ---------------------
    # STATUS
    # ---------------------
    if msg.startswith("status"):
        if user_id not in timers:
            return jsonify({"reply": "❌ No active session."})

        info = timers[user_id]
        task = info["task"]
        remaining = info["end"] - datetime.utcnow()

        sec = max(int(remaining.total_seconds()), 0)
        mins = sec // 60
        secs = sec % 60

        return jsonify({"reply": f"🍅 **{task}**\n⏳ {mins}m {secs}s left"})

    # ---------------------
    # STOP
    # ---------------------
    if msg.startswith("stop"):
        if user_id in timers:
            del timers[user_id]
            return jsonify({"reply": "🛑 Pomodoro stopped."})
        return jsonify({"reply": "❌ No session to stop."})

    # ---------------------
    # TODAY SUMMARY
    # ---------------------
    if msg.startswith("today"):
        history = load_history()
        today = datetime.utcnow().strftime("%Y-%m-%d")

        tasks = [h for h in history if h["user"] == user_id and h["date"] == today]

        if not tasks:
            return jsonify({"reply": "📭 No tasks completed today."})

        out = "📅 **Today's Pomodoros**:\n\n"
        for h in tasks:
            out += f"• {h['task']} — {h['duration']}m\n"

        return jsonify({"reply": out})

    # ---------------------
    # WEEK SUMMARY
    # ---------------------
    if msg.startswith("week"):
        history = load_history()
        tasks = [h for h in history if h["user"] == user_id]

        if not tasks:
            return jsonify({"reply": "📭 No history found."})

        summary = {}
        for h in tasks:
            date = h["date"]
            summary[date] = summary.get(date, 0) + 1

        out = "📊 **Weekly Summary**:\n\n"
        for date, count in summary.items():
            out += f"{date}: {'🍅' * count} ({count})\n"

        return jsonify({"reply": out})

    return jsonify({"reply": "🤖 Unknown command"})


# ---------------------------------------------------
# ROOT ENDPOINT
# ---------------------------------------------------
@app.route("/")
def home():
    return "Pomodoro Bot Running."


# ---------------------------------------------------
# RUN
# ---------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
