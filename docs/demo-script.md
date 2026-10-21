# Recording the 60-second demo GIF

Goal of the clip: one request hits a provider that is out of quota (429),
and the gateway silently answers from the next provider. No paid tokens
needed - the "providers" are the included mock server.

1. Terminal 1 - fake providers:

   python scripts/mock_upstream.py

2. Terminal 2 - the gateway, pointed at the fakes. `.dev.vars`:

   GROQ_API_KEY=demo
   GEMINI_API_KEY=demo
   GROQ_BASE_URL=http://127.0.0.1:8790/fail
   GEMINI_BASE_URL=http://127.0.0.1:8790/ok

   npm install
   npm run dev

3. Terminal 3 - the money shot, run twice:

   curl -s http://127.0.0.1:8787/v1/chat/completions \
     -H "content-type: application/json" \
     -d '{"model": "default", "messages": [{"role": "user", "content": "hi"}]}' \
     -D - | grep -iE "HTTP/|x-never429"

   First run shows `x-never429-provider: gemini` and
   `x-never429-attempts: groq:http_429,gemini:ok`.
   Second run shows `x-never429-cache: hit` - same answer, zero upstream
   calls.

4. Record the terminal (ScreenToGif on Windows works well), crop to ~60s,
   export as docs/demo.gif (keep it under 5 MB so it plays inline on X and
   GitHub).

Optional second scene: point GROQ_BASE_URL back to the real API with a real
key and run one request - shows it working against live providers.
