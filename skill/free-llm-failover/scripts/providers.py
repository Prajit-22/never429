"""Provider registry, vendored from never429 (github.com/Prajit-22/never429).

Each provider speaks the OpenAI chat-completions format, so the same request
body works with any of them - only the base URL, key and model name change.
Add a provider by adding one dict entry here.
"""

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        # Optional override, mostly for local demos and tests.
        "base_env": "GROQ_BASE_URL",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "base_env": "GEMINI_BASE_URL",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "key_env": "CEREBRAS_API_KEY",
        "base_env": "CEREBRAS_BASE_URL",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "base_env": "MISTRAL_BASE_URL",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "base_env": "OPENROUTER_BASE_URL",
    },
}

# Which upstream model to try, in order, for each model name a caller asks
# for. "default" is used when the requested model is not listed here.
# Format: "<provider>:<upstream model id>". Providers without a configured
# API key are skipped when the chain is built, so it is safe to list all.
DEFAULT_MODEL_MAP = {
    "default": [
        "groq:openai/gpt-oss-120b",
        "gemini:gemini-flash-latest",
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
    ],
    "fast": [
        "groq:openai/gpt-oss-20b",
        "gemini:gemini-flash-lite-latest",
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
    ],
}
