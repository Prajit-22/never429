# Failover design: the semantics and why

Read this before tuning the chain, changing cooldown behavior, or explaining
tradeoffs to the user.

## The problem

Every free LLM tier rate-limits: requests per minute, requests per day,
tokens per minute. A single-provider integration works in the demo and dies
in the middle of real work. Naive retry makes it worse - hammering a
rate-limited key extends the limiting and can get the account flagged.

## The chain

Each model name ("default", "fast") maps to an ordered list of
`provider:model` hops. A request walks the chain in order; the first hop
that answers 200 wins. Providers with no key configured are dropped when
the chain is built, so partial setups still work.

Several keys for one provider (comma-separated env var) become consecutive
hops - `groq#1`, `groq#2` - so a dead or revoked key does not bench its
siblings. Caveat: providers count quota differently. Groq enforces rate
limits per organization, so two keys from the same Groq org share one quota
(second key only helps if the first is revoked, not for more capacity);
Gemini limits per project. For real extra capacity, use keys from separate
orgs/projects or from different providers.

## What fails over, what does not

- Fails over: 401, 403, 408, 409, 425, 429, 500, 502, 503, 504, plus
  network errors (timeout, DNS, connection reset). 401/403 usually mean a
  dead or revoked key - exactly what a fallback chain is for.
- Does NOT fail over: 400, 404, 422. Those mean the request itself is
  broken; the next provider would reject it too. The error is returned
  as-is with the upstream's message.

## Cooldowns and Retry-After

After a 429, that key is benched. The upstream's Retry-After header wins
over the 60-second default when present, because the provider knows its own
reset - but it is capped at one hour, so a broken or hostile upstream
cannot bench a key for days. A Retry-After of 0 records no cooldown. While
benched, the hop reports `skipped_cooldown` and the next key serves.

Cooldowns are tracked per key (`groq#1`), never per provider - but remember
providers may still count the underlying quota per org or project.

## Daily budget caps

`DAILY_CAPS` (JSON: `{"groq": 900}`) caps attempts per provider per UTC
day. Providers reset their daily quotas on their own clocks - Groq resets
at midnight UTC, Gemini at midnight Pacific - so a UTC-day counter is a
conservative approximation, not an exact mirror of any provider's quota.
Set caps below the real daily limit of your tightest provider. Over-cap
hops report `skipped_budget` and the chain moves on. Caps are best effort:
they count attempts, not tokens.

## Honest exhaustion

When every hop fails or is skipped, the caller gets one error listing every
attempt (`groq#1:http_429`, `gemini:error:Timeout`, ...). That list is the
diagnostics: it distinguishes "everything is rate-limited" (wait) from
"everything is erroring" (something is wrong with the request or network).
Never retry the whole chain in a tight loop after exhaustion - read the
attempts, check `status`, tell the user the longest cooldown.

## What the hosted Worker adds

The Cloudflare Worker version (same repo root) adds: an OpenAI-compatible
HTTP endpoint anyone can point their existing client at, edge response
caching (identical prompts are free repeats), streaming with pre-token
failover, and KV-backed shared state across all callers. The semantics
above are identical in both.
