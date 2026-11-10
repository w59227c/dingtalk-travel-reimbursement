from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_QUANTUM = Decimal("0.01")
MAX_REIMBURSEMENT_AMOUNT = Decimal("999999999999.99")
MAX_ORIGINAL_AMOUNT = Decimal("999999999999999.99")

_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_SMALL_UNITS = ("", "拾", "佰", "仟")
_GROUP_UNITS = ("", "万", "亿")


def quantize_money(value: Decimal) -> Decimal:
    return _quantize_amount(value, maximum=MAX_REIMBURSEMENT_AMOUNT)


def quantize_original_amount(value: Decimal) -> Decimal:
    return _quantize_amount(value, maximum=MAX_ORIGINAL_AMOUNT)


def _quantize_amount(value: Decimal, *, maximum: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError("money values must be Decimal")
    if not value.is_finite():
        raise ValueError("money values must be finite")
    rounded = value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if rounded < 0:
        raise ValueError("money values must not be negative")
    if rounded > maximum:
        raise ValueError("money value exceeds supported limit")
    return rounded


def money_string(value: Decimal) -> str:
    return format(quantize_money(value), ".2f")


def original_amount_string(value: Decimal) -> str:
    return format(quantize_original_amount(value), ".2f")


def _four_digit_group(value: int) -> str:
    parts: list[str] = []
    pending_zero = False
    for position in range(3, -1, -1):
        divisor = 10**position
        digit = value // divisor % 10
        if digit:
            if pending_zero and parts:
                parts.append(_DIGITS[0])
            parts.append(_DIGITS[digit])
            parts.append(_SMALL_UNITS[position])
            pending_zero = False
        elif parts and value % divisor:
            pending_zero = True
    return "".join(parts)


def _integer_uppercase(value: int) -> str:
    if value == 0:
        return _DIGITS[0]
    groups: list[int] = []
    while value:
        groups.append(value % 10_000)
        value //= 10_000

    parts: list[str] = []
    pending_zero = False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            if parts and any(groups[:index]):
                pending_zero = True
            continue
        if parts and (pending_zero or group < 1_000):
            if parts[-1] != _DIGITS[0]:
                parts.append(_DIGITS[0])
        parts.append(_four_digit_group(group))
        parts.append(_GROUP_UNITS[index])
        pending_zero = False
    return "".join(parts)


def amount_to_chinese_uppercase(value: Decimal) -> str:
    """Convert a non-negative Decimal amount into Chinese financial uppercase."""

    try:
        amount = quantize_money(value)
    except InvalidOperation as exc:
        raise ValueError("invalid money value") from exc
    fen_total = int(amount * 100)
    integer, fraction = divmod(fen_total, 100)
    jiao, fen = divmod(fraction, 10)
    result = f"{_integer_uppercase(integer)}元"
    if jiao == 0 and fen == 0:
        return f"{result}整"
    if jiao:
        result += f"{_DIGITS[jiao]}角"
    elif fen:
        result += _DIGITS[0]
    if fen:
        result += f"{_DIGITS[fen]}分"
    return result
