"""Build a GatewayConfig from flat environment variables.

The Worker entry point passes its bindings/secrets here as a plain dict of
strings (or None). Keeping this separate from gateway.py means config parsing
is unit-tested too.
"""

import json

import providers
from gateway import GatewayConfig, ProviderSpec


def build_chain(model: str, env: dict) -> list:
    """Resolve the failover chain for a requested model name.

    MODEL_MAP (env var, JSON) overrides providers.DEFAULT_MODEL_MAP.
    Entries whose provider has no API key configured are dropped, so a fresh
    deploy with one key still works - the chain is just shorter.
    """
    raw = env.get("MODEL_MAP")
    model_map = providers.DEFAULT_MODEL_MAP
    if raw:
        try:
            model_map = json.loads(raw)
        except json.JSONDecodeError:
            pass  # bad config should not break the gateway; use defaults

    entries = model_map.get(model) or model_map.get("default") or []
    chain = []
    for entry in entries:
        provider_name, _, upstream_model = entry.partition(":")
        meta = providers.PROVIDERS.get(provider_name)
        if meta is None or not upstream_model:
            continue
        api_key = env.get(meta["key_env"])
        if not api_key:
            continue
        base_url = env.get(meta["base_env"]) or meta["base_url"]
        chain.append(ProviderSpec(provider_name, base_url, api_key, upstream_model))
    return chain


def build_config(model: str, env: dict) -> GatewayConfig:
    caps = {}
    raw_caps = env.get("DAILY_CAPS")
    if raw_caps:
        try:
            caps = {k: int(v) for k, v in json.loads(raw_caps).items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            caps = {}

    def _int(name, default):
        try:
            return int(env.get(name) or default)
        except ValueError:
            return default

    return GatewayConfig(
        chain=build_chain(model, env),
        cache_ttl=_int("CACHE_TTL_SECONDS", 3600),
        daily_caps=caps,
        timeout=float(_int("UPSTREAM_TIMEOUT_SECONDS", 30)),
    )
