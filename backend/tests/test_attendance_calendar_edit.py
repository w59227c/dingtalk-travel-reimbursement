"""Saved-calendar editing uses synthetic/public dates and source ownership."""

import copy
import json
from datetime import date, timedelta

import pytest
from conftest import mock_login
from test_attendance_calendar import CROSS_NOTICE, NOTICE_2025, admin, items, save

from app.models.setting import Setting


def editor(client, year=2025):
    response = client.get(f"/api/admin/attendance-calendars/{year}/edit")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def preview(client, headers, form):
    return client.post(
        "/api/admin/attendance-calendars/parse", json={"text": "", **form}, headers=headers
    )


def apply(client, headers, form, plan):
    return client.post(
        "/api/admin/attendance-calendars/apply",
        json={
            "text": "",
            **form,
            "previewDigest": plan["previewDigest"],
            "expectedRevisions": plan["expectedRevisions"],
        },
        headers=headers,
    )


def move_new_year(form):
    value = next(
        row for row in form["correction"]["ranges"] if row["name"] == "元旦" and not row["working"]
    )
    value.update(
        startDate=f"{form['correction']['year']}-01-02",
        endDate=f"{form['correction']['year']}-01-02",
    )


def test_edit_prefills_compact_ranges_previews_without_writes_and_retries_safely(client_factory):
    client, headers = admin(client_factory)
    assert save(client, headers, NOTICE_2025).status_code == 200
    before = items(client)
    form = editor(client)
    assert form["editContext"]["expectedRevisions"] == {
        "2024": None,
        "2025": before[0]["revision"],
        "2026": None,
    }
    assert len(form["correction"]["ranges"]) < len(before[0]["days"])
    same = preview(client, headers, form).json()["data"]
    assert same["canSave"] and same["unchanged"]
    assert apply(client, headers, form, same).json()["data"]["unchanged"]
    assert items(client) == before
    move_new_year(form)
    plan = preview(client, headers, form).json()["data"]
    assert plan["canSave"] and not plan["unchanged"]
    assert plan["changes"][0]["removed"][0]["date"] == "2025-01-01"
    assert plan["changes"][0]["added"][0]["date"] == "2025-01-02"
    assert items(client) == before
    assert apply(client, headers, form, plan).status_code == 200
    after = items(client)
    assert "手动修改" in after[0]["noticeTitle"]
    assert apply(client, headers, form, plan).json()["data"]["unchanged"]
    assert items(client) == after


def test_edit_includes_own_cross_year_dates_and_never_absorbs_other_sources(client_factory):
    client, headers = admin(client_factory)
    assert save(client, headers, NOTICE_2025.replace("2025", "2018")).status_code == 200
    assert save(client, headers, CROSS_NOTICE).status_code == 200
    own = editor(client, 2018)
    assert all(row["startDate"] < "2018-12-29" for row in own["correction"]["ranges"])
    move_new_year(own)
    plan = preview(client, headers, own).json()["data"]
    assert apply(client, headers, own, plan).status_code == 200
    own = editor(client, 2019)
    assert any(row["startDate"] == "2018-12-30" for row in own["correction"]["ranges"])
    before = {value["year"]: value for value in items(client)}
    own["correction"]["ranges"] = [
        row for row in own["correction"]["ranges"] if row["endDate"] > "2018-12-31"
    ]
    for row in own["correction"]["ranges"]:
        if row["startDate"] < "2019-01-01":
            row["startDate"] = "2019-01-01"
    plan = preview(client, headers, own).json()["data"]
    assert plan["canSave"]
    assert apply(client, headers, own, plan).status_code == 200
    after = {value["year"]: value for value in items(client)}
    assert after[2018]["sourceYears"] == [2018]
    assert after[2018]["days"] == [
        day for day in before[2018]["days"] if day["date"] < "2018-12-29"
    ]
    assert after[2018]["complete"] and after[2019]["complete"]


