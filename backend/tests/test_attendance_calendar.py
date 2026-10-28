"""Public-format and synthetic fixtures; no employee/production records."""

import json

import pytest
from conftest import mock_login

from app.core.errors import ApiError
from app.models.setting import Setting
from app.services.attendance_calendar import (
    NoticeApply,
    apply_notice,
    attendance_calendar,
    preview_notice,
)
from app.services.holiday_notice_parser import parse_holiday_notice

NOTICE = """国务院办公厅关于2026年部分节假日安排的通知
一、元旦：1月1日（星期四）至3日（星期六）放假调休，共3天。1月4日（星期日）上班。
二、春节：2月15日（农历腊月二十八、星期日）至23日（星期一）放假调休，共9天。2月14日（星期六）、2月28日（星期六）上班。
三、清明节：4月4日至6日放假，共3天。
四、劳动节：5月1日至5日放假调休，共5天。5月9日上班。
五、端午节：6月19日至21日放假，共3天。
六、中秋节：9月25日至27日放假，共3天。
七、国庆节：10月1日至7日放假调休，共7天。9月20日、10月10日上班。
节假日期间，各地区、各部门要妥善安排好值班和安全工作。
国务院办公厅 2025年11月4日
"""
NOTICE_2025 = """国务院办公厅关于2025年部分节假日安排的通知
自2025年1月1日起，春节、劳动节各增加1天。
一、元旦：1月1日放假1天，不调休。
二、春节：1月28日至2月4日放假调休，共8天。1月26日、2月8日上班。
三、清明节：4月4日至6日放假，共3天。
四、劳动节：5月1日至5日放假调休，共5天。4月27日上班。
五、端午节：5月31日至6月2日放假，共3天。
六、国庆节、中秋节：10月1日至8日放假调休，共8天。9月28日、10月11日上班。
"""
# Synthetic arrangements exercising the historical cross-year grammar.
CROSS_NOTICE = NOTICE_2025.replace("2025", "2019").replace(
    "1月1日放假1天，不调休。", "2018年12月30日至2019年1月1日放假调休，共3天。2018年12月29日上班。"
)


def admin(client_factory):
    client = client_factory(auth_mock_enabled=True, admin_user_ids="mock-user")
    return client, {"X-CSRF-Token": mock_login(client)["csrfToken"]}


def preview(client, headers, text=NOTICE):
    return client.post(
        "/api/admin/attendance-calendars/parse", json={"text": text}, headers=headers
    )


def save(client, headers, text=NOTICE, plan=None):
    plan = plan or preview(client, headers, text).json()["data"]
    return client.post(
        "/api/admin/attendance-calendars/apply",
        json={
            "text": text,
            "previewDigest": plan["previewDigest"],
            "expectedRevisions": plan["expectedRevisions"],
        },
        headers=headers,
    )


def items(client):
    return client.get("/api/admin/attendance-calendars").json()["data"]["items"]


def test_annual_formats_counts_recommendations_and_weekend_combination():
    for text, rest, work in [(NOTICE, 33, 6), (NOTICE_2025, 28, 5)]:
        parsed = parse_holiday_notice(text)
        assert parsed.can_save, parsed.errors
        assert sum(not day.working for day in parsed.days) == rest
        assert sum(day.working for day in parsed.days) == work
    assert len(parse_holiday_notice(NOTICE_2025).sections) == 6
    assert parse_holiday_notice(CROSS_NOTICE).can_save
    value = NOTICE_2025.replace("二、春节：", "二、春节：鼓励各单位安排职工在除夕（1月27日）休息。")
    parsed = parse_holiday_notice(value)
    assert parsed.can_save and parsed.warnings
    assert "2025-01-27" not in [day.date.isoformat() for day in parsed.days]
    value = NOTICE_2025.replace("1月1日放假1天，不调休", "1月3日放假，与周末连休，共3天")
    assert parse_holiday_notice(value).can_save
    assert parse_holiday_notice(NOTICE_2025.replace("2025", "2042")).can_save


