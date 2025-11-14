# app.py
from flask import Flask, request, jsonify
import time
import threading
import requests
import os

app = Flask(__name__)

# -------------------------
# CONFIG — fill these
# -------------------------
ZOHO_INCOMING_URL = os.getenv("ZOHO_INCOMING_URL")
ZOHO_OAUTH_TOKEN = os.getenv("ZOHO_OAUTH_TOKEN")

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
 # set if you want auto-refresh

# Defaults
DEFAULT_POMODORO_MIN = 25
DEFAULT_BREAK_MIN = 5

# -------------------------
# In-memory session store
# sessions structure per user_id:
# {
#   "type": "work",
#   "start": timestamp,
#   "duration": seconds,
#   "remaining": seconds,
#   "paused": False,
#   "break": { "start": timestamp, "duration": seconds }  # optional
# }
# -------------------------
sessions = {}
sessions_lock = threading.Lock()

# Headers helper
def zoho_headers():
    return {"Authorization": ZOHO_OAUTH_TOKEN, "Content-Type": "application/json"}

# -------------------------
# Notify function (sends to Zoho incoming webhook)
# retries once if token expired and refresh available
# -------------------------
def notify_user(message):
    try:
        r = requests.post(ZOHO_INCOMING_URL, json={"text": message}, headers=zoho_headers(), timeout=10)
        if r.status_code == 401 and CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN:
            # try refreshing token once
            print("Zoho returned 401 — attempting token refresh...")
            if refresh_access_token():
                r = requests.post(ZOHO_INCOMING_URL, json={"text": message}, headers=zoho_headers(), timeout=10)
        print("Notify -> status:", r.status_code, r.text if r is not None else "no response")
        return r
    except Exception as e:
        print("Error sending notify:", e)
        return None

# -------------------------
# Token refresh (if you configured refresh token)
# -------------------------
def refresh_access_token():
    global ZOHO_OAUTH_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return False
    url = "https://accounts.zoho.in/oauth/v2/token"
    data = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
    }
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.status_code == 200:
            j = r.json()
            access = j.get("access_token")
            if access:
                ZOHO_OAUTH_TOKEN = f"Zoho-oauthtoken {access}"
                print("Access token refreshed successfully.")
                return True
        print("Failed to refresh token:", r.status_code, r.text)
    except Exception as e:
        print("Exception refreshing token:", e)
    return False

# -------------------------
# Background watcher
# -------------------------
def timer_watcher():
    print("Timer watcher thread started.")
    while True:
        now = time.time()
        with sessions_lock:
            for user_id in list(sessions.keys()):
                session = sessions.get(user_id)
                if not session:
                    continue

                # If break running
                if "break" in session:
                    b = session["break"]
                    elapsed = now - b["start"]
                    if elapsed >= b["duration"]:
                        # end break, resume work
                        session.pop("break", None)
                        session["paused"] = False
                        session["start"] = now
                        try:
                            notify_user(f"🟢 Break finished! Type `start` to begin next Pomodoro.")
                        except Exception as e:
                            print("Notify exception on break end:", e)
                    # else continue to next user
                    continue

                # Work session check (active or paused)
                if not session.get("paused", False):
                    elapsed = now - session["start"]
                    remaining = session["remaining"] - elapsed
                else:
                    remaining = session["remaining"]

                if remaining <= 0:
                    # session finished
                    sessions.pop(user_id, None)
                    try:
                        notify_user("⏰ Pomodoro completed! Type `break` to start your 5 min break.")
                    except Exception as e:
                        print("Notify exception on pomodoro end:", e)

        time.sleep(1)  # check every second

# start watcher in a way that works with gunicorn / other servers:
@app.before_request
def start_background_thread():
    if not getattr(app, "watcher_thread_started", False):
        t = threading.Thread(target=timer_watcher, daemon=True)
        t.start()
        app.watcher_thread_started = True


