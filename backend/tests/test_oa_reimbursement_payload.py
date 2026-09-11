from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.domain.expenses import calculate_expense_totals
from app.domain.subsidy import calculate_subsidy
from app.integrations.dingtalk.client import DingTalkOpenAPIClient
from app.integrations.dingtalk.storage import ApprovalAttachment
from app.integrations.dingtalk.workflow import (
    DingTalkWorkflowClient,
    FormComponent,
    FormOption,
    FormSchema,
    schema_fingerprint,
)
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftRelatedApproval,
    ReimbursementDraftStatus,
    ReimbursementOcrStatus,
    utc_now,
)
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services import oa_reimbursement_payload as payloads
from app.services.excel_generator import ResolvedProject
from app.services.oa_reimbursement_payload import (
    OriginalFileSource,
    RelatedApprovalSource,
    SnapshotIdentity,
    SnapshotSource,
    build_create_command,
    build_snapshot,
    collect_snapshot_source,
    create_command_sha256,
    hash_excel_template,
    parse_create_command,
    parse_snapshot,
    serialize_create_command,
    serialize_snapshot,
    snapshot_excel_input,
    snapshot_sha256,
    verify_excel_template,
)
from app.services.oa_template_profiles import (
    OaTemplateCatalogContract,
    ReimbursementTemplateContract,
    TravelTemplateContract,
)
from app.services.reimbursement_drafts import DraftActor, validate_and_calculate_input
from app.services.reimbursement_staging import StagingArea
from app.services.subsidy_calculation import merge_overlapping_subsidy_trips


def _component(
    component_id: str,
    component_type: str,
    label: str,
    *,
    options: tuple[FormOption, ...] = (),
) -> FormComponent:
    return FormComponent(
        component_id=component_id,
        component_type=component_type,
        label=label,
        biz_alias=f"alias-{component_id}",
        required=True,
        disabled=False,
        hidden=False,
        ancestor_disabled=False,
        ancestor_hidden=False,
        nested=False,
        in_subtable=False,
        unsupported_container_ancestor=False,
        value_format=("yyyy-MM-dd" if component_type == "DDDateField" else None),
        unit=None,
        options=options,
        parent_component_id=None,
        related_template_policy=None,
    )


def _schema(process_code: str, components: tuple[FormComponent, ...]) -> FormSchema:
    fingerprint = schema_fingerprint(
        process_code,
        process_code,
        f"uuid-{process_code}",
        "2026-09-04T00:00:00Z",
        "PUBLISHED",
        components,
    )
    return FormSchema(
        process_code=process_code,
        form_code=process_code,
        form_uuid=f"uuid-{process_code}",
        modified_at="2026-09-04T00:00:00Z",
        status="PUBLISHED",
        template_name="差旅费报销申请",
        title="差旅费报销申请",
        components=components,
        fingerprint=fingerprint,
    )


def _catalog() -> OaTemplateCatalogContract:
    company = FormOption(value="北京", label="北京公司", key="company-beijing")
    budget = FormOption(value="26007 项目", label="26007 项目", key="budget-26007")
    travel_type = FormOption(
        value="市外项目出差（短期）",
        label="市外项目出差（短期）",
        key="short-project",
    )
    field_data = (
        ("company", "DDSelectField", "所属公司", (company,)),
        ("budgetCode", "DDSelectField", "预算代码", (budget,)),
        ("travelType", "DDSelectField", "出差类别", (travel_type,)),
        ("startDate", "DDDateField", "开始时间", ()),
        ("endDate", "DDDateField", "结束时间", ()),
        ("durationDays", "NumberField", "时长（天）", ()),
        ("description", "TextareaField", "明细说明", ()),
        ("reimbursementAmount", "NumberField", "报销金额", ()),
        ("relatedApprovals", "RelateField", "关联审批单", ()),
        ("attachments", "DDAttachment", "附件", ()),
    )
    components = tuple(
        _component(f"component-{key}", component_type, label, options=options)
        for key, component_type, label, options in field_data
    )
    reimbursement_schema = _schema("PROC-REIMBURSEMENT", components)
    travel_components = (
        _component("travel-start", "DDDateField", "开始日期"),
        _component("travel-end", "DDDateField", "结束日期"),
    )
    travel_schema = _schema("PROC-TRAVEL", travel_components)
    return OaTemplateCatalogContract(
        config_version=7,
        reimbursement=ReimbursementTemplateContract(
            process_code=reimbursement_schema.process_code,
            schema=reimbursement_schema,
            mappings={key: f"component-{key}" for key, *_rest in field_data},
        ),
        travel_profiles=(
            TravelTemplateContract(
                profile_key="domestic",
                display_name="境内出差",
                process_code=travel_schema.process_code,
                schema=travel_schema,
                start_date_component_id="travel-start",
                end_date_component_id="travel-end",
                travel_type_option=travel_type,
            ),
        ),
    )


