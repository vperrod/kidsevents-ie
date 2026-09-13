#!/usr/bin/env python3
"""Free-first LLM routing for the Small Days factory.

Every model call in this project goes through `complete()`. It tries, in
order, the cheapest lane that can actually answer right now:

1. **local** — the mini PC's `llama-server` (Qwen3.5-35B-A3B), reached over
   the SSH forward `kidsevents-llama-tunnel.service` puts on
   127.0.0.1:18089. Free, ~4 s, no rate limit. WanderTold's factory and photo
   gate share those slots, so we yield when `llamacpp:requests_processing`
   is already at `LOCAL_BUSY_AT`, and we never send it a prompt longer than
   `LOCAL_MAX_PROMPT_CHARS` (2 slots x 8192 ctx).
2. **named OmniRoute lanes** — `ROUTING_LANES`, round-robin. Free lanes rot
   constantly (404 "model does not exist", 402 "add credits", 429, "all
   credentials are cooling down"), so the roster is probed once per process
   and a lane that errors mid-run is parked for `LANE_PARK_SECS`. A lane
   error never fails the item; it moves to the next lane.
3. **auto/best-free** — OmniRoute's own pick-a-free-model lane. Slower and
   itself failable, but it needs no roster maintenance.
4. **hermes CLI** — the old sole lane. Kept last because it is the slowest
   (22-90 s) and throttles at ~1 req/min per model; on 2026-09-13 it timed
   out on every single call, which is what left the factory with no verdicts
   at all and why this module exists.
5. `""` — same "no answer" contract callers already handle.

The gateway at `OMNIROUTE_URL` takes no auth header (and its `/v1/models`
route answers "Invalid API key" — don't call it). No key or token is stored
anywhere in this module.

Every lane attempt appends one line to `routing.jsonl`:
`{ts, kind, lane, model, ms, ok, prompt_chars, err}`. `stats()` folds that
file into the `llm` block of `factory_state.json` the admin Production view
reads.
"""

import json
import os
import re
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROUTING_LOG = BASE / "routing.jsonl"

_ENV_FILE_CACHE = None


def _env(key, default):
    """os.environ first (so a one-off `KEY=... python3 staging.py` wins),
    then .env, then the default."""
    global _ENV_FILE_CACHE
    if os.environ.get(key):
        return os.environ[key]
    if _ENV_FILE_CACHE is None:
        _ENV_FILE_CACHE = {}
        env_file = BASE / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    _ENV_FILE_CACHE[k.strip()] = v.strip().strip('"')
    return _ENV_FILE_CACHE.get(key, default)


LOCAL_LLM_URL = _env("LOCAL_LLM_URL", "http://127.0.0.1:18089").rstrip("/")
LOCAL_MAX_PROMPT_CHARS = int(_env("LOCAL_MAX_PROMPT_CHARS", "12000"))
LOCAL_BUSY_AT = int(_env("LOCAL_BUSY_AT", "2"))
# Health + metrics get 1 s each, so a dead tunnel falls through to the
# gateway in under 2 s instead of stalling every item.
LOCAL_PROBE_TIMEOUT = 1

OMNIROUTE_URL = _env("OMNIROUTE_URL", "http://127.0.0.1:20128").rstrip("/")
AUTO_LANE = "auto/best-free"
DEFAULT_LANES = [
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "oc/mimo-v2.5-free",
    "kg/tencent/hy3:free",
    "kg/stepfun/step-3.7-flash:free",
    "kg/meituan/longcat-2.0-free",
    "openrouter/dots-studio/dots-3-note-preview:free",
    "openrouter/google/gemma-4-31b-it:free",
    "oc/nemotron-3-ultra-free",
]
ROUTING_LANES = [x.strip() for x in _env("ROUTING_LANES", ",".join(DEFAULT_LANES)).split(",") if x.strip()]
LANE_PARK_SECS = int(_env("LANE_PARK_SECS", "600"))
LANE_PROBE_TIMEOUT = int(_env("LANE_PROBE_TIMEOUT", "30"))
PROBE_TOKENS = int(_env("PROBE_TOKENS", "256"))
# A named lane can take a minute to time out; trying the whole roster on one
# item would blow the per-item budget, so each call tries at most this many
# before dropping to auto/best-free.
LANE_TRIES = int(_env("LANE_TRIES", "2"))
LANE_TIMEOUT = int(_env("LANE_TIMEOUT", "60"))

