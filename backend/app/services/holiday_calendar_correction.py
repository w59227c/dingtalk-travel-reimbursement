"""Explicit administrator corrections, sharing the strict annual JSON validation.

Corrections never make a failed automatic parse saveable implicitly. They form
a new preview, bound to both the original pasted source and the corrected ranges.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.primitives import StrictCalendarDate
from app.services.holiday_calendar_import import parse_annual_json, parse_calendar_content
from app.services.holiday_notice_parser import ParsedNotice


class CorrectionRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    startDate: StrictCalendarDate
    endDate: StrictCalendarDate
    working: bool

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip() or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        ):
            raise ValueError("请填写有效的节日名称")
        return value.strip()


class CalendarCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    year: int = Field(ge=1, le=9999)
    ranges: list[CorrectionRange] = Field(min_length=1, max_length=80)


def corrected_calendar(text: str, correction: CalendarCorrection) -> ParsedNotice:
    original = parse_calendar_content(text)
    result = ParsedNotice(year=correction.year, source_format="manual")
    if not text.strip() or re.fullmatch(r"https?://\S+", text.strip()):
        result.issue("请保留完整来源内容，不能仅提供网址或空白内容")
        return result
    if len(text.encode("utf-8")) > 64 * 1024:
        result.issue("来源内容超过 64 KB，请缩短到完整年度安排正文")
        return result
    days = []
    for index, item in enumerate(correction.ranges, 1):
        length = (item.endDate - item.startDate).days + 1
        if not 1 <= length <= 32:
            result.issue(f"第 {index} 段日期倒序或超过 32 天，请拆分并核对")
            continue
        if any(abs(day.year - correction.year) > 1 for day in (item.startDate, item.endDate)):
            result.issue(f"第 {index} 段日期与适用年份相差超过一年")
            continue
        if len(days) + length > 400:
            result.issue("全年放假与补班日期合计最多 400 天，请核对是否选入了普通工作周")
            return result
        days.extend(
            {
                "date": (item.startDate + timedelta(days=offset)).isoformat(),
                "name": item.name,
                "isOffDay": not item.working,
            }
            for offset in range(length)
        )
    if result.errors:
        return result
    # Reuse date, duplicate/conflict, annual coverage, and adjacent-year checks.
    result = parse_annual_json(
        json.dumps({"year": correction.year, "days": days}, ensure_ascii=False)
    )
    result.source_format = "manual"
    result.title = f"{correction.year} 年年度安排（手动核对）"
    result.warnings = []
    for issue in result.errors:
        if issue["message"].startswith("缺少全年节日安排："):
            issue["message"] = (
                issue["message"].split("；", 1)[0] + "；请补齐这些节日的放假日期后重新生成预览"
            )
        elif issue["message"] == "JSON 存在重复或冲突日期，请核对完整数据":
            issue["message"] = "存在重复或冲突日期，请修改或删除重叠的日期安排"
        elif issue["message"] == "JSON 未包含适用年份的日期":
            issue["message"] = "修正内容未包含适用年份的日期，请核对年份与各段日期"
    result.issue("此安排经过手动修正，请对照来源核对全年放假及补班日期后保存。", warning=True)

    def source_key(name: str) -> frozenset[str]:
        return frozenset(re.split(r"[、,，和及与]", name))

    sources = {source_key(section["name"]): section["source"] for section in original.sections}
    for section in result.sections:
        section["source"] = sources.get(
            source_key(section["name"]), "本项没有可用的自动识别原文，请返回完整来源内容核对。"
        )
    return result
