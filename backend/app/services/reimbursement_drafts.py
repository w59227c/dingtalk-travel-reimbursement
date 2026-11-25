from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import Select, and_, case, delete, func, or_, select, update
from sqlalchemy.orm import Session, undefer

from app.core.config import Settings
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
    ReimbursementSubmissionStatus,
    utc_now,
)
from app.models.session import UserSession
from app.schemas.excel import ExcelExpenseItemInput
from app.schemas.expenses import TripInput
from app.schemas.reimbursements import (
    CURRENT_OCR_DISPOSITION_VERSION,
    BudgetProjectInput,
    GeneralApprovalSummary,
    ReimbursementDraftExpenseItemInput,
    ReimbursementDraftInput,
    RelatedApprovalSelectionInput,
)
from app.services.dingtalk import DepartmentIdentity
from app.services.general_approvals import approval_summary, verify_general_approvals
from app.services.general_reimbursement import (
    general_options,
    get_general_catalog,
    subject_category,
)
from app.services.oa_template_profiles import (
    OaTemplateCatalogContract,
    require_submission_ready_catalog,
)
from app.services.reimbursement_delegation_policy import require_delegation_permission_for_session
from app.services.reimbursement_ocr_state import recover_stale_running_ocr
from app.services.sessions import CurrentSession
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
    available_department_ids: tuple[str, ...] = ()
    operator_user_id: str | None = None
    session_id_hash: str | None = None
    operator_department_ids: tuple[str, ...] = ()

    def require_current_delegation(self, database: Session) -> None:
        # Recheck after remote or filesystem work using the caller's transaction.
        # Internal maintenance actors are reconstructed from an existing draft.
        if self.operator_user_id is None or self.session_id_hash is None:
            return
        session = database.scalar(
            select(UserSession)
            .where(
                UserSession.session_id_hash == self.session_id_hash,
                UserSession.corp_id == self.corp_id,
                UserSession.dingtalk_user_id == self.operator_user_id,
                UserSession.expires_at > utc_now(),
            )
            .execution_options(populate_existing=True)
        )
        if session is None:
            raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
        require_delegation_permission_for_session(database, session, self.operator_department_ids)


@dataclass(frozen=True, slots=True)
class DraftCalculation:
    canonical_json: str
    input_data: dict[str, object]
    totals_data: dict[str, object]
    attachment_mode: str | None = None
    submitted_details: dict[str, object] | None = None


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
    department_name = current.reimbursement_department_name
    department_id = current.reimbursement_department_id
    corp_id = str(current.record.corp_id or "").strip()
    user_id = str(current.reimbursement_user_id or "").strip()
    if not corp_id or not user_id:
        raise ApiError("UNAUTHORIZED", "登录身份数据无效，请重新进入", 401)
    return DraftActor(
        corp_id=corp_id,
        user_id=user_id,
        department_id=department_id or "",
        department_name=department_name or "",
        available_department_ids=tuple(
            department.id for department in current.reimbursement_departments
        ),
        operator_user_id=current.reimbursement_operator_user_id,
        session_id_hash=(
            current.record.session_id_hash
            if current.reimbursement_operator_user_id is not None
            else None
        ),
        operator_department_ids=current.reimbursement_operator_department_ids,
    )


def require_owned_draft(
    database: Session,
    *,
    draft_id: str,
    actor: DraftActor,
    mutable: bool = False,
    allow_submitted_beneficiary: bool = False,
    now: datetime | None = None,
) -> ReimbursementDraft:
    actor.require_current_delegation(database)
    operator = ReimbursementDraft.operator_user_id == actor.operator_user_id
    if allow_submitted_beneficiary and not mutable and actor.operator_user_id is None:
        operator = or_(
            operator,
            and_(
                ReimbursementDraft.status == ReimbursementDraftStatus.LOCKED.value,
                select(ReimbursementSubmission.id)
                .where(
                    ReimbursementSubmission.draft_id == ReimbursementDraft.id,
                    ReimbursementSubmission.corp_id == ReimbursementDraft.corp_id,
                    ReimbursementSubmission.originator_user_id == ReimbursementDraft.owner_user_id,
                    ReimbursementSubmission.operator_user_id == ReimbursementDraft.operator_user_id,
                    ReimbursementSubmission.status == ReimbursementSubmissionStatus.SUBMITTED.value,
                )
                .exists(),
            ),
        )
    draft = database.scalar(
        select(ReimbursementDraft).where(
            ReimbursementDraft.id == draft_id,
            ReimbursementDraft.corp_id == actor.corp_id,
            ReimbursementDraft.owner_user_id == actor.user_id,
            operator,
        )
    )
    if draft is None:
        raise _not_found_error()
    if mutable:
        require_resolved_workspace(database, actor, kind=draft.kind)
        _require_mutable(draft, now=now or utc_now())
    return draft


