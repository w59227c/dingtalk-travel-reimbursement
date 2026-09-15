from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.categories import CATEGORY_BY_ID, ExpenseCategory
from app.domain.expenses import ExpenseTotals, calculate_expense_totals
from app.domain.money import money_string
from app.domain.reimbursement_proofs import payment_proof_required
from app.domain.subsidy import SubsidyCalculation, TripType
from app.integrations.dingtalk.storage import ApprovalAttachment
from app.integrations.dingtalk.workflow import (
    CreateProcessInstanceCommand,
    CreateWorkflowFormValue,
    FormOption,
    form_schema_from_dict,
    serialize_create_process_instance_command,
)
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftRelatedApproval,
    ReimbursementDraftStatus,
    ReimbursementOcrStatus,
    ReimbursementSubmission,
    utc_now,
)
from app.schemas.excel import ExcelExpenseItemInput
from app.schemas.expenses import TripInput, TripPurpose
from app.schemas.primitives import DecimalString, MinuteTime, StrictCalendarDate
from app.schemas.reimbursements import (
    ReimbursementDraftExpenseItemInput,
    ReimbursementDraftInput,
)
from app.services.excel_generator import (
    XLSX_MEDIA_TYPE,
    ResolvedProject,
    build_download_filename,
    sanitize_filename_component,
)
from app.services.oa_template_profiles import (
    REIMBURSEMENT_LOGICAL_FIELD_SPECS,
    OaTemplateCatalogContract,
    require_submission_ready_catalog,
)
from app.services.reimbursement_drafts import (
    DraftActor,
    apply_ocr_evidence,
    complete_expense_items,
    require_owned_draft,
    validate_and_calculate_input,
    validate_draft_file_references,
)
from app.services.reimbursement_ocr_state import recover_stale_running_ocr
from app.services.reimbursement_staging import (
    ReimbursementStaging,
    ReimbursementStagingError,
)
from app.services.subsidy_calculation import (
    merge_overlapping_subsidy_trips,
    subsidy_purpose_for_travel_type,
)

SNAPSHOT_VERSION = 6
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_SIGNED_INT64_MAX = 9_223_372_036_854_775_807
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_POSITIVE_DECIMAL_IDENTIFIER = re.compile(r"^[1-9]\d{0,18}$")
_OA_LOGICAL_KEYS = tuple(item.key for item in REIMBURSEMENT_LOGICAL_FIELD_SPECS)
_OA_VALUE_KEYS = _OA_LOGICAL_KEYS[:-1]
_ATTACHMENTS_KEY = _OA_LOGICAL_KEYS[-1]
_TRANSITIONAL_FILE_STATUSES = frozenset(
    {
        ReimbursementDraftFileStatus.RESERVED.value,
        ReimbursementDraftFileStatus.WRITING.value,
        ReimbursementDraftFileStatus.DELETING.value,
    }
)

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=255)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Sha256Text = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class SnapshotIdentity(_SnapshotModel):
    corp_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    user_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    union_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    department_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    department_name: ShortText
    microapp_agent_id: PositiveInt


class SnapshotOption(_SnapshotModel):
    value: LongText
    label: LongText
    key: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None


class SnapshotTemplateField(_SnapshotModel):
    logical_key: ShortText
    component_id: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    component_type: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    name: ShortText
    biz_alias: ShortText | None = None


class SnapshotTravelProfile(_SnapshotModel):
    profile_key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    process_code: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    schema_fingerprint: Sha256Text
    start_date_component_id: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    end_date_component_id: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    travel_type_option: SnapshotOption
    company_component_id: str | None = None
    budget_code_component_id: str | None = None
    travel_type_component_id: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    travel_type_mappings: dict[str, SnapshotOption] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SnapshotTemplate(_SnapshotModel):
    config_version: PositiveInt
    process_code: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    template_name: ShortText
    schema_fingerprint: Sha256Text
    schema_canonical_json: Annotated[
        str,
        StringConstraints(min_length=2, max_length=2 * 1024 * 1024),
    ]
    fields: tuple[SnapshotTemplateField, ...]
    travel_profiles: tuple[SnapshotTravelProfile, ...]


class SnapshotProject(_SnapshotModel):
    mode: Literal["selected", "manual"]
    selected_id: PositiveInt | None = None
    manual_text: LongText | None = None
    display_text: LongText
    filename_component: Annotated[str, StringConstraints(min_length=1, max_length=255)]

    @model_validator(mode="after")
    def validate_source(self) -> SnapshotProject:
        if self.mode == "selected" and (self.selected_id is None or self.manual_text is not None):
            raise ValueError("selected projects require only selectedId")
        if self.mode == "manual" and (self.manual_text is None or self.selected_id is not None):
            raise ValueError("manual projects require only manualText")
        return self


class SnapshotTrip(_SnapshotModel):
    trip_type: TripPurpose
    related_approval_id: ShortText | None = None
    start_date: StrictCalendarDate
    start_time: MinuteTime
    end_date: StrictCalendarDate
    end_time: MinuteTime
    policy_confirmed: bool
    confirmed_effective_days: DecimalString | None = None
    no_subsidy_exception: bool
    manual_subsidy_amount: DecimalString | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_serializer("start_time", "end_time", when_used="json")
    def serialize_minute_time(self, value: object) -> str:
        return value.strftime("%H:%M")


class SnapshotExpenseItem(_SnapshotModel):
    category: ExpenseCategory
    date: StrictCalendarDate
    display_date: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    amount: DecimalString
    receipt_count: Annotated[int, Field(strict=True, ge=1, le=10_000)]
    source_file_id: Annotated[str, StringConstraints(min_length=1, max_length=36)] | None = None
    itinerary_file_ids: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=36)], ...
    ] = ()
    requires_itinerary: bool = False
    payment_proof_file_ids: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=36)], ...
    ] = ()
    hotel_bill_file_ids: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=36)], ...
    ] = ()
    rail_type: Literal["high_speed", "emu", "regular", "unknown"] = "unknown"
    transport_type: Literal["ride_hailing", "taxi", "rail", "hotel", "other"] | None = None
    original_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    original_amount: DecimalString | None = None
    cny_amount_confirmed: bool = False
    requires_cny_confirmation: bool = False


class SnapshotInput(_SnapshotModel):
    ocr_disposition_version: Literal[1]
    company_value: LongText
    budget_code_value: LongText
    project: SnapshotProject
    trip: SnapshotTrip | None
    trips: tuple[SnapshotTrip, ...] = ()
    items: tuple[SnapshotExpenseItem, ...]
    dismissed_ocr_file_ids: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=36)], ...
    ] = ()

    @model_validator(mode="after")
    def validate_ocr_dispositions(self) -> SnapshotInput:
        source_ids = [item.source_file_id for item in self.items if item.source_file_id is not None]
        dismissed_ids = list(self.dismissed_ocr_file_ids)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("OCR source file ids must be unique")
        if len(dismissed_ids) != len(set(dismissed_ids)):
            raise ValueError("OCR dismissed file ids must be unique")
        if set(source_ids).intersection(dismissed_ids):
            raise ValueError("OCR disposition cannot be both linked and dismissed")
        return self


class SnapshotSubsidy(_SnapshotModel):
    trip_type: TripType
    calendar_days: PositiveInt
    effective_days: DecimalString
    daily_rate: DecimalString
    total: DecimalString
    related_approval_id: ShortText | None = None


class SnapshotTotals(_SnapshotModel):
    expense_total: DecimalString
    subsidy_total: DecimalString
    total_amount: DecimalString
    receipt_count: NonNegativeInt
    uppercase_amount: Annotated[str, StringConstraints(min_length=1, max_length=100)]


