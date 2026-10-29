"""Durable annual holiday-cn acquisition; employee requests never fetch remotely."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.models.setting import Setting
from app.services.attendance_calendar import (
    NoticeApply,
    apply_notice,
    list_calendars,
    preview_notice,
)

PREFIX = "attendance_calendar_sync:"
INTERVAL = timedelta(days=5)
LEASE = timedelta(minutes=2)
SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)
MESSAGES = {
    "unavailable": "holiday-cn 暂未提供该年数据，下次将自动重试。",
    "network": "holiday-cn 连接失败或超时，下次将自动重试；也可手动录入。",
    "invalid": "holiday-cn 数据未通过完整年度校验，请手动核对或等待下次重试。",
    "storage": "保存未完成，下次将自动重试；可重新检查或手动录入。",
}


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read(database: Session) -> dict[int, dict]:
    return {
        int(key[len(PREFIX) :]): json.loads(value)
        for key, value in database.execute(
            select(Setting.key, Setting.value).where(
                Setting.key.startswith(PREFIX, autoescape=True)
            )
        )
    }


def _put(database: Session, state: dict) -> None:
    key = f"{PREFIX}{state['year']}"
    row = database.get(Setting, key)
    if row is None:
        database.add(Setting(key=key, value=_json(state)))
    else:
        row.value = _json(state)
    database.flush()


def _new(year: int) -> dict:
    return {
        "year": year,
        "status": "pending",
        "lastAttemptAt": None,
        "nextAttemptAt": None,
        "completedAt": None,
        "errorCode": "",
        "leaseToken": "",
        "leaseUntil": None,
    }


def pause_year(database: Session, year: int) -> None:
    state = _read(database).get(year, _new(year))
    state.update(
        status="paused",
        nextAttemptAt=None,
        leaseToken="",
        leaseUntil=None,
        errorCode="",
        completedAt=None,
    )
    _put(database, state)


def _targets(now: datetime) -> list[int]:
    local = now.astimezone(SHANGHAI)
    return [local.year] + ([local.year + 1] if local.month >= 11 and local.year < 9999 else [])


def _automatic_window_open(year: int, now: datetime) -> bool:
    # A manual query must not start future annual polling ahead of November.
    return year <= _targets(now)[-1]


def _window_start(year: int) -> datetime:
    return datetime(max(1, year - 1), 11, 1, tzinfo=SHANGHAI).astimezone(UTC)


async def fetch_holiday_cn(year: int) -> str:
    # No arbitrary URLs, environment proxy, redirects, HTML parsing or remote code.
    url = f"https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
            ) as response:
                if response.status_code == 404:
                    raise ApiError("unavailable", MESSAGES["unavailable"], 503)
                if response.status_code != 200:
                    raise ApiError("network", MESSAGES["network"], 503)
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    content.extend(chunk)
                    if len(content) > 64 * 1024:
                        raise ApiError("invalid", MESSAGES["invalid"], 422)
                return content.decode("utf-8-sig")
    except (httpx.HTTPError, UnicodeError):
        raise ApiError("network", MESSAGES["network"], 503) from None


class AttendanceCalendarSync:
    def __init__(self, session_factory: sessionmaker[Session], *, enabled: bool = True):
        self.session_factory = session_factory
        self.enabled = enabled

    def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        with self.session_factory() as database:
            states = _read(database)
            complete = {item["year"] for item in list_calendars(database) if item["complete"]}
        local = now.astimezone(SHANGHAI)
        target = local.year + 1 if local.year < 9999 else local.year
        if target not in states:
            state = _new(target)
            if target not in complete and local.month < 11:
                state.update(
                    status="scheduled",
                    nextAttemptAt=datetime(local.year, 11, 1, tzinfo=SHANGHAI)
                    .astimezone(UTC)
                    .isoformat(),
                )
            states[target] = state
        for year in _targets(now):
            states.setdefault(year, _new(year))
        items = []
        for year, value in sorted(states.items(), reverse=True):
            value = dict(value)
            if year in complete:
                value.update(status="completed", nextAttemptAt=None, errorCode="")
            elif (
                value["status"] == "running" and datetime.fromisoformat(value["leaseUntil"]) <= now
            ):
                value.update(status="retry", errorCode="storage", nextAttemptAt=now.isoformat())
            if (
                year not in complete
                and not _automatic_window_open(year, now)
                and value["status"] not in {"paused", "running"}
            ):
                value.update(status="scheduled", nextAttemptAt=_window_start(year).isoformat())
            items.append(
                {
                    key: value[key]
                    for key in (
                        "year",
                        "status",
                        "lastAttemptAt",
                        "nextAttemptAt",
                        "completedAt",
                        "errorCode",
                    )
                }
                | {"message": MESSAGES.get(value["errorCode"], "")}
            )
        return {
            "enabled": self.enabled,
            "startMonth": 11,
            "intervalDays": 5,
            "source": "holiday_cn",
            "items": items,
        }

    def _prepare(self, now: datetime) -> list[int]:
        with self.session_factory() as database:
            database.execute(text("BEGIN IMMEDIATE"))
            states = _read(database)
            for year in _targets(now):
                if year not in states:
                    states[year] = _new(year)
                    _put(database, states[year])
            database.commit()
        return sorted(
            year
            for year, value in states.items()
            if value["status"] not in {"completed", "paused"} and _automatic_window_open(year, now)
        )

    def _claim(self, year: int, now: datetime, force: bool) -> str | None:
        with self.session_factory() as database:
            database.execute(text("BEGIN IMMEDIATE"))
            state = _read(database).get(year, _new(year))
            if any(item["year"] == year and item["complete"] for item in list_calendars(database)):
                state.update(status="completed", nextAttemptAt=None, leaseToken="", leaseUntil=None)
                _put(database, state)
                database.commit()
                return None
            if state["leaseUntil"] and datetime.fromisoformat(state["leaseUntil"]) > now:
                return None
            if not force and not _automatic_window_open(year, now):
                return None
            if not force and (
                state["status"] in {"completed", "paused"}
                or state["status"] != "running"
                and state["nextAttemptAt"]
                and datetime.fromisoformat(state["nextAttemptAt"]) > now
            ):
                return None
            token = uuid4().hex
            state.update(
                status="running",
                lastAttemptAt=now.isoformat(),
                errorCode="",
                leaseToken=token,
                leaseUntil=(now + LEASE).isoformat(),
                nextAttemptAt=(now + INTERVAL).isoformat(),
            )
            _put(database, state)
            database.commit()
            return token

    def _finish(
        self, year: int, token: str, content: str | None, error: str, now: datetime
    ) -> None:
        with self.session_factory() as database:
            database.execute(text("BEGIN IMMEDIATE"))
            state = _read(database).get(year)
            if (
                not state
                or state["leaseToken"] != token
                or datetime.fromisoformat(state["leaseUntil"]) <= now
            ):
                return
            complete = any(
                item["year"] == year and item["complete"] for item in list_calendars(database)
            )
            if content is not None and not complete:
                plan = preview_notice(database, content)
                if (
                    plan["sourceFormat"] != "holiday_cn"
                    or plan["year"] != year
                    or not plan["canSave"]
                ):
                    error = "invalid"
                else:
                    apply_notice(
                        database,
                        NoticeApply(
                            text=content,
                            previewDigest=plan["previewDigest"],
                            expectedRevisions=plan["expectedRevisions"],
                        ),
                        commit=False,
                    )
                    complete = True
            state.update(
                status="completed" if complete else "retry",
                leaseToken="",
                leaseUntil=None,
                errorCode="" if complete else error or "storage",
                nextAttemptAt=None
                if complete
                else (
                    now + INTERVAL if _automatic_window_open(year, now) else _window_start(year)
                ).isoformat(),
            )
            if complete:
                state["completedAt"] = now.isoformat()
            _put(database, state)
            database.commit()

    async def check(self, year: int, *, force: bool = False, now: datetime | None = None) -> dict:
        started = now or datetime.now(UTC)
        token = await asyncio.to_thread(self._claim, year, started, force)
        if token:
            content, error = None, ""
            try:
                async with asyncio.timeout(20):
                    content = await fetch_holiday_cn(year)
            except ApiError as failure:
                error = failure.code if failure.code in MESSAGES else "network"
            except TimeoutError:
                error = "network"
            try:
                await asyncio.to_thread(
                    self._finish, year, token, content, error, now or datetime.now(UTC)
                )
            except Exception:
                # Leave the durable lease recoverable; never log response contents.
                logger.warning(
                    "Attendance calendar save failed", extra={"error_code": "CALENDAR_SYNC_STORAGE"}
                )
        return await asyncio.to_thread(self.status, now)

    async def tick(self, now: datetime | None = None) -> None:
        observed = now or datetime.now(UTC)
        for year in await asyncio.to_thread(self._prepare, observed):
            await self.check(year, now=now)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.warning(
                    "Attendance calendar scheduler retry",
                    extra={"error_code": "CALENDAR_SYNC_RETRY"},
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except TimeoutError:
                pass
