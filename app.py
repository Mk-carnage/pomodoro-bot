from flask import Flask, request, jsonify
import time

app = Flask(__name__)

sessions = {}

POMODORO_MINUTES = 25
BREAK_MINUTES = 5


@app.route("/pomodoro", methods=["POST"])
def pomodoro():
    data = request.json
    msg = data.get("message", "").lower()
    user = str(data.get("user"))

    # START WORK SESSION
    if msg == "start":
        sessions[user] = {
            "type": "work",
            "start": time.time(),
            "duration": POMODORO_MINUTES * 60,
            "remaining": POMODORO_MINUTES * 60,
            "paused": False
        }
        return jsonify({"reply": f"🍅 Pomodoro started for {POMODORO_MINUTES} minutes!"})

    # START BREAK WHILE WORK IS RUNNING
    if msg == "break":
        if user not in sessions or sessions[user]["type"] != "work":
            return jsonify({"reply": "❌ No active Pomodoro to pause. Start with: start"})

        # PAUSE WORK SESSION
        elapsed = time.time() - sessions[user]["start"]
        sessions[user]["remaining"] -= elapsed
        sessions[user]["paused"] = True

        # START BREAK SESSION
        sessions[user]["break"] = {
            "start": time.time(),
            "duration": BREAK_MINUTES * 60
        }

        return jsonify({"reply": f"☕ Break started for {BREAK_MINUTES} minutes!"})

    # STOP SESSION
    if msg == "stop":

        # If currently on break → end break and resume work
        if user in sessions and "break" in sessions[user]:
            break_data = sessions[user].pop("break")
            remaining = int(sessions[user]["remaining"] // 60)
            secs = int(sessions[user]["remaining"] % 60)
            return jsonify({
                "reply": f"☕ Break stopped. 🍅 Pomodoro resumed. Remaining: {remaining}m {secs}s"
            })

        # If stopping full pomodoro
        sessions.pop(user, None)
        return jsonify({"reply": "⏹️ Pomodoro ended."})

    # STATUS CHECK
    if msg == "status":

        if user not in sessions:
            return jsonify({"reply": "❌ No active session. Use: start"})

        session = sessions[user]

        # If break ongoing
        if "break" in session:
            elapsed = time.time() - session["break"]["start"]
            remaining = session["break"]["duration"] - elapsed

            if remaining <= 0:
                # End break, resume work
                session.pop("break")
                session["paused"] = False
                session["start"] = time.time()
                return jsonify({"reply": "🟢 Break finished. Pomodoro resumed!"})

            mins = int(remaining // 60)
            secs = int(remaining % 60)
            return jsonify({"reply": f"☕ Break left: {mins}m {secs}s"})

        # Work session (active or resumed)
        if not session["paused"]:
            elapsed = time.time() - session["start"]
            remaining = session["remaining"] - elapsed
        else:
            remaining = session["remaining"]

        if remaining <= 0:
            sessions.pop(user, None)
            return jsonify({"reply": "✨ Pomodoro session completed!"})

        mins = int(remaining // 60)
        secs = int(remaining % 60)
        return jsonify({"reply": f"🍅 Pomodoro left: {mins}m {secs}s"})

    # Unknown command
    return jsonify({"reply": "🙂 Commands:\nstart | break | stop | status"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