class SnapshotRelatedApproval(_SnapshotModel):
    sort_order: NonNegativeInt
    process_instance_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    profile_key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    process_code: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    catalog_config_version: PositiveInt
    schema_fingerprint: Sha256Text
    listed_from_ms: NonNegativeInt
    listed_to_ms: NonNegativeInt
    start_date: StrictCalendarDate
    end_date: StrictCalendarDate
    title: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    business_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    instance_created_at: ShortText
    verified_at: ShortText
    originator_department_id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128),
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    source_travel_type_value: LongText | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SnapshotOriginalFile(_SnapshotModel):
    draft_file_id: Annotated[str, StringConstraints(min_length=1, max_length=36)]
    sort_order: NonNegativeInt
    processing_role: Literal["EXPENSE_SOURCE", "ATTACHMENT_ONLY"]
    attachment_kind: Literal["itinerary", "payment_proof", "hotel_bill", "other"] = "other"
    storage_key: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    file_name: ShortText
    file_type: Literal["jpg", "jpeg", "png", "pdf"]
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    size_bytes: PositiveInt
    sha256: Sha256Text
    ocr_status: Literal["NOT_REQUESTED", "COMPLETE", "FAILED"]


class SnapshotExcel(_SnapshotModel):
    template_sha256: Sha256Text
    file_name: ShortText
    file_type: Literal["xlsx"] = "xlsx"
    media_type: Literal["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"] = (
        XLSX_MEDIA_TYPE
    )


class SnapshotTravelPeriod(_SnapshotModel):
    start_date: StrictCalendarDate
    end_date: StrictCalendarDate
    duration_days: PositiveInt


class SnapshotSelections(_SnapshotModel):
    company: SnapshotOption
    budget_code: SnapshotOption
    travel_type: SnapshotOption


class SnapshotFormValue(_SnapshotModel):
    logical_key: ShortText
    value: Annotated[str, StringConstraints(max_length=2 * 1024 * 1024)]


class ReimbursementSnapshot(_SnapshotModel):
    snapshot_version: Literal[6] = SNAPSHOT_VERSION
    draft_id: Annotated[str, StringConstraints(min_length=1, max_length=36)]
    draft_revision: PositiveInt
    identity: SnapshotIdentity
    template: SnapshotTemplate
    selections: SnapshotSelections
    input: SnapshotInput
    subsidy: SnapshotSubsidy | None
    subsidies: tuple[SnapshotSubsidy, ...] = ()
    totals: SnapshotTotals
    travel_period: SnapshotTravelPeriod
    related_approvals: tuple[SnapshotRelatedApproval, ...]
    original_files: tuple[SnapshotOriginalFile, ...]
    excel: SnapshotExcel
    form_values: tuple[SnapshotFormValue, ...]

    @model_validator(mode="after")
    def validate_complete_snapshot(self) -> ReimbursementSnapshot:
        _validate_snapshot_semantics(self)
        return self


@dataclass(frozen=True, slots=True)
class RelatedApprovalSource:
    sort_order: int
    process_instance_id: str
    profile_key: str
    process_code: str
    catalog_config_version: int
    schema_fingerprint: str
    listed_from_ms: int
    listed_to_ms: int
    start_date: date
    end_date: date
    title: str
    business_id: str
    instance_created_at: datetime
    verified_at: datetime
    originator_department_id: str | None = None
    source_travel_type_value: str | None = None


@dataclass(frozen=True, slots=True)
class OriginalFileSource:
    draft_file_id: str
    sort_order: int
    processing_role: str
    storage_key: str
    file_name: str
    file_type: str
    media_type: str
    size_bytes: int
    sha256: str
    ocr_status: str
    attachment_kind: str = "other"


@dataclass(frozen=True, slots=True)
class SnapshotSource:
    draft_id: str
    draft_revision: int
    identity: SnapshotIdentity
    catalog: OaTemplateCatalogContract
    draft_input: ReimbursementDraftInput
    resolved_project: ResolvedProject
    totals_data: dict[str, object]
    related_approvals: tuple[RelatedApprovalSource, ...]
    original_files: tuple[OriginalFileSource, ...]
    excel_template_sha256: str


@dataclass(frozen=True, slots=True)
class ExcelGenerationInput:
    employee_name: str
    department_name: str
    project: ResolvedProject
    trip: TripInput | None
    trips: tuple[TripInput, ...]
    items: tuple[ExcelExpenseItemInput, ...]
    subsidy: SubsidyCalculation | None
    subsidies: tuple[SubsidyCalculation, ...]
    totals: ExpenseTotals


def collect_snapshot_source(
    database: Session,
    *,
    staging: ReimbursementStaging,
    actor: DraftActor,
    originator_union_id: str,
    originator_name: str,
    draft_id: str,
    expected_revision: int,
    microapp_agent_id: int,
    excel_template_path: Path,
    max_items: int,
    ocr_timeout_seconds: int,
) -> SnapshotSource:
    """Recover interrupted OCR, then read one fail-closed submission view.

    Recovery may settle stale file markers without changing the draft revision.
    The caller must immediately claim the same draft/revision through the
    submission state service in this Session after this function returns.
    """

    _require_positive_integer(expected_revision, "expected_revision")
    identity = _snapshot_identity(
        actor=actor,
        union_id=originator_union_id,
        name=originator_name,
        microapp_agent_id=microapp_agent_id,
    )
    draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
        mutable=True,
        now=utc_now(),
    )
    recover_stale_running_ocr(
        database,
        draft_id=draft.id,
        ocr_timeout_seconds=ocr_timeout_seconds,
    )
    if draft.status != ReimbursementDraftStatus.REVIEW_READY.value or draft.locked_at is not None:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_NOT_READY",
            "请完成并确认报销内容后再提交",
            409,
        )
    if draft.revision != expected_revision:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
            "报销内容版本已更新，请刷新后重试",
            409,
        )

    catalog = require_submission_ready_catalog(database)
    if (
        draft.template_process_code != catalog.reimbursement.process_code
        or draft.template_config_version != catalog.config_version
        or draft.schema_fingerprint != catalog.reimbursement.schema.fingerprint
    ):
        raise _snapshot_error("审批模板已更新，请刷新后重新确认")
    try:
        draft_input = ReimbursementDraftInput.model_validate_json(draft.input_json)
    except ValidationError:
        raise _snapshot_error("报销数据损坏，请重新填写") from None
    if not draft_input.accounting_source_verified:
        raise _snapshot_error("请重新确认关联出差审批，以核验所属公司和预算代码")
    draft_input = apply_ocr_evidence(database, draft_id=draft.id, draft_input=draft_input)
    calculation = validate_and_calculate_input(
        database,
        catalog=catalog,
        draft_input=draft_input,
        max_items=max_items,
    )
    if calculation.canonical_json != draft.input_json:
        raise _snapshot_error("报销内容已变化，请重新确认后再提交")

    related_rows = database.scalars(
        select(ReimbursementDraftRelatedApproval)
        .where(ReimbursementDraftRelatedApproval.draft_id == draft.id)
        .order_by(ReimbursementDraftRelatedApproval.sort_order)
    ).all()
    related = tuple(_related_source(item) for item in related_rows)
    _validate_related_sources(draft.related_instance_ids_json, related, catalog, draft_input)

    all_files = database.scalars(
        select(ReimbursementDraftFile)
        .where(ReimbursementDraftFile.draft_id == draft.id)
        .order_by(ReimbursementDraftFile.sort_order, ReimbursementDraftFile.id)
    ).all()
    if any(
        item.file_status in _TRANSITIONAL_FILE_STATUSES
        or (
            item.file_status == ReimbursementDraftFileStatus.ACTIVE.value
            and item.ocr_status == ReimbursementOcrStatus.RUNNING.value
        )
        or (
            item.file_status == ReimbursementDraftFileStatus.ACTIVE.value
            and item.processing_role == ReimbursementDraftFileRole.EXPENSE_SOURCE.value
            and item.ocr_status == ReimbursementOcrStatus.NOT_REQUESTED.value
        )
        for item in all_files
    ):
        raise ApiError(
            "REIMBURSEMENT_DRAFT_NOT_READY",
            "附件仍在上传、删除或识别中，请稍后重试",
            409,
        )
    active_files = tuple(
        item for item in all_files if item.file_status == ReimbursementDraftFileStatus.ACTIVE.value
    )
    validate_draft_file_references(
        database,
        draft_id=draft.id,
        draft_input=draft_input,
        require_terminal_disposition=True,
        require_submission_proofs=True,
    )
    # Upload sort_order is the compact manifest order. Deleted draft files may
    # legitimately leave gaps in their own ordering.
    ordered_ids = list(
        dict.fromkeys(
            file_id
            for item in draft_input.items
            for file_id in ([item.source_file_id] if item.source_file_id else [])
            + item.hotel_bill_file_ids
            + item.itinerary_file_ids
            + item.payment_proof_file_ids
        )
    )
    active_by_id = {item.id: item for item in active_files}
    ordered_files = [active_by_id[file_id] for file_id in ordered_ids]
    ordered_files.extend(item for item in active_files if item.id not in set(ordered_ids))
    originals = tuple(
        _original_source(item, manifest_order=manifest_order)
        for manifest_order, item in enumerate(ordered_files)
    )
    if not originals:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_NOT_READY",
            "请先上传至少一个有效附件",
            409,
        )
    try:
        for original in originals:
            with staging.open_verified(
                original.storage_key,
                expected_size=original.size_bytes,
                expected_sha256=original.sha256,
            ):
                pass
    except (OSError, ReimbursementStagingError):
        raise ApiError(
            "REIMBURSEMENT_DRAFT_FILE_CHANGED",
            "附件文件已丢失或内容发生变化，请删除后重新上传",
            409,
        ) from None
    return SnapshotSource(
        draft_id=draft.id,
        draft_revision=draft.revision,
        identity=identity,
        catalog=catalog,
        draft_input=draft_input,
        resolved_project=ResolvedProject(
            display_text=_mapped_option(catalog, "budgetCode", draft_input.budget_code_value).label,
            filename_component=sanitize_filename_component(
                _mapped_option(catalog, "budgetCode", draft_input.budget_code_value).label
            ),
        ),
        totals_data=calculation.totals_data,
        related_approvals=related,
        original_files=originals,
        excel_template_sha256=hash_excel_template(excel_template_path),
    )


