"""
Flask server: serves the web frontend, events JSON API, and admin dashboard.
Run: python3 server.py (in venv)
"""
from flask import Flask, jsonify, send_from_directory, send_file, request
from firebase_auth import AuthenticationError, public_config, verified_member
from member_store import MemberStore
import json
import os
import re
import subprocess
import time
import threading
from datetime import datetime, timezone

import contract
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

CLAIMS_FILE = os.path.join(BASE_DIR, "staged", "claims.json")
NEEDS_INPUT_FILE = os.path.join(BASE_DIR, "staged", "needs_input.json")
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


def _on_air(path):
    """The records in a store that are actually published. A record with no
    `status` predates the contract and is on air by virtue of being in the
    file at all."""
    return [r for r in _read_store(path, []) if r.get("status", "on-air") == "on-air"]


def _legacy(path):
    """On-air records flattened onto the keys web/index.html reads, so the
    public API keeps its response shape while the stores hold the contract."""
    return [contract.legacy_view(r) for r in _on_air(path)]


@app.route("/api/events")
def api_events():
    events = _legacy(EVENTS_FILE)
    events.sort(key=lambda e: e.get("start_date", ""))
    return jsonify(events)


@app.route("/api/holidays")
def api_holidays():
    """Curated, evergreen family day-out ideas kept separate from dated events."""
    return jsonify(_legacy(HOLIDAYS_FILE))


@app.route("/api/places")
def api_places():
    """Year-round local activities/venues (soft play, farms, museums) --
    evergreen like Holidays, but its own section: smaller, closer-to-home
    things to do, not curated bigger day-trip destinations."""
    return jsonify(_legacy(PLACES_FILE))


@app.route("/api/v1/events")
def api_v1_events():
    """The full contract record — everything the flat legacy view drops
    (facts and their quotes, the facet taxonomy, media, provenance)."""
    events = _on_air(EVENTS_FILE)
    events.sort(key=lambda e: (e.get("event") or {}).get("start_date", ""))
    return jsonify(events)


@app.route("/api/v1/places")
def api_v1_places():
    return jsonify(_on_air(PLACES_FILE))


@app.route("/api/v1/holidays")
def api_v1_holidays():
    return jsonify(_on_air(HOLIDAYS_FILE))


def _faceted(path):
    """The legacy view plus the facets the public filters need.

    `/api/v1/*` carries the whole contract record, which is far more than a
    browser filtering a list has any use for (every fact and its quote, the
    full provenance). This is the same on-air set flattened for rendering with
    the taxonomy, county and coordinates the facet panel reads kept alongside.
    """
    feed = []
    for record in _on_air(path):
        taxonomy = record.get("taxonomy") or {}
        location = record.get("location") or {}
        view = contract.legacy_view(record)
        view["id"] = record.get("id", "")
        view["facets"] = {
            "age_bands": taxonomy.get("age_bands") or [],
            "price_band": taxonomy.get("price_band") or "",
            "setting": taxonomy.get("setting") or "",
            "activity_types": taxonomy.get("activity_types") or [],
            "accessibility": taxonomy.get("accessibility") or [],
            "county": location.get("county") or "",
            "region": location.get("region") or "",
            "lat": location.get("lat"),
            "lon": location.get("lon"),
        }
        feed.append(view)
    return feed


@app.route("/api/v2/events")
def api_v2_events():
    events = _faceted(EVENTS_FILE)
    events.sort(key=lambda e: e.get("start_date", ""))
    return jsonify(events)


@app.route("/api/v2/places")
def api_v2_places():
    return jsonify(_faceted(PLACES_FILE))


@app.route("/api/v2/holidays")
def api_v2_holidays():
    return jsonify(_faceted(HOLIDAYS_FILE))


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "events_count": len(_on_air(EVENTS_FILE))})


# ─────────────────────────────────────────────
# ORGANISER CLAIMS (public, unauthenticated)
# ─────────────────────────────────────────────

_RELATIONSHIPS = ("owner", "manager", "other")
# Deliberately loose: this rejects the typos and the obvious junk, and nothing
# short of sending mail can tell a real mailbox from a well-formed one.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_CLAIM_WINDOW_SECS = 3600
_claim_seen = {}
_claim_seen_lock = threading.Lock()


def _claim_throttled(ip, source_url):
    """One claim per listing per IP per hour. The form is public and has no
    login, so this blunts a script hammering one listing; it is not access
    control and it is in memory, so a restart forgives everyone."""
    now = time.time()
    with _claim_seen_lock:
        for stale in [k for k, seen in _claim_seen.items() if now - seen > _CLAIM_WINDOW_SECS]:
            del _claim_seen[stale]
        return (ip, source_url) in _claim_seen


def _claim_recorded(ip, source_url):
    with _claim_seen_lock:
        _claim_seen[(ip, source_url)] = time.time()