def require_previewable_draft(
    database: Session,
    *,
    draft_id: str,
    actor: DraftActor,
    expected_revision: int | None = None,
) -> ReimbursementDraft:
    draft = require_owned_draft(
        database, draft_id=draft_id, actor=actor, allow_submitted_beneficiary=True
    )
    if expected_revision is not None and draft.revision != expected_revision:
        raise _revision_conflict_error()
    # Historical locked snapshots remain previewable after workspace retirement.
    # Every editable preview must still belong to a live, completed workspace.
    if draft.status != ReimbursementDraftStatus.LOCKED.value or draft.recovery_pending:
        _require_mutable(draft, now=utc_now())
        files = database.scalars(
            select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == draft.id)
        ).all()
        _validate_linked_support_files(_stored_input(draft), files)
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
    actor.require_current_delegation(database)
    result = database.execute(
        update(ReimbursementDraft)
        .where(
            ReimbursementDraft.id == draft_id,
            ReimbursementDraft.corp_id == actor.corp_id,
            ReimbursementDraft.owner_user_id == actor.user_id,
            ReimbursementDraft.operator_user_id == actor.operator_user_id,
            ReimbursementDraft.revision == expected_revision,
            ReimbursementDraft.status.in_(_MUTABLE_STATUSES),
            ReimbursementDraft.recovery_pending.is_(False),
            ReimbursementDraft.workspace_retired.is_(False),
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
            "报销内容版本已更新，请刷新后重试",
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
    reuse_existing: bool = False,
    replaces_draft_id: str | None = None,
    replaces_draft_revision: int | None = None,
    now: datetime | None = None,
    kind: str = "travel",
    bind_department_for: UserSession | None = None,
    departments: tuple[DepartmentIdentity, ...] = (),
    expected_department_id: str | None = None,
    expected_department_name: str | None = None,
) -> dict[str, object]:
    if kind == "general" and actor.operator_user_id is not None:
        raise ApiError("OA_GENERAL_SELF_ONLY", "普通费用暂只支持本人报销", 403)
    if _draft_file_reference_ids(draft_input) or any(
        item.itinerary_file_ids
        or item.payment_proof_file_ids
        or item.hotel_bill_file_ids
        or item.receipt_attachment_file_ids
        or item.waybill_file_ids
        for item in draft_input.items
    ):
        raise _invalid_file_reference_error()
    draft_input = draft_input.model_copy(
        update={
            "ocr_disposition_version": CURRENT_OCR_DISPOSITION_VERSION,
            "company_value": "",
            "accounting_source_verified": False,
            "general_reason": "",
            "general_approval_ids": [],
            "general_approval_summaries": [],
            "budget_code_value": "",
            "project": None,
        }
    )
    catalog = (get_general_catalog if kind == "general" else require_submission_ready_catalog)(
        database
    )
    # Calculating subsidies can seed settings and commit on an empty database.
    calculation = validate_and_calculate_input(
        database,
        catalog=catalog,
        draft_input=draft_input,
        max_items=max_items,
        allow_partial=True,
    )
    try:
        # Serialize current-record selection, retirement and creation across tabs.
        database.connection().exec_driver_sql("BEGIN IMMEDIATE")
        actor.require_current_delegation(database)
        require_resolved_workspace(database, actor, kind=kind)
        catalog = (get_general_catalog if kind == "general" else require_submission_ready_catalog)(
            database
        )
        created_at = now or utc_now()
        binding = CatalogBinding.from_catalog(catalog)
        if replaces_draft_id:
            # Replacement retires the owned workspace record; it does not edit
            # its old department, expiry or frozen submission contents.
            previous = database.scalar(
                select(ReimbursementDraft)
                .where(
                    ReimbursementDraft.id == replaces_draft_id,
                    ReimbursementDraft.corp_id == actor.corp_id,
                    ReimbursementDraft.owner_user_id == actor.user_id,
                    ReimbursementDraft.operator_user_id == actor.operator_user_id,
                )
                .execution_options(populate_existing=True)
            )
            if previous is None:
                raise _not_found_error()
            latest = database.scalar(
                _workspace_drafts(actor, kind=kind)
                .where(
                    ReimbursementDraft.workspace_retired.is_(False),
                )
                .order_by(ReimbursementDraft.created_at.desc(), ReimbursementDraft.id.desc())
                .limit(1)
            )
            if (
                previous.kind != kind
                or previous.workspace_retired
                or previous.revision != replaces_draft_revision
                or (latest and latest.id != previous.id)
            ):
                raise ApiError(
                    "REIMBURSEMENT_DRAFT_REVISION_CONFLICT", "当前报销已变化，请刷新后再新开", 409
                )
        else:
            existing = database.scalar(
                _workspace_drafts(actor, kind=kind)
                .where(
                    ReimbursementDraft.workspace_retired.is_(False),
                )
                .order_by(ReimbursementDraft.created_at.desc(), ReimbursementDraft.id.desc())
                .limit(1)
            )
            if existing is not None:
                if not reuse_existing:
                    raise ApiError(
                        "REIMBURSEMENT_CURRENT_EXISTS",
                        "已有当前报销，请确认新开后再替换",
                        409,
                    )
                database.commit()
                return draft_data(database, existing, now=created_at)
        # Reuse is independent of a page's stale department choice. Only after
        # deciding to create a record do we validate and adopt its explicit value.
        if expected_department_id is not None or expected_department_name is not None:
            department = next(
                (d for d in departments
                 if (expected_department_id is None or d.id == expected_department_id)
                 and (expected_department_name is None or d.name == expected_department_name)),
                None,
            )
            if department is None:
                raise ApiError("INVALID_DEPARTMENT", "请选择有效的报销部门", 422)
            actor = replace(actor, department_id=department.id, department_name=department.name)
        if not actor.department_id or not actor.department_name:
            raise ApiError("DEPARTMENT_REQUIRED", "请先填写本次报销部门", 409)
        if replaces_draft_id:
            database.execute(
                update(ReimbursementDraft)
                .where(
                    ReimbursementDraft.corp_id == actor.corp_id,
                    ReimbursementDraft.owner_user_id == actor.user_id,
                    ReimbursementDraft.operator_user_id == actor.operator_user_id,
                    ReimbursementDraft.kind == kind,
                )
                .values(workspace_retired=True, updated_at=ReimbursementDraft.updated_at)
            )
        draft = ReimbursementDraft(
            kind=kind,
            corp_id=actor.corp_id,
            owner_user_id=actor.user_id,
            operator_user_id=actor.operator_user_id,
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
        database.add(draft)
        if bind_department_for is not None:
            bind_department_for.current_department_id = actor.department_id
            bind_department_for.current_department_name = actor.department_name
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
    kind: str = "travel",
) -> dict[str, object]:
    actor.require_current_delegation(database)
    statement = _owned_drafts(actor, kind=kind)
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


def list_reimbursement_submission_history(
    database: Session,
    *,
    corp_id: str,
    user_id: str,
    kind: str | None = None,
) -> list[dict[str, object]]:
    from app.services.oa_reimbursement_payload import parse_locked_submission_snapshot

    statement = _initiated_submissions(corp_id, user_id)
    if kind is not None:
        statement = statement.where(ReimbursementDraft.kind == kind)
    rows = database.execute(
        statement.options(
            undefer(ReimbursementDraft.operator_user_id),
            undefer(ReimbursementSubmission.operator_user_id),
        )
        .order_by(ReimbursementDraft.locked_at.desc(), ReimbursementDraft.id.desc())
        .limit(10)
    ).all()
    results = []
    for draft, submission in rows:
        # Never recalculate successful history with current rates or options.
        snapshot = parse_locked_submission_snapshot(draft, submission)
        results.append(
            {
                "id": draft.id,
                "kind": draft.kind,
                "department": {"id": draft.department_id, "name": draft.department_name},
                "lockedAt": _timestamp(draft.locked_at),
                "relatedApprovalCount": _related_id_count(draft.related_instance_ids_json),
                "totalAmount": format(snapshot.totals.total_amount, ".2f"),
                "budgetLabel": snapshot.selections.budget_code.label,
                **(
                    {"beneficiaryName": snapshot.identity.name}
                    if snapshot.identity.user_id != user_id
                    else {}
                ),
            }
        )
    return results


def _initiated_submissions(
    corp_id: str, user_id: str
) -> Select[tuple[ReimbursementDraft, ReimbursementSubmission]]:
    snapshot_json = ReimbursementSubmission.form_snapshot_json
    initiator = case(
        (
            func.json_valid(snapshot_json) == 1,
            func.json_extract(snapshot_json, "$.oaInitiator.userId"),
        )
    )
    return (
        select(ReimbursementDraft, ReimbursementSubmission)
        .join(ReimbursementSubmission, ReimbursementSubmission.draft_id == ReimbursementDraft.id)
        .where(
            ReimbursementDraft.corp_id == corp_id,
            ReimbursementDraft.status == ReimbursementDraftStatus.LOCKED.value,
            ReimbursementDraft.locked_at.is_not(None),
            ReimbursementSubmission.corp_id == corp_id,
            ReimbursementSubmission.originator_user_id == ReimbursementDraft.owner_user_id,
            func.coalesce(initiator, ReimbursementSubmission.originator_user_id) == user_id,
            or_(initiator.is_(None), initiator == ReimbursementSubmission.operator_user_id),
            ReimbursementSubmission.operator_user_id.is_not_distinct_from(
                ReimbursementDraft.operator_user_id
            ),
            ReimbursementSubmission.status == ReimbursementSubmissionStatus.SUBMITTED.value,
        )
    )


def history_draft_actor(database: Session, current: CurrentSession, draft_id: str) -> DraftActor:
    from app.services.oa_reimbursement_payload import parse_locked_submission_snapshot

    corp_id, user_id = current.record.corp_id, current.record.dingtalk_user_id
    row = database.execute(
        _initiated_submissions(corp_id, user_id).where(ReimbursementDraft.id == draft_id)
    ).one_or_none()
    if row is None:
        raise _not_found_error()
    draft, submission = row
    if parse_locked_submission_snapshot(draft, submission).approval_initiator.user_id != user_id:
        raise _not_found_error()
    # Reconstruct the original department for this verified read only. Do not
    # adopt it as the employee's present department or provide write access.
    return DraftActor(
        corp_id,
        draft.owner_user_id,
        draft.department_id,
        draft.department_name,
        operator_user_id=draft.operator_user_id,
    )


def _owned_drafts(
    actor: DraftActor, *, kind: str = "travel"
) -> Select[tuple[ReimbursementDraft]]:
    return select(ReimbursementDraft).where(
        ReimbursementDraft.corp_id == actor.corp_id,
        ReimbursementDraft.owner_user_id == actor.user_id,
        ReimbursementDraft.kind == kind,
        ReimbursementDraft.operator_user_id == actor.operator_user_id,
    )


def _workspace_drafts(
    actor: DraftActor, *, kind: str = "travel"
) -> Select[tuple[ReimbursementDraft]]:
    return select(ReimbursementDraft).where(
        ReimbursementDraft.corp_id == actor.corp_id,
        ReimbursementDraft.owner_user_id == actor.user_id,
        ReimbursementDraft.operator_user_id == actor.operator_user_id,
        ReimbursementDraft.kind == kind,
        ReimbursementDraft.workspace_retired.is_(False),
    )


def require_resolved_workspace(database: Session, actor: DraftActor, *, kind: str) -> None:
    ids = database.scalars(
        _workspace_drafts(actor, kind=kind).with_only_columns(ReimbursementDraft.id).limit(2)
    ).all()
    if len(ids) > 1:
        raise ApiError(
            "REIMBURSEMENT_WORKSPACE_SELECTION_REQUIRED",
            "有多笔旧报销，请先选择保留哪一笔当前报销",
            409,
        )


def get_reimbursement_draft(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
        allow_submitted_beneficiary=True,
    )
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
    catalog = (
        get_general_catalog if draft.kind == "general" else require_submission_ready_catalog
    )(database)
    require_draft_catalog_binding(draft, CatalogBinding.from_catalog(catalog))
    # Accounting values belong to the verified approval selection. An ordinary
    # autosave can carry a stale client snapshot, but cannot replace these values.
    stored_input = _stored_input(draft)
    has_related = bool(json.loads(draft.related_instance_ids_json))
    draft_input = draft_input.model_copy(
        update={
            "company_value": draft_input.company_value
            if catalog.is_general
            else (stored_input.company_value if has_related else ""),
            "accounting_source_verified": stored_input.accounting_source_verified
            if has_related
            else False,
            "budget_code_value": draft_input.budget_code_value
            if catalog.is_general
            else (stored_input.budget_code_value if has_related else ""),
            "general_reason": stored_input.general_reason,
            "general_approval_ids": stored_input.general_approval_ids,
            "general_approval_summaries": stored_input.general_approval_summaries,
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
        saved_general_input=stored_input if catalog.is_general else None,
    )
    draft_input = ReimbursementDraftInput.model_validate(calculation.input_data)
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
    settings: Settings,
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
    recover_stale_running_ocr(
        database,
        draft_id=draft.id,
        ocr_timeout_seconds=settings.ocr_operation_timeout_seconds,
        now=changed_at,
    )
    catalog = (
        get_general_catalog if draft.kind == "general" else require_submission_ready_catalog
    )(database)
    binding = CatalogBinding.from_catalog(catalog)
    require_draft_catalog_binding(draft, binding)
    if not catalog.is_general and not json.loads(draft.related_instance_ids_json):
        raise _not_ready_error("请先关联至少一张已通过的出差审批单")
    draft_input = _stored_input(draft)
    if not catalog.is_general and not draft_input.accounting_source_verified:
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
    draft_input = ReimbursementDraftInput.model_validate(calculation.input_data)
    if not catalog.is_general:
        _validate_related_snapshot(draft, related, catalog, draft_input=draft_input)
    elif not database.scalar(
        select(ReimbursementDraftFile.id)
        .where(
            ReimbursementDraftFile.draft_id == draft.id,
            ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
        )
        .limit(1)
    ):
        raise _not_ready_error("请先上传至少一个有效附件")
    _validate_file_snapshot(database, draft_id=draft.id, draft_input=draft_input)

    try:
        result = database.execute(
            update(ReimbursementDraft)
            .where(
                ReimbursementDraft.id == draft_id,
                ReimbursementDraft.corp_id == actor.corp_id,
                ReimbursementDraft.owner_user_id == actor.user_id,
                ReimbursementDraft.operator_user_id == actor.operator_user_id,
                ReimbursementDraft.revision == expected_revision,
                ReimbursementDraft.status.in_(_MUTABLE_STATUSES),
                ReimbursementDraft.locked_at.is_(None),
                ReimbursementDraft.workspace_retired.is_(False),
                ReimbursementDraft.recovery_pending.is_(False),
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
    departments: tuple[DepartmentIdentity, ...] = (),
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
    if draft.kind != "travel":
        raise _not_found_error()
    catalog = require_submission_ready_catalog(database)
    binding = CatalogBinding.from_catalog(catalog)
    require_draft_catalog_binding(draft, binding)
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
            required_cc_user_id=actor.operator_user_id,
        )
        if domain_selections
        else None
    )
    # Re-derive this ordinary field for every saved selection, including removals.
    # Ambiguous or historical sources retain the current value for manual choice.
    department = None
    if verified is not None:
        department = next((d for d in departments if d.id == verified.department_id), None)
        if department is None and len(departments) == 1:
            department = departments[0]
    return _store_related_approvals(
        database,
        actor=actor,
        draft_id=draft_id,
        expected_revision=expected_revision,
        expected_binding=binding,
        verified=verified,
        target_actor=replace(
            actor, department_id=department.id, department_name=department.name
        ) if department is not None else None,
        changed_at=now or utc_now(),
    )


async def replace_general_approvals(
    database: Session,
    workflow: DingTalkWorkflowClient,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    instance_ids: list[str],
) -> dict[str, object]:
    draft = require_owned_draft(
        database, draft_id=draft_id, actor=actor, mutable=True, now=utc_now()
    )
    if draft.kind != "general" or actor.operator_user_id is not None:
        raise _not_found_error()
    if draft.revision != expected_revision:
        raise _revision_conflict_error()
    binding = CatalogBinding.from_catalog(get_general_catalog(database))
    require_draft_catalog_binding(draft, binding)
    draft_input = _stored_input(draft)
    saved_ids = set(draft_input.general_approval_ids)
    summaries = {
        entry.process_instance_id: entry for entry in draft_input.general_approval_summaries
    }
    database.rollback()
    # Removing a selection must still work when another saved approval is unavailable.
    # Verify all requested approvals when the selection adds an instance.
    if set(instance_ids) - saved_ids:
        approvals = await verify_general_approvals(
            workflow, user_id=actor.user_id, instance_ids=instance_ids
        )
        summaries = {
            item.instance_id: GeneralApprovalSummary.model_validate(approval_summary(item))
            for item in approvals
        }
    calculation = _calculate_input(
        database,
        draft_input.model_copy(
            update={
                "general_approval_ids": instance_ids,
                "general_approval_summaries": [
                    summaries[instance_id]
                    for instance_id in instance_ids
                    if instance_id in summaries
                ],
            },
        ),
    )
    database.connection().exec_driver_sql("BEGIN IMMEDIATE")
    current_binding = CatalogBinding.from_catalog(get_general_catalog(database))
    require_draft_catalog_binding(draft, current_binding)
    revision = bump_owned_draft_revision(
        database,
        draft_id=draft_id,
        actor=actor,
        expected_revision=expected_revision,
        now=utc_now(),
    )
    database.execute(
        update(ReimbursementDraft)
        .where(
            ReimbursementDraft.id == draft_id,
            ReimbursementDraft.revision == revision,
        )
        .values(
            input_json=calculation.canonical_json,
            related_instance_ids_json=json.dumps(instance_ids),
        )
    )
    database.commit()
    database.expire_all()
    return get_reimbursement_draft(database, actor=actor, draft_id=draft_id)


async def read_general_approval_summaries(
    database: Session, workflow: DingTalkWorkflowClient, *, actor: DraftActor, draft_id: str
) -> list[dict[str, object]]:
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor)
    if draft.kind != "general" or actor.operator_user_id is not None:
        raise _not_found_error()
    revision = draft.revision
    draft_input = _stored_input(draft)
    saved = {
        entry.process_instance_id: entry.model_dump(by_alias=True)
        for entry in draft_input.general_approval_summaries
    }
    database.rollback()
    for instance_id in draft_input.general_approval_ids:
        if instance_id in saved:
            continue
        try:
            (instance,) = await verify_general_approvals(
                workflow, user_id=actor.user_id, instance_ids=[instance_id]
            )
            saved[instance_id] = approval_summary(instance)
        except ApiError:
            continue
    current = require_owned_draft(database, draft_id=draft_id, actor=actor)
    if current.revision != revision:
        raise _revision_conflict_error()
    return [
        saved[instance_id]
        for instance_id in draft_input.general_approval_ids
        if instance_id in saved
    ]


def validate_and_calculate_input(
    database: Session,
    *,
    catalog: OaTemplateCatalogContract,
    draft_input: ReimbursementDraftInput,
    max_items: int,
    allow_partial: bool = False,
    saved_general_input: ReimbursementDraftInput | None = None,
) -> DraftCalculation:
    if len(draft_input.items) > max_items:
        raise ApiError(
            "TOO_MANY_EXPENSE_LINES",
            f"当前部署每张报销单最多处理 {max_items} 条费用明细",
            422,
        )
    previous = saved_general_input if allow_partial and catalog.is_general else None
    if draft_input.company_value or not allow_partial:
        _require_exact_option(
            catalog,
            "company",
            draft_input.company_value,
            unchanged_value=previous.company_value if previous else None,
        )
    budget = None
    if draft_input.budget_code_value or not allow_partial:
        budget = _require_exact_option(
            catalog,
            "budgetCode",
            draft_input.budget_code_value,
            unchanged_value=previous.budget_code_value if previous else None,
        )
    if catalog.is_general:
        if (
            draft_input.trip
            or draft_input.trips
            or (draft_input.editing_state and draft_input.editing_state.include_subsidy)
        ):
            raise _not_ready_error("普通费用报销不能申请差旅补助")
        subjects = {option.value: option for option in general_options(catalog, "subjects")}
        retained_subjects = Counter(
            (item.subject_value, item.category)
            for item in (previous.items if previous else [])
            if item.subject_value and item.subject_value not in subjects
        )
        normalized = []
        for index, item in enumerate(draft_input.items, 1):
            selected = subjects.get(item.subject_value or "")
            if item.subject_value and selected is None:
                retained = (item.subject_value, item.category)
                if not retained_subjects[retained]:
                    raise _invalid_general_subject_error(index)
                retained_subjects[retained] -= 1
                normalized.append(item)
                continue
            if selected is None and not allow_partial:
                raise _not_ready_error("请逐项选择费用科目")
            category = subject_category(selected.label) if selected else ExpenseCategory.OTHER
            normalized.append(
                item.model_copy(
                    update={
                        "category": category,
                        "welfare_type": None,
                        "waybill_file_ids": item.waybill_file_ids
                        if category == ExpenseCategory.EXPRESS
                        else [],
                    }
                )
            )
        if not allow_partial and not normalized:
            raise _not_ready_error("请添加费用明细")
        draft_input = draft_input.model_copy(update={"items": normalized, "editing_state": None})
    else:
        if (
            draft_input.general_reason
            or draft_input.general_approval_ids
            or any(
                item.subject_value or item.category == ExpenseCategory.EXPRESS
                for item in draft_input.items
            )
        ):
            raise _not_ready_error("差旅报销不能保存普通费用科目或审批")
        draft_input = draft_input.model_copy(
            update={
                "items": [
                    item.model_copy(
                        update={
                            "welfare_type": None,
                            "waybill_file_ids": [],
                        }
                    )
                    if item.category != ExpenseCategory.EMPLOYEE_WELFARE
                    else item.model_copy(
                        update={
                            "waybill_file_ids": item.waybill_file_ids
                            if item.welfare_type == "express"
                            else [],
                        }
                    )
                    for item in draft_input.items
                ]
            }
        )
    # The selected OA budget option is authoritative for the generated workbook.
    project = BudgetProjectInput(mode="manual", text=budget.label) if budget else None
    if budget is None and previous and draft_input.budget_code_value == previous.budget_code_value:
        project = previous.project
    draft_input = draft_input.model_copy(update={"project": project})
    if not allow_partial:
        require_complete_draft_input(draft_input)
    return _calculate_input(database, draft_input, allow_partial=allow_partial)


def complete_expense_items(draft_input: ReimbursementDraftInput) -> list[ExcelExpenseItemInput]:
    """Only validated workbook fields may reach arithmetic or generation."""
    result = [
        ExcelExpenseItemInput.model_validate(
            item.model_dump(
                mode="json",
                by_alias=True,
                include=set(ExcelExpenseItemInput.model_fields),
            )
        )
        for item in draft_input.items
    ]
    return [
        value.model_copy(update={"description": express_description(value.description)})
        if item.welfare_type == "express"
        else value
        for item, value in zip(draft_input.items, result, strict=True)
    ]


def express_description(description: str) -> str:
    return description if description.startswith("快递") else f"快递｜{description}"


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
    require_linked_support_files: bool = True,
) -> None:
    """Validate receipt provenance without revealing another draft's files."""

    reference_ids = _draft_file_reference_ids(draft_input)
    all_files = database.scalars(
        select(ReimbursementDraftFile).where(
            ReimbursementDraftFile.draft_id == draft_id,
        )
    ).all()
    if require_terminal_disposition and any(
        file.file_status
        in {
            ReimbursementDraftFileStatus.RESERVED.value,
            ReimbursementDraftFileStatus.WRITING.value,
            ReimbursementDraftFileStatus.DELETING.value,
        }
        or (
            file.file_status == ReimbursementDraftFileStatus.ACTIVE.value
            and (
                file.ocr_status == ReimbursementOcrStatus.RUNNING.value
                or (
                    file.processing_role == ReimbursementDraftFileRole.EXPENSE_SOURCE.value
                    and file.ocr_status == ReimbursementOcrStatus.NOT_REQUESTED.value
                )
            )
        )
        for file in all_files
    ):
        raise _not_ready_error("票据尚未识别，或附件仍在上传、删除、识别中")
    files = [
        file for file in all_files if file.file_status == ReimbursementDraftFileStatus.ACTIVE.value
    ]
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

    receipt_attachment_ids: set[str] = set()
    for item in draft_input.items:
        if receipt_attachment_ids.intersection(item.receipt_attachment_file_ids):
            raise _invalid_file_reference_error()
        receipt_attachment_ids.update(item.receipt_attachment_file_ids)
        for proof_ids, kind in (
            (item.itinerary_file_ids, "itinerary"),
            (item.payment_proof_file_ids, "payment_proof"),
            (item.hotel_bill_file_ids, "hotel_bill"),
            (item.receipt_attachment_file_ids, "other"),
            (item.waybill_file_ids, "waybill"),
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
    if require_linked_support_files and (require_terminal_disposition or require_submission_proofs):
        _validate_linked_support_files(draft_input, files)

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


def _validate_linked_support_files(
    draft_input: ReimbursementDraftInput,
    files: Sequence[ReimbursementDraftFile],
) -> None:
    for kind, field, label in (
        ("itinerary", "itinerary_file_ids", "行程单"),
        ("payment_proof", "payment_proof_file_ids", "付款凭证"),
        ("hotel_bill", "hotel_bill_file_ids", "住宿明细"),
    ):
        linked = {file_id for item in draft_input.items for file_id in getattr(item, field)}
        if any(
            file.file_status == ReimbursementDraftFileStatus.ACTIVE.value
            and file.processing_role == ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
            and file.attachment_kind == kind
            and file.id not in linked
            for file in files
        ):
            raise _not_ready_error(
                f"还有未关联费用的{label}，请关联费用、修改用途或删除后再预览和提交"
            )


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
                category=_proof_category(item),
                rail_type=_authoritative_rail_type(item, evidence),
                receipt_count=1 if item.source_file_id else item.receipt_count,
            )
            and not item.payment_proof_file_ids
        ):
            raise _not_ready_error("单张票据金额超过500元，请上传并关联付款凭证")
        requires_itinerary = item.requires_itinerary or item.transport_type == "ride_hailing"
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
    if _proof_category(item) is not ExpenseCategory.RAIL_FARE or evidence.get("categoryId") not in (
        None,
        "other",
        "rail_fare",
    ):
        return "unknown"
    observed = evidence.get("railType")
    if observed in {"high_speed", "emu", "regular"}:
        if item.rail_type == "high_speed" and observed != "high_speed":
            return str(observed)
        return item.rail_type if item.rail_type != "unknown" else str(observed)
    return item.rail_type