def test_stale_opening_and_preview_bases_reject_atomically_and_can_be_rebased(client_factory):
    client, headers = admin(client_factory)
    assert save(client, headers, NOTICE_2025).status_code == 200
    form = editor(client)
    move_new_year(form)
    # A new adjacent source changes one of the opening revisions.
    assert save(client, headers, NOTICE_2025.replace("2025", "2026")).status_code == 200
    before = items(client)
    assert preview(client, headers, form).status_code == 409
    assert items(client) == before
    form["editContext"] = editor(client)["editContext"]
    plan = preview(client, headers, form).json()["data"]
    assert plan["canSave"]
    assert (
        save(client, headers, NOTICE_2025.replace("1月1日放假1天", "1月3日放假1天")).status_code
        == 200
    )
    before = items(client)
    assert apply(client, headers, form, plan).status_code == 409
    assert items(client) == before
    form["editContext"] = editor(client)["editContext"]
    fresh = preview(client, headers, form).json()["data"]
    assert fresh["previewDigest"] != plan["previewDigest"]
    assert apply(client, headers, form, plan).status_code == 422
    assert apply(client, headers, form, fresh).status_code == 200


@pytest.mark.parametrize("mutation", ["year", "text", "missing", "revision", "context"])
def test_edit_context_cannot_silently_change_scope_or_reuse_a_different_preview(
    client_factory, mutation
):
    client, headers = admin(client_factory)
    assert save(client, headers, NOTICE_2025).status_code == 200
    form = editor(client)
    move_new_year(form)
    plan = preview(client, headers, form).json()["data"]
    before = items(client)
    altered = copy.deepcopy(form)
    if mutation == "year":
        altered["editContext"]["year"] += 1
    elif mutation == "text":
        altered["text"] = "另一个来源"
    elif mutation == "missing":
        altered.pop("correction")
    elif mutation == "revision":
        altered["editContext"]["expectedRevisions"] = {}
    else:
        altered.pop("editContext")
    assert apply(client, headers, altered, plan).status_code == 422
    if mutation == "revision":
        assert preview(client, headers, altered).status_code == 409
    assert items(client) == before


def test_partial_missing_deleted_and_invalid_year_editors_are_actionable(client_factory):
    client, headers = admin(client_factory)
    assert save(client, headers, CROSS_NOTICE).status_code == 200
    for year in [2018, 2042]:
        response = client.get(f"/api/admin/attendance-calendars/{year}/edit")
        assert response.status_code == 409 and "来源年度" in response.json()["error"]["message"]
    form = editor(client, 2019)
    plan = preview(client, headers, form).json()["data"]
    current = next(value for value in items(client) if value["year"] == 2019)
    assert (
        client.delete(
            "/api/admin/attendance-calendars/2019",
            params={"revision": current["revision"]},
            headers=headers,
        ).status_code
        == 200
    )
    assert apply(client, headers, form, plan).status_code == 409
    assert items(client) == []
    for year in [0, 10000, "not-a-year"]:
        assert client.get(f"/api/admin/attendance-calendars/{year}/edit").status_code == 422


def test_edit_get_requires_authenticated_administrator_and_writes_still_require_csrf(
    client_factory,
):
    client = client_factory(auth_mock_enabled=True, admin_user_ids="another-user")
    assert client.get("/api/admin/attendance-calendars/2025/edit").status_code == 401
    mock_login(client)
    assert client.get("/api/admin/attendance-calendars/2025/edit").status_code == 403
    client, headers = admin(client_factory)
    assert save(client, headers, NOTICE_2025).status_code == 200
    form = editor(client)
    assert preview(client, {}, form).status_code == 403
    plan = preview(client, headers, form).json()["data"]
    assert apply(client, {}, form, plan).status_code == 403


def test_legacy_dates_remain_editable_and_long_ranges_are_split(client_factory):
    client, _ = admin(client_factory)
    days = [
        {
            "date": (date(2042, 1, 1) + timedelta(days=index)).isoformat(),
            "working": False,
            "name": "",
        }
        for index in range(40)
    ]
    with client.app.state.database_session_factory() as database:
        database.add(
            Setting(
                key="attendance_calendar:2042",
                value=json.dumps(
                    {
                        "year": 2042,
                        "revision": "legacy-one",
                        "updatedAt": "2041-12-01T00:00:00Z",
                        "days": days,
                    }
                ),
            )
        )
        database.commit()
    before = items(client)
    form = editor(client, 2042)
    assert form["correction"]["ranges"] == [
        {"name": "", "working": False, "startDate": "2042-01-01", "endDate": "2042-02-01"},
        {"name": "", "working": False, "startDate": "2042-02-02", "endDate": "2042-02-09"},
    ]
    assert items(client) == before
