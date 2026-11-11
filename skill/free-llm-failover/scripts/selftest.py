#!/usr/bin/env python3
"""Smoke test for the free-llm-failover skill: proves failover, cooldown
(including Retry-After), daily budget caps and chain exhaustion against
local mock providers - no real API keys, no network beyond localhost.

    python3 scripts/selftest.py

Exits 0 and prints PASS when every check holds, 1 with the failures."""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LLM = os.path.join(HERE, "llm.py")
FAILOVER = os.path.join(HERE, "failover.py")

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, ok))
    print(("%s %s %s" % ("PASS" if ok else "FAIL", name, detail)).rstrip())
    return ok


def serve(plan):
    """One mock OpenAI-compatible endpoint. ``plan`` is a list of
    (status, headers, body) consumed one per request, last one repeating.
    Each call builds a fresh handler class so concurrent mock providers
    never share plan or hit count."""

    class MockProvider(BaseHTTPRequestHandler):
        hits = 0

        def do_POST(self):
            type(self).hits += 1
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            idx = min(type(self).hits - 1, len(plan) - 1)
            status, headers, body = plan[idx]
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_port


def ok_body(text):
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": text}}]
    })


def run(env, *args, input_text=None):
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        [sys.executable, LLM] + list(args),
        input=input_text, capture_output=True, text=True, env=full_env,
    )


def main():
    ok = True
    tmp = tempfile.mkdtemp(prefix="failover-selftest-")
    state = os.path.join(tmp, "state.json")

    flaky, flaky_url = serve([
        (429, {"Retry-After": "120"}, '{"error":"rate limited"}'),
        (200, {}, ok_body("answer from second key")),
    ])
    healthy, healthy_url = serve([(200, {}, ok_body("answer from gemini"))])
    env = {
        "GROQ_API_KEY": "key-one,key-two",
        "GROQ_BASE_URL": flaky_url,
        "GEMINI_API_KEY": "gem-key",
        "GEMINI_BASE_URL": healthy_url,
        "LLM_FAILOVER_STATE": state,
        "DAILY_CAPS": '{"groq": 5}',
    }

    r = run(env, "ask", "hello")
    ok &= check("failover 429 -> next key",
                r.returncode == 0 and "answer from second key" in r.stdout,
                (r.stdout + r.stderr).strip().splitlines()[-1])

    r = run(env, "status")
    ok &= check("cooldown recorded with Retry-After seconds",
                "groq#1" in r.stdout and "retry in" in r.stdout,
                r.stdout.strip().replace("\n", " | "))

    # groq#1 is benched for 120s: the next call must skip it and go straight
    # to groq#2 (which answers 200 on its second hit).
    r = run(env, "ask", "hello again")
    ok &= check("cooled key skipped on next call",
                r.returncode == 0 and "skipped_cooldown" in r.stderr
                and "groq#1(skipped_cooldown)" in r.stderr,
                r.stderr.strip())

    # Burn the remaining groq budget (cap 5; 3 attempts counted so far:
    # call1 key1+key2, call2 key2), then groq must be skipped over budget
    # and gemini must answer.
    run(env, "ask", "burn one")
    run(env, "ask", "burn two")
    r = run(env, "ask", "over budget now")
    ok &= check("daily budget cap skip -> next provider",
                r.returncode == 0 and "answer from gemini" in r.stdout
                and "skipped_budget" in r.stderr,
                (r.stderr.strip().splitlines() or ["no stderr"])[0])

    # Every provider failing -> exit 1 with all attempts reported.
    dead, dead_url = serve([(503, {}, '{"error":"down"}')])
    env_all_dead = {
        "GROQ_API_KEY": "k",
        "GROQ_BASE_URL": dead_url,
        "LLM_FAILOVER_STATE": os.path.join(tmp, "state2.json"),
    }
    r = run(env_all_dead, "ask", "nobody home")
    ok &= check("chain exhaustion exits 1 and lists attempts",
                r.returncode == 1 and "groq:http_503" in r.stderr,
                r.stderr.strip())

    # No keys at all -> clean usage error, not a traceback.
    r = run({"LLM_FAILOVER_STATE": os.path.join(tmp, "s3.json")}, "ask", "hi")
    ok &= check("no keys -> clean error",
                r.returncode == 2 and "no API keys" in r.stderr,
                r.stderr.strip())

    # models command reflects which providers have keys.
    r = run(env, "models")
    ok &= check("models marks keyless hops",
                "no key" in r.stdout and "ready" in r.stdout,
                r.stdout.strip().splitlines()[1] if r.stdout else "")

    flaky.shutdown(); healthy.shutdown(); dead.shutdown()
    print()
    print("PASS" if ok else "FAIL", "-", sum(1 for _, good in CHECKS if good),
          "of", len(CHECKS), "checks")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