def _proof_category(item: ReimbursementDraftExpenseItemInput) -> ExpenseCategory:
    if (
        item.subject_value
        and item.category == ExpenseCategory.LOCAL_TRANSPORT
        and item.transport_type == "rail"
    ):
        return ExpenseCategory.RAIL_FARE
    return item.category


def file_ocr_evidence(file: ReimbursementDraftFile | None) -> dict[str, object]:
    if file is None:
        return {}
    try:
        value = json.loads(file.ocr_result_json or "null")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _original_details_were_edited(
    item: ReimbursementDraftExpenseItemInput,
    evidence: dict[str, object],
) -> bool:
    """Preserve corrected values from drafts written before the edit marker existed."""
    if "original_details_edited" in item.model_fields_set:
        return item.original_details_edited
    if item.original_currency is None and item.original_amount is None:
        return False
    evidence_currency = evidence.get("originalCurrency")
    if item.original_currency != (
        evidence_currency if isinstance(evidence_currency, str) else None
    ):
        return True
    evidence_amount = evidence.get("originalAmount")
    try:
        normalized_evidence_amount = (
            Decimal(str(evidence_amount)) if evidence_amount is not None else None
        )
    except (InvalidOperation, ValueError):
        return True
    return item.original_amount != normalized_evidence_amount


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
        if (
            not item.subject_value
            and item.category == ExpenseCategory.EMPLOYEE_WELFARE
            and item.welfare_type is None
            and "WELFARE_TYPE_NEEDS_CONFIRMATION" in (evidence.get("warnings") or [])
        ):
            values["welfareType"] = "express"
        original_details_edited = _original_details_were_edited(item, evidence)
        if original_details_edited:
            values["originalDetailsEdited"] = True
        if item.source_file_id is not None:
            values["receiptCount"] = 1
        values["railType"] = _authoritative_rail_type(item, evidence)
        currency = evidence.get("originalCurrency")
        if _evidence_requires_cny_confirmation(evidence):
            values["requiresCnyConfirmation"] = True
        if isinstance(currency, str) and currency != "CNY":
            values["requiresCnyConfirmation"] = True
            if not original_details_edited:
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
                "receipt_attachment_file_ids": [
                    value for value in item.receipt_attachment_file_ids if value not in file_ids
                ],
                "waybill_file_ids": [
                    value for value in item.waybill_file_ids if value not in file_ids
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
    if not allow_partial and not draft_input.items and Decimal(totals["subsidyTotal"]) <= 0:
        raise _not_ready_error("请添加费用明细或申请有效的出差补助")
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
        **({"attachmentMode": calculation.attachment_mode} if calculation.attachment_mode else {}),
        **(
            {"submittedDetails": calculation.submitted_details}
            if calculation.submitted_details
            else {}
        ),
        "relatedApprovals": [_related_data(item) for item in related],
        "relatedApprovalSummary": _related_summary(related),
    }


def _locked_draft_calculation(
    database: Session,
    draft: ReimbursementDraft,
    stored_input: ReimbursementDraftInput,
) -> DraftCalculation:
    from app.services.oa_reimbursement_payload import (
        parse_locked_submission_snapshot,
        snapshot_subject_options,
    )

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
        attachment_mode=snapshot.attachment_mode or "bundle_and_excel",
        submitted_details={
            "employeeName": snapshot.identity.name,
            "companyLabel": snapshot.selections.company.label,
            "budgetLabel": snapshot.selections.budget_code.label,
            "travelTypeLabel": snapshot.selections.travel_type.label
            if snapshot.selections.travel_type
            else "",
            **(
                {
                    "subjectLabels": {
                        option.value: option.label for option in snapshot_subject_options(snapshot)
                    }
                }
                if snapshot.kind == "general"
                else {}
            ),
        },
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
    target_actor: DraftActor | None = None,
    bind_department_for: UserSession | None = None,
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
    require_draft_catalog_binding(current_draft, current_binding)
    destination = target_actor or replace(
        actor,
        department_id=current_draft.department_id,
        department_name=current_draft.department_name,
    )
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
                    originator_department_id=approval.instance.originator_department_id,
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
                department_id=destination.department_id,
                department_name=destination.department_name,
                related_instance_ids_json=json.dumps(
                    instance_ids,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
        if bind_department_for is not None:
            bind_department_for.current_department_id = destination.department_id
            bind_department_for.current_department_name = destination.department_name
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    updated = require_owned_draft(database, draft_id=draft_id, actor=destination)
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
    if draft_input.general_reason:
        result["generalReason"] = draft_input.general_reason
    if draft_input.general_approval_ids:
        result["generalApprovalIds"] = draft_input.general_approval_ids
    if draft_input.general_approval_summaries:
        selected = set(draft_input.general_approval_ids)
        result["generalApprovalSummaries"] = [
            item.model_dump(by_alias=True)
            for item in draft_input.general_approval_summaries
            if item.process_instance_id in selected
        ]
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
    *,
    unchanged_value: str | None = None,
) -> FormOption | None:
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
        if requested_value == unchanged_value:
            return None
        code = (
            "REIMBURSEMENT_COMPANY_OPTION_INVALID"
            if logical_field == "company"
            else "REIMBURSEMENT_BUDGET_OPTION_INVALID"
        )
        message = "所选 OA 模板选项已失效，请刷新后重新选择"
        if catalog.is_general:
            field = "报销主体" if logical_field == "company" else "预算代码"
            message = f"请选择有效的{field}"
        raise ApiError(code, message, 422)
    return option


def require_draft_catalog_binding(
    draft: ReimbursementDraft,
    binding: CatalogBinding,
) -> None:
    if (
        draft.template_process_code != binding.process_code
        or draft.template_config_version != binding.config_version
    ):
        raise _template_changed_error()


def require_general_draft_options(
    draft: ReimbursementDraft, catalog: OaTemplateCatalogContract
) -> None:
    require_draft_catalog_binding(draft, CatalogBinding.from_catalog(catalog))
    draft_input = _stored_input(draft)
    _require_exact_option(catalog, "company", draft_input.company_value)
    _require_exact_option(catalog, "budgetCode", draft_input.budget_code_value)
    subjects = {option.value for option in general_options(catalog, "subjects")}
    for index, item in enumerate(draft_input.items, 1):
        if not item.subject_value:
            raise _not_ready_error("请逐项选择费用科目")
        if item.subject_value not in subjects:
            raise _invalid_general_subject_error(index)


def _invalid_general_subject_error(index: int) -> ApiError:
    return ApiError(
        "REIMBURSEMENT_SUBJECT_OPTION_INVALID", f"第 {index} 笔费用的科目已停用，请重新选择", 409
    )


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
    expected_purpose = subsidy_purpose_for_travel_type(
        selected_travel_type.label, related[0].source_travel_type_value
    )
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
    # The caller has already required a complete input and a positive subsidy
    # for an empty expense list. Ordinary expenses still need actual materials.
    if draft_input.items and not any(
        item.file_status == ReimbursementDraftFileStatus.ACTIVE.value for item in files
    ):
        raise _not_ready_error("请先上传至少一个有效附件")
    validate_draft_file_references(
        database,
        draft_id=draft_id,
        draft_input=draft_input,
        require_terminal_disposition=True,
        require_submission_proofs=True,
    )


def _require_mutable(draft: ReimbursementDraft, *, now: datetime) -> None:
    if draft.workspace_retired:
        raise ApiError("REIMBURSEMENT_DRAFT_REPLACED", "已新开另一笔报销，请返回当前报销", 409)
    if draft.recovery_pending:
        raise ApiError(
            "REIMBURSEMENT_RECOVERY_PENDING", "材料尚未恢复完成，请继续恢复后再编辑", 409
        )
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
        **({"kind": draft.kind} if draft.kind == "general" else {}),
        "status": status,
        "revision": draft.revision,
        "department": {"id": draft.department_id, "name": draft.department_name},
        "templateConfigVersion": draft.template_config_version,
        "relatedApprovalCount": _related_id_count(draft.related_instance_ids_json),
        "expiresAt": _timestamp(draft.expires_at),
        "createdAt": _timestamp(draft.created_at),
        "updatedAt": _timestamp(draft.updated_at),
        "lockedAt": _timestamp(draft.locked_at),
        **({"workspaceRetired": True} if draft.workspace_retired else {}),
        **(
            {
                "recoverySourceDraftId": draft.recovery_source_draft_id,
                "recoveryPending": draft.recovery_pending,
            }
            if draft.recovery_source_draft_id
            else {}
        ),
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
        **(
            {"originatorDepartmentId": item.originator_department_id}
            if item.originator_department_id is not None
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
        "报销内容版本已更新，请刷新后重试",
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
