from datetime import date
from decimal import Decimal

import pytest

from app.domain.subsidy import (
    automatic_effective_days,
    calculate_subsidy,
    effective_subsidy_periods,
)
from app.schemas.expenses import TripInput
from app.services.subsidy_description import summarize_subsidies


def trip(**changes):
    return TripInput.model_validate(
        {
            "tripType": "business",
            "startDate": "2026-07-01",
            "startTime": "09:00",
            "endDate": "2026-07-10",
            "endTime": "18:00",
            **changes,
        }
    )


def calculation(value, rate="100"):
    return calculate_subsidy(
        trip_type=value.subsidy_trip_type(),
        period=value.as_period(),
        configured_daily_rate=Decimal(rate),
        attendance_dates=value.attendance_dates,
        excluded_periods=[item.as_period() for item in value.excluded_periods],
    )


@pytest.mark.parametrize(
    "changes,expected",
    [
        (
            {"startDate": "2026-11-02", "endDate": "2026-11-04"},
            (
                "11/2上午—11/4下午，3天",
                "3天 × 100元/天 = 300元",
            ),
        ),
        (
            {
                "excludedPeriods": [
                    {"type": "late_departure", "endDate": "2026-07-02", "endTime": "18:00"},
                    {"type": "early_end", "startDate": "2026-07-09", "startTime": "09:00"},
                ]
            },
            ("7/2下午—7/9上午，7天", "7天 × 100元/天 = 700元"),
        ),
        (
            {
                "excludedPeriods": [
                    {
                        "startDate": "2026-07-04",
                        "startTime": "09:00",
                        "endDate": "2026-07-06",
                        "endTime": "18:00",
                    },
                ]
            },
            ("7/1上午—7/4上午，3.5天", "7/6下午—7/10下午，4.5天", "8天 × 100元/天 = 800元"),
        ),
        (
            {"startDate": "2026-11-02", "endDate": "2026-11-02", "endTime": "09:00"},
            (
                "11/2上午—11/2上午，0.5天",
                "0.5天 × 100元/天 = 50元",
            ),
        ),
    ],
)
def test_compact_excel_lists_only_effective_periods_and_one_formula(changes, expected):
    value = trip(**changes)
    result = calculation(value)
    summary = summarize_subsidies([value], [result])
    assert summary is not None
    assert summary.excel_lines == expected
    assert summary.effective_days == result.effective_days
    periods = effective_subsidy_periods(
        value.as_period(), [p.as_period() for p in value.excluded_periods]
    )
    assert sum((automatic_effective_days(p) for p in periods), Decimal(0)) == result.effective_days


def test_two_approvals_across_years_have_one_last_date_and_total():
    values = [
        trip(startDate="2026-12-30", endDate="2026-12-31"),
        trip(startDate="2027-01-04", endDate="2027-01-05"),
    ]
    summary = summarize_subsidies(values, [calculation(value) for value in values])
    assert summary.first_date == date(2026, 12, 30)
    assert summary.last_date == date(2027, 1, 5)
    assert summary.oa_description == "2026/12/30—2027/1/5，计补4天"
    assert summary.total == Decimal("400.00")
    assert summary.excel_lines == (
        "2026/12/30上午—2026/12/31下午，2天",
        "2027/1/4上午—2027/1/5下午，2天",
        "4天 × 100元/天 = 400元",
    )


def test_city_attendance_keeps_short_monthly_dates_and_uses_last_attendance_day():
    value = trip(
        tripType="same_city_project",
        startDate="2026-09-28",
        endDate="2026-10-10",
        attendanceDates=[
            "2026-09-28",
            "2026-09-29",
            "2026-09-30",
            "2026-10-01",
            "2026-10-02",
            "2026-10-07",
        ],
    )
    summary = summarize_subsidies([value], [calculation(value, "50")])
    assert summary.last_date == date(2026, 10, 7)
    assert summary.oa_description == "2026/9/28—2026/10/7，计补6天"
    assert summary.excel_lines == (
        "2026年9月：28-30日",
        "2026年10月：1-2日、7日",
        "6天 × 50元/天 = 300元",
    )


def test_city_zero_attendance_has_no_occurrence_date_or_subsidy_row():
    value = trip(tripType="same_city_project", attendanceDates=[])
    assert summarize_subsidies([value], [calculation(value, "50")]) is None


def test_formula_keeps_decimal_rates_and_does_not_double_count_the_amount():
    value = trip(startDate="2026-11-02", endDate="2026-11-02", endTime="09:00")
    summary = summarize_subsidies([value], [calculation(value, "100.50")])
    assert summary.excel_lines[-1] == "0.5天 × 100.5元/天 = 50.25元"
    assert summary.total == Decimal("50.25")


def test_formula_preserves_per_approval_rounding_for_half_days_with_odd_cent_rates():
    values = [
        trip(startDate="2026-11-02", endDate="2026-11-02", endTime="09:00"),
        trip(startDate="2026-11-04", endDate="2026-11-04", endTime="09:00"),
    ]
    summary = summarize_subsidies(values, [calculation(value, "1.01") for value in values])
    assert summary.total == Decimal("1.02")
    assert summary.excel_lines[-3:] == (
        "0.5天 × 1.01元/天 = 0.51元",
        "0.5天 × 1.01元/天 = 0.51元",
        "0.51元 + 0.51元 = 1.02元",
    )