# Process environment only, never .env: the checked-out .env carries a stale
# `HERMES_MODEL=hermes(poolside/laguna-s-2.1:free)` (hermes CLI shorthand, not
# a model id) that the old hardcoded constants ignored — reading it would send
# every hermes-lane call to a model that does not exist. `nous`, hermes's own
# default provider, has no credentials on this VM (2026-09-12), which is why
# the default is OpenRouter's free tier.
HERMES_PROVIDER = os.environ.get("HERMES_PROVIDER") or "openrouter"
HERMES_MODEL = os.environ.get("HERMES_MODEL") or "google/gemma-4-31b-it:free"
# systemd user units get a bare PATH without ~/.local/bin: the 2026-09-12
# 15:03 timer run failed every LLM call with "No such file: 'hermes'".
HERMES_BIN = _env("HERMES_BIN", "") or str(Path.home() / ".local" / "bin" / "hermes")
HERMES_TIMEOUT = int(_env("HERMES_TIMEOUT", "90"))

_log_lock = threading.Lock()
_lane_lock = threading.Lock()
_probe_lock = threading.Lock()
_live_lanes = None       # set by _probe_lanes(), once per process
_parked = {}             # lane -> unix ts it may be used again
_next_lane = 0


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(kind, lane, model, ms, ok, prompt_chars, err=""):
    line = json.dumps({
        "ts": _now(), "kind": kind, "lane": lane, "model": model,
        "ms": int(ms), "ok": bool(ok), "prompt_chars": int(prompt_chars),
        "err": err[:300],
    }, ensure_ascii=False)
    with _log_lock:
        with open(ROUTING_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# HTTP (stdlib only — both endpoints are on loopback, nothing to negotiate)
# ---------------------------------------------------------------------------

def _get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def _chat(base_url, model, prompt, max_tokens, timeout):
    """One OpenAI-compatible completion. Raises on anything that is not a
    usable answer — including a 200 with an `error` body, which is how
    OmniRoute reports a rotted lane."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}: {error.read().decode('utf-8', 'ignore')[:200]}")
    data = json.loads(body)
    if data.get("error"):
        message = data["error"]
        raise RuntimeError(str(message.get("message", message))[:200] if isinstance(message, dict) else str(message)[:200])
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if not text.strip():
        raise RuntimeError("empty completion")
    return text


# ---------------------------------------------------------------------------
# Lane 1 — the mini PC's local model
# ---------------------------------------------------------------------------

def _local_available(prompt_chars):
    if prompt_chars > LOCAL_MAX_PROMPT_CHARS:
        return False
    try:
        if json.loads(_get(f"{LOCAL_LLM_URL}/health", LOCAL_PROBE_TIMEOUT)).get("status") != "ok":
            return False
        metrics = _get(f"{LOCAL_LLM_URL}/metrics", LOCAL_PROBE_TIMEOUT)
    except Exception:
        return False
    match = re.search(r"^llamacpp:requests_processing\s+([0-9.]+)", metrics, re.M)
    return bool(match) and float(match.group(1)) < LOCAL_BUSY_AT


# ---------------------------------------------------------------------------
# Lane 2 — named OmniRoute lanes
# ---------------------------------------------------------------------------

def _probe_lanes():
    """Keep the lanes that answer a short test. Probed concurrently once per
    process: sequentially this would cost minutes at the roster's current hit
    rate. The budget is PROBE_TOKENS, not a handful — several of these lanes
    are reasoning models that spend the whole allowance thinking and return
    an empty answer (`kg/stepfun/step-3.7-flash:free` returns "" at 5 and at
    32 tokens, "OK" at 256), which would drop a working lane."""
    global _live_lanes
    with _probe_lock:
        if _live_lanes is not None:
            return _live_lanes
        return _run_probe()


def _run_probe():
    global _live_lanes

    def ping(lane):
        started = time.time()
        try:
            _chat(OMNIROUTE_URL, lane, "Reply with exactly: OK", PROBE_TOKENS, LANE_PROBE_TIMEOUT)
            ok, err = True, ""
        except Exception as error:
            ok, err = False, str(error)
        _record("lane_probe", lane, lane, (time.time() - started) * 1000, ok, 21, err)
        return ok

    with ThreadPoolExecutor(max_workers=max(len(ROUTING_LANES), 1)) as pool:
        results = list(pool.map(ping, ROUTING_LANES))
    _live_lanes = [lane for lane, ok in zip(ROUTING_LANES, results) if ok]
    return _live_lanes


def _take_lane():
    """Next usable lane in round-robin order, or None if all are parked."""
    global _next_lane
    lanes = _probe_lanes()
    if not lanes:
        return None
    now = time.time()
    with _lane_lock:
        for _ in range(len(lanes)):
            lane = lanes[_next_lane % len(lanes)]
            _next_lane += 1
            if _parked.get(lane, 0) <= now:
                return lane
    return None


def _park(lane):
    with _lane_lock:
        _parked[lane] = time.time() + LANE_PARK_SECS


# ---------------------------------------------------------------------------
# Lane 4 — hermes CLI
# ---------------------------------------------------------------------------

def _hermes_cli(prompt, model, provider, timeout):
    if len(prompt) > 50_000:
        ds, de = "<data>", "</data>"
        if ds in prompt and de in prompt:
            i, j = prompt.index(ds), prompt.index(de) + len(de)
            keep = 40_000
            data = prompt[i:j]
            if len(data) > keep:
                data = "...[truncated]...\\n" + data[-keep:]
            prompt = prompt[:i] + data + prompt[j:]
    cmd = [HERMES_BIN, "-z", prompt, "--cli",
           "--provider", provider or HERMES_PROVIDER,
           "-m", model or HERMES_MODEL]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "NO_COLOR": "1"},
    )
    text = re.sub(r"\x1b$$[0-9;]*m", "", result.stdout)
    if not text.strip():
        raise RuntimeError(f"exit {result.returncode}: {result.stderr.strip()[:200]}")
    return text


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def complete(prompt, kind, max_tokens=1200, timeout=120, hermes_model=None, hermes_provider=None):
    """Answer `prompt` on the cheapest lane that can. `kind` is the pipeline
    step making the call (e.g. "enrich_event") and is only used for the
    routing log. Returns "" when no lane answered — callers already treat an
    empty answer as "no verdict"."""
    chars = len(prompt)

    def attempt(lane, model, call):
        started = time.time()
        try:
            text = call()
            _record(kind, lane, model, (time.time() - started) * 1000, True, chars)
            return text
        except Exception as error:
            _record(kind, lane, model, (time.time() - started) * 1000, False, chars, str(error))
            return None

    if _local_available(chars):
        text = attempt("local", "local/llama-server",
                       lambda: _chat(LOCAL_LLM_URL, "local", prompt, max_tokens, timeout))
        if text is not None:
            return text

    for _ in range(LANE_TRIES):
        lane = _take_lane()
        if lane is None:
            break
        text = attempt(lane, lane,
                       lambda: _chat(OMNIROUTE_URL, lane, prompt, max_tokens, min(timeout, LANE_TIMEOUT)))
        if text is not None:
            return text
        _park(lane)

    text = attempt("auto", AUTO_LANE,
                   lambda: _chat(OMNIROUTE_URL, AUTO_LANE, prompt, max_tokens, timeout))
    if text is not None:
        return text

    text = attempt("hermes", hermes_model or HERMES_MODEL,
                   lambda: _hermes_cli(prompt, hermes_model, hermes_provider, HERMES_TIMEOUT))
    if text is not None:
        return text
    return ""


# ---------------------------------------------------------------------------
# Stats for factory_state.json
# ---------------------------------------------------------------------------

def stats(path=None):
    """Fold today's `routing.jsonl` into the block the admin Production view
    reads. Lane probe pings are counted as `last_probe`, not as calls."""
    path = Path(path or ROUTING_LOG)
    today = datetime.now(timezone.utc).date().isoformat()
    by_lane, last_probe, calls = {}, None, 0
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") == "lane_probe":
                if last_probe is None or row.get("ts", "") > last_probe:
                    last_probe = row.get("ts")
                continue
            if not str(row.get("ts", "")).startswith(today):
                continue
            calls += 1
            lane = by_lane.setdefault(row.get("lane", "?"), {"calls": 0, "errors": 0, "_ms": []})
            lane["calls"] += 1
            if not row.get("ok"):
                lane["errors"] += 1
            lane["_ms"].append(row.get("ms", 0))
    for lane in by_lane.values():
        lane["p50_ms"] = int(statistics.median(lane.pop("_ms") or [0]))
    return {"calls_today": calls, "by_lane": by_lane, "last_probe": last_probe}
