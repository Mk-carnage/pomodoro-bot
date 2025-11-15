# ============================================
# Pomodoro Bot (Flask) - Full code with export fix
# and Smart AI Suggestions (Option A, calm & friendly)
# ============================================

import os
import threading
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

# ============================================
# ENVIRONMENT VARIABLES
# ============================================

ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# ============================================
# JSON FILES
# ============================================

HISTORY_FILE = "history.json"
SUMMARY_STATE_FILE = "summary_state.json"
STREAK_FILE = "streaks.json"
SCORE_FILE = "scores.json"
TASK_FILE = "tasks.json"

app = Flask(__name__)
timers = {}
timers_lock = threading.Lock()


# ============================================
# FILE HELPERS
# ============================================

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


# ============================================
# TOKEN REFRESH
# ============================================

def refresh_access_token():
    global ZOHO_OAUTH_TOKEN

    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return False

    url = "https://accounts.zoho.in/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
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


# ============================================
# SEND MESSAGE TO ZOHO CLIQ
# ============================================

def send_message(text):
    headers = {
        "Authorization": ZOHO_OAUTH_TOKEN,
        "Content-Type": "application/json"
    }

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


# ============================================
# PARSE START COMMAND
# ============================================

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


# ============================================
# STREAK SYSTEM
# ============================================

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


# ============================================
# XP / LEVEL SYSTEM
# ============================================

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

    gained = base_xp + streak_bonus + long_bonus
    user["xp"] += gained
    user["level"] = calculate_level(user["xp"])

    scores[user_id] = user
    save_scores(scores)

    return gained, user["xp"], user["level"]


# ============================================
# COUNT TODAY'S POMODOROS
# ============================================

