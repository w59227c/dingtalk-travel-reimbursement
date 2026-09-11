from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest
from conftest import mock_login
from pydantic import ValidationError

from app.core.errors import ApiError
from app.domain.categories import EXPENSE_CATEGORIES, ExpenseCategory
from app.domain.expenses import calculate_expense_totals
from app.domain.money import (
    MAX_REIMBURSEMENT_AMOUNT,
    amount_to_chinese_uppercase,
    quantize_money,
)
from app.domain.subsidy import TripPeriod, TripType, calculate_subsidy
from app.schemas.expenses import ExpenseItem


def period(
    start_date: date,
    start_time: time,
    end_date: date,
    end_time: time,
) -> TripPeriod:
    return TripPeriod(start_date, start_time, end_date, end_time)


@pytest.mark.parametrize(
    ("trip_period", "expected"),
    [
        (period(date(2026, 6, 30), time(9), date(2026, 7, 7), time(18)), "8.0"),
        (period(date(2026, 6, 30), time(12), date(2026, 7, 1), time(11, 59)), "1.0"),
        (period(date(2026, 6, 30), time(11, 59), date(2026, 7, 1), time(12)), "2.0"),
        (period(date(2026, 6, 30), time(9), date(2026, 6, 30), time(12)), "1.0"),
        (period(date(2026, 6, 30), time(12), date(2026, 6, 30), time(18)), "0.5"),
        (period(date(2026, 6, 30), time(9), date(2026, 6, 30), time(9)), "0.5"),
        (period(date(2026, 6, 30), time(18), date(2026, 6, 30), time(18)), "0.5"),
        (period(date(2026, 12, 31), time(13), date(2027, 1, 1), time(8)), "1.0"),
    ],
)
def test_automatic_half_day_rules(trip_period: TripPeriod, expected: str) -> None:
    result = calculate_subsidy(
        trip_type=TripType.BUSINESS,
        period=trip_period,
        configured_daily_rate=Decimal("100.00"),
    )
    assert result.effective_days == Decimal(expected)


def test_date_range_rejects_reverse_time_and_date() -> None:
    with pytest.raises(ApiError, match="结束时间"):
        calculate_subsidy(
            trip_type=TripType.BUSINESS,
            period=period(date(2026, 6, 30), time(13), date(2026, 6, 30), time(9)),
            configured_daily_rate=Decimal("100.00"),
        )
    with pytest.raises(ApiError, match="结束时间"):
        calculate_subsidy(
            trip_type=TripType.BUSINESS,
            period=period(date(2026, 7, 1), time(9), date(2026, 6, 30), time(18)),
            configured_daily_rate=Decimal("100.00"),
        )


def test_same_city_requires_confirmation_but_uses_automatic_days() -> None:
    trip_period = period(date(2026, 6, 30), time(9), date(2026, 7, 1), time(18))
    with pytest.raises(ApiError) as error:
        calculate_subsidy(
            trip_type=TripType.SAME_CITY_PROJECT,
            period=trip_period,
            configured_daily_rate=Decimal("50.00"),
        )
    assert error.value.code == "POLICY_CONFIRMATION_REQUIRED"

    result = calculate_subsidy(
        trip_type=TripType.SAME_CITY_PROJECT,
        period=trip_period,
        configured_daily_rate=Decimal("50.00"),
        policy_confirmed=True,
    )
    assert result.effective_days == Decimal("2.0")
    assert result.daily_rate == Decimal("50.00")
    assert result.total == Decimal("100.00")


def test_long_term_project_uses_automatic_half_day_calculation() -> None:
    result = calculate_subsidy(
        trip_type=TripType.LONG_TERM_PROJECT,
        period=period(date(2026, 6, 1), time(9), date(2026, 7, 1), time(18)),
        configured_daily_rate=Decimal("150.00"),
    )
    assert result.calendar_days == 31
    assert result.effective_days == Decimal("31.0")
    assert result.total == Decimal("4650.00")