# -------------------------
# Helper: create session
# -------------------------
def start_pomodoro_for(user_id, minutes):
    secs = int(minutes * 60)
    with sessions_lock:
        sessions[user_id] = {
            "type": "work",
            "start": time.time(),
            "duration": secs,
            "remaining": secs,
            "paused": False
        }

# -------------------------
# Flask endpoint for Deluge -> POST { "message": "start 25", "user": "12345" }
# Responds with JSON: { "reply": "..." }
# -------------------------
@app.route("/pomodoro", methods=["POST"])
def pomodoro_endpoint():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"reply": "Invalid request. Send JSON: { message, user }"}), 400

    raw_msg = str(data.get("message", "")).strip()
    user = str(data.get("user", "unknown"))

    msg = raw_msg.lower().strip()
    reply = "🙂 Commands: start [minutes] | break | stop | status"

    try:
        if msg.startswith("start"):
            parts = msg.split()
            if len(parts) == 2 and parts[1].isdigit():
                minutes = int(parts[1])
            else:
                minutes = DEFAULT_POMODORO_MIN
            start_pomodoro_for(user, minutes)
            reply = f"🍅 Pomodoro started for {minutes} minutes!"
            return jsonify({"reply": reply})

        if msg == "break":
            with sessions_lock:
                if user not in sessions or sessions[user]["type"] != "work":
                    return jsonify({"reply": "❌ No active Pomodoro to pause. Type `start` first."})
                # pause work, compute remaining
                elapsed = time.time() - sessions[user]["start"]
                sessions[user]["remaining"] -= elapsed
                sessions[user]["paused"] = True
                # start break
                sessions[user]["break"] = {
                    "start": time.time(),
                    "duration": DEFAULT_BREAK_MIN * 60
                }
            reply = f"☕ Break started for {DEFAULT_BREAK_MIN} minutes!"
            return jsonify({"reply": reply})

        if msg == "stop":
            with sessions_lock:
                # if break in progress -> stop break and resume work
                if user in sessions and "break" in sessions[user]:
                    sessions[user].pop("break", None)
                    # resume: set start to now (work remaining unchanged)
                    sessions[user]["start"] = time.time()
                    sessions[user]["paused"] = False
                    rem = int(sessions[user]["remaining"])
                    m = rem // 60
                    s = rem % 60
                    return jsonify({"reply": f"☕ Break stopped. 🍅 Pomodoro resumed. Remaining: {m}m {s}s"})
                # else stop whole session
                sessions.pop(user, None)
            return jsonify({"reply": "⏹️ Pomodoro ended."})

        if msg == "status":
            with sessions_lock:
                if user not in sessions:
                    return jsonify({"reply": "❌ No active session. Use: start"})
                session = sessions[user]

                # if break ongoing
                if "break" in session:
                    elapsed = time.time() - session["break"]["start"]
                    remaining = session["break"]["duration"] - elapsed
                    if remaining <= 0:
                        # break finished, resume
                        session.pop("break", None)
                        session["paused"] = False
                        session["start"] = time.time()
                        return jsonify({"reply": "🟢 Break finished. Pomodoro resumed!"})
                    mins = int(remaining // 60)
                    secs = int(remaining % 60)
                    return jsonify({"reply": f"☕ Break left: {mins}m {secs}s"})

                # work session
                if not session.get("paused", False):
                    elapsed = time.time() - session["start"]
                    remaining = session["remaining"] - elapsed
                else:
                    remaining = session["remaining"]

                if remaining <= 0:
                    sessions.pop(user, None)
                    return jsonify({"reply": "✨ Pomodoro completed!"})

                mins = int(remaining // 60)
                secs = int(remaining % 60)
                return jsonify({"reply": f"🍅 Pomodoro left: {mins}m {secs}s"})

        # fallback
        return jsonify({"reply": reply})
    except Exception as e:
        print("Exception in /pomodoro:", e)
        return jsonify({"reply": "Internal server error"}), 500

# -------------------------
# Optional health check endpoint
# -------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    # If you run directly (python app.py) the watcher will start before_first_request as well.
    print("Starting Flask (dev) server...")
    app.run(host="0.0.0.0", port=5000)
