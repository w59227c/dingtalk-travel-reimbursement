"""2017–2026 official notice regressions, with an independent date oracle.

Fixtures are public government text, not employee data. Runtime code neither
loads these fixtures nor downloads notices; it still parses only pasted text.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from conftest import mock_login

from app.services.holiday_notice_parser import parse_holiday_notice

ROOT = Path(__file__).parent / "fixtures" / "holiday_notices"
CASES = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))


def full_date(value: str, year: int) -> date:
    return date.fromisoformat(value if len(value) == 10 else f"{year}-{value}")


def expected_sections(case: dict) -> list[dict]:
    output = []
    for section in case["sections"]:
        rest = []
        for span in section["restRanges"]:
            endpoints = span.split("/")
            start = full_date(endpoints[0], case["year"])
            end = full_date(endpoints[-1], case["year"])
            rest.extend(
                date.fromordinal(day).isoformat()
                for day in range(start.toordinal(), end.toordinal() + 1)
            )
        output.append(
            {
                "name": section["name"],
                "restDates": sorted(rest),
                "workDates": sorted(
                    full_date(day, case["year"]).isoformat() for day in section["workDates"]
                ),
            }
        )
    return output


def expected_days(case: dict) -> list[dict]:
    return sorted(
        [
            {"date": day, "working": working, "name": section["name"]}
            for section in expected_sections(case)
            for working, key in ((False, "restDates"), (True, "workDates"))
            for day in section[key]
        ],
        key=lambda day: day["date"],
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["year"]))
def test_official_notice_all_sections_exact_dates_and_labels(case):
    text = (ROOT / f"{case['year']}.txt").read_text(encoding="utf-8")
    parsed = parse_holiday_notice(text)
    assert parsed.can_save, {"source": case["source"], "errors": parsed.errors}
    assert parsed.year == case["year"]
    assert [
        {key: item[key] for key in ("name", "restDates", "workDates")} for item in parsed.sections
    ] == expected_sections(case)
    assert [day.as_dict() for day in parsed.days] == expected_days(case)
    assert sum(not day.working for day in parsed.days) == case["restCount"]
    assert sum(day.working for day in parsed.days) == case["workCount"]
    # The 2024 recommendation about New Year's Eve is not mandatory holiday.
    assert bool(parsed.warnings) is (case["year"] == 2024)


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["year"]))
def test_copied_whitespace_metadata_and_duplicate_title_do_not_change_dates(case):
    text = (ROOT / f"{case['year']}.txt").read_text(encoding="utf-8")
    title = text.splitlines()[0]
    copied = f"发布日期：{case['year'] - 1}年11月\n{title}\n" + text.replace("月", " 月 ").replace(
        "日", "日\n"
    )
    assert [day.as_dict() for day in parse_holiday_notice(copied).days] == expected_days(case)
    assert parse_holiday_notice(copied).can_save


@pytest.mark.parametrize(
    "year,before,after",
    [
        (2017, "1月2日（星期一）补休", "1月2日（星期二）补休"),
        (2017, "1月2日（星期一）补休", "1月1日（星期日）补休"),
        (2017, "1月2日（星期一）补休", "1月2日（星期一）不补休"),
        (2025, "农历除夕，周二", "农历除夕，周一"),
        (2025, "农历除夕", "农历另行安排"),
    ],
)
def test_compensatory_rest_and_lunar_support_keep_strict_validation(year, before, after):
    text = (ROOT / f"{year}.txt").read_text(encoding="utf-8")
    assert before in text
    assert not parse_holiday_notice(text.replace(before, after)).can_save


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["year"]))
def test_official_notice_parse_and_save_preserve_exact_annual_and_adjacent_dates(
    client_factory, case
):
    client = client_factory(auth_mock_enabled=True, admin_user_ids="mock-user")
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    text = (ROOT / f"{case['year']}.txt").read_text(encoding="utf-8")
    parsed = client.post(
        "/api/admin/attendance-calendars/parse", json={"text": text}, headers=headers
    )
    assert parsed.status_code == 200
    preview = parsed.json()["data"]
    assert preview["canSave"], preview["errors"]
    assert client.get("/api/admin/attendance-calendars").json()["data"]["items"] == []
    saved = client.post(
        "/api/admin/attendance-calendars/apply",
        headers=headers,
        json={
            "text": text,
            "previewDigest": preview["previewDigest"],
            "expectedRevisions": preview["expectedRevisions"],
        },
    )
    assert saved.status_code == 200, saved.text
    items = saved.json()["data"]["items"]
    assert sorted(
        [day for item in items for day in item["days"]], key=lambda day: day["date"]
    ) == expected_days(case)
    assert [item["year"] for item in items if item["complete"]] == [case["year"]]
