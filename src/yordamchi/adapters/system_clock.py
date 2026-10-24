"""`SystemClock`: the production `Clock` — the only `datetime.now` in the codebase (CONVENTIONS §3)."""

import asyncio
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