def build_snapshot(source: SnapshotSource) -> ReimbursementSnapshot:
    """Build the versioned immutable payload persisted with a submission."""

    try:
        company = _mapped_option(source.catalog, "company", source.draft_input.company_value)
        budget = _mapped_option(
            source.catalog,
            "budgetCode",
            source.draft_input.budget_code_value,
        )
        travel_type = _travel_type_option(source.catalog, source.related_approvals)
        start_date = min(item.start_date for item in source.related_approvals)
        end_date = max(item.end_date for item in source.related_approvals)
        # DingTalk exposes one reimbursement date range.  It therefore records
        # the complete earliest-to-latest span, while subsidy calculation keeps
        # using the separately persisted real approval periods and excludes gaps.
        duration_days = (end_date - start_date).days + 1
        template = _snapshot_template(source.catalog)
        project = SnapshotProject(
            mode="manual",
            manual_text=budget.label,
            display_text=budget.label,
            filename_component=sanitize_filename_component(budget.label),
        )
        trip = _snapshot_trip(source.draft_input.trip)
        trips = tuple(_snapshot_trip(item) for item in source.draft_input.trips)
        items = tuple(_snapshot_expense_item(item) for item in source.draft_input.items)
        subsidy = _snapshot_subsidy(source.totals_data.get("subsidy"))
        subsidies = tuple(
            _snapshot_subsidy(item) for item in source.totals_data.get("subsidies", [])
        )
        totals = _snapshot_totals(source.totals_data)
        related = tuple(_snapshot_related(item) for item in source.related_approvals)
        originals = tuple(_snapshot_original(item) for item in source.original_files)
        excel = SnapshotExcel(
            template_sha256=source.excel_template_sha256,
            file_name=build_download_filename(
                source.identity.name,
                project.filename_component,
            ),
        )
        form_values = _snapshot_form_values(
            company=company,
            budget=budget,
            travel_type=travel_type,
            start_date=start_date,
            end_date=end_date,
            duration_days=duration_days,
            items=items,
            subsidy=subsidy,
            subsidies=subsidies,
            totals=totals,
            related=related,
        )
        return ReimbursementSnapshot(
            draft_id=source.draft_id,
            draft_revision=source.draft_revision,
            identity=source.identity,
            template=template,
            selections=SnapshotSelections(
                company=_snapshot_option(company),
                budget_code=_snapshot_option(budget),
                travel_type=_snapshot_option(travel_type),
            ),
            input=SnapshotInput(
                ocr_disposition_version=source.draft_input.ocr_disposition_version,
                company_value=source.draft_input.company_value,
                budget_code_value=source.draft_input.budget_code_value,
                project=project,
                trip=trip,
                trips=trips,
                items=items,
                dismissed_ocr_file_ids=tuple(source.draft_input.dismissed_ocr_file_ids),
            ),
            subsidy=subsidy,
            subsidies=subsidies,
            totals=totals,
            travel_period=SnapshotTravelPeriod(
                start_date=start_date,
                end_date=end_date,
                duration_days=duration_days,
            ),
            related_approvals=related,
            original_files=originals,
            excel=excel,
            form_values=form_values,
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise _snapshot_error("报销提交快照无法生成，请刷新后重试") from None


def serialize_snapshot(snapshot: ReimbursementSnapshot) -> str:
    snapshot = _require_snapshot(snapshot)
    return _canonical_json(snapshot.model_dump(mode="json", by_alias=True))


def snapshot_sha256(snapshot: ReimbursementSnapshot) -> str:
    return hashlib.sha256(serialize_snapshot(snapshot).encode("utf-8")).hexdigest()


def parse_snapshot(
    raw: str,
    *,
    expected_sha256: str | None = None,
) -> ReimbursementSnapshot:
    try:
        value = _load_canonical_json(raw, maximum_bytes=_MAX_SNAPSHOT_BYTES)
        snapshot = ReimbursementSnapshot.model_validate(value)
    except (ApiError, TypeError, ValueError, ValidationError):
        raise _snapshot_error("报销提交快照损坏，请联系管理员") from None
    canonical = serialize_snapshot(snapshot)
    if canonical != raw:
        raise _snapshot_error("报销提交快照不是规范格式，请联系管理员")
    if expected_sha256 is not None:
        _verify_sha256(canonical, expected_sha256, "报销提交快照校验失败，请联系管理员")
    return snapshot


def parse_locked_submission_snapshot(
    draft: ReimbursementDraft,
    submission: ReimbursementSubmission,
) -> ReimbursementSnapshot:
    """Read and cross-check the immutable snapshot behind one locked draft."""

    snapshot = parse_snapshot(
        submission.form_snapshot_json,
        expected_sha256=submission.snapshot_sha256,
    )
    if (
        draft.status != ReimbursementDraftStatus.LOCKED.value
        or draft.locked_at is None
        or submission.draft_id != draft.id
        or submission.corp_id != draft.corp_id
        or submission.originator_user_id != draft.owner_user_id
        or snapshot.draft_id != draft.id
        or snapshot.draft_revision + 1 != draft.revision
        or snapshot.identity.corp_id != draft.corp_id
        or snapshot.identity.user_id != draft.owner_user_id
        or snapshot.identity.department_id != draft.department_id
        or snapshot.identity.department_name != draft.department_name
        or snapshot.template.process_code != draft.template_process_code
        or snapshot.template.config_version != draft.template_config_version
        or snapshot.template.schema_fingerprint != draft.schema_fingerprint
    ):
        raise _snapshot_error("报销提交快照与锁定记录不一致，请联系管理员")
    return snapshot


def snapshot_excel_input(snapshot: ReimbursementSnapshot) -> ExcelGenerationInput:
    snapshot = _require_snapshot(snapshot)
    request = draft_input_from_snapshot(snapshot)
    subsidy = _subsidy_from_snapshot(snapshot.subsidy)
    subsidies = tuple(_subsidy_from_snapshot(item) for item in snapshot.subsidies)
    totals = _totals_from_snapshot(snapshot.totals)
    output_trips = merge_overlapping_subsidy_trips(
        request.trips or ([request.trip] if request.trip is not None else [])
    )
    return ExcelGenerationInput(
        employee_name=snapshot.identity.name,
        department_name=snapshot.identity.department_name,
        project=ResolvedProject(
            display_text=snapshot.input.project.display_text,
            filename_component=snapshot.input.project.filename_component,
        ),
        trip=output_trips[0] if request.trip is not None and output_trips else None,
        trips=tuple(output_trips) if request.trips else (),
        items=tuple(complete_expense_items(request)),
        subsidy=subsidy,
        subsidies=tuple(item for item in subsidies if item is not None),
        totals=totals,
    )


def verify_excel_template(snapshot: ReimbursementSnapshot, template_path: Path) -> None:
    snapshot = _require_snapshot(snapshot)
    observed = hash_excel_template(template_path)
    if not hmac.compare_digest(observed, snapshot.excel.template_sha256):
        raise ApiError(
            "EXCEL_TEMPLATE_CHANGED",
            "报销 Excel 模板已更新，请重新确认后提交",
            409,
        )


def hash_excel_template(template_path: Path) -> str:
    path = Path(template_path)
    try:
        if not path.is_file():
            raise OSError
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        raise ApiError(
            "EXCEL_TEMPLATE_UNAVAILABLE",
            "报销 Excel 模板不可用，请联系管理员",
            500,
        ) from None


def build_create_command(
    snapshot: ReimbursementSnapshot,
    attachments: Sequence[ApprovalAttachment],
) -> CreateProcessInstanceCommand:
    """Build all ten OA fields, requiring originals first and Excel last."""

    snapshot = _require_snapshot(snapshot)
    normalized_attachments = _validate_attachment_manifest(snapshot, attachments)
    values_by_key = {item.logical_key: item.value for item in snapshot.form_values}
    values_by_key[_ATTACHMENTS_KEY] = _canonical_json(
        [item.as_oa_value() for item in normalized_attachments]
    )
    bindings = {item.logical_key: item for item in snapshot.template.fields}
    has_range = bindings["startDate"].component_type == "DDDateRangeField"
    if has_range:
        values_by_key["startDate"] = _canonical_json(
            [values_by_key["startDate"], values_by_key["endDate"]]
        )
    form_values = tuple(
        CreateWorkflowFormValue(
            name=bindings[key].name,
            value=values_by_key[key],
            component_id=bindings[key].component_id,
            component_type=bindings[key].component_type,
            biz_alias=bindings[key].biz_alias,
        )
        for key in _OA_LOGICAL_KEYS
        if not has_range or key not in {"endDate", "durationDays"}
    )
    try:
        department_id = int(snapshot.identity.department_id)
    except ValueError:
        raise _snapshot_error("发起部门标识无效，请重新进入应用") from None
    return CreateProcessInstanceCommand(
        process_code=snapshot.template.process_code,
        originator_user_id=snapshot.identity.user_id,
        department_id=department_id,
        microapp_agent_id=snapshot.identity.microapp_agent_id,
        form_values=form_values,
    )


def serialize_create_command(command: CreateProcessInstanceCommand) -> str:
    """Canonical request JSON; DingTalkWorkflowClient transmits this same contract."""

    return serialize_create_process_instance_command(command)


def create_command_sha256(command: CreateProcessInstanceCommand) -> str:
    return hashlib.sha256(serialize_create_command(command).encode("utf-8")).hexdigest()


def parse_create_command(
    raw: str,
    *,
    expected_sha256: str | None = None,
) -> CreateProcessInstanceCommand:
    try:
        value = _load_canonical_json(raw, maximum_bytes=_MAX_SNAPSHOT_BYTES)
        if not isinstance(value, dict) or set(value) != {
            "processCode",
            "originatorUserId",
            "deptId",
            "microappAgentId",
            "formComponentValues",
        }:
            raise ValueError
        raw_values = value["formComponentValues"]
        if not isinstance(raw_values, list):
            raise ValueError
        form_values = tuple(_parse_create_form_value(item) for item in raw_values)
        command = CreateProcessInstanceCommand(
            process_code=value["processCode"],
            originator_user_id=value["originatorUserId"],
            department_id=value["deptId"],
            microapp_agent_id=value["microappAgentId"],
            form_values=form_values,
        )
        canonical = serialize_create_command(command)
    except (KeyError, TypeError, ValueError):
        raise _snapshot_error("审批创建请求损坏，请联系管理员") from None
    if canonical != raw:
        raise _snapshot_error("审批创建请求不是规范格式，请联系管理员")
    if expected_sha256 is not None:
        _verify_sha256(canonical, expected_sha256, "审批创建请求校验失败，请联系管理员")
    return command


def _snapshot_identity(
    *,
    actor: DraftActor,
    union_id: str,
    name: str,
    microapp_agent_id: int,
) -> SnapshotIdentity:
    try:
        return SnapshotIdentity(
            corp_id=_required_text(actor.corp_id),
            user_id=_required_text(actor.user_id),
            union_id=_required_text(union_id),
            name=_required_text(name),
            department_id=_required_text(actor.department_id),
            department_name=_required_text(actor.department_name),
            microapp_agent_id=microapp_agent_id,
        )
    except (TypeError, ValueError, ValidationError):
        raise ApiError("UNAUTHORIZED", "登录身份数据无效，请重新进入", 401) from None


def _related_source(item: ReimbursementDraftRelatedApproval) -> RelatedApprovalSource:
    return RelatedApprovalSource(
        sort_order=item.sort_order,
        process_instance_id=item.process_instance_id,
        profile_key=item.travel_profile_key,
        process_code=item.process_code,
        catalog_config_version=item.catalog_config_version,
        schema_fingerprint=item.travel_schema_fingerprint,
        listed_from_ms=item.listed_from_ms,
        listed_to_ms=item.listed_to_ms,
        start_date=item.travel_start_date,
        end_date=item.travel_end_date,
        title=item.title,
        business_id=item.business_id,
        instance_created_at=item.instance_created_at,
        verified_at=item.verified_at,
        originator_department_id=item.originator_department_id,
        source_travel_type_value=item.source_travel_type_value,
    )


def _original_source(
    item: ReimbursementDraftFile,
    *,
    manifest_order: int,
) -> OriginalFileSource:
    if item.size_bytes is None or item.sha256 is None:
        raise _snapshot_error("附件信息不完整，请重新上传")
    return OriginalFileSource(
        draft_file_id=item.id,
        sort_order=manifest_order,
        processing_role=item.processing_role,
        storage_key=item.storage_key,
        file_name=item.original_name,
        file_type=item.extension.removeprefix(".").lower(),
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        ocr_status=item.ocr_status,
        attachment_kind=item.attachment_kind,
    )


def _validate_related_sources(
    raw_ids: str,
    related: tuple[RelatedApprovalSource, ...],
    catalog: OaTemplateCatalogContract,
    draft_input: ReimbursementDraftInput,
) -> None:
    if not related:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_NOT_READY",
            "请先关联至少一张已通过的出差审批单",
            409,
        )
    try:
        stored_ids = json.loads(raw_ids)
    except (TypeError, ValueError):
        raise _snapshot_error("关联审批数据损坏，请重新选择") from None
    ids = [item.process_instance_id for item in related]
    if stored_ids != ids or len(ids) != len(set(ids)):
        raise _snapshot_error("关联审批数据损坏，请重新选择")
    profiles = {item.profile_key: item for item in catalog.travel_profiles}
    travel_type_options: dict[str, FormOption] = {}
    travel_type_identities: set[tuple[str, str]] = set()
    for item in related:
        profile = profiles.get(item.profile_key)
        if (
            profile is None
            or item.process_code != profile.process_code
            or item.catalog_config_version != catalog.config_version
            or item.schema_fingerprint != profile.schema.fingerprint
            or item.end_date < item.start_date
            or item.listed_to_ms < item.listed_from_ms
        ):
            raise _snapshot_error("关联审批或模板已经变化，请重新选择")
        option = _profile_selected_type(profile, item.source_travel_type_value)
        travel_type_options[option.value] = option
        if profile.travel_type_mappings is not None:
            if item.source_travel_type_value is None:
                raise _snapshot_error("关联审批的来源出差类别缺失，请重新选择")
            travel_type_identities.add(("source", item.source_travel_type_value))
        else:
            travel_type_identities.add(("target", option.value))
    if len(travel_type_options) != 1 or len(travel_type_identities) != 1:
        raise _snapshot_error("关联的出差审批类别不一致，请重新选择")
    periods = [(item.start_date, item.end_date) for item in related]
    approval_start = min(start for start, _end in periods)
    approval_end = max(end for _start, end in periods)
    subsidy_trips = draft_input.trips or (
        [draft_input.trip] if draft_input.trip is not None else []
    )
    selected_travel_type = next(iter(travel_type_options.values()))
    expected_purpose = subsidy_purpose_for_travel_type(selected_travel_type.label)
    if subsidy_trips and (
        expected_purpose is None
        or any(item.trip_type is not expected_purpose for item in subsidy_trips)
    ):
        raise _snapshot_error("出差补助类别与所选出差审批类别不一致，请重新确认")
    if draft_input.trips:
        related_by_id = {item.process_instance_id: item for item in related}
        if len(draft_input.trips) != len(related_by_id):
            raise _snapshot_error("每张已关联的出差审批必须对应一个补助项")
        for trip in draft_input.trips:
            related_item = related_by_id.get(trip.related_approval_id or "")
            if (
                related_item is None
                or trip.start_date != related_item.start_date
                or trip.end_date != related_item.end_date
            ):
                raise _snapshot_error("补助项与关联出差审批不一致，请重新确认")
    if subsidy_trips:
        reimbursement_start = min(item.start_date for item in subsidy_trips)
        reimbursement_end = max(item.end_date for item in subsidy_trips)
        if approval_start > reimbursement_start or approval_end < reimbursement_end:
            raise ApiError(
                "REIMBURSEMENT_TRAVEL_DATE_MISMATCH",
                "所选出差审批日期必须完整覆盖出差补助日期",
                409,
            )
    elif draft_input.items:
        reimbursement_start = min(item.date for item in draft_input.items)
        reimbursement_end = max(item.date for item in draft_input.items)
    else:
        return
    if not subsidy_trips and (
        approval_end < reimbursement_start or approval_start > reimbursement_end
    ):
        raise ApiError(
            "REIMBURSEMENT_TRAVEL_DATE_MISMATCH",
            "所选出差审批日期与本次报销费用日期不重叠，请重新选择",
            409,
        )


