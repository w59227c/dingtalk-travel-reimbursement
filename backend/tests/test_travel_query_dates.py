"""Live query bounds must reject dates before making any DingTalk request."""

from datetime import date

import pytest

from app.core.errors import ApiError
from app.services.travel_approvals import TravelApprovalQueryWindow, requested_query_window


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        (date(2025, 1, 1), date(2025, 5, 31), "TRAVEL_APPROVAL_QUERY_HISTORY_UNAVAILABLE"),
        (date(2025, 10, 29), date(2025, 10, 30), "TRAVEL_APPROVAL_QUERY_HISTORY_UNAVAILABLE"),
        (date(2026, 10, 29), date(2026, 10, 30), "TRAVEL_APPROVAL_QUERY_FUTURE"),
    ],
)
def test_live_queries_reject_unavailable_history_and_future_dates(start, end, code):
    with pytest.raises(ApiError) as caught:
        requested_query_window(start, end, today=date(2026, 10, 29))
    assert caught.value.code == code
    assert caught.value.status_code == 422


def test_oldest_full_calendar_day_and_maximum_span_are_inclusive():
    window = requested_query_window(date(2025, 10, 30), date(2026, 4, 27), today=date(2026, 10, 29))
    assert (window.to_date - window.from_date).days == 179
    with pytest.raises(ApiError) as caught:
        requested_query_window(date(2025, 10, 30), date(2026, 4, 28), today=date(2026, 10, 29))
    assert caught.value.code == "TRAVEL_APPROVAL_QUERY_WINDOW_INVALID"


def test_historical_stored_query_proofs_remain_structurally_readable():
    window = TravelApprovalQueryWindow.from_dates(date(2020, 1, 1), date(2020, 1, 2))
    assert window.as_dict() == {"from": "2020-01-01", "to": "2020-01-02"}
