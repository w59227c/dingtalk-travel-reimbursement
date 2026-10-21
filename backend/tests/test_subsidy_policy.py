from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from conftest import mock_login
from test_oa_reimbursement_payload import _source
from test_reimbursement_drafts import _create, _install_catalog

from app.core.errors import ApiError
from app.domain.expenses import calculate_expense_totals
from app.domain.subsidy import TripType, calculate_subsidy
from app.models.setting import Setting
from app.schemas.expenses import TripInput
from app.services.application_settings import get_expense_settings
from app.services.oa_reimbursement_payload import (
    build_snapshot,
    parse_snapshot,
    serialize_snapshot,
    snapshot_excel_input,
)
from app.services.reimbursement_drafts import require_complete_draft_input


def trip(**overrides):
    return {
        "tripType": "long_term_project",
        "startDate": "2026-10-01",
        "startTime": "09:00",
        "endDate": "2026-10-31",
        "endTime": "18:00",
        **overrides,
    }


def excluded(start="2026-10-10", end="2026-10-13", start_time="09:00", end_time="18:00"):
    return {"startDate": start, "startTime": start_time, "endDate": end, "endTime": end_time}


@pytest.mark.parametrize(
    ("start_time", "end_time", "paid_days", "excluded_days"),
    [
        ("09:00", "09:00", "28.5", "2.5"),
        ("09:00", "18:00", "28.0", "3.0"),
        ("18:00", "09:00", "29.0", "2.0"),
        ("18:00", "18:00", "28.5", "2.5"),
    ],
)
def test_excluded_period_departure_rules(start_time, end_time, paid_days, excluded_days):
    value = TripInput.model_validate(
        trip(excludedPeriods=[excluded(start_time=start_time, end_time=end_time)])
    )
    result = calculate_subsidy(
        trip_type=value.subsidy_trip_type(),
        period=value.as_period(),
        configured_daily_rate=Decimal("150"),
        excluded_periods=[item.as_period() for item in value.excluded_periods],
    )
    assert result.effective_days == Decimal(paid_days)
    assert result.excluded_days == Decimal(excluded_days)
    assert result.total == Decimal(paid_days) * 150


def test_legacy_calculation_unions_overlapping_exclusions_and_single_day_boundaries():
    periods = [
        excluded(),
        excluded(),
        excluded("2026-10-11", "2026-10-12"),
        excluded("2026-10-20", "2026-10-22", "18:00", "09:00"),
        excluded("2026-10-25", "2026-10-25"),
    ]
    value = TripInput.model_validate(trip(excludedPeriods=periods))
    result = calculate_subsidy(
        trip_type=value.subsidy_trip_type(),
        period=value.as_period(),
        configured_daily_rate=Decimal("150"),
        excluded_periods=[item.as_period() for item in value.excluded_periods],
    )
    assert result.excluded_days == Decimal("4.0")
    assert result.effective_days == Decimal("27.0")
    assert result.total == Decimal("4050.00")


@pytest.mark.parametrize(
    "other",
    [
        excluded(),
        excluded("2026-10-11", "2026-10-12"),
        excluded("2026-10-09", "2026-10-14"),
        excluded("2026-10-12", "2026-10-15"),
        excluded("2026-10-13", "2026-10-15", "18:00", "09:00"),
        excluded("2026-10-13", "2026-10-13", "18:00", "18:00"),
    ],
)
def test_overlapping_personal_absences_are_rejected_even_without_overlapping_deductions(
    client_factory, other
):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={"trip": trip(excludedPeriods=[excluded(), other]), "items": []},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "OVERLAPPING_EXCLUDED_PERIODS"
    assert "时段 1 与时段 2 重叠" in response.json()["error"]["message"]


def test_morning_return_and_afternoon_departure_on_same_day_are_separate(client_factory):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={
            "trip": trip(excludedPeriods=[
                excluded("2026-10-13", "2026-10-15", "18:00", "09:00"),
                excluded("2026-10-10", "2026-10-13", "09:00", "09:00"),
            ]),
            "items": [],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["subsidy"]["excludedDays"] == "3.5"


@pytest.mark.parametrize(
    "period",
    [
        excluded("2026-09-30", "2026-10-12"),
        excluded("2026-10-10", "2026-11-01"),
        excluded("2026-10-13", "2026-10-10"),
        excluded("2026-10-10", "2026-10-10", "18:00", "09:00"),
        excluded("", "2026-10-12"),
    ],
)
def test_invalid_excluded_periods_are_rejected(client_factory, period):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={"trip": trip(excludedPeriods=[period]), "items": []},
    )
    assert response.status_code in {400, 422}, response.text


