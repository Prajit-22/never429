"""Core gateway logic for never429.

This module is pure Python with zero Cloudflare imports. Everything that
touches the outside world - HTTP calls, the edge cache, usage counters - is
passed in as a small async callable, so the entire module runs and is
unit-tested locally with plain pytest. The Worker entry point (entry.py)
only wires these ports to Cloudflare APIs.

Flow for one chat completion request:
  1. Check the edge cache (non-streaming requests only).
  2. Walk the provider chain for the requested model.
  3. Skip providers that are over their daily budget.
  4. Try each provider; on 429/5xx/auth error/timeout, fail over to the next.
  5. Cache a successful response and return it.
  6. If every provider failed, return one 503 that lists every attempt.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

# Upstream statuses that mean "try the next provider". 401/403 usually mean
# a dead or revoked key, which is exactly what a fallback chain is for.
# 400/404/422 mean the request itself is broken, so we return those as-is.
FAILOVER_STATUSES = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}

# Days worth of seconds; KV entries self-expire so the namespace stays empty.
USAGE_KEY_TTL = 60 * 60 * 48


@dataclass
class ProviderSpec:
    """One hop in the failover chain: who to call and with which model.
    display labels the hop in debug headers; when a provider has several
    keys it appears once per key as groq#1, groq#2, ..."""
    name: str
    base_url: str
    api_key: str
    model: str
    display: str = ""

    def __post_init__(self):
        if not self.display:
            self.display = self.name


@dataclass
class GatewayConfig:
    chain: list  # list[ProviderSpec], in failover order
    cache_ttl: int = 3600          # seconds; 0 disables the cache
    daily_caps: dict = field(default_factory=dict)  # {"groq": 900, ...}
    timeout: float = 30.0          # per-attempt upstream timeout, seconds


@dataclass
class Reply:
    """What the caller should send back. For streaming, raw holds the
    upstream response object and body is None."""
    status: int
    body: bytes | None
    headers: dict = field(default_factory=dict)
    provider: str | None = None
    from_cache: bool = False
    raw: object | None = None       # streaming passthrough handle
    attempts: list = field(default_factory=list)


def cache_key(body: dict) -> str:
    """Stable hash of the request body. Two identical prompts (same model
    chain, same messages, same parameters) produce the same key, so repeat
    questions are served from the edge cache for free."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "v1:" + hashlib.sha256(canonical.encode()).hexdigest()


def _usage_key(provider: str) -> str:
    """One counter per provider per UTC day, matching how every provider
    resets its own daily free-tier quota."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"usage:{provider}:{day}"


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return max(60, int((tomorrow - now).total_seconds()) + 300)


def _attempts_header(attempts: list) -> str:
    return ",".join(f"{a['provider']}:{a['outcome']}" for a in attempts)


def _error_body(message: str, attempts: list) -> bytes:
    return json.dumps({
        "error": {
            "message": message,
            "type": "upstream_unavailable",
            "attempts": attempts,
        }
    }).encode()


async def _over_budget(spec: ProviderSpec, config: GatewayConfig, usage) -> bool:
    """Best-effort daily cap. If no KV namespace is bound, usage is None and
    caps are simply not enforced - the chain still works."""
    cap = config.daily_caps.get(spec.name)
    if cap is None or usage is None:
        return False
    used = await usage.get(_usage_key(spec.name)) or 0
    return used >= cap


async def _count_attempt(spec: ProviderSpec, usage) -> None:
    if usage is None:
        return
    key = _usage_key(spec.name)
    used = await usage.get(key) or 0
    await usage.put(key, used + 1, _seconds_until_utc_midnight())


def _forward_body(body: dict, upstream_model: str) -> dict:
    """Copy the client request and swap in the upstream model id. Everything
    else - messages, temperature, tools - passes through untouched."""
    out = dict(body)
    out["model"] = upstream_model
    return out


def _headers(spec: ProviderSpec) -> dict:
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {spec.api_key}",
    }