def _draft_input(
    *,
    trip: bool = True,
    source_file_id: str | None = "source-file-0",
) -> ReimbursementDraftInput:
    value: dict[str, object] = {
        "ocrDispositionVersion": 1,
        "companyValue": "北京",
        "budgetCodeValue": "26007 项目",
        "project": {"mode": "manual", "text": "26007 MES 项目"},
        "items": [
            {
                "category": "local_transport",
                "date": "2026-09-01",
                "displayDate": "9月1日",
                "description": "机场到酒店",
                "amount": "44.89",
                "receiptCount": 1 if source_file_id else 2,
                **({"sourceFileId": source_file_id} if source_file_id else {}),
            },
            {
                "category": "rail_fare",
                "date": "2026-09-03",
                "displayDate": "9月3日",
                "description": "北京南-合肥南",
                "amount": "473.50",
                "receiptCount": 1,
            },
        ],
        "dismissedOcrFileIds": [],
    }
    if trip:
        value["trip"] = {
            "tripType": "project",
            "startDate": "2026-09-01",
            "startTime": "08:00",
            "endDate": "2026-09-03",
            "endTime": "18:00",
        }
    return ReimbursementDraftInput.model_validate(value)


def _source(*, original_count: int = 2) -> SnapshotSource:
    catalog = _catalog()
    draft_input = _draft_input()
    assert draft_input.trip is not None
    subsidy = calculate_subsidy(
        trip_type=draft_input.trip.subsidy_trip_type(),
        period=draft_input.trip.as_period(),
        configured_daily_rate=Decimal("180.00"),
    )
    totals = calculate_expense_totals(draft_input.items, subsidy)
    totals_data = totals.as_api_dict()
    totals_data["subsidy"] = subsidy.as_api_dict()
    related = (
        RelatedApprovalSource(
            sort_order=0,
            process_instance_id="travel-instance-1",
            profile_key="domestic",
            process_code="PROC-TRAVEL",
            catalog_config_version=catalog.config_version,
            schema_fingerprint=catalog.travel_profiles[0].schema.fingerprint,
            listed_from_ms=1_780_000_000_000,
            listed_to_ms=1_790_000_000_000,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 3),
            title="测试员工提交的境内出差申请",
            business_id="TRAVEL-20260901",
            instance_created_at=datetime(2026, 8, 20, 1, 2, 3),
            verified_at=datetime(2026, 9, 4, 2, 3, 4),
        ),
    )
    originals = tuple(
        OriginalFileSource(
            draft_file_id=f"source-file-{index}",
            sort_order=index,
            processing_role=("EXPENSE_SOURCE" if index == 0 else "ATTACHMENT_ONLY"),
            storage_key=f"drafts/draft-1/{index:08d}-receipt.{extension}",
            file_name=f"票据-{index}.{extension}",
            file_type=extension,
            media_type=("application/pdf" if extension == "pdf" else "image/png"),
            size_bytes=size,
            sha256=str(index + 1) * 64,
            ocr_status=("COMPLETE" if index == 0 else "NOT_REQUESTED"),
        )
        for index, (extension, size) in enumerate((("pdf", 101), ("png", 202))[:original_count])
    )
    return SnapshotSource(
        draft_id="draft-1",
        draft_revision=4,
        identity=SnapshotIdentity(
            corp_id="corp-1",
            user_id="employee-1",
            union_id="union-1",
            name="测试员工",
            department_id="100",
            department_name="工业物联二部",
            microapp_agent_id=4_951_124_324,
        ),
        catalog=catalog,
        draft_input=draft_input,
        resolved_project=ResolvedProject(
            display_text="26007 MES 项目",
            filename_component="26007",
        ),
        totals_data=totals_data,
        related_approvals=related,
        original_files=originals,
        excel_template_sha256="e" * 64,
    )


