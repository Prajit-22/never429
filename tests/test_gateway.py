"""Unit tests for never429's core logic. Run: pytest -q"""

import asyncio
import json
import sys
import os

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as gw_config
import gateway as gw
from gateway import GatewayConfig, ProviderSpec
from fakes import FakeCache, FakeUsage, make_http_post, chat_response

BODY = {"model": "default", "messages": [{"role": "user", "content": "hi"}]}


def make_config(names=("groq", "gemini", "cerebras"), caps=None,
                cache_ttl=3600):
    chain = [ProviderSpec(n, f"https://{n}.test", f"key-{n}", f"model-{n}")
             for n in names]
    return GatewayConfig(chain=chain, cache_ttl=cache_ttl, daily_caps=caps or {})


def run(coro):
    return asyncio.run(coro)


def router(responses):
    """Map hostname -> httpx.Response (or list of them, popped in order)."""
    def handler(request):
        host = request.url.host.split(".")[0]
        value = responses[host]
        if isinstance(value, list):
            return value.pop(0)
        return value
    return handler


def ok_json(host="x"):
    return httpx.Response(200, json=chat_response(host))


def test_first_provider_success():
    cfg = make_config()
    post = make_http_post(router({"groq": ok_json("groq")}))
    reply = run(gw.handle_chat(BODY, config=cfg, http_post=post,
                               cache=FakeCache()))
    assert reply.status == 200
    assert reply.provider == "groq"
    assert reply.headers["x-never429-cache"] == "miss"
    assert json.loads(reply.body)["model"] == "groq"


def test_429_fails_over_to_second_provider():
    cfg = make_config()
    post = make_http_post(router({
        "groq": httpx.Response(429, json={"error": "rate limited"}),
        "gemini": ok_json("gemini"),
    }))
    reply = run(gw.handle_chat(BODY, config=cfg, http_post=post,
                               cache=FakeCache()))
    assert reply.status == 200
    assert reply.provider == "gemini"
    assert reply.attempts[0] == {"provider": "groq", "outcome": "http_429"}
    assert reply.attempts[-1]["outcome"] == "ok"


def test_bad_key_401_also_fails_over():
    cfg = make_config(names=("groq", "gemini"))
    post = make_http_post(router({
        "groq": httpx.Response(401, json={"error": "bad key"}),
        "gemini": ok_json(),
    }))
    reply = run(gw.handle_chat(BODY, config=cfg, http_post=post))
    assert reply.status == 200


def test_client_error_400_does_not_fail_over():
    cfg = make_config()
    post = make_http_post(router({
        "groq": httpx.Response(400, json={"error": "bad request"}),
    }))
    reply = run(gw.handle_chat(BODY, config=cfg, http_post=post))
    assert reply.status == 400  # returned as-is, gemini never tried
    assert reply.provider == "groq"


def test_network_error_fails_over():
    def handler(request):
        if request.url.host.startswith("groq"):
            raise httpx.ConnectError("boom", request=request)
        return ok_json("gemini")
    cfg = make_config()
    reply = run(gw.handle_chat(BODY, config=cfg,
                               http_post=make_http_post(handler)))
    assert reply.status == 200
    assert reply.provider == "gemini"
    assert reply.attempts[0]["outcome"].startswith("error:")


def test_all_providers_fail_returns_503_with_attempts():
    cfg = make_config()
    post = make_http_post(router({
        "groq": httpx.Response(429, json={}),
        "gemini": httpx.Response(503, json={}),
        "cerebras": httpx.Response(500, json={}),
    }))
    reply = run(gw.handle_chat(BODY, config=cfg, http_post=post))
    assert reply.status == 503
    error = json.loads(reply.body)["error"]
    assert len(error["attempts"]) == 3
    assert "x-never429-attempts" in reply.headers


def test_cache_hit_skips_upstream():
    cfg = make_config()
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return ok_json("groq")

    cache = FakeCache()
    post = make_http_post(handler)
    run(gw.handle_chat(BODY, config=cfg, http_post=post, cache=cache))
    second = run(gw.handle_chat(BODY, config=cfg, http_post=post, cache=cache))
    assert second.from_cache is True
    assert second.headers["x-never429-cache"] == "hit"
    assert len(calls) == 1  # upstream called only once