def count_pomodoros_today(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    history = load_history()

    return sum(
        1 for h in history
        if h.get("user") == user_id and
           h.get("date") == today and
           h.get("type", "pomodoro") == "pomodoro"
    )

# ============================================
# PART 2 — Break System, Timer Watcher & Threads
# ============================================

# -----------------------------
# Start a manual break (pauses a running pomodoro if present)
# -----------------------------
def start_manual_break(user_id, minutes):
    """
    Starts a manual break for user_id.
    If a pomodoro is running it will be paused and stored in paused_pomodoro.
    """

    with timers_lock:
        current = timers.get(user_id)
        paused_pomodoro = None

        # if a pomodoro is running, pause it
        if current and current.get("type") == "pomodoro":
            remaining = int(max((current["end"] - datetime.utcnow()).total_seconds(), 0))
            paused_pomodoro = {
                "task": current.get("task"),
                "remaining_seconds": remaining
            }

        # start/override break timer
        end_time = datetime.utcnow() + timedelta(minutes=minutes)
        timers[user_id] = {
            "type": "break",
            "end": end_time,
            "task": f"Manual Break ({minutes} min)",
            "duration": minutes,
            "paused_pomodoro": paused_pomodoro
        }

    # notify user
    send_message(f"☕ Break started for {minutes} minutes. (Type `stop break` to cancel and resume.)")
    return True


# -----------------------------
# Stop an active break (resumes paused pomodoro if any)
# -----------------------------
def stop_break(user_id):
    """
    Stops the current break for user_id.
    If a paused pomodoro exists it will be resumed.
    Returns (ok:bool, message:str)
    """
    with timers_lock:
        current = timers.get(user_id)
        if not current or current.get("type") != "break":
            return False, "❌ No active break to stop."

        paused = current.get("paused_pomodoro")
        # remove current break
        timers.pop(user_id, None)

        if paused:
            remaining = paused.get("remaining_seconds", 0)
            new_end = datetime.utcnow() + timedelta(seconds=remaining)
            timers[user_id] = {
                "type": "pomodoro",
                "end": new_end,
                "task": paused.get("task"),
                "duration": round(remaining / 60, 2),
                "paused_pomodoro": None
            }
            return True, f"▶️ Break stopped. Resumed **{paused.get('task')}** with {remaining//60}m {remaining%60}s left."
        else:
            return True, "🛑 Break stopped."


# -----------------------------
# TIMER WATCHER (runs in background)
# -----------------------------
def timer_watcher():
    """
    Background thread that checks timers every second,
    processes completed pomodoros and breaks, updates history,
    streaks, scores, and starts auto-breaks / resumes paused pomodoros.
    """
    print("⏳ Timer watcher thread started.")
    while True:
        now = datetime.utcnow()
        to_process = []

        # collect expired timers
        with timers_lock:
            for uid, info in list(timers.items()):
                try:
                    if now >= info.get("end"):
                        to_process.append((uid, info))
                except Exception:
                    # malformed timer entry - remove it
                    timers.pop(uid, None)

        # process each expired timer outside lock
        for uid, info in to_process:
            try:
                ttype = info.get("type", "pomodoro")

                # ---------- POMODORO COMPLETED ----------
                if ttype == "pomodoro":
                    task = info.get("task", "Untitled Task")
                    duration = info.get("duration", 25)

                    # append history entry
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

                    # update streak and score
                    current_streak, longest = update_streak_for_user(uid)
                    gained, total_xp, level = update_score(uid, duration, current_streak)

                    # notify completion
                    send_message(
                        f"⏰ Pomodoro completed!\n"
                        f"✔ Task: **{task}** ({duration} min)\n\n"
                        f"🔥 Streak: {current_streak} days\n"
                        f"🏆 Longest streak: {longest} days\n\n"
                        f"🎯 XP earned: +{gained}\n"
                        f"💠 Total XP: {total_xp}\n"
                        f"⭐ Level: {level}"
                    )

                    # AUTO-BREAK logic: short/long break
                    completed_today = count_pomodoros_today(uid)
                    # long break after every 4 pomodoros in a day
                    auto_break_min = 15 if (completed_today % 4 == 0) else 5

                    with timers_lock:
                        timers[uid] = {
                            "type": "break",
                            "end": datetime.utcnow() + timedelta(minutes=auto_break_min),
                            "task": f"Auto Break ({auto_break_min} min)",
                            "duration": auto_break_min,
                            "paused_pomodoro": None
                        }

                    send_message(f"☕ Auto-break started for {auto_break_min} minutes. (Type `stop break` to cancel or `break <min>` to override.)")

                # ---------- BREAK COMPLETED ----------
                elif ttype == "break":
                    br_task = info.get("task", "Break")
                    br_duration = info.get("duration", 5)
                    paused = info.get("paused_pomodoro")

                    # save break history
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

                    # if a pomodoro was paused, resume it
                    if paused:
                        remaining = paused.get("remaining_seconds", 0)
                        new_end = datetime.utcnow() + timedelta(seconds=remaining)
                        with timers_lock:
                            timers[uid] = {
                                "type": "pomodoro",
                                "end": new_end,
                                "task": paused.get("task"),
                                "duration": round(remaining / 60, 2),
                                "paused_pomodoro": None
                            }
                        send_message(f"⏰ Break over — resuming **{paused.get('task')}** with {remaining//60}m {remaining%60}s left.")
                    else:
                        send_message("☕ Break over! Ready to get back to work.")
                        # suggest next queued task if available
                        tasks = load_tasks()
                        user_tasks = tasks.get(uid, [])
                        if user_tasks:
                            next_task = user_tasks[0]
                            send_message(f"⏭ Next task in queue: **{next_task['task']}** ({next_task['duration']} min). Type `start next` to continue.")

                # After processing, remove timer if it wasn't replaced by a resume
                with timers_lock:
                    cur = timers.get(uid)
                    # compare by "end" datetimes (they are datetime objects)
                    if cur is None:
                        pass
                    else:
                        # if same end as processed info then remove it (processed)
                        if cur.get("end") == info.get("end") and cur.get("type") == info.get("type"):
                            timers.pop(uid, None)

            except Exception as e:
                print("⚠️ Error processing timer for user", uid, ":", e)

        time.sleep(1)


# -----------------------------
# Start background threads on first request
# -----------------------------
@app.before_request
def start_threads():
    if not getattr(app, "threads_started", False):
        threading.Thread(target=timer_watcher, daemon=True).start()
        app.threads_started = True
        print("🚀 Background threads started (timer watcher).")

# ============================================
# PART 3 — Routes: /pomodoro, weekly chart, summaries
# ============================================

# -----------------------------
# Weekly Analytics Chart (Combined: count + minutes)
# -----------------------------
def get_weekly_chart(user_id):
    history = load_history()
    if not history:
        return "📭 No history available."

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekly = {d: {"count": 0, "minutes": 0} for d in days}

    for h in history:
        if h.get("user") != user_id:
            continue
        # parse completed_at safely
        try:
            dt = datetime.fromisoformat(h.get("completed_at"))
        except Exception:
            try:
                dt = datetime.strptime(h.get("date"), "%Y-%m-%d")
            except Exception:
                dt = datetime.utcnow()
        weekday = days[dt.weekday()]
        weekly[weekday]["count"] += 1
        weekly[weekday]["minutes"] += int(h.get("duration", 0))

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
# Today summary wrapper (uses existing build_daily_summary)
# -----------------------------
def build_today_summary(user_id):
    return build_daily_summary(user_id)


# -----------------------------------------
# Smart AI Suggestions (Option A: Calm & Friendly)
# -----------------------------------------
def smart_suggestions(user_id):
    history = load_history()
    tasks = load_tasks().get(user_id, [])
    scores = load_scores().get(user_id, {"xp": 0, "level": 1})
    streaks = load_streaks().get(user_id, {"current_streak": 0})

    suggestions = []
    now = datetime.now(LOCAL_TZ)
    hour = now.hour
    level = scores.get("level", 1)
    streak = streaks.get("current_streak", 0)

    # -------------- 1. Time-of-day suggestions --------------
    if hour < 12:
        suggestions.append("A fresh morning! Maybe review something you learned recently.")
    elif 12 <= hour < 17:
        suggestions.append("Good afternoon! This is a great time for focused deep work.")
    elif 17 <= hour < 21:
        suggestions.append("Evening vibes — perfect for light review or creative tasks.")
    else:
        suggestions.append("It's pretty late. How about planning tomorrow's tasks?")

    # -------------- 2. Streak-based motivation --------------
    if streak >= 5:
        suggestions.append(f"You're on a {streak}-day streak! Keep the momentum with a short session.")
    elif 1 <= streak <= 4:
        suggestions.append("You're building consistency — one Pomodoro today will strengthen your streak.")
    else:
        suggestions.append("Starting your streak today can set a good tone for the week.")

    # -------------- 3. XP / Level adaptive suggestions --------------
    if level <= 3:
        suggestions.append("You're at an early level — try a simple 15–20 min session to progress.")
    elif 4 <= level <= 7:
        suggestions.append("You're leveling up well — a single strong session can boost your XP.")
    else:
        suggestions.append("High-level focus! A long deep-work task might feel rewarding.")

    # -------------- 4. Idle-time detection --------------
    last_activity = None
    for h in reversed(history):
        if h.get("user") == user_id:
            try:
                last_activity = datetime.fromisoformat(h.get("completed_at"))
            except:
                pass
            break

    if last_activity:
        idle_hours = (datetime.utcnow() - last_activity).total_seconds() / 3600
        if idle_hours >= 3:
            suggestions.append("It's been a while since your last session — a quick 10 min focus might help restart.")

    # -------------- 5. Suggest queued tasks --------------
    if tasks:
        suggestions.append(f"You could continue your next queued task: **{tasks[0]['task']}**.")

    # -------------- 6. Frequent past tasks --------------
    freq = {}
    for h in history:
        if h.get("user") == user_id and h.get("type") == "pomodoro":
            t = h.get("task")
            freq[t] = freq.get(t, 0) + 1

    if freq:
        top_task = max(freq, key=freq.get)
        suggestions.append(f"You've worked a lot on **{top_task}** — maybe continue improving it.")

    # -------------- If nothing to suggest --------------
    if not suggestions:
        suggestions = ["Try a small 15-minute session to get started."]

    return suggestions


# -----------------------------
# /pomodoro route (all commands)
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

    # ---- Weekly summary (simple) ----
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

    # ---- Weekly analytics chart ----
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

    # ---- Export Commands ----
    if msg.startswith("export"):
        if msg in ("export today", "export daily"):
            filepath = generate_daily_report(user)
            link = request.host_url + "download/" + os.path.basename(filepath)
            return jsonify({"reply": f"📄 Daily Report Ready!\nDownload: {link}"})

        if msg in ("export week", "export weekly"):
            filepath = generate_weekly_report(user)
            link = request.host_url + "download/" + os.path.basename(filepath)
            return jsonify({"reply": f"📄 Weekly Report Ready!\nDownload: {link}"})

        if msg in ("export month", "export monthly"):
            filepath = generate_monthly_report(user)
            link = request.host_url + "download/" + os.path.basename(filepath)
            return jsonify({"reply": f"📄 Monthly Report Ready!\nDownload: {link}"})

        return jsonify({"reply": "❌ Use: export today | export week | export month"})

    # ---- AI Suggestions ----
    if msg in ("suggest", "ai suggest", "suggestions"):
        items = smart_suggestions(user)
        reply = "🤖 *You're doing great.* Here are some gentle suggestions:\n\n"
        for i in items:
            reply += f"• {i}\n"
        return jsonify({"reply": reply})

    # ---- Help / fallback ----
    return jsonify({"reply": "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | export week | export month | suggest"})

# ============================================
# PART 4 — PDF EXPORT + DOWNLOAD ROUTE + export handler
# ============================================

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch

# Ensure reports directory exists
REPORT_DIR = "reports"
if not os.path.exists(REPORT_DIR):
    os.makedirs(REPORT_DIR)


# -----------------------------
# Helper: create PDF
# -----------------------------
def create_pdf(filepath, title, lines):
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    # title
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, height - 80, title)

    # body
    c.setFont("Helvetica", 12)
    y = height - 120

    for line in lines:
        if y < 50:   # page break
            c.showPage()
            c.setFont("Helvetica", 12)
            y = height - 50
        c.drawString(50, y, line)
        y -= 20

    c.save()