def _attachments(snapshot) -> tuple[ApprovalAttachment, ...]:
    return (
        ApprovalAttachment(
            space_id="space-1",
            file_id="remote-bundle",
            file_name="票据汇总.pdf",
            file_size=12_345,
            file_type="pdf",
        ),
        ApprovalAttachment(
            space_id="space-1",
            file_id="remote-excel",
            file_name=snapshot.excel.file_name,
            file_size=12_345,
            file_type="xlsx",
        ),
    )


def test_snapshot_is_versioned_canonical_immutable_and_hash_verified() -> None:
    first = build_snapshot(_source())
    second = build_snapshot(_source())

    serialized = serialize_snapshot(first)
    assert serialized == serialize_snapshot(second)
    assert json.loads(serialized)["snapshotVersion"] == payloads.SNAPSHOT_VERSION
    assert parse_snapshot(serialized, expected_sha256=snapshot_sha256(first)) == first
    assert len(first.template.fields) == 10
    assert first.template.schema_fingerprint in first.template.schema_canonical_json
    assert [item.draft_file_id for item in first.original_files] == [
        "source-file-0",
        "source-file-1",
    ]
    assert first.input.items[0].source_file_id == "source-file-0"
    assert json.loads(serialized)["input"]["items"][0]["sourceFileId"] == ("source-file-0")
    assert json.loads(serialized)["input"]["ocrDispositionVersion"] == 1
    assert json.loads(serialized)["input"]["dismissedOcrFileIds"] == []
    with pytest.raises(ValidationError):
        first.identity.name = "篡改"


