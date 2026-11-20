from __future__ import annotations

import pytest

from app.integrations.dingtalk.approval_summary import approval_summary_fields


def test_summary_prioritizes_useful_fields_and_skips_empty_or_non_display_data():
    values = [
        {"name": "其他", "value": "次要信息"},
        {"name": "姓名", "value": "合成员工"},
        {"name": "说明", "componentType": "TextNote", "value": "固定提示"},
        {"name": "附件", "componentType": "DDAttachment", "value": "合成附件"},
        {"name": "表格", "componentType": "TableField", "value": '[{"secret":"synthetic"}]'},
        {"name": "开始时间", "componentType": "DDDateField", "value": "2026-11-17 00:00"},
        {"name": "申请事项", "componentType": "TextareaField", "value": "办公耗材\n采购"},
        {"name": "预算项目", "componentType": "DDSelectField", "value": "SYNTHETIC-001 合成项目"},
        {"name": "所属公司", "componentType": "DDSelectField", "value": "示例公司"},
        {"name": "所属公司", "componentType": "DDSelectField", "value": "示例公司"},
        {"name": "变更链接", "value": "https://example.test/synthetic"},
        {"name": "备注", "value": None},
    ]
    fields = approval_summary_fields(values)
    assert [(field.label, field.value) for field in fields] == [
        ("所属公司", "示例公司"),
        ("预算项目", "SYNTHETIC-001 合成项目"),
        ("申请事项", "办公耗材 采购"),
    ]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "bad",
        {},
        [None, 1, {}, {"name": None, "value": "x"}],
        [{"name": "JSON", "value": '{"nested":"data"}'}],
        [{"name": "坏 JSON", "value": "[" * 1100}],
        [{"name": "HTML", "value": "<script>synthetic</script>"}],
        [{"name": "URL", "value": "https://example.test"}],
        [{"name": "未知", "componentType": {}, "value": "x"}],
    ],
)
def test_optional_summary_failures_never_break_eligibility(value):
    assert approval_summary_fields(value) == ()


def test_summary_formats_choices_bounds_text_and_preserves_distinct_repeated_labels():
    fields = approval_summary_fields(
        [
            {"name": "详情", "value": '["合成事项一","合成事项二"]'},
            {"name": "详情", "value": "很长" * 300},
            {"name": "详情", "value": "很长" * 300},
            {"name": "其他", "value": "正常值"},
        ]
    )
    assert fields[0].value == "合成事项一、合成事项二"
    assert len(fields[1].value) == 160
    assert fields[1].value.endswith("…")
    assert len(fields) == 3


def test_summary_scan_is_bounded():
    assert approval_summary_fields([None] * 128 + [{"name": "事项", "value": "超出范围"}]) == ()


def test_summary_prefers_explicit_choice_label_and_falls_back_on_invalid_extensions():
    fields = approval_summary_fields(
        [
            {
                "name": "所属公司",
                "componentType": "DDSelectField",
                "value": "stable-key",
                "extValue": '{"label":"示例公司","key":"stable-key"}',
            },
            {
                "name": "项目",
                "componentType": "DDSelectField",
                "value": "合成项目",
                "extValue": "[" * 1100,
            },
        ]
    )
    assert [field.value for field in fields] == ["示例公司", "合成项目"]
