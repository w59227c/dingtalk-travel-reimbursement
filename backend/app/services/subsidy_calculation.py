from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.subsidy import (
    SubsidyCalculation,
    calculate_subsidy,
    validate_non_overlapping_exclusions,
)
from app.schemas.expenses import TripInput, TripPurpose
from app.services.application_settings import get_expense_settings


def subsidy_purpose_for_travel_type(
    label: str,
    source_label: str | None = None,
) -> TripPurpose | None:
    """Keep the approved long/short category, including older collapsed OA mappings."""

    if source_label:
        source_purpose = subsidy_purpose_for_travel_type(source_label)
        if source_purpose is not None:
            return source_purpose
    normalized = "".join(label.split())
    long_term = "长期" in normalized
    short_term = "短期" in normalized
    if "境外" in normalized:
        if long_term:
            return TripPurpose.LONG_TERM_OVERSEAS
        return TripPurpose.SHORT_TERM_OVERSEAS if short_term else None
    if "同市" in normalized or "市内项目" in normalized:
        return TripPurpose.SAME_CITY_PROJECT
    if "公司内部" in normalized:
        if long_term:
            return TripPurpose.LONG_TERM_INTERNAL
        return TripPurpose.SHORT_TERM_INTERNAL if short_term else None
    if "市外项目" in normalized:
        if long_term:
            return TripPurpose.LONG_TERM_PROJECT
        return TripPurpose.SHORT_TERM_PROJECT if short_term else None
    if "商务" in normalized:
        return TripPurpose.BUSINESS
    return None


def calculate_trip_subsidies(
    database: Session,
    trips: list[TripInput],
) -> list[SubsidyCalculation]:
    """Calculate one authoritative subsidy line for every non-overlapping period."""

    if not trips:
        return []
    for trip in trips:
        if trip.trip_type is TripPurpose.SAME_CITY_PROJECT:
            if trip.attendance_dates is None:
                raise ApiError("ATTENDANCE_DATES_REQUIRED", "请核对并选择市内出差的出勤日期", 422)
            if any(day < trip.start_date or day > trip.end_date for day in trip.attendance_dates):
                raise ApiError("INVALID_ATTENDANCE_DATES", "出勤日期必须位于所属审批日期内", 422)
    settings = get_expense_settings(database)
    results: list[SubsidyCalculation] = []
    for trip in merge_overlapping_subsidy_trips(trips):
        trip_type = trip.subsidy_trip_type()
        excluded_periods = [item.as_period() for item in trip.excluded_periods]
        # Validate after merging approvals: exclusions on different source rows
        # still belong to the same displayed subsidy period. Historical locked
        # snapshots retain their original union calculation in the domain layer.
        if trip.attendance_dates is None:
            validate_non_overlapping_exclusions(excluded_periods, trip.as_period())
        results.append(
            calculate_subsidy(
                trip_type=trip_type,
                period=trip.as_period(),
                configured_daily_rate=settings.daily_rate_for(trip_type),
                policy_confirmed=trip.policy_confirmed,
                confirmed_effective_days=trip.confirmed_effective_days,
                no_subsidy_exception=trip.no_subsidy_exception,
                manual_subsidy_amount=trip.manual_subsidy_amount,
                related_approval_id=trip.related_approval_id,
                excluded_periods=excluded_periods,
                attendance_dates=trip.attendance_dates,
            )
        )
    return results


def merge_overlapping_subsidy_trips(trips: list[TripInput]) -> list[TripInput]:
    """Merge inclusive overlaps without filling gaps or adjacent trips.

    Drafts retain one input per approval for auditing.  This derived list is used
    for calculation and workbook rows so overlapping calendar days are paid once.
    """

    grouped_by_type: dict[TripPurpose, list[TripInput]] = {}
    for trip in trips:
        grouped_by_type.setdefault(trip.trip_type, []).append(trip)

    merged: list[TripInput] = []
    for typed_trips in grouped_by_type.values():
        ordered = sorted(
            typed_trips,
            key=lambda item: (
                item.start_date,
                item.end_date,
                item.related_approval_id or "",
            ),
        )
        groups: list[list[TripInput]] = []
        covered_end = None
        for trip in ordered:
            if not groups or covered_end is None or trip.start_date > covered_end:
                groups.append([trip])
                covered_end = trip.end_date
                continue
            groups[-1].append(trip)
            covered_end = max(covered_end, trip.end_date)

        for group in groups:
            start_date = min(item.start_date for item in group)
            end_date = max(item.end_date for item in group)
            start_time = min(item.start_time for item in group if item.start_date == start_date)
            end_time = max(item.end_time for item in group if item.end_date == end_date)
            first = group[0]
            merged.append(
                first.model_copy(
                    update={
                        "start_date": start_date,
                        "start_time": start_time,
                        "end_date": end_date,
                        "end_time": end_time,
                        "policy_confirmed": all(item.policy_confirmed for item in group),
                        "attendance_dates": sorted(
                            {day for item in group for day in item.attendance_dates or []}
                        )
                        if all(item.attendance_dates is not None for item in group)
                        else None,
                        "excluded_periods": [
                            excluded for item in group for excluded in item.excluded_periods
                        ],
                        "confirmed_effective_days": next(
                            (
                                item.confirmed_effective_days
                                for item in group
                                if item.confirmed_effective_days is not None
                            ),
                            None,
                        ),
                        "no_subsidy_exception": any(item.no_subsidy_exception for item in group),
                        "manual_subsidy_amount": next(
                            (
                                item.manual_subsidy_amount
                                for item in group
                                if item.manual_subsidy_amount is not None
                            ),
                            None,
                        ),
                    }
                )
            )
    return sorted(
        merged,
        key=lambda item: (
            item.start_date,
            item.end_date,
            item.related_approval_id or "",
        ),
    )


def request_trips(*, trip: TripInput | None, trips: list[TripInput]) -> list[TripInput]:
    return trips if trips else ([trip] if trip is not None else [])
