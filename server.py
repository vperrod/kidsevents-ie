"""
Flask server: serves the web frontend, events JSON API, and admin dashboard.
Run: python3 server.py (in venv)
"""
from flask import Flask, jsonify, send_from_directory, send_file, request
from firebase_auth import AuthenticationError, public_config, verified_member
from member_store import MemberStore
import json
import os
import subprocess
import time
import threading
from datetime import datetime, timezone

import factory_worker
import staging

app = Flask(__name__, 
            static_folder="web",
            static_url_path="",
            template_folder="web")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(BASE_DIR, "events_output.json")
HOLIDAYS_FILE = os.path.join(BASE_DIR, "holidays_output.json")
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")
FACTORY_SCRIPT = os.path.join(BASE_DIR, "factory_worker.py")
PIPELINE_LOG = os.path.join(BASE_DIR, "pipeline.log")

ADMIN_STATE_FILE = os.path.join(BASE_DIR, "admin_state.json")
MEMBERS_DB = os.path.join(BASE_DIR, "members.sqlite3")
# Track factory run state
_factory_state = {
    "running": False,
    "last_run": None,
    "last_result": None,
}

# ─────────────────────────────────────────────
# PUBLIC ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "web", "index.html"))


@app.route("/api/events")
def api_events():
    if not os.path.exists(EVENTS_FILE):
        return jsonify([])
    with open(EVENTS_FILE, "r") as f:
        events = json.load(f)
    events.sort(key=lambda e: e.get("start_date", ""))
    return jsonify(events)


@app.route("/api/holidays")
def api_holidays():
    """Curated, evergreen family day-out ideas kept separate from dated events."""
    if not os.path.exists(HOLIDAYS_FILE):
        return jsonify([])
    with open(HOLIDAYS_FILE, "r") as f:
        return jsonify(json.load(f))

@app.route("/api/health")
def health():
    count = 0
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, "r") as f:
            count = len(json.load(f))
    return jsonify({"status": "ok", "events_count": count})


def _member_from_request():
    """Return a server-verified member, never a browser-supplied identity."""
    if public_config() is None:
        return None, (jsonify({"error": "Authentication is not configured"}), 503)
    try:
        return verified_member(request.headers.get("Authorization")), None
    except AuthenticationError as error:
        return None, (jsonify({"error": str(error)}), 401)


def _member_store(member):
    store = MemberStore(MEMBERS_DB)
    store.initialise()
    store.upsert_member(member["provider_subject"], member["email"])
    return store


def _event_key_from_request():
    payload = request.get_json(silent=True) or {}
    event_key = payload.get("event_key")
    if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 2048:
        return None
    return event_key.strip()


@app.route("/api/auth/config")
def auth_config():
    """Expose only Firebase's intentionally public browser configuration."""
    config = public_config()
    return jsonify({"configured": config is not None, "firebase": config})


@app.route("/api/member/saves", methods=["GET", "POST", "DELETE"])
def member_saves():
    member, error_response = _member_from_request()
    if error_response:
        return error_response
    store = _member_store(member)
    subject = member["provider_subject"]

    if request.method == "GET":
        return jsonify({"event_keys": store.saved_event_keys(subject), "email": member["email"]})

    event_key = _event_key_from_request()
    if event_key is None:
        return jsonify({"error": "A valid event_key is required"}), 400
    if request.method == "POST":
        store.save_event(subject, event_key)
    else:
        store.remove_saved_event(subject, event_key)
    return jsonify({"event_keys": store.saved_event_keys(subject)})

# ─────────────────────────────────────────────
# ADMIN ROUTES — Dashboard + APIs
# ─────────────────────────────────────────────

@app.route("/admin", strict_slashes=False)
def admin_dashboard():
    return send_file(os.path.join(BASE_DIR, "web", "admin.html"))

