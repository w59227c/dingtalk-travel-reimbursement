"""One authoritative adjustment explanation for OA and printable workbook rows."""

from decimal import Decimal

from app.domain.money import money_string
from app.domain.subsidy import (
    AdjustmentType,
    SubsidyCalculation,
    excluded_effective_days,
)
from app.schemas.expenses import TripInput


def compact_days(value) -> str:
    return str(int(value)) if value == value.to_integral_value() else format(value.normalize(), "f")


def _point(day, clock) -> str:
    slot = "上午" if clock.hour < 12 else "下午"
    return f"{day.year}/{day.month}/{day.day}{slot}"


def subsidy_explanation(trip: TripInput, result: SubsidyCalculation) -> tuple[str, ...]:
    """Summary first, then one complete non-financial line for every adjustment."""
    if trip.attendance_dates is not None:
        lines = [
            f"市内出差补助：出勤{compact_days(result.effective_days)}天 × "
            f"{money_string(result.daily_rate)}元/天 = {money_string(result.total)}元"
        ]
        months: dict[str, list[str]] = {}
        for day in sorted(trip.attendance_dates):
            months.setdefault(day.strftime("%Y-%m"), []).append(day.isoformat())
        lines.extend("出勤日期：" + "、".join(days) for days in months.values())
        return tuple(lines)
    original = result.effective_days + (result.excluded_days or 0)
    lines = [
        f"出差补助：审批期间{_point(trip.start_date, trip.start_time)}—"
        f"{_point(trip.end_date, trip.end_time)}；原补助{compact_days(original)}天，"
        f"扣除{compact_days(result.excluded_days or Decimal(0))}天，实际补助"
        f"{compact_days(result.effective_days)}天 × {money_string(result.daily_rate)}元/天"
        f" = {money_string(result.total)}元"
    ]
    for excluded in trip.excluded_periods:
        kind = excluded.adjustment_type
        if kind == AdjustmentType.LATE_DEPARTURE:
            detail = f"延后出发：{_point(excluded.end_date, excluded.end_time)}，此前不计补助"
        elif kind == AdjustmentType.EARLY_END:
            detail = f"提前结束：{_point(excluded.start_date, excluded.start_time)}，不再返回"
        else:
            detail = f"中途离开：{_point(excluded.start_date, excluded.start_time)}出发，"
            detail += f"{_point(excluded.end_date, excluded.end_time)}出发返回"
        deducted = excluded_effective_days(trip.as_period(), [excluded.as_period()])
        lines.append(f"{detail}（扣除{compact_days(deducted)}天）")
    return tuple(lines)
