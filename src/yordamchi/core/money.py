"""Money: `Decimal` in code, integer micro-USD in the DB (`*_usd_micros`), never a float (CONVENTIONS §3).

SDKs report cost as a float (`ResultMessage.total_cost_usd`); adapters convert it once with `usd_float_to_micros`.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

MICROS_PER_USD: Final = 1_000_000
_MICROS: Final = Decimal(MICROS_PER_USD)
_ONE: Final = Decimal(1)


def usd_to_micros(amount: Decimal) -> int:
    """Round half-up to a whole micro-dollar."""
    return int((amount * _MICROS).quantize(_ONE, rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> Decimal:
    return Decimal(micros) / _MICROS


def usd_float_to_micros(amount: float) -> int:
    """For adapters: a vendor's float USD, via its shortest repr, so 0.1 stays 100000 micros."""
    return usd_to_micros(Decimal(repr(amount)))


def format_usd(micros: int, *, places: int = 2) -> str:
    """`$1.23` for display; rounds half-up."""
    quantum = Decimal(1).scaleb(-places)
    return f"${micros_to_usd(micros).quantize(quantum, rounding=ROUND_HALF_UP)}"