# -----------------------------
# DAILY REPORT
# -----------------------------
def generate_daily_report(user_id):
    history = load_history()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    entries = [
        h for h in history
        if h.get("user") == user_id and h.get("date") == today
    ]

    filepath = f"{REPORT_DIR}/daily_{user_id}.pdf"
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


# -----------------------------
# WEEKLY REPORT
# -----------------------------
def generate_weekly_report(user_id):
    history = load_history()
    today = datetime.utcnow()
    start = today - timedelta(days=7)

    entries = [
        h for h in history
        if h.get("user") == user_id and datetime.fromisoformat(h.get("completed_at")) >= start
    ]

    filepath = f"{REPORT_DIR}/weekly_{user_id}.pdf"
    lines = []
    total = 0

    if not entries:
        lines.append("No activity in last 7 days.")
    else:
        lines.append("Weekly Pomodoro Activity:\n")
        for h in entries:
            date = h.get("date")
            t = h.get("task")
            d = h.get("duration", 0)
            tt = h.get("type")
            lines.append(f"{date}: {t} ({d} min, {tt})")
            if tt == "pomodoro":
                total += d

        lines.append("")
        lines.append(f"Total Focus Time (7 days): {total} min")

    create_pdf(filepath, "Weekly Report", lines)
    return filepath


