from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from enum import StrEnum

from app.core.errors import ApiError
from app.domain.money import money_string, quantize_money

HALF_DAY = Decimal("0.5")
NOON = time(12, 0)
MAX_DAILY_SUBSIDY = Decimal("10000.00")
MAX_ATTENDANCE_DATES = 3660


class TripType(StrEnum):
    BUSINESS = "business"
    SHORT_TERM_PROJECT = "short_term_project"
    LONG_TERM_PROJECT = "long_term_project"
    SAME_CITY_PROJECT = "same_city_project"
    INTERNAL = "internal"
    OVERSEAS = "overseas"
    SHORT_TERM_INTERNAL = "short_term_internal"
    LONG_TERM_INTERNAL = "long_term_internal"
    SHORT_TERM_OVERSEAS = "short_term_overseas"
    LONG_TERM_OVERSEAS = "long_term_overseas"


@dataclass(frozen=True, slots=True)
class TripPeriod:
    start_date: date
    start_time: time
    end_date: date
    end_time: time


class AdjustmentType(StrEnum):
    TEMPORARY_ABSENCE = "temporary_absence"
    LATE_DEPARTURE = "late_departure"
    EARLY_END = "early_end"


@dataclass(frozen=True, slots=True)
class SubsidyAdjustment:
    adjustment_type: AdjustmentType = AdjustmentType.TEMPORARY_ABSENCE
    start_date: date | None = None
    start_time: time | None = None
    end_date: date | None = None
    end_time: time | None = None


def _adjustment(value: TripPeriod | SubsidyAdjustment) -> SubsidyAdjustment:
    if isinstance(value, SubsidyAdjustment):
        return value
    return SubsidyAdjustment(
        start_date=value.start_date,
        start_time=value.start_time,
        end_date=value.end_date,
        end_time=value.end_time,
    )


def _half_day(day: date | None, clock: time | None) -> int:
    if day is None or clock is None:
        raise ApiError("INVALID_EXCLUDED_PERIOD", "请补齐非补助时段的日期和时段", 422)
    return day.toordinal() * 2 + (clock >= NOON)


def _adjustment_intervals(
    period: TripPeriod,
    excluded_periods: list[TripPeriod | SubsidyAdjustment],
) -> list[tuple[int, int, int, int]]:
    """Return unpaid and occupied half-day intervals for each original input."""
    lower = _half_day(period.start_date, period.start_time)
    upper = _half_day(period.end_date, period.end_time)
    actual_start, actual_end = lower, upper
    adjustments = [_adjustment(item) for item in excluded_periods]
    for kind in (AdjustmentType.LATE_DEPARTURE, AdjustmentType.EARLY_END):
        if sum(item.adjustment_type == kind for item in adjustments) > 1:
            label = "延后出发" if kind == AdjustmentType.LATE_DEPARTURE else "提前结束"
            raise ApiError("DUPLICATE_SUBSIDY_ADJUSTMENT", f"每项补助最多添加一条{label}", 422)
    intervals = []
    for item in adjustments:
        if item.adjustment_type == AdjustmentType.LATE_DEPARTURE:
            end = _half_day(item.end_date, item.end_time)
            actual_start = end
            start, unpaid_start, unpaid_end, occupied_end = lower, lower, end, end
        elif item.adjustment_type == AdjustmentType.EARLY_END:
            start = _half_day(item.start_date, item.start_time)
            actual_end = start
            end, unpaid_start, unpaid_end, occupied_end = upper, start + 1, upper + 1, upper + 1
        else:
            start = _half_day(item.start_date, item.start_time)
            end = _half_day(item.end_date, item.end_time)
            unpaid_start, unpaid_end, occupied_end = start + 1, end, end + 1
        if not lower <= start <= upper or not lower <= end <= upper:
            raise ApiError("INVALID_EXCLUDED_PERIOD", "非补助时段必须在本项出差时段内", 422)
        if start > end:
            raise ApiError("INVALID_EXCLUDED_PERIOD", "非补助时段的结束时间不能早于开始时间", 422)
        intervals.append((unpaid_start, unpaid_end, start, occupied_end))
    if actual_start > actual_end:
        raise ApiError("INVALID_EXCLUDED_PERIOD", "实际出发时间不能晚于提前结束时间", 422)
    return intervals


