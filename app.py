import os
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
import requests

# -----------------------------
# Load Environment Variables
# -----------------------------
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")

app = Flask(__name__)

# -----------------------------
# GLOBAL STORAGE (RAM)
# -----------------------------
timers = {}  # per-user timers


# -----------------------------
# Helper: Refresh Token if Needed
# -----------------------------
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN

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
        print("🔄 Access token refreshed!")
        ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + data["access_token"]
    else:
        print("❌ Failed to refresh token:", data)


# -----------------------------
# Helper: Send message to Zoho
# -----------------------------
def send_message(bot_user, text):
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

    print("📤 Sent to Zoho:", r.text)
    return r.text


# -----------------------------
# Parse Start Command
# -----------------------------
def parse_start_command(message):
    parts = message.split()

    duration = 25
    task_name = "Untitled Task"

    # Case 1: "start 20 Write report"
    if len(parts) >= 3 and parts[1].isdigit():
        duration = int(parts[1])
        task_name = " ".join(parts[2:]).strip()

    # Case 2: "start Write report"
    elif len(parts) >= 2:
        task_name = " ".join(parts[1:]).strip()

    return duration, task_name


# -----------------------------
# Background Timer Watcher
# -----------------------------
def timer_watcher():
    print("⏳ Background timer watcher started...")

    while True:
        now = datetime.utcnow()

        finished_users = []

        for user_id, info in timers.items():
            if now >= info["end"]:
                finished_users.append(user_id)

        # Process completed timers
        for user in finished_users:
            task = timers[user]["task"]
            send_message(user, f"⏰ Pomodoro completed!\n✔ Finished: **{task}**\nType `break` to start your rest.")
            del timers[user]

        time.sleep(1)


# -----------------------------
# Start watcher only once
# (Flask 3 compatible)
# -----------------------------
@app.before_request
def start_background_thread():
    if not getattr(app, "watcher_thread_started", False):
        t = threading.Thread(target=timer_watcher, daemon=True)
        t.start()
        app.watcher_thread_started = True
        print("🚀 Watcher thread started")


# -----------------------------
# Main Endpoint for Bot
# -----------------------------
@app.route("/pomodoro", methods=["POST"])
def pomodoro():
    data = request.json
    message = data.get("raw", "").lower()
    user_id = data.get("user")

    if not user_id:
        return jsonify({"reply": "❌ No user ID"}), 400

    # -----------------------------
    # START COMMAND
    # -----------------------------
    if message.startswith("start"):
        duration, task_name = parse_start_command(data["raw"])

        end_time = datetime.utcnow() + timedelta(minutes=duration)

        timers[user_id] = {
            "end": end_time,
            "task": task_name,
            "break": False
        }

        return jsonify({"reply": f"🍅 Pomodoro started: **{task_name}** for {duration} minutes!"})

    # -----------------------------
    # STATUS
    # -----------------------------
    if message.startswith("status"):
        if user_id not in timers:
            return jsonify({"reply": "❌ No active pomodoro session."})

        info = timers[user_id]
        task = info.get("task", "Unnamed Task")

        remaining = info["end"] - datetime.utcnow()
        seconds = max(int(remaining.total_seconds()), 0)
        mins = seconds // 60
        secs = seconds % 60

        return jsonify({"reply": f"🍅 Task: **{task}**\n⏳ Time left: {mins}m {secs}s"})

    # -----------------------------
    # STOP TIMER
    # -----------------------------
    if message.startswith("stop"):
        if user_id in timers:
            del timers[user_id]
            return jsonify({"reply": "🛑 Pomodoro stopped."})
        return jsonify({"reply": "❌ No active session to stop."})

    return jsonify({"reply": "🤖 Unknown command"})


# -----------------------------
# Root (optional)
# -----------------------------
@app.route("/")
def home():
    return "Pomodoro Bot Running."


# -----------------------------
# Run Server
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