# -----------------------------
# MONTHLY REPORT
# -----------------------------
def generate_monthly_report(user_id):
    history = load_history()
    today = datetime.utcnow()
    start = today - timedelta(days=30)

    entries = [
        h for h in history
        if h.get("user") == user_id and datetime.fromisoformat(h.get("completed_at")) >= start
    ]

    filepath = f"{REPORT_DIR}/monthly_{user_id}.pdf"
    lines = []
    total = 0
    p_count = 0
    b_count = 0

    if not entries:
        lines.append("No activity in last 30 days.")
    else:
        lines.append("Monthly Pomodoro Activity:\n")
        for h in entries:
            date = h.get("date")
            t = h.get("task")
            d = h.get("duration")
            tt = h.get("type")
            lines.append(f"{date}: {t} ({d} min, {tt})")
            if tt == "pomodoro":
                p_count += 1
                total += d
            else:
                b_count += 1

        lines.append("")
        lines.append(f"Pomodoro Sessions: {p_count}")
        lines.append(f"Break Sessions: {b_count}")
        lines.append(f"Total Focus Time: {total} min")

    create_pdf(filepath, "Monthly Report", lines)
    return filepath


# -----------------------------
# PUBLIC PDF DOWNLOAD ENDPOINT
# -----------------------------
@app.route("/download/<path:filename>")
def download_report(filename):
    filepath = os.path.join(REPORT_DIR, filename)
    if not os.path.exists(filepath):
        return "❌ File not found.", 404

    from flask import send_file
    return send_file(filepath, as_attachment=True)


# -----------------------------
# EXPORT COMMAND ENDPOINT
# -----------------------------
@app.route("/export", methods=["POST"])
def export_route():
    data = request.json or {}
    raw = data.get("raw", "")
    msg = raw.lower().strip()
    user = data.get("user")

    if not user:
        return jsonify({"reply": "❌ Missing user id."}), 400

    # DAILY
    if msg in ("export today", "export daily"):
        filepath = generate_daily_report(user)
        link = request.host_url + "download/" + os.path.basename(filepath)
        return jsonify({"reply": f"📄 Daily Report Ready!\nDownload: {link}"})

    # WEEKLY
    if msg in ("export week", "export weekly"):
        filepath = generate_weekly_report(user)
        link = request.host_url + "download/" + os.path.basename(filepath)
        return jsonify({"reply": f"📄 Weekly Report Ready!\nDownload: {link}"})

    # MONTHLY
    if msg in ("export month", "export monthly"):
        filepath = generate_monthly_report(user)
        link = request.host_url + "download/" + os.path.basename(filepath)
        return jsonify({"reply": f"📄 Monthly Report Ready!\nDownload: {link}"})

    return jsonify({"reply": "❌ Use one of:\nexport today | export week | export month"})


# -----------------------------
# RUN (for local testing)
# Render/Railway uses Gunicorn, so this is safe.
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
