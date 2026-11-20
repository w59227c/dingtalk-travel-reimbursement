"""Optional, bounded display fields for approvals from arbitrary templates."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApprovalSummaryField:
    label: str
    value: str


_TEXT_TYPES = frozenset(
    {
        "TextField",
        "TextareaField",
        "DDSelectField",
        "DDMultiSelectField",
        "DDDateField",
        "DDDateRangeField",
        "MoneyField",
        "NumberField",
        "DDNumberField",
    }
)
_REPEATED_LABELS = frozenset(
    {
        "姓名",
        "申请人",
        "发起人",
        "申请人姓名",
        "员工姓名",
        "报销人",
        "报销人姓名",
        "工号",
        "员工工号",
    }
)
_PRIORITIES = (
    ("公司", "主体"),
    ("项目", "预算"),
    ("事项", "事由", "用途", "类型", "内容", "原因", "名称", "说明", "详情"),
    ("日期", "时间"),
)


def _text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    value = " ".join(value.split())
    if not value or re.search(r"https?://|<[^>]+>", value, re.IGNORECASE):
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _display_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith(("[", "{")):
        if len(value) > 4096:
            return None
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return None
        if not isinstance(decoded, list) or len(decoded) > 20:
            return None
        values = [_text(item, limit=160) for item in decoded]
        if not values or any(item is None for item in values):
            return None
        value = "、".join(item for item in values if item is not None)
    return _text(value, limit=160)


def approval_summary_fields(raw_values: object) -> tuple[ApprovalSummaryField, ...]:
    """Ignore unsupported display data without changing approval eligibility."""
    if not isinstance(raw_values, list):
        return ()
    candidates: list[tuple[int, ApprovalSummaryField]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_values[:128]:
        if not isinstance(raw, dict):
            continue
        component_type = raw.get("componentType")
        if component_type is not None and (
            not isinstance(component_type, str) or component_type not in _TEXT_TYPES
        ):
            continue
        label = _text(raw.get("name"), limit=40)
        value = _display_value(raw.get("value"))
        if component_type == "DDSelectField":
            extension = raw.get("extValue")
            if isinstance(extension, str) and len(extension) <= 4096:
                try:
                    choice = json.loads(extension)
                except (ValueError, RecursionError):
                    choice = None
                if isinstance(choice, dict):
                    value = _text(choice.get("label"), limit=160) or value
        if not label or not value or label in _REPEATED_LABELS or "链接" in label:
            continue
        if (label, value) in seen:
            continue
        seen.add((label, value))
        priority = next(
            (i for i, words in enumerate(_PRIORITIES) if any(word in label for word in words)),
            len(_PRIORITIES),
        )
        candidates.append((priority, ApprovalSummaryField(label, value)))
    candidates.sort(key=lambda candidate: candidate[0])
    return tuple(field for _, field in candidates[:3])