def _snapshot_template(catalog: OaTemplateCatalogContract) -> SnapshotTemplate:
    from app.services.travel_approvals import travel_source_component_id

    reimbursement = catalog.reimbursement
    components = {item.component_id: item for item in reimbursement.schema.components}
    fields: list[SnapshotTemplateField] = []
    for spec in REIMBURSEMENT_LOGICAL_FIELD_SPECS:
        component_id = reimbursement.mappings.get(spec.key)
        component = components.get(component_id)
        if component is None or component.component_type not in spec.supported_component_types:
            raise _snapshot_error("审批模板字段映射已经变化，请管理员重新确认")
        fields.append(
            SnapshotTemplateField(
                logical_key=spec.key,
                component_id=component.component_id,
                component_type=component.component_type,
                name=component.label,
                biz_alias=component.biz_alias,
            )
        )
    schema_json = _canonical_json(reimbursement.schema.as_dict())
    return SnapshotTemplate(
        config_version=catalog.config_version,
        process_code=reimbursement.process_code,
        template_name=reimbursement.schema.template_name,
        schema_fingerprint=reimbursement.schema.fingerprint,
        schema_canonical_json=schema_json,
        fields=tuple(fields),
        travel_profiles=tuple(
            SnapshotTravelProfile(
                profile_key=item.profile_key,
                display_name=item.display_name,
                process_code=item.process_code,
                schema_fingerprint=item.schema.fingerprint,
                start_date_component_id=item.start_date_component_id,
                end_date_component_id=item.end_date_component_id,
                travel_type_option=_snapshot_option(item.travel_type_option),
                company_component_id=travel_source_component_id(item, "company"),
                budget_code_component_id=travel_source_component_id(item, "budgetCode"),
                travel_type_component_id=item.travel_type_component_id,
                travel_type_mappings=(
                    {
                        key: _snapshot_option(option)
                        for key, option in item.travel_type_mappings.items()
                    }
                    if item.travel_type_mappings is not None
                    else None
                ),
            )
            for item in catalog.travel_profiles
        ),
    )


