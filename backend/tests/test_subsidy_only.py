import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_oa_reimbursement_payload import _attachments, _source

from app.core.errors import ApiError
from app.domain.expenses import calculate_expense_totals
from app.domain.subsidy import calculate_subsidy
from app.services.oa_reimbursement_payload import (
    build_create_command,
    build_snapshot,
    generated_upload_manifest,
    parse_snapshot,
    serialize_snapshot,
    snapshot_sha256,
)


def subsidy_source(*, rate="180", with_materials=False):
    source = _source()
    trip = source.draft_input.trip
    result = calculate_subsidy(
        trip_type=trip.subsidy_trip_type(),
        period=trip.as_period(),
        configured_daily_rate=Decimal(rate),
    )
    totals = calculate_expense_totals([], result).as_api_dict()
    totals["subsidy"] = result.as_api_dict()
    return replace(
        source,
        draft_input=source.draft_input.model_copy(update={"items": []}),
        original_files=(
            tuple(
                replace(file, processing_role="ATTACHMENT_ONLY", attachment_kind="other")
                for file in source.original_files
            )
            if with_materials
            else ()
        ),
        totals_data=totals,
    )


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_subsidy_only_snapshot_and_exact_excel_attachment_manifest():
    snapshot = build_snapshot(subsidy_source())
    assert snapshot.snapshot_version == 11
    assert snapshot.attachment_mode == "excel_only"
    encoded = serialize_snapshot(snapshot)
    assert (
        serialize_snapshot(parse_snapshot(encoded, expected_sha256=snapshot_sha256(snapshot)))
        == encoded
    )
    assert not snapshot.input.items and not snapshot.original_files
    assert snapshot.totals.receipt_count == 0
    assert snapshot.totals.total_amount == snapshot.totals.subsidy_total == Decimal("540")
    assert generated_upload_manifest(snapshot) == (("GENERATED_EXCEL", 0),)
    excel = _attachments(snapshot)[1:]
    command = build_create_command(snapshot, excel)
    assert len(json.loads(command.form_values[-1].value)) == 1
    assert json.loads(command.form_values[-1].value)[0]["fileType"] == "xlsx"
    for attachments in ((), _attachments(snapshot), _attachments(snapshot)[:1], excel * 2):
        with pytest.raises(ApiError):
            build_create_command(snapshot, attachments)


def test_subsidy_only_with_materials_retains_pdf_and_zero_subsidy_cannot_submit():
    snapshot = build_snapshot(subsidy_source(with_materials=True))
    assert snapshot.attachment_mode == "bundle_and_excel"
    assert generated_upload_manifest(snapshot) == (("GENERATED_PDF", 0), ("GENERATED_EXCEL", 1))
    assert (
        len(
            json.loads(build_create_command(snapshot, _attachments(snapshot)).form_values[-1].value)
        )
        == 2
    )
    for with_materials in (False, True):
        with pytest.raises(ApiError):
            build_snapshot(subsidy_source(rate="0", with_materials=with_materials))


@pytest.mark.parametrize(
    "change", ["mode", "missing_mode", "files", "items", "zero", "old_version"]
)
def test_excel_only_snapshot_rejects_manifest_and_content_tampering(change):
    snapshot = build_snapshot(subsidy_source())
    raw = json.loads(serialize_snapshot(snapshot))
    if change == "mode":
        raw["attachmentMode"] = "bundle_and_excel"
    elif change == "missing_mode":
        raw.pop("attachmentMode")
    elif change == "files":
        raw["originalFiles"] = json.loads(serialize_snapshot(build_snapshot(_source())))[
            "originalFiles"
        ]
    elif change == "items":
        raw["input"]["items"] = json.loads(serialize_snapshot(build_snapshot(_source())))["input"][
            "items"
        ]
    elif change == "zero":
        raw["totals"]["subsidyTotal"] = "0.00"
    else:
        raw["snapshotVersion"] = 9
        raw.pop("attachmentMode")
    with pytest.raises(ApiError):
        parse_snapshot(canonical(raw))


def test_v9_canonical_snapshot_keeps_two_attachments_and_no_new_fields():
    raw = json.loads(serialize_snapshot(build_snapshot(_source())))
    raw["snapshotVersion"] = 9
    raw.pop("attachmentMode")
    encoded = canonical(raw)
    restored = parse_snapshot(encoded)
    assert serialize_snapshot(restored) == encoded
    assert len(generated_upload_manifest(restored)) == 2
    assert (
        len(
            json.loads(build_create_command(restored, _attachments(restored)).form_values[-1].value)
        )
        == 2
    )
    with pytest.raises(ApiError):
        build_create_command(restored, _attachments(restored)[1:])
