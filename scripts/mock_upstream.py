"""Mock upstream for recording the demo without spending a single token.

Serves two fake providers on http://127.0.0.1:8790:
  POST /fail/chat/completions  -> always 429, like a provider out of quota
  POST /ok/chat/completions    -> always 200 with a canned completion

Run:  python scripts/mock_upstream.py
Then: GROQ_BASE_URL=http://127.0.0.1:8790/fail and
      GEMINI_BASE_URL=http://127.0.0.1:8790/ok in .dev.vars
Now every request to the gateway shows a real failover.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)  # drain the request body

        if self.path.startswith("/fail"):
            print(f"429 -> {self.path}")
            self._send(429, {"error": {"message": "rate limit reached"}})
        else:
            print(f"200 -> {self.path}")
            self._send(200, {
                "id": "chatcmpl-demo",
                "object": "chat.completion",
                "model": "mock-free-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": "served by the fallback provider"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 5,
                          "total_tokens": 6},
            })

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    print("mock upstream on http://127.0.0.1:8790  (/fail and /ok)")
    HTTPServer(("127.0.0.1", 8790), Handler).serve_forever()