def test_automatic_trip_rejects_client_override() -> None:
    with pytest.raises(ApiError) as error:
        calculate_subsidy(
            trip_type=TripType.SHORT_TERM_PROJECT,
            period=period(date(2026, 6, 30), time(9), date(2026, 7, 1), time(18)),
            configured_daily_rate=Decimal("100.00"),
            policy_confirmed=True,
            confirmed_effective_days=Decimal("1.0"),
        )
    assert error.value.code == "UNEXPECTED_POLICY_OVERRIDE"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0", "零元整"),
        ("1", "壹元整"),
        ("10", "壹拾元整"),
        ("100", "壹佰元整"),
        ("1001", "壹仟零壹元整"),
        ("10001", "壹万零壹元整"),
        ("100000001", "壹亿零壹元整"),
        ("1772.39", "壹仟柒佰柒拾贰元叁角玖分"),
        ("1.05", "壹元零伍分"),
        ("0.50", "零元伍角"),
        ("0.005", "零元零壹分"),
        (str(MAX_REIMBURSEMENT_AMOUNT), "玖仟玖佰玖拾玖亿玖仟玖佰玖拾玖万玖仟玖佰玖拾玖元玖角玖分"),
    ],
)
def test_amount_to_chinese_uppercase(amount: str, expected: str) -> None:
    assert amount_to_chinese_uppercase(Decimal(amount)) == expected


def test_money_rejects_float_negative_nonfinite_and_excess() -> None:
    with pytest.raises(TypeError):
        quantize_money(44.89)  # type: ignore[arg-type]
    for value in (Decimal("-0.01"), Decimal("NaN"), Decimal("1000000000000")):
        with pytest.raises(ValueError):
            quantize_money(value)


def test_decimal_totals_and_receipt_count() -> None:
    subsidy = calculate_subsidy(
        trip_type=TripType.BUSINESS,
        period=period(date(2026, 6, 30), time(9), date(2026, 7, 7), time(18)),
        configured_daily_rate=Decimal("100.00"),
    )
    items = [
        ExpenseItem(
            id="one",
            category=ExpenseCategory.LOCAL_TRANSPORT,
            date=date(2026, 7, 1),
            displayDate="2026-07-01",
            description="市内交通",
            amount=Decimal("44.89"),
            receiptCount=4,
            source="manual",
        ),
        ExpenseItem(
            id="two",
            category=ExpenseCategory.RAIL_FARE,
            date=date(2026, 6, 30),
            displayDate="2026-06-30",
            description="北京南-合肥南",
            amount=Decimal("454"),
            receiptCount=1,
            source="ocr",
        ),
        ExpenseItem(
            id="three",
            category=ExpenseCategory.RAIL_FARE,
            date=date(2026, 7, 7),
            displayDate="2026-07-07",
            description="合肥南-北京南",
            amount=Decimal("473.50"),
            receiptCount=1,
            source="manual",
        ),
    ]
    totals = calculate_expense_totals(items, subsidy)
    assert totals.expense_total == Decimal("972.39")
    assert totals.subsidy_total == Decimal("800.00")
    assert totals.total_amount == Decimal("1772.39")
    assert totals.receipt_count == 6
    assert totals.uppercase_amount == "壹仟柒佰柒拾贰元叁角玖分"


def test_category_contract_is_complete_and_unique() -> None:
    assert len(EXPENSE_CATEGORIES) == 17
    assert len({item.id for item in EXPENSE_CATEGORIES}) == 17
    assert [item.order for item in EXPENSE_CATEGORIES] == list(range(1, 18))
    subsidy = next(item for item in EXPENSE_CATEGORIES if item.id is ExpenseCategory.SUBSIDY)
    assert subsidy.manual_selectable is False


def valid_trip() -> dict[str, object]:
    return {
        "tripType": "business",
        "startDate": "2026-06-30",
        "startTime": "09:00",
        "endDate": "2026-07-07",
        "endTime": "18:00",
    }


