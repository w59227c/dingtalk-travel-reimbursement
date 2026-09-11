from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.domain.categories import EXPENSE_CATEGORIES
from app.domain.expenses import calculate_expense_totals
from app.schemas.common import success
from app.schemas.expenses import TotalsRequest
from app.services.sessions import (
    CurrentSession,
    get_current_session,
    require_csrf,
    require_selected_department,
)
from app.services.subsidy_calculation import calculate_trip_subsidies, request_trips

router = APIRouter(tags=["reimbursement"])


@router.get("/expense-categories")
def expense_categories(
    _current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    return success([item.as_api_dict() for item in EXPENSE_CATEGORIES])


@router.post("/calculate/totals")
def totals(
    body: TotalsRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    require_selected_department(current)
    max_items = request.app.state.settings.expense_max_items
    if len(body.items) > max_items:
        raise ApiError(
            "TOO_MANY_EXPENSE_LINES",
            f"当前部署每张报销单最多处理 {max_items} 条费用明细",
            422,
        )
    requested = request_trips(trip=body.trip, trips=body.trips)
    subsidy_calculations = calculate_trip_subsidies(database, requested)
    result = calculate_expense_totals(body.items, subsidy_calculations).as_api_dict()
    result["subsidy"] = (
        subsidy_calculations[0].as_api_dict() if len(subsidy_calculations) == 1 else None
    )
    result["subsidies"] = [item.as_api_dict() for item in subsidy_calculations]
    return success(result)
