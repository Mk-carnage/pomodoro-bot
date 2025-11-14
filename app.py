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

    # Start
    if msg == "start":
        sessions[user] = {
            "start": time.time(),
            "duration": POMODORO_MINUTES * 60
        }
        return jsonify({
            "reply": f"🍅 Pomodoro started for {POMODORO_MINUTES} minutes!"
        })

    # Stop
    elif msg == "stop":
        sessions.pop(user, None)
        return jsonify({
            "reply": "⏹️ Pomodoro stopped."
        })

    # Status
    elif msg == "status":
        if user not in sessions:
            return jsonify({
                "reply": "❌ No active pomodoro session."
            })

        start = sessions[user]["start"]
        duration = sessions[user]["duration"]
        remaining = duration - (time.time() - start)

        if remaining <= 0:
            return jsonify({"reply": "✨ Pomodoro session completed!"})

        mins = int(remaining // 60)
        secs = int(remaining % 60)

        return jsonify({"reply": f"⏳ Time left: {mins}m {secs}s"})

    return jsonify({
        "reply": "🙂 Send: start | stop | status"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
