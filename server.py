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
PLACES_FILE = os.path.join(BASE_DIR, "places_output.json")
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")
FACTORY_SCRIPT = os.path.join(BASE_DIR, "factory_worker.py")
PIPELINE_LOG = os.path.join(BASE_DIR, "pipeline.log")

ADMIN_STATE_FILE = os.path.join(BASE_DIR, "admin_state.json")
FACTORY_STATE_FILE = os.path.join(BASE_DIR, "factory_state.json")
MEMBERS_DB = os.path.join(BASE_DIR, "members.sqlite3")
# Track factory run state
_factory_state = {
    "running": False,
    "last_run": None,
    "last_result": None,
}
# The server is threaded now, so "is a run already going?" is a check-and-set
# two requests can otherwise both win -- that starts two factory processes on
# one output file.
_factory_start_lock = threading.Lock()


def _read_store(path, default):
    """Read a JSON store. Torn content raises rather than reporting an empty
    catalogue, which is a lie the admin page would act on."""
    return factory_worker.load_json_store(path, default)

# ─────────────────────────────────────────────
# PUBLIC ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "web", "index.html"))


@app.route("/api/events")
def api_events():
    events = _read_store(EVENTS_FILE, [])
    events.sort(key=lambda e: e.get("start_date", ""))
    return jsonify(events)


@app.route("/api/holidays")
def api_holidays():
    """Curated, evergreen family day-out ideas kept separate from dated events."""
    return jsonify(_read_store(HOLIDAYS_FILE, []))


@app.route("/api/places")
def api_places():
    """Year-round local activities/venues (soft play, farms, museums) --
    evergreen like Holidays, but its own section: smaller, closer-to-home
    things to do, not curated bigger day-trip destinations."""
    return jsonify(_read_store(PLACES_FILE, []))

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "events_count": len(_read_store(EVENTS_FILE, []))})


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
    state = _read_store(FACTORY_STATE_FILE, {})
    if state.get("last_run"):
        result["last_run"] = state["last_run"]
        result["last_run_result"] = state.get("last_result", "")

    return jsonify(result)

def _load_json_file(path):
    return _read_store(path, [])


@app.route("/admin/api/metrics")
def admin_metrics():
    """Overall + today + this-hour progress across events, places, holidays
    and the social review queue -- for the landing page's metrics square."""
    now = datetime.now(timezone.utc)
    today_cutoff = now.date().isoformat() + "T00:00:00"
    hour_cutoff = now.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    staged = staging.load_staged()

    def reviewed_since(status, cutoff):
        return sum(
            1 for c in staged
            if c.get("status") == status and (c.get("reviewed_at") or "") >= cutoff
        )

    return jsonify({
        "totals": {
            "events": len(_load_json_file(EVENTS_FILE)),
            "places": len(_load_json_file(PLACES_FILE)),
            "holidays": len(_load_json_file(HOLIDAYS_FILE)),
            "needs_review": sum(1 for c in staged if c.get("status") == "needs_review"),
        },
        "today": {
            "approved": reviewed_since("approved", today_cutoff),
            "rejected": reviewed_since("rejected", today_cutoff),
        },
        "hour": {
            "approved": reviewed_since("approved", hour_cutoff),
        },
        "generated_at": now.isoformat(timespec="seconds"),
    })


@app.route("/admin/api/stats")
def admin_stats():
    """Event statistics: counts, sources, recent events."""
    events = _read_store(EVENTS_FILE, [])
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
    data = _read_store(SOURCES_FILE, {})
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
    return jsonify({"sources": sources})

@app.route("/admin/api/pipeline")
def admin_pipeline():
    """Pipeline status and run history."""
    state = _read_store(ADMIN_STATE_FILE, {})
    # Fall back to factory_state.json for last_run if admin state is empty
    if not state.get("run_history"):
        fs = _read_store(FACTORY_STATE_FILE, {})
        if fs.get("last_run") and not state.get("last_run"):
            state["last_run"] = fs["last_run"]

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
    with _factory_start_lock:
        if _factory_state["running"]:
            return jsonify({"status": "already_running", "message": "Factory is already running"})
        _factory_state["running"] = True

    def run_factory():
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
            
            events_count = len(_read_store(EVENTS_FILE, []))
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
    state = _read_store(ADMIN_STATE_FILE, {})
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
    events = _read_store(EVENTS_FILE, [])
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
    """Mark one staged candidate (locked, fresh-read). Never deleted — audit trail."""
    def _mutate(candidate):
        candidate["status"] = status
        candidate["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if note:
            candidate["review_note"] = note
        return dict(candidate)
    return staging.mark_candidate(source_url, _mutate)


@app.route("/admin/api/social/approve", methods=["POST"])
def admin_social_approve():
    """Classify one staged candidate as a dated event or an evergreen place
    and publish it accordingly. An optional `hint` (a curator's note typed on
    the admin page — a date, a venue name) is folded into the classification;
    plain re-clicking Approve with no hint just retries the same gate."""
    body = request.get_json(silent=True) or {}
    source_url = body.get("source_url", "")
    hint = (body.get("hint") or "").strip()

    candidates = staging.load_staged()
    candidate = next((c for c in candidates if c.get("source_url") == source_url), None)
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404

    published = staging.try_approve_by_url(source_url, hint=hint)
    if not published:
        updated = next((c for c in staging.load_staged() if c.get("source_url") == source_url), None)
        reason = (updated or {}).get("reason", "No usable date or identifiable place could be read from this post.")
        return jsonify({"error": "still needs manual info", "message": reason}), 422

    return jsonify({"status": "approved", "published": True})


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
    state = _read_store(ADMIN_STATE_FILE, {})
    history = state.get("run_history", [])
    src_data = _read_store(SOURCES_FILE, {})
    if isinstance(src_data, dict):
        src_count = sum(len(v) if isinstance(v, dict) else 1 for v in src_data.values())
    else:
        src_count = len(src_data)
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
    
    factory_worker.write_json_atomic(ADMIN_STATE_FILE, state)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8128, debug=False, threaded=True)