@pytest.mark.parametrize(
    "text",
    [
        "一、元旦：1月1日放假。",
        NOTICE.replace("共9天", "共8天"),
        NOTICE.replace("星期四", "星期三"),
        NOTICE.replace("星期四", "取消上班、3月1日放假"),
        NOTICE.replace("共9天", "共9天，共8天"),
        NOTICE.replace("至3日", "至0日"),
        NOTICE.replace("七、国庆节", "八、国庆节"),
        NOTICE.replace("七、国庆节", "七、中秋节"),
        NOTICE.replace("10月1日至7日", "10月7日至1日"),
        NOTICE.replace("1月4日（星期日）上班", "1月1日（星期四）上班"),
        NOTICE.replace("1月4日（星期日）上班", "1月4日另行安排上班"),
        NOTICE.replace("1月4日（星期日）上班", "除1月4日外上班"),
        NOTICE.replace("1月4日（星期日）上班", "1月4日、1月5号上班"),
        NOTICE.replace("1月4日（星期日）上班", "1月4日至5日或者6日上班"),
        NOTICE + NOTICE_2025,
        "国务院办公厅关于调整2026年劳动节假期安排的通知" + NOTICE,
        NOTICE[: NOTICE.index("七、国庆节")],
        "x" * 20001,
    ],
)
def test_ambiguous_incomplete_changed_notices_block_saving(text):
    assert not parse_holiday_notice(text).can_save


def test_preview_read_only_apply_reparse_and_lost_response_retry(client_factory):
    client, headers = admin(client_factory)
    plan = preview(client, headers).json()["data"]
    assert plan["canSave"] and plan["year"] == 2026 and len(plan["sections"]) == 7
    assert plan["expectedRevisions"] == {"2026": None} and items(client) == []
    response = save(client, headers, plan=plan)
    assert response.status_code == 200, response.text
    saved = items(client)[0]
    assert saved["complete"] and saved["sourceYears"] == [2026] and not saved["legacy"]
    assert len(saved["days"]) == 39
    again = save(client, headers, plan=plan)
    assert again.status_code == 200 and again.json()["data"]["unchanged"]
    assert items(client)[0]["revision"] == saved["revision"]
    for text in [NOTICE.replace("共9天", "共8天"), NOTICE + " "]:
        assert save(client, headers, text, plan).status_code == 422
    with client.app.state.database_session_factory() as database:
        raw = database.get(Setting, "attendance_calendar:2026").value
        assert "节假日期间" not in raw and "星期" not in raw
        assert attendance_calendar(database)["years"] == [2026]


