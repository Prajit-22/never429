#!/usr/bin/env python3
"""Call free LLM tiers with automatic failover - the agent-facing CLI.

    python3 llm.py ask "summarize this text: ..."          # default chain
    python3 llm.py ask --model fast "quick question"
    python3 llm.py ask --system "You are terse" "hello"
    python3 llm.py status        # today's usage + active cooldowns
    python3 llm.py models        # the chains and which hops have keys

API keys come from the environment: GROQ_API_KEY, GEMINI_API_KEY,
CEREBRAS_API_KEY, MISTRAL_API_KEY, OPENROUTER_API_KEY (comma-separate
several keys for one provider). Optional: DAILY_CAPS='{"groq": 900}',
MODEL_MAP (JSON chain override), LLM_FAILOVER_STATE (state file path).

ask prints ONLY the model's reply on stdout, so an agent can use the output
directly; provider and failover details go to stderr. Exit codes: 0 ok,
1 every provider failed, 2 usage/config error.
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import failover
import providers


def cmd_ask(args) -> int:
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    messages.append({"role": "user", "content": prompt})
    extra = {}
    if args.temperature is not None:
        extra["temperature"] = args.temperature
    try:
        text, provider, attempts = failover.chat(
            messages, model=args.model, timeout=args.timeout, extra=extra
        )
    except failover.ChainExhausted as exc:
        print("FAILED: " + str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    hops = " -> ".join(
        "%s(%s)" % (a["provider"], a["outcome"]) for a in attempts
    )
    print("via %s | %s" % (provider, hops), file=sys.stderr)
    print(text)
    return 0


def cmd_status(args) -> int:
    report = failover.status_report()
    print("date: " + report["date"])
    if report["usage"]:
        print("attempts today:")
        for name, count in sorted(report["usage"].items()):
            print("  %s: %d" % (name, count))
    else:
        print("attempts today: none")
    if report["cooldowns"]:
        print("cooldowns:")
        for row in report["cooldowns"]:
            print("  %s: retry in %ds" % (row["provider"], row["retry_in_seconds"]))
    else:
        print("cooldowns: none")
    return 0


def cmd_models(args) -> int:
    for name, entries in providers.DEFAULT_MODEL_MAP.items():
        chain = failover.build_chain(name)
        ready = [c["display"] for c in chain]
        print("%s:" % name)
        for entry in entries:
            provider = entry.split(":", 1)[0]
            mark = "ready" if any(
                c == provider or c.startswith(provider + "#") for c in ready
            ) else "no key"
            print("  %-40s %s" % (entry, mark))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Free LLM tiers with automatic failover (never429, client-side)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="send a prompt through the failover chain")
    p.add_argument("prompt", nargs="?", help="the prompt; omit to read stdin")
    p.add_argument("--model", default="default", help="chain name (default: %(default)s)")
    p.add_argument("--system", help="optional system message")
    p.add_argument("--temperature", type=float)
    p.add_argument("--timeout", type=float, default=30.0)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("status", help="today's usage and active cooldowns")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("models", help="list chains and which hops have keys")
    p.set_defaults(func=cmd_models)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
