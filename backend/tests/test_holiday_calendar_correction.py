"""Explicit corrections use public/synthetic annual notices, never employee data."""

import copy
from datetime import date, timedelta

import pytest
from conftest import mock_login
from test_attendance_calendar import CROSS_NOTICE, NOTICE, NOTICE_2025, admin, items, save

from app.models.setting import Setting
from app.services.holiday_notice_parser import parse_holiday_notice


def correction(text=NOTICE):
    parsed = parse_holiday_notice(text)
    assert parsed.can_save
    return {
        "year": parsed.year,
        "ranges": [
            {
                "name": day.name,
                "startDate": day.date.isoformat(),
                "endDate": day.date.isoformat(),
                "working": day.working,
            }
            for day in parsed.days
        ],
    }


def preview(client, headers, text, patch):
    return client.post(
        "/api/admin/attendance-calendars/parse",
        json={"text": text, "correction": patch},
        headers=headers,
    )


def apply(client, headers, text, patch, plan):
    return client.post(
        "/api/admin/attendance-calendars/apply",
        json={
            "text": text,
            "correction": patch,
            "previewDigest": plan["previewDigest"],
            "expectedRevisions": plan["expectedRevisions"],
        },
        headers=headers,
    )


def test_failed_auto_parse_can_be_explicitly_corrected_previewed_saved_and_retried(client_factory):
    client, headers = admin(client_factory)
    source = NOTICE.replace("星期四", "星期三")
    patch = correction()
    auto = client.post(
        "/api/admin/attendance-calendars/parse", json={"text": source}, headers=headers
    ).json()["data"]
    assert not auto["canSave"]
    plan = preview(client, headers, source, patch).json()["data"]
    assert plan["canSave"] and plan["sourceFormat"] == "manual" and plan["warnings"]
    assert "星期三" in plan["sections"][0]["source"]
    assert items(client) == []
    result = apply(client, headers, source, patch, plan)
    assert result.status_code == 200, result.text
    saved = items(client)
    assert len(saved[0]["days"]) == 39 and "手动核对" in saved[0]["noticeTitle"]
    assert apply(client, headers, source, patch, plan).json()["data"]["unchanged"]
    assert items(client) == saved
    assert save(client, headers, source, auto).status_code == 422
    with client.app.state.database_session_factory() as database:
        raw = database.get(Setting, "attendance_calendar:2026").value
        assert "星期三" not in raw and "节假日期间" not in raw


def test_unknown_format_can_be_corrected_but_original_and_corrections_bind_the_preview(
    client_factory,
):
    client, headers = admin(client_factory)
    source, patch = "合成来源：另一种格式的全年日期安排，供手工核对。", correction()
    plan = preview(client, headers, source, patch).json()["data"]
    assert plan["canSave"]
    altered = copy.deepcopy(patch)
    altered["ranges"][0]["name"] = "元旦修正"
    assert apply(client, headers, source, altered, plan).status_code == 422
    assert apply(client, headers, source + "改动", patch, plan).status_code == 422
    assert apply(client, headers, source, None, plan).status_code == 422
    assert items(client) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(year=2028),
        lambda value: value["ranges"].append(copy.deepcopy(value["ranges"][0])),
        lambda value: value["ranges"].append({**value["ranges"][0], "working": True}),
        lambda value: value["ranges"][0].update(startDate="2026-01-02", endDate="2026-01-01"),
        lambda value: value["ranges"][0].update(startDate="2026-01-01", endDate="2026-03-01"),
        lambda value: value.update(
            ranges=[day for day in value["ranges"] if "春节" not in day["name"]]
        ),
    ],
)
def test_invalid_corrections_cannot_generate_a_saveable_preview(client_factory, change):
    client, headers = admin(client_factory)
    patch = correction()
    change(patch)
    response = preview(client, headers, NOTICE, patch)
    assert response.status_code == 200, response.text
    plan = response.json()["data"]
    assert not plan["canSave"] and plan["errors"]
    assert apply(client, headers, NOTICE, patch, plan).status_code == 422
    assert items(client) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(year=True),
        lambda value: value.update(year=0),
        lambda value: value.update(ranges=[]),
        lambda value: value.update(ranges=value["ranges"] * 3),
        lambda value: value["ranges"][0].update(working="false"),
        lambda value: value["ranges"][0].update(startDate="2026-02-30"),
        lambda value: value["ranges"][0].update(startDate="2026-1-1"),
        lambda value: value["ranges"][0].update(name="   "),
        lambda value: value["ranges"][0].update(name="元旦\n"),
        lambda value: value["ranges"][0].update(name="元旦\u007f"),
        lambda value: value["ranges"][0].update(extra="unexpected"),
    ],
)
def test_strict_correction_schema(client_factory, change):
    client, headers = admin(client_factory)
    patch = correction()
    change(patch)
    response = preview(client, headers, NOTICE, patch)
    assert response.status_code == 422
    assert "元旦\\n" not in response.text
    assert items(client) == []


