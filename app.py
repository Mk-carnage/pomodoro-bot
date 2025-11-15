# ============================
# PART 1 — Imports & Config
# ============================

import os
import threading
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

# =============================
# ENVIRONMENT VARIABLES
# =============================
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# =============================
# JSON FILES
# =============================
HISTORY_FILE = "history.json"
SUMMARY_STATE_FILE = "summary_state.json"
STREAK_FILE = "streaks.json"
SCORE_FILE = "scores.json"
TASK_FILE = "tasks.json"

app = Flask(__name__)
timers = {}
timers_lock = threading.Lock()


# =============================
# FILE HELPERS
# =============================
def load_file(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default


def save_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


def load_history(): return load_file(HISTORY_FILE, [])
def save_history(data): save_file(HISTORY_FILE, data)

def load_summary_state(): return load_file(SUMMARY_STATE_FILE, {})
def save_summary_state(data): save_file(SUMMARY_STATE_FILE, data)

def load_streaks(): return load_file(STREAK_FILE, {})
def save_streaks(data): save_file(STREAK_FILE, data)

def load_scores(): return load_file(SCORE_FILE, {})
def save_scores(data): save_file(SCORE_FILE, data)

def load_tasks(): return load_file(TASK_FILE, {})
def save_tasks(data): save_file(TASK_FILE, data)


# =============================
# TOKEN REFRESH
# =============================
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
        r = requests.post(url, params=params).json()
        if "access_token" in r:
            ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + r["access_token"]
            print("🔄 Refreshed Zoho Access Token.")
            return True
    except Exception as e:
        print("Error refreshing token:", e)

    return False


# =============================
# SEND MESSAGE
# =============================
def send_message(text):
    headers = {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}

    try:
        r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers)

        if r.status_code == 401:
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_INCOMING_URL, json={"text": text}, headers=headers)

        return r
    except Exception as e:
        print("Send message error:", e)
        return None


# =============================
# PARSE START COMMAND
# =============================
def parse_start_command(text):
    parts = text.split()
    duration = 25
    task = "Untitled Task"

    if len(parts) >= 3 and parts[1].isdigit():
        duration = int(parts[1])
        task = " ".join(parts[2:])
    elif len(parts) >= 2:
        task = " ".join(parts[1:])

    return duration, task


# =============================
# STREAK SYSTEM
# =============================
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


# =============================
# XP / LEVEL SYSTEM
# =============================
def calculate_level(xp):
    if xp < 100: return 1
    if xp < 250: return 2
    if xp < 500: return 3
    if xp < 1000: return 4
    return xp // 500 + 4


def update_score(user_id, duration, streak):
    scores = load_scores()

    user = scores.get(user_id, {"xp": 0, "level": 1})

    base_xp = duration
    streak_bonus = streak * 5
    long_bonus = 10 if duration >= 30 else 0

    gained = base_xsum = base_xp + streak_bonus + long_bonus
    user["xp"] += gained
    user["level"] = calculate_level(user["xp"])

    scores[user_id] = user
    save_scores(scores)

    return gained, user["xp"], user["level"]