def test_cache_disabled_with_zero_ttl():
    cfg = make_config(cache_ttl=0)
    calls = []

    def handler(request):
        calls.append(1)
        return ok_json()

    cache = FakeCache()
    post = make_http_post(handler)
    run(gw.handle_chat(BODY, config=cfg, http_post=post, cache=cache))
    run(gw.handle_chat(BODY, config=cfg, http_post=post, cache=cache))
    assert len(calls) == 2
    assert cache.store == {}


def test_budget_cap_skips_provider():
    cfg = make_config(names=("groq", "gemini"), caps={"groq": 5})
    usage = FakeUsage(initial={})
    # Pretend groq already burned 5 requests today.
    day_key = [k for k in [gw._usage_key("groq")]][0]
    usage.counts[day_key] = 5
    post = make_http_post(router({"gemini": ok_json("gemini")}))
    reply = run(gw.handle_chat(BODY, config=cfg, http_post=post,
                               usage=usage))
    assert reply.provider == "gemini"
    assert reply.attempts[0]["outcome"] == "skipped_budget"


def test_every_attempt_is_counted():
    cfg = make_config()
    usage = FakeUsage()
    post = make_http_post(router({
        "groq": httpx.Response(429, json={}),
        "gemini": ok_json(),
    }))
    run(gw.handle_chat(BODY, config=cfg, http_post=post, usage=usage))
    assert usage.counts[gw._usage_key("groq")] == 1
    assert usage.counts[gw._usage_key("gemini")] == 1


def test_missing_messages_is_400():
    reply = run(gw.handle_chat({"model": "default"}, config=make_config(),
                               http_post=make_http_post(lambda r: ok_json())))
    assert reply.status == 400


def test_upstream_model_name_is_rewritten():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok_json()

    cfg = make_config(names=("groq",))
    run(gw.handle_chat(BODY, config=cfg, http_post=make_http_post(handler)))
    assert seen["body"]["model"] == "model-groq"
    assert seen["body"]["messages"] == BODY["messages"]


def test_streaming_fails_over_before_tokens_flow():
    cfg = make_config(names=("groq", "gemini"))

    async def http_stream(url, headers, json_body, timeout):
        if "groq" in url:
            return 429, None
        return 200, "RAW-STREAM-HANDLE"

    reply = run(gw.handle_stream({**BODY, "stream": True}, config=cfg,
                                 http_stream=http_stream))
    assert reply.status == 200
    assert reply.raw == "RAW-STREAM-HANDLE"
    assert reply.provider == "gemini"


def test_streaming_all_fail_is_503():
    cfg = make_config(names=("groq",))

    async def http_stream(url, headers, json_body, timeout):
        return 429, None

    reply = run(gw.handle_stream({**BODY, "stream": True}, config=cfg,
                                 http_stream=http_stream))
    assert reply.status == 503
    assert reply.raw is None


# --- config.py tests ------------------------------------------------------

ENV_KEYS = {
    "GROQ_API_KEY": "gk",
    "GEMINI_API_KEY": "mk",
    # cerebras and openrouter keys deliberately missing
}


def test_chain_skips_providers_without_keys():
    chain = gw_config.build_chain("default", ENV_KEYS)
    names = [s.name for s in chain]
    assert "groq" in names and "gemini" in names
    assert "cerebras" not in names and "openrouter" not in names


def test_model_map_env_override():
    env = {**ENV_KEYS, "MODEL_MAP": json.dumps({"coding": ["gemini:x-1"]})}
    chain = gw_config.build_chain("coding", env)
    assert len(chain) == 1
    assert chain[0].model == "x-1"


def test_unknown_model_falls_back_to_default_chain():
    chain = gw_config.build_chain("nonexistent-model", ENV_KEYS)
    assert len(chain) > 0


def test_base_url_override_for_demo():
    env = {**ENV_KEYS, "GROQ_BASE_URL": "http://127.0.0.1:8790/fail"}
    chain = gw_config.build_chain("default", env)
    groq = [s for s in chain if s.name == "groq"][0]
    assert groq.base_url == "http://127.0.0.1:8790/fail"


def test_bad_json_config_does_not_crash():
    env = {**ENV_KEYS, "MODEL_MAP": "{not json", "DAILY_CAPS": "??"}
    cfg = gw_config.build_config("default", env)
    assert len(cfg.chain) > 0
    assert cfg.daily_caps == {}
