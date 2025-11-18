# app.py
"""
Pomodoro bot with server-driven timers + Zoho Card UI (mixed themes).
Supports commands (from Cliq handlers / Postman / Deluge):
- start [<minutes>] [<task name>]
- break [<minutes>]
- stop / cancel / end
- stop break
- resume
- start next
- add task <task name> <duration>
- tasks
- done <n>
- clear tasks
- status/time/progress
- today / week / chart / streak / score
- export today|week|month
- suggest
"""

import os, time, json, threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_file
import requests
from pymongo import MongoClient

# Optional reportlab
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ----------------- CONFIG (ENV) -----------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "pomodoro_db")

ZOHO_BOT_API = os.getenv("ZOHO_BOT_API", "")  # e.g. https://cliq.zoho.com/api/v2/bots/<botunique>/message
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN", "")  # 'Zoho-oauthtoken <token>'
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")
ZOHO_ACCOUNTS_BASE = os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com")

LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Asia/Kolkata")
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc

REPORT_DIR = "reports"
os.makedirs(REPORT_DIR, exist_ok=True)

if not MONGO_URI:
    raise RuntimeError("MONGO_URI must be set in env")

# ----------------- DB -----------------
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
col_timers = db.get_collection("timers")      # {user, type, ends_at, task, duration, paused_pomodoro}
col_history = db.get_collection("history")
col_tasks = db.get_collection("tasks")
col_users = db.get_collection("users")

# ensure index
try:
    col_timers.create_index("user", unique=True)
except Exception:
    pass

# ----------------- Flask -----------------
app = Flask(__name__)

# ----------------- Helpers -----------------
def now_ts(): return int(time.time())
def ts_to_iso(ts:int): return datetime.fromtimestamp(ts, timezone.utc).isoformat()
def iso_to_dt(s: str):
    try:
        if s is None: return datetime.utcnow().replace(tzinfo=timezone.utc)
        s2 = str(s).replace("Z","+00:00")
        return datetime.fromisoformat(s2).astimezone(timezone.utc)
    except:
        return datetime.utcnow().replace(tzinfo=timezone.utc)

def set_active_timer(user: str, timer_obj: dict):
    doc = timer_obj.copy()
    doc["user"] = user
    col_timers.find_one_and_update({"user": user}, {"$set": doc}, upsert=True)

def get_active_timer(user: str):
    return col_timers.find_one({"user": user})

def remove_active_timer(user: str):
    col_timers.delete_one({"user": user})

def append_history(item: dict):
    col_history.insert_one(item)

def load_tasks_for_user(user: str):
    d = col_tasks.find_one({"user": user})
    return d.get("queue", []) if d else []

def save_tasks_for_user(user: str, queue):
    col_tasks.find_one_and_update({"user": user}, {"$set": {"queue": queue}}, upsert=True)

def load_user_stats(user: str):
    doc = col_users.find_one({"user": user})
    if not doc:
        return {"xp":0,"level":1,"current_streak":0,"longest_streak":0,"last_completed_date":None}
    return {
        "xp": doc.get("xp",0),
        "level": doc.get("level",1),
        "current_streak": doc.get("current_streak",0),
        "longest_streak": doc.get("longest_streak",0),
        "last_completed_date": doc.get("last_completed_date")
    }

def save_user_stats(user: str, stats: dict):
    s = stats.copy()
    s["user"] = user
    col_users.find_one_and_update({"user": user}, {"$set": s}, upsert=True)

# ----------------- OAuth / Zoho -----------------
ZOHO_LOCK = threading.Lock()

def refresh_access_token():
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return False
    url = f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
    }
    try:
        r = requests.post(url, params=params, timeout=10)
        j = r.json()
        if "access_token" in j:
            with ZOHO_LOCK:
                ZOHO_OAUTH_TOKEN = "Zoho-oauthtoken " + j["access_token"]
            print("🔄 Zoho token refreshed.")
            return True
        else:
            print("Refresh failed:", j)
    except Exception as e:
        print("Refresh exception:", e)
    return False

