from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import mock_login
from pydantic import ValidationError

from app.domain.subsidy import TripType, calculate_subsidy
from app.schemas.expenses import TripInput


def overseas_trip(**overrides):
    return {
        "tripType": "short_term_overseas",
        "startDate": "2026-09-01",
        "startTime": "09:00",
        "endDate": "2026-09-03",
        "endTime": "18:00",
        **overrides,
    }


def test_overseas_subsidy_uses_configured_daily_rate():
    trip = TripInput.model_validate(overseas_trip())
    result = calculate_subsidy(
        trip_type=TripType.SHORT_TERM_OVERSEAS,
        period=trip.as_period(),
        configured_daily_rate=Decimal("120.00"),
    )
    assert result.total == Decimal("360.00")


def test_manual_overseas_amount_is_rejected():
    with pytest.raises(ValidationError):
        TripInput.model_validate(overseas_trip(manualSubsidyAmount="321.45"))


def test_overseas_trip_does_not_add_subsidy_to_totals(client_factory):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    trip = overseas_trip()
    item = {
        "category": "other",
        "date": "2026-09-01",
        "displayDate": "9月1日",
        "description": "费用",
        "amount": "10.00",
        "receiptCount": 1,
    }
    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={"trip": trip, "items": [item | {"id": "manual-1", "source": "manual"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["totalAmount"] == "10.00"
    assert response.json()["data"]["subsidyTotal"] == "0.00"
    assert response.json()["data"]["subsidies"][0]["dailyRate"] == "0.00"
