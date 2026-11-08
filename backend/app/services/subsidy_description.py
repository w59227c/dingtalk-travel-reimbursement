"""One authoritative adjustment explanation for OA and printable workbook rows."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import groupby
from typing import Literal

from app.domain.money import money_string, quantize_money
from app.domain.subsidy import (
    AdjustmentType,
    SubsidyCalculation,
    automatic_effective_days,
    effective_subsidy_periods,
    excluded_effective_days,
)
from app.schemas.expenses import TripInput

MonthlyAttendanceDescriptionFormat = Literal["monthly_ranges", "monthly_ranges_hyphen"]
AttendanceDescriptionFormat = Literal["individual_dates", MonthlyAttendanceDescriptionFormat]
CURRENT_ATTENDANCE_DESCRIPTION_FORMAT: MonthlyAttendanceDescriptionFormat = "monthly_ranges_hyphen"
SubsidyDescriptionFormat = Literal["compact_effective_periods"]
CURRENT_SUBSIDY_DESCRIPTION_FORMAT: SubsidyDescriptionFormat = "compact_effective_periods"


def attendance_date_lines(
    dates: Iterable[date], *, separator: Literal["-", "—"] = "-", include_label: bool = True
) -> tuple[str, ...]:
    """Losslessly shorten actual attendance dates, keeping each year/month explicit."""
    lines = []
    for (year, month), days in groupby(sorted(dates), key=lambda day: (day.year, day.month)):
        ranges: list[tuple[date, date]] = []
        for day in days:
            if ranges and day.toordinal() == ranges[-1][1].toordinal() + 1:
                ranges[-1] = (ranges[-1][0], day)
            else:
                ranges.append((day, day))
        labels = (
            f"{start.day}{separator}{end.day}日" if start != end else f"{start.day}日"
            for start, end in ranges
        )
        prefix = f"出勤日期（{year}年{month}月）：" if include_label else f"{year}年{month}月："
        lines.append(prefix + "、".join(labels))
    return tuple(lines)


def compact_days(value) -> str:
    return str(int(value)) if value == value.to_integral_value() else format(value.normalize(), "f")


def _point(day, clock) -> str:
    slot = "上午" if clock.hour < 12 else "下午"
    return f"{day.year}/{day.month}/{day.day}{slot}"


def _date(day: date) -> str:
    return f"{day.year}/{day.month}/{day.day}"


@dataclass(frozen=True, slots=True)
class SubsidyPresentation:
    first_date: date
    last_date: date
    effective_days: Decimal
    total: Decimal
    excel_lines: tuple[str, ...]

    @property
    def oa_description(self) -> str:
        return (
            f"{_date(self.first_date)}—{_date(self.last_date)}，"
            f"计补{compact_days(self.effective_days)}天"
        )


def summarize_subsidies(
    trips: Iterable[TripInput], calculations: Iterable[SubsidyCalculation],
) -> SubsidyPresentation | None:
    """Present already-calculated, overlap-merged subsidies without changing their money."""
    periods = []
    attendance: list[date] = []
    days_by_rate: dict[Decimal, Decimal] = {}
    amounts: list[SubsidyCalculation] = []
    total = Decimal(0)
    for trip, result in zip(trips, calculations, strict=True):
        if result.effective_days == 0:
            continue
        if trip.attendance_dates is not None:
            if Decimal(len(trip.attendance_dates)) != result.effective_days:
                raise ValueError("attendance days mismatch")
            attendance.extend(trip.attendance_dates)
        else:
            paid = effective_subsidy_periods(
                trip.as_period(), [item.as_period() for item in trip.excluded_periods]
            )
            if sum((automatic_effective_days(item) for item in paid), Decimal(0)) != (
                result.effective_days
            ):
                raise ValueError("effective subsidy periods mismatch")
            periods.extend(paid)
        days_by_rate[result.daily_rate] = (
            days_by_rate.get(result.daily_rate, Decimal(0)) + result.effective_days
        )
        total += result.total
        amounts.append(result)
    if not days_by_rate:
        return None
    bounds = attendance + [day for item in periods for day in (item.start_date, item.end_date)]
    first, last = min(bounds), max(bounds)
    include_year = first.year != last.year

    def point(day, clock):
        label = _point(day, clock)
        return label if include_year else label.split("/", 1)[1]

    lines = [
        f"{point(item.start_date, item.start_time)}—{point(item.end_date, item.end_time)}，"
        f"{compact_days(automatic_effective_days(item))}天"
        for item in sorted(periods, key=lambda item: (item.start_date, item.start_time))
    ]
    lines.extend(attendance_date_lines(attendance, include_label=False))
    formula = " + ".join(
        f"{compact_days(days)}天 × {compact_days(rate)}元/天"
        for rate, days in sorted(days_by_rate.items())
    )
    formula_total = sum(
        (quantize_money(days * rate) for rate, days in days_by_rate.items()), Decimal(0)
    )
    if formula_total == total:
        lines.append(f"{formula} = {compact_days(total)}元")
    else:
        # Half-day amounts with cent-valued rates round per calculated approval.
        # Keep the frozen accounting total instead of implying aggregation changes it.
        lines.extend(
            f"{compact_days(item.effective_days)}天 × {compact_days(item.daily_rate)}元/天"
            f" = {compact_days(item.total)}元"
            for item in amounts
        )
        lines.append(" + ".join(f"{compact_days(item.total)}元" for item in amounts)
                     + f" = {compact_days(total)}元")
    return SubsidyPresentation(first, last, sum(days_by_rate.values()), total, tuple(lines))


def subsidy_explanation(
    trip: TripInput,
    result: SubsidyCalculation,
    *,
    attendance_format: AttendanceDescriptionFormat = CURRENT_ATTENDANCE_DESCRIPTION_FORMAT,
) -> tuple[str, ...]:
    """Summary first, then one complete non-financial line for every adjustment."""
    if trip.attendance_dates is not None:
        lines = [
            f"市内出差补助：出勤{compact_days(result.effective_days)}天 × "
            f"{money_string(result.daily_rate)}元/天 = {money_string(result.total)}元"
        ]
        if attendance_format in {"monthly_ranges", "monthly_ranges_hyphen"}:
            separator = "—" if attendance_format == "monthly_ranges" else "-"
            lines.extend(attendance_date_lines(trip.attendance_dates, separator=separator))
        else:
            # Preserve the exact pre-format-marker text for immutable snapshots.
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
