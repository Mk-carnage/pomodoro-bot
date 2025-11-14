from flask import Flask, request, jsonify
import time

app = Flask(__name__)

# Store sessions per user
sessions = {}

POMODORO_MINUTES = 25
BREAK_MINUTES = 5


@app.route("/pomodoro", methods=["POST"])
def pomodoro():
    data = request.json
    msg = data.get("message", "").lower()
    user = str(data.get("user"))

    # START POMODORO
    if msg == "start":
        sessions[user] = {
            "type": "work",
            "start": time.time(),
            "duration": POMODORO_MINUTES * 60
        }
        return jsonify({
            "reply": f"🍅 Pomodoro started for {POMODORO_MINUTES} minutes!"
        })

    # START BREAK
    elif msg == "break":
        sessions[user] = {
            "type": "break",
            "start": time.time(),
            "duration": BREAK_MINUTES * 60
        }
        return jsonify({
            "reply": f"☕ Break started for {BREAK_MINUTES} minutes!"
        })

    # STOP SESSION
    elif msg == "stop":
        sessions.pop(user, None)
        return jsonify({
            "reply": "⏹️ Session stopped."
        })

    # STATUS CHECK
    elif msg == "status":
        if user not in sessions:
            return jsonify({
                "reply": "❌ No active session. Type 'start' or 'break'."
            })

        session = sessions[user]
        start = session["start"]
        duration = session["duration"]
        remaining = duration - (time.time() - start)

        if remaining <= 0:
            session_type = session["type"]

            if session_type == "work":
                return jsonify({
                    "reply": "✨ Pomodoro session completed! Type 'break' to start a 5 min break."
                })
            else:
                return jsonify({
                    "reply": "🟢 Break finished! Type 'start' to begin next Pomodoro."
                })

        mins = int(remaining // 60)
        secs = int(remaining % 60)

        emoji = "🍅" if session["type"] == "work" else "☕"
        label = "Work Time" if session["type"] == "work" else "Break Time"

        return jsonify({
            "reply": f"{emoji} {label} left: {mins}m {secs}s"
        })

    # UNKNOWN COMMAND
    return jsonify({
        "reply": "🙂 Commands: start | break | status | stop"
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