def _snapshot_trip(trip: TripInput | None) -> SnapshotTrip | None:
    if trip is None:
        return None
    return SnapshotTrip(
        trip_type=trip.trip_type,
        related_approval_id=trip.related_approval_id,
        start_date=trip.start_date,
        start_time=trip.start_time,
        end_date=trip.end_date,
        end_time=trip.end_time,
        policy_confirmed=trip.policy_confirmed,
        confirmed_effective_days=trip.confirmed_effective_days,
        no_subsidy_exception=trip.no_subsidy_exception,
        manual_subsidy_amount=trip.manual_subsidy_amount,
    )


def _snapshot_expense_item(
    item: ReimbursementDraftExpenseItemInput,
) -> SnapshotExpenseItem:
    return SnapshotExpenseItem(
        category=item.category,
        date=item.date,
        display_date=item.display_date,
        description=item.description,
        amount=item.amount,
        receipt_count=item.receipt_count,
        source_file_id=item.source_file_id,
        itinerary_file_ids=tuple(item.itinerary_file_ids),
        requires_itinerary=item.requires_itinerary,
        payment_proof_file_ids=tuple(item.payment_proof_file_ids),
        hotel_bill_file_ids=tuple(item.hotel_bill_file_ids),
        rail_type=item.rail_type,
        transport_type=item.transport_type,
        original_currency=item.original_currency,
        original_amount=item.original_amount,
        cny_amount_confirmed=item.cny_amount_confirmed,
        requires_cny_confirmation=item.requires_cny_confirmation,
    )


def _snapshot_subsidy(value: object) -> SnapshotSubsidy | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid subsidy snapshot")
    return SnapshotSubsidy.model_validate(value)


def _snapshot_totals(value: dict[str, object]) -> SnapshotTotals:
    return SnapshotTotals.model_validate(
        {key: item for key, item in value.items() if key not in {"subsidy", "subsidies"}}
    )


