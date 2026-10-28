import json
from dataclasses import replace
from datetime import date, time
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from conftest import mock_login
from openpyxl import load_workbook
from test_oa_reimbursement_payload import _schema, _source

from app.core.errors import ApiError
from app.domain.expenses import calculate_expense_totals
from app.domain.subsidy import TripPeriod, TripType, calculate_subsidy
from app.integrations.dingtalk.workflow import FormOption
from app.schemas.expenses import TripInput
from app.services.excel_generator import generate_expense_workbook
from app.services.oa_reimbursement_payload import (
    build_snapshot,
    parse_snapshot,
    serialize_snapshot,
    snapshot_excel_input,
)
from app.services.subsidy_description import subsidy_explanation


def attendance_trip(**changes):
    return {
        "tripType": "same_city_project",
        "startDate": "2026-09-01",
        "endDate": "2026-09-10",
        "startTime": "09:00",
        "endTime": "18:00",
        "attendanceDates": ["2026-09-01", "2026-09-05", "2026-09-06"],
        **changes,
    }


@pytest.mark.parametrize(
    "start,end", [(time(9), time(9)), (time(18), time(18)), (time(18), time(9))]
)
def test_attendance_pays_one_whole_day_even_for_half_day_or_weekend(start, end):
    day = date(2026, 9, 5)
    result = calculate_subsidy(
        trip_type=TripType.SAME_CITY_PROJECT,
        period=TripPeriod(day, start, day, end),
        configured_daily_rate=Decimal("50"),
        attendance_dates=[day],
    )
    assert result.effective_days == 1
    assert result.total == Decimal("50.00")


