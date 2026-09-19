#!/usr/bin/env python3
"""Parallel Multi-Model agents — different prompts to different models simultaneously.

This extends the Small Days factory's lane-based fallback into a parallel execution
model where each subtask uses its optimal model.

USAGE:
    from llm import select_model, task_prompts
    results = parallel_agents.submit(task_name, data, strategy="nvidia" | "omni")
    best = parallel_agents.synthesize(results)
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Callable

BASE = Path(__file__).resolve().parent
ROUTING_LOG = BASE / "routing_parallel.jsonl"

# Lock for thread-safe logging
_log_lock = threading.Lock()

# ============================================================================
# Task → Prompt Templates (different per model type)
# ============================================================================

TASK_PROMPTS = {
    "event": {
        "classify": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Classify this event type: {event_data}\nRespond with EXACTLY: [event]",
        },
        "facts": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Extract key facts from: {event_data}\nFormat: {\"date\":..., \"location\":..., \"title\":...}",
        },
        "social": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Write an engaging social media caption for: {event_data}\nBe concise, add relevant emojis.",
        },
        "verify": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Verify these details are consistent: {event_data}\nReturn any inconsistencies.",
        },
    },
    "social": {
        "ideation": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Brainstorm 3 post ideas for: {content}\nFormat: [1], [2], [3]",
        },
        "capture": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Write a short, punchy caption: {content}",
        },
        "hashtag": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Generate relevant hashtags: {content}\n5-10, comma separated.",
        },
        "audit": {
            "provider": "omniroute",
            "model": "auto/best-fast",
            "template": "Review and suggest improvements: {content}",
        },
    },
}

# ============================================================================
# HTTP clients for each provider type
# ============================================================================

import urllib.request
import urllib.error

def _chat_openai_compat(base_url: str, model: str, prompt: str, max_tokens: int, timeout: int, key: str = "", extra_headers: dict | None = None) -> str:
    """Generic OpenAI-compatible completion."""
    headers = {"Content-Type": "application/json"}
    if key and not base_url.endswith(":8123") and "localhost" not in base_url:
        headers["Authorization"] = f"Bearer {key}"
    if extra_headers:
        headers.update(extra_headers)
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(f"{base_url}/v1/chat/completions", data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not text.strip():
                raise RuntimeError("empty completion")
            return text
    except Exception as e:
        raise RuntimeError(f"HTTP error: {e}")

def _chat_cohere(api_key: str, model: str, prompt: str, max_tokens: int, timeout: int) -> str:
    """Cohere's native chat endpoint (not OpenAI-compatible)."""
    payload = json.dumps({
        "message": prompt,
        "model": model,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.cohere.com/v1/chat",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            text = data.get("text", "")
            if not text.strip():
                raise RuntimeError("empty completion")
            return text
    except Exception as e:
        raise RuntimeError(f"HTTP error: {e}")

# ============================================================================
# Provider endpoint registry
# ============================================================================

PROVIDER_ENDPOINTS = {
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "GOOGLE_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "together": ("https://api.together.ai/v1", "TOGETHER_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "omniroute": ("http://127.0.0.1:20128", ""),
    "nous": ("https://inference-api.nousresearch.com/v1", ""),
    "huggingface": ("https://router.huggingface.co/v1", "HUGGINGFACE_API_KEY"),
    "cohere": ("https://api.cohere.com/v1/chat", "COHERE_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_NIM_API_KEY"),
}

# Extra headers per provider (OpenRouter needs these for free tier)
PROVIDER_EXTRA_HEADERS = {
    "openrouter": {"HTTP-Referer": "http://localhost", "X-Title": "Hermes"},
}

def _get_env(key: str, default: str = "") -> str:
    """Read API key from environment or ~/.hermes/.env file."""
    import os as _os
    val = _os.environ.get(key)
    if val:
        return val
    env_file = _os.path.expanduser("~/.hermes/.env")
    if _os.path.exists(env_file):
        for line in open(env_file).readlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip("'\"")
    return default

# ============================================================================
# Parallel Agent Executor
# ============================================================================

def _format_template(template: str, payload: str) -> str:
    """Replace only {event_data} and {content} placeholders, leave JSON braces intact."""
    return template.replace("{event_data}", payload).replace("{content}", payload)

def _execute_subtask(name: str, config: dict, payload: str, max_tokens: int = 500, timeout: int = 30) -> dict:
    """Execute one subtask with its designated model. Returns result dict.
    
    Tries the primary provider first; on failure, falls back to the next
    provider in the chain: OmniRoute (most models) -> HuggingFace -> Nous.
    """
    provider = config["provider"]
    model = config["model"]
    template = config["template"]
    prompt = _format_template(template, payload)
    
    # Fallback chain: primary -> working providers when OmniRoute is down
    _fallback_chain = [
        (provider, model),
        ("nvidia", "google/gemma-4-31b-it"),
        ("google", "gemini-3.6-flash"),
        ("together", "deepseek-ai/DeepSeek-V4-Flash-0731"),
    ]
    
    last_error = ""
    for i, (prov, mdl) in enumerate(_fallback_chain):
        if prov == provider and mdl == model and i > 0:
            continue  # already tried as primary
        if prov == "omniroute" and provider == "omniroute" and i > 0:
            continue  # don't retry omniroute as fallback if it was the primary
        started = datetime.now(timezone.utc)
        try:
            if prov == "cohere":
                key = _get_env("COHERE_API_KEY")
                text = _chat_cohere(key, mdl, prompt, max_tokens, timeout=10)
            elif prov in ("google", "groq", "openrouter", "together", "mistral", "huggingface"):
                base_url, key_env = PROVIDER_ENDPOINTS[prov]
                key = _get_env(key_env)
                extra = PROVIDER_EXTRA_HEADERS.get(prov)
                text = _chat_openai_compat(base_url, mdl, prompt, max_tokens, timeout=timeout, key=key, extra_headers=extra)
            elif prov == "omniroute":
                key = _get_env("OMNIROUTE_LOCAL_API_KEY")
                text = None
                for attempt in range(3):
                    try:
                        text = _chat_openai_compat("http://127.0.0.1:20128", mdl, prompt, max_tokens, timeout=timeout, key=key)
                        break
                    except Exception:
                        if attempt == 2:
                            raise RuntimeError("OmniRoute unavailable (no binary running)")
                        time.sleep(2)  # wait for OmniRoute to restart (auto-restart in ~2s)
            elif prov == "nous":
                key = _get_env("NOUS_API_KEY")
                text = _chat_openai_compat("https://inference-api.nousresearch.com/v1", mdl, prompt, max_tokens, timeout=timeout, key=key)
            elif prov == "nvidia":
                key = _get_env("NVIDIA_NIM_API_KEY")
                text = _chat_openai_compat("https://integrate.api.nvidia.com/v1", mdl, prompt, max_tokens, timeout=10, key=key)
            else:
                raise RuntimeError(f"Unknown provider: {prov}")
            
            with open(ROUTING_LOG, "a") as fh:
                json.dump({
                    "ts": started.isoformat(timespec="seconds"),
                    "subtask": name,
                    "provider": prov,
                    "model": mdl,
                    "fallback": i > 0,
                    "ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                    "ok": True,
                }, fh)
                fh.write("\n")
            
            return {"subtask": name, "provider": prov, "model": mdl, "result": text, "ok": True,
                    "fallback": i > 0}

        except Exception as e:
            last_error = str(e)[:200]
            with open(ROUTING_LOG, "a") as fh:
                json.dump({
                    "ts": started.isoformat(timespec="seconds"),
                    "subtask": name,
                    "provider": prov,
                    "model": mdl,
                    "fallback": i > 0,
                    "ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                    "ok": False,
                    "error": str(e)[:200],
                }, fh)
                fh.write("\n")

    # All providers in fallback chain failed
    with open(ROUTING_LOG, "a") as fh:
        json.dump({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "subtask": name,
            "provider": provider,
            "model": model,
            "ok": False,
            "error": last_error,
        }, fh)
        fh.write("\n")
    return {"subtask": name, "provider": provider, "model": model, "result": None, "ok": False, "error": last_error}


def parallel_agents(task_type: str, payload: str, strategy: str = "nvidia") -> List[dict]:
    """Run all subtask prompts in parallel, return results.

    Args:
        task_type: "event" or "social"
        payload: Raw event data or social content
        strategy: "nvidia" (quality), "omni" (fast), or "balanced" (mixed)

    Returns:
        List of dicts: [{subtask, provider, model, result, ok, ...}, ...]
    """
    if task_type not in TASK_PROMPTS:
        raise ValueError(f"Unknown task type: {task_type}. Available: {list(TASK_PROMPTS)}")

    subprompts = TASK_PROMPTS[task_type]
    if strategy == "omni":
        # Replace NVIDIA models with OmniRoute fast lanes
        subprompts = {
            k: dict(v, provider="omniroute", model="auto/best-fast")
            for k, v in subprompts.items()
        }
    elif strategy == "balanced":
        # Mix NVIDIA quality with OmniRoute speed
        for k, v in subprompts.items():
            if v["provider"] == "nvidia":
                v["provider"] = "omniroute"
                v["model"] = "auto/best-fast"

    results = []
    with ThreadPoolExecutor(max_workers=len(subprompts)) as pool:
        futures = {
            pool.submit(_execute_subtask, name, config, payload): name
            for name, config in subprompts.items()
        }
        for future in as_completed(futures):
            try:
                result = future.result(timeout=30)
                results.append(result)
            except Exception as e:
                name = futures[future]
                results.append({"subtask": name, "ok": False, "error": str(e)})

    return results


def synthesize(results: List[dict]) -> dict:
    """Merge parallel results into a coherent output."""
    successful = [r for r in results if r.get("ok") and r.get("result")]
    if not successful:
        return {"success": False, "error": "All subtasks failed"}

    merged = {}
    for r in successful:
        merged[r["subtask"]] = {"provider": r["provider"], "model": r["model"], "result": r["result"]}

    return {"success": True, "parts": merged, "count": len(successful), "total": len(results)}