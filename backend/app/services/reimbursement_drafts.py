from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.categories import ExpenseCategory
from app.domain.expenses import calculate_expense_totals
from app.domain.material_classification import material_classification
from app.domain.reimbursement_proofs import payment_proof_required
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient, FormOption
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
from app.schemas.expenses import TripInput
from app.schemas.reimbursements import (
    CURRENT_OCR_DISPOSITION_VERSION,
    BudgetProjectInput,
    ReimbursementDraftExpenseItemInput,
    ReimbursementDraftInput,
    RelatedApprovalSelectionInput,
)
from app.services.oa_template_profiles import (
    OaTemplateCatalogContract,
    require_submission_ready_catalog,
)
from app.services.sessions import CurrentSession, require_selected_department
from app.services.subsidy_calculation import (
    calculate_trip_subsidies,
    request_trips,
    subsidy_purpose_for_travel_type,
)
from app.services.travel_approvals import (
    TravelApprovalQueryWindow,
    TravelApprovalSelection,
    VerifiedTravelSelection,
    reverify_travel_approval_selection,
)

_MUTABLE_STATUSES = (
    ReimbursementDraftStatus.DRAFT.value,
    ReimbursementDraftStatus.REVIEW_READY.value,
)


@dataclass(frozen=True, slots=True)
class DraftActor:
    corp_id: str
    user_id: str
    department_id: str
    department_name: str


@dataclass(frozen=True, slots=True)
class DraftCalculation:
    canonical_json: str
    input_data: dict[str, object]
    totals_data: dict[str, object]


@dataclass(frozen=True, slots=True)
class CatalogBinding:
    process_code: str
    config_version: int
    schema_fingerprint: str

    @classmethod
    def from_catalog(cls, catalog: OaTemplateCatalogContract) -> CatalogBinding:
        return cls(
            process_code=catalog.reimbursement.process_code,
            config_version=catalog.config_version,
            schema_fingerprint=catalog.reimbursement.schema.fingerprint,
        )


def draft_actor(current: CurrentSession) -> DraftActor:
    department_name = require_selected_department(current)
    department_id = str(current.record.current_department_id)
    if not any(
        department.id == department_id and department.name == department_name
        for department in current.departments
    ):
        raise ApiError(
            "INVALID_DEPARTMENT_CONTEXT",
            "当前报销部门不在登录员工的所属部门中，请重新选择",
            409,
        )
    corp_id = str(current.record.corp_id or "").strip()
    user_id = str(current.record.dingtalk_user_id or "").strip()
    if not corp_id or not user_id:
        raise ApiError("UNAUTHORIZED", "登录身份数据无效，请重新进入", 401)
    return DraftActor(
        corp_id=corp_id,
        user_id=user_id,
        department_id=department_id,
        department_name=department_name,
    )


def require_owned_draft(
    database: Session,
    *,
    draft_id: str,
    actor: DraftActor,
    mutable: bool = False,
    now: datetime | None = None,
) -> ReimbursementDraft:
    draft = database.scalar(
        select(ReimbursementDraft).where(
            ReimbursementDraft.id == draft_id,
            ReimbursementDraft.corp_id == actor.corp_id,
            ReimbursementDraft.owner_user_id == actor.user_id,
        )
    )
    if draft is None:
        raise _not_found_error()
    _require_department(draft, actor)
    if mutable:
        _require_mutable(draft, now=now or utc_now())
    return draft


def bump_owned_draft_revision(
    database: Session,
    *,
    draft_id: str,
    actor: DraftActor,
    expected_revision: int,
    now: datetime | None = None,
) -> int:
    """Atomically claim one mutation revision without committing the caller's transaction."""

    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("expected_revision must be a positive integer")
    changed_at = now or utc_now()
    result = database.execute(
        update(ReimbursementDraft)
        .where(
            ReimbursementDraft.id == draft_id,
            ReimbursementDraft.corp_id == actor.corp_id,
            ReimbursementDraft.owner_user_id == actor.user_id,
            ReimbursementDraft.department_id == actor.department_id,
            ReimbursementDraft.department_name == actor.department_name,
            ReimbursementDraft.revision == expected_revision,
            ReimbursementDraft.status.in_(_MUTABLE_STATUSES),
            ReimbursementDraft.locked_at.is_(None),
            ReimbursementDraft.expires_at > changed_at,
        )
        .values(
            revision=expected_revision + 1,
            status=ReimbursementDraftStatus.DRAFT.value,
            updated_at=changed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        return expected_revision + 1

    database.expire_all()
    current = require_owned_draft(database, draft_id=draft_id, actor=actor)
    _require_mutable(current, now=changed_at)
    if current.revision != expected_revision:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
            "报销内容已在其他页面更新，请刷新后重试",
            409,
        )
    raise ApiError(
        "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
        "报销状态已变化，请刷新后重试",
        409,
    )


def create_reimbursement_draft(
    database: Session,
    *,
    actor: DraftActor,
    draft_input: ReimbursementDraftInput,
    ttl_days: int,
    max_items: int,
    now: datetime | None = None,
) -> dict[str, object]:
    created_at = now or utc_now()
    if _draft_file_reference_ids(draft_input) or any(
        item.itinerary_file_ids or item.payment_proof_file_ids or item.hotel_bill_file_ids
        for item in draft_input.items
    ):
        raise _invalid_file_reference_error()
    draft_input = draft_input.model_copy(
        update={
            "ocr_disposition_version": CURRENT_OCR_DISPOSITION_VERSION,
            "company_value": "",
            "accounting_source_verified": False,
            "budget_code_value": "",
            "project": None,
        }
    )
    catalog = require_submission_ready_catalog(database)
    calculation = validate_and_calculate_input(
        database,
        catalog=catalog,
        draft_input=draft_input,
        max_items=max_items,
        allow_partial=True,
    )
    binding = CatalogBinding.from_catalog(catalog)
    draft = ReimbursementDraft(
        corp_id=actor.corp_id,
        owner_user_id=actor.user_id,
        status=ReimbursementDraftStatus.DRAFT.value,
        revision=1,
        department_id=actor.department_id,
        department_name=actor.department_name,
        template_process_code=binding.process_code,
        template_config_version=binding.config_version,
        schema_fingerprint=binding.schema_fingerprint,
        input_json=calculation.canonical_json,
        related_instance_ids_json="[]",
        expires_at=created_at + timedelta(days=ttl_days),
        created_at=created_at,
        updated_at=created_at,
    )
    try:
        database.add(draft)
        database.commit()
        database.refresh(draft)
    except Exception:
        database.rollback()
        raise
    return draft_data(database, draft, calculation=calculation, now=created_at)


def list_reimbursement_drafts(
    database: Session,
    *,
    actor: DraftActor,
    offset: int,
    limit: int,
    now: datetime | None = None,
) -> dict[str, object]:
    statement = select(ReimbursementDraft).where(
        ReimbursementDraft.corp_id == actor.corp_id,
        ReimbursementDraft.owner_user_id == actor.user_id,
        ReimbursementDraft.department_id == actor.department_id,
        ReimbursementDraft.department_name == actor.department_name,
    )
    total = database.scalar(select(func.count()).select_from(statement.subquery())) or 0
    drafts = database.scalars(
        statement.order_by(
            ReimbursementDraft.updated_at.desc(),
            ReimbursementDraft.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    observed_at = now or utc_now()
    return {
        "items": [_draft_summary(draft, now=observed_at) for draft in drafts],
        "offset": offset,
        "limit": limit,
        "total": total,
    }


def get_reimbursement_draft(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor)
    return draft_data(database, draft, now=now)


def update_reimbursement_draft(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    draft_input: ReimbursementDraftInput,
    max_items: int,
    now: datetime | None = None,
) -> dict[str, object]:
    changed_at = now or utc_now()
    draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
        mutable=True,
        now=changed_at,
    )
    catalog = require_submission_ready_catalog(database)
    _require_catalog_binding(draft, CatalogBinding.from_catalog(catalog))
    # Accounting values belong to the verified approval selection. An ordinary
    # autosave can carry a stale client snapshot, but cannot replace these values.
    stored_input = _stored_input(draft)
    has_related = bool(json.loads(draft.related_instance_ids_json))
    draft_input = draft_input.model_copy(
        update={
            "company_value": stored_input.company_value if has_related else "",
            "accounting_source_verified": stored_input.accounting_source_verified
            if has_related
            else False,
            "budget_code_value": stored_input.budget_code_value if has_related else "",
            "project": stored_input.project if has_related else None,
        }
    )
    draft_input = apply_ocr_evidence(database, draft_id=draft.id, draft_input=draft_input)
    calculation = validate_and_calculate_input(
        database,
        catalog=catalog,
        draft_input=draft_input,
        max_items=max_items,
        allow_partial=True,
    )
    validate_draft_file_references(
        database,
        draft_id=draft.id,
        draft_input=draft_input,
        require_terminal_disposition=False,
    )
    try:
        new_revision = bump_owned_draft_revision(
            database,
            draft_id=draft_id,
            actor=actor,
            expected_revision=expected_revision,
            now=changed_at,
        )
        database.execute(
            update(ReimbursementDraft)
            .where(
                ReimbursementDraft.id == draft_id,
                ReimbursementDraft.revision == new_revision,
            )
            .values(input_json=calculation.canonical_json)
            .execution_options(synchronize_session=False)
        )
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    updated = require_owned_draft(database, draft_id=draft_id, actor=actor)
    return draft_data(database, updated, calculation=calculation, now=changed_at)


def mark_reimbursement_draft_review_ready(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    max_items: int,
    now: datetime | None = None,
) -> dict[str, object]:
    """Validate a complete local snapshot and atomically mark it review-ready."""

    changed_at = now or utc_now()
    draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
        mutable=True,
        now=changed_at,
    )
    if draft.revision != expected_revision:
        raise _revision_conflict_error()
    catalog = require_submission_ready_catalog(database)
    binding = CatalogBinding.from_catalog(catalog)
    _require_catalog_binding(draft, binding)
    if not json.loads(draft.related_instance_ids_json):
        raise _not_ready_error("请先关联至少一张已通过的出差审批单")
    draft_input = _stored_input(draft)
    if not draft_input.accounting_source_verified:
        raise _not_ready_error("请重新确认关联出差审批，以核验所属公司和预算代码")
    draft_input = apply_ocr_evidence(database, draft_id=draft.id, draft_input=draft_input)
    calculation = validate_and_calculate_input(
        database,
        catalog=catalog,
        draft_input=draft_input,
        max_items=max_items,
    )
    related = database.scalars(
        select(ReimbursementDraftRelatedApproval)
        .where(ReimbursementDraftRelatedApproval.draft_id == draft.id)
        .order_by(ReimbursementDraftRelatedApproval.sort_order)
    ).all()
    _validate_related_snapshot(draft, related, catalog, draft_input=draft_input)
    _validate_file_snapshot(database, draft_id=draft.id, draft_input=draft_input)

    try:
        result = database.execute(
            update(ReimbursementDraft)
            .where(
                ReimbursementDraft.id == draft_id,
                ReimbursementDraft.corp_id == actor.corp_id,
                ReimbursementDraft.owner_user_id == actor.user_id,
                ReimbursementDraft.department_id == actor.department_id,
                ReimbursementDraft.department_name == actor.department_name,
                ReimbursementDraft.revision == expected_revision,
                ReimbursementDraft.status.in_(_MUTABLE_STATUSES),
                ReimbursementDraft.locked_at.is_(None),
                ReimbursementDraft.expires_at > changed_at,
            )
            .values(
                revision=expected_revision + 1,
                status=ReimbursementDraftStatus.REVIEW_READY.value,
                input_json=calculation.canonical_json,
                updated_at=changed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            database.rollback()
            require_owned_draft(
                database,
                draft_id=draft_id,
                actor=actor,
                mutable=True,
                now=changed_at,
            )
            raise _revision_conflict_error()
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    updated = require_owned_draft(database, draft_id=draft_id, actor=actor)
    return draft_data(database, updated, calculation=calculation, now=changed_at)


async def replace_related_approvals(
    database: Session,
    workflow: DingTalkWorkflowClient,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    selections: list[RelatedApprovalSelectionInput],
    now: datetime | None = None,
) -> dict[str, object]:
    preflight_at = now or utc_now()
    draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
        mutable=True,
        now=preflight_at,
    )
    if draft.revision != expected_revision:
        raise _revision_conflict_error()
    catalog = require_submission_ready_catalog(database)
    binding = CatalogBinding.from_catalog(catalog)
    _require_catalog_binding(draft, binding)
    domain_selections = tuple(
        TravelApprovalSelection(
            profile_key=item.profile_key,
            process_instance_id=item.process_instance_id,
            query_window=TravelApprovalQueryWindow.from_dates(
                item.query_window.from_date,
                item.query_window.to_date,
            ),
        )
        for item in selections
    )

    # CurrentSession, draft ownership and catalog are immutable snapshots now.
    # Release the shared auth/catalog read transaction before remote pagination.
    database.rollback()
    verified = (
        await reverify_travel_approval_selection(
            workflow,
            catalog,
            current_user_id=actor.user_id,
            selections=domain_selections,
            expected_department_id=actor.department_id,
        )
        if domain_selections
        else None
    )
    return _store_related_approvals(
        database,
        actor=actor,
        draft_id=draft_id,
        expected_revision=expected_revision,
        expected_binding=binding,
        verified=verified,
        changed_at=now or utc_now(),
    )


def validate_and_calculate_input(
    database: Session,
    *,
    catalog: OaTemplateCatalogContract,
    draft_input: ReimbursementDraftInput,
    max_items: int,
    allow_partial: bool = False,
) -> DraftCalculation:
    if len(draft_input.items) > max_items:
        raise ApiError(
            "TOO_MANY_EXPENSE_LINES",
            f"当前部署每张报销单最多处理 {max_items} 条费用明细",
            422,
        )
    if draft_input.company_value or not allow_partial:
        _require_exact_option(catalog, "company", draft_input.company_value)
    budget = None
    if draft_input.budget_code_value or not allow_partial:
        budget = _require_exact_option(catalog, "budgetCode", draft_input.budget_code_value)
    # The selected OA budget option is authoritative for the generated workbook.
    draft_input = draft_input.model_copy(
        update={
            "project": BudgetProjectInput(mode="manual", text=budget.label) if budget else None,
        }
    )
    if not allow_partial:
        require_complete_draft_input(draft_input)
    return _calculate_input(database, draft_input, allow_partial=allow_partial)


def complete_expense_items(draft_input: ReimbursementDraftInput) -> list[ExcelExpenseItemInput]:
    """Only validated workbook fields may reach arithmetic or generation."""
    return [
        ExcelExpenseItemInput.model_validate(
            item.model_dump(
                mode="json",
                by_alias=True,
                include=set(ExcelExpenseItemInput.model_fields),
            )
        )
        for item in draft_input.items
    ]


def require_complete_draft_input(draft_input: ReimbursementDraftInput) -> None:
    try:
        complete_expense_items(draft_input)
    except ValueError:
        raise _not_ready_error("请补齐费用的日期、说明和人民币报销金额") from None
    if any(
        (
            item.requires_cny_confirmation
            or (item.original_currency and item.original_currency != "CNY")
        )
        and not item.cny_amount_confirmed
        for item in draft_input.items
    ):
        raise _not_ready_error("请填写并确认海外票据对应的人民币报销金额")
    state = draft_input.editing_state
    if state is not None:
        calculated_trips = request_trips(trip=draft_input.trip, trips=draft_input.trips)
        if not state.include_subsidy and calculated_trips:
            raise _not_ready_error("出差补助选择已变化，请重新确认")
        if state.include_subsidy:
            try:
                editing_trips = state.trips or [state.trip]
                normalized_editing_trips: list[TripInput] = []
                for item in editing_trips:
                    raw = item.model_dump(mode="json", by_alias=True)
                    if not raw.get("confirmedEffectiveDays"):
                        raw.pop("confirmedEffectiveDays", None)
                    normalized_editing_trips.append(TripInput.model_validate(raw))
                if normalized_editing_trips != calculated_trips:
                    raise ValueError("editing trip differs")
            except ValueError:
                raise _not_ready_error("请逐项补齐并确认出差补助的日期和时段") from None


def validate_draft_file_references(
    database: Session,
    *,
    draft_id: str,
    draft_input: ReimbursementDraftInput,
    require_terminal_disposition: bool = False,
    require_submission_proofs: bool = False,
) -> None:
    """Validate receipt provenance without revealing another draft's files."""

    reference_ids = _draft_file_reference_ids(draft_input)
    files = database.scalars(
        select(ReimbursementDraftFile).where(
            ReimbursementDraftFile.draft_id == draft_id,
            ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
        )
    ).all()
    files_by_id = {item.id: item for item in files}
    if require_submission_proofs or require_terminal_disposition:
        for file in files:
            classification = material_classification(file.ocr_result_json)
            if classification and (
                classification.get("status") not in {"classified", "confirmed"}
                or file.ocr_status == ReimbursementOcrStatus.RUNNING.value
            ):
                raise _not_ready_error("请等待材料识别完成，并确认所有待确认材料的用途")
    if any(
        file_id not in files_by_id
        or files_by_id[file_id].processing_role != ReimbursementDraftFileRole.EXPENSE_SOURCE.value
        for file_id in reference_ids
    ):
        raise _invalid_file_reference_error()

    for item in draft_input.items:
        for proof_ids, kind in (
            (item.itinerary_file_ids, "itinerary"),
            (item.payment_proof_file_ids, "payment_proof"),
            (item.hotel_bill_file_ids, "hotel_bill"),
        ):
            if any(
                file_id not in files_by_id
                or files_by_id[file_id].processing_role
                != ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
                or files_by_id[file_id].attachment_kind != kind
                for file_id in proof_ids
            ):
                raise _invalid_file_reference_error()
    if require_submission_proofs:
        validate_submission_evidence(draft_input, files_by_id)

    if not require_terminal_disposition:
        return
    terminal_ids = {
        item.id
        for item in files
        if item.processing_role == ReimbursementDraftFileRole.EXPENSE_SOURCE.value
        and item.ocr_status
        in {
            ReimbursementOcrStatus.COMPLETE.value,
            ReimbursementOcrStatus.FAILED.value,
        }
    }
    if not terminal_ids.issubset(reference_ids):
        raise _not_ready_error("请确认每张已识别票据的费用明细，或明确忽略识别结果")


def validate_submission_evidence(
    draft_input: ReimbursementDraftInput,
    files_by_id: dict[str, ReimbursementDraftFile],
) -> None:
    for item in draft_input.items:
        evidence = file_ocr_evidence(files_by_id.get(item.source_file_id or ""))
        if item.category is ExpenseCategory.LODGING and not item.hotel_bill_file_ids:
            raise _not_ready_error("住宿费用必须上传并关联住宿明细")
        if (
            payment_proof_required(
                amount=item.amount,
                category=item.category,
                rail_type=_authoritative_rail_type(item, evidence),
            )
            and not item.payment_proof_file_ids
        ):
            raise _not_ready_error("单张票据金额超过500元，请上传并关联付款凭证")
        requires_itinerary = (
            item.requires_itinerary
            or item.transport_type == "ride_hailing"
            or evidence.get("requiresItinerary") is True
            or evidence.get("transportType") == "ride_hailing"
        )
        if requires_itinerary and not item.itinerary_file_ids:
            raise _not_ready_error("网约车费用缺少对应行程单，请上传并关联后提交")
        currency = evidence.get("originalCurrency") or item.original_currency
        if (
            item.requires_cny_confirmation
            or _evidence_requires_cny_confirmation(evidence)
            or (currency and currency != "CNY")
        ) and not item.cny_amount_confirmed:
            raise _not_ready_error("请填写并确认海外票据对应的人民币报销金额")


def _evidence_requires_cny_confirmation(evidence: dict[str, object]) -> bool:
    warnings = evidence.get("warnings")
    return evidence.get("type") == "foreign_receipt" or (
        isinstance(warnings, list) and "FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT" in warnings
    )


def _authoritative_rail_type(
    item: ReimbursementDraftExpenseItemInput, evidence: dict[str, object]
) -> str:
    # "other" is an unclassified fallback, not positive non-rail evidence.
    if item.category is not ExpenseCategory.RAIL_FARE or evidence.get("categoryId") not in (
        None,
        "other",
        "rail_fare",
    ):
        return "unknown"
    observed = evidence.get("railType")
    if observed in {"high_speed", "emu", "regular"}:
        return str(observed)
    return item.rail_type


def file_ocr_evidence(file: ReimbursementDraftFile | None) -> dict[str, object]:
    if file is None:
        return {}
    try:
        value = json.loads(file.ocr_result_json or "null")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def apply_ocr_evidence(
    database: Session,
    *,
    draft_id: str,
    draft_input: ReimbursementDraftInput,
) -> ReimbursementDraftInput:
    files = database.scalars(
        select(ReimbursementDraftFile).where(
            ReimbursementDraftFile.draft_id == draft_id,
            ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
        )
    ).all()
    by_id = {file.id: file for file in files}
    items = []
    for item in draft_input.items:
        evidence = file_ocr_evidence(by_id.get(item.source_file_id or ""))
        values = item.model_dump(mode="json", by_alias=True)
        if item.source_file_id is not None:
            values["receiptCount"] = 1
        values["railType"] = _authoritative_rail_type(item, evidence)
        if (
            evidence.get("requiresItinerary") is True
            or evidence.get("transportType") == "ride_hailing"
        ):
            values.update(requiresItinerary=True, transportType="ride_hailing")
        currency = evidence.get("originalCurrency")
        if _evidence_requires_cny_confirmation(evidence):
            values["requiresCnyConfirmation"] = True
        if isinstance(currency, str) and currency != "CNY":
            values["requiresCnyConfirmation"] = True
            values["originalCurrency"] = currency
            if evidence.get("originalAmount") is not None:
                values["originalAmount"] = evidence["originalAmount"]
        items.append(ReimbursementDraftExpenseItemInput.model_validate(values))
    return draft_input.model_copy(update={"items": items})


def detach_draft_file_from_input(
    database: Session,
    *,
    draft: ReimbursementDraft,
    file_id: str,
) -> DraftCalculation:
    """Canonicalize input after removing one file's item and disposition."""
    return detach_draft_files_from_input(database, draft=draft, file_ids={file_id})


def detach_draft_files_from_input(
    database: Session, *, draft: ReimbursementDraft, file_ids: set[str]
) -> DraftCalculation:
    """Remove a batch of sources and proof links in one calculation."""

    draft_input = _stored_input(draft)
    remaining_items = [
        item.model_copy(
            update={
                "itinerary_file_ids": [
                    value for value in item.itinerary_file_ids if value not in file_ids
                ],
                "payment_proof_file_ids": [
                    value for value in item.payment_proof_file_ids if value not in file_ids
                ],
                "hotel_bill_file_ids": [
                    value for value in item.hotel_bill_file_ids if value not in file_ids
                ],
            }
        )
        for item in draft_input.items
        if item.source_file_id not in file_ids
    ]
    remaining_dismissed = [
        value for value in draft_input.dismissed_ocr_file_ids if value not in file_ids
    ]
    if remaining_items == draft_input.items and len(remaining_dismissed) == len(
        draft_input.dismissed_ocr_file_ids
    ):
        return _calculate_input(database, draft_input)
    updated_input = draft_input.model_copy(
        update={
            "items": remaining_items,
            "dismissed_ocr_file_ids": remaining_dismissed,
        }
    )
    return _calculate_input(database, updated_input)


def _calculate_input(
    database: Session,
    draft_input: ReimbursementDraftInput,
    *,
    allow_partial: bool = True,
) -> DraftCalculation:
    subsidies = []
    try:
        subsidies = calculate_trip_subsidies(
            database,
            request_trips(trip=draft_input.trip, trips=draft_input.trips),
        )
    except (ApiError, ValueError):
        if not allow_partial:
            raise
    complete = []
    for item in draft_input.items:
        try:
            complete.extend(
                complete_expense_items(draft_input.model_copy(update={"items": [item]}))
            )
        except ValueError:
            if not allow_partial:
                raise
    totals = calculate_expense_totals(complete, subsidies).as_api_dict()
    totals["subsidy"] = subsidies[0].as_api_dict() if len(subsidies) == 1 else None
    totals["subsidies"] = [item.as_api_dict() for item in subsidies]
    input_data = _canonical_input_data(draft_input)
    return DraftCalculation(
        canonical_json=json.dumps(
            input_data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        input_data=input_data,
        totals_data=totals,
    )


def draft_data(
    database: Session,
    draft: ReimbursementDraft,
    *,
    calculation: DraftCalculation | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    if calculation is None:
        stored_input = _stored_input(draft)
        if draft.status == ReimbursementDraftStatus.LOCKED.value:
            calculation = _locked_draft_calculation(database, draft, stored_input)
        else:
            calculation = _calculate_input(database, stored_input)
    related = database.scalars(
        select(ReimbursementDraftRelatedApproval)
        .where(ReimbursementDraftRelatedApproval.draft_id == draft.id)
        .order_by(ReimbursementDraftRelatedApproval.sort_order)
    ).all()
    return {
        **_draft_summary(draft, now=now or utc_now()),
        "template": {
            "processCode": draft.template_process_code,
            "configVersion": draft.template_config_version,
            "schemaFingerprint": draft.schema_fingerprint,
        },
        "input": calculation.input_data,
        "totals": calculation.totals_data,
        "relatedApprovals": [_related_data(item) for item in related],
        "relatedApprovalSummary": _related_summary(related),
    }


def _locked_draft_calculation(
    database: Session,
    draft: ReimbursementDraft,
    stored_input: ReimbursementDraftInput,
) -> DraftCalculation:
    from app.services.oa_reimbursement_payload import parse_locked_submission_snapshot

    submission = database.scalar(
        select(ReimbursementSubmission).where(ReimbursementSubmission.draft_id == draft.id)
    )
    if submission is None:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_CORRUPTED",
            "锁定报销缺少提交快照，请联系管理员",
            500,
        )
    snapshot = parse_locked_submission_snapshot(draft, submission)
    totals = snapshot.totals.model_dump(mode="json", by_alias=True)
    totals["subsidy"] = (
        snapshot.subsidy.model_dump(mode="json", by_alias=True)
        if snapshot.subsidy is not None
        else None
    )
    totals["subsidies"] = [
        item.model_dump(mode="json", by_alias=True) for item in snapshot.subsidies
    ]
    return DraftCalculation(
        canonical_json=draft.input_json,
        input_data=_canonical_input_data(stored_input),
        totals_data=totals,
    )


def _store_related_approvals(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    expected_binding: CatalogBinding,
    verified: VerifiedTravelSelection | None,
    changed_at: datetime,
) -> dict[str, object]:
    current_catalog = require_submission_ready_catalog(database)
    current_binding = CatalogBinding.from_catalog(current_catalog)
    if current_binding != expected_binding:
        raise _template_changed_error()
    current_draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
        mutable=True,
        now=changed_at,
    )
    _require_catalog_binding(current_draft, current_binding)
    approvals = verified.approvals if verified is not None else ()
    instance_ids = [item.instance.instance_id for item in approvals]
    derived_input = _stored_input(current_draft).model_copy(
        update={
            "company_value": verified.company_option.value if verified else "",
            "accounting_source_verified": verified is not None,
            "budget_code_value": verified.budget_code_option.value if verified else "",
            "project": BudgetProjectInput(mode="manual", text=verified.budget_code_option.label)
            if verified
            else None,
        }
    )
    try:
        bump_owned_draft_revision(
            database,
            draft_id=draft_id,
            actor=actor,
            expected_revision=expected_revision,
            now=changed_at,
        )
        database.execute(
            delete(ReimbursementDraftRelatedApproval).where(
                ReimbursementDraftRelatedApproval.draft_id == draft_id
            )
        )
        for sort_order, approval in enumerate(approvals):
            database.add(
                ReimbursementDraftRelatedApproval(
                    draft_id=draft_id,
                    corp_id=actor.corp_id,
                    owner_user_id=actor.user_id,
                    sort_order=sort_order,
                    process_instance_id=approval.instance.instance_id,
                    travel_profile_key=approval.listed.profile_key,
                    process_code=approval.listed.source_process_code,
                    catalog_config_version=current_binding.config_version,
                    travel_schema_fingerprint=approval.listed.schema_fingerprint,
                    listed_from_ms=approval.listed.query_window.start_time_ms,
                    listed_to_ms=approval.listed.query_window.end_time_ms,
                    travel_start_date=approval.start_date,
                    travel_end_date=approval.end_date,
                    source_travel_type_value=approval.listed.source_travel_type_value,
                    title=approval.instance.title,
                    business_id=approval.instance.business_id,
                    instance_created_at=_upstream_datetime(approval.instance.created_at),
                    verified_at=changed_at,
                    created_at=changed_at,
                    updated_at=changed_at,
                )
            )
        database.execute(
            update(ReimbursementDraft)
            .where(ReimbursementDraft.id == draft_id)
            .values(
                input_json=json.dumps(
                    _canonical_input_data(derived_input),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                related_instance_ids_json=json.dumps(
                    instance_ids,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    updated = require_owned_draft(database, draft_id=draft_id, actor=actor)
    return draft_data(database, updated, now=changed_at)


def _canonical_input_data(draft_input: ReimbursementDraftInput) -> dict[str, object]:
    project = (
        draft_input.project.model_dump(mode="json", by_alias=True) if draft_input.project else None
    )
    items = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in draft_input.items
    ]
    for item, value in zip(draft_input.items, items, strict=True):
        if item.date is None:
            value["date"] = None
        if item.amount is None:
            value["amount"] = None
    trip: dict[str, object] | None = None
    if draft_input.trip is not None:
        trip = draft_input.trip.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude_defaults=True,
        )
        trip["startTime"] = draft_input.trip.start_time.strftime("%H:%M")
        trip["endTime"] = draft_input.trip.end_time.strftime("%H:%M")
    result = {
        "ocrDispositionVersion": draft_input.ocr_disposition_version,
        "companyValue": draft_input.company_value,
        "accountingSourceVerified": draft_input.accounting_source_verified,
        "budgetCodeValue": draft_input.budget_code_value,
        "project": project,
        "trip": trip,
        "items": items,
        "dismissedOcrFileIds": list(draft_input.dismissed_ocr_file_ids),
    }
    if draft_input.trips:
        trips = [
            item.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
                exclude_defaults=True,
            )
            for item in draft_input.trips
        ]
        for item, value in zip(draft_input.trips, trips, strict=True):
            value["startTime"] = item.start_time.strftime("%H:%M")
            value["endTime"] = item.end_time.strftime("%H:%M")
        result["trips"] = trips
    if draft_input.editing_state is not None:
        result["editingState"] = draft_input.editing_state.model_dump(mode="json", by_alias=True)
    return result


def _draft_file_reference_ids(draft_input: ReimbursementDraftInput) -> set[str]:
    return {
        *(item.source_file_id for item in draft_input.items if item.source_file_id is not None),
        *draft_input.dismissed_ocr_file_ids,
    }


def _stored_input(draft: ReimbursementDraft) -> ReimbursementDraftInput:
    try:
        return ReimbursementDraftInput.model_validate_json(draft.input_json)
    except ValueError:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_CORRUPTED",
            "报销数据损坏，请联系管理员",
            500,
        ) from None


def _require_exact_option(
    catalog: OaTemplateCatalogContract,
    logical_field: str,
    requested_value: str,
) -> FormOption:
    component_id = catalog.reimbursement.mappings.get(logical_field)
    component = next(
        (
            item
            for item in catalog.reimbursement.schema.components
            if item.component_id == component_id
        ),
        None,
    )
    option = (
        next(
            (item for item in component.options if item.value == requested_value),
            None,
        )
        if component is not None
        else None
    )
    if option is None:
        code = (
            "REIMBURSEMENT_COMPANY_OPTION_INVALID"
            if logical_field == "company"
            else "REIMBURSEMENT_BUDGET_OPTION_INVALID"
        )
        raise ApiError(code, "所选 OA 模板选项已失效，请刷新后重新选择", 422)
    return option


def _require_catalog_binding(
    draft: ReimbursementDraft,
    binding: CatalogBinding,
) -> None:
    if (
        draft.template_process_code != binding.process_code
        or draft.template_config_version != binding.config_version
        or draft.schema_fingerprint != binding.schema_fingerprint
    ):
        raise _template_changed_error()


def _validate_related_snapshot(
    draft: ReimbursementDraft,
    related: list[ReimbursementDraftRelatedApproval],
    catalog: OaTemplateCatalogContract,
    *,
    draft_input: ReimbursementDraftInput,
) -> None:
    if not related:
        raise _not_ready_error("请先关联至少一张已通过的出差审批单")

    expected_ids = [item.process_instance_id for item in related]
    try:
        stored_ids = json.loads(draft.related_instance_ids_json)
    except (TypeError, ValueError):
        raise _corrupted_error() from None
    if stored_ids != expected_ids:
        raise _corrupted_error()

    profiles = {profile.profile_key: profile for profile in catalog.travel_profiles}
    travel_type_options: dict[str, FormOption] = {}
    travel_type_identities: set[tuple[str, str]] = set()
    for item in related:
        profile = profiles.get(item.travel_profile_key)
        if (
            profile is None
            or item.process_code != profile.process_code
            or item.catalog_config_version != catalog.config_version
            or item.travel_schema_fingerprint != profile.schema.fingerprint
        ):
            raise _template_changed_error()
        if getattr(profile, "travel_type_mappings", None) is not None:
            source_type = item.source_travel_type_value
            option = (
                profile.travel_type_mappings.get(source_type) if source_type is not None else None
            )
            if option is None:
                raise _template_changed_error()
            travel_type_identities.add(("source", source_type))
        else:
            option = profile.travel_type_option
            travel_type_identities.add(("target", option.value))
        travel_type_options[option.value] = option
    if len(travel_type_options) != 1 or len(travel_type_identities) != 1:
        raise _template_changed_error()

    periods = [(item.travel_start_date, item.travel_end_date) for item in related]
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
        raise ApiError(
            "REIMBURSEMENT_SUBSIDY_TRAVEL_TYPE_MISMATCH",
            "出差补助类别与所选出差审批类别不一致，请刷新后重新确认",
            409,
        )
    if draft_input.trips:
        related_by_id = {item.process_instance_id: item for item in related}
        if len(draft_input.trips) != len(related_by_id):
            raise ApiError(
                "REIMBURSEMENT_SUBSIDY_APPROVAL_MISMATCH",
                "每张已关联的出差审批必须对应一个补助项",
                409,
            )
        for trip in draft_input.trips:
            related_item = related_by_id.get(trip.related_approval_id or "")
            if (
                related_item is None
                or trip.start_date != related_item.travel_start_date
                or trip.end_date != related_item.travel_end_date
            ):
                raise ApiError(
                    "REIMBURSEMENT_SUBSIDY_APPROVAL_MISMATCH",
                    "补助项与关联出差审批不一致，请刷新后重新确认",
                    409,
                )
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


def _validate_file_snapshot(
    database: Session,
    *,
    draft_id: str,
    draft_input: ReimbursementDraftInput,
) -> None:
    files = database.scalars(
        select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == draft_id)
    ).all()
    if not any(item.file_status == ReimbursementDraftFileStatus.ACTIVE.value for item in files):
        raise _not_ready_error("请先上传至少一个有效附件")
    if any(
        item.file_status
        in {
            ReimbursementDraftFileStatus.RESERVED.value,
            ReimbursementDraftFileStatus.WRITING.value,
            ReimbursementDraftFileStatus.DELETING.value,
        }
        or item.ocr_status == ReimbursementOcrStatus.RUNNING.value
        or (
            item.file_status == ReimbursementDraftFileStatus.ACTIVE.value
            and item.processing_role == ReimbursementDraftFileRole.EXPENSE_SOURCE.value
            and item.ocr_status == ReimbursementOcrStatus.NOT_REQUESTED.value
        )
        for item in files
    ):
        raise _not_ready_error("票据尚未识别，或附件仍在上传、删除、识别中")
    validate_draft_file_references(
        database,
        draft_id=draft_id,
        draft_input=draft_input,
        require_terminal_disposition=True,
        require_submission_proofs=True,
    )


def _require_department(draft: ReimbursementDraft, actor: DraftActor) -> None:
    if draft.department_id != actor.department_id or draft.department_name != actor.department_name:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_DEPARTMENT_MISMATCH",
            "本次报销所属部门与当前选择不同，请切换部门后重试",
            409,
        )


def _require_mutable(draft: ReimbursementDraft, *, now: datetime) -> None:
    if draft.status == ReimbursementDraftStatus.LOCKED.value or draft.locked_at is not None:
        raise ApiError("REIMBURSEMENT_DRAFT_LOCKED", "报销已进入提交处理，不能继续修改", 409)
    if draft.expires_at <= now or draft.status == ReimbursementDraftStatus.EXPIRED.value:
        raise ApiError("REIMBURSEMENT_DRAFT_EXPIRED", "报销资料已过期，请重新填写", 409)
    if draft.status not in _MUTABLE_STATUSES:
        raise ApiError("REIMBURSEMENT_DRAFT_LOCKED", "报销已进入提交处理，不能继续修改", 409)


def _draft_summary(draft: ReimbursementDraft, *, now: datetime) -> dict[str, object]:
    status = (
        ReimbursementDraftStatus.EXPIRED.value
        if draft.status in _MUTABLE_STATUSES and draft.expires_at <= now
        else draft.status
    )
    return {
        "id": draft.id,
        "status": status,
        "revision": draft.revision,
        "department": {"id": draft.department_id, "name": draft.department_name},
        "templateConfigVersion": draft.template_config_version,
        "relatedApprovalCount": _related_id_count(draft.related_instance_ids_json),
        "expiresAt": _timestamp(draft.expires_at),
        "createdAt": _timestamp(draft.created_at),
        "updatedAt": _timestamp(draft.updated_at),
        "lockedAt": _timestamp(draft.locked_at),
    }


