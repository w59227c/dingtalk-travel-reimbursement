"""Compact output is lossless; immutable pre-change snapshots remain byte-identical."""

import json
import re
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import load_workbook
from test_attendance_subsidy import attendance_source

from app.core.errors import ApiError
from app.services.excel_generator import generate_expense_workbook
from app.services.oa_reimbursement_payload import (
    build_snapshot,
    parse_snapshot,
    serialize_snapshot,
    snapshot_excel_input,
    snapshot_sha256,
)
from app.services.subsidy_description import attendance_date_lines

FIXTURES = Path(__file__).parent / "fixtures" / "reimbursement_snapshots"


@pytest.mark.parametrize(
    "dates,expected",
    [
        ([], ()),
        (["2026-09-01"], ("出勤日期（2026年9月）：1日",)),
        (["2026-09-01", "2026-09-02"], ("出勤日期（2026年9月）：1-2日",)),
        (
            ["2026-09-05", "2026-09-01", "2026-09-04", "2026-09-02"],
            ("出勤日期（2026年9月）：1-2日、4-5日",),
        ),
        (
            ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-07"],
            ("出勤日期（2026年9月）：30日", "出勤日期（2026年10月）：1-2日、7日"),
        ),
        (
            ["2026-12-31", "2027-01-01"],
            ("出勤日期（2026年12月）：31日", "出勤日期（2027年1月）：1日"),
        ),
        (
            ["2024-02-28", "2024-02-29", "2024-03-01"],
            ("出勤日期（2024年2月）：28-29日", "出勤日期（2024年3月）：1日"),
        ),
        (["0001-01-01", "9999-12-31"], (
            "出勤日期（1年1月）：1日", "出勤日期（9999年12月）：31日",
        )),
    ],
)
def test_monthly_ranges_keep_gaps_month_year_and_calendar_boundaries(dates, expected):
    assert attendance_date_lines(date.fromisoformat(day) for day in dates) == expected


@pytest.mark.parametrize("span", [31, 183, 3660])
def test_expanding_ranges_recovers_every_selected_date_once(span):
    # Include weekends and leave gaps without assuming a working-week calendar.
    first = date(2024, 1, 1)
    selected = [first + timedelta(days=i) for i in range(span) if i % 9 not in (2, 6)]
    expanded = []
    for line in attendance_date_lines(reversed(selected)):
        match = re.fullmatch(r"出勤日期（(\d+)年(\d+)月）：(.+)", line)
        assert match
        year, month = int(match[1]), int(match[2])
        for token in match[3].split("、"):
            endpoints = [int(value) for value in token.removesuffix("日").split("-")]
            for day in range(endpoints[0], endpoints[-1] + 1):
                expanded.append(date(year, month, day))
    assert expanded == selected


def test_leave_gaps_and_weekend_attendance_produce_the_agreed_monthly_example():
    selected = [
        date(2026, 9, day)
        for day in [*range(1, 6), *range(7, 13), *range(14, 19), *range(20, 26), 30]
    ]
    assert len(selected) == 23
    assert attendance_date_lines(selected) == (
        "出勤日期（2026年9月）：1-5日、7-12日、14-18日、20-25日、30日",
    )


def workbook_text(snapshot):
    value = snapshot_excel_input(snapshot)
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
        attendance_description_format=value.attendance_description_format,
    )
    workbook = load_workbook(BytesIO(result.content))
    try:
        return "\n".join(
            str(cell.value)
            for row in workbook.active
            for cell in row
            if cell.value is not None
        )
    finally:
        workbook.close()


def test_pre_change_v11_snapshot_keeps_canonical_json_sha_and_excel_description():
    # Generated with b113f89, before the formatter changed; all identities are synthetic.
    raw = (FIXTURES / "attendance_v11.json").read_text(encoding="utf-8")
    digest = (FIXTURES / "attendance_v11.sha256").read_text().strip()
    restored = parse_snapshot(raw, expected_sha256=digest)
    assert serialize_snapshot(restored) == raw
    assert snapshot_sha256(restored) == digest
    assert restored.attendance_description_format is None
    assert "attendanceDescriptionFormat" not in json.loads(serialize_snapshot(restored))
    expected = "出勤日期：2026-09-01、2026-09-03"
    description = next(
        item.value for item in restored.form_values if item.logical_key == "description"
    )
    assert expected in description
    text = workbook_text(restored)
    assert expected in text and "出勤日期（" not in text
    assert "出勤2天 × 50.00元/天 = 100.00元" in text


def test_frozen_monthly_snapshot_keeps_original_long_dash_and_sha():
    raw = (FIXTURES / "attendance_monthly_v11.json").read_text(encoding="utf-8")
    digest = (FIXTURES / "attendance_monthly_v11.sha256").read_text().strip()
    restored = parse_snapshot(raw, expected_sha256=digest)
    assert restored.attendance_description_format == "monthly_ranges"
    assert serialize_snapshot(restored) == raw
    assert snapshot_sha256(restored) == digest
    expected = "出勤日期（2026年9月）：1—3日"
    assert any(expected in item.value for item in restored.form_values)
    text = workbook_text(restored)
    assert expected in text and "1-3日" not in text
    assert "出勤3天 × 50.00元/天 = 150.00元" in text


def test_new_oa_and_excel_share_frozen_compact_format_and_same_amount():
    snapshot = build_snapshot(attendance_source())
    restored = parse_snapshot(
        serialize_snapshot(snapshot), expected_sha256=snapshot_sha256(snapshot)
    )
    assert restored.attendance_description_format == "monthly_ranges_hyphen"
    expected = "出勤日期（2026年9月）：1日、3日"
    description = next(
        item.value for item in restored.form_values if item.logical_key == "description"
    )
    assert expected in description and expected in workbook_text(restored)
    assert restored.subsidy.total == parse_snapshot(
        (FIXTURES / "attendance_v11.json").read_text(encoding="utf-8")
    ).subsidy.total


@pytest.mark.parametrize("change", ["remove", "unknown", "legacy_marker", "legacy_monthly_marker"])
def test_description_format_tampering_is_rejected(change):
    if change == "legacy_monthly_marker":
        value = json.loads((FIXTURES / "attendance_monthly_v11.json").read_text(encoding="utf-8"))
        value["attendanceDescriptionFormat"] = "monthly_ranges_hyphen"
    elif change == "legacy_marker":
        value = json.loads((FIXTURES / "attendance_v11.json").read_text(encoding="utf-8"))
        value["attendanceDescriptionFormat"] = "monthly_ranges"
    else:
        value = json.loads(serialize_snapshot(build_snapshot(attendance_source())))
        if change == "remove":
            del value["attendanceDescriptionFormat"]
        else:
            value["attendanceDescriptionFormat"] = "unknown"
    with pytest.raises(ApiError):
        parse_snapshot(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
