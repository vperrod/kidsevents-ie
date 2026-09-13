"""Lane fallback for llm.complete(), against a fake OpenAI-compatible server.

No network: one ThreadingHTTPServer on a loopback port plays both the local
llama-server (model "local", plus /health and /metrics) and the OmniRoute
gateway (every other model), and the hermes CLI lane is stubbed.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import llm

BUSY = {"requests_processing": 0}
FAILING_MODELS = set()
CALLED_MODELS = []


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        body = f"llamacpp:requests_processing {BUSY['requests_processing']}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = request["model"]
        CALLED_MODELS.append(model)
        if model in FAILING_MODELS:
            return self._send(200, {"error": {"message": f"{model} is cooling down"}})
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": f"answer from {model}"}}]})


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def routed(server, tmp_path, monkeypatch):
    """llm pointed at the fake server, with a clean routing log and lane state."""
    BUSY["requests_processing"] = 0
    FAILING_MODELS.clear()
    CALLED_MODELS.clear()
    monkeypatch.setattr(llm, "ROUTING_LOG", tmp_path / "routing.jsonl")
    monkeypatch.setattr(llm, "LOCAL_LLM_URL", server)
    monkeypatch.setattr(llm, "OMNIROUTE_URL", server)
    monkeypatch.setattr(llm, "ROUTING_LANES", ["lane-a", "lane-b"])
    monkeypatch.setattr(llm, "_live_lanes", None)
    monkeypatch.setattr(llm, "_parked", {})
    monkeypatch.setattr(llm, "_next_lane", 0)
    monkeypatch.setattr(llm, "_hermes_cli", lambda *a, **k: "answer from hermes")
    return llm


def routing_lanes(module):
    """The lane of every non-probe line in the routing log, in order."""
    lines = module.ROUTING_LOG.read_text().splitlines()
    return [json.loads(x)["lane"] for x in lines if json.loads(x)["kind"] != "lane_probe"]


def test_free_local_model_answers_first(routed):
    assert routed.complete("hello", "test") == "answer from local"


def test_busy_local_model_is_skipped_for_a_named_lane(routed):
    BUSY["requests_processing"] = 2
    assert routed.complete("hello", "test") == "answer from lane-a"


def test_long_prompt_is_not_sent_to_the_local_model(routed):
    assert routed.complete("x" * (llm.LOCAL_MAX_PROMPT_CHARS + 1), "test") == "answer from lane-a"


def test_unreachable_local_model_falls_through(routed, monkeypatch):
    monkeypatch.setattr(llm, "LOCAL_LLM_URL", "http://127.0.0.1:1")
    assert routed.complete("hello", "test") == "answer from lane-a"


def test_failing_named_lane_falls_through_to_the_next_one(routed):
    BUSY["requests_processing"] = 2
    FAILING_MODELS.add("lane-a")
    assert routed.complete("hello", "test") == "answer from lane-b"


def test_all_named_lanes_failing_falls_through_to_auto(routed):
    BUSY["requests_processing"] = 2
    FAILING_MODELS.update({"lane-a", "lane-b"})
    assert routed.complete("hello", "test") == f"answer from {llm.AUTO_LANE}"


def test_failing_gateway_falls_through_to_hermes(routed):
    BUSY["requests_processing"] = 2
    FAILING_MODELS.update({"lane-a", "lane-b", llm.AUTO_LANE})
    assert routed.complete("hello", "test") == "answer from hermes"


def test_every_lane_failing_returns_empty(routed, monkeypatch):
    BUSY["requests_processing"] = 2
    FAILING_MODELS.update({"lane-a", "lane-b", llm.AUTO_LANE})
    monkeypatch.setattr(llm, "_hermes_cli", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no hermes")))
    assert routed.complete("hello", "test") == ""


def test_lane_that_failed_the_probe_is_never_used(routed):
    BUSY["requests_processing"] = 2
    FAILING_MODELS.add("lane-a")
    routed.complete("hello", "test")
    assert "lane-a" not in routing_lanes(routed)[1:]


def test_a_lane_that_errors_mid_run_is_parked(routed):
    BUSY["requests_processing"] = 2
    routed.complete("first", "test")
    FAILING_MODELS.add("lane-b")
    routed.complete("second", "test")   # lane-b errors and is parked
    routed.complete("third", "test")
    assert routing_lanes(routed) == ["lane-a", "lane-b", "lane-a", "lane-a"]


def test_every_attempt_is_logged_with_its_outcome(routed):
    BUSY["requests_processing"] = 2
    FAILING_MODELS.add("lane-a")
    routed.complete("hello", "enrich_event")
    lines = [json.loads(x) for x in routed.ROUTING_LOG.read_text().splitlines()]
    assert [(x["kind"], x["lane"], x["ok"]) for x in lines if x["kind"] != "lane_probe"] == [
        ("enrich_event", "lane-b", True)
    ]


def test_stats_counts_calls_per_lane(routed):
    routed.complete("hello", "test")
    routed.complete("hello again", "test")
    assert routed.stats(routed.ROUTING_LOG)["by_lane"]["local"]["calls"] == 2


def test_stats_reports_the_last_probe(routed):
    BUSY["requests_processing"] = 2
    routed.complete("hello", "test")
    assert routed.stats(routed.ROUTING_LOG)["last_probe"] is not None
