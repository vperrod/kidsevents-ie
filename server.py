"""
Simple Flask server: serves the web frontend and events JSON API.
Run: python3 server.py (in venv)
"""
from flask import Flask, jsonify, send_from_directory, send_file
import json
import os

app = Flask(__name__, 
            static_folder="web", 
            static_url_path="",
            template_folder="web")

EVENTS_FILE = os.path.join(os.path.dirname(__file__), "events_output.json")


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "web", "index.html"))


@app.route("/api/events")
def api_events():
    if not os.path.exists(EVENTS_FILE):
        return jsonify([])
    with open(EVENTS_FILE, "r") as f:
        events = json.load(f)
    # Sort by date
    events.sort(key=lambda e: e.get("start_date", ""))
    return jsonify(events)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "events_count": len(json.load(open(EVENTS_FILE))) if os.path.exists(EVENTS_FILE) else 0})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8128, debug=False)
