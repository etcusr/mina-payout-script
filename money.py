"""Exact MINA accounting. Network and persisted amounts use integer nanomina."""
from decimal import Decimal, localcontext

NANOMINA = 1_000_000_000


def parse_decimal(value):
    """Parse an exact finite quantity; binary floats and bools are not money."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise TypeError('money must be a string, integer or Decimal')
    try:
        result = Decimal(value)
    except (ValueError, ArithmeticError) as exc:
        raise ValueError('invalid decimal amount') from exc
    if not result.is_finite():
        raise ValueError('money must be finite')
    return result


def mina_to_nano(value, *, allow_negative=False, rounding=None):
    """Convert MINA exactly; sub-nano values need an explicit Decimal rounding rule."""
    amount = parse_decimal(value)
    if amount < 0 and not allow_negative:
        raise ValueError('amount must not be negative')
    with localcontext() as ctx:
        ctx.prec = max(50, len(amount.as_tuple().digits) + abs(amount.as_tuple().exponent) + 12)
        scaled = amount * NANOMINA
        integral = scaled.to_integral_value(rounding=rounding)
        if rounding is None and integral != scaled:
            raise ValueError('amount has fractional nanomina')
        return int(integral)


def nano_to_mina(value):
    """Convert integer nanomina to a Decimal without context-dependent rounding."""
    if type(value) is not int:
        raise TypeError('nanomina must be an integer')
    return Decimal(f'{"-" if value < 0 else ""}{abs(value) // NANOMINA}.{abs(value) % NANOMINA:09d}')