def _snapshot_related(item: RelatedApprovalSource) -> SnapshotRelatedApproval:
    return SnapshotRelatedApproval(
        sort_order=item.sort_order,
        process_instance_id=item.process_instance_id,
        profile_key=item.profile_key,
        process_code=item.process_code,
        catalog_config_version=item.catalog_config_version,
        schema_fingerprint=item.schema_fingerprint,
        listed_from_ms=item.listed_from_ms,
        listed_to_ms=item.listed_to_ms,
        start_date=item.start_date,
        end_date=item.end_date,
        title=item.title,
        business_id=item.business_id,
        instance_created_at=_utc_timestamp(item.instance_created_at),
        verified_at=_utc_timestamp(item.verified_at),
        originator_department_id=item.originator_department_id,
        source_travel_type_value=item.source_travel_type_value,
    )


def _snapshot_original(item: OriginalFileSource) -> SnapshotOriginalFile:
    return SnapshotOriginalFile(
        draft_file_id=item.draft_file_id,
        sort_order=item.sort_order,
        processing_role=item.processing_role,
        storage_key=item.storage_key,
        file_name=item.file_name,
        file_type=item.file_type,
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        ocr_status=item.ocr_status,
        attachment_kind=item.attachment_kind,
    )


def _snapshot_option(option: FormOption) -> SnapshotOption:
    return SnapshotOption(value=option.value, label=option.label, key=option.key)


def _mapped_option(
    catalog: OaTemplateCatalogContract,
    logical_key: str,
    selected_value: str,
) -> FormOption:
    component_id = catalog.reimbursement.mappings.get(logical_key)
    component = next(
        (
            item
            for item in catalog.reimbursement.schema.components
            if item.component_id == component_id
        ),
        None,
    )
    option = (
        next((item for item in component.options if item.value == selected_value), None)
        if component is not None
        else None
    )
    if option is None:
        raise _snapshot_error("所选审批模板选项已失效，请刷新后重新选择")
    return option


def _profile_selected_type(profile, source_value: str | None):
    if profile.travel_type_mappings is None:
        if source_value is not None or profile.travel_type_component_id is not None:
            raise ValueError("unexpected source travel category")
        return profile.travel_type_option
    if not profile.travel_type_component_id or source_value not in profile.travel_type_mappings:
        raise ValueError("source travel category mapping missing")
    return profile.travel_type_mappings[source_value]


def _travel_type_option(
    catalog: OaTemplateCatalogContract,
    related: tuple[RelatedApprovalSource, ...],
) -> FormOption:
    profiles = {item.profile_key: item for item in catalog.travel_profiles}
    options = {
        (
            _profile_selected_type(profiles[item.profile_key], item.source_travel_type_value).value,
            _profile_selected_type(profiles[item.profile_key], item.source_travel_type_value).label,
            _profile_selected_type(profiles[item.profile_key], item.source_travel_type_value).key,
        )
        for item in related
        if item.profile_key in profiles
    }
    if len(options) != 1 or len(related) == 0:
        raise _snapshot_error("关联的出差审批类别不一致，请重新选择")
    value, label, key = options.pop()
    return FormOption(value=value, label=label, key=key)


def _snapshot_form_values(
    *,
    company: FormOption,
    budget: FormOption,
    travel_type: FormOption,
    start_date: date,
    end_date: date,
    duration_days: int,
    items: tuple[SnapshotExpenseItem, ...],
    subsidy: SnapshotSubsidy | None,
    subsidies: tuple[SnapshotSubsidy, ...] = (),
    totals: SnapshotTotals,
    related: tuple[SnapshotRelatedApproval, ...],
) -> tuple[SnapshotFormValue, ...]:
    related_value = _canonical_json([item.process_instance_id for item in related])
    values = (
        company.value,
        budget.value,
        travel_type.value,
        start_date.isoformat(),
        end_date.isoformat(),
        str(duration_days),
        _description_v1(items, subsidy, totals, subsidies),
        money_string(totals.total_amount),
        related_value,
    )
    return tuple(
        SnapshotFormValue(logical_key=key, value=value)
        for key, value in zip(_OA_VALUE_KEYS, values, strict=True)
    )


def _description_v1(
    items: tuple[SnapshotExpenseItem, ...],
    subsidy: SnapshotSubsidy | None,
    totals: SnapshotTotals,
    subsidies: tuple[SnapshotSubsidy, ...] = (),
) -> str:
    lines = [
        f"{item.date.isoformat()} {CATEGORY_BY_ID[item.category].name} "
        f"{item.description}：{money_string(item.amount)}"
        for item in items
    ]
    calculated = subsidies or ((subsidy,) if subsidy is not None else ())
    for item in calculated:
        lines.append(
            f"出差补助：{format(item.effective_days, '.1f')}天 × "
            f"{money_string(item.daily_rate)} = {money_string(item.total)}"
        )
    lines.append(f"合计：{money_string(totals.total_amount)}")
    return "\n".join(lines)


def _validate_snapshot_semantics(snapshot: ReimbursementSnapshot) -> None:
    if (
        not _POSITIVE_DECIMAL_IDENTIFIER.fullmatch(snapshot.identity.department_id)
        or int(snapshot.identity.department_id) > _SIGNED_INT64_MAX
    ):
        raise ValueError("department id must be a positive signed int64")
    if snapshot.identity.microapp_agent_id > _SIGNED_INT64_MAX:
        raise ValueError("microapp agent id must be a positive signed int64")
    if tuple(item.logical_key for item in snapshot.template.fields) != _OA_LOGICAL_KEYS:
        raise ValueError("template fields are incomplete or out of order")
    if tuple(item.logical_key for item in snapshot.form_values) != _OA_VALUE_KEYS:
        raise ValueError("form values are incomplete or out of order")
    range_bindings = [
        item for item in snapshot.template.fields if item.component_type == "DDDateRangeField"
    ]
    if range_bindings and (
        {item.logical_key for item in range_bindings} != {"startDate", "endDate", "durationDays"}
        or len({item.component_id for item in range_bindings}) != 1
    ):
        raise ValueError("date range requires all three date bindings")
    expected_ids = len(_OA_LOGICAL_KEYS) - (2 if range_bindings else 0)
    if len({item.component_id for item in snapshot.template.fields}) != expected_ids:
        raise ValueError("template component ids must be unique")
    if not snapshot.related_approvals or not snapshot.original_files:
        raise ValueError("related approvals and original files are required")
    if tuple(item.sort_order for item in snapshot.related_approvals) != tuple(
        sorted(item.sort_order for item in snapshot.related_approvals)
    ):
        raise ValueError("related approvals must be ordered")
    if tuple(item.sort_order for item in snapshot.original_files) != tuple(
        sorted(item.sort_order for item in snapshot.original_files)
    ):
        raise ValueError("original files must be ordered")
    if len({item.sort_order for item in snapshot.related_approvals}) != len(
        snapshot.related_approvals
    ):
        raise ValueError("related approval sort orders must be unique")
    if len({item.sort_order for item in snapshot.original_files}) != len(snapshot.original_files):
        raise ValueError("original file sort orders must be unique")
    if len({item.process_instance_id for item in snapshot.related_approvals}) != len(
        snapshot.related_approvals
    ):
        raise ValueError("related approval ids must be unique")
    related_department_ids = [
        item.originator_department_id for item in snapshot.related_approvals
    ]
    if any(related_department_ids) and (
        any(item is None for item in related_department_ids)
        or len(set(related_department_ids)) != 1
    ):
        raise ValueError("related approval departments must be complete and identical")
    if len({item.draft_file_id for item in snapshot.original_files}) != len(
        snapshot.original_files
    ):
        raise ValueError("original file ids must be unique")
    if len({item.profile_key for item in snapshot.template.travel_profiles}) != len(
        snapshot.template.travel_profiles
    ) or len({item.process_code for item in snapshot.template.travel_profiles}) != len(
        snapshot.template.travel_profiles
    ):
        raise ValueError("travel profile identities must be unique")
    _validate_template_snapshot(snapshot)
    _validate_business_snapshot(snapshot)


