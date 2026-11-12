from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.errors import ApiError
from app.domain.categories import CATEGORY_BY_ID, ExpenseCategory
from app.domain.money import (
    MAX_REIMBURSEMENT_AMOUNT,
    amount_to_chinese_uppercase,
    money_string,
    quantize_money,
)
from app.domain.subsidy import SubsidyCalculation
from app.schemas.expenses import ExpenseLineBase


def expense_output_sort_key(
    occurrence_date: date, category: ExpenseCategory,
) -> tuple[date, int]:
    # The stable sort preserves saved order within each date and category.
    return occurrence_date, CATEGORY_BY_ID[category].order


@dataclass(frozen=True, slots=True)
class ExpenseTotals:
    expense_total: Decimal
    subsidy_total: Decimal
    total_amount: Decimal
    receipt_count: int
    uppercase_amount: str

    def as_api_dict(self) -> dict[str, object]:
        return {
            "expenseTotal": money_string(self.expense_total),
            "subsidyTotal": money_string(self.subsidy_total),
            "totalAmount": money_string(self.total_amount),
            "receiptCount": self.receipt_count,
            "uppercaseAmount": self.uppercase_amount,
        }


def calculate_expense_totals(
    items: list[ExpenseLineBase],
    subsidy: SubsidyCalculation | list[SubsidyCalculation] | tuple[SubsidyCalculation, ...] | None,
) -> ExpenseTotals:
    expense_sum = sum((item.amount for item in items), Decimal("0.00"))
    if expense_sum > MAX_REIMBURSEMENT_AMOUNT:
        raise ApiError(
            "REIMBURSEMENT_TOTAL_EXCEEDED",
            "报销金额合计不能超过 999999999999.99 元",
            422,
        )
    expense_total = quantize_money(expense_sum)
    subsidies = (
        []
        if subsidy is None
        else ([subsidy] if isinstance(subsidy, SubsidyCalculation) else subsidy)
    )
    subsidy_total = quantize_money(sum((item.total for item in subsidies), Decimal("0.00")))
    combined_total = expense_total + subsidy_total
    if combined_total > MAX_REIMBURSEMENT_AMOUNT:
        raise ApiError(
            "REIMBURSEMENT_TOTAL_EXCEEDED",
            "报销金额合计不能超过 999999999999.99 元",
            422,
        )
    total_amount = quantize_money(combined_total)
    receipt_count = sum(item.receipt_count for item in items)
    return ExpenseTotals(
        expense_total=expense_total,
        subsidy_total=subsidy_total,
        total_amount=total_amount,
        receipt_count=receipt_count,
        uppercase_amount=amount_to_chinese_uppercase(total_amount),
    )
