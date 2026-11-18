"""CLIが申告した消費量だけを保持する。料金表からの推測はしない。"""

import json
import math


def number(value):
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def normalize(provider, usage, cost=None):
    usage = usage if isinstance(usage, dict) else {}
    incoming = number(usage.get("input_tokens"))
    outgoing = number(usage.get("output_tokens"))
    cached = number(
        usage.get("cache_read_input_tokens" if provider == "claude" else "cached_input_tokens")
    )
    creation = number(usage.get("cache_creation_input_tokens")) if provider == "claude" else None
    if provider == "claude" and incoming is not None:
        incoming += (cached or 0) + (creation or 0)
    return dict(
        input_tokens=incoming,
        cached_input_tokens=cached,
        output_tokens=outgoing,
        total_tokens=incoming + outgoing if incoming is not None and outgoing is not None else None,
        cost_usd=number(cost),
        source="cli_reported",
    )


def claude_usage(envelope):
    models = envelope.get("modelUsage")
    if (
        not isinstance(models, dict)
        or not models
        or not all(isinstance(m, dict) for m in models.values())
    ):
        return normalize("claude", envelope.get("usage"), envelope.get("total_cost_usd"))
    fields = {
        "input_tokens": "inputTokens",
        "output_tokens": "outputTokens",
        "cache_read_input_tokens": "cacheReadInputTokens",
        "cache_creation_input_tokens": "cacheCreationInputTokens",
    }
    totals = {
        target: sum(number(m.get(source)) for m in models.values())
        if all(number(m.get(source)) is not None for m in models.values())
        else None
        for target, source in fields.items()
    }
    return normalize("claude", totals, envelope.get("total_cost_usd"))


def extract(provider, raw):
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except ValueError:
            continue
    if provider == "claude":
        envelope = next(
            (
                e
                for e in reversed(events)
                if e.get("type") == "result" or "structured_output" in e or "usage" in e
            ),
            None,
        )
        if envelope:
            return claude_usage(envelope)
    else:
        completed = [
            e
            for e in events
            if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict)
        ]
        if completed:
            summed = {
                k: sum(number(e["usage"].get(k)) or 0 for e in completed)
                if all(number(e["usage"].get(k)) is not None for e in completed)
                else None
                for k in ["input_tokens", "cached_input_tokens", "output_tokens"]
            }
            return normalize(provider, summed)
    return normalize(provider, {})