def send_zoho_message(text: str, card: dict = None, buttons: list = None):
    """
    Compose payload per Zoho v2 API:
    - Must include text (string)
    - Optional card (object) and buttons (array)
    """
    if not ZOHO_BOT_API:
        print("ZOHO_BOT_API not configured - skipping send.")
        return None
    headers = {
        "Authorization": ZOHO_OAUTH_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {"text": text}
    if card:
        payload["card"] = card
    if buttons:
        payload["buttons"] = buttons

    # debug
    print("📨 Sending to Zoho:", json.dumps(payload)[:800])
    try:
        r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        if r.status_code == 401:
            if refresh_access_token():
                headers["Authorization"] = ZOHO_OAUTH_TOKEN
                r = requests.post(ZOHO_BOT_API, json=payload, headers=headers, timeout=10)
        print("Zoho status:", r.status_code, r.text[:400])
        return r
    except Exception as e:
        print("send_zoho_message exception:", e)
        return None

# ----------------- Scoring / streaks -----------------
def update_streak_for_user(user_id: str):
    s = load_user_stats(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    last = s.get("last_completed_date")
    if last == yesterday:
        s["current_streak"] = s.get("current_streak",0) + 1
    elif last != today:
        s["current_streak"] = 1
    s["longest_streak"] = max(s.get("longest_streak",0), s.get("current_streak",0))
    s["last_completed_date"] = today
    save_user_stats(user_id, s)
    return s["current_streak"], s["longest_streak"]

def calculate_level(xp:int):
    if xp < 100: return 1
    if xp < 250: return 2
    if xp < 500: return 3
    if xp < 1000: return 4
    return xp // 500 + 4

def update_score(user_id:str, duration:int, streak:int):
    s = load_user_stats(user_id)
    base = int(duration)
    streak_bonus = streak * 5
    long_bonus = 10 if duration >= 30 else 0
    gained = base + streak_bonus + long_bonus
    s["xp"] = s.get("xp",0) + gained
    s["level"] = calculate_level(s["xp"])
    save_user_stats(user_id, s)
    return gained, s["xp"], s["level"]

def count_pomodoros_today(user_id: str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return col_history.count_documents({"user": user_id, "date": today, "type": "pomodoro"})

# ----------------- Timer Engine -----------------
timer_threads = {}
timer_threads_lock = threading.Lock()

def _timer_worker(user: str):
    print("Timer worker start:", user)
    while True:
        t = get_active_timer(user)
        if not t:
            print("No active timer - worker exit:", user)
            break
        ends_at = int(t.get("ends_at",0))
        remaining = ends_at - now_ts()
        if remaining <= 0:
            typ = t.get("type", "pomodoro")
            if typ == "pomodoro":
                # record
                completed_iso = ts_to_iso(now_ts())
                hist = {
                    "user": user,
                    "task": t.get("task","Untitled Task"),
                    "duration": int(t.get("duration",25)),
                    "completed_at": completed_iso,
                    "date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "type": "pomodoro"
                }
                append_history(hist)

                # update user stats
                current_streak, longest = update_streak_for_user(user)
                gained, total_xp, level = update_score(user, int(t.get("duration",25)), current_streak)

                # send completion card once (text + card)
                card = build_completed_card(user, hist, current_streak, longest, gained, total_xp, level)
                send_zoho_message(text=f"Pomodoro completed: {hist['task']}", card=card)

                # auto-start break
                completed_today = count_pomodoros_today(user)
                auto_break_min = 15 if (completed_today % 4 == 0) else 5
                break_ends_at = now_ts() + auto_break_min * 60
                set_active_timer(user, {"type":"break","ends_at":break_ends_at,"task":f"Auto Break ({auto_break_min} min)","duration":auto_break_min})
                # send break started card
                bcard = build_break_card(user, auto_break_min, auto=True)
                send_zoho_message(text=f"Auto-break started for {auto_break_min} minutes.", card=bcard)

                # continue to monitor break
                continue

            elif typ == "break":
                # record break
                completed_iso = ts_to_iso(now_ts())
                hist = {
                    "user": user,
                    "task": t.get("task","Break"),
                    "duration": int(t.get("duration",5)),
                    "completed_at": completed_iso,
                    "date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "type": "break"
                }
                append_history(hist)

                paused = t.get("paused_pomodoro")
                if paused:
                    remaining_sec = int(paused.get("remaining_seconds",0))
                    ends_at = now_ts() + remaining_sec
                    set_active_timer(user, {"type":"pomodoro","ends_at":ends_at,"task":paused.get("task"),"duration":round(remaining_sec/60,2)})
                    # notify resumed
                    send_zoho_message(text=f"Break over — resuming {paused.get('task')}",
                                     card=build_resume_card(user, paused))
                    continue
                else:
                    remove_active_timer(user)
                    send_zoho_message(text="Break over! Ready to get back to work.",
                                     card=build_simple_card("Break over", "Ready to get back to work.", theme="standard"))
                    q = load_tasks_for_user(user)
                    if q:
                        send_zoho_message(text=f"Next task in queue: {q[0]['task']}",
                                         card=build_task_queue_card(user))
                    break
            else:
                remove_active_timer(user)
                break
        else:
            sleep_for = min(1.0, max(0.25, remaining))
            time.sleep(sleep_for)

    with timer_threads_lock:
        timer_threads.pop(user, None)
    print("Timer worker ended:", user)

def start_timer_thread_if_needed(user: str):
    with timer_threads_lock:
        if user in timer_threads:
            return
        t = threading.Thread(target=_timer_worker, args=(user,), daemon=True)
        timer_threads[user] = t
        t.start()

def schedule_timer(user:str, typ:str, duration_min: float, task:str, paused_pomodoro=None):
    ends_at = now_ts() + int(duration_min * 60)
    doc = {"type": typ, "ends_at": ends_at, "task": task, "duration": duration_min}
    if paused_pomodoro:
        doc["paused_pomodoro"] = paused_pomodoro
    set_active_timer(user, doc)
    start_timer_thread_if_needed(user)

def rehydrate_timers():
    docs = list(col_timers.find({}))
    for d in docs:
        u = d.get("user")
        if not u: continue
        start_timer_thread_if_needed(u)
    print("Rehydrated timers:", len(docs))

# ----------------- Request parsing -----------------
def parse_incoming_request(data):
    # Accept either {"raw": "...", "user": "123"} or Zoho message object etc.
    if not isinstance(data, dict):
        return "unknown", ""
    for key in ("raw","message","msg","text","raw_message","raw_msg"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return str(data.get("user") or data.get("user_id") or data.get("sender") or "unknown"), v.strip()
    # message_details (Zoho)
    if "message_details" in data and isinstance(data["message_details"], dict):
        md = data["message_details"]
        for key in ("raw_message","message","text"):
            v = md.get(key)
            if isinstance(v, str) and v.strip():
                return str(data.get("user") or md.get("from") or "unknown"), v.strip()
    # fallback: whole raw
    if data.get("raw"):
        return str(data.get("user") or data.get("user_id") or "unknown"), str(data.get("raw"))
    return str(data.get("user") or data.get("user_id") or "unknown"), ""

# ----------------- Cards builders (mix themes) -----------------
def build_simple_card(title, subtitle, theme="standard", thumbnail=None):
    card = {"title": title, "theme": theme, "sections": [{"widgets":[{"type":"label","text": subtitle}]}]}
    if thumbnail:
        card["thumbnail"] = thumbnail
    return card

def build_start_card(user:str, task:str, duration:int):
    card = {
        "title": "Pomodoro Started",
        "theme": "primary",
        "thumbnail": "https://img.icons8.com/color/96/tomato.png",
        "sections": [
            {"widgets":[
                {"type":"label","text": f"Task: {task}"},
                {"type":"label","text": f"Duration: {duration} min"},
            ]}
        ]
    }
    buttons = [
        {"label":"Status","type":"+","action":{"type":"invoke.function","data":{"name":"status_cmd"}}},
        {"label":"Stop","type":"-","action":{"type":"invoke.function","data":{"name":"stop_cmd"}}}
    ]
    return {"text": f"Started {task} ({duration} min)", "card": card, "buttons": buttons}

def build_status_card_payload(user:str):
    cur = get_active_timer(user)
    if not cur:
        card = {"title":"Status","theme":"standard","sections":[{"widgets":[{"type":"label","text":"No active session."}]}]}
        return {"text":"No active session.","card":card,"buttons":[{"label":"Start","type":"+","action":{"type":"invoke.function","data":{"name":"start_cmd"}}}]}
    rem = max(0, cur.get("ends_at",0) - now_ts())
    typ = cur.get("type","pomodoro")
    mins = rem//60
    secs = rem%60
    card = {
        "title":"Session Status",
        "theme":"standard",
        "thumbnail":"https://img.icons8.com/color/96/tomato.png",
        "sections":[
            {"widgets":[
                {"type":"label","text":f"Task: {cur.get('task')}"},
                {"type":"label","text":f"Type: {typ}"},
                {"type":"label","text":f"Remaining: {mins}m {secs}s"},
            ]}
        ]
    }
    buttons = [
        {"label":"Stop","type":"-","action":{"type":"invoke.function","data":{"name":"stop_cmd"}}},
        {"label":"Break","type":"+","action":{"type":"invoke.function","data":{"name":"break_cmd"}}}
    ]
    return {"text": f"Status for {cur.get('task')}", "card": card, "buttons": buttons}

def build_break_card(user:str, minutes:int, auto=False):
    title = "Auto Break Started" if auto else "Break Started"
    theme = "warning"
    card = {
        "title": title,
        "theme": theme,
        "sections":[{"widgets":[
            {"type":"label","text":f"Break for {minutes} minutes."}
        ]}]
    }
    buttons = [{"label":"Stop Break","type":"-","action":{"type":"invoke.function","data":{"name":"stopbreak_cmd"}}}]
    return {"text": f"Break started for {minutes} minutes.", "card": card, "buttons": buttons}

def build_completed_card(user:str, hist:dict, streak:int, longest:int, gained:int, total_xp:int, level:int):
    card = {
        "title":"Pomodoro Completed",
        "theme":"success",
        "thumbnail":"https://img.icons8.com/color/96/tomato.png",
        "sections":[
            {"widgets":[
                {"type":"label","text": f"Task: {hist.get('task')} ({hist.get('duration')} min)"},
                {"type":"label","text": f"Streak: {streak} days  •  Longest: {longest}"},
                {"type":"label","text": f"XP earned: +{gained}  •  Total XP: {total_xp}  •  Level: {level}"}
            ]}
        ]
    }
    buttons = [{"label":"View Summary","type":"+", "action":{"type":"invoke.function","data":{"name":"today_cmd"}}}]
    return {"text": f"Pomodoro complete: {hist.get('task')}", "card": card, "buttons": buttons}

def build_resume_card(user:str, paused:dict):
    card = {"title":"Resume Pomodoro","theme":"primary","sections":[{"widgets":[{"type":"label","text":f"Resuming {paused.get('task')}"}]}]}
    return {"text": f"Resuming {paused.get('task')}", "card": card}

def build_task_queue_card(user:str):
    q = load_tasks_for_user(user)
    if not q:
        return {"text":"No tasks.","card": build_simple_card("Task Queue","No tasks in queue.", theme="standard")}
    widgets = []
    for i, t in enumerate(q, start=1):
        widgets.append({"type":"label","text":f"{i}. {t['task']} ({t['duration']} min)"})
    card = {"title":"Task Queue","theme":"primary","sections":[{"widgets": widgets}]}
    return {"text":"Task queue","card": card}

def build_daily_summary_card(user:str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    entries = list(col_history.find({"user": user, "date": today}))
    if not entries:
        return {"text":"No activity today.","card": build_simple_card("Daily Summary","No activity today.", theme="standard")}
    total = 0
    widgets = []
    for h in entries:
        widgets.append({"type":"label","text":f"- {h.get('task')} ({h.get('duration')}m, {h.get('type')})"})
        if h.get("type")=="pomodoro": total += int(h.get("duration",0))
    card = {"title":"Daily Summary","theme":"standard","sections":[{"widgets":[{"type":"label","text":f"Total focus: {total} min"}] + widgets}]}
    return {"text":"Daily summary", "card":card}

def build_weekly_analytics_card(user:str):
    docs = list(col_history.find({"user": user}))
    if not docs:
        return {"text":"No history","card": build_simple_card("Weekly Analytics","No history available.", theme="standard")}
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    weekly = {d:{"count":0,"minutes":0} for d in days}
    for h in docs:
        try:
            dt = iso_to_dt(h.get("completed_at"))
            weekday = days[dt.weekday()]
        except:
            weekday = days[0]
        weekly[weekday]["count"] += 1
        weekly[weekday]["minutes"] += int(h.get("duration",0))
    widgets = []
    for d in days:
        widgets.append({"type":"label","text":f"{d}: {weekly[d]['count']} sessions ({weekly[d]['minutes']} min)"})
    card = {"title":"Weekly Analytics","theme":"primary","sections":[{"widgets": widgets}]}
    return {"text":"Weekly analytics","card":card}

def build_score_card(user:str):
    s = load_user_stats(user)
    card = {"title":"Your Score","theme":"standard","sections":[{"widgets":[
        {"type":"label","text":f"XP: {s.get('xp',0)}"},
        {"type":"label","text":f"Level: {s.get('level',1)}"},
        {"type":"label","text":f"Streak: {s.get('current_streak',0)} days"},
    ]}]}
    return {"text":"Your score","card":card}

def build_suggestion_card(user:str, suggestions:list):
    # Use poll theme for interactive feel
    widgets = [{"type":"label","text": s} for s in suggestions]
    card = {"title":"Suggestions","theme":"poll","sections":[{"widgets":[{"type":"label","text":"Here are some suggestions:"}] + widgets}]}
    # include a "Try" button
    buttons = [{"label":"Try 15min","type":"+","action":{"type":"invoke.function","data":{"name":"start_15_cmd"}}}]
    return {"text":"Suggestions", "card":card, "buttons":buttons}

# ----------------- Exports -----------------
def create_pdf(filepath, title, lines):
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab missing")
    c = canvas.Canvas(filepath, pagesize=A4)
    w,h = A4
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h-80, title)
    c.setFont("Helvetica", 12)
    y = h-120
    for line in lines:
        if y < 60:
            c.showPage()
            y = h-60
        c.drawString(50,y,line)
        y -= 18
    c.save()

def generate_daily_report(user):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    entries = list(col_history.find({"user": user, "date": today}))
    fp = f"{REPORT_DIR}/daily_{user}.pdf"
    lines = []
    total = 0
    if not entries:
        lines.append("No activity today.")
    else:
        for h in entries:
            lines.append(f"{h.get('date')} - {h.get('task')} ({h.get('duration')}m, {h.get('type')})")
            if h.get("type")=="pomodoro": total += int(h.get("duration",0))
        lines.append(f"Total focus: {total}m")
    create_pdf(fp, "Daily Report", lines)
    return fp

def generate_weekly_report(user):
    start = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=7)
    entries = [h for h in col_history.find({"user":user}) if iso_to_dt(h.get("completed_at")) >= start]
    fp = f"{REPORT_DIR}/weekly_{user}.pdf"
    lines = []
    for h in entries: lines.append(f"{h.get('date')} - {h.get('task')} ({h.get('duration')}m)")
    create_pdf(fp, "Weekly Report", lines)
    return fp

def generate_monthly_report(user):
    start = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=30)
    entries = [h for h in col_history.find({"user":user}) if iso_to_dt(h.get("completed_at")) >= start]
    fp = f"{REPORT_DIR}/monthly_{user}.pdf"
    lines = []
    for h in entries: lines.append(f"{h.get('date')} - {h.get('task')} ({h.get('duration')}m)")
    create_pdf(fp, "Monthly Report", lines)
    return fp

@app.route("/download/<path:filename>")
def download_report(filename):
    fp = os.path.join(REPORT_DIR, filename)
    if not os.path.exists(fp): return "Not found", 404
    return send_file(fp, as_attachment=True)

# ----------------- Suggestions -----------------
def smart_suggestions(user):
    history = list(col_history.find({"user": user}).sort("completed_at",-1).limit(200))
    tasks = load_tasks_for_user(user)
    s = load_user_stats(user)
    out = []
    now = datetime.now(LOCAL_TZ)
    hour = now.hour
    streak = s.get("current_streak",0)
    if hour < 12:
        out.append("Morning: try 25m focus.")
    elif hour < 17:
        out.append("Afternoon: try a 50m deep work.")
    else:
        out.append("Evening: keep it light — 15m review.")
    if streak >= 5:
        out.append(f"You are on a {streak}-day streak — keep going!")
    if tasks:
        out.append(f"Next task: {tasks[0]['task']} ({tasks[0]['duration']}m)")
    # recommend frequent task
    freq = {}
    for h in history:
        if h.get("type")=="pomodoro":
            freq[h.get("task")] = freq.get(h.get("task"),0) + 1
    if freq:
        top = max(freq, key=freq.get)
        out.append(f"You've done {top} often — continue it.")
    if not out: out = ["Try a short 15-minute focus to start."]
    return out

# ----------------- Request routes -----------------
@app.route("/", methods=["GET","HEAD"])
def home(): return "OK", 200

@app.route("/pomodoro", methods=["POST"])
def pomodoro_route():
    data = request.json or {}
    user, raw = parse_incoming_request(data)
    user = str(user)
    raw = (raw or "").strip()
    cmd = raw.lower()

    # --- add task
    if cmd.startswith("add task"):
        parts = raw.split()
        if len(parts) < 4: return jsonify({"reply":"Usage: add task <task name> <duration>"}), 200
        duration = parts[-1]
        if not duration.isdigit(): return jsonify({"reply":"Duration must be a number"}),200
        duration = int(duration)
        name = " ".join(parts[2:-1])
        q = load_tasks_for_user(user); q.append({"task":name,"duration":duration}); save_tasks_for_user(user,q)
        return jsonify({"reply": f"📝 Added task: {name} ({duration} min)"}), 200

    # tasks
    if cmd == "tasks":
        q = load_tasks_for_user(user)
        if not q: return jsonify({"reply":"📭 No tasks in queue."}),200
        out = "📋 Task Queue:\n" + "\n".join([f"{i+1}. {t['task']} ({t['duration']}m)" for i,t in enumerate(q)])
        # also send card
        payload = build_task_queue_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"))
        return jsonify({"reply": out}), 200

    # start next
    if cmd == "start next":
        q = load_tasks_for_user(user)
        if not q: return jsonify({"reply":"No tasks in queue."}),200
        next_task = q.pop(0); save_tasks_for_user(user,q)
        duration = next_task["duration"]; task_name = next_task["task"]
        cur = get_active_timer(user)
        if cur and cur.get("type")=="break": remove_active_timer(user)
        schedule_timer(user,"pomodoro", duration, task_name)
        payload = build_start_card(user, task_name, duration)
        send_zoho_message(text=payload["text"], card=payload.get("card"), buttons=payload.get("buttons"))
        return jsonify({"reply": f"Started next: {task_name} ({duration}m)"}),200

    # done n
    if cmd.startswith("done"):
        parts = cmd.split()
        if len(parts)!=2 or not parts[1].isdigit(): return jsonify({"reply":"Usage: done <task_number>"}),200
        idx = int(parts[1])-1
        q = load_tasks_for_user(user)
        if idx<0 or idx>=len(q): return jsonify({"reply":"Invalid task number."}),200
        removed = q.pop(idx); save_tasks_for_user(user,q)
        return jsonify({"reply": f"Removed: {removed['task']}"}),200

    if cmd == "clear tasks":
        save_tasks_for_user(user, []); return jsonify({"reply":"Cleared all tasks."}),200

    # break <minutes> or break
    if cmd.startswith("break"):
        parts = raw.split()
        minutes = 5
        if len(parts)>=2 and parts[1].isdigit():
            minutes = int(parts[1])
        cur = get_active_timer(user)
        paused = None
        if cur and cur.get("type")=="pomodoro":
            remaining = max(0, cur.get("ends_at",0) - now_ts())
            paused = {"task":cur.get("task"), "remaining_seconds": remaining}
            remove_active_timer(user)
        schedule_timer(user, "break", minutes, f"Manual Break ({minutes} min)", paused_pomodoro=paused)
        payload = build_break_card(user, minutes, auto=False)
        send_zoho_message(text=payload["text"], card=payload.get("card"), buttons=payload.get("buttons"))
        return jsonify({"reply": f"Break started for {minutes} minutes."}),200

    # stop break
    if cmd in ("stop break","stopbreak","break stop"):
        cur = get_active_timer(user)
        if not cur or cur.get("type")!="break": return jsonify({"reply":"No active break."}),200
        paused = cur.get("paused_pomodoro")
        remove_active_timer(user)
        if paused:
            remaining = int(paused.get("remaining_seconds",0))
            schedule_timer(user, "pomodoro", remaining/60.0, paused.get("task"))
            send_zoho_message(text=f"Resumed {paused.get('task')}", card=build_resume_card(user, paused))
            return jsonify({"reply":"Break stopped. Resumed previous session."}),200
        else:
            return jsonify({"reply":"Break stopped."}),200

    # start [<minutes>] [task name...]
    if cmd.startswith("start"):
        parts = raw.split()
        duration = 25
        task_name = "Untitled Task"
        # cases:
        # - "start 1" -> duration=1, task=Untitled
        # - "start 1 homework" -> duration=1, task=homework
        # - "start homework" -> task=homework, duration default 25
        if len(parts) >= 2:
            if parts[1].isdigit():
                duration = int(parts[1])
                if len(parts) >= 3:
                    task_name = " ".join(parts[2:])
            else:
                task_name = " ".join(parts[1:]) or "Untitled Task"
        # cancel break if active
        cur = get_active_timer(user)
        if cur and cur.get("type")=="break": remove_active_timer(user)
        schedule_timer(user, "pomodoro", duration, task_name)
        payload = build_start_card(user, task_name, duration)
        send_zoho_message(text=payload["text"], card=payload.get("card"), buttons=payload.get("buttons"))
        return jsonify({"reply": f"Started {task_name} ({duration} min)"}),200

    # status/time/progress -> send card containing data
    if cmd in ("status","time","progress"):
        payload = build_status_card_payload(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"), buttons=payload.get("buttons"))
        return jsonify({"reply":"Status card sent."}),200

    # stop / cancel / end
    if cmd in ("stop","end","cancel"):
        cur = get_active_timer(user)
        if cur:
            remove_active_timer(user)
            send_zoho_message(text="Session stopped.", card=build_simple_card("Stopped","Session stopped.", theme="danger"))
            return jsonify({"reply":"Stopped."}),200
        return jsonify({"reply":"No active session."}),200

    # resume (if in break with paused pomodoro)
    if cmd == "resume":
        cur = get_active_timer(user)
        if cur and cur.get("type")=="break" and cur.get("paused_pomodoro"):
            paused = cur.get("paused_pomodoro")
            remaining = paused.get("remaining_seconds",0)
            schedule_timer(user, "pomodoro", remaining/60.0, paused.get("task"))
            payload = build_resume_card(user, paused)
            send_zoho_message(text=payload["text"], card=payload.get("card"))
            return jsonify({"reply":"Resumed."}),200
        return jsonify({"reply":"Nothing to resume."}),200

    # today -> daily summary card
    if cmd == "today":
        payload = build_daily_summary_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"))
        return jsonify({"reply":"Daily summary sent."}),200

    # week -> weekly analytics card
    if cmd == "week":
        payload = build_weekly_analytics_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"))
        return jsonify({"reply":"Weekly analytics sent."}),200

    # chart / analytics -> same as weekly
    if cmd in ("chart","analytics","weekly chart","weekly analytics"):
        payload = build_weekly_analytics_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"))
        return jsonify({"reply":"Analytics sent."}),200

    # streak -> score/streak
    if cmd == "streak":
        s = load_user_stats(user)
        card = {"title":"Streaks","theme":"standard","sections":[{"widgets":[
            {"type":"label","text":f"Current streak: {s.get('current_streak',0)} days"},
            {"type":"label","text":f"Longest streak: {s.get('longest_streak',0)} days"}
        ]}]}
        send_zoho_message(text="Streak info", card=card)
        return jsonify({"reply":"Streak sent."}),200

    # score
    if cmd == "score":
        payload = build_score_card(user)
        send_zoho_message(text=payload["text"], card=payload.get("card"))
        return jsonify({"reply":"Score sent."}),200

    # export
    if cmd.startswith("export"):
        if not REPORTLAB_AVAILABLE:
            return jsonify({"reply":"Export not available (reportlab missing)."}),200
        if cmd in ("export today","export daily"):
            fp = generate_daily_report(user)
        elif cmd in ("export week","export weekly"):
            fp = generate_weekly_report(user)
        elif cmd in ("export month","export monthly"):
            fp = generate_monthly_report(user)
        else:
            return jsonify({"reply":"Use: export today|week|month"}),200
        link = request.host_url.rstrip("/") + "/download/" + os.path.basename(fp)
        send_zoho_message(text=f"Report ready: {link}")
        return jsonify({"reply": f"Report: {link}"}),200

    # suggest
    if cmd in ("suggest","ai suggest","suggestions"):
        items = smart_suggestions(user)
        payload = build_suggestion_card(user, items)
        send_zoho_message(text=payload["text"], card=payload.get("card"), buttons=payload.get("buttons"))
        return jsonify({"reply":"Suggestions sent."}),200

    # fallback - help
    help_text = "Commands: start | break | stop break | resume | status | stop | today | week | chart | streak | score | add task | tasks | start next | done | clear tasks | export today | suggest"
    return jsonify({"reply": help_text}), 200

# ----------------- Boot -----------------
if __name__ == "__main__":
    print("Starting Pomodoro Bot with Card UI...")
    rehydrate_timers()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)))
