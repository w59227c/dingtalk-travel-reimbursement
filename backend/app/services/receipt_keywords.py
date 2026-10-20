from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.categories import CATEGORY_BY_ID, ExpenseCategory
from app.models.receipt_keyword import ReceiptKeywordMapping
from app.ocr.types import ReceiptKeywordRule

MAX_RECEIPT_KEYWORD_MAPPINGS = 500


def normalize_receipt_keyword(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split()).casefold()


def load_receipt_keyword_rules(database: Session) -> tuple[ReceiptKeywordRule, ...]:
    rows = database.scalars(select(ReceiptKeywordMapping).order_by(ReceiptKeywordMapping.id)).all()
    rules: list[ReceiptKeywordRule] = []
    for row in rows:
        try:
            category = ExpenseCategory(row.category_id)
        except ValueError:
            continue
        metadata = CATEGORY_BY_ID.get(category)
        if metadata is None or not metadata.manual_selectable or category is ExpenseCategory.OTHER:
            continue
        transport_type = row.transport_type if category is ExpenseCategory.LOCAL_TRANSPORT else None
        rules.append(
            ReceiptKeywordRule(
                keyword=row.normalized_keyword,
                category=category,
                transport_type=transport_type
                if transport_type in {"ride_hailing", "taxi"}
                else None,
            )
        )
    return tuple(rules)