def test_cross_year_merge_replace_delete_preserves_other_sources(client_factory):
    client, headers = admin(client_factory)
    assert save(client, headers, CROSS_NOTICE).status_code == 200
    records = {item["year"]: item for item in items(client)}
    assert not records[2018]["complete"] and records[2018]["sourceYears"] == [2019]
    with client.app.state.database_session_factory() as database:
        calendar = attendance_calendar(database)
        assert calendar["years"] == [2019] and calendar["days"]["2018-12-29"]["working"]
    older = NOTICE_2025.replace("2025", "2018")
    assert save(client, headers, older).status_code == 200
    records = {item["year"]: item for item in items(client)}
    assert records[2018]["sourceYears"] == [2018, 2019]
    assert any(day["date"] == "2018-12-29" for day in records[2018]["days"])
    assert save(client, headers, NOTICE_2025.replace("2025", "2019")).status_code == 200
    records = {item["year"]: item for item in items(client)}
    assert records[2018]["sourceYears"] == [2018]
    assert not any(day["date"].startswith("2018-12") for day in records[2018]["days"])
    assert save(client, headers, CROSS_NOTICE).status_code == 200
    records = {item["year"]: item for item in items(client)}
    assert (
        client.delete(
            "/api/admin/attendance-calendars/2018",
            params={"revision": records[2018]["revision"]},
            headers=headers,
        ).status_code
        == 200
    )
    remaining = {item["year"]: item for item in items(client)}
    assert remaining[2018]["sourceYears"] == [2019] and not remaining[2018]["complete"]
    assert (
        client.delete(
            "/api/admin/attendance-calendars/2018",
            params={"revision": remaining[2018]["revision"]},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.delete(
            "/api/admin/attendance-calendars/2019",
            params={"revision": remaining[2019]["revision"]},
            headers=headers,
        ).status_code
        == 200
    )
    assert items(client) == []


def test_stale_preview_rejected_without_partial_changes(client_factory):
    client, headers = admin(client_factory)
    plan = preview(client, headers, CROSS_NOTICE).json()["data"]
    assert save(client, headers, NOTICE_2025.replace("2025", "2018")).status_code == 200
    before = items(client)
    assert save(client, headers, CROSS_NOTICE, plan).status_code == 409
    assert (
        client.delete(
            "/api/admin/attendance-calendars/2018", params={"revision": "stale"}, headers=headers
        ).status_code
        == 409
    )
    assert items(client) == before


def test_newer_notice_precedence_and_delete_restores_legacy_base(client_factory):
    client, headers = admin(client_factory)
    with client.app.state.database_session_factory() as database:
        database.add(
            Setting(
                key="attendance_calendar:2018",
                value=json.dumps(
                    {
                        "year": 2018,
                        "revision": "legacy",
                        "updatedAt": "2026-10-28T00:00:00+00:00",
                        "days": [{"date": "2018-12-29", "working": False, "name": "旧配置"}],
                    }
                ),
            )
        )
        database.commit()
    assert save(client, headers, CROSS_NOTICE).status_code == 200
    year = next(item for item in items(client) if item["year"] == 2018)
    assert next(day for day in year["days"] if day["date"] == "2018-12-29")["working"]
    notice = next(item for item in items(client) if item["year"] == 2019)
    assert (
        client.delete(
            "/api/admin/attendance-calendars/2019",
            params={"revision": notice["revision"]},
            headers=headers,
        ).status_code
        == 200
    )
    year = items(client)[0]
    assert year["complete"] and year["legacy"]
    assert year["days"] == [{"date": "2018-12-29", "working": False, "name": "旧配置"}]


def test_atomic_rollback_on_late_sql_failure(client_factory, monkeypatch):
    client, _ = admin(client_factory)
    with client.app.state.database_session_factory() as database:
        data = {
            "year": 2019,
            "revision": "old",
            "updatedAt": "2026-10-28T00:00:00+00:00",
            "days": [],
        }
        database.add(Setting(key="attendance_calendar:2019", value=json.dumps(data)))
        database.commit()
        plan = preview_notice(database, CROSS_NOTICE)
        execute = database.execute

        def conflict(statement, *args, **kwargs):
            if getattr(statement, "is_update", False):
                raise ApiError("ATTENDANCE_CALENDAR_CONFLICT", "test race", 409)
            return execute(statement, *args, **kwargs)

        monkeypatch.setattr(database, "execute", conflict)
        with pytest.raises(ApiError):
            apply_notice(
                database,
                NoticeApply(
                    text=CROSS_NOTICE,
                    previewDigest=plan["previewDigest"],
                    expectedRevisions=plan["expectedRevisions"],
                ),
            )
    assert [item["year"] for item in items(client)] == [2019]
    assert items(client)[0]["revision"] == "old"


def test_legacy_preserved_until_replacement_and_unrelated_settings_unchanged(client_factory):
    client, headers = admin(client_factory)
    with client.app.state.database_session_factory() as database:
        database.add(Setting(key="unrelated", value='{"keep":true}'))
        database.add(
            Setting(
                key="attendance_calendar:2026",
                value=json.dumps(
                    {
                        "year": 2026,
                        "revision": "legacy",
                        "updatedAt": "2026-10-28T00:00:00+00:00",
                        "days": [{"date": "2026-06-01", "working": False, "name": "旧配置"}],
                    }
                ),
            )
        )
        database.commit()
    assert items(client)[0]["legacy"]
    plan = preview(client, headers).json()["data"]
    assert plan["changes"][0]["removed"][0]["date"] == "2026-06-01"
    assert save(client, headers, plan=plan).status_code == 200
    with client.app.state.database_session_factory() as database:
        assert database.get(Setting, "unrelated").value == '{"keep":true}'


def test_permissions_csrf_limits_and_removed_entrypoints(client_factory):
    client, headers = admin(client_factory)
    for path in ["parse", "apply"]:
        assert (
            client.post(
                f"/api/admin/attendance-calendars/{path}", json={"text": NOTICE}
            ).status_code
            == 403
        )
    assert (
        client.post(
            "/api/admin/attendance-calendars/parse",
            content=b"x" * (128 * 1024 + 1),
            headers={**headers, "Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert preview(client, headers, "x" * 20001).status_code == 422
    assert (
        client.post(
            "/api/admin/attendance-calendars/parse",
            content=b"\xff",
            headers={**headers, "Content-Type": "application/json"},
        ).status_code
        == 422
    )
    assert client.post("/api/admin/attendance-calendars/preview", headers=headers).status_code in {
        404,
        405,
    }
    assert (
        client.put(
            "/api/admin/attendance-calendars/2026", json={"days": []}, headers=headers
        ).status_code
        == 405
    )
    user = client_factory(auth_mock_enabled=True, auth_mock_user_id="ordinary-user")
    user_headers = {"X-CSRF-Token": mock_login(user)["csrfToken"]}
    assert user.get("/api/admin/attendance-calendars").status_code == 403
    assert preview(user, user_headers).status_code == 403
    assert (
        save(
            user, user_headers, plan={"previewDigest": "a" * 64, "expectedRevisions": {}}
        ).status_code
        == 403
    )
    assert (
        user.delete(
            "/api/admin/attendance-calendars/2026", params={"revision": "x"}, headers=user_headers
        ).status_code
        == 403
    )
