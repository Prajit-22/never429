"""never429 Worker entry point.

This file is the only one that imports Cloudflare/Pyodide modules. It is a
thin adapter: parse the HTTP request, enforce the optional gateway key, call
into gateway.py, and translate the result back into a Response. All logic
lives in gateway.py and is covered by local unit tests.
"""

import json

import httpx
from js import Object, fetch as js_fetch, caches
from pyodide.ffi import to_js as _to_js
from workers import Response, WorkerEntrypoint

import config as gw_config
import gateway as gw


def to_js(obj):
    # Pyodide needs help turning nested Python dicts into JS objects.
    return _to_js(obj, dict_converter=Object.fromEntries)


def env_dict(env, *names):
    """Pull the settings we care about out of the Worker's env, as plain
    Python strings. Secrets and vars both arrive as attributes on env."""
    return {n: str(getattr(env, n)) for n in names
            if getattr(env, n, None) is not None}


ENV_NAMES = (
    "GATEWAY_KEY", "MODEL_MAP", "DAILY_CAPS",
    "CACHE_TTL_SECONDS", "UPSTREAM_TIMEOUT_SECONDS",
    "GROQ_API_KEY", "GROQ_BASE_URL",
    "GEMINI_API_KEY", "GEMINI_BASE_URL",
    "CEREBRAS_API_KEY", "CEREBRAS_BASE_URL",
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
)


class EdgeCache:
    """never429's cache port, backed by the Cloudflare edge cache. Keys are
    synthetic URLs because the Cache API is keyed on URLs, not strings. All
    failures are swallowed: a cache outage must never break the gateway."""

    async def get(self, key):
        try:
            resp = await caches.default.match(f"https://cache.never429/{key}")
            if resp is None:
                return None
            return await resp.text()
        except Exception:
            return None

    async def put(self, key, text, ttl):
        try:
            resp = Response(text, headers={
                "Cache-Control": f"max-age={ttl}",
                "content-type": "application/json",
            })
            await caches.default.put(f"https://cache.never429/{key}", resp)
        except Exception:
            pass


class KvUsage:
    """never429's usage port, backed by an optional Workers KV namespace.
    Bound as USAGE_KV in wrangler.toml; without it, daily caps are off."""

    def __init__(self, kv):
        self.kv = kv

    async def get(self, key):
        value = await self.kv.get(key)
        return int(value) if value is not None else None

    async def put(self, key, count, ttl):
        await self.kv.put(key, str(count), expirationTtl=ttl)


async def http_post(url, headers, json_body, timeout):
    """One blocking upstream call. httpx runs inside Python Workers; locally
    the tests swap this for a mock transport."""

    def _call():
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=json_body)
            return resp.status_code, dict(resp.headers), resp.content

    return _call()


async def http_stream(url, headers, json_body, timeout):
    """Open a streaming upstream via the JS fetch API and hand back the raw
    Response object so entry.py can return it without buffering tokens."""
    init = {
        "method": "POST",
        "headers": headers,
        "body": json.dumps(json_body),
    }
    resp = await js_fetch(url, to_js(init))
    return int(resp.status), resp


def openai_error(message, status):
    return Response.json(
        {"error": {"message": message, "type": "invalid_request_error"}},
        status=status)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        from urllib.parse import urlparse
        path = urlparse(request.url).path
        env = env_dict(self.env, *ENV_NAMES)

        if path in ("/", "/health"):
            return Response.json({
                "service": "never429",
                "status": "ok",
                "docs": "https://github.com/Prajit-22/never429",
            })

        if path == "/v1/models":
            return Response.json({
                "object": "list",
                "data": [{"id": m, "object": "model"}
                         for m in self._models(env)],
            })

        if path != "/v1/chat/completions":
            return openai_error("not found", 404)
        if request.method != "POST":
            return openai_error("POST only", 405)

        # If the deployer set a gateway key, clients must present it. Without
        # this anyone who finds the workers.dev URL could burn your quotas.
        gateway_key = env.get("GATEWAY_KEY")
        if gateway_key:
            if request.headers.get("authorization") != f"Bearer {gateway_key}":
                return openai_error("missing or wrong gateway key", 401)

        try:
            body = await request.json()
        except Exception:
            return openai_error("body must be JSON", 400)

        cfg = gw_config.build_config(str(body.get("model") or "default"), env)
        if not cfg.chain:
            return openai_error(
                "no providers configured - set at least one *_API_KEY secret",
                503)

        cache = EdgeCache()
        kv = getattr(self.env, "USAGE_KV", None)
        usage = KvUsage(kv) if kv is not None else None

        if body.get("stream"):
            reply = await gw.handle_stream(
                body, config=cfg, http_stream=http_stream, usage=usage)
            if reply.raw is not None:
                # Hand the live upstream stream straight to the client.
                return reply.raw
        else:
            reply = await gw.handle_chat(
                body, config=cfg, http_post=http_post,
                cache=cache, usage=usage)

        return Response(reply.body or b"", status=reply.status,
                        headers=reply.headers)

    def _models(self, env):
        raw = env.get("MODEL_MAP")
        if raw:
            try:
                return sorted(json.loads(raw).keys())
            except json.JSONDecodeError:
                pass
        import providers
        return sorted(providers.DEFAULT_MODEL_MAP.keys())
