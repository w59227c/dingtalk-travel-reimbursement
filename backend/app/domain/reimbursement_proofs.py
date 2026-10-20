from __future__ import annotations

from decimal import Decimal

from app.domain.categories import ExpenseCategory

# This is a reimbursement policy, not a claim that every railway ticket is high-speed.
PAYMENT_PROOF_EXEMPT_RAIL_TYPES = frozenset({"high_speed"})


def payment_proof_required(
    *, amount: Decimal | None, category: ExpenseCategory, rail_type: str, receipt_count: int = 1
) -> bool:
    return (
        receipt_count == 1
        and amount is not None
        and amount > Decimal("500.00")
        and not (
            category is ExpenseCategory.RAIL_FARE and rail_type in PAYMENT_PROOF_EXEMPT_RAIL_TYPES
        )
    )