@pytest.mark.parametrize(
    ("category", "end", "expected"),
    [
        ("long_term_project", "2026-10-02", "300.00"),
        ("short_term_project", "2026-10-31", "3100.00"),
        ("long_term_internal", "2026-10-31", "3100.00"),
        ("short_term_internal", "2026-10-31", "3100.00"),
    ],
)
def test_category_never_changes_with_duration(client_factory, category, end, expected):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={"trip": trip(tripType=category, endDate=end), "items": []},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["subsidy"]["tripType"] == category
    assert response.json()["data"]["subsidyTotal"] == expected


def test_eight_settings_preserve_legacy_rate_and_allow_independent_zero_and_overseas(
    client_factory,
):
    client = client_factory(auth_mock_enabled=True, auth_mock_user_id="bootstrap-admin")
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    with client.app.state.database_session_factory() as database:
        database.add(Setting(key="subsidy_internal_per_day", value="123.45"))
        database.commit()
    settings = client.get("/api/admin/settings").json()["data"]
    rates = settings["subsidyRates"]
    assert len(rates) == 8
    assert rates["short_term_internal"] == rates["long_term_internal"] == "123.45"
    assert rates["short_term_overseas"] == rates["long_term_overseas"] == "0.00"
    assert rates["business"] == "100.00"
    assert rates["short_term_project"] == "100.00"
    assert rates["long_term_project"] == "150.00"
    assert rates["same_city_project"] == "50.00"
    rates.update(business="0.00", long_term_overseas="200.00", long_term_internal="99.00")
    body = {
        key: settings[key]
        for key in ("subsidyRates", "calculationMode", "appTitle", "adminUserIds")
    }
    assert client.put("/api/admin/settings", json=body).status_code == 403
    response = client.put("/api/admin/settings", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert client.get("/api/settings").json()["data"]["subsidyRates"] == rates
    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={"trip": trip(tripType="long_term_overseas", endDate="2026-10-03"), "items": []},
    )
    assert response.json()["data"]["subsidyTotal"] == "600.00"
    with client.app.state.database_session_factory() as database:
        assert get_expense_settings(database).daily_rate_for(
            TripType.SHORT_TERM_INTERNAL
        ) == Decimal("123.45")
    for invalid in ("-0.01", "10000.01", "0.001"):
        body["subsidyRates"]["business"] = invalid
        assert client.put("/api/admin/settings", headers=headers, json=body).status_code == 422


@pytest.mark.parametrize("overlap", [False, True])
def test_merged_approvals_validate_exclusions_across_original_rows(client_factory, overlap):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/calculate/totals",
        headers=headers,
        json={
            "trips": [
                trip(
                    relatedApprovalId="a",
                    endDate="2026-10-15",
                    excludedPeriods=[excluded("2026-10-10", "2026-10-20")],
                ),
                trip(
                    relatedApprovalId="b",
                    startDate="2026-10-15",
                    excludedPeriods=[
                        excluded("2026-10-15", "2026-10-20")
                        if overlap else excluded("2026-10-25", "2026-10-27")
                    ],
                ),
            ],
            "items": [],
        },
    )
    if overlap:
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "OVERLAPPING_EXCLUDED_PERIODS"
        return
    assert response.status_code == 200, response.text
    result = response.json()["data"]["subsidies"]
    assert len(result) == 1
    assert result[0]["excludedDays"] == "12.0"
    assert result[0]["total"] == "2850.00"


def test_exclusions_persist_in_drafts_and_unfinished_edits_block_preview(
    client_factory, monkeypatch
):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    body = {"items": [], "trip": trip(tripType="business", excludedPeriods=[excluded()])}
    response = _create(client, headers, body)
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    assert draft["totals"]["subsidyTotal"] == "2800.00"
    persisted = client.get(f"/api/reimbursements/drafts/{draft['id']}").json()["data"]
    assert persisted["input"]["trip"]["excludedPeriods"] == [excluded()]
    from app.schemas.reimbursements import ReimbursementDraftInput

    editing = trip(tripType="business", excludedPeriods=[excluded(start="")])
    partial = ReimbursementDraftInput.model_validate(
        {"items": [], "editingState": {"includeSubsidy": True, "trip": editing}}
    )
    with pytest.raises(ApiError, match="补齐"):
        require_complete_draft_input(partial)
    conflict = client.put(
        f"/api/reimbursements/drafts/{draft['id']}",
        headers=headers,
        json={"expectedRevision": 999, "input": body},
    )
    assert conflict.status_code == 409


