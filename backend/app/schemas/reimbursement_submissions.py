from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.money import MAX_REIMBURSEMENT_AMOUNT, quantize_money
from app.schemas.primitives import DecimalString


class SubmitReimbursementRequest(BaseModel):
    """Identify the exact reviewed draft revision the employee is submitting."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    expected_revision: int = Field(alias="expectedRevision", ge=1, strict=True)
    expected_total_amount: DecimalString | None = Field(
        default=None, alias="expectedTotalAmount", ge=0, le=MAX_REIMBURSEMENT_AMOUNT
    )

    @field_validator("expected_total_amount")
    @classmethod
    def validate_confirmed_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is not None:
            if value.as_tuple().exponent < -2:
                raise ValueError("amount supports at most two decimal places")
            return quantize_money(value)
        return value
