from flask import Flask, request, jsonify
import threading
import time
import requests

app = Flask(__name__)

# ================================
# SETTINGS
# ================================
POMODORO_MINUTES = 25
BREAK_MINUTES = 5

# Your Zoho bot incoming webhook URL (PUT YOUR URL HERE)
ZOHO_INCOMING_URL = "https://cliq.zoho.in/api/v2/bots/pomodorobot/incoming"


# Store user sessions
sessions = {}

# ================================
# BACKGROUND CHECKER FOR AUTO NOTIFS
# ================================
def timer_watcher():
    while True:
        now = time.time()

        for user, session in list(sessions.items()):
            # Break session check
            if "break" in session:
                elapsed = now - session["break"]["start"]
                if elapsed >= session["break"]["duration"]:
                    sessions[user].pop("break")

                    message = "🟢 **Break finished!**\nType **start** to begin next Pomodoro."
                    requests.post(ZOHO_INCOMING_URL, json={"text": message})

                    # resume work
                    session["paused"] = False
                    session["start"] = now

            else:
                # Work session check
                elapsed = now - session["start"]
                if elapsed >= session["remaining"]:
                    sessions.pop(user, None)

                    message = "⏰ **Pomodoro completed!**\nType **break** to start your 5 min break."
                    requests.post(ZOHO_INCOMING_URL, json={"text": message})

        time.sleep(1)  # check every second


# Run background thread
thread = threading.Thread(target=timer_watcher, daemon=True)
thread.start()

# ================================
# MAIN API ENDPOINT
# ================================
@app.route("/pomodoro", methods=["POST"])
def pomodoro():
    data = request.json
    msg = data.get("message", "").lower()
    user = str(data.get("user"))

    # START WORK SESSION
       # START WORK SESSION (custom or default)
    if msg.startswith("start"):

        parts = msg.split()

        # custom minutes
        if len(parts) == 2 and parts[1].isdigit():
            custom_minutes = int(parts[1])
            duration_seconds = custom_minutes * 60
            duration_text = f"{custom_minutes} minutes"
        else:
            # default 25 min
            custom_minutes = 25
            duration_seconds = POMODORO_MINUTES * 60
            duration_text = "25 minutes"

        sessions[user] = {
            "type": "work",
            "start": time.time(),
            "duration": duration_seconds,
            "remaining": duration_seconds,
            "paused": False
        }

        return jsonify({"reply": f"🍅 Pomodoro started for {duration_text}!"})

    # START BREAK (pause Pomodoro)
    if msg == "break":
        if user not in sessions or sessions[user]["type"] != "work":
            return jsonify({"reply": "❌ No Pomodoro to pause. Type 'start' first."})

        elapsed = time.time() - sessions[user]["start"]
        sessions[user]["remaining"] -= elapsed
        sessions[user]["paused"] = True

        sessions[user]["break"] = {
            "start": time.time(),
            "duration": BREAK_MINUTES * 60
        }

        return jsonify({"reply": "☕ Break started for 5 minutes!"})

    # STOP SESSION
    if msg == "stop":
        if user in sessions and "break" in sessions[user]:
            sessions[user].pop("break")
            mins = int(sessions[user]["remaining"] // 60)
            secs = int(sessions[user]["remaining"] % 60)
            return jsonify({"reply": f"☕ Break stopped. 🍅 Pomodoro resumed with {mins}m {secs}s left."})

        sessions.pop(user, None)
        return jsonify({"reply": "⏹️ Pomodoro ended."})

    # STATUS
    if msg == "status":
        if user not in sessions:
            return jsonify({"reply": "❌ No session active."})

        session = sessions[user]

        # If break active
        if "break" in session:
            elapsed = time.time() - session["break"]["start"]
            remaining = session["break"]["duration"] - elapsed
            if remaining <= 0:
                return jsonify({"reply": "🟢 Break finished. Pomodoro resumed!"})

            return jsonify({"reply": f"☕ Break left: {int(remaining//60)}m {int(remaining%60)}s"})

        # Work active
        if not session["paused"]:
            elapsed = time.time() - session["start"]
            remaining = session["remaining"] - elapsed
        else:
            remaining = session["remaining"]

        if remaining <= 0:
            return jsonify({"reply": "✨ Pomodoro completed!"})

        return jsonify({"reply": f"🍅 Pomodoro left: {int(remaining//60)}m {int(remaining%60)}s"})

    return jsonify({"reply": "🙂 Commands: start | break | stop | status"})


# ================================
# MAIN
# ================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
