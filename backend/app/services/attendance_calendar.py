"""Offline notice ingestion and atomic, revision-checked calendar composition.

Each annual notice owns its contributions, including dates in adjacent years.
Reimporting or deleting it cannot discard a different annual notice's dates.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models.setting import Setting
from app.schemas.primitives import StrictCalendarDate
from app.services.holiday_calendar_correction import CalendarCorrection, corrected_calendar
from app.services.holiday_calendar_import import SOURCE_LABELS, parse_calendar_content
from app.services.holiday_notice_parser import MAX_NOTICE_CHARS, ParsedNotice

PREFIX = "attendance_calendar:"
MAX_BODY_BYTES = 128 * 1024


class CalendarDay(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    date: StrictCalendarDate
    working: bool
    name: str = Field(default="", max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError("说明不能包含控制字符")
        return value


class NoticeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(max_length=MAX_NOTICE_CHARS)
    correction: CalendarCorrection | None = None


class NoticeApply(NoticeInput):
    previewDigest: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    expectedRevisions: dict[str, str | None] = Field(max_length=3)


def _conflict() -> ApiError:
    return ApiError(
        "ATTENDANCE_CALENDAR_CONFLICT",
        "相关年度日历已变更，原文仍保留；请重新解析并核对差异后保存",
        409,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _days(values: list, year: int) -> list[dict]:
    checked = [CalendarDay.model_validate(value) for value in values]
    if len(checked) > 366 or len({day.date for day in checked}) != len(checked):
        raise ValueError
    if any(day.date.year != year for day in checked):
        raise ValueError
    return [day.model_dump(mode="json") for day in sorted(checked, key=lambda day: day.date)]


def _compose(record: dict) -> dict:
    values = {day["date"]: day for day in record["legacyDays"]}
    for source in sorted(record["noticeParts"], key=int):
        values.update((day["date"], day) for day in record["noticeParts"][source]["days"])
    record["days"] = sorted(values.values(), key=lambda day: day["date"])
    record["complete"] = record["legacyComplete"] or str(record["year"]) in record["noticeParts"]
    return record


def _stored(key: str, raw: str) -> dict:
    try:
        data = json.loads(raw)
        year = data["year"]
        if type(year) is not int or not 1 <= year <= 9999 or key != f"{PREFIX}{year}":
            raise ValueError
        if not isinstance(data["revision"], str) or not 1 <= len(data["revision"]) <= 36:
            raise ValueError
        datetime.fromisoformat(data["updatedAt"])
        old_days = _days(data["days"], year)
        if "noticeParts" not in data:  # Previous offline calendars remain valid.
            data.update(legacyDays=old_days, legacyComplete=True, noticeParts={})
        data["legacyDays"] = _days(data["legacyDays"], year)
        if type(data["legacyComplete"]) is not bool or not isinstance(data["noticeParts"], dict):
            raise ValueError
        if len(data["noticeParts"]) > 3:
            raise ValueError
        for source, part in data["noticeParts"].items():
            if (
                not source.isdecimal()
                or str(int(source)) != source
                or not 1 <= int(source) <= 9999
                or abs(int(source) - year) > 1
            ):
                raise ValueError
            if not isinstance(part["title"], str) or len(part["title"]) > 100:
                raise ValueError
            part["days"] = _days(part["days"], year)
        _compose(data)
        if data["days"] != old_days:
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ApiError(
            "ATTENDANCE_CALENDAR_INVALID", "已保存的出勤日历无效，请联系管理员检查配置", 500
        ) from None


def _records(database: Session) -> dict[int, tuple[str, dict]]:
    rows = database.execute(
        select(Setting.key, Setting.value)
        .where(Setting.key.startswith(PREFIX, autoescape=True))
        .order_by(Setting.key)
    )
    return {data["year"]: (raw, data) for key, raw in rows for data in [_stored(key, raw)]}


def _public(record: dict) -> dict:
    own = record["noticeParts"].get(str(record["year"]))
    return {key: record[key] for key in ("year", "revision", "updatedAt", "days", "complete")} | {
        "noticeTitle": own["title"] if own else "",
        "sourceYears": sorted(int(source) for source in record["noticeParts"]),
        "legacy": record["legacyComplete"],
    }


def list_calendars(database: Session) -> list[dict]:
    return [_public(data) for _, data in _records(database).values()]


def attendance_calendar(database: Session) -> dict[str, object]:
    calendars = list_calendars(database)
    return {
        "years": [item["year"] for item in calendars if item["complete"]],
        "days": {
            day["date"]: {"working": day["working"], "name": day["name"]}
            for item in calendars
            for day in item["days"]
        },
    }


def _plan(parsed: ParsedNotice, records: dict) -> dict[int, dict]:
    source = str(parsed.year)
    grouped: dict[int, list] = {parsed.year: []}
    for day in parsed.days:
        grouped.setdefault(day.date.year, []).append(day.as_dict())
    affected = set(grouped) | {
        year for year, (_, data) in records.items() if source in data["noticeParts"]
    }
    plan = {}
    for year in sorted(affected):
        value = (
            copy.deepcopy(records[year][1])
            if year in records
            else {
                "year": year,
                "revision": "",
                "updatedAt": "",
                "legacyDays": [],
                "legacyComplete": False,
                "noticeParts": {},
            }
        )
        value["noticeParts"].pop(source, None)
        if year == parsed.year:
            value["legacyDays"] = []
            value["legacyComplete"] = False
        if year in grouped:
            value["noticeParts"][source] = {"title": parsed.title, "days": grouped[year]}
        plan[year] = _compose(value)
    return plan


def _same(old: dict | None, new: dict) -> bool:
    if old is None:
        return not new["noticeParts"] and not new["legacyComplete"]
    return all(old[key] == new[key] for key in ("legacyDays", "legacyComplete", "noticeParts"))


def _digest(text: str, correction: CalendarCorrection | None = None) -> str:
    content = (
        text
        if correction is None
        else _json({"text": text, "correction": correction.model_dump(mode="json")})
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def preview_notice(
    database: Session, text: str, correction: CalendarCorrection | None = None
) -> dict:
    parsed = (
        parse_calendar_content(text) if correction is None else corrected_calendar(text, correction)
    )
    response = {
        "year": parsed.year,
        "title": parsed.title,
        "sourceFormat": parsed.source_format,
        "sourceLabel": SOURCE_LABELS[parsed.source_format],
        "canSave": parsed.can_save,
        "sections": parsed.sections,
        "errors": parsed.errors,
        "warnings": parsed.warnings,
        "previewDigest": _digest(text, correction),
        "expectedRevisions": {},
        "changes": [],
        "unchanged": False,
    }
    if not parsed.can_save:
        return response
    records = _records(database)
    plan = _plan(parsed, records)
    changes = []
    for year, value in plan.items():
        old = records.get(year, (None, None))[1]
        previous = {day["date"]: day for day in old["days"]} if old else {}
        after = {day["date"]: day for day in value["days"]}
        changes.append(
            {
                "year": year,
                "complete": value["complete"],
                "days": value["days"],
                "mode": "annual" if year == parsed.year else "adjacent",
                "added": [after[key] for key in sorted(after.keys() - previous.keys())],
                "removed": [previous[key] for key in sorted(previous.keys() - after.keys())],
                "modified": [
                    {"before": previous[key], "after": after[key]}
                    for key in sorted(after.keys() & previous.keys())
                    if previous[key] != after[key]
                ],
                "changed": not _same(old, value),
            }
        )
        response["expectedRevisions"][str(year)] = old["revision"] if old else None
    response["changes"] = changes
    response["unchanged"] = not any(change["changed"] for change in changes)
    return response


def _write_plan(
    database: Session, records: dict, plan: dict[int, dict], *, commit: bool = True
) -> None:
    try:
        for year, value in plan.items():
            raw, old = records.get(year, (None, None))
            if _same(old, value):
                continue
            key = f"{PREFIX}{year}"
            empty = not value["noticeParts"] and not value["legacyComplete"]
            value.update(revision=str(uuid4()), updatedAt=datetime.now(UTC).isoformat())
            if raw is None:
                if not empty:
                    database.execute(insert(Setting).values(key=key, value=_json(value)))
            else:
                statement = delete(Setting) if empty else update(Setting).values(value=_json(value))
                result = database.execute(
                    statement.where(Setting.key == key, Setting.value == raw),
                    execution_options={"synchronize_session": False},
                )
                if result.rowcount != 1:
                    raise _conflict()
        if commit:
            database.commit()
    except IntegrityError:
        database.rollback()
        raise _conflict() from None
    except Exception:
        database.rollback()
        raise


def apply_notice(database: Session, body: NoticeApply, *, commit: bool = True) -> dict:
    parsed = (
        parse_calendar_content(body.text)
        if body.correction is None
        else corrected_calendar(body.text, body.correction)
    )
    if not parsed.can_save or body.previewDigest != _digest(body.text, body.correction):
        raise ApiError("ATTENDANCE_NOTICE_INVALID", "内容或预览已变化，请重新解析并核对后保存", 422)
    records = _records(database)
    plan = _plan(parsed, records)
    unchanged = all(
        _same(records.get(year, (None, None))[1], value) for year, value in plan.items()
    )
    # A lost successful response may be retried with the same preview safely.
    if not unchanged and body.expectedRevisions != {
        str(year): records[year][1]["revision"] if year in records else None for year in plan
    }:
        raise _conflict()
    if not unchanged:
        _write_plan(database, records, plan, commit=commit)
    return {"items": list_calendars(database), "unchanged": unchanged, "year": parsed.year}


def delete_calendar(database: Session, year: int, revision: str) -> None:
    records = _records(database)
    old = records.get(year, (None, None))[1]
    if old is None or old["revision"] != revision:
        raise _conflict()
    if not old["complete"]:
        raise ApiError(
            "ATTENDANCE_CALENDAR_PARTIAL", "这些日期来自相邻年度通知，请在对应通知年度删除", 422
        )
    plan = {}
    for target, (_, data) in records.items():
        if target == year or str(year) in data["noticeParts"]:
            value = copy.deepcopy(data)
            value["noticeParts"].pop(str(year), None)
            if target == year:
                value.update(legacyDays=[], legacyComplete=False)
            plan[target] = _compose(value)
    # Pause this year's automatic job in the same transaction as deletion.
    # A response already in flight must not restore a deliberately removed year.
    from app.services.attendance_calendar_sync import pause_year

    _write_plan(database, records, plan, commit=False)
    pause_year(database, year)
    database.commit()