def source_with_trips(values, rate="100"):
    """Synthetic source using the same overlap projection as preview and submission."""
    from dataclasses import replace

    from test_attendance_subsidy import attendance_source
    from test_oa_reimbursement_payload import _source

    from app.domain.expenses import calculate_expense_totals
    from app.services.subsidy_calculation import merge_overlapping_subsidy_trips

    city = values[0].trip_type.value == "same_city_project"
    source = attendance_source() if city else _source()
    values = [
        value.model_copy(
            update={
                "trip_type": source.draft_input.trip.trip_type,
                "related_approval_id": f"synthetic-approval-{index}",
            }
        )
        for index, value in enumerate(values)
    ]
    merged = merge_overlapping_subsidy_trips(values)
    results = [
        calculate_subsidy(
            trip_type=value.subsidy_trip_type(),
            period=value.as_period(),
            configured_daily_rate=Decimal(rate),
            related_approval_id=value.related_approval_id,
            attendance_dates=value.attendance_dates,
            excluded_periods=[item.as_period() for item in value.excluded_periods],
        )
        for value in merged
    ]
    totals = calculate_expense_totals([], results).as_api_dict()
    totals["subsidies"] = [result.as_api_dict() for result in results]
    return replace(
        source,
        draft_input=source.draft_input.model_copy(
            update={
                "trip": None,
                "trips": values,
                "items": [],
                "dismissed_ocr_file_ids": [],
            }
        ),
        related_approvals=tuple(
            replace(
                source.related_approvals[0],
                sort_order=index,
                process_instance_id=value.related_approval_id,
                start_date=value.start_date,
                end_date=value.end_date,
            )
            for index, value in enumerate(values)
        ),
        original_files=(),
        totals_data=totals,
    )


@pytest.mark.parametrize("overlap", [False, True])
def test_snapshot_and_excel_aggregate_approvals_at_last_effective_day(overlap):
    from io import BytesIO
    from pathlib import Path

    from openpyxl import load_workbook

    from app.services.excel_generator import generate_expense_workbook
    from app.services.oa_reimbursement_payload import (
        build_snapshot,
        parse_snapshot,
        serialize_snapshot,
        snapshot_excel_input,
        snapshot_sha256,
    )

    values = [
        trip(startDate="2026-12-30", endDate="2026-12-31"),
        trip(
            startDate="2026-12-31" if overlap else "2027-01-04",
            endDate="2027-01-05",
            excludedPeriods=[
                {"type": "early_end", "startDate": "2027-01-04", "startTime": "09:00"}
            ],
        ),
    ]
    source = source_with_trips(values)
    snapshot = build_snapshot(source)
    restored = parse_snapshot(
        serialize_snapshot(snapshot), expected_sha256=snapshot_sha256(snapshot)
    )
    assert restored.subsidy_description_format == "compact_effective_periods"
    assert restored.travel_period.end_date == date(2027, 1, 5)
    text = next(item.value for item in restored.form_values if item.logical_key == "description")
    assert "2027-01-04 出差补助 2026/12/30—2027/1/4，计补" in text
    assert text.count("出差补助") == 1
    value = snapshot_excel_input(restored)
    result = generate_expense_workbook(
        template_path=Path(__file__).parents[1] / "app/templates/expense_template.xlsx",
        employee_name=value.employee_name,
        department_name=value.department_name,
        project=value.project,
        trip=value.trip,
        trips=value.trips,
        items=list(value.items),
        subsidy=value.subsidy,
        subsidies=value.subsidies,
        totals=value.totals,
        detailed_subsidies=value.detailed_subsidies,
        compact_subsidies=value.compact_subsidies,
    )
    workbook = load_workbook(BytesIO(result.content))
    try:
        sheet = workbook.active
        assert sheet["B4"].value == "出差补助"
        assert sheet["C4"].value == "2027/1/4"
        assert sheet["G4"].value == sheet["G55"].value == (550 if overlap else 250)
        assert sheet["I4"].value == sheet["I55"].value == 0
        assert "2027/1/4上午" in sheet["D4"].value
        assert sheet["D4"].value.endswith("550元" if overlap else "250元")
        assert sheet["B5"].value is None and sheet["G5"].value is None
    finally:
        workbook.close()


@pytest.mark.parametrize("change", ["remove", "unknown", "old_version", "oa_text"])
def test_compact_snapshot_rejects_format_and_oa_text_tampering(change):
    import json

    from app.core.errors import ApiError
    from app.services.oa_reimbursement_payload import (
        build_snapshot,
        parse_snapshot,
        serialize_snapshot,
    )

    value = json.loads(serialize_snapshot(build_snapshot(source_with_trips([trip()]))))
    if change == "remove":
        del value["subsidyDescriptionFormat"]
    elif change == "unknown":
        value["subsidyDescriptionFormat"] = "unknown"
    elif change == "old_version":
        value["snapshotVersion"] = 10
    else:
        entry = next(item for item in value["formValues"] if item["logicalKey"] == "description")
        entry["value"] = entry["value"].replace("计补10天", "计补9天")
    with pytest.raises(ApiError):
        parse_snapshot(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
