from decimal import Decimal

import pytest
from conftest import mock_login

from app.core.errors import ApiError
from app.domain.subsidy import calculate_subsidy
from app.schemas.expenses import TripInput


def trip(*adjustments, **overrides):
    return {
        "tripType": "long_term_project",
        "startDate": "2026-07-01",
        "startTime": "09:00",
        "endDate": "2026-07-10",
        "endTime": "18:00",
        "excludedPeriods": list(adjustments),
        **overrides,
    }


def late(day="2026-07-02", clock="18:00"):
    return {"type": "late_departure", "endDate": day, "endTime": clock}


def early(day="2026-07-09", clock="09:00"):
    return {"type": "early_end", "startDate": day, "startTime": clock}


def middle(start="2026-07-04", end="2026-07-06", start_time="09:00", end_time="18:00"):
    return {"startDate": start, "startTime": start_time, "endDate": end, "endTime": end_time}


@pytest.mark.parametrize(
    "adjustments,paid,deducted",
    [
        ([late(clock="09:00")], "9.0", "1.0"),
        ([late()], "8.5", "1.5"),
        ([early(clock="18:00")], "9.0", "1.0"),
        ([early()], "8.5", "1.5"),
        ([late(), early()], "7.0", "3.0"),
        ([late(), early(), middle()], "5.0", "5.0"),
        ([late(), early("2026-07-02", "18:00")], "0.5", "9.5"),
        ([early("2026-07-10", "18:00")], "10.0", "0.0"),
        ([late("2026-07-01", "09:00")], "10.0", "0.0"),
    ],
)
def test_adjustment_boundaries_keep_approved_category(adjustments, paid, deducted):
    value = TripInput.model_validate(trip(*adjustments))
    result = calculate_subsidy(
        trip_type=value.subsidy_trip_type(),
        period=value.as_period(),
        configured_daily_rate=Decimal("150"),
        excluded_periods=[item.as_period() for item in value.excluded_periods],
    )
    assert result.trip_type.value == "long_term_project"
    assert result.effective_days == Decimal(paid)
    assert result.excluded_days == Decimal(deducted)
    assert result.total == Decimal(paid) * 150


@pytest.mark.parametrize(
    "adjustments",
    [
        [late(), late("2026-07-03")],
        [early(), early("2026-07-08")],
        [late("2026-06-30")],
        [early("2026-07-11")],
        [late("2026-07-09", "18:00"), early("2026-07-09", "09:00")],
        [late("2026-07-05"), middle()],
        [early("2026-07-05"), middle()],
        [{"type": "early_end", "startDate": "2026-07-09"}],
        [{"type": "late_departure", "endDate": "2026-07-02"}],
        [{"type": "unknown", **middle()}],
        [{**late(), "startDate": "2026-07-01", "startTime": "09:00"}],
    ],
)
def test_invalid_adjustments_cannot_bypass_server_validation(client_factory, adjustments):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/calculate/totals", headers=headers, json={"trip": trip(*adjustments), "items": []}
    )
    assert response.status_code == 422, response.text


def test_legacy_period_json_has_no_new_default_fields():
    value = TripInput.model_validate(trip(middle()))
    assert value.model_dump(mode="json", by_alias=True)["excludedPeriods"] == [middle()]


def test_adjustments_save_restore_partial_state_and_revision(client_factory, monkeypatch):
    from test_reimbursement_drafts import _create, _install_catalog

    from app.schemas.reimbursements import ReimbursementDraftInput
    from app.services.reimbursement_drafts import require_complete_draft_input

    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    complete = trip(late(), early(), middle(), tripType="business")
    response = _create(client, headers, {"items": [], "trip": complete})
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    restored = client.get(f"/api/reimbursements/drafts/{draft['id']}").json()["data"]
    assert restored["input"]["trip"]["excludedPeriods"] == [late(), early(), middle()]
    assert restored["totals"]["subsidyTotal"] == "500.00"
    incomplete = {**complete, "excludedPeriods": [late(day="")]}
    value = {
        **restored["input"],
        "trip": None,
        "editingState": {"includeSubsidy": True, "trip": incomplete},
    }
    saved = client.put(
        f"/api/reimbursements/drafts/{draft['id']}",
        headers=headers,
        json={"expectedRevision": draft["revision"], "input": value},
    )
    assert saved.status_code == 200, saved.text
    state = saved.json()["data"]["input"]
    assert state["editingState"]["trip"]["excludedPeriods"][0]["type"] == "late_departure"
    assert state["editingState"]["trip"]["excludedPeriods"][0]["endDate"] == ""
    with pytest.raises(ApiError, match="补齐"):
        require_complete_draft_input(ReimbursementDraftInput.model_validate(state))
    conflict = client.put(
        f"/api/reimbursements/drafts/{draft['id']}",
        headers=headers,
        json={"expectedRevision": draft["revision"], "input": value},
    )
    assert conflict.status_code == 409


