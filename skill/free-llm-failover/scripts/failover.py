"""Client-side failover across free LLM tiers - the never429 gateway logic,
ported to a synchronous, dependency-free form for agent skill use.

Same semantics as the hosted Cloudflare Worker (github.com/Prajit-22/never429):
walk the provider chain for the requested model, skip keys cooling down
after a 429 (honoring the upstream's Retry-After, capped), skip providers
over their daily budget, fail over on rate limits/auth errors/timeouts/5xx,
and report every attempt when the whole chain fails.

State (cooldowns + daily counters) lives in one JSON file, so separate
processes - and separate agent runs - share it. No third-party packages.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import providers

# Upstream statuses that mean "try the next provider". 401/403 usually mean
# a dead or revoked key, which is exactly what a fallback chain is for.
# 400/404/422 mean the request itself is broken, so it is returned as-is.
FAILOVER_STATUSES = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}

# A Retry-After hint is honored only up to this ceiling: a broken or hostile
# upstream must not be able to bench a key for days.
MAX_RETRY_AFTER_SECONDS = 3600

DEFAULT_STATE_PATH = os.environ.get(
    "LLM_FAILOVER_STATE",
    os.path.join(os.path.expanduser("~"), ".free-llm-failover", "state.json"),
)


class ChainExhausted(Exception):
    """Every provider failed or was skipped. ``attempts`` lists each hop's
    outcome (ok / http_429 / skipped_cooldown / skipped_budget / error:...)."""

    def __init__(self, attempts):
        self.attempts = attempts
        summary = ", ".join("%s:%s" % (a["provider"], a["outcome"]) for a in attempts)
        super().__init__("all providers failed or over budget: " + summary)


# --- chain construction -----------------------------------------------------


def build_chain(model, env=None):
    """Resolve the failover chain for a model name from API keys in the env.

    Returns a list of dicts: name, base_url, api_key, model, display.
    Providers with no key configured are dropped, so one key still works -
    the chain is just shorter. Comma-separated keys for one provider become
    consecutive hops (groq#1, groq#2). MODEL_MAP (env, JSON) overrides the
    built-in map.
    """
    env = os.environ if env is None else env
    model_map = providers.DEFAULT_MODEL_MAP
    raw = env.get("MODEL_MAP")
    if raw:
        try:
            model_map = json.loads(raw)
        except json.JSONDecodeError:
            pass  # bad config should not break the chain; use defaults
    chain = []
    for entry in model_map.get(model) or model_map.get("default") or []:
        provider_name, _, upstream_model = entry.partition(":")
        meta = providers.PROVIDERS.get(provider_name)
        if meta is None or not upstream_model:
            continue
        raw_keys = env.get(meta["key_env"])
        if not raw_keys:
            continue
        base_url = env.get(meta["base_env"]) or meta["base_url"]
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        for i, api_key in enumerate(keys):
            display = provider_name if len(keys) == 1 else "%s#%d" % (provider_name, i + 1)
            chain.append(
                {
                    "name": provider_name,
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": upstream_model,
                    "display": display,
                }
            )
    return chain


def _daily_caps(env=None):
    env = os.environ if env is None else env
    try:
        return {k: int(v) for k, v in json.loads(env.get("DAILY_CAPS", "{}")).items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


# --- state file -------------------------------------------------------------


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            state.setdefault("cooldowns", {})
            state.setdefault("usage", {})
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"cooldowns": {}, "usage": {}}


def _save_state(path, state):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def retry_after_seconds(headers):
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds,
    or None when missing/unparseable."""
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


# --- reports ----------------------------------------------------------------


def status_report(state_path=DEFAULT_STATE_PATH, env=None):
    """Today's attempt counters per provider plus keys currently benched
    after a 429, with seconds left on each cooldown."""
    state = _load_state(state_path)
    now = time.time()
    # Drop expired cooldowns so reports and chain walks agree.
    state["cooldowns"] = {
        k: v for k, v in state["cooldowns"].items() if v > now
    }
    cooldowns = [
        {"provider": k, "retry_in_seconds": max(0, int(v - now))}
        for k, v in sorted(state["cooldowns"].items())
    ]
    usage = state["usage"].get(_today(), {})
    return {"date": _today(), "usage": usage, "cooldowns": cooldowns}


# --- the call ---------------------------------------------------------------


def chat(messages, model="default", state_path=DEFAULT_STATE_PATH, env=None,
         timeout=30.0, cooldown_seconds=60, extra=None):
    """Send a chat-completions request through the failover chain.

    ``messages`` is the OpenAI messages array. Returns
    (reply_text, provider_name, attempts). Raises ChainExhausted when every
    hop fails or is skipped, and ValueError when no chain can be built
    (no API keys configured for the requested model).
    """
    env = os.environ if env is None else env
    chain = build_chain(model, env)
    if not chain:
        raise ValueError(
            "no API keys configured for model %r - set at least one of: %s"
            % (model, ", ".join(sorted(m["key_env"] for m in providers.PROVIDERS.values())))
        )
    caps = _daily_caps(env)
    state = _load_state(state_path)
    now = time.time()
    # Prune expired cooldowns once, up front.
    state["cooldowns"] = {k: v for k, v in state["cooldowns"].items() if v > now}
    attempts = []
    dirty = False

    for spec in chain:
        cap = caps.get(spec["name"])
        used = state["usage"].setdefault(_today(), {}).get(spec["name"], 0)
        if cap is not None and used >= cap:
            attempts.append({"provider": spec["display"], "outcome": "skipped_budget"})
            continue
        if spec["display"] in state["cooldowns"]:
            attempts.append({"provider": spec["display"], "outcome": "skipped_cooldown"})
            continue

        state["usage"][_today()][spec["name"]] = used + 1
        dirty = True
        body = {"model": spec["model"], "messages": messages}
        if extra:
            body.update(extra)
        req = urllib.request.Request(
            spec["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + spec["api_key"])
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            attempts.append({"provider": spec["display"], "outcome": "ok"})
            _save_state(state_path, state)
            text = payload["choices"][0]["message"]["content"]
            return text, spec["name"], attempts
        except urllib.error.HTTPError as exc:
            status = exc.code
            attempts.append({"provider": spec["display"], "outcome": "http_%d" % status})
            if status == 429 and cooldown_seconds > 0:
                ttl = cooldown_seconds
                ra = retry_after_seconds(dict(exc.headers.items()))
                if ra is not None:
                    ttl = min(ra, MAX_RETRY_AFTER_SECONDS)
                if ttl > 0:
                    state["cooldowns"][spec["display"]] = time.time() + ttl
            if status not in FAILOVER_STATUSES:
                # Client-side problem (bad request shape): fail it back, the
                # rest of the chain would reject it too.
                _save_state(state_path, state)
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise ValueError(
                    "provider %s rejected the request (HTTP %d): %s"
                    % (spec["display"], status, detail)
                )
        except Exception as exc:  # timeout, DNS, connection reset, ...
            attempts.append(
                {"provider": spec["display"], "outcome": "error:%s" % type(exc).__name__}
            )

    _save_state(state_path, state) if dirty else None
    raise ChainExhausted(attempts)