def test_real_api_uses_selected_dates_and_server_rate_without_confirmation(client_factory):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    body = {"trip": attendance_trip(), "items": []}
    assert client.post("/api/calculate/totals", json=body).status_code == 403
    response = client.post("/api/calculate/totals", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["subsidyTotal"] == "150.00"
    assert response.json()["data"]["subsidy"]["effectiveDays"] == "3.0"
    empty = client.post(
        "/api/calculate/totals",
        json={"trip": attendance_trip(attendanceDates=[]), "items": []},
        headers=headers,
    )
    assert empty.status_code == 200
    assert empty.json()["data"]["subsidyTotal"] == "0.00"


@pytest.mark.parametrize(
    "changes",
    [
        {"attendanceDates": None},
        {"attendanceDates": ["2026-09-01", "2026-09-01"]},
        {"attendanceDates": ["2026-09-11"]},
        {"attendanceDates": ["2026-02-30"]},
        {"startDate": "2020-01-01", "endDate": "2040-01-01"},
        {"tripType": "business"},
        {
            "excludedPeriods": [
                {"type": "late_departure", "endDate": "2026-09-03", "endTime": "09:00"}
            ]
        },
        {
            "excludedPeriods": [
                {
                    "startDate": "2026-09-04",
                    "startTime": "09:00",
                    "endDate": "2026-09-05",
                    "endTime": "18:00",
                }
            ]
        },
    ],
)
def test_attendance_api_rejects_missing_duplicate_outside_or_mixed_rules(client_factory, changes):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/calculate/totals",
        json={"trip": attendance_trip(**changes), "items": []},
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_overlapping_approvals_deduplicate_dates_and_preserve_source_bounds(client_factory):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    first = attendance_trip(
        relatedApprovalId="synthetic-a",
        endDate="2026-09-05",
        attendanceDates=["2026-09-01", "2026-09-05"],
    )
    second = attendance_trip(
        relatedApprovalId="synthetic-b",
        startDate="2026-09-05",
        attendanceDates=["2026-09-05", "2026-09-06"],
    )
    response = client.post(
        "/api/calculate/totals", json={"trips": [first, second], "items": []}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["subsidyTotal"] == "150.00"
    assert len(response.json()["data"]["subsidies"]) == 1
    first["attendanceDates"].append("2026-09-06")
    rejected = client.post(
        "/api/calculate/totals", json={"trips": [first, second], "items": []}, headers=headers
    )
    assert rejected.status_code == 422


def attendance_source():
    source = _source()
    city = FormOption("市内项目", "境内市内项目出差（长/短期）", "synthetic-city")
    schema = source.catalog.reimbursement.schema
    schema = _schema(
        schema.process_code,
        tuple(
            replace(component, options=(city,))
            if component.component_id == "component-travelType"
            else component
            for component in schema.components
        ),
    )
    catalog = replace(
        source.catalog,
        reimbursement=replace(source.catalog.reimbursement, schema=schema),
        travel_profiles=(replace(source.catalog.travel_profiles[0], travel_type_option=city),),
    )
    trip = TripInput.model_validate(
        attendance_trip(endDate="2026-09-03", attendanceDates=["2026-09-01", "2026-09-03"])
    )
    draft_input = source.draft_input.model_copy(update={"trip": trip})
    subsidy = calculate_subsidy(
        trip_type=TripType.SAME_CITY_PROJECT,
        period=trip.as_period(),
        configured_daily_rate=Decimal("50"),
        attendance_dates=trip.attendance_dates,
    )
    totals = calculate_expense_totals(draft_input.items, subsidy).as_api_dict()
    totals["subsidy"] = subsidy.as_api_dict()
    return replace(source, catalog=catalog, draft_input=draft_input, totals_data=totals)


def test_attendance_snapshot_freezes_dates_explanation_and_rejects_tampering():
    snapshot = build_snapshot(attendance_source())
    assert snapshot.snapshot_version == 11
    assert snapshot.input.trip.attendance_dates == (date(2026, 9, 1), date(2026, 9, 3))
    raw = serialize_snapshot(snapshot)
    assert parse_snapshot(raw) == snapshot
    assert "出勤2天" in raw and "出勤日期" in raw
    tampered = json.loads(raw)
    tampered["input"]["trip"]["attendanceDates"] = ["2026-09-01", "2026-09-02"]
    with pytest.raises(ApiError):
        parse_snapshot(json.dumps(tampered))
    tampered = json.loads(raw)
    tampered["snapshotVersion"] = 10
    with pytest.raises(ApiError):
        parse_snapshot(json.dumps(tampered))


def test_attendance_description_lists_exact_dates_for_excel_and_oa():
    source = attendance_source()
    trip = source.draft_input.trip
    calculation = calculate_subsidy(
        trip_type=TripType.SAME_CITY_PROJECT,
        period=trip.as_period(),
        configured_daily_rate=Decimal("50"),
        attendance_dates=trip.attendance_dates,
    )
    lines = subsidy_explanation(trip, calculation)
    assert "出勤2天" in lines[0] and "100.00元" in lines[0]
    assert lines[1] == "出勤日期：2026-09-01、2026-09-03"


def test_attendance_excel_uses_frozen_dates_and_amount():
    value = snapshot_excel_input(build_snapshot(attendance_source()))
    result = generate_expense_workbook(
        template_path=Path(__file__).parents[1] / "app/templates/expense_template.xlsx",
        employee_name=value.employee_name,
        department_name=value.department_name,
        project=value.project,
        trip=value.trip,
        trips=value.trips,
        items=value.items,
        subsidy=value.subsidy,
        subsidies=value.subsidies,
        totals=value.totals,
        detailed_subsidies=value.detailed_subsidies,
    )
    workbook = load_workbook(BytesIO(result.content))
    texts = [str(cell.value) for row in workbook.active for cell in row if cell.value is not None]
    assert any("出勤2天" in text and "100.00元" in text for text in texts)
    assert any("出勤日期：2026-09-01、2026-09-03" in text for text in texts)
    workbook.close()


def test_legacy_city_snapshot_keeps_original_half_day_result_and_json(monkeypatch):
    from app.services import oa_reimbursement_payload as payloads

    source = attendance_source()
    trip = source.draft_input.trip.model_copy(
        update={"attendance_dates": None, "policy_confirmed": True}
    )
    subsidy = calculate_subsidy(
        trip_type=TripType.SAME_CITY_PROJECT,
        period=trip.as_period(),
        configured_daily_rate=Decimal("50"),
        policy_confirmed=True,
    )
    totals = calculate_expense_totals(source.draft_input.items, subsidy).as_api_dict()
    totals["subsidy"] = subsidy.as_api_dict()
    source = replace(
        source, draft_input=source.draft_input.model_copy(update={"trip": trip}), totals_data=totals
    )
    original_model = payloads.ReimbursementSnapshot

    def legacy_model(**values):
        return original_model(**{**values, "snapshot_version": 10})

    with monkeypatch.context() as patch:
        patch.setattr(payloads, "ReimbursementSnapshot", legacy_model)
        snapshot = build_snapshot(source)
    raw = serialize_snapshot(snapshot)
    assert "attendanceDates" not in raw
    restored = parse_snapshot(raw)
    assert restored.subsidy.total == Decimal("150.00")
    assert serialize_snapshot(restored) == raw


def test_attendance_dates_and_conflicting_range_save_restore_without_losing_choices(
    client_factory, monkeypatch
):
    from test_reimbursement_drafts import _create, _install_catalog

    from app.schemas.reimbursements import ReimbursementDraftInput
    from app.services.reimbursement_drafts import require_complete_draft_input

    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = _create(client, headers, {"items": [], "trip": attendance_trip()})
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    editing = attendance_trip(
        excludedPeriods=[{"type": "late_departure", "endDate": "2026-09-03", "endTime": "09:00"}]
    )
    value = {
        **draft["input"],
        "trip": None,
        "trips": [],
        "editingState": {"includeSubsidy": True, "trip": editing},
    }
    saved = client.put(
        f"/api/reimbursements/drafts/{draft['id']}",
        headers=headers,
        json={"expectedRevision": draft["revision"], "input": value},
    )
    assert saved.status_code == 200, saved.text
    loaded = client.get(f"/api/reimbursements/drafts/{draft['id']}").json()["data"]
    assert loaded["input"]["editingState"]["trip"]["attendanceDates"] == editing["attendanceDates"]
    with pytest.raises(ApiError):
        require_complete_draft_input(ReimbursementDraftInput.model_validate(loaded["input"]))