@pytest.mark.parametrize("historical_overlap", [False, True])
@pytest.mark.parametrize("version", [7, 8])
def test_snapshot_freezes_exclusions_rejects_tampering_and_keeps_v6_readable(
    historical_overlap, version,
):
    source = _source()
    period = excluded("2026-09-01", "2026-09-03")
    trip_input = TripInput.model_validate(
        source.draft_input.trip.model_dump(by_alias=True)
        | {"excludedPeriods": [period, period] if historical_overlap else [period]}
    )
    result = calculate_subsidy(
        trip_type=trip_input.subsidy_trip_type(),
        period=trip_input.as_period(),
        configured_daily_rate=Decimal("180"),
        excluded_periods=[item.as_period() for item in trip_input.excluded_periods],
    )
    totals = calculate_expense_totals(source.draft_input.items, result).as_api_dict() | {
        "subsidy": result.as_api_dict()
    }
    # Reconstruct a genuine v7 payload: historical overlapping periods used the
    # union rule and the original concise OA description. New v9 rejects them.
    base = build_snapshot(source)
    historical = json.loads(serialize_snapshot(base))
    historical["snapshotVersion"] = version
    historical["input"]["trip"]["excludedPeriods"] = (
        [period, period] if historical_overlap else [period]
    )
    historical["subsidy"].update(result.as_api_dict())
    historical["totals"] = {
        key: value for key, value in totals.items() if key not in {"subsidy", "subsidies"}
    }
    description = next(
        item for item in historical["formValues"] if item["logicalKey"] == "description"
    )
    description["value"] = "\n".join(
        "出差补助：1.0天 × 180.00 = 180.00（已扣除2.0天非补助时段）"
        if line.startswith("出差补助：") else f"合计：{totals['totalAmount']}"
        if line.startswith("合计：") else line
        for line in description["value"].splitlines()
    )
    next(item for item in historical["formValues"] if item["logicalKey"] == "reimbursementAmount")[
        "value"
    ] = totals["totalAmount"]
    raw = json.dumps(historical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    restored = parse_snapshot(raw)
    assert serialize_snapshot(restored) == raw
    excel_input = snapshot_excel_input(restored)
    assert excel_input.detailed_subsidies is False
    from app.services.excel_generator import _ordered_lines

    excel_lines = _ordered_lines(
        excel_input.items,
        excel_input.trip,
        excel_input.subsidy,
        excel_input.trips,
        excel_input.subsidies,
        detailed_subsidies=excel_input.detailed_subsidies,
    )
    subsidy_lines = [line for line in excel_lines if line.subsidy]
    assert len(subsidy_lines) == 1
    assert "扣除2天非补助时段" in subsidy_lines[0].description
    assert not any(line.continuation for line in excel_lines)
    assert restored.subsidy.excluded_days == Decimal("2.0")
    assert restored.subsidy.effective_days == Decimal("1.0")
    assert restored.input.trip.excluded_periods[0].start_date == date(2026, 9, 1)
    assert "扣除2.0天非补助时段" in next(
        item.value for item in restored.form_values if item.logical_key == "description"
    )
    tampered = json.loads(raw)
    tampered["subsidy"]["excludedDays"] = "1.0"
    with pytest.raises(ApiError):
        parse_snapshot(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    legacy = json.loads(serialize_snapshot(build_snapshot(source)))
    legacy["snapshotVersion"] = 6
    legacy["input"]["trip"]["tripType"] = "project"
    raw_legacy = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    old_snapshot = parse_snapshot(raw_legacy)
    assert serialize_snapshot(old_snapshot) == raw_legacy
    assert snapshot_excel_input(old_snapshot).subsidy.total == Decimal("540.00")


def test_snapshot_rejects_a_different_subsidy_category_even_at_the_same_rate():
    from app.schemas.expenses import TripPurpose

    source = _source()
    changed = replace(
        source,
        draft_input=source.draft_input.model_copy(
            update={
                "trip": source.draft_input.trip.model_copy(
                    update={
                        "trip_type": TripPurpose.LONG_TERM_PROJECT,
                    }
                ),
            }
        ),
        totals_data=source.totals_data
        | {
            "subsidy": source.totals_data["subsidy"] | {"tripType": "long_term_project"},
        },
    )
    with pytest.raises(ApiError):
        build_snapshot(changed)
