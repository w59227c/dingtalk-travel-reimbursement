from decimal import Decimal

import pytest

from app.domain.categories import ExpenseCategory
from app.ocr.types import ParsedExpense
from app.schemas.reimbursements import ReimbursementDraftExpenseItemInput
from app.services.ocr_service import parsed_expense_payload


def test_large_original_amount_keeps_all_digits_without_raising_the_cny_limit():
    value = "999999999999999.90"
    row = ReimbursementDraftExpenseItemInput.model_validate(
        {
            "category": "other",
            "amount": "1.00",
            "originalCurrency": "VND",
            "originalAmount": value,
        }
    )
    assert row.model_dump(mode="json", by_alias=True)["originalAmount"] == value
    payload = parsed_expense_payload(
        "synthetic-file",
        ParsedExpense(
            receipt_type="foreign",
            category=ExpenseCategory.OTHER,
            date=None,
            description="Synthetic foreign total",
            amount=None,
            confidence=1.0,
            original_amount=Decimal(value),
            original_currency="VND",
        ),
    )
    assert payload["originalAmount"] == value
    with pytest.raises(ValueError):
        ReimbursementDraftExpenseItemInput.model_validate({"category": "other", "amount": value})


@pytest.mark.parametrize("value", ["1000000000000000.00", "-1.00", "NaN", "Infinity", "1.001"])
def test_original_amount_still_rejects_invalid_or_out_of_range_values(value):
    with pytest.raises(ValueError):
        ReimbursementDraftExpenseItemInput.model_validate(
            {
                "category": "other",
                "amount": "1.00",
                "originalAmount": value,
            }
        )
