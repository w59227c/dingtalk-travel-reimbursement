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

    @property
    def requires_confirmation(self) -> bool:
        return self is TripType.SAME_CITY_PROJECT


@dataclass(frozen=True, slots=True)
class TripPeriod:
    start_date: date
    start_time: time
    end_date: date
    end_time: time


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


def validate_non_overlapping_exclusions(excluded_periods: list[TripPeriod]) -> None:
    """Each selected morning or afternoon may belong to only one personal absence."""
    ordered = sorted(
        (
            (item.start_date, item.start_time >= NOON),
            (item.end_date, item.end_time >= NOON),
            index,
        )
        for index, item in enumerate(excluded_periods, start=1)
    )
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current[0] <= previous[1]:
            first, second = sorted((previous[2], current[2]))
            raise ApiError(
                "OVERLAPPING_EXCLUDED_PERIODS",
                f"非补助时段 {first} 与时段 {second} 重叠，请调整日期或出发时段",
                422,
            )


def excluded_effective_days(period: TripPeriod, excluded_periods: list[TripPeriod]) -> Decimal:
    """Union unpaid half-days, clipped to the original paid half-days.

    Leaving for home is a return leg (AM pays half, PM pays all). Leaving
    home for work is a departure leg (AM pays all, PM pays half).
    Integer half-day intervals avoid double deductions across overlapping inputs.
    """
    automatic_effective_days(period)
    paid_start = period.start_date.toordinal() * 2 + (period.start_time >= NOON)
    paid_end = period.end_date.toordinal() * 2 + 1 + (period.end_time >= NOON)
    intervals: list[tuple[int, int]] = []
    for excluded in excluded_periods:
        automatic_effective_days(excluded)
        if (excluded.start_date, excluded.start_time) < (period.start_date, period.start_time) or (
            excluded.end_date,
            excluded.end_time,
        ) > (period.end_date, period.end_time):
            raise ApiError("INVALID_EXCLUDED_PERIOD", "非补助时段必须在本项出差时段内", 422)
        start = excluded.start_date.toordinal() * 2 + 1 + (excluded.start_time >= NOON)
        end = excluded.end_date.toordinal() * 2 + (excluded.end_time >= NOON)
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
    excluded_periods: list[TripPeriod] | None = None,
) -> SubsidyCalculation:
    calculated_days = automatic_effective_days(period)
    calendar_days = (period.end_date - period.start_date).days + 1
    excluded_days = excluded_effective_days(period, excluded_periods or [])
    calculated_days -= excluded_days

    if manual_subsidy_amount is not None:
        raise ApiError("UNEXPECTED_POLICY_OVERRIDE", "出差补助由管理员设置的每日标准计算", 422)

    if trip_type.requires_confirmation:
        if not policy_confirmed:
            raise ApiError(
                "POLICY_CONFIRMATION_REQUIRED",
                "境内同市项目需要勾选已按公司制度确认",
                422,
            )
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