def _validate_template_snapshot(snapshot: ReimbursementSnapshot) -> None:
    raw_schema = _load_canonical_json(
        snapshot.template.schema_canonical_json,
        maximum_bytes=2 * 1024 * 1024,
    )
    if not isinstance(raw_schema, dict):
        raise ValueError("template schema must be an object")
    if _canonical_json(raw_schema) != snapshot.template.schema_canonical_json:
        raise ValueError("template schema must use canonical JSON")
    schema = form_schema_from_dict(raw_schema)
    if (
        schema.process_code != snapshot.template.process_code
        or schema.template_name != snapshot.template.template_name
        or schema.fingerprint != snapshot.template.schema_fingerprint
        or raw_schema.get("schemaFingerprint") != schema.fingerprint
    ):
        raise ValueError("template schema identity mismatch")
    components = {item.component_id: item for item in schema.components}
    specs = {item.key: item for item in REIMBURSEMENT_LOGICAL_FIELD_SPECS}
    for binding in snapshot.template.fields:
        component = components.get(binding.component_id)
        if (
            component is None
            or component.component_type != binding.component_type
            or component.label != binding.name
            or component.biz_alias != binding.biz_alias
            or component.component_type not in specs[binding.logical_key].supported_component_types
            or component.disabled
            or component.hidden
            or component.ancestor_disabled
            or component.ancestor_hidden
            or component.in_subtable
            or component.unsupported_container_ancestor
            or (
                binding.logical_key in {"startDate", "endDate"}
                and component.value_format != "yyyy-MM-dd"
            )
        ):
            raise ValueError("template field binding mismatch")
    selection_by_key = {
        "company": snapshot.selections.company,
        "budgetCode": snapshot.selections.budget_code,
        "travelType": snapshot.selections.travel_type,
    }
    bindings = {item.logical_key: item for item in snapshot.template.fields}
    for logical_key, selection in selection_by_key.items():
        component = components[bindings[logical_key].component_id]
        if not any(
            option.value == selection.value
            and option.label == selection.label
            and option.key == selection.key
            for option in component.options
        ):
            raise ValueError("selected option is absent from the frozen schema")


def _validate_business_snapshot(snapshot: ReimbursementSnapshot) -> None:
    request = draft_input_from_snapshot(snapshot)
    source_file_id_values = [
        item.source_file_id for item in snapshot.input.items if item.source_file_id is not None
    ]
    dismissed_file_id_values = list(snapshot.input.dismissed_ocr_file_ids)
    if len(source_file_id_values) != len(set(source_file_id_values)):
        raise ValueError("OCR source file ids must be unique")
    if len(dismissed_file_id_values) != len(set(dismissed_file_id_values)):
        raise ValueError("OCR dismissed file ids must be unique")
    source_file_ids = set(source_file_id_values)
    dismissed_file_ids = set(dismissed_file_id_values)
    if source_file_ids.intersection(dismissed_file_ids):
        raise ValueError("OCR disposition cannot be both linked and dismissed")
    terminal_expense_file_ids = {
        item.draft_file_id
        for item in snapshot.original_files
        if item.processing_role == ReimbursementDraftFileRole.EXPENSE_SOURCE.value
        and item.ocr_status
        in {
            ReimbursementOcrStatus.COMPLETE.value,
            ReimbursementOcrStatus.FAILED.value,
        }
    }
    if any(
        item.processing_role == ReimbursementDraftFileRole.EXPENSE_SOURCE.value
        and item.ocr_status == ReimbursementOcrStatus.NOT_REQUESTED.value
        for item in snapshot.original_files
    ):
        raise ValueError("expense source OCR must be complete before submission")
    if terminal_expense_file_ids != source_file_ids | dismissed_file_ids:
        raise ValueError("terminal OCR files require an exact disposition")
    support_ids = {
        item.draft_file_id
        for item in snapshot.original_files
        if item.processing_role == ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
    }
    by_id = {file.draft_file_id: file for file in snapshot.original_files}
    for item in snapshot.input.items:
        if len(item.itinerary_file_ids) != len(set(item.itinerary_file_ids)) or not set(
            item.itinerary_file_ids
        ).issubset(support_ids):
            raise ValueError("itinerary must reference active support files")
        if (
            item.requires_itinerary or item.transport_type == "ride_hailing"
        ) and not item.itinerary_file_ids:
            raise ValueError("ride-hailing requires itinerary")
        if (
            item.requires_cny_confirmation
            or (item.original_currency and item.original_currency != "CNY")
        ) and not item.cny_amount_confirmed:
            raise ValueError("foreign receipts require a confirmed CNY amount")
        if item.source_file_id is not None and item.receipt_count != 1:
            raise ValueError("one source receipt must have receipt count one")
        for proof_ids, kind in (
            (item.itinerary_file_ids, "itinerary"),
            (item.payment_proof_file_ids, "payment_proof"),
            (item.hotel_bill_file_ids, "hotel_bill"),
        ):
            if len(proof_ids) != len(set(proof_ids)) or any(
                file_id not in support_ids or by_id[file_id].attachment_kind != kind
                for file_id in proof_ids
            ):
                raise ValueError("proof must reference support files of the correct kind")
        if (
            payment_proof_required(
                amount=item.amount,
                category=item.category,
                rail_type=item.rail_type,
            )
            and not item.payment_proof_file_ids
        ):
            raise ValueError("payment proof is required for this receipt")
        if item.category is ExpenseCategory.LODGING and not item.hotel_bill_file_ids:
            raise ValueError("lodging requires hotel stay details")
    if (
        snapshot.input.project.display_text != snapshot.selections.budget_code.label
        or snapshot.input.project.manual_text != snapshot.selections.budget_code.label
    ):
        raise ValueError("workbook project must match the budget label")
    subsidy = _subsidy_from_snapshot(snapshot.subsidy)
    subsidies = tuple(
        item
        for item in (_subsidy_from_snapshot(value) for value in snapshot.subsidies)
        if item is not None
    )
    calculated_subsidies = subsidies or ((subsidy,) if subsidy is not None else ())
    totals = _totals_from_snapshot(snapshot.totals)
    expected_totals = calculate_expense_totals(
        complete_expense_items(request),
        calculated_subsidies,
    )
    if (
        money_string(expected_totals.expense_total) != money_string(totals.expense_total)
        or money_string(expected_totals.subsidy_total) != money_string(totals.subsidy_total)
        or money_string(expected_totals.total_amount) != money_string(totals.total_amount)
        or expected_totals.receipt_count != totals.receipt_count
        or expected_totals.uppercase_amount != totals.uppercase_amount
    ):
        raise ValueError("snapshot totals mismatch")
    subsidy_trips = request.trips or ([request.trip] if request.trip is not None else [])
    output_trips = merge_overlapping_subsidy_trips(subsidy_trips)
    if len(calculated_subsidies) != len(output_trips):
        raise ValueError("trip input requires one subsidy calculation per merged period")
    for trip_item, subsidy_item in zip(output_trips, calculated_subsidies, strict=True):
        if (
            subsidy_item.trip_type != trip_item.subsidy_trip_type()
            or subsidy_item.calendar_days != (trip_item.end_date - trip_item.start_date).days + 1
            or subsidy_item.related_approval_id != trip_item.related_approval_id
            or money_string(subsidy_item.effective_days * subsidy_item.daily_rate)
            != money_string(subsidy_item.total)
        ):
            raise ValueError("snapshot subsidy mismatch")
    start_date = min(item.start_date for item in snapshot.related_approvals)
    end_date = max(item.end_date for item in snapshot.related_approvals)
    if snapshot.travel_period != SnapshotTravelPeriod(
        start_date=start_date,
        end_date=end_date,
        duration_days=(end_date - start_date).days + 1,
    ):
        raise ValueError("travel period mismatch")
    if subsidy_trips:
        reimbursement_start = min(item.start_date for item in subsidy_trips)
        reimbursement_end = max(item.end_date for item in subsidy_trips)
    elif request.items:
        reimbursement_start = min(item.date for item in request.items)
        reimbursement_end = max(item.date for item in request.items)
    else:
        reimbursement_start = start_date
        reimbursement_end = end_date
    if end_date < reimbursement_start or start_date > reimbursement_end:
        raise ValueError("related approval date mismatch")
    profiles = {item.profile_key: item for item in snapshot.template.travel_profiles}
    options = []
    for related in snapshot.related_approvals:
        profile = profiles.get(related.profile_key)
        if (
            profile is None
            or profile.process_code != related.process_code
            or profile.schema_fingerprint != related.schema_fingerprint
            or related.catalog_config_version != snapshot.template.config_version
            or related.end_date < related.start_date
            or related.listed_to_ms < related.listed_from_ms
        ):
            raise ValueError("related approval source mismatch")
        options.append(_profile_selected_type(profile, related.source_travel_type_value))
    if any(item != snapshot.selections.travel_type for item in options):
        raise ValueError("travel type option mismatch")
    expected_values = _snapshot_form_values(
        company=FormOption(**snapshot.selections.company.model_dump()),
        budget=FormOption(**snapshot.selections.budget_code.model_dump()),
        travel_type=FormOption(**snapshot.selections.travel_type.model_dump()),
        start_date=start_date,
        end_date=end_date,
        duration_days=snapshot.travel_period.duration_days,
        items=snapshot.input.items,
        subsidy=snapshot.subsidy,
        subsidies=snapshot.subsidies,
        totals=snapshot.totals,
        related=snapshot.related_approvals,
    )
    if snapshot.form_values != expected_values:
        raise ValueError("stored form values mismatch")
    if snapshot.input.company_value != snapshot.selections.company.value:
        raise ValueError("company selection mismatch")
    if snapshot.input.budget_code_value != snapshot.selections.budget_code.value:
        raise ValueError("budget selection mismatch")
    expected_filename = build_download_filename(
        snapshot.identity.name,
        snapshot.input.project.filename_component,
    )
    if snapshot.excel.file_name != expected_filename:
        raise ValueError("generated Excel name mismatch")


