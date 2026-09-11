from __future__ import annotations

from app.domain.categories import ExpenseCategory
from app.ocr.parsers import ReceiptParserRegistry
from app.ocr.types import OcrLine, ParseContext, ReceiptKeywordRule


def _invoice(item: str, amount: str = "39.90") -> str:
    return f"电子发票\n开票日期：2026-08-31\n项目名称 {item}\n价税合计（小写）：￥{amount}"


def test_new_defaults_are_precise_and_explicit_empty_or_overridden_rules_win():
    context = ParseContext(reference_year=2026)
    for item in ("收派服务费", "床品", "床笠", "床单", "被套"):
        lines = [OcrLine(line, 1) for line in _invoice(item).splitlines()]
        assert (
            ReceiptParserRegistry().parse(lines, context).category
            == ExpenseCategory.EMPLOYEE_WELFARE
        )
        assert (
            ReceiptParserRegistry(keyword_rules=()).parse(lines, context).category
            == ExpenseCategory.OTHER
        )
        custom = (ReceiptKeywordRule(item, ExpenseCategory.OFFICE),)
        assert (
            ReceiptParserRegistry(keyword_rules=custom).parse(lines, context).category
            == ExpenseCategory.OFFICE
        )
    for item in ("三件套", "服务费", "商品", "服装", "日用品", "制卡费"):
        lines = [OcrLine(line, 1) for line in _invoice(item).splitlines()]
        assert ReceiptParserRegistry().parse(lines, context).category == ExpenseCategory.OTHER