@dataclass(frozen=True, slots=True)
class SubsidyCalculation:
    trip_type: TripType
    calendar_days: int
    effective_days: Decimal
    daily_rate: Decimal
    total: Decimal
    related_approval_id: str | None = None
    excluded_days: Decimal | None = None

    def as_api_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "tripType": self.trip_type.value,
            "calendarDays": self.calendar_days,
            "effectiveDays": format(self.effective_days, ".1f"),
            "dailyRate": money_string(self.daily_rate),
            "total": money_string(self.total),
        }
        if self.related_approval_id is not None:
            data["relatedApprovalId"] = self.related_approval_id
        if self.excluded_days is not None:
            data["excludedDays"] = format(self.excluded_days, ".1f")
        return data


def validate_non_overlapping_exclusions(
    excluded_periods: list[TripPeriod | SubsidyAdjustment],
    period: TripPeriod,
) -> None:
    """Each selected morning or afternoon may belong to only one personal absence."""
    ordered = sorted(
        (start, end, index)
        for index, (_, _, start, end) in enumerate(
            _adjustment_intervals(period, excluded_periods), start=1
        )
        if start < end
    )
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current[0] < previous[1]:
            first, second = sorted((previous[2], current[2]))
            raise ApiError(
                "OVERLAPPING_EXCLUDED_PERIODS",
                f"非补助时段 {first} 与时段 {second} 重叠，请调整日期或出发时段",
                422,
            )


def excluded_effective_days(
    period: TripPeriod,
    excluded_periods: list[TripPeriod | SubsidyAdjustment],
) -> Decimal:
    """Union unpaid half-days, clipped to the original paid half-days.

    Leaving for home is a return leg (AM pays half, PM pays all). Leaving
    home for work is a departure leg (AM pays all, PM pays half).
    Integer half-day intervals avoid double deductions across overlapping inputs.
    """
    automatic_effective_days(period)
    paid_start = period.start_date.toordinal() * 2 + (period.start_time >= NOON)
    paid_end = period.end_date.toordinal() * 2 + 1 + (period.end_time >= NOON)
    intervals: list[tuple[int, int]] = []
    for start, end, _, _ in _adjustment_intervals(period, excluded_periods):
        start, end = max(start, paid_start), min(end, paid_end)
        if start < end:
            intervals.append((start, end))
    covered_end = 0
    half_days = 0
    for start, end in sorted(intervals):
        half_days += max(0, end - max(start, covered_end))
        covered_end = max(covered_end, end)
    return Decimal(half_days) * HALF_DAY


def automatic_effective_days(period: TripPeriod) -> Decimal:
    if period.end_date < period.start_date or (
        period.end_date == period.start_date and period.end_time < period.start_time
    ):
        raise ApiError("INVALID_DATE_RANGE", "结束时间不能早于开始时间", 400)

    if period.start_date == period.end_date:
        crosses_noon = period.start_time < NOON <= period.end_time
        return Decimal("1.0") if crosses_noon else HALF_DAY

    date_difference = (period.end_date - period.start_date).days
    middle_days = max(date_difference - 1, 0)
    departure = Decimal("1.0") if period.start_time < NOON else HALF_DAY
    returned = HALF_DAY if period.end_time < NOON else Decimal("1.0")
    return departure + Decimal(middle_days) + returned