@pytest.mark.parametrize("text", ["", "  ", "https://example.invalid/holiday", "中" * 22000])
def test_correction_still_requires_bounded_pasted_source(client_factory, text):
    client, headers = admin(client_factory)
    response = preview(client, headers, text, correction())
    if response.status_code == 200:
        assert not response.json()["data"]["canSave"]
    else:
        assert response.status_code == 422
    assert items(client) == []


def test_corrected_cross_year_cas_conflict_is_atomic_and_preserves_other_sources(client_factory):
    client, headers = admin(client_factory)
    patch = correction(CROSS_NOTICE)
    plan = preview(client, headers, CROSS_NOTICE, patch).json()["data"]
    assert plan["canSave"] and set(plan["expectedRevisions"]) == {"2018", "2019"}
    assert save(client, headers, NOTICE_2025.replace("2025", "2018")).status_code == 200
    before = items(client)
    assert apply(client, headers, CROSS_NOTICE, patch, plan).status_code == 409
    assert items(client) == before
    fresh = preview(client, headers, CROSS_NOTICE, patch).json()["data"]
    assert apply(client, headers, CROSS_NOTICE, patch, fresh).status_code == 200
    saved = {item["year"]: item for item in items(client)}
    assert saved[2018]["sourceYears"] == [2018, 2019]
    assert not saved[2019]["legacy"]


def test_correction_endpoints_enforce_admin_and_csrf(client_factory):
    client = client_factory(auth_mock_enabled=True, admin_user_ids="someone-else")
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    for path in ["parse", "apply"]:
        for supplied in [headers, {}]:
            assert (
                client.post(
                    f"/api/admin/attendance-calendars/{path}",
                    json={"text": NOTICE, "correction": correction()},
                    headers=supplied,
                ).status_code
                == 403
            )
    client, _ = admin(client_factory)
    assert preview(client, {}, NOTICE, correction()).status_code == 403


def test_combined_holiday_preserves_its_original_source_after_correction(client_factory):
    client, headers = admin(client_factory)
    plan = preview(client, headers, NOTICE_2025, correction(NOTICE_2025)).json()["data"]
    combined = next(section for section in plan["sections"] if "国庆" in section["name"])
    assert "10月1日至8日" in combined["source"]


def test_correction_errors_explain_which_fields_to_fix_instead_of_requesting_another_json(
    client_factory,
):
    client, headers = admin(client_factory)
    patch = correction()
    patch["ranges"] = [day for day in patch["ranges"] if day["name"] != "元旦"]
    issue = preview(client, headers, NOTICE, patch).json()["data"]["errors"][0]["message"]
    assert "补齐这些节日的放假日期" in issue and "JSON" not in issue
    patch = correction()
    patch["ranges"].append(copy.deepcopy(patch["ranges"][0]))
    issue = preview(client, headers, NOTICE, patch).json()["data"]["errors"][0]["message"]
    assert "删除重叠的日期安排" in issue and "JSON" not in issue


def test_structured_ranges_are_bounded_independently_of_the_pasted_source_length(client_factory):
    client, headers = admin(client_factory)
    names = ["元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"]
    ranges = []
    for offset in range(0, 400, 32):
        start = date(2042, 1, 1) + timedelta(days=offset)
        end = start + timedelta(days=min(32, 400 - offset) - 1)
        ranges.append(
            {
                "name": names[(offset // 32) % 7] + "合成说明" * 12,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "working": False,
            }
        )
    patch = {"year": 2042, "ranges": ranges}
    assert preview(client, headers, "合成完整年度来源，供手动核对。", patch).json()["data"][
        "canSave"
    ]
    ranges[-1]["endDate"] = (end + timedelta(days=1)).isoformat()
    rejected = preview(client, headers, "合成完整年度来源，供手动核对。", patch).json()["data"]
    assert not rejected["canSave"] and "400 天" in rejected["errors"][0]["message"]