@pytest.mark.parametrize(
    "invalid_date",
    [0, "2026-06-30T00:00:00", "2026-6-3", " 2026-06-30", "2026-06-30 ", "2026-02-29"],
)
def test_expense_item_schema_rejects_non_exact_or_invalid_dates(invalid_date: object) -> None:
    with pytest.raises(ValidationError):
        ExpenseItem.model_validate(
            {
                "id": "one",
                "category": "other",
                "date": invalid_date,
                "displayDate": "2026-06-30",
                "description": "测试",
                "amount": "1.00",
                "receiptCount": 1,
                "source": "manual",
            }
        )


def test_expense_item_schema_keeps_valid_leap_date_as_python_date() -> None:
    item = ExpenseItem.model_validate(
        {
            "id": "one",
            "category": "other",
            "date": "2028-02-29",
            "displayDate": "2028-02-29",
            "description": "测试",
            "amount": "1.00",
            "receiptCount": 1,
            "source": "manual",
        }
    )

    assert item.date == date(2028, 2, 29)


def test_totals_support_one_subsidy_item_per_related_trip(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    response = client.post(
        "/api/calculate/totals",
        headers={"X-CSRF-Token": csrf},
        json={
            "trips": [
                valid_trip()
                | {
                    "relatedApprovalId": "approval-1",
                    "startDate": "2026-06-30",
                    "endDate": "2026-07-01",
                },
                valid_trip()
                | {
                    "relatedApprovalId": "approval-2",
                    "startDate": "2026-07-02",
                    "endDate": "2026-07-03",
                },
            ],
            "items": [],
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["subsidyTotal"] == "400.00"
    assert [item["relatedApprovalId"] for item in data["subsidies"]] == [
        "approval-1",
        "approval-2",
    ]


def test_totals_keep_discontinuous_trips_separate(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    response = client.post(
        "/api/calculate/totals",
        headers={"X-CSRF-Token": csrf},
        json={
            "trips": [
                valid_trip()
                | {
                    "relatedApprovalId": "approval-1",
                    "startDate": "2026-08-01",
                    "endDate": "2026-08-02",
                },
                valid_trip()
                | {
                    "relatedApprovalId": "approval-2",
                    "startDate": "2026-08-05",
                    "endDate": "2026-08-06",
                },
            ],
            "items": [],
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["subsidyTotal"] == "400.00"
    assert [item["relatedApprovalId"] for item in data["subsidies"]] == [
        "approval-1",
        "approval-2",
    ]


def test_totals_merge_overlapping_and_transitively_overlapping_trips(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    response = client.post(
        "/api/calculate/totals",
        headers={"X-CSRF-Token": csrf},
        json={
            "trips": [
                valid_trip()
                | {
                    "relatedApprovalId": "approval-1",
                    "startDate": "2026-08-01",
                    "endDate": "2026-08-03",
                },
                valid_trip()
                | {
                    "relatedApprovalId": "approval-2",
                    "startDate": "2026-08-03",
                    "endDate": "2026-08-05",
                },
                valid_trip()
                | {
                    "relatedApprovalId": "approval-3",
                    "startDate": "2026-08-05",
                    "endDate": "2026-08-07",
                },
            ],
            "items": [],
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["subsidyTotal"] == "700.00"
    assert data["subsidies"] == [
        {
            "tripType": "business",
            "calendarDays": 7,
            "effectiveDays": "7.0",
            "dailyRate": "100.00",
            "total": "700.00",
            "relatedApprovalId": "approval-1",
        }
    ]


def test_categories_and_totals_api(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    headers = {"X-CSRF-Token": csrf}
    categories = client.get("/api/expense-categories")
    assert categories.status_code == 200
    assert len(categories.json()["data"]) == 17

    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={
            "trip": valid_trip(),
            "items": [
                {
                    "id": "one",
                    "category": "local_transport",
                    "date": "2026-07-01",
                    "displayDate": "7月1日、7月6日",
                    "description": "酒店-项目-酒店",
                    "amount": "44.89",
                    "receiptCount": 4,
                    "source": "manual",
                },
                {
                    "id": "two",
                    "category": "rail_fare",
                    "date": "2026-06-30",
                    "displayDate": "2026-06-30",
                    "description": "北京南-合肥南",
                    "amount": "454.00",
                    "receiptCount": 1,
                    "source": "manual",
                },
                {
                    "id": "three",
                    "category": "rail_fare",
                    "date": "2026-07-07",
                    "displayDate": "2026-07-07",
                    "description": "合肥南-北京南",
                    "amount": "473.50",
                    "receiptCount": 1,
                    "source": "manual",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "expenseTotal": "972.39",
        "subsidyTotal": "800.00",
        "totalAmount": "1772.39",
        "receiptCount": 6,
        "uppercaseAmount": "壹仟柒佰柒拾贰元叁角玖分",
        "subsidy": {
            "tripType": "business",
            "calendarDays": 8,
            "effectiveDays": "8.0",
            "dailyRate": "100.00",
            "total": "800.00",
        },
        "subsidies": [
            {
                "tripType": "business",
                "calendarDays": 8,
                "effectiveDays": "8.0",
                "dailyRate": "100.00",
                "total": "800.00",
            }
        ],
    }


def test_totals_without_trip_do_not_add_subsidy(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    response = client.post(
        "/api/calculate/totals",
        headers={"X-CSRF-Token": csrf},
        json={
            "trip": None,
            "items": [
                {
                    "id": "one",
                    "category": "other",
                    "date": "2026-07-08",
                    "displayDate": "2026-07-08",
                    "description": "非出差报销",
                    "amount": "16.90",
                    "receiptCount": 1,
                    "source": "manual",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "expenseTotal": "16.90",
        "subsidyTotal": "0.00",
        "totalAmount": "16.90",
        "receiptCount": 1,
        "uppercaseAmount": "壹拾陆元玖角",
        "subsidy": None,
        "subsidies": [],
    }


def test_totals_reject_invalid_category_system_line_and_too_many_items(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True, expense_max_items=2)
    csrf = mock_login(client)["csrfToken"]
    headers = {"X-CSRF-Token": csrf}
    base = {
        "id": "one",
        "category": "not-a-category",
        "displayDate": "2026-06-30",
        "description": "测试",
        "amount": "1.00",
        "receiptCount": 1,
        "source": "manual",
    }
    response = client.post(
        "/api/calculate/totals", headers=headers, json={"trip": valid_trip(), "items": [base]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    system_subsidy = base | {
        "category": "subsidy",
        "receiptCount": 0,
        "source": "system",
    }
    assert (
        client.post(
            "/api/calculate/totals",
            headers=headers,
            json={"trip": valid_trip(), "items": [system_subsidy]},
        ).status_code
        == 422
    )

    items = [base | {"id": str(index), "category": "other"} for index in range(3)]
    overflow = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={"trip": valid_trip(), "items": items},
    )
    assert overflow.status_code == 422
    assert overflow.json()["error"] == {
        "code": "TOO_MANY_EXPENSE_LINES",
        "message": "当前部署每张报销单最多处理 2 条费用明细",
    }


def test_totals_return_stable_422_when_individually_valid_lines_overflow(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    headers = {"X-CSRF-Token": csrf}
    item = {
        "category": "other",
        "displayDate": "2026-06-30",
        "description": "边界测试",
        "amount": str(MAX_REIMBURSEMENT_AMOUNT),
        "receiptCount": 1,
        "source": "manual",
    }

    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={
            "trip": valid_trip(),
            "items": [item | {"id": "one"}, item | {"id": "two"}],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "REIMBURSEMENT_TOTAL_EXCEEDED",
        "message": "报销金额合计不能超过 999999999999.99 元",
    }
    assert response.json()["requestId"]


def test_totals_return_stable_422_when_subsidy_pushes_total_over_limit(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]

    response = client.post(
        "/api/calculate/totals",
        headers={"X-CSRF-Token": csrf},
        json={
            "trip": valid_trip(),
            "items": [
                {
                    "id": "one",
                    "category": "other",
                    "displayDate": "2026-06-30",
                    "description": "边界测试",
                    "amount": str(MAX_REIMBURSEMENT_AMOUNT),
                    "receiptCount": 1,
                    "source": "manual",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "REIMBURSEMENT_TOTAL_EXCEEDED",
        "message": "报销金额合计不能超过 999999999999.99 元",
    }
    assert response.json()["requestId"]
