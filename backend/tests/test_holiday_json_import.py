"""Actual provider samples plus synthetic malformed envelopes; no live HTTP tests."""

import copy
import json
from datetime import date
from pathlib import Path

import pytest
from conftest import mock_login
from test_holiday_notice_history import CASES, expected_days

from app.services.holiday_calendar_import import parse_calendar_content

ROOT = Path(__file__).parent / "fixtures" / "holiday_json"
SOURCES = json.loads((ROOT / "sources.json").read_text())


def load(provider="holiday-cn", year=2026):
    return json.loads((ROOT / f"{provider}-{year}.json").read_text())


def provider_days(provider, data):
    field = "holiday" if provider == "timor" else "isOffDay"
    values = data["holiday"].values() if provider == "timor" else data["days"]
    return sorted(
        [
            {"date": value["date"], "name": value["name"], "working": not value[field]}
            for value in values
        ],
        key=lambda day: day["date"],
    )


@pytest.mark.parametrize("sample", SOURCES, ids=lambda item: f"{item['provider']}-{item['year']}")
def test_real_json_exact_flags_dates_labels_and_effective_attendance_match_official_notice(sample):
    data = load(sample["provider"], sample["year"])
    parsed = parse_calendar_content(json.dumps(data, ensure_ascii=False))
    assert parsed.can_save, parsed.errors
    assert parsed.source_format == ("timor" if sample["provider"] == "timor" else "holiday_cn")
    assert parsed.year == sample["year"]
    label = "提莫节假日 API" if sample["provider"] == "timor" else "holiday-cn 年度数据"
    assert parsed.title == f"{label}（{sample['year']} 年）"
    assert [day.as_dict() for day in parsed.days] == provider_days(sample["provider"], data)
    assert parsed.warnings and parsed.sections
    # Some providers explicitly list contiguous weekends; compare the actual
    # working/rest outcome for every date, not only the number of exceptions.
    case = next(item for item in CASES if item["year"] == sample["year"])
    official = {item["date"]: item["working"] for item in expected_days(case)}
    imported = {day.date.isoformat(): day.working for day in parsed.days}
    for ordinal in range(
        date(case["year"], 1, 1).toordinal(), date(case["year"], 12, 31).toordinal() + 1
    ):
        day = date.fromordinal(ordinal)
        assert imported.get(day.isoformat(), day.weekday() < 5) == official.get(
            day.isoformat(), day.weekday() < 5
        ), day