def _adjusted_snapshot():
    from dataclasses import replace

    from test_oa_reimbursement_payload import _source

    from app.domain.expenses import calculate_expense_totals
    from app.services.oa_reimbursement_payload import build_snapshot

    source = _source()
    value = TripInput.model_validate(
        {
            **source.draft_input.trip.model_dump(by_alias=True),
            "excludedPeriods": [late("2026-09-02", "09:00"), early("2026-09-03")],
        }
    )
    result = calculate_subsidy(
        trip_type=value.subsidy_trip_type(),
        period=value.as_period(),
        configured_daily_rate=Decimal("180"),
        excluded_periods=[item.as_period() for item in value.excluded_periods],
    )
    totals = calculate_expense_totals(source.draft_input.items, result).as_api_dict()
    totals["subsidy"] = result.as_api_dict()
    return build_snapshot(
        replace(
            source,
            draft_input=source.draft_input.model_copy(update={"trip": value}),
            totals_data=totals,
        )
    )


def test_v9_freezes_typed_adjustments_and_oa_explanation_rejecting_tampering():
    import json

    from app.services.oa_reimbursement_payload import parse_snapshot, serialize_snapshot

    snapshot = _adjusted_snapshot()
    assert snapshot.snapshot_version == 10
    encoded = serialize_snapshot(snapshot)
    assert serialize_snapshot(parse_snapshot(encoded)) == encoded
    description = next(
        item.value for item in snapshot.form_values if item.logical_key == "description"
    )
    for text in [
        "延后出发：2026/9/2上午",
        "提前结束：2026/9/3上午",
        "扣除1天",
        "扣除0.5天",
        "原补助3天",
        "实际补助1.5天",
        "270.00元",
    ]:
        assert text in description
    changed = json.loads(encoded)
    changed["input"]["trip"]["excludedPeriods"][0]["endTime"] = "18:00"
    with pytest.raises(ApiError):
        parse_snapshot(
            json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    for version in (7, 8):
        changed = json.loads(encoded)
        changed["snapshotVersion"] = version
        with pytest.raises(ApiError):
            parse_snapshot(
                json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )


def test_excel_keeps_adjustments_in_one_cell_without_duplicate_amounts_or_receipt_counts():
    from io import BytesIO
    from pathlib import Path

    from openpyxl import load_workbook

    from app.services.excel_generator import generate_expense_workbook
    from app.services.oa_reimbursement_payload import snapshot_excel_input

    value = snapshot_excel_input(_adjusted_snapshot())
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
    sheet = workbook.active
    rows = [
        row
        for row in range(4, sheet.max_row + 1)
        if any(label in str(sheet[f"D{row}"].value) for label in ["延后出发", "提前结束"])
    ]
    assert len(rows) == 1
    assert "2026/9/2上午" in sheet[f"D{rows[0]}"].value
    assert "2026/9/3上午" in sheet[f"D{rows[0]}"].value
    assert sheet[f"D{rows[0]}"].value.count("\n") == 2
    assert sheet[f"D{rows[0]}"].alignment.wrap_text
    assert sheet[f"D{rows[0]}"].alignment.horizontal == "left"
    assert sheet[f"D{rows[0]}"].alignment.vertical == "top"
    assert 30 < sheet.row_dimensions[rows[0]].height <= 409.5
    financial_rows = [
        row for row in range(4, sheet.max_row + 1) if sheet[f"B{row}"].value == "出差补助"
    ]
    assert len(financial_rows) == 1
    assert sheet[f"G{financial_rows[0]}"].value == 270
    assert sheet[f"I{financial_rows[0]}"].value == 0
    workbook.close()


def test_excel_keeps_all_one_hundred_adjustments_visible_without_truncation(client_factory):
    from datetime import date, timedelta

    from test_excel import generate, open_result

    from app.excel.template_contract import EXCEL_TEMPLATE

    first = date(2026, 1, 1)
    values = [
        middle(str(first + timedelta(days=index * 3)), str(first + timedelta(days=index * 3 + 1)))
        for index in range(100)
    ]
    client = client_factory()
    output = generate(
        client,
        {
            "project": {"mode": "manual", "text": "合成项目"},
            "trip": trip(*values, startDate=str(first), endDate=str(first + timedelta(days=299))),
            "items": [],
        },
    )
    workbook = open_result(output)
    sheet = workbook[EXCEL_TEMPLATE.sheet_name]
    rows = [row for row in range(4, 55) if sheet[f"D{row}"].value]
    descriptions = [sheet[f"D{row}"].value for row in rows]
    assert 1 < len(rows) < 100
    assert "原补助300天" in descriptions[0]
    assert "实际补助200天" in descriptions[0]
    assert sum(text.count("中途离开") for text in descriptions) == 100
    assert str(first + timedelta(days=298)).replace("-0", "/").replace("-", "/") in descriptions[-1]
    for value in values:
        start = value["startDate"].split("-")
        end = value["endDate"].split("-")
        detail = (
            f"中途离开：{int(start[0])}/{int(start[1])}/{int(start[2])}上午出发，"
            f"{int(end[0])}/{int(end[1])}/{int(end[2])}下午出发返回（扣除1天）"
        )
        assert "\n".join(descriptions).count(detail) == 1
    for row in rows:
        assert not sheet.row_dimensions[row].hidden
        assert sheet[f"D{row}"].alignment.wrap_text
        assert sheet.row_dimensions[row].height <= 409.5
    for row in rows[1:]:
        assert sheet[f"B{row}"].value is None
        assert sheet[f"G{row}"].value is None
        assert sheet[f"I{row}"].value is None
    assert sheet["G4"].value == sheet["G55"].value == 30000
    assert sheet["I55"].value == 0
    workbook.close()


def test_excel_three_adjustments_share_one_cell_and_large_reports_expand(client_factory):
    from datetime import date, timedelta

    from test_excel import generate, open_result

    client = client_factory()
    sample = generate(
        client,
        {
            "project": {"mode": "manual", "text": "合成项目"},
            "trip": trip(late(), early(), middle()),
            "items": [],
        },
    )
    workbook = open_result(sample)
    sheet = workbook.active
    text = sheet["D4"].value
    assert text.count("\n") == 3
    assert all(kind in text for kind in ("延后出发", "提前结束", "中途离开"))
    assert "原补助10天，扣除5天，实际补助5天" in text
    assert sheet["G4"].value == sheet["G55"].value == 750
    assert sheet["D5"].value is None
    assert sheet.row_dimensions[5].hidden
    workbook.close()

    trips = []
    for index in range(5):
        first = date(2026 + index, 1, 1)
        trips.append(trip(
            *[
                middle(str(first + timedelta(days=n * 3)), str(first + timedelta(days=n * 3 + 1)))
                for n in range(100)
            ],
            relatedApprovalId=f"synthetic-approval-{index}",
            startDate=str(first), endDate=str(first + timedelta(days=299)),
        ))
    large = generate(
        client,
        {"project": {"mode": "manual", "text": "合成项目"}, "trips": trips, "items": []},
    )
    workbook = open_result(large)
    sheet = workbook.active
    total_row = next(row[0].row for row in sheet.iter_rows() if row[1].value == "总合计")
    assert total_row > 55
    descriptions = [sheet[f"D{row}"].value or "" for row in range(4, total_row)]
    assert sum(text.count("中途离开") for text in descriptions) == 500
    money_rows = [row for row in range(4, total_row) if sheet[f"B{row}"].value == "出差补助"]
    assert len(money_rows) == 5
    assert sum(sheet[f"G{row}"].value for row in money_rows) == sheet[f"G{total_row}"].value
    assert sheet[f"G{total_row}"].value == 150000
    assert sheet[f"I{total_row}"].value == 0
    assert f"$I${total_row}" in str(sheet.print_area)
    workbook.close()