@app.route("/api/claim", methods=["POST"])
def api_claim():
    """An organiser telling us about their own listing. Stored only — phase 7
    builds the inbound half of §4.6; the outbound mail needs the domain and the
    mailboxes, which do not exist yet."""
    body = request.get_json(silent=True) or {}

    def field(name, limit):
        return str(body.get(name) or "").strip()[:limit]

    name = field("name", 120)
    email = field("email", 254)
    relationship = field("relationship", 20).lower()
    message = field("message", 4000)
    source_url = field("source_url", 2048)

    if not name:
        return jsonify({"error": "Please tell us your name."}), 400
    if not _EMAIL_RE.match(email):
        return jsonify({"error": "Please enter an email address we can reply to."}), 400
    if relationship not in _RELATIONSHIPS:
        return jsonify({"error": "Please tell us how you are connected to the venue."}), 400
    if not message:
        return jsonify({"error": "Please tell us what needs to change."}), 400

    ip = request.remote_addr or "unknown"
    if _claim_throttled(ip, source_url):
        return jsonify({
            "error": "We already have a message about this listing from you. "
                     "Give us a little time to read it."
        }), 429

    claim = {
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_id": field("record_id", 256),
        "record_title": field("record_title", 200),
        "source_url": source_url,
        "name": name,
        "email": email,
        "relationship": relationship,
        "message": message,
        "has_photos": bool(body.get("has_photos")),
        "status": "new",
    }
    os.makedirs(os.path.dirname(CLAIMS_FILE), exist_ok=True)
    with factory_worker.output_lock():
        claims = _read_store(CLAIMS_FILE, [])
        claims.append(claim)
        factory_worker.write_json_atomic(CLAIMS_FILE, claims)
    _claim_recorded(ip, source_url)

    return jsonify({
        "status": "received",
        "message": "Thank you — your message is with us. We will email you at "
                   f"{email} if we need anything else.",
    })


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
    events.sort(key=lambda e: contract.legacy_view(e).get("start_date") or "")

    # Count by source
    sources_count = {}
    for e in events:
        src = contract.legacy_view(e).get("source") or e.get("source_type", "unknown")
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
    """All events for admin table — full contract records, not the legacy view."""
    events = _read_store(EVENTS_FILE, [])
    events.sort(key=lambda e: contract.legacy_view(e).get("start_date") or "")
    return jsonify({"events": events})

@app.route("/admin/api/claims")
def admin_claims():
    """Organiser claim/update messages, newest first."""
    claims = _read_store(CLAIMS_FILE, [])
    claims.sort(key=lambda c: c.get("received_at", ""), reverse=True)
    return jsonify({"claims": claims, "total": len(claims)})


@app.route("/admin/api/needs_input")
def admin_needs_input():
    """Catalogue records the gate could not publish, with the one nameable
    field a curator's note would fix -- distinct from `/admin/api/social/
    staged`, which is raw social candidates awaiting their first classification.
    These already are events/places/holidays; they just need one correction."""
    records = [r for r in _read_store(NEEDS_INPUT_FILE, []) if r.get("status") == "needs-input"]
    by_missing_field = {}
    for record in records:
        key = record.get("missing_field") or "unspecified"
        by_missing_field[key] = by_missing_field.get(key, 0) + 1
    return jsonify({"records": records, "total": len(records), "by_missing_field": by_missing_field})


@app.route("/admin/api/needs_input/resubmit", methods=["POST"])
def admin_needs_input_resubmit():
    """Apply a curator's patch to one needs-input catalogue record and ask the
    gate again. Publishes it on air if that clears the block; otherwise the
    record stays here with an updated reason, same shape as the social
    approve route's "still needs manual info" response."""
    body = request.get_json(silent=True) or {}
    record_id = body.get("id", "")
    patch = body.get("patch") or {}
    if not record_id or not isinstance(patch, dict):
        return jsonify({"error": "id and patch are required"}), 400

    with factory_worker.output_lock():
        records = _read_store(NEEDS_INPUT_FILE, [])
        record = next((r for r in records if r.get("id") == record_id), None)
        if not record:
            return jsonify({"error": "record not found"}), 404
        ok, reason, missing_field = factory_worker.patch_and_regate(record, patch)
        if ok:
            published = factory_worker.publish_needs_input_record(record)
            remaining = [r for r in records if r.get("id") != record_id]
            factory_worker.write_json_atomic(NEEDS_INPUT_FILE, remaining)
            if not published:
                return jsonify({"error": "already on air"}), 409
            return jsonify({"status": "on-air", "id": record_id})
        record["status"] = "needs-input" if missing_field else "rejected"
        record["reason"] = reason
        record["missing_field"] = missing_field
        factory_worker.write_json_atomic(NEEDS_INPUT_FILE, records)
        return jsonify({"status": record["status"], "reason": reason,
                        "missing_field": missing_field}), 422


@app.route("/admin/api/needs_input/reject", methods=["POST"])
def admin_needs_input_reject():
    """Mark one needs-input catalogue record rejected -- never deleted, the
    same audit-trail rule the social candidates follow."""
    body = request.get_json(silent=True) or {}
    record_id = body.get("id", "")
    if not record_id:
        return jsonify({"error": "id is required"}), 400

    with factory_worker.output_lock():
        records = _read_store(NEEDS_INPUT_FILE, [])
        record = next((r for r in records if r.get("id") == record_id), None)
        if not record:
            return jsonify({"error": "record not found"}), 404
        record["status"] = "rejected"
        record["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        factory_worker.write_json_atomic(NEEDS_INPUT_FILE, records)
    return jsonify({"status": "rejected", "id": record_id})


@app.route("/admin/api/social/staged")
def admin_social_staged():
    """Social candidates awaiting a review decision, plus per-source counts.

    `needs_input` is in here too: the gate could not publish it, but it named
    the one field (`missing_field`) a curator's note would fix, so it is still
    a decision waiting for a human rather than a closed one.
    """
    pending = [c for c in staging.load_staged()
               if c.get("status") in ("needs_review", "needs_input")]
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
