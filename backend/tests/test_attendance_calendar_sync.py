"""Deterministic network fixtures and independent, durable scheduler instances."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from test_attendance_calendar import NOTICE, admin, items, save

from app.core.errors import ApiError
from app.services import attendance_calendar_sync as sync_module
from app.services.attendance_calendar import (
    delete_calendar,
)
from app.services.attendance_calendar_sync import AttendanceCalendarSync
from app.services.holiday_notice_parser import parse_holiday_notice


def annual(year=2027):
    # Calendar dates are public-format synthetic fixtures, not production data.
    parsed = parse_holiday_notice(NOTICE)
    return json.dumps(
        {
            "year": year,
            "days": [
                {
                    "date": f"{year}{day.date.isoformat()[4:]}",
                    "name": day.name,
                    "isOffDay": not day.working,
                }
                for day in parsed.days
            ],
        }
    )


def worker(client):
    return AttendanceCalendarSync(client.app.state.database_session_factory)


def job(service, year, now):
    return next(item for item in service.status(now)["items"] if item["year"] == year)


def test_november_boundary_stops_after_success_and_survives_restart(client_factory, monkeypatch):
    client, headers = admin(client_factory)
    assert save(client, headers).status_code == 200
    fetch = AsyncMock(return_value=annual())
    monkeypatch.setattr(sync_module, "fetch_holiday_cn", fetch)
    service = worker(client)
    before = datetime(2026, 10, 31, 15, 59, tzinfo=UTC)
    asyncio.run(service.tick(before))
    fetch.assert_not_called()
    assert job(service, 2027, before)["status"] == "scheduled"
    start = before + timedelta(minutes=1)
    asyncio.run(service.tick(start))
    fetch.assert_awaited_once_with(2027)
    assert job(service, 2027, start)["status"] == "completed"
    restarted = worker(client)
    asyncio.run(restarted.tick(start + timedelta(days=10)))
    fetch.assert_awaited_once()
    assert any(item["year"] == 2027 and item["complete"] for item in items(client))


def test_failure_retries_every_five_days_and_keeps_original_target_across_new_year(
    client_factory, monkeypatch
):
    client, headers = admin(client_factory)
    save(client, headers)
    fetch = AsyncMock(side_effect=ApiError("unavailable", "safe", 503))
    monkeypatch.setattr(sync_module, "fetch_holiday_cn", fetch)
    service = worker(client)
    now = datetime(2026, 12, 30, tzinfo=UTC)
    asyncio.run(service.tick(now))
    assert job(service, 2027, now)["nextAttemptAt"] == (now + timedelta(days=5)).isoformat()
    asyncio.run(worker(client).tick(now + timedelta(days=4)))
    assert fetch.await_count == 1
    fetch.side_effect = None
    fetch.return_value = annual()
    asyncio.run(worker(client).tick(now + timedelta(days=5)))
    assert fetch.await_count == 2
    assert job(service, 2027, now + timedelta(days=5))["status"] == "completed"


def test_manual_future_query_does_not_start_automatic_polling_before_november(
    client_factory, monkeypatch
):
    client, headers = admin(client_factory)
    save(client, headers)
    service = worker(client)
    fetch = AsyncMock(side_effect=ApiError("unavailable", "safe", 503))
    monkeypatch.setattr(sync_module, "fetch_holiday_cn", fetch)
    october = datetime(2026, 10, 1, tzinfo=UTC)
    asyncio.run(service.check(2027, force=True, now=october))
    assert fetch.await_count == 1
    state = job(service, 2027, october)
    assert state["status"] == "scheduled"
    november = datetime(2026, 10, 31, 16, tzinfo=UTC)
    assert state["nextAttemptAt"] == november.isoformat()
    for elapsed in [5, 10, 25]:
        asyncio.run(worker(client).tick(october + timedelta(days=elapsed)))
    assert service._claim(2027, october + timedelta(days=25), False) is None
    assert fetch.await_count == 1
    asyncio.run(service.tick(november))
    assert fetch.await_count == 2
    assert job(service, 2027, november)["status"] == "retry"


def test_far_future_manual_failure_waits_for_its_own_annual_window(client_factory, monkeypatch):
    client, headers = admin(client_factory)
    save(client, headers)
    service = worker(client)
    fetch = AsyncMock(side_effect=ApiError("unavailable", "safe", 503))
    monkeypatch.setattr(sync_module, "fetch_holiday_cn", fetch)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    asyncio.run(service.check(2029, force=True, now=now))
    assert 2029 not in service._prepare(now + timedelta(days=5))
    state = job(service, 2029, now)
    assert state["status"] == "scheduled"
    assert state["nextAttemptAt"] == datetime(2028, 10, 31, 16, tzinfo=UTC).isoformat()


def test_expired_early_manual_lease_reports_the_actual_automatic_start(client_factory):
    client, _ = admin(client_factory)
    service = worker(client)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    assert service._claim(2027, now, True)
    assert job(service, 2027, now)["status"] == "running"
    expired = job(service, 2027, now + timedelta(minutes=3))
    assert expired["status"] == "scheduled"
    assert expired["nextAttemptAt"] == datetime(2026, 10, 31, 16, tzinfo=UTC).isoformat()


@pytest.mark.parametrize(
    "content",
    [
        "<html>captcha</html>",
        "{}",
        annual(2028),
        json.dumps(
            {"year": 2027, "days": [{"date": "2027-01-01", "name": "元旦", "isOffDay": True}]}
        ),
    ],
)
def test_invalid_or_wrong_year_never_marks_complete(client_factory, monkeypatch, content):
    client, _ = admin(client_factory)
    monkeypatch.setattr(sync_module, "fetch_holiday_cn", AsyncMock(return_value=content))
    now = datetime(2026, 11, 1, tzinfo=UTC)
    service = worker(client)
    asyncio.run(service.check(2027, now=now))
    assert items(client) == []
    assert job(service, 2027, now)["status"] == "retry"
    assert job(service, 2027, now)["errorCode"] == "invalid"


def test_lease_excludes_other_workers_and_recovers_after_expiry(client_factory):
    client, _ = admin(client_factory)
    first, second = worker(client), worker(client)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    token = first._claim(2027, now, False)
    assert token
    assert second._claim(2027, now, True) is None
    later = now + timedelta(minutes=3)
    replacement = second._claim(2027, later, False)
    assert replacement and replacement != token
    first._finish(2027, token, annual(), "", later)
    assert items(client) == []
    second._finish(2027, replacement, annual(), "", later)
    assert job(first, 2027, later)["status"] == "completed"


def test_manual_save_wins_over_inflight_fetch_and_delete_pauses_atomically(client_factory):
    client, headers = admin(client_factory)
    service = worker(client)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    token = service._claim(2026, now, False)
    saved = save(client, headers).json()["data"]["items"][0]
    service._finish(2026, token, annual(2026), "", now)
    assert items(client)[0] == saved
    with client.app.state.database_session_factory() as database:
        delete_calendar(database, 2026, saved["revision"])
    service._finish(2026, token, annual(2026), "", now)
    assert items(client) == []
    assert job(service, 2026, now)["status"] == "paused"
    assert service._claim(2026, now, False) is None
    assert service._claim(2026, now, True)


def test_calendar_and_job_completion_roll_back_together_on_storage_failure(
    client_factory, monkeypatch
):
    client, _ = admin(client_factory)
    service = worker(client)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    token = service._claim(2027, now, False)
    original = sync_module._put

    def fail(database, state):
        if state["status"] == "completed":
            raise RuntimeError("synthetic write failure")
        original(database, state)

    monkeypatch.setattr(sync_module, "_put", fail)
    with pytest.raises(RuntimeError):
        service._finish(2027, token, annual(), "", now)
    assert items(client) == []
    assert job(service, 2027, now)["status"] == "running"


def test_bootstrap_current_year_and_manual_completion_do_not_fetch_again(
    client_factory, monkeypatch
):
    client, _ = admin(client_factory)
    service = worker(client)
    fetch = AsyncMock(return_value=annual(2026))
    monkeypatch.setattr(sync_module, "fetch_holiday_cn", fetch)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    asyncio.run(service.tick(now))
    fetch.assert_awaited_once_with(2026)
    asyncio.run(service.tick(now + timedelta(days=5)))
    fetch.assert_awaited_once()


def test_sync_api_auth_csrf_and_readonly_status(client_factory, monkeypatch):
    anonymous = client_factory()
    assert anonymous.get("/api/admin/attendance-calendars/sync").status_code == 401
    user = client_factory(auth_mock_enabled=True)
    from conftest import mock_login

    user_headers = {"X-CSRF-Token": mock_login(user)["csrfToken"]}
    assert user.get("/api/admin/attendance-calendars/sync").status_code == 403
    assert (
        user.post(
            "/api/admin/attendance-calendars/sync", json={"year": 2027}, headers=user_headers
        ).status_code
        == 403
    )
    client, headers = admin(client_factory)
    monkeypatch.setattr(sync_module, "fetch_holiday_cn", AsyncMock(return_value=annual()))
    assert client.get("/api/admin/attendance-calendars/sync").status_code == 200
    assert (
        client.post("/api/admin/attendance-calendars/sync", json={"year": 2027}).status_code == 403
    )
    response = client.post(
        "/api/admin/attendance-calendars/sync", json={"year": 2027}, headers=headers
    )
    assert response.status_code == 200 and any(item["year"] == 2027 for item in items(client))
    for bad in [True, 0, 10000, "2027"]:
        assert (
            client.post(
                "/api/admin/attendance-calendars/sync", json={"year": bad}, headers=headers
            ).status_code
            == 422
        )


def test_http_fetch_bounds_and_fixed_url(monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def transport(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=annual())

    def client(**options):
        assert options["follow_redirects"] is False and options["trust_env"] is False
        return real_client(**options, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(sync_module.httpx, "AsyncClient", client)
    assert json.loads(asyncio.run(sync_module.fetch_holiday_cn(2027)))["year"] == 2027
    assert seen == ["https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/2027.json"]


@pytest.mark.parametrize(
    "status,body,code",
    [(404, "", "unavailable"), (302, "", "network"), (200, "x" * (64 * 1024 + 1), "invalid")],
)
def test_http_fetch_rejects_missing_redirect_or_oversize(monkeypatch, status, body, code):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        sync_module.httpx,
        "AsyncClient",
        lambda **options: real_client(
            **options, transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body))
        ),
    )
    with pytest.raises(ApiError) as failure:
        asyncio.run(sync_module.fetch_holiday_cn(2027))
    assert failure.value.code == code
