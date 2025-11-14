# app.py
import os
import threading
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

# ==========================================
# ENVIRONMENT VARIABLES
# ==========================================
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

# Summary times (Indian work hours)
MORNING_HOUR = 9
EVENING_HOUR = 17

LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# Data files
HISTORY_FILE = "history.json"
SUMMARY_STATE_FILE = "summary_state.json"
STREAK_FILE = "streaks.json"
SCORE_FILE = "scores.json"

app = Flask(__name__)

# Timer storage
timers = {}
timers_lock = threading.Lock()


# ==========================================
# FILE HELPERS
# ==========================================
def load_file(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
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


# ==========================================
# SEND MESSAGE
# ==========================================
def send_message(text):
    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}
    payload = {"text": text}

    r = requests.post(ZOHO_INCOMING_URL, json=payload, headers=headers)

    if r.status_code == 401:
        refresh_access_token()
        headers["Authorization"] = ZOHO_OAUTH_TOKEN
        r = requests.post(ZOHO_INCOMING_URL, json=payload, headers=headers)

    return r.text


# ==========================================
# REFRESH TOKEN
# ==========================================
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN

    url = "https://accounts.zoho.in/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
    }

    r = requests.post(url, params=params).json()

    if "access_token" in r:
        ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + r["access_token"]


# ==========================================
# PARSE START COMMAND
# ==========================================
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


# ==========================================
# STREAK SYSTEM
# ==========================================
def update_streak_for_user(user_id):
    streaks = load_streaks()

    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    user = streaks.get(user_id, {
        "current_streak": 0,
        "longest_streak": 0,
        "last_completed_date": None
    })

    last = user["last_completed_date"]

    if last == yesterday:
        user["current_streak"] += 1
    elif last != today:
        user["current_streak"] = 1

    user["longest_streak"] = max(user["longest_streak"], user["current_streak"])
    user["last_completed_date"] = today

    streaks[user_id] = user
    save_streaks(streaks)

    return user["current_streak"], user["longest_streak"]


# ==========================================
# FOCUS SCORE SYSTEM
# ==========================================
def calculate_level(xp):
    if xp < 100:
        return 1
    elif xp < 250:
        return 2
    elif xp < 500:
        return 3
    elif xp < 1000:
        return 4
    return xp // 500 + 4


def update_score(user_id, duration, streak):
    scores = load_scores()

    user = scores.get(user_id, {"xp": 0, "level": 1})

    base_xp = duration
    streak_bonus = streak * 5
    long_bonus = 10 if duration >= 30 else 0

    gained = base_xp + streak_bonus + long_bonus
    user["xp"] += gained
    user["level"] = calculate_level(user["xp"])

    scores[user_id] = user
    save_scores(scores)

    return gained, user["xp"], user["level"]


# ==========================================
# TIMER WATCHER
# ==========================================
def timer_watcher():
    while True:
        now = datetime.utcnow()
        completed = []

        with timers_lock:
            for uid, info in list(timers.items()):
                if now >= info["end"]:
                    completed.append((uid, info))

        for uid, info in completed:
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

            # Streak update
            current, longest = update_streak_for_user(uid)

            # Score update
            gained, total_xp, level = update_score(uid, duration, current)

            send_message(
                f"⏰ Pomodoro Completed!\n"
                f"✔ Task: **{task}**\n\n"
                f"🔥 Streak: {current} days\n"
                f"🏆 Longest: {longest} days\n\n"
                f"🎯 XP Earned: +{gained}\n"
                f"💠 Total XP: {total_xp}\n"
                f"⭐ Level: {level}"
            )

            with timers_lock:
                timers.pop(uid, None)

        time.sleep(1)


# ==========================================
# SUMMARY SYSTEM (9 AM + 5 PM)
# ==========================================
def build_daily_summary(uid):
    history = load_history()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    tasks = [h for h in history if h["user"] == uid and h["date"] == today]

    if not tasks:
        return "📅 No tasks completed today."

    total = sum(h["duration"] for h in tasks)

    out = f"📅 **Daily Summary**\nTotal Focus: {total} min\n\nTasks:\n"
    for h in tasks:
        out += f"• {h['task']} — {h['duration']}m\n"

    return out


def summary_scheduler():
    while True:
        now = datetime.now(LOCAL_TZ).strftime("%H:%M")

        if now in ["09:00", "17:00"]:
            history = load_history()
            users = list({h["user"] for h in history})

            for uid in users:
                send_message(build_daily_summary(uid))

            time.sleep(60)

        time.sleep(20)


# ==========================================
# FLASK STARTUP THREADS
# ==========================================
@app.before_request
def start_threads():
    if not getattr(app, "threads_started", False):
        threading.Thread(target=timer_watcher, daemon=True).start()
        threading.Thread(target=summary_scheduler, daemon=True).start()
        app.threads_started = True


# ==========================================
# BOT ENDPOINT
# ==========================================
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

        return jsonify({"reply": f"🍅 Started **{task}** ({duration} min)"})

    if msg.startswith("status"):
        with timers_lock:
            if user not in timers:
                return jsonify({"reply": "❌ No active session"})
            info = timers[user]
            remaining = info["end"] - datetime.utcnow()
            sec = max(int(remaining.total_seconds()), 0)
            return jsonify({"reply": f"{info['task']} — {sec//60}m {sec%60}s left"})

    if msg.startswith("stop"):
        with timers_lock:
            if user in timers:
                timers.pop(user, None)
                return jsonify({"reply": "🛑 Stopped"})
        return jsonify({"reply": "❌ No session to stop"})

    if msg.startswith("today"):
        return jsonify({"reply": build_daily_summary(user)})

    if msg.startswith("week"):
        history = load_history()
        user_hist = [h for h in history if h["user"] == user]

        if not user_hist:
            return jsonify({"reply": "📭 No history"})

        summary = {}
        for h in user_hist:
            summary[h["date"]] = summary.get(h["date"], 0) + 1

        out = "📊 Weekly Summary\n\n"
        for d, c in summary.items():
            out += f"{d}: {'🍅'*c} ({c})\n"

        return jsonify({"reply": out})

    if msg.startswith("streak"):
        streaks = load_streaks()
        s = streaks.get(user)
        if not s:
            return jsonify({"reply": "🔥 No streak yet"})
        return jsonify({"reply": f"🔥 Streak: {s['current_streak']} days\n🏆 Longest: {s['longest_streak']} days"})

    if msg.startswith("score"):
        scores = load_scores()
        s = scores.get(user)
        if not s:
            return jsonify({"reply": "🎯 No XP yet"})
        return jsonify({"reply": f"🎯 XP: {s['xp']}\n⭐ Level: {s['level']}"})

    return jsonify({"reply": "Commands: start | status | stop | today | week | streak | score"})


# ==========================================
# ROOT
# ==========================================
@app.route("/")
def home():
    return "Pomodoro Bot Running"


# ==========================================
# RUN
# ==========================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
