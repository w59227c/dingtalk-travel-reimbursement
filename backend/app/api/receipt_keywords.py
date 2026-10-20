from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.domain.categories import CATEGORY_BY_ID, ExpenseCategory
from app.models.receipt_keyword import ReceiptKeywordMapping
from app.schemas.common import success
from app.services.receipt_keywords import (
    MAX_RECEIPT_KEYWORD_MAPPINGS,
    normalize_receipt_keyword,
)
from app.services.sessions import CurrentSession, require_admin, require_admin_csrf

router = APIRouter(tags=["receipt keyword mappings"])


class ReceiptKeywordWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    keyword: str = Field(min_length=2, max_length=100)
    category_id: str = Field(alias="categoryId", min_length=1, max_length=64)
    transport_type: Literal["other", "ride_hailing", "taxi"] | None = Field(
        default=None, alias="transportType"
    )

    @model_validator(mode="after")
    def validate_transport_type(self) -> ReceiptKeywordWrite:
        if self.transport_type is not None and self.category_id != ExpenseCategory.LOCAL_TRANSPORT:
            raise ValueError("transport type is only available for local transport keywords")
        return self

    @field_validator("keyword")
    @classmethod
    def normalize_keyword(cls, value: str) -> str:
        normalized = " ".join(value.replace("\x00", " ").split())
        if len(normalized) < 2:
            raise ValueError("keyword must contain at least two characters")
        return normalized

    @field_validator("category_id")
    @classmethod
    def validate_category(cls, value: str) -> str:
        try:
            category = ExpenseCategory(value)
        except ValueError:
            raise ValueError("unknown expense category") from None
        metadata = CATEGORY_BY_ID[category]
        if not metadata.manual_selectable or category is ExpenseCategory.OTHER:
            raise ValueError("category cannot be used by keyword mappings")
        return category.value


def _mapping_data(mapping: ReceiptKeywordMapping) -> dict[str, object]:
    category = ExpenseCategory(mapping.category_id)
    data: dict[str, object] = {
        "id": mapping.id,
        "keyword": mapping.keyword,
        "categoryId": category.value,
        "categoryName": CATEGORY_BY_ID[category].name,
    }
    if category is ExpenseCategory.LOCAL_TRANSPORT:
        data["transportType"] = mapping.transport_type or "other"
    return data


def _save(database: Session, mapping: ReceiptKeywordMapping) -> None:
    try:
        database.add(mapping)
        database.commit()
        database.refresh(mapping)
    except IntegrityError:
        database.rollback()
        raise ApiError("RECEIPT_KEYWORD_EXISTS", "该关键词已存在", 409) from None


@router.get("/admin/receipt-keywords")
def list_receipt_keywords(
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin)],
) -> dict[str, object]:
    mappings = database.scalars(
        select(ReceiptKeywordMapping).order_by(
            ReceiptKeywordMapping.category_id,
            ReceiptKeywordMapping.id,
        )
    ).all()
    return success([_mapping_data(mapping) for mapping in mappings])


@router.post("/admin/receipt-keywords", status_code=201)
def create_receipt_keyword(
    body: ReceiptKeywordWrite,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    count = database.scalar(select(func.count()).select_from(ReceiptKeywordMapping)) or 0
    if count >= MAX_RECEIPT_KEYWORD_MAPPINGS:
        raise ApiError("RECEIPT_KEYWORD_LIMIT", "分类关键词已达到 500 条上限", 409)
    mapping = ReceiptKeywordMapping(
        keyword=body.keyword,
        normalized_keyword=normalize_receipt_keyword(body.keyword),
        category_id=body.category_id,
        transport_type=body.transport_type if body.transport_type != "other" else None,
    )
    _save(database, mapping)
    return success(_mapping_data(mapping))


@router.put("/admin/receipt-keywords/{mapping_id}")
def update_receipt_keyword(
    mapping_id: int,
    body: ReceiptKeywordWrite,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    mapping = database.get(ReceiptKeywordMapping, mapping_id)
    if mapping is None:
        raise ApiError("RECEIPT_KEYWORD_NOT_FOUND", "分类关键词不存在", 404)
    mapping.keyword = body.keyword
    mapping.normalized_keyword = normalize_receipt_keyword(body.keyword)
    mapping.category_id = body.category_id
    mapping.transport_type = body.transport_type if body.transport_type != "other" else None
    _save(database, mapping)
    return success(_mapping_data(mapping))


@router.delete("/admin/receipt-keywords/{mapping_id}")
def delete_receipt_keyword(
    mapping_id: int,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    mapping = database.get(ReceiptKeywordMapping, mapping_id)
    if mapping is None:
        raise ApiError("RECEIPT_KEYWORD_NOT_FOUND", "分类关键词不存在", 404)
    database.delete(mapping)
    database.commit()
    return success({})