def test_snapshot_parser_rejects_noncanonical_unknown_version_and_wrong_hash() -> None:
    snapshot = build_snapshot(_source())
    serialized = serialize_snapshot(snapshot)

    with pytest.raises(ApiError, match="快照"):
        parse_snapshot(" " + serialized)
    value = json.loads(serialized)
    value["snapshotVersion"] = 99
    with pytest.raises(ApiError, match="快照"):
        parse_snapshot(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    with pytest.raises(ApiError, match="校验"):
        parse_snapshot(serialized, expected_sha256="0" * 64)


def test_snapshot_rejects_missing_proof_and_unconfirmed_foreign_amount() -> None:
    source = _source()
    for changes in (
        {"requires_itinerary": True},
        {"original_currency": "VND", "original_amount": Decimal("97600000.00")},
        {"original_currency": None, "requires_cny_confirmation": True},
    ):
        items = [
            source.draft_input.items[0].model_copy(update=changes),
            source.draft_input.items[1],
        ]
        with pytest.raises(ApiError):
            build_snapshot(
                replace(source, draft_input=source.draft_input.model_copy(update={"items": items}))
            )


def test_new_snapshot_rejects_multiple_receipts_for_one_source_file():
    source = _source()
    source.draft_input.items[0].receipt_count = 2
    with pytest.raises(ApiError):
        build_snapshot(source)


def test_editing_only_matching_intent_never_changes_snapshot_hash():
    source = _source()
    assert source.draft_input.items[0].itinerary_auto_match_disabled is False
    before = build_snapshot(source)
    source.draft_input.items[0].itinerary_auto_match_disabled = True
    after = build_snapshot(source)
    assert serialize_snapshot(after) == serialize_snapshot(before)
    assert snapshot_sha256(after) == snapshot_sha256(before)
    assert "itineraryAutoMatchDisabled" not in serialize_snapshot(after)
    restored = parse_snapshot(serialize_snapshot(before), expected_sha256=snapshot_sha256(before))
    assert serialize_snapshot(restored) == serialize_snapshot(before)


def test_snapshot_rejects_a_terminal_ocr_file_without_a_disposition() -> None:
    source = _source()
    unlinked = replace(source, draft_input=_draft_input(source_file_id=None))

    with pytest.raises(ApiError, match="快照"):
        build_snapshot(unlinked)


def test_unknown_foreign_currency_confirmation_survives_snapshot_and_worker_readback() -> None:
    source = _source()
    items = [
        source.draft_input.items[0].model_copy(
            update={"requires_cny_confirmation": True, "cny_amount_confirmed": True}
        ),
        source.draft_input.items[1],
    ]
    snapshot = build_snapshot(
        replace(source, draft_input=source.draft_input.model_copy(update={"items": items}))
    )
    serialized = serialize_snapshot(snapshot)
    recovered = parse_snapshot(serialized, expected_sha256=snapshot_sha256(snapshot))
    assert recovered.input.items[0].original_currency is None
    assert recovered.input.items[0].requires_cny_confirmation is True
    assert recovered.input.items[0].cny_amount_confirmed is True
    tampered = recovered.model_copy(
        update={
            "input": recovered.input.model_copy(
                update={
                    "items": (
                        recovered.input.items[0].model_copy(update={"cny_amount_confirmed": False}),
                        recovered.input.items[1],
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="confirmed CNY amount"):
        build_create_command(tampered, _attachments(snapshot))


def test_snapshot_consumers_revalidate_forged_model_copies() -> None:
    snapshot = build_snapshot(_source())
    source_id = snapshot.input.items[0].source_file_id
    assert source_id is not None
    unknown_snapshot_version = snapshot.model_copy(update={"snapshot_version": 99})
    invalid_ocr_disposition = snapshot.model_copy(
        update={"input": snapshot.input.model_copy(update={"ocr_disposition_version": 0})}
    )
    overlapping = snapshot.model_copy(
        update={"input": snapshot.input.model_copy(update={"dismissed_ocr_file_ids": (source_id,)})}
    )
    duplicate_source = snapshot.model_copy(
        update={
            "input": snapshot.input.model_copy(
                update={
                    "items": (
                        snapshot.input.items[0],
                        snapshot.input.items[1].model_copy(update={"source_file_id": source_id}),
                    )
                }
            )
        }
    )
    duplicate_dismissed = snapshot.model_copy(
        update={
            "input": snapshot.input.model_copy(
                update={"dismissed_ocr_file_ids": ("ignored-file", "ignored-file")}
            )
        }
    )
    invalid_role = snapshot.model_copy(
        update={
            "original_files": (
                snapshot.original_files[0],
                snapshot.original_files[1].model_copy(update={"processing_role": "BAD"}),
            )
        }
    )
    invalid_ocr_status = snapshot.model_copy(
        update={
            "original_files": (
                snapshot.original_files[0],
                snapshot.original_files[1].model_copy(update={"ocr_status": "RUNNING"}),
            )
        }
    )

    for invalid in (
        unknown_snapshot_version,
        invalid_ocr_disposition,
        overlapping,
        duplicate_source,
        duplicate_dismissed,
        invalid_role,
        invalid_ocr_status,
    ):
        with pytest.raises(ValueError):
            serialize_snapshot(invalid)
        with pytest.raises(ValueError):
            snapshot_excel_input(invalid)


def test_snapshot_hash_rejects_canonical_tampering() -> None:
    snapshot = build_snapshot(_source())
    serialized = serialize_snapshot(snapshot)
    value = json.loads(serialized)
    value["identity"]["corpId"] = "corp-tampered"
    tampered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    with pytest.raises(ApiError, match="校验"):
        parse_snapshot(tampered, expected_sha256=snapshot_sha256(snapshot))


def test_builds_all_ten_oa_fields_and_round_trips_exact_request() -> None:
    snapshot = build_snapshot(_source())
    attachments = _attachments(snapshot)
    command = build_create_command(snapshot, attachments)
    serialized = serialize_create_command(command)
    body = json.loads(serialized)

    assert body["processCode"] == "PROC-REIMBURSEMENT"
    assert body["originatorUserId"] == "employee-1"
    assert body["deptId"] == 100
    assert body["microappAgentId"] == 4_951_124_324
    values = body["formComponentValues"]
    assert [item["name"] for item in values] == [
        "所属公司",
        "预算代码",
        "出差类别",
        "开始时间",
        "结束时间",
        "时长（天）",
        "明细说明",
        "报销金额",
        "关联审批单",
        "附件",
    ]
    assert [item["value"] for item in values[:6]] == [
        "北京",
        "26007 项目",
        "市外项目出差（短期）",
        "2026-09-01",
        "2026-09-03",
        "3",
    ]
    assert values[7]["value"] == "1058.39"
    assert json.loads(values[8]["value"]) == ["travel-instance-1"]
    assert [item["fileId"] for item in json.loads(values[9]["value"])] == [
        "remote-bundle",
        "remote-excel",
    ]
    assert (
        parse_create_command(
            serialized,
            expected_sha256=create_command_sha256(command),
        )
        == command
    )


def test_snapshot_counts_discontinuous_days_and_merges_overlapping_subsidies() -> None:
    base = _source()
    raw_input = _draft_input(trip=False).model_dump(mode="json", by_alias=True)
    raw_input["trips"] = [
        {
            "tripType": "project",
            "relatedApprovalId": "travel-instance-1",
            "startDate": "2026-09-01",
            "startTime": "09:00",
            "endDate": "2026-09-03",
            "endTime": "18:00",
        },
        {
            "tripType": "project",
            "relatedApprovalId": "travel-instance-2",
            "startDate": "2026-09-03",
            "startTime": "09:00",
            "endDate": "2026-09-05",
            "endTime": "18:00",
        },
        {
            "tripType": "project",
            "relatedApprovalId": "travel-instance-3",
            "startDate": "2026-09-09",
            "startTime": "09:00",
            "endDate": "2026-09-10",
            "endTime": "18:00",
        },
    ]
    draft_input = ReimbursementDraftInput.model_validate(raw_input)
    output_trips = merge_overlapping_subsidy_trips(draft_input.trips)
    subsidies = tuple(
        calculate_subsidy(
            trip_type=trip.subsidy_trip_type(),
            period=trip.as_period(),
            configured_daily_rate=Decimal("180.00"),
            related_approval_id=trip.related_approval_id,
        )
        for trip in output_trips
    )
    totals = calculate_expense_totals(draft_input.items, subsidies)
    totals_data = totals.as_api_dict()
    totals_data["subsidy"] = None
    totals_data["subsidies"] = [item.as_api_dict() for item in subsidies]
    first_related = base.related_approvals[0]
    related = (
        first_related,
        replace(
            first_related,
            sort_order=1,
            process_instance_id="travel-instance-2",
            start_date=date(2026, 9, 3),
            end_date=date(2026, 9, 5),
        ),
        replace(
            first_related,
            sort_order=2,
            process_instance_id="travel-instance-3",
            start_date=date(2026, 9, 9),
            end_date=date(2026, 9, 10),
        ),
    )
    snapshot = build_snapshot(
        replace(
            base,
            draft_input=draft_input,
            totals_data=totals_data,
            related_approvals=related,
        )
    )

    assert snapshot.travel_period.start_date == date(2026, 9, 1)
    assert snapshot.travel_period.end_date == date(2026, 9, 10)
    # OA has one date-range field, so it records the full calendar span.  The
    # two subsidy rows below still omit the uncovered 6th-8th gap.
    assert snapshot.travel_period.duration_days == 10
    assert len(snapshot.subsidies) == 2
    assert len(snapshot_excel_input(snapshot).trips) == 2
    values = {item.logical_key: item.value for item in snapshot.form_values}
    assert values["durationDays"] == "10"


def test_snapshot_allows_expenses_in_one_of_several_discontinuous_approval_periods() -> None:
    base = _source()
    draft_input = _draft_input(trip=False)
    totals = calculate_expense_totals(draft_input.items, None)
    totals_data = totals.as_api_dict()
    totals_data["subsidy"] = None
    totals_data["subsidies"] = []
    first_related = replace(
        base.related_approvals[0],
        start_date=date(2026, 8, 25),
        end_date=date(2026, 8, 25),
    )
    second_related = replace(
        base.related_approvals[0],
        sort_order=1,
        process_instance_id="travel-instance-2",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 3),
    )

    snapshot = build_snapshot(
        replace(
            base,
            draft_input=draft_input,
            totals_data=totals_data,
            related_approvals=(first_related, second_related),
        )
    )

    assert snapshot.travel_period.start_date == date(2026, 8, 25)
    assert snapshot.travel_period.end_date == date(2026, 9, 3)
    assert snapshot.travel_period.duration_days == 10
    assert snapshot.totals.subsidy_total == Decimal("0.00")


def test_create_command_parser_rejects_noncanonical_and_tampered_request() -> None:
    snapshot = build_snapshot(_source())
    command = build_create_command(snapshot, _attachments(snapshot))
    serialized = serialize_create_command(command)
    expected_sha256 = create_command_sha256(command)

    with pytest.raises(ApiError, match="不是规范格式"):
        parse_create_command(" " + serialized)

    value = json.loads(serialized)
    value["originatorUserId"] = "employee-tampered"
    tampered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with pytest.raises(ApiError, match="校验"):
        parse_create_command(tampered, expected_sha256=expected_sha256)


def test_attachment_manifest_requires_bundle_then_generated_excel() -> None:
    snapshot = build_snapshot(_source())
    bundle, generated = _attachments(snapshot)

    with pytest.raises(ApiError, match="首位"):
        build_create_command(snapshot, (generated, bundle))
    with pytest.raises(ApiError, match="数量"):
        build_create_command(snapshot, (bundle,))

    renamed_excel = ApprovalAttachment(
        space_id=generated.space_id,
        file_id=generated.file_id,
        file_name="差旅费报销单-测试员工-26007 (1).xlsx",
        file_size=generated.file_size,
        file_type=generated.file_type,
    )
    command = build_create_command(snapshot, (bundle, renamed_excel))
    assert json.loads(command.form_values[-1].value)[-1]["fileName"].endswith("(1).xlsx")


def test_excel_input_is_rebuilt_only_for_the_frozen_template(tmp_path: Path) -> None:
    template = tmp_path / "template.xlsx"
    template.write_bytes(b"template-v1")
    source = _source()
    source = replace(
        source,
        excel_template_sha256=hash_excel_template(template),
    )
    snapshot = build_snapshot(source)
    excel_input = snapshot_excel_input(snapshot)

    assert excel_input.employee_name == "测试员工"
    assert excel_input.department_name == "工业物联二部"
    assert excel_input.project.display_text == "26007 项目"
    assert excel_input.totals.total_amount == Decimal("1058.39")
    assert not hasattr(excel_input.items[0], "source_file_id")
    verify_excel_template(snapshot, template)

    template.write_bytes(b"template-v2")
    with pytest.raises(ApiError, match="模板已更新"):
        verify_excel_template(snapshot, template)


@pytest.mark.asyncio
async def test_workflow_transmits_the_same_json_contract_that_is_hashed(settings_factory) -> None:
    snapshot = build_snapshot(_source(original_count=1))
    command = build_create_command(snapshot, _attachments(snapshot))
    expected = serialize_create_command(command)
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        seen.append(request.content)
        return httpx.Response(200, json={"instanceId": "instance-1"})

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    workflow = DingTalkWorkflowClient(client)
    try:
        created = await workflow.create_process_instance(command)
    finally:
        await client.close()

    assert created.instance_id == "instance-1"
    assert seen[0].decode("utf-8") == expected


def test_collect_snapshot_source_reads_review_ready_state_without_mutating(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory()
    catalog = _catalog()
    monkeypatch.setattr(payloads, "require_submission_ready_catalog", lambda _db: catalog)
    now = utc_now()
    draft_input = _draft_input(trip=False, source_file_id="snapshot-source-file").model_copy(
        update={"accounting_source_verified": True}
    )
    actor = DraftActor(
        corp_id="corp-fixed",
        user_id="employee-1",
        department_id="100",
        department_name="工业物联二部",
    )
    with client.app.state.database_session_factory() as database:
        staging = client.app.state.reimbursement_staging
        file_content = b"a" * 101
        calculation = validate_and_calculate_input(
            database,
            catalog=catalog,
            draft_input=draft_input,
            max_items=100,
        )
        draft = ReimbursementDraft(
            id="11111111-1111-4111-8111-111111111111",
            corp_id=actor.corp_id,
            owner_user_id=actor.user_id,
            status=ReimbursementDraftStatus.REVIEW_READY.value,
            revision=5,
            department_id=actor.department_id,
            department_name=actor.department_name,
            template_process_code=catalog.reimbursement.process_code,
            template_config_version=catalog.config_version,
            schema_fingerprint=catalog.reimbursement.schema.fingerprint,
            input_json=calculation.canonical_json,
            related_instance_ids_json='["travel-instance-1"]',
            expires_at=now.replace(year=now.year + 1),
        )
        database.add(draft)
        database.flush()
        reservation = staging.new_reservation(
            StagingArea.DRAFTS,
            draft.id,
            "pdf",
            reserved_bytes=len(file_content),
        )
        staged = staging.write_bytes(reservation, file_content)
        database.add_all(
            [
                ReimbursementDraftRelatedApproval(
                    draft_id=draft.id,
                    corp_id=draft.corp_id,
                    owner_user_id=draft.owner_user_id,
                    sort_order=0,
                    process_instance_id="travel-instance-1",
                    travel_profile_key="domestic",
                    process_code="PROC-TRAVEL",
                    catalog_config_version=catalog.config_version,
                    travel_schema_fingerprint=catalog.travel_profiles[0].schema.fingerprint,
                    listed_from_ms=1_780_000_000_000,
                    listed_to_ms=1_790_000_000_000,
                    travel_start_date=date(2026, 9, 1),
                    travel_end_date=date(2026, 9, 3),
                    title="境内出差",
                    business_id="TRAVEL-1",
                    instance_created_at=now,
                    verified_at=now,
                ),
                ReimbursementDraftFile(
                    id="snapshot-source-file",
                    draft_id=draft.id,
                    # Historical draft positions may contain gaps after a
                    # deleted file; the submission manifest is compact.
                    sort_order=4,
                    processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE.value,
                    file_status=ReimbursementDraftFileStatus.ACTIVE.value,
                    storage_key=staged.storage_key,
                    part_storage_key=None,
                    reserved_bytes=101,
                    reservation_expires_at=None,
                    original_name="发票.pdf",
                    extension="pdf",
                    media_type="application/pdf",
                    size_bytes=staged.size_bytes,
                    sha256=staged.sha256,
                    ocr_status=ReimbursementOcrStatus.COMPLETE.value,
                    ocr_result_json="{}",
                ),
            ]
        )
        database.commit()
        unlinked_calculation = validate_and_calculate_input(
            database,
            catalog=catalog,
            draft_input=_draft_input(trip=False, source_file_id=None).model_copy(
                update={"accounting_source_verified": True}
            ),
            max_items=100,
        )
        draft.input_json = unlinked_calculation.canonical_json
        database.commit()
        with pytest.raises(ApiError, match="确认每张已识别票据"):
            collect_snapshot_source(
                database,
                staging=staging,
                actor=actor,
                originator_union_id="union-1",
                originator_name="测试员工",
                draft_id=draft.id,
                expected_revision=5,
                microapp_agent_id=4_951_124_324,
                excel_template_path=client.app.state.settings.excel_template_path,
                max_items=100,
            )
        draft.input_json = calculation.canonical_json
        database.commit()
        source = collect_snapshot_source(
            database,
            staging=staging,
            actor=actor,
            originator_union_id="union-1",
            originator_name="测试员工",
            draft_id=draft.id,
            expected_revision=5,
            microapp_agent_id=4_951_124_324,
            excel_template_path=client.app.state.settings.excel_template_path,
            max_items=100,
        )

        assert source.draft_revision == 5
        assert source.related_approvals[0].process_instance_id == "travel-instance-1"
        assert source.original_files[0].storage_key == staged.storage_key
        assert source.original_files[0].sort_order == 0
        database.expire_all()
        unchanged = database.get(ReimbursementDraft, draft.id)
        assert unchanged is not None
        assert unchanged.status == ReimbursementDraftStatus.REVIEW_READY.value
        assert unchanged.revision == 5

        file_record = database.get(ReimbursementDraftFile, "snapshot-source-file")
        assert file_record is not None
        file_record.sha256 = "0" * 64
        database.commit()
        with pytest.raises(ApiError) as changed:
            collect_snapshot_source(
                database,
                staging=staging,
                actor=actor,
                originator_union_id="union-1",
                originator_name="测试员工",
                draft_id=draft.id,
                expected_revision=5,
                microapp_agent_id=4_951_124_324,
                excel_template_path=client.app.state.settings.excel_template_path,
                max_items=100,
            )
        assert changed.value.code == "REIMBURSEMENT_DRAFT_FILE_CHANGED"
        database.refresh(draft)
        assert draft.status == ReimbursementDraftStatus.REVIEW_READY.value
        assert draft.locked_at is None
