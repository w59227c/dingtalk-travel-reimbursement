"""Offline adapters for pasted annual notices and two public JSON formats.

Only explicit dates and strict Boolean day types control the calendar. URLs,
schemas, wage multipliers and other metadata never trigger network requests.
"""

from __future__ import annotations

import json
import re
from datetime import date

from app.services.holiday_notice_parser import (
    FESTIVALS,
    MAX_NOTICE_CHARS,
    NoticeDay,
    ParsedNotice,
    parse_holiday_notice,
)

SOURCE_LABELS = {
    "notice": "政府年度通知",
    "timor": "提莫节假日 API",
    "holiday_cn": "holiday-cn 年度数据",
    "unknown": "未识别格式",
    "manual": "手动核对",
}
_ORDER = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")
_NAMES = {
    "元旦": ("元旦",),
    "春节": ("春节", "除夕"),
    "清明节": ("清明",),
    "劳动节": ("劳动", "五一"),
    "端午节": ("端午",),
    "中秋节": ("中秋",),
    "国庆节": ("国庆",),
}


def _families(name: str) -> set[str]:
    values = {family for family, names in _NAMES.items() if any(token in name for token in names)}
    if re.fullmatch(r"(?:大年)?初[一二三四五六七八九十]", name) or name == "大年三十":
        values.add("春节")
    return values


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复字段或日期，请复制完整年度数据")
        result[key] = value
    return result


def _invalid_constant(_value: str):
    raise ValueError("JSON 不能包含 NaN 或 Infinity")


def _entry(value: object, field: str) -> NoticeDay:
    if not isinstance(value, dict) or type(value.get(field)) is not bool:
        raise ValueError(f"日期条目的 {field} 必须为 true 或 false，不能省略或使用字符串")
    raw = value.get("date")
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError("每条记录都需要 YYYY-MM-DD 格式的完整 date 日期")
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise ValueError("JSON 中存在无效公历日期") from None
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise ValueError("每条记录都需要 1—80 字的节日名称 name")
    if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in name):
        raise ValueError("节日名称不能包含控制字符")
    return NoticeDay(day, not value[field], name.strip())


def parse_annual_json(text: str) -> ParsedNotice:
    """Parse bounded annual JSON; callers enforce source or structured-input limits."""
    result = ParsedNotice(source_format="unknown")
    try:
        data = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(data, dict):
            raise ValueError("请复制完整年度 JSON 对象，不能只复制日期数组或单条记录")
        if "holiday" in data and "days" in data:
            raise ValueError("同时出现两种数据格式，请每次只粘贴一个来源的完整年度 JSON")
        if "holiday" in data or "code" in data:
            result.source_format = "timor"
            if type(data.get("code")) is not int or data["code"] != 0:
                raise ValueError("提莫接口未返回成功数据，请更换年份或使用其他来源")
            values = data.get("holiday")
            if not isinstance(values, dict) or not 1 <= len(values) <= 366:
                raise ValueError(
                    "提莫年度 holiday 数据为空或无效，请复制带年份和末尾斜杠的年度接口结果"
                )
            days = []
            for key, value in values.items():
                day = _entry(value, "holiday")
                if key != day.date.isoformat()[5:]:
                    raise ValueError("提莫日期键必须为 MM-DD，且与条目的 date 一致")
                days.append(day)
            years = {day.date.year for day in days}
            if len(years) != 1:
                raise ValueError("提莫数据混有多个年份，无法确定年度；请使用单个完整年度接口")
            result.year = years.pop()
            if "year" in data and (type(data["year"]) is not int or data["year"] != result.year):
                raise ValueError("提莫数据的年份与日期不一致")
        elif "days" in data or "year" in data:
            result.source_format = "holiday_cn"
            year, values = data.get("year"), data.get("days")
            if type(year) is not int or not 1 <= year <= 9999:
                raise ValueError("holiday-cn 需要有效整数 year，请复制完整年度 JSON")
            if not isinstance(values, list) or not 1 <= len(values) <= 400:
                raise ValueError("holiday-cn 的 days 必须为非空年度日期列表（最多 400 条）")
            result.year = year
            days = [_entry(value, "isOffDay") for value in values]
            if any(abs(day.date.year - year) > 1 for day in days):
                raise ValueError("holiday-cn 日期与 year 相差超过一年")
        else:
            raise ValueError(
                "未识别 JSON 格式；支持提莫 code/holiday 或 holiday-cn year/days 年度数据"
            )
        if len({day.date for day in days}) != len(days):
            raise ValueError("JSON 存在重复或冲突日期，请核对完整数据")
        if not any(day.date.year == result.year for day in days):
            raise ValueError("JSON 未包含适用年份的日期")
        covered = set().union(*(_families(day.name) for day in days if not day.working))
        missing = FESTIVALS - covered
        result.days = sorted(days, key=lambda day: day.date)
        result.title = f"{SOURCE_LABELS[result.source_format]}（{result.year} 年）"
        groups: dict[str, list[NoticeDay]] = {}
        for day in result.days:
            families = _families(day.name)
            name = "、".join(family for family in _ORDER if family in families) or day.name
            groups.setdefault(name, []).append(day)
        flag = "holiday" if result.source_format == "timor" else "isOffDay"
        for name, group in groups.items():
            source = [
                {"date": day.date.isoformat(), "name": day.name, flag: not day.working}
                for day in group
            ]
            result.sections.append(
                {
                    "name": name,
                    "source": json.dumps(source, ensure_ascii=False, indent=2),
                    "restDates": [day.date.isoformat() for day in group if not day.working],
                    "workDates": [day.date.isoformat() for day in group if day.working],
                }
            )
        if missing:
            raise ValueError(
                "缺少全年节日安排："
                + "、".join(sorted(missing))
                + "；请复制完整年度 JSON，不能使用月度或单日数据"
            )
        result.issue(
            "第三方数据可能与政府通知存在差异，请核对全年放假和补班日期后保存。", warning=True
        )
    except (ValueError, RecursionError) as error:
        message = (
            "JSON 不完整或格式无效，请从第一个 { 复制到最后一个 }"
            if isinstance(error, (json.JSONDecodeError, RecursionError))
            else str(error)
        )
        result.issue(message)
    return result


def parse_calendar_content(text: str) -> ParsedNotice:
    """Detect format from pasted content; no caller-selected format or URL fetch."""
    if len(text) > MAX_NOTICE_CHARS or len(text.encode("utf-8")) > 64 * 1024:
        result = ParsedNotice(source_format="unknown")
        result.issue("内容过长，请仅粘贴年度通知或年度 JSON（最多 20000 字、64 KB）")
        return result
    cleaned = text.strip().lstrip("\ufeff").strip()
    if cleaned.startswith(("{", "[")):
        return parse_annual_json(cleaned)
    if cleaned.startswith(("http://", "https://")):
        result = ParsedNotice(source_format="unknown")
        result.issue("请打开网址后复制完整通知或 JSON 内容，不能只粘贴网址；系统不会联网抓取")
        return result
    return parse_holiday_notice(cleaned)