def calculate_subsidy(
    *,
    trip_type: TripType,
    period: TripPeriod,
    configured_daily_rate: Decimal,
    policy_confirmed: bool = False,
    confirmed_effective_days: Decimal | None = None,
    no_subsidy_exception: bool = False,
    manual_subsidy_amount: Decimal | None = None,
    related_approval_id: str | None = None,
    excluded_periods: list[TripPeriod | SubsidyAdjustment] | None = None,
    attendance_dates: list[date] | None = None,
) -> SubsidyCalculation:
    if attendance_dates is not None:
        if trip_type is not TripType.SAME_CITY_PROJECT:
            raise ApiError("UNEXPECTED_ATTENDANCE_DATES", "仅市内出差按出勤日期计算", 422)
        if (
            manual_subsidy_amount is not None
            or confirmed_effective_days is not None
            or no_subsidy_exception
        ):
            raise ApiError("UNEXPECTED_POLICY_OVERRIDE", "出勤补助由日期和每日标准计算", 422)
        lower, upper = period.start_date, period.end_date
        seen: set[AdjustmentType] = set()
        for raw in excluded_periods or []:
            item = _adjustment(raw)
            if item.adjustment_type is AdjustmentType.TEMPORARY_ABSENCE:
                raise ApiError(
                    "ATTENDANCE_LEGACY_PERIODS", "请核对并删除旧非补助时段，改用出勤日期", 422
                )
            if item.adjustment_type in seen:
                raise ApiError("DUPLICATE_SUBSIDY_ADJUSTMENT", "补助起止调整不能重复", 422)
            seen.add(item.adjustment_type)
            boundary = (
                item.end_date
                if item.adjustment_type is AdjustmentType.LATE_DEPARTURE
                else item.start_date
            )
            if boundary is None or not period.start_date <= boundary <= period.end_date:
                raise ApiError("INVALID_EXCLUDED_PERIOD", "补助日期必须在审批日期范围内", 422)
            if item.adjustment_type is AdjustmentType.LATE_DEPARTURE:
                lower = boundary
            else:
                upper = boundary
        if lower > upper:
            raise ApiError("INVALID_DATE_RANGE", "补助开始日期不能晚于结束日期", 422)
        if (upper - lower).days >= MAX_ATTENDANCE_DATES:
            raise ApiError("INVALID_ATTENDANCE_DATES", "出勤范围过长，请缩小补助日期范围", 422)
        if len(attendance_dates) > MAX_ATTENDANCE_DATES or len(set(attendance_dates)) != len(
            attendance_dates
        ):
            raise ApiError("INVALID_ATTENDANCE_DATES", "出勤日期重复或数量超出限制", 422)
        if any(day < lower or day > upper for day in attendance_dates):
            raise ApiError("INVALID_ATTENDANCE_DATES", "出勤日期必须在当前补助日期范围内", 422)
        days = Decimal(len(attendance_dates))
        rate = quantize_money(configured_daily_rate)
        return SubsidyCalculation(
            trip_type=trip_type,
            calendar_days=(period.end_date - period.start_date).days + 1,
            effective_days=days,
            daily_rate=rate,
            total=quantize_money(days * rate),
            related_approval_id=related_approval_id,
        )
    calculated_days = automatic_effective_days(period)
    calendar_days = (period.end_date - period.start_date).days + 1
    excluded_days = excluded_effective_days(period, excluded_periods or [])
    calculated_days -= excluded_days

    if manual_subsidy_amount is not None:
        raise ApiError("UNEXPECTED_POLICY_OVERRIDE", "出差补助由管理员设置的每日标准计算", 422)

    if trip_type is TripType.SAME_CITY_PROJECT:
        # Keep accepting the legacy acknowledgement field in drafts and snapshots.
        if confirmed_effective_days is not None or no_subsidy_exception:
            raise ApiError(
                "UNEXPECTED_POLICY_OVERRIDE",
                "有效天数由系统按出发和返回时段自动计算",
                422,
            )
        effective_days = calculated_days
        daily_rate = quantize_money(configured_daily_rate)
    else:
        if policy_confirmed or confirmed_effective_days is not None or no_subsidy_exception:
            raise ApiError(
                "UNEXPECTED_POLICY_OVERRIDE",
                "该出差类型由系统自动计算，不能覆盖天数或标准",
                422,
            )
        effective_days = calculated_days
        daily_rate = quantize_money(configured_daily_rate)

    total = quantize_money(effective_days * daily_rate)
    return SubsidyCalculation(
        trip_type=trip_type,
        calendar_days=calendar_days,
        effective_days=effective_days,
        daily_rate=daily_rate,
        total=total,
        related_approval_id=related_approval_id,
        excluded_days=excluded_days if excluded_periods else None,
    )