def draft_input_from_snapshot(snapshot: ReimbursementSnapshot) -> ReimbursementDraftInput:
    project = snapshot.input.project
    project_value: dict[str, object]
    if project.mode == "selected":
        project_value = {"mode": "selected", "id": project.selected_id}
    else:
        project_value = {"mode": "manual", "text": project.manual_text}
    trip = snapshot.input.trip
    trip_value = (
        {
            **trip.model_dump(mode="json", by_alias=True),
            "startTime": trip.start_time.strftime("%H:%M"),
            "endTime": trip.end_time.strftime("%H:%M"),
        }
        if trip is not None
        else None
    )
    trips_value = [
        {
            **item.model_dump(mode="json", by_alias=True),
            "startTime": item.start_time.strftime("%H:%M"),
            "endTime": item.end_time.strftime("%H:%M"),
        }
        for item in snapshot.input.trips
    ]
    return ReimbursementDraftInput.model_validate(
        {
            "ocrDispositionVersion": snapshot.input.ocr_disposition_version,
            "companyValue": snapshot.input.company_value,
            "budgetCodeValue": snapshot.input.budget_code_value,
            "project": project_value,
            "trip": trip_value,
            "trips": trips_value,
            "items": [
                item.model_dump(mode="json", by_alias=True, exclude_none=True)
                for item in snapshot.input.items
            ],
            "dismissedOcrFileIds": list(snapshot.input.dismissed_ocr_file_ids),
        }
    )


def _subsidy_from_snapshot(value: SnapshotSubsidy | None) -> SubsidyCalculation | None:
    if value is None:
        return None
    return SubsidyCalculation(
        trip_type=value.trip_type,
        calendar_days=value.calendar_days,
        effective_days=value.effective_days,
        daily_rate=value.daily_rate,
        total=value.total,
        related_approval_id=value.related_approval_id,
    )


def _totals_from_snapshot(value: SnapshotTotals) -> ExpenseTotals:
    return ExpenseTotals(
        expense_total=value.expense_total,
        subsidy_total=value.subsidy_total,
        total_amount=value.total_amount,
        receipt_count=value.receipt_count,
        uppercase_amount=value.uppercase_amount,
    )


def _validate_attachment_manifest(
    snapshot: ReimbursementSnapshot,
    attachments: Sequence[ApprovalAttachment],
) -> tuple[ApprovalAttachment, ...]:
    if not isinstance(attachments, Sequence) or isinstance(attachments, (str, bytes)):
        raise _snapshot_error("审批附件清单无效，请稍后重试")
    values = tuple(attachments)
    if len(values) != 2 or any(not isinstance(item, ApprovalAttachment) for item in values):
        raise _snapshot_error("审批附件数量与提交快照不一致，请稍后重试")
    spaces = {item.space_id for item in values}
    identities = {(item.space_id, item.file_id) for item in values}
    if len(spaces) != 1 or len(identities) != len(values):
        raise _snapshot_error("审批附件标识不一致，请稍后重试")
    if values[0].file_type.lower().removeprefix(".") != "pdf" or values[0].file_size <= 0:
        raise _snapshot_error("票据汇总 PDF 必须位于附件清单首位")
    generated = values[-1]
    # DingTalk may safely de-duplicate the committed name (for example by
    # adding "(1)"). Role/order and type prove this is the generated workbook;
    # the OA value must retain the authoritative name returned by commit.
    if generated.file_type.lower().removeprefix(".") != "xlsx" or generated.file_size <= 0:
        raise _snapshot_error("生成的报销 Excel 必须位于附件清单最后")
    return values


def _parse_create_form_value(value: object) -> CreateWorkflowFormValue:
    if not isinstance(value, dict):
        raise ValueError("invalid form value")
    allowed = {"name", "value", "id", "componentType", "bizAlias"}
    if not {"name", "value"} <= set(value) or not set(value) <= allowed:
        raise ValueError("invalid form value keys")
    return CreateWorkflowFormValue(
        name=value["name"],
        value=value["value"],
        component_id=value.get("id"),
        component_type=value.get("componentType"),
        biz_alias=value.get("bizAlias"),
    )


def _load_canonical_json(raw: str, *, maximum_bytes: int) -> object:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > maximum_bytes:
        raise ValueError("invalid canonical JSON size")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid number")),
        )
    except (TypeError, ValueError):
        raise ValueError("invalid canonical JSON") from None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _verify_sha256(raw: str, expected: str, message: str) -> None:
    if not isinstance(expected, str) or not _SHA256_PATTERN.fullmatch(expected):
        raise _snapshot_error(message)
    observed = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(observed, expected):
        raise _snapshot_error(message)


def _require_snapshot(snapshot: ReimbursementSnapshot) -> ReimbursementSnapshot:
    if not isinstance(snapshot, ReimbursementSnapshot):
        raise TypeError("snapshot must be ReimbursementSnapshot")
    # model_copy(update=...) is intentionally trusted by Pydantic and skips
    # validators. Rebuild the complete tree from primitive values before every
    # downstream use so field constraints and semantic validators both run.
    return ReimbursementSnapshot.model_validate(
        snapshot.model_dump(mode="python", by_alias=True, warnings=False)
    )


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("required text is invalid")
    return value


def _require_positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be a datetime")
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _snapshot_error(message: str) -> ApiError:
    return ApiError("REIMBURSEMENT_SNAPSHOT_INVALID", message, 409)