@app.route("/admin/api/status")
def admin_status():
    """System status: factory service, timer, last run."""
    result = {
        "factory_running": _factory_state["running"],
        "factory_timer": "unknown",
        "last_run": _factory_state["last_run"],
        "last_run_result": _factory_state["last_result"],
    }
    
    # Check systemd service status
    try:
        svc = subprocess.run(
            ["systemctl", "--user", "is-active", "kidsevents-factory.service"],
            capture_output=True, text=True, timeout=5
        )
        result["factory_service_active"] = svc.stdout.strip() == "active"
    except Exception:
        result["factory_service_active"] = False
    
    # Check timer status
    try:
        tmr = subprocess.run(
            ["systemctl", "--user", "is-active", "kidsevents-factory.timer"],
            capture_output=True, text=True, timeout=5
        )
        result["factory_timer"] = tmr.stdout.strip() if tmr.returncode == 0 else "inactive"
    except Exception:
        result["factory_timer"] = "unknown"
    
    # Check last factory state file
    state_file = os.path.join(BASE_DIR, "factory_state.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            if state.get("last_run"):
                result["last_run"] = state["last_run"]
                result["last_run_result"] = state.get("last_result", "")
        except Exception:
            pass
    
    return jsonify(result)

@app.route("/admin/api/stats")
def admin_stats():
    """Event statistics: counts, sources, recent events."""
    events = []
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r") as f:
                events = json.load(f)
        except Exception:
            pass
    
    events.sort(key=lambda e: e.get("start_date", ""))
    
    # Count by source
    sources_count = {}
    for e in events:
        src = e.get("source", e.get("source_type", "unknown"))
        # Normalize: use source name before colon
        src_name = src.split(":")[0] if src and ":" in src else (src or "unknown")
        sources_count[src_name] = sources_count.get(src_name, 0) + 1
    
    # Recent events (last 30 days or newest 20)
    recent = events[-20:] if len(events) > 20 else events
    
    # File stats
    events_size = 0
    if os.path.exists(EVENTS_FILE):
        events_size = os.path.getsize(EVENTS_FILE)
    
    log_exists = os.path.exists(PIPELINE_LOG)
    log_size = 0
    if log_exists:
        log_size = os.path.getsize(PIPELINE_LOG)
    
    return jsonify({
        "total_events": len(events),
        "active_sources": len(sources_count),
        "sources_by_type": sources_count,
        "recent_events": recent,
        "all_events": events,
        "events_file_exists": os.path.exists(EVENTS_FILE),
        "events_file_size": events_size,
        "pipeline_log_exists": log_exists,
        "pipeline_log_size": log_size,
    })

@app.route("/admin/api/sources")
def admin_sources():
    """List data sources from sources.json (dict keyed by city)."""
    sources = []
    if os.path.exists(SOURCES_FILE):
        try:
            with open(SOURCES_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                sources = data
            elif isinstance(data, dict):
                # Dict keyed by city -> {source_name: url} OR {source_name: [urls]}
                for city, entries in data.items():
                    if isinstance(entries, dict):
                        for src_name, url in entries.items():
                            if isinstance(url, list):
                                for u in url:
                                    sources.append({"city": city, "name": src_name, "type": src_name, "url": u})
                            else:
                                sources.append({"city": city, "name": src_name, "type": src_name, "url": url})
                    elif isinstance(entries, list):
                        for u in entries:
                            if isinstance(u, dict):
                                u["city"] = city
                                sources.append(u)
                            elif isinstance(u, str):
                                sources.append({"city": city, "url": u})
                    elif isinstance(entries, str):
                        sources.append({"city": city, "url": entries})
        except Exception:
            pass
    return jsonify({"sources": sources})

@app.route("/admin/api/pipeline")
def admin_pipeline():
    """Pipeline status and run history."""
    state = {}
    if os.path.exists(ADMIN_STATE_FILE):
        try:
            with open(ADMIN_STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception:
            pass
    # Fall back to factory_state.json for last_run if admin state is empty
    if not state.get("run_history"):
        factory_state_file = os.path.join(BASE_DIR, "factory_state.json")
        if os.path.exists(factory_state_file):
            try:
                with open(factory_state_file, "r") as f:
                    fs = json.load(f)
                if fs.get("last_run") and not state.get("last_run"):
                    state["last_run"] = fs["last_run"]
            except Exception:
                pass
    
    return jsonify({
        "url_discovery": state.get("url_discovery_status", "idle"),
        "crawling": state.get("crawling_status", "idle"),
        "extraction": state.get("extraction_status", "idle"),
        "deduplication": state.get("dedup_status", "idle"),
        "output": state.get("output_status", "idle"),
        "history": state.get("run_history", []),
        "last_run": state.get("last_run"),
    })

@app.route("/admin/api/pipeline/run", methods=["POST"])
def admin_run_factory():
    """Trigger a factory run in background."""
    if _factory_state["running"]:
        return jsonify({"status": "already_running", "message": "Factory is already running"})
    
    def run_factory():
        _factory_state["running"] = True
        _factory_state["last_run"] = datetime.now().isoformat()
        start_time = time.time()
        
        try:
            # Run factory_worker.py
            result = subprocess.run(
                [os.path.join(BASE_DIR, "venv", "bin", "python3"), FACTORY_SCRIPT],
                capture_output=True, text=True, timeout=600,  # 10 min max
                cwd=BASE_DIR,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
            
            duration = time.time() - start_time
            
            # Count events after run
            events_count = 0
            if os.path.exists(EVENTS_FILE):
                try:
                    with open(EVENTS_FILE, "r") as f:
                        events_count = len(json.load(f))
                except Exception:
                    pass
            
            success = result.returncode == 0
            _factory_state["last_result"] = f"{'OK' if success else 'FAIL'} — {events_count} events in {duration:.0f}s"
            
            # Update state file
            _update_factory_state(success, events_count, duration)
            
        except subprocess.TimeoutExpired:
            _factory_state["last_result"] = "TIMEOUT after 600s"
            _update_factory_state(False, 0, 600)
        except Exception as e:
            _factory_state["last_result"] = f"ERROR: {str(e)}"
            _update_factory_state(False, 0, 0)
        finally:
            _factory_state["running"] = False
    
    thread = threading.Thread(target=run_factory, daemon=True)
    thread.start()
    
    return jsonify({"status": "started", "message": "Factory run started"})

@app.route("/admin/api/pipeline/run", methods=["GET"])
def admin_factory_status():
    """Check current factory run status."""
    state = {}
    if os.path.exists(ADMIN_STATE_FILE):
        try:
            with open(ADMIN_STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception:
            pass
    
    last = state.get("run_history", [{}])[-1] if state.get("run_history") else {}
    
    return jsonify({
        "status": "running" if _factory_state["running"] else ("completed" if last else "idle"),
        "events_count": last.get("events_count", 0),
        "duration": last.get("duration", ""),
        "success": last.get("success", False),
    })

@app.route("/admin/api/logs")
def admin_logs():
    """Return last 200 lines of pipeline.log."""
    lines = []
    if os.path.exists(PIPELINE_LOG):
        try:
            with open(PIPELINE_LOG, "r") as f:
                all_lines = f.readlines()
            lines = [l.rstrip() for l in all_lines[-200:]]
        except Exception:
            lines = ["Error reading log file"]
    else:
        lines = ["No log file found — run the factory to generate logs"]
    return jsonify({"lines": lines})

@app.route("/admin/api/events")
def admin_events():
    """All events for admin table."""
    events = []
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r") as f:
                events = json.load(f)
        except Exception:
            pass
    events.sort(key=lambda e: e.get("start_date", ""))
    return jsonify({"events": events})

@app.route("/admin/api/social/staged")
def admin_social_staged():
    """Social candidates awaiting a review decision, plus per-source counts."""
    pending = [c for c in staging.load_staged() if c.get("status") == "needs_review"]
    found_via = {}
    for candidate in pending:
        key = candidate.get("found_via") or "Unattributed"
        found_via[key] = found_via.get(key, 0) + 1
    return jsonify({
        "candidates": pending,
        "total": len(pending),
        "found_via": found_via,
    })


def _set_candidate_status(source_url, status, note=None):
    """Mark one staged candidate. Candidates are never deleted — audit trail."""
    candidates = staging.load_staged()
    for candidate in candidates:
        if candidate.get("source_url") == source_url:
            candidate["status"] = status
            candidate["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if note:
                candidate["review_note"] = note
            staging.save_staged(candidates)
            return candidate
    return None


@app.route("/admin/api/social/approve", methods=["POST"])
def admin_social_approve():
    """Enrich one staged candidate and publish it if it yields a usable date."""
    source_url = (request.get_json(silent=True) or {}).get("source_url", "")
    candidate = next(
        (c for c in staging.load_staged() if c.get("source_url") == source_url), None
    )
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404

    event = factory_worker.promote_candidate(candidate)
    if not event:
        return jsonify({
            "error": "still needs manual info",
            "message": "No usable date could be read from this post — it stays staged.",
        }), 422

    events = []
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r") as f:
                events = json.load(f)
        except Exception:
            events = []
    key = factory_worker.event_key(event)
    added = not any(factory_worker.event_key(e) == key for e in events)
    if added:
        events.append(event)
        events.sort(key=lambda e: (e.get("start_date", ""), -len(e.get("all_sources", []))))
        with open(EVENTS_FILE, "w") as f:
            json.dump(events, f, indent=2, ensure_ascii=False)

    _set_candidate_status(source_url, "approved")
    return jsonify({"status": "approved", "published": added, "event": event})


@app.route("/admin/api/social/reject", methods=["POST"])
def admin_social_reject():
    body = request.get_json(silent=True) or {}
    candidate = _set_candidate_status(
        body.get("source_url", ""), "rejected", body.get("note", "")
    )
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404
    return jsonify({"status": "rejected"})


def _update_factory_state(success, events_count, duration):
    """Update admin_state.json with run results."""
    state = {}
    if os.path.exists(ADMIN_STATE_FILE):
        try:
            with open(ADMIN_STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception:
            pass
    
    history = state.get("run_history", [])
    src_count = 0
    if os.path.exists(SOURCES_FILE):
        try:
            with open(SOURCES_FILE, "r") as f:
                src_data = json.load(f)
            if isinstance(src_data, dict):
                src_count = sum(len(v) if isinstance(v, dict) else 1 for v in src_data.values())
            elif isinstance(src_data, list):
                src_count = len(src_data)
        except Exception:
            pass
    history.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "success": success,
        "events_count": events_count,
        "duration": f"{duration:.0f}s" if duration else "—",
        "sources_count": src_count,
    })
    
    # Keep last 50 runs
    if len(history) > 50:
        history = history[-50:]
    
    state["run_history"] = history
    state["last_run"] = datetime.now().isoformat()
    state["last_result"] = _factory_state["last_result"]
    
    with open(ADMIN_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8128, debug=False)
