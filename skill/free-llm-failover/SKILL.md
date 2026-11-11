---
name: free-llm-failover
description: Calls free-tier LLM providers (Groq, Gemini, Cerebras, Mistral, OpenRouter) with automatic failover - when one key or provider rate-limits, times out, or errors, the next one answers. Use whenever the agent needs an LLM completion without a paid API bill, when a provider returns 429 or 5xx, or when the user wants resilient model calls with budget caps and per-key cooldowns instead of a single point of failure.
license: MIT
compatibility: Python 3.9+. No third-party packages. Network access required to reach the LLM providers. Needs at least one provider API key in the environment (all five providers offer a free tier or free models at the time of writing; quotas change).
metadata:
  author: Prajit Chaturvedi
  version: "1.0"
  homepage: https://github.com/Prajit-22/never429
---

# Free LLM Failover

One command for every free LLM tier: when one provider rate-limits you, the
next one answers. Keys cooling down after a 429 are benched (honoring the
upstream's Retry-After), daily budget caps stop you from burning a
provider's free quota, and every call reports which hops it took.

This is the client-side sibling of never429
(github.com/Prajit-22/never429), a Cloudflare Worker that runs the same
failover logic as a hosted OpenAI-compatible gateway in production.

## Setup

Put one or more free-tier keys in the environment (comma-separate several
keys for one provider - a rate-limited key fails over to its sibling):

```bash
export GROQ_API_KEY="gsk_..."            # console.groq.com (free)
export GEMINI_API_KEY="..."              # aistudio.google.com (free)
export OPENROUTER_API_KEY="sk-or-..."    # openrouter.ai (free models)
# optional: export DAILY_CAPS='{"groq": 900, "gemini": 1400}'
```

All five providers offer a free tier or free models at the time of writing,
but quotas, reset schedules and which models are free change - check the
provider's current rate-limit page before relying on a specific model. The
chains in `scripts/providers.py` are examples that work today, not a
guarantee; override them with `MODEL_MAP` when a provider reshuffles.

## Usage

```bash
python3 scripts/llm.py ask "Summarize this: ..."        # answer on stdout
python3 scripts/llm.py ask --model fast "quick check"   # smaller models
python3 scripts/llm.py status                           # usage + cooldowns
python3 scripts/llm.py models                           # chains + key readiness
```

`ask` prints ONLY the model's reply on stdout - use it directly in your
answer. The provider used and the failover path (e.g.
`groq#1(http_429) -> gemini(ok)`) print on stderr. Exit codes: 0 answered,
1 every provider failed (attempts listed on stderr), 2 config error such as
no keys.

## How you should use it as an agent

1. Route any free-tier LLM call through `ask` instead of a single-provider
   request. If it exits 0, answer from the stdout text.
2. On exit 1, read the attempts on stderr and tell the user plainly which
   providers failed and why (all rate-limited vs all erroring). Do not
   retry in a tight loop - run `status` and tell the user the longest
   cooldown instead.
3. On exit 2 with "no API keys", tell the user which env var to set; every
   provider listed has a free tier.
4. When the user cares which model answered, read the stderr line - the
   provider name is always there.
5. Before a long batch of calls, run `status`: if several keys are cooling
   down or near their caps, warn the user first.

## What happens under the hood

- The chain for each model name walks providers in order
  (`references/design.md` has the full semantics). A hop is skipped when
  its key is cooling down or its provider is over its daily cap.
- After a 429, that key is benched for the upstream's Retry-After (capped
  at one hour) or a 60-second default. Other keys on the same provider keep
  serving.
- Usage counters and cooldowns live in `~/.free-llm-failover/state.json`
  (override with `LLM_FAILOVER_STATE`), so separate runs share them.
- Streaming is intentionally not included; the hosted Worker handles it.

## Files

- `scripts/failover.py` - the failover core: chain building, cooldowns,
  Retry-After, budget caps, state file. Sync, stdlib only.
- `scripts/providers.py` - provider registry and model chains.
- `scripts/llm.py` - the CLI: ask / status / models.
- `scripts/selftest.py` - 7-check smoke test against local mock providers
  (failover, cooldown, Retry-After, budget caps, exhaustion). Run it after
  install and after any tuning.
- `references/design.md` - the failover semantics and their rationale.
- `references/gateway-worker.md` - the hosted Cloudflare Worker version of
  this same design, for a shared always-on endpoint.

## Tuning

- `COOLDOWN_SECONDS` equivalent: `cooldown_seconds` param in failover.chat
  (default 60); per-call in llm.py ask via code, or edit the default.
- `DAILY_CAPS` (JSON env) caps attempts per provider per UTC day. Providers
  reset quotas on their own clocks (Groq: midnight UTC; Gemini: midnight
  Pacific), so set caps below your tightest provider's real daily limit.
- Custom chains: set `MODEL_MAP` to JSON like
  `{"default": ["gemini:gemini-flash-latest", "groq:openai/gpt-oss-120b"]}`.
  Verify changes with `python3 scripts/selftest.py`.
