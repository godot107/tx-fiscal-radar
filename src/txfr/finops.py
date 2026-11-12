"""FinOps: what one question costs, by component (unit economics / showback).

A Meter collects usage while one request runs (context-local, so concurrent requests in the Function App don't mix)
and prices it at list price. The app returns the result with the answer and logs one COST_EVENT line per request
(user, session, $ by component); docs/COSTS.md → Unit economics has the KQL and the measured numbers.

Prices: Azure Retail Prices API (prices.azure.com), checked 2026-11-10, USD, pay-as-you-go list price.
    gpt-4.1-mini Global      $0.40 / 1M input, $0.10 / 1M cached input, $1.60 / 1M output   (eastus2)
    text-embedding-3-small   ~$0.022 / 1M tokens                                           (eastus2, data zone meter)
    Content Safety text      $0.375 / 1,000 text records; a record = up to 1,000 characters (eastus2, Standard)
                             (Prompt Shields and groundedness detection counted as text records of everything sent)
    Functions Flex on demand $0.000026 / GB-second after 100,000 free GB-s a month          (centralus)
Not metered here: AI Search (free tier), Azure SQL (free offer), the memory store's own model calls (billed as model
tokens inside Foundry; counted as operations only), Log Analytics ingestion (pennies at this volume).
"""
from __future__ import annotations

import contextvars
import math

PRICES = {
    "model_input_per_1k": 0.0004, "model_cached_input_per_1k": 0.0001, "model_output_per_1k": 0.0016,
    "embedding_per_1k": 0.000022, "content_safety_per_record": 0.375 / 1000,
    "functions_per_gb_s": 0.000026,
}
PRICES_CHECKED = "2026-11-10"
_meter: contextvars.ContextVar["Meter | None"] = contextvars.ContextVar("txfr_meter", default=None)


class Meter:
    def __init__(self):
        self.u = {"agent_prompt": 0, "agent_cached": 0, "agent_completion": 0, "screen_prompt": 0, "screen_cached": 0,
                  "screen_completion": 0, "embedding_tokens": 0, "content_safety_records": 0, "shield_calls": 0,
                  "groundedness_calls": 0, "memory_ops": 0, "function_gb_s": 0.0}

    def add(self, **kw):
        for k, v in kw.items():
            self.u[k] = self.u.get(k, 0) + (v or 0)

    def usd(self) -> dict:
        p, u = PRICES, self.u
        parts = {
            "agent_model": ((u["agent_prompt"] - u["agent_cached"]) * p["model_input_per_1k"]
                            + u["agent_cached"] * p["model_cached_input_per_1k"]
                            + u["agent_completion"] * p["model_output_per_1k"]) / 1000,
            "request_screen": ((u["screen_prompt"] - u["screen_cached"]) * p["model_input_per_1k"]
                               + u["screen_cached"] * p["model_cached_input_per_1k"]
                               + u["screen_completion"] * p["model_output_per_1k"]) / 1000,
            "embeddings": u["embedding_tokens"] * p["embedding_per_1k"] / 1000,
            "content_safety": u["content_safety_records"] * p["content_safety_per_record"],
            "functions": u["function_gb_s"] * p["functions_per_gb_s"],
        }
        parts = {k: round(v, 6) for k, v in parts.items()}
        parts["total"] = round(sum(parts.values()), 6)
        return parts

    def report(self) -> dict:
        return {"usage": dict(self.u), "usd": self.usd(), "prices_checked": PRICES_CHECKED}


def start() -> Meter:
    m = Meter()
    _meter.set(m)
    return m


def current() -> Meter | None:
    return _meter.get()


def add(**kw):
    m = _meter.get()
    if m is not None:
        m.add(**kw)


def records(*texts) -> int:
    """Content Safety text records: each text is billed in 1,000-character units (at least one)."""
    n = 0
    for t in texts:
        if isinstance(t, (list, tuple)):
            n += records(*t)
        elif t:
            n += max(1, math.ceil(len(str(t)) / 1000))
    return n
