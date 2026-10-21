"""Test doubles for never429's ports. No Cloudflare, no network: httpx's
MockTransport plays the upstream providers, and two tiny async classes play
the edge cache and the KV usage counters."""

import httpx


class FakeCache:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def put(self, key, text, ttl):
        self.store[key] = text


class FakeUsage:
    def __init__(self, initial=None):
        self.counts = dict(initial or {})

    async def get(self, key):
        return self.counts.get(key)

    async def put(self, key, count, ttl):
        self.counts[key] = count


def make_http_post(handler):
    """Build a gateway http_post port whose 'network' is an httpx
    MockTransport running the given handler function."""

    async def http_post(url, headers, json_body, timeout):
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport,
                                     timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=json_body)
            return resp.status_code, dict(resp.headers), resp.content

    return http_post


def chat_response(model, text="hello"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                  "total_tokens": 4},
    }