@pytest.mark.parametrize("sample", SOURCES, ids=lambda item: f"{item['provider']}-{item['year']}")
def test_both_formats_preview_apply_and_lost_response_retry(sample, client_factory):
    client = client_factory(auth_mock_enabled=True, admin_user_ids="mock-user")
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    data = load(sample["provider"], sample["year"])
    text = json.dumps(data)
    response = client.post(
        "/api/admin/attendance-calendars/parse", json={"text": text}, headers=headers
    )
    preview = response.json()["data"]
    assert response.status_code == 200 and preview["canSave"]
    assert preview["sourceLabel"] in {"提莫节假日 API", "holiday-cn 年度数据"}
    assert client.get("/api/admin/attendance-calendars").json()["data"]["items"] == []
    body = {
        "text": text,
        "previewDigest": preview["previewDigest"],
        "expectedRevisions": preview["expectedRevisions"],
    }
    saved = client.post("/api/admin/attendance-calendars/apply", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    items = saved.json()["data"]["items"]
    assert sorted(
        [day for item in items for day in item["days"]], key=lambda day: day["date"]
    ) == provider_days(sample["provider"], data)
    assert [item["year"] for item in items if item["complete"]] == [sample["year"]]
    retry = client.post("/api/admin/attendance-calendars/apply", json=body, headers=headers)
    assert retry.status_code == 200 and retry.json()["data"]["unchanged"]
    assert retry.json()["data"]["items"] == items


@pytest.mark.parametrize("provider", ["timor", "holiday-cn"])
def test_bom_and_unused_external_metadata_do_not_change_the_calendar(provider):
    data = load(provider)
    expected = provider_days(provider, data)
    data["$schema"] = "https://invalid.example/schema"
    data["papers"] = ["http://127.0.0.1/private"]
    data["wage"] = "ignored"
    text = "\ufeff" + json.dumps(data)
    parsed = parse_calendar_content(text)
    assert parsed.can_save
    assert [day.as_dict() for day in parsed.days] == expected


@pytest.mark.parametrize("provider", ["timor", "holiday-cn"])
def test_missing_holiday_keeps_valid_entries_for_manual_correction_without_allowing_save(provider):
    data = load(provider)
    if provider == "timor":
        data["holiday"] = {
            key: value for key, value in data["holiday"].items() if "元旦" not in value["name"]
        }
    else:
        data["days"] = [value for value in data["days"] if "元旦" not in value["name"]]
    parsed = parse_calendar_content(json.dumps(data))
    assert not parsed.can_save
    assert "元旦" in parsed.errors[0]["message"]
    assert [day.as_dict() for day in parsed.days] == provider_days(provider, data)
    assert len(parsed.sections) == 6
    assert all(section["restDates"] for section in parsed.sections)


@pytest.mark.parametrize("provider", ["timor", "holiday-cn"])
@pytest.mark.parametrize(
    "problem",
    [
        "missing_flag",
        "string_flag",
        "number_flag",
        "missing_date",
        "invalid_date",
        "missing_name",
        "control_name",
        "partial_year",
    ],
)
def test_rejects_incomplete_or_malformed_daily_data(provider, problem):
    data = load(provider)
    field = "holiday" if provider == "timor" else "isOffDay"
    row = next(iter(data["holiday"].values())) if provider == "timor" else data["days"][0]
    if problem == "missing_flag":
        row.pop(field)
    elif problem == "string_flag":
        row[field] = "false"
    elif problem == "number_flag":
        row[field] = 0
    elif problem == "missing_date":
        row.pop("date")
    elif problem == "invalid_date":
        row["date"] = "2026-02-30"
    elif problem == "missing_name":
        row.pop("name")
    elif problem == "control_name":
        row["name"] = "元旦\n"
    else:
        if provider == "timor":
            data["holiday"] = {
                key: value for key, value in data["holiday"].items() if key.startswith("01-")
            }
        else:
            data["days"] = [value for value in data["days"] if value["date"].startswith("2026-01")]
    assert not parse_calendar_content(json.dumps(data)).can_save


@pytest.mark.parametrize(
    "text",
    [
        '{"code":-1,"holiday":{}}',
        '{"code":true,"holiday":{}}',
        '{"code":0,"holiday":{}}',
        '{"year":true,"days":[]}',
        '{"year":"2026","days":[]}',
        '{"year":2026,"days":[]}',
        '{"year":2026,"year":2025,"days":[]}',
        '{"code":0,"code":-1,"holiday":{}}',
        '{"year":2026,"days":[',
        "[]",
        '{"message":"error page"}',
        '{"code":NaN}',
        "https://timor.tech/api/holiday/year/2026/",
    ],
)
def test_error_envelopes_duplicate_fields_arrays_and_url_only_input_are_not_calendars(text):
    assert not parse_calendar_content(text).can_save


def test_year_mismatch_duplicate_dates_mixed_formats_and_tampered_preview(client_factory):
    data = load("timor")
    data["holiday"]["01-01"]["date"] = "2025-01-01"
    assert not parse_calendar_content(json.dumps(data)).can_save
    data = load("timor")
    data["holiday"]["01-02"]["date"] = "2026-01-01"
    assert not parse_calendar_content(json.dumps(data)).can_save
    data = load()
    data["days"].append(copy.deepcopy(data["days"][0]))
    assert not parse_calendar_content(json.dumps(data)).can_save
    data = load()
    data["year"] = 2020
    assert not parse_calendar_content(json.dumps(data)).can_save
    data = load()
    data.update(code=0, holiday={})
    assert not parse_calendar_content(json.dumps(data)).can_save
    client = client_factory(auth_mock_enabled=True, admin_user_ids="mock-user")
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    text = json.dumps(load())
    preview = client.post(
        "/api/admin/attendance-calendars/parse", json={"text": text}, headers=headers
    ).json()["data"]
    response = client.post(
        "/api/admin/attendance-calendars/apply",
        json={
            "text": text + " ",
            "previewDigest": preview["previewDigest"],
            "expectedRevisions": preview["expectedRevisions"],
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert client.get("/api/admin/attendance-calendars").json()["data"]["items"] == []
