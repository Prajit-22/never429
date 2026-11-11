# The hosted version: never429 as a Cloudflare Worker

This skill is the client-side core. The same failover design also runs as a
hosted OpenAI-compatible gateway you deploy to your own Cloudflare account -
useful when several agents, apps or teammates should share one always-on
endpoint instead of each holding provider keys.

Full source and deploy guide: https://github.com/Prajit-22/never429 (MIT).
It has a one-click Deploy-to-Cloudflare button and a ~1 minute wrangler
flow; it runs entirely on Cloudflare's free tier.

## What the Worker adds over this skill

- **An OpenAI-compatible endpoint** (`https://never429.<you>.workers.dev/v1`):
  point any existing OpenAI client at it by changing base_url - no code
  changes.
- **Edge response caching**: identical prompts (same model chain, messages
  and parameters) are served from Cloudflare's cache for free.
- **Streaming**: failover happens before tokens start flowing; live streams
  pass through untouched.
- **Shared state in KV**: cooldowns and daily counters are shared by every
  caller, not per machine.
- **A `GATEWAY_KEY` secret** to protect the endpoint, and `/v1/usage` for
  today's counters plus active cooldowns.

## Deploy in one paragraph

Click the Deploy-to-Cloudflare button in the repo README (or clone and
`uv run pywrangler deploy`), then `npx wrangler secret put GROQ_API_KEY`
(and any other provider keys, comma-separated for several). Point any
OpenAI client at the worker URL with `api_key` set to your `GATEWAY_KEY`
(or anything, if you did not set one). There is no third-party proxy in the
middle - keys live as secrets in your own Cloudflare account. Each fronted
provider offers a free tier or free models at the time of writing; check
their current rate-limit pages, since quotas and model availability change.
