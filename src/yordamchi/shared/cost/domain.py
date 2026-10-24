"""Usage ledger rules, pure (SPEC §4.2 `usage_records`; UC-07, NFR-COST-01/02). Recording only; reports and caps
are P2 (T51).

Money is integer micro-USD end to end: ports already deliver `cost_usd_micros` (adapters convert the vendor's float
once, `core.money.usd_float_to_micros`), and the ledger stores exactly that. On the Claude plan the amount is the SDK's
API-equivalent estimate (UC-07), so `is_paid_api` — not the amount — separates spend from usage. A `Usage` that
cannot be true — a negative amount, or a paid API call on the Claude plan or Edge TTS — is a bug: `ValueError`.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

USAGE_KEEP: Final = timedelta(days=400)
MODEL_MAX_LEN: Final = 96
SOURCE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,31}")
UNIT_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")


class Provider(StrEnum):
    ANTHROPIC_PLAN = "anthropic_plan"  # the owner's Claude subscription: usage, not money
    GOOGLE = "google"
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    EDGE = "edge"  # free TTS fallback


# Providers that can bill per call; a Google or OpenRouter key on a free tier records is_paid_api=False.
PAYABLE: Final = frozenset({Provider.GOOGLE, Provider.OPENROUTER, Provider.OPENAI})


@dataclass(frozen=True, slots=True)
class Usage:
    """One billable or metered call: a finished turn, an STT or TTS request, a routing ask.

    `source` names the path (`turn`, `routing`, `report`, `stt`, `tts`, `video`, `qa`); `units` are the provider's own
    counters (`input_tokens`, `output_tokens`, `audio_ms`, `characters`).
    """

    source: str
    provider: Provider
    model: str
    is_paid_api: bool
    cost_usd_micros: int
    duration_ms: int
    units: Mapping[str, int] = field(default_factory=dict)
    project_id: str | None = None
    turn_id: int | None = None


def problems(usage: Usage) -> list[str]:
    """Why `usage` cannot be recorded; empty when it can."""
    found: list[str] = []
    if not SOURCE_PATTERN.fullmatch(usage.source):
        found.append("bad_source")
    if not usage.model or len(usage.model) > MODEL_MAX_LEN:
        found.append("bad_model")
    if usage.cost_usd_micros < 0:
        found.append("negative_cost")
    if usage.duration_ms < 0:
        found.append("negative_duration")
    if usage.is_paid_api and usage.provider not in PAYABLE:
        found.append("not_a_paid_api")
    if any(not UNIT_PATTERN.fullmatch(name) or count < 0 for name, count in usage.units.items()):
        found.append("bad_units")
    return found


def require_valid(usage: Usage) -> None:
    found = problems(usage)
    if found:
        msg = f"usage cannot be recorded: {', '.join(found)}"
        raise ValueError(msg)


def retention_cutoff(now: datetime) -> datetime:
    """Records strictly older than this may be deleted."""
    return now - USAGE_KEEP