# =============================
# COUNT TODAY'S POMODOROS
# =============================
def count_pomodoros_today(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    history = load_history()

    return sum(
        1 for h in history
        if h.get("user") == user_id and
        h.get("date") == today and
        h.get("type", "pomodoro") == "pomodoro"
    )
# =============================
# BREAK SYSTEM — MANUAL BREAK
# =============================
def start_manual_break(user_id, minutes):
    with timers_lock:
        current = timers.get(user_id)
        paused_pomodoro = None

        # If pomodoro is running — pause it
        if current and current.get("type") == "pomodoro":
            remaining = int(max((current["end"] - datetime.utcnow()).total_seconds(), 0))
            paused_pomodoro = {
                "task": current["task"],
                "remaining_seconds": remaining
            }

        # Start break timer
        end_time = datetime.utcnow() + timedelta(minutes=minutes)
        timers[user_id] = {
            "type": "break",
            "end": end_time,
            "task": f"Manual Break ({minutes} min)",
            "duration": minutes,
            "paused_pomodoro": paused_pomodoro
        }

    send_message(f"☕ Break started for {minutes} minutes. (Use `stop break` to resume work.)")
    return True


# =============================
# STOP BREAK
# =============================
def stop_break(user_id):
    with timers_lock:
        current = timers.get(user_id)

        if not current or current.get("type") != "break":
            return False, "❌ No active break to stop."

        paused = current.get("paused_pomodoro")
        timers.pop(user_id, None)

        # Resume paused pomodoro
        if paused:
            remaining = paused["remaining_seconds"]
            new_end = datetime.utcnow() + timedelta(seconds=remaining)

            timers[user_id] = {
                "type": "pomodoro",
                "end": new_end,
                "task": paused["task"],
                "duration": round(remaining / 60, 2),
                "paused_pomodoro": None
            }

            return True, f"▶️ Resumed **{paused['task']}** with {remaining//60}m {remaining%60}s left."

        return True, "🛑 Break stopped."


# =============================
# TIMER WATCHER THREAD
# =============================
def timer_watcher():
    print("⏳ Timer watcher started.")

    while True:
        now = datetime.utcnow()
        completed = []

        # CHECK FINISHED TIMERS
        with timers_lock:
            for uid, info in list(timers.items()):
                if now >= info["end"]:
                    completed.append((uid, info))

        # PROCESS FINISHED TIMERS
        for uid, info in completed:
            try:
                timer_type = info.get("type")

                # -------------------------------------
                # POMODORO FINISHED
                # -------------------------------------
                if timer_type == "pomodoro":
                    task = info["task"]
                    duration = info["duration"]

                    # Save to history
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

                    # Update streak + score
                    current, longest = update_streak_for_user(uid)
                    gained, total_xp, level = update_score(uid, duration, current)

                    # Completion message
                    send_message(
                        f"⏰ Pomodoro Completed!\n"
                        f"✔ **{task}** ({duration} min)\n\n"
                        f"🔥 Streak: {current} days\n"
                        f"🏆 Longest: {longest} days\n\n"
                        f"🎯 XP: +{gained}\n"
                        f"💠 Total XP: {total_xp}\n"
                        f"⭐ Level: {level}"
                    )

                    # -----------------------
                    # AUTO BREAK STARTS HERE
                    # -----------------------
                    completed_today = count_pomodoros_today(uid)

                    auto_break_min = 15 if completed_today % 4 == 0 else 5

                    break_end = datetime.utcnow() + timedelta(minutes=auto_break_min)

                    with timers_lock:
                        timers[uid] = {
                            "type": "break",
                            "end": break_end,
                            "task": f"Auto Break ({auto_break_min} min)",
                            "duration": auto_break_min,
                            "paused_pomodoro": None
                        }

                    send_message(
                        f"☕ Auto-break started for {auto_break_min} minutes.\n"
                        f"(Type `stop break` to skip or `break <min>` to override.)"
                    )

                # -------------------------------------
                # BREAK FINISHED
                # -------------------------------------
                elif timer_type == "break":
                    break_task = info["task"]
                    duration = info["duration"]
                    paused = info.get("paused_pomodoro")

                    # Save break to history
                    history = load_history()
                    history.append({
                        "user": uid,
                        "task": break_task,
                        "duration": duration,
                        "completed_at": datetime.utcnow().isoformat(),
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "type": "break"
                    })
                    save_history(history)

                    if paused:
                        # Resume paused pomodoro
                        remaining = paused["remaining_seconds"]
                        new_end = datetime.utcnow() + timedelta(seconds=remaining)

                        with timers_lock:
                            timers[uid] = {
                                "type": "pomodoro",
                                "end": new_end,
                                "task": paused["task"],
                                "duration": round(remaining / 60, 2),
                                "paused_pomodoro": None
                            }

                        send_message(
                            f"⏰ Break over — Resuming **{paused['task']}** "
                            f"({remaining//60}m {remaining%60}s left)"
                        )
                    else:
                        send_message("☕ Break over! Ready for your next session.")

                        # Suggest next task
                        tasks = load_tasks()
                        user_tasks = tasks.get(uid, [])

                        if user_tasks:
                            next_task = user_tasks[0]
                            send_message(
                                f"⏭ Next task: **{next_task['task']}** "
                                f"({next_task['duration']} min)\n"
                                f"Type `start next` to begin."
                            )

                # -------------------------------------
                # Remove processed timer
                # -------------------------------------
                with timers_lock:
                    current = timers.get(uid)
                    if current and current["end"] == info["end"]:
                        timers.pop(uid, None)

            except Exception as e:
                print("⚠️ Timer error:", e)

        time.sleep(1)


# =============================
# BACKGROUND THREAD STARTER
# =============================
@app.before_request
def start_threads():
    if not getattr(app, "started", False):
        threading.Thread(target=timer_watcher, daemon=True).start()
        app.started = True
        print("🚀 Background threads started.")
# =============================
# PART 3 — Routes, Weekly Chart, Run
# =============================

# -----------------------------
# Weekly Analytics Chart (Combined: count + minutes)
# -----------------------------
def get_weekly_chart(user_id):
    history = load_history()
    if not history:
        return "📭 No history available."

    # Days mapping Mon..Sun
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekly = {d: {"count": 0, "minutes": 0} for d in days}

    for h in history:
        if h.get("user") != user_id:
            continue
        try:
            dt = datetime.fromisoformat(h.get("completed_at"))
        except Exception:
            # fallback: use now date if parsing fails
            dt = datetime.strptime(h.get("date"), "%Y-%m-%d") if h.get("date") else datetime.utcnow()
        weekday = days[dt.weekday()]
        weekly[weekday]["count"] += 1
        weekly[weekday]["minutes"] += int(h.get("duration", 0))

    # Build chart with bars proportional to counts (one '█' per session)
    out = "📊 **WEEKLY ANALYTICS**\n\n"
    total_sessions = 0
    total_minutes = 0
    most_day = None
    most_count = 0

    for day in days:
        count = weekly[day]["count"]
        minutes = weekly[day]["minutes"]
        bar = ("█" * count) if count > 0 else "-"
        out += f"{day}: {bar} {count} ({minutes} min)\n"
        total_sessions += count
        total_minutes += minutes
        if count > most_count:
            most_count = count
            most_day = day

    out += f"\nTotal Sessions: {total_sessions}\n"
    out += f"Total Focus Time: {total_minutes} min\n"
    if most_day:
        out += f"🔥 Most productive day: {most_day} ({most_count} sessions)"

    return out


# -----------------------------
# Helper: Today summary (already used elsewhere) kept as simple wrapper
# -----------------------------
def build_today_summary(user_id):
    return build_daily_summary(user_id)


# -----------------------------
# Bot endpoint — all commands
# -----------------------------
@app.route("/pomodoro", methods=["POST"])
def pomodoro_route():
    data = request.json or {}
    raw = data.get("raw", "").strip()
    msg = raw.lower()
    user = data.get("user")
    if not user:
        return jsonify({"reply": "❌ No user id provided."}), 400

    # ---- Add task ----
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

    # ---- Show tasks ----
    if msg == "tasks":
        tasks = load_tasks()
        user_tasks = tasks.get(user, [])
        if not user_tasks:
            return jsonify({"reply": "📭 No tasks in queue."})
        out = "📋 Task Queue:\n"
        for i, t in enumerate(user_tasks, start=1):
            out += f"{i}. {t['task']} ({t['duration']} min)\n"
        return jsonify({"reply": out})

    # ---- Start next ----
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

    # ---- Remove task ----
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

    # ---- Clear tasks ----
    if msg == "clear tasks":
        tasks = load_tasks()
        tasks[user] = []
        save_tasks(tasks)
        return jsonify({"reply": "🗑 Cleared all tasks."})

    # ---- Manual break ----
    if msg.startswith("break"):
        parts = raw.split()
        if len(parts) == 1:
            minutes = 5
        elif len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
        else:
            return jsonify({"reply": "Usage: break OR break <minutes>"})
        start_manual_break(user, minutes)
        return jsonify({"reply": f"☕ Break started for {minutes} minutes."})

    # ---- Stop break ----
    if msg in ("stop break", "stopbreak", "break stop"):
        ok, message = stop_break(user)
        return jsonify({"reply": message})

    # ---- Start pomodoro ----
    if msg.startswith("start"):
        duration, task_name = parse_start_command(raw)
        end = datetime.utcnow() + timedelta(minutes=duration)
        with timers_lock:
            cur = timers.get(user)
            if cur and cur.get("type") == "break":
                # user wants to start pomodoro during break — cancel break and start pomodoro
                timers.pop(user, None)
            timers[user] = {"type": "pomodoro", "end": end, "task": task_name, "duration": duration, "paused_pomodoro": None}
        return jsonify({"reply": f"🍅 Started **{task_name}** ({duration} min)"})

    # ---- Status ----
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

    # ---- Stop / Cancel ----
    if msg in ("stop", "end", "cancel"):
        with timers_lock:
            if user in timers:
                timers.pop(user, None)
                return jsonify({"reply": "🛑 Stopped."})
        return jsonify({"reply": "❌ No active session."})

    # ---- Resume (for paused pomodoro during break) ----
    if msg == "resume":
        with timers_lock:
            cur = timers.get(user)
            if cur and cur.get("type") == "break" and cur.get("paused_pomodoro"):
                paused = cur.get("paused_pomodoro")
                remaining = paused.get("remaining_seconds", 0)
                end = datetime.utcnow() + timedelta(seconds=remaining)
                timers[user] = {"type": "pomodoro", "end": end, "task": paused.get("task"), "duration": round(remaining / 60, 2), "paused_pomodoro": None}
                return jsonify({"reply": f"⏯ Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."})
        return jsonify({"reply": "❌ Nothing to resume."})

    # ---- Today summary ----
    if msg == "today":
        return jsonify({"reply": build_today_summary(user)})

    # ---- Weekly summary (simple count per date) ----
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

    # ---- Weekly analytics chart (ASCII combined) ----
    if msg in ("chart", "weekly chart", "analytics", "weekly analytics"):
        chart_text = get_weekly_chart(user)
        return jsonify({"reply": chart_text})

    # ---- Streak ----
    if msg == "streak":
        streaks = load_streaks()
        s = streaks.get(user)
        if not s:
            return jsonify({"reply": "🔥 No streak yet"})
        return jsonify({"reply": f"🔥 Streak: {s['current_streak']} days\n🏆 Longest: {s['longest_streak']} days"})

    # ---- Score ----
    if msg == "score":
        scores = load_scores()
        s = scores.get(user)
        if not s:
            return jsonify({"reply": "🎯 No XP yet"})
        return jsonify({"reply": f"🎯 XP: {s['xp']}\n⭐ Level: {s['level']}"})

    # ---- Help / fallback ----
    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks"})
    

# -----------------------------
# Root endpoint
# -----------------------------
@app.route("/")
def home():
    return "Pomodoro Bot Running"


# -----------------------------
# Run server
# -----------------------------
if __name__ == "__main__":
    # ensure JSON files exist to prevent crashes
    for fn, init in [
        (HISTORY_FILE, []),
        (SUMMARY_STATE_FILE, {}),
        (STREAK_FILE, {}),
        (SCORE_FILE, {}),
        (TASK_FILE, {})
    ]:
        if not os.path.exists(fn):
            save_file(fn, init)

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