async def handle_chat(body: dict, *, config: GatewayConfig, http_post,
                      cache=None, usage=None) -> Reply:
    """Serve one POST /v1/chat/completions request.

    http_post(url, headers, json_body, timeout) -> (status, headers, bytes)
    cache: async get(key) -> str | None, put(key, text, ttl)
    usage: async get(key) -> int | None, put(key, count, ttl)
    """
    if not isinstance(body.get("messages"), list):
        return Reply(400, _error_body("request needs a messages array", []),
                     {"content-type": "application/json"})

    stream = bool(body.get("stream"))

    key = cache_key(body)
    if not stream and config.cache_ttl > 0 and cache is not None:
        hit = await cache.get(key)
        if hit is not None:
            try:
                cached = json.loads(hit)
                return Reply(200, cached["body_text"].encode(),
                             {"content-type": "application/json",
                              "x-never429-cache": "hit",
                              "x-never429-provider": cached["provider"]},
                             provider=cached["provider"], from_cache=True)
            except (json.JSONDecodeError, KeyError):
                pass  # corrupt cache entry: just go upstream

    attempts = []
    for spec in config.chain:
        if await _over_budget(spec, config, usage):
            attempts.append({"provider": spec.display, "outcome": "skipped_budget"})
            continue

        await _count_attempt(spec, usage)
        try:
            status, _h, payload = await http_post(
                f"{spec.base_url}/chat/completions",
                _headers(spec), _forward_body(body, spec.model), config.timeout)
        except Exception as exc:  # timeout, DNS, connection reset, ...
            attempts.append({"provider": spec.display,
                             "outcome": f"error:{type(exc).__name__}"})
            continue

        if status in FAILOVER_STATUSES:
            attempts.append({"provider": spec.display, "outcome": f"http_{status}"})
            continue

        if status != 200:
            # Client-side problem (bad request shape): fail it back unchanged.
            return Reply(status, payload,
                         {"content-type": "application/json",
                          "x-never429-attempts": _attempts_header(attempts)},
                         provider=spec.name, attempts=attempts)

        attempts.append({"provider": spec.display, "outcome": "ok"})
        headers = {"content-type": "application/json",
                   "x-never429-provider": spec.name,
                   "x-never429-cache": "miss",
                   "x-never429-attempts": _attempts_header(attempts)}
        if not stream and config.cache_ttl > 0 and cache is not None:
            try:
                await cache.put(key, json.dumps({
                    "provider": spec.name,
                    "body_text": payload.decode("utf-8", "replace"),
                }), config.cache_ttl)
            except Exception:
                pass  # caching is an optimization, never a failure mode
        return Reply(200, payload, headers,
                     provider=spec.name, attempts=attempts)

    return Reply(503, _error_body("all providers failed or over budget", attempts),
                 {"content-type": "application/json",
                  "x-never429-attempts": _attempts_header(attempts)},
                 attempts=attempts)


async def handle_stream(body: dict, *, config: GatewayConfig, http_stream,
                        usage=None) -> Reply:
    """Streaming variant. Failover can only happen *before* tokens start
    flowing, so we open each upstream, check its status, and either hand the
    raw live response to the caller or close it and try the next provider.
    Streaming responses are never cached. A non-failover status (a client
    problem like 400) passes the upstream's own response back, same as the
    non-streaming path, instead of a generic 503."""
    if not isinstance(body.get("messages"), list):
        return Reply(400, _error_body("request needs a messages array", []),
                     {"content-type": "application/json"})
    attempts = []
    for spec in config.chain:
        if await _over_budget(spec, config, usage):
            attempts.append({"provider": spec.display, "outcome": "skipped_budget"})
            continue

        await _count_attempt(spec, usage)
        try:
            status, raw = await http_stream(
                f"{spec.base_url}/chat/completions",
                _headers(spec), _forward_body(body, spec.model), config.timeout)
        except Exception as exc:
            attempts.append({"provider": spec.display,
                             "outcome": f"error:{type(exc).__name__}"})
            continue

        if 200 <= status < 300:
            attempts.append({"provider": spec.display, "outcome": "ok"})
            return Reply(status, None, raw=raw,
                         provider=spec.name, attempts=attempts)

        attempts.append({"provider": spec.display, "outcome": f"http_{status}"})
        if status not in FAILOVER_STATUSES:
            # Client-side problem (bad request shape): hand the upstream's own
            # response back rather than blaming the whole chain with a 503.
            return Reply(status, None, raw=raw,
                         provider=spec.name, attempts=attempts)

    return Reply(503, _error_body("all providers failed or over budget", attempts),
                 {"content-type": "application/json",
                  "x-never429-attempts": _attempts_header(attempts)},
                 attempts=attempts)


async def usage_report(provider_names: list, usage) -> dict:
    """Today's attempt counters per provider, for GET /v1/usage. Empty when
    no KV namespace is bound."""
    if usage is None:
        return {}
    report = {}
    for name in provider_names:
        used = await usage.get(_usage_key(name))
        if used:
            report[name] = used
    return report
