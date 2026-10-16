"""Resolve dated model rates and request-length pricing from the JSON tables."""

import re
from datetime import datetime, timezone


def resolve_rate(pricing, model, ts_str, prompt_tokens=0):
    model = model or "unknown"
    schedule = pricing["models"].get(model)
    if schedule is None:
        # Dated snapshots inherit the full model's rates, including cache and
        # context pricing. Do not mistake a newer version for an older model.
        for name in sorted(pricing["models"], key=len, reverse=True):
            if model.startswith(name + "-") and re.fullmatch(
                r"\d{8}|\d{4}-\d{2}-\d{2}", model[len(name) + 1:]
            ):
                schedule = pricing["models"][name]
                break

    if schedule:
        try:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            timestamp = timestamp.replace(tzinfo=timestamp.tzinfo or timezone.utc)
        except (AttributeError, TypeError, ValueError):
            timestamp = None
        ordered = sorted(schedule, key=lambda entry: entry.get("from") or "0000-01-01")
        rate = ordered[0]
        for entry in ordered:
            start = entry.get("from")
            if start is None or (timestamp is not None and timestamp >=
                                datetime.fromisoformat(start + "T00:00:00+00:00")):
                rate = entry
    else:
        rate = next((rate for prefix, rate in pricing["prefix_fallback"]
                     if model.startswith(prefix)), pricing["unknown_default"])

    long_context = rate.get("long_context")
    if long_context and prompt_tokens > long_context["above_tokens"]:
        return {**rate, **long_context}
    return rate