def _related_data(item: ReimbursementDraftRelatedApproval) -> dict[str, object]:
    return {
        "processInstanceId": item.process_instance_id,
        "profileKey": item.travel_profile_key,
        "sourceProcessCode": item.process_code,
        **(
            {"sourceTravelTypeValue": item.source_travel_type_value}
            if item.source_travel_type_value is not None
            else {}
        ),
        "title": item.title,
        "businessId": item.business_id,
        "startDate": item.travel_start_date.isoformat(),
        "endDate": item.travel_end_date.isoformat(),
        "queryWindow": {
            "startTimeMs": item.listed_from_ms,
            "endTimeMs": item.listed_to_ms,
        },
        "verifiedAt": _timestamp(item.verified_at),
    }


def _related_summary(
    related: list[ReimbursementDraftRelatedApproval],
) -> dict[str, object] | None:
    if not related:
        return None
    return {
        "count": len(related),
        "startDate": min(item.travel_start_date for item in related).isoformat(),
        "endDate": max(item.travel_end_date for item in related).isoformat(),
    }


def _related_id_count(value: str) -> int:
    try:
        decoded = json.loads(value)
        return len(decoded) if isinstance(decoded, list) else 0
    except (TypeError, ValueError):
        return 0


def _upstream_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC).replace(tzinfo=None)
    except (AttributeError, ValueError):
        raise ApiError(
            "TRAVEL_APPROVAL_DETAIL_INVALID",
            "钉钉返回的出差审批时间格式无效，请稍后重试",
            502,
        ) from None


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _not_found_error() -> ApiError:
    return ApiError("REIMBURSEMENT_DRAFT_NOT_FOUND", "报销记录不存在", 404)


def _revision_conflict_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
        "报销内容已在其他页面更新，请刷新后重试",
        409,
    )


def _template_changed_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED",
        "OA 审批模板已更新，请重新发起报销",
        409,
    )


def _not_ready_error(message: str) -> ApiError:
    return ApiError("REIMBURSEMENT_DRAFT_NOT_READY", message, 409)


def _invalid_file_reference_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_DRAFT_FILE_REFERENCE_INVALID",
        "费用明细引用的票据文件无效，请刷新后重试",
        422,
    )


def _corrupted_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_DRAFT_CORRUPTED",
        "报销数据损坏，请联系管理员",
        500,
    )
