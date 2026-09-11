from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIError
from app.integrations.dingtalk.storage import (
    ApprovalAttachment,
    ApprovalFile,
    ApprovalSpace,
    AttachmentProbe,
    AttachmentRecycleOutcome,
    DingTalkStorageClient,
    DingTalkStorageCommitOutcomeUnknown,
    DingTalkStorageRecycleOutcomeUnknown,
    DingTalkStorageUploadError,
)
from app.integrations.dingtalk.workflow import (
    CreateProcessInstanceCommand,
    CreateWorkflowFormValue,
    DingTalkProcessInstanceCreateOutcomeUnknown,
    DingTalkProcessInstanceCreateRejected,
    DingTalkWorkflowClient,
    FormOption,
    WorkflowFormValue,
    WorkflowProcessInstance,
)
from app.models.reimbursement import (
    ReimbursementDraftFile,
    ReimbursementDraftFileStatus,
    ReimbursementSubmission,
    ReimbursementSubmissionStatus,
    ReimbursementUpload,
    ReimbursementUploadRole,
    ReimbursementUploadStatus,
)
from app.services.excel_generator import generate_expense_workbook
from app.services.oa_reimbursement_payload import (
    ReimbursementSnapshot,
    build_create_command,
    create_command_sha256,
    draft_input_from_snapshot,
    parse_create_command,
    parse_snapshot,
    serialize_create_command,
    snapshot_excel_input,
    verify_excel_template,
)
from app.services.receipt_bundle import (
    RECEIPT_BUNDLE_FILENAME,
    ReceiptBundleSource,
    generate_receipt_bundle,
)
from app.services.reimbursement_drafts import validate_draft_file_references
from app.services.reimbursement_quota import (
    ReimbursementQuotaCoordinator,
    ReimbursementQuotaError,
)
from app.services.reimbursement_quota import (
    SubmissionLease as QuotaSubmissionLease,
)
from app.services.reimbursement_staging import (
    InvalidStorageKey,
    ReimbursementStaging,
    ReimbursementStagingError,
    StagingIntegrityError,
    StagingLayoutError,
    StagingObjectNotFound,
)
from app.services.reimbursement_submissions import (
    ReimbursementSubmissionConflict,
    UploadCheckpoint,
    advance_submission,
    begin_orphan_cleanup,
    begin_upload_cleanup,
    begin_upload_commit,
    begin_upload_put,
    checkpoint_oa_create,
    claim_due_submission,
    complete_orphan_cleanup,
    fail_submission_final,
    fail_submission_retryable,
    finalize_bundled_source_release,
    finalize_local_upload_release,
    list_due_bundled_source_release_candidates,
    list_due_linked_local_release_candidates,
    mark_oa_create_uncertain,
    mark_submission_manual_review,
    mark_submission_submitted,
    mark_upload_commit_uncertain,
    mark_upload_linked,
    record_oa_instance,
    record_upload_cleaned,
    record_upload_committed,
    record_upload_put_done,
    release_submission_lease,
    remove_discarded_generated_upload,
    renew_submission_lease,
    reset_rejected_upload_commit,
)
from app.services.travel_approvals import (
    travel_accounting_options,
    travel_approval_dates,
    travel_instance_type_option,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_RECONCILIATION_PAGE_SIZE = 20
_MAX_RECONCILIATION_PAGES = 100
_RECONCILIATION_CLOCK_SKEW = timedelta(minutes=5)
_JSON_FORM_COMPONENT_TYPES = frozenset({"RelateField", "DDAttachment"})


class WorkHeartbeat(Protocol):
    """Lease-agnostic callback invoked around potentially slow work."""

    async def __call__(self) -> None: ...


async def _call_with_heartbeat(
    heartbeat: WorkHeartbeat,
    operation: Callable[[], Awaitable[_T]],
) -> _T:
    """Renew on both sides of one bounded operation and preserve lease loss."""

    await heartbeat()
    try:
        result = await operation()
    except Exception:
        # An error response may itself consume most of the lease. Renew before
        # the caller persists a retry/final state, but never mask a lost lease.
        await heartbeat()
        raise
    await heartbeat()
    return result


@dataclass(frozen=True, slots=True)
class SubmissionLease:
    """Opaque authority returned by a durable database claim."""

    submission_id: str
    corp_id: str
    user_id: str
    token: str
    status_version: int
    status: ReimbursementSubmissionStatus


@dataclass(frozen=True, slots=True)
class SubmissionUpload:
    id: str
    status_version: int
    status: ReimbursementUploadStatus
    file_name: str
    file_type: str
    media_type: str
    size_bytes: int | None
    sha256: str | None
    sort_order: int = 0
    role: str = "ORIGINAL"
    local_storage_key: str = ""
    source_draft_file_id: str | None = None
    space_id: str | None = None
    file_id: str | None = None

    def as_approval_file(self, content: bytes) -> ApprovalFile:
        if self.size_bytes is None or self.sha256 is None:
            raise ApiError(
                "REIMBURSEMENT_LOCAL_FILE_INCOMPLETE",
                "待提交附件尚未准备完成",
                409,
            )
        if len(content) != self.size_bytes:
            raise ApiError(
                "REIMBURSEMENT_LOCAL_FILE_CHANGED",
                "待提交附件大小与锁定快照不一致",
                409,
            )
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise ApiError(
                "REIMBURSEMENT_LOCAL_FILE_CHANGED",
                "待提交附件内容与锁定快照不一致",
                409,
            )
        return ApprovalFile(
            file_name=self.file_name,
            file_type=self.file_type,
            content=content,
        )

    def as_attachment(self) -> ApprovalAttachment:
        if not self.space_id or not self.file_id or self.size_bytes is None:
            raise ValueError("committed upload has no remote identity")
        return ApprovalAttachment(
            space_id=self.space_id,
            file_id=self.file_id,
            file_name=self.file_name,
            file_size=self.size_bytes,
            file_type=self.file_type,
        )


@dataclass(frozen=True, slots=True)
class SubmissionJob:
    id: str
    draft_id: str
    status: ReimbursementSubmissionStatus
    resume_status: ReimbursementSubmissionStatus | None
    process_code: str
    originator_user_id: str
    originator_union_id: str
    department_id: int
    snapshot_json: str
    snapshot_sha256: str
    oa_request_json: str | None
    oa_request_hash: str | None
    created_at: datetime
    oa_create_started_at: datetime | None
    reconciliation_deadline_at: datetime | None
    process_instance_id: str | None
    attempt_count: int
    uploads: tuple[SubmissionUpload, ...]


class SubmissionStatePort(Protocol):
    """All mutations are durable CAS operations guarded by ``SubmissionLease``."""

    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> SubmissionLease | None: ...

    def renew(
        self,
        lease: SubmissionLease,
        *,
        lease_seconds: int,
        now: datetime,
    ) -> SubmissionLease: ...

    def load_job(self, lease: SubmissionLease) -> SubmissionJob: ...

    def transition(
        self,
        lease: SubmissionLease,
        *,
        to_status: ReimbursementSubmissionStatus,
        next_attempt_at: datetime | None = None,
        resume_status: ReimbursementSubmissionStatus | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
        increment_reconciliation: bool = False,
    ) -> SubmissionLease: ...

    def ensure_generated_excel(
        self,
        lease: SubmissionLease,
        *,
        snapshot_json: str,
        snapshot_sha256: str,
    ) -> SubmissionLease: ...

    def begin_upload_put(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload: ...

    def record_upload_put_done(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload: ...

    def begin_upload_commit(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload: ...

    def record_upload_committed(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        attachment: ApprovalAttachment,
    ) -> SubmissionUpload: ...

    def mark_upload_retryable(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        *,
        error_code: str,
    ) -> None: ...

    def mark_upload_commit_uncertain(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        *,
        error_code: str,
    ) -> None: ...

    def confirm_orphan_cleanup(
        self,
        lease: SubmissionLease,
        *,
        confirmation_code: str,
        now: datetime,
    ) -> SubmissionLease: ...

    def begin_upload_cleanup(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload: ...

    def record_upload_cleaned(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        *,
        now: datetime,
    ) -> None: ...

    def checkpoint_oa_create(
        self,
        lease: SubmissionLease,
        *,
        request_json: str,
        request_hash: str,
        reconciliation_deadline_at: datetime,
    ) -> SubmissionLease: ...

    def record_oa_instance(
        self,
        lease: SubmissionLease,
        *,
        process_instance_id: str,
    ) -> SubmissionLease: ...

    def complete_submission(
        self,
        lease: SubmissionLease,
        *,
        process_instance_id: str,
        business_id: str,
        approval_url: str | None,
        now: datetime,
    ) -> None: ...


class SubmissionMaterializer(Protocol):
    """Pure snapshot/file operations; it never decides durable state transitions."""

    async def validate(
        self,
        job: SubmissionJob,
        *,
        heartbeat: WorkHeartbeat,
    ) -> None: ...

    def read_upload(self, job: SubmissionJob, upload: SubmissionUpload) -> bytes: ...

    def build_create_command(
        self,
        job: SubmissionJob,
        attachments: Sequence[ApprovalAttachment],
    ) -> CreateProcessInstanceCommand: ...

    def serialize_create_command(self, command: CreateProcessInstanceCommand) -> str: ...

    def parse_create_command(
        self,
        value: str,
        *,
        expected_sha256: str,
    ) -> CreateProcessInstanceCommand: ...

    def create_command_hash(self, command: CreateProcessInstanceCommand) -> str: ...

    def matches_instance(
        self,
        command: CreateProcessInstanceCommand,
        instance: WorkflowProcessInstance,
    ) -> bool: ...

    def approval_url(self, process_instance_id: str) -> str | None: ...


class SnapshotSubmissionMaterializer:
    """Use only the locked snapshot and verified staging objects at execution time."""

    def __init__(
        self,
        *,
        workflow: DingTalkWorkflowClient,
        staging: ReimbursementStaging,
        excel_template_path: Path,
        approval_url_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._workflow = workflow
        self._staging = staging
        self._excel_template_path = Path(excel_template_path)
        self._approval_url_factory = approval_url_factory

    async def validate(
        self,
        job: SubmissionJob,
        *,
        heartbeat: WorkHeartbeat,
    ) -> None:
        snapshot = self._snapshot(job)
        _validate_job_identity(job, snapshot)
        await _call_with_heartbeat(
            heartbeat,
            lambda: asyncio.to_thread(
                verify_excel_template,
                snapshot,
                self._excel_template_path,
            ),
        )
        schema = await _call_with_heartbeat(
            heartbeat,
            lambda: self._workflow.get_form_schema(snapshot.template.process_code),
        )
        if schema.fingerprint != snapshot.template.schema_fingerprint:
            raise ApiError(
                "OA_TEMPLATE_CHANGED",
                "审批模板已更新，请重新确认后提交",
                409,
            )

        profile_by_key = {item.profile_key: item for item in snapshot.template.travel_profiles}
        schema_by_process_code = {}
        related_with_profiles = []
        for related in snapshot.related_approvals:
            profile = profile_by_key.get(related.profile_key)
            if profile is None or profile.process_code != related.process_code:
                raise ApiError(
                    "TRAVEL_APPROVAL_TEMPLATE_CHANGED",
                    "关联出差审批模板已变化，请重新选择",
                    409,
                )
            travel_schema = schema_by_process_code.get(profile.process_code)
            if travel_schema is None:
                travel_schema = await _call_with_heartbeat(
                    heartbeat,
                    lambda process_code=profile.process_code: self._workflow.get_form_schema(
                        process_code
                    ),
                )
                schema_by_process_code[profile.process_code] = travel_schema
            if travel_schema.fingerprint != profile.schema_fingerprint:
                raise ApiError(
                    "TRAVEL_APPROVAL_TEMPLATE_CHANGED",
                    "关联出差审批模板已更新，请重新选择",
                    409,
                )
            related_with_profiles.append((related, profile))

        membership_groups = {}
        for related, _profile in related_with_profiles:
            key = (
                related.process_code,
                related.listed_from_ms,
                related.listed_to_ms,
                snapshot.identity.user_id,
            )
            membership_groups.setdefault(key, []).append(related)
        for (
            process_code,
            listed_from_ms,
            listed_to_ms,
            user_id,
        ), related_group in membership_groups.items():
            expected_ids = {item.process_instance_id for item in related_group}
            listed_ids = await self._listed_instance_ids(
                process_code=process_code,
                listed_from_ms=listed_from_ms,
                listed_to_ms=listed_to_ms,
                user_id=user_id,
                expected_ids=expected_ids,
                heartbeat=heartbeat,
            )
            if not expected_ids.issubset(listed_ids):
                raise ApiError(
                    "TRAVEL_APPROVAL_MEMBERSHIP_CHANGED",
                    "所选出差审批已不可用，请重新选择",
                    409,
                )

        for related, profile in related_with_profiles:
            instance = await _call_with_heartbeat(
                heartbeat,
                lambda instance_id=related.process_instance_id: self._workflow.get_process_instance(
                    instance_id
                ),
            )
            dates = travel_approval_dates(
                instance,
                start_date_component_id=profile.start_date_component_id,
                end_date_component_id=profile.end_date_component_id,
            )
            if (
                instance.instance_id != related.process_instance_id
                or instance.originator_user_id != snapshot.identity.user_id
                or instance.status != "COMPLETED"
                or instance.result != "agree"
                or dates is None
                or dates[0] != related.start_date
                or dates[1] != related.end_date
            ):
                raise ApiError(
                    "TRAVEL_APPROVAL_MEMBERSHIP_CHANGED",
                    "所选出差审批状态或日期已变化，请重新选择",
                    409,
                )
            option, source_value, type_reason = travel_instance_type_option(
                instance,
                source_schema=schema_by_process_code[profile.process_code],
                component_id=profile.travel_type_component_id,
                mappings=(
                    {
                        key: FormOption(**value.model_dump())
                        for key, value in profile.travel_type_mappings.items()
                    }
                    if profile.travel_type_mappings is not None
                    else None
                ),
                fixed_option=FormOption(**profile.travel_type_option.model_dump()),
            )
            if (
                type_reason
                or source_value != related.source_travel_type_value
                or option is None
                or option.as_dict() != snapshot.selections.travel_type.model_dump()
            ):
                raise ApiError(
                    "TRAVEL_APPROVAL_TYPE_CHANGED",
                    type_reason or "出差审批类别已变化，请重新关联后提交",
                    409,
                )
            fields = {field.logical_key: field.component_id for field in snapshot.template.fields}
            components = {component.component_id: component for component in schema.components}
            company, budget, reason = travel_accounting_options(
                instance,
                source_schema=schema_by_process_code[profile.process_code],
                company_component_id=profile.company_component_id,
                budget_code_component_id=profile.budget_code_component_id,
                company_options=components[fields["company"]].options,
                budget_options=components[fields["budgetCode"]].options,
            )
            if (
                reason
                or company is None
                or budget is None
                or company.as_dict() != snapshot.selections.company.model_dump()
                or budget.as_dict() != snapshot.selections.budget_code.model_dump()
            ):
                raise ApiError(
                    "TRAVEL_APPROVAL_ACCOUNTING_CHANGED",
                    reason or "出差审批的所属公司或预算代码已变化，请重新关联后提交",
                    409,
                )
        _validate_original_manifest(snapshot, job.uploads)

    def generate_excel(self, job: SubmissionJob) -> tuple[str, str, bytes]:
        snapshot = self._snapshot(job)
        verify_excel_template(snapshot, self._excel_template_path)
        workbook_input = snapshot_excel_input(snapshot)
        result = generate_expense_workbook(
            template_path=self._excel_template_path,
            employee_name=workbook_input.employee_name,
            department_name=workbook_input.department_name,
            project=workbook_input.project,
            trip=workbook_input.trip,
            items=workbook_input.items,
            subsidy=workbook_input.subsidy,
            totals=workbook_input.totals,
            trips=workbook_input.trips,
            subsidies=workbook_input.subsidies,
        )
        if result.filename != snapshot.excel.file_name:
            raise ApiError(
                "REIMBURSEMENT_EXCEL_NAME_MISMATCH",
                "报销单文件名与锁定快照不一致",
                409,
            )
        return result.filename, snapshot.excel.media_type, result.content

    def generate_bundle(self, job: SubmissionJob) -> tuple[str, str, bytes]:
        snapshot = self._snapshot(job)

        def sources():
            for source in snapshot.original_files:
                upload = SubmissionUpload(
                    id=source.draft_file_id,
                    status_version=1,
                    status=ReimbursementUploadStatus.PENDING,
                    file_name=source.file_name,
                    file_type=source.file_type,
                    media_type=source.media_type,
                    size_bytes=source.size_bytes,
                    sha256=source.sha256,
                    local_storage_key=source.storage_key,
                )
                yield ReceiptBundleSource(
                    source.file_name, source.file_type, self.read_upload(job, upload)
                )

        content = generate_receipt_bundle(sources(), max_bytes=self._staging.max_object_bytes)
        return RECEIPT_BUNDLE_FILENAME, "application/pdf", content

    def read_upload(self, _job: SubmissionJob, upload: SubmissionUpload) -> bytes:
        if not upload.local_storage_key:
            raise ApiError(
                "REIMBURSEMENT_LOCAL_FILE_MISSING",
                "待提交附件存储信息不完整",
                500,
            )
        if upload.size_bytes is None or upload.sha256 is None:
            raise ApiError(
                "REIMBURSEMENT_LOCAL_FILE_INCOMPLETE",
                "待提交附件尚未准备完成",
                409,
            )
        try:
            return self._staging.read_bytes(
                upload.local_storage_key,
                expected_size=upload.size_bytes,
                expected_sha256=upload.sha256,
            )
        except StagingObjectNotFound:
            raise ApiError(
                "REIMBURSEMENT_LOCAL_FILE_MISSING",
                "锁定的待提交附件已不存在",
                409,
            ) from None
        except (InvalidStorageKey, StagingIntegrityError, StagingLayoutError):
            raise ApiError(
                "REIMBURSEMENT_LOCAL_FILE_CHANGED",
                "锁定的待提交附件已损坏或发生变化",
                409,
            ) from None
        except ReimbursementStagingError:
            raise ApiError(
                "REIMBURSEMENT_STAGING_UNAVAILABLE",
                "报销附件暂存服务暂时不可用",
                503,
            ) from None

    def build_create_command(
        self,
        job: SubmissionJob,
        attachments: Sequence[ApprovalAttachment],
    ) -> CreateProcessInstanceCommand:
        return build_create_command(self._snapshot(job), attachments)

    def serialize_create_command(self, command: CreateProcessInstanceCommand) -> str:
        return serialize_create_command(command)

    def parse_create_command(
        self,
        value: str,
        *,
        expected_sha256: str,
    ) -> CreateProcessInstanceCommand:
        return parse_create_command(value, expected_sha256=expected_sha256)

    def create_command_hash(self, command: CreateProcessInstanceCommand) -> str:
        return create_command_sha256(command)

    def matches_instance(
        self,
        command: CreateProcessInstanceCommand,
        instance: WorkflowProcessInstance,
    ) -> bool:
        return strict_instance_matches_command(command, instance)

    def approval_url(self, process_instance_id: str) -> str | None:
        if self._approval_url_factory is None:
            return None
        value = self._approval_url_factory(process_instance_id)
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise ApiError(
                "OA_APPROVAL_URL_INVALID",
                "审批详情链接配置无效",
                500,
            )
        return value.strip()

    def _snapshot(self, job: SubmissionJob) -> ReimbursementSnapshot:
        return parse_snapshot(job.snapshot_json, expected_sha256=job.snapshot_sha256)

    async def _listed_instance_ids(
        self,
        *,
        process_code: str,
        listed_from_ms: int,
        listed_to_ms: int,
        user_id: str,
        expected_ids: set[str],
        heartbeat: WorkHeartbeat,
    ) -> frozenset[str]:
        next_token = 0
        listed_ids: set[str] = set()
        for _ in range(50):
            page = await _call_with_heartbeat(
                heartbeat,
                lambda token=next_token: self._workflow.list_process_instance_ids(
                    process_code=process_code,
                    start_time=listed_from_ms,
                    end_time=listed_to_ms,
                    next_token=token,
                    max_results=20,
                    user_ids=(user_id,),
                    statuses=("COMPLETED",),
                ),
            )
            listed_ids.update(page.instance_ids)
            if expected_ids.issubset(listed_ids):
                return frozenset(listed_ids)
            if page.next_token is None:
                return frozenset(listed_ids)
            next_token = page.next_token
        raise ApiError(
            "TRAVEL_APPROVAL_RESULT_LIMIT",
            "关联出差审批查询结果过多，请缩小日期范围",
            422,
        )


class DatabaseSubmissionState:
    """Adapter from the orchestrator port to short SQLAlchemy transactions."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        worker_id: str,
        quota: ReimbursementQuotaCoordinator,
        staging: ReimbursementStaging,
        materializer: SnapshotSubmissionMaterializer,
        generated_reservation_seconds: int,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if generated_reservation_seconds < 30:
            raise ValueError("generated_reservation_seconds must be at least thirty")
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._quota = quota
        self._staging = staging
        self._materializer = materializer
        self._generated_reservation_seconds = generated_reservation_seconds

    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> SubmissionLease | None:
        if worker_id != self._worker_id:
            raise ValueError("worker identity does not match state adapter")
        with self._session_factory() as database:
            claim = claim_due_submission(
                database,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                now=now,
            )
        if claim is None:
            return None
        return _work_lease(claim.lease, ReimbursementSubmissionStatus(claim.status))

    def renew(
        self,
        lease: SubmissionLease,
        *,
        lease_seconds: int,
        now: datetime,
    ) -> SubmissionLease:
        with self._session_factory() as database:
            renewed = renew_submission_lease(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                lease_seconds=lease_seconds,
                now=now,
            )
        return _work_lease(renewed, lease.status)

    def load_job(self, lease: SubmissionLease) -> SubmissionJob:
        now = _utc_now()
        with self._session_factory() as database:
            submission = database.scalar(
                select(ReimbursementSubmission).where(
                    ReimbursementSubmission.id == lease.submission_id,
                    ReimbursementSubmission.corp_id == lease.corp_id,
                    ReimbursementSubmission.originator_user_id == lease.user_id,
                    ReimbursementSubmission.status_version == lease.status_version,
                    ReimbursementSubmission.status == lease.status.value,
                    ReimbursementSubmission.lease_owner == self._worker_id,
                    ReimbursementSubmission.lease_token == lease.token,
                    ReimbursementSubmission.lease_expires_at.is_not(None),
                    ReimbursementSubmission.lease_expires_at > now,
                )
            )
            if submission is None:
                raise ReimbursementSubmissionConflict("submission lease is stale or expired")
            rows = database.scalars(
                select(ReimbursementUpload)
                .where(ReimbursementUpload.submission_id == submission.id)
                .order_by(ReimbursementUpload.sort_order, ReimbursementUpload.id)
            ).all()
            try:
                department_id = int(submission.department_id)
                status = ReimbursementSubmissionStatus(submission.status)
                resume_status = (
                    ReimbursementSubmissionStatus(submission.resume_status)
                    if submission.resume_status is not None
                    else None
                )
                uploads = tuple(_work_upload(row) for row in rows)
            except (TypeError, ValueError):
                raise ApiError(
                    "REIMBURSEMENT_SUBMISSION_CORRUPTED",
                    "报销提交记录损坏，请联系管理员",
                    500,
                ) from None
            return SubmissionJob(
                id=submission.id,
                draft_id=submission.draft_id,
                status=status,
                resume_status=resume_status,
                process_code=submission.template_process_code,
                originator_user_id=submission.originator_user_id,
                originator_union_id=submission.originator_union_id,
                department_id=department_id,
                snapshot_json=submission.form_snapshot_json,
                snapshot_sha256=submission.snapshot_sha256,
                oa_request_json=submission.oa_request_json,
                oa_request_hash=submission.oa_request_hash,
                created_at=submission.created_at,
                oa_create_started_at=submission.oa_create_started_at,
                reconciliation_deadline_at=submission.reconciliation_deadline_at,
                process_instance_id=submission.process_instance_id,
                attempt_count=submission.attempt_count,
                uploads=uploads,
            )

    def transition(
        self,
        lease: SubmissionLease,
        *,
        to_status: ReimbursementSubmissionStatus,
        next_attempt_at: datetime | None = None,
        resume_status: ReimbursementSubmissionStatus | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
        increment_reconciliation: bool = False,
    ) -> SubmissionLease:
        del increment_attempt, increment_reconciliation
        now = _utc_now()
        database_lease = _quota_lease(lease)
        code = error_code or "REIMBURSEMENT_SUBMISSION_FAILED"
        message = error_message or "报销提交处理失败"
        with self._session_factory() as database:
            if to_status in {
                ReimbursementSubmissionStatus.VALIDATING,
                ReimbursementSubmissionStatus.GENERATING_EXCEL,
                ReimbursementSubmissionStatus.UPLOADING,
            }:
                changed = advance_submission(
                    database,
                    lease=database_lease,
                    worker_id=self._worker_id,
                    to_status=to_status,
                    now=now,
                )
                return _work_lease(changed, to_status)

            if to_status is ReimbursementSubmissionStatus.FAILED_RETRYABLE:
                if resume_status is not None and resume_status is not lease.status:
                    raise ValueError("resume status differs from the leased phase")
                if next_attempt_at is None:
                    raise ValueError("retryable failure requires next_attempt_at")
                version = fail_submission_retryable(
                    database,
                    lease=database_lease,
                    worker_id=self._worker_id,
                    next_attempt_at=next_attempt_at,
                    error_code=code,
                    error_message=message,
                    now=now,
                )
                return replace_lease_terminal(lease, to_status, version)

            if to_status is ReimbursementSubmissionStatus.RECONCILING:
                changed = database_lease
                if lease.status is ReimbursementSubmissionStatus.OA_CREATING:
                    changed = mark_oa_create_uncertain(
                        database,
                        lease=database_lease,
                        worker_id=self._worker_id,
                        error_code=code,
                        error_message=message,
                        now=now,
                    )
                elif lease.status is not ReimbursementSubmissionStatus.RECONCILING:
                    raise ValueError("only OA_CREATING or RECONCILING may reconcile")
                retry_at = next_attempt_at or now + timedelta(seconds=1)
                version = release_submission_lease(
                    database,
                    lease=changed,
                    worker_id=self._worker_id,
                    next_attempt_at=retry_at,
                    now=now,
                )
                return replace_lease_terminal(lease, to_status, version)

            if to_status is ReimbursementSubmissionStatus.MANUAL_REVIEW:
                version = mark_submission_manual_review(
                    database,
                    lease=database_lease,
                    worker_id=self._worker_id,
                    error_code=code,
                    error_message=message,
                    now=now,
                )
                return replace_lease_terminal(lease, to_status, version)

            if to_status is ReimbursementSubmissionStatus.FAILED_FINAL:
                if lease.status is ReimbursementSubmissionStatus.ORPHAN_CLEANUP:
                    version = complete_orphan_cleanup(
                        database,
                        lease=database_lease,
                        worker_id=self._worker_id,
                        error_code=code,
                        error_message=message,
                        now=now,
                    )
                else:
                    version = fail_submission_final(
                        database,
                        lease=database_lease,
                        worker_id=self._worker_id,
                        error_code=code,
                        error_message=message,
                        now=now,
                    )
                return replace_lease_terminal(lease, to_status, version)
        raise ValueError(f"unsupported state transition to {to_status.value}")

    def ensure_generated_excel(
        self,
        lease: SubmissionLease,
        *,
        snapshot_json: str,
        snapshot_sha256: str,
    ) -> SubmissionLease:
        job = self.load_job(lease)
        if job.snapshot_json != snapshot_json or job.snapshot_sha256 != snapshot_sha256:
            raise ReimbursementSubmissionConflict("submission snapshot changed")
        parse_snapshot(snapshot_json, expected_sha256=snapshot_sha256)
        self._ensure_generated_file(
            lease,
            job,
            role=ReimbursementUploadRole.GENERATED_PDF,
            sort_order=0,
            generate=self._materializer.generate_bundle,
        )
        self._ensure_generated_file(
            lease,
            job,
            role=ReimbursementUploadRole.GENERATED_EXCEL,
            sort_order=1,
            generate=self._materializer.generate_excel,
        )
        return lease

    def _ensure_generated_file(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
        *,
        role: ReimbursementUploadRole,
        sort_order: int,
        generate: Callable[[SubmissionJob], tuple[str, str, bytes]],
    ) -> None:
        generated = tuple(item for item in job.uploads if item.role == role.value)
        if len(generated) > 1:
            raise ApiError(
                "REIMBURSEMENT_SUBMISSION_CORRUPTED",
                "报销单生成记录重复，请联系管理员",
                500,
            )
        if generated:
            existing = generated[0]
            if (
                existing.status is ReimbursementUploadStatus.PENDING
                and existing.size_bytes is not None
                and existing.sha256 is not None
            ):
                return
            if existing.status is ReimbursementUploadStatus.DISCARDED:
                with self._session_factory() as database:
                    remove_discarded_generated_upload(
                        database,
                        lease=_quota_lease(lease),
                        worker_id=self._worker_id,
                        upload_id=existing.id,
                        expected_status_version=existing.status_version,
                    )
            else:
                raise ApiError(
                    "REIMBURSEMENT_EXCEL_GENERATION_PENDING",
                    "报销单生成仍在恢复，请稍后重试",
                    503,
                )

        file_name, _media_type, content = generate(job)
        if len(content) > self._staging.max_object_bytes:
            raise ApiError(
                "REIMBURSEMENT_EXCEL_TOO_LARGE",
                "生成的报销单超过附件大小限制",
                413,
            )
        quota_lease = _quota_lease(lease)
        reservation = None
        try:
            reservation = self._quota.reserve_generated_upload(
                quota_lease,
                sort_order=sort_order,
                file_name=file_name,
                role=role,
                reserved_bytes=len(content),
                expires_at=_utc_now() + timedelta(seconds=self._generated_reservation_seconds),
            )
            self._quota.mark_writing(quota_lease, reservation)
            staged = self._staging.write_bytes(
                reservation.staging,
                content,
                expected_size=len(content),
                expected_sha256=hashlib.sha256(content).hexdigest(),
            )
            self._quota.finalize(quota_lease, reservation, staged)
        except (ReimbursementQuotaError, ReimbursementStagingError) as exc:
            self._release_failed_generated(quota_lease, reservation)
            raise ApiError(
                "REIMBURSEMENT_STAGING_UNAVAILABLE",
                "报销单暂存空间不足，请稍后重试",
                503,
            ) from exc
        except Exception:
            self._release_failed_generated(quota_lease, reservation)
            raise

    def begin_upload_put(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload:
        with self._session_factory() as database:
            checkpoint = begin_upload_put(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
            )
        return replace_upload_checkpoint(upload, checkpoint)

    def record_upload_put_done(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload:
        with self._session_factory() as database:
            checkpoint = record_upload_put_done(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
            )
        return replace_upload_checkpoint(upload, checkpoint)

    def begin_upload_commit(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload:
        with self._session_factory() as database:
            checkpoint = begin_upload_commit(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
            )
        return replace_upload_checkpoint(upload, checkpoint)

    def record_upload_committed(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        attachment: ApprovalAttachment,
    ) -> SubmissionUpload:
        with self._session_factory() as database:
            checkpoint = record_upload_committed(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
                space_id=attachment.space_id,
                file_id=attachment.file_id,
                file_name=attachment.file_name,
                file_size=attachment.file_size,
                file_type=attachment.file_type,
            )
        return replace(
            replace_upload_checkpoint(upload, checkpoint),
            space_id=attachment.space_id,
            file_id=attachment.file_id,
            file_name=attachment.file_name.strip(),
            file_type=attachment.file_type.strip(),
            size_bytes=attachment.file_size,
        )

    def mark_upload_retryable(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        *,
        error_code: str,
    ) -> None:
        if upload.status is ReimbursementUploadStatus.PUTTING:
            # PUT is idempotent and begin_upload_put accepts PUTTING, so the
            # persisted checkpoint itself is the retry marker.
            return
        if upload.status is not ReimbursementUploadStatus.COMMITTING:
            raise ValueError("only PUTTING or definitively rejected COMMITTING may retry")
        with self._session_factory() as database:
            reset_rejected_upload_commit(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
                error_code=error_code,
            )

    def mark_upload_commit_uncertain(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        *,
        error_code: str,
    ) -> None:
        with self._session_factory() as database:
            mark_upload_commit_uncertain(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
                error_code=error_code,
                error_message="附件提交结果需要人工核对",
            )

    def confirm_orphan_cleanup(
        self,
        lease: SubmissionLease,
        *,
        confirmation_code: str,
        now: datetime,
    ) -> SubmissionLease:
        with self._session_factory() as database:
            changed = begin_orphan_cleanup(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                confirmation_code=confirmation_code,
                now=now,
            )
        return _work_lease(changed, ReimbursementSubmissionStatus.ORPHAN_CLEANUP)

    def begin_upload_cleanup(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
    ) -> SubmissionUpload:
        with self._session_factory() as database:
            checkpoint = begin_upload_cleanup(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
            )
        return replace_upload_checkpoint(upload, checkpoint)

    def record_upload_cleaned(
        self,
        lease: SubmissionLease,
        upload: SubmissionUpload,
        *,
        now: datetime,
    ) -> None:
        with self._session_factory() as database:
            record_upload_cleaned(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                upload_id=upload.id,
                expected_status_version=upload.status_version,
                now=now,
            )

    def checkpoint_oa_create(
        self,
        lease: SubmissionLease,
        *,
        request_json: str,
        request_hash: str,
        reconciliation_deadline_at: datetime,
    ) -> SubmissionLease:
        with self._session_factory() as database:
            submission = database.get(ReimbursementSubmission, lease.submission_id)
            if submission is None:
                raise ReimbursementSubmissionConflict("submission disappeared")
            snapshot = parse_snapshot(
                submission.form_snapshot_json, expected_sha256=submission.snapshot_sha256
            )
            validate_draft_file_references(
                database,
                draft_id=submission.draft_id,
                draft_input=draft_input_from_snapshot(snapshot),
                require_terminal_disposition=True,
                require_submission_proofs=True,
            )
            sources = database.scalars(
                select(ReimbursementDraftFile).where(
                    ReimbursementDraftFile.draft_id == submission.draft_id,
                    ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
                )
            ).all()
            actual = {
                (item.id, item.storage_key, item.size_bytes, item.sha256, item.processing_role)
                for item in sources
            }
            expected = {
                (
                    item.draft_file_id,
                    item.storage_key,
                    item.size_bytes,
                    item.sha256,
                    item.processing_role,
                )
                for item in snapshot.original_files
            }
            if actual != expected:
                raise ApiError(
                    "REIMBURSEMENT_ATTACHMENT_MANIFEST_CHANGED",
                    "待提交票据与已确认内容不一致，请重新确认",
                    409,
                )
            changed = checkpoint_oa_create(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                oa_request_json=request_json,
                oa_request_hash=request_hash,
                reconciliation_deadline_at=reconciliation_deadline_at,
            )
        return _work_lease(changed, ReimbursementSubmissionStatus.OA_CREATING)

    def record_oa_instance(
        self,
        lease: SubmissionLease,
        *,
        process_instance_id: str,
    ) -> SubmissionLease:
        with self._session_factory() as database:
            changed = record_oa_instance(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                process_instance_id=process_instance_id,
            )
        return _work_lease(changed, ReimbursementSubmissionStatus.VERIFYING)

    def complete_submission(
        self,
        lease: SubmissionLease,
        *,
        process_instance_id: str,
        business_id: str,
        approval_url: str | None,
        now: datetime,
    ) -> None:
        job = self.load_job(lease)
        if job.process_instance_id != process_instance_id:
            raise ReimbursementSubmissionConflict("verified OA identity changed")
        for upload in job.uploads:
            if upload.status is ReimbursementUploadStatus.LINKED:
                continue
            with self._session_factory() as database:
                mark_upload_linked(
                    database,
                    lease=_quota_lease(lease),
                    worker_id=self._worker_id,
                    upload_id=upload.id,
                    expected_status_version=upload.status_version,
                    now=now,
                )
        with self._session_factory() as database:
            mark_submission_submitted(
                database,
                lease=_quota_lease(lease),
                worker_id=self._worker_id,
                business_id=business_id,
                approval_url=approval_url,
                now=now,
            )

    def _release_failed_generated(
        self,
        lease: QuotaSubmissionLease,
        reservation,
    ) -> None:
        if reservation is None:
            return
        try:
            self._quota.release(lease, reservation)
        except Exception as exc:
            logger.warning(
                "Generated workbook reservation cleanup deferred",
                extra={
                    "submission_id": lease.submission_id,
                    "exception_type": type(exc).__name__,
                },
            )


class LinkedLocalFileMaintenance:
    """Release safe local bytes, then CAS all affected accounting rows."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        staging: ReimbursementStaging,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._staging = staging
        self._clock = clock or _utc_now

    async def release_linked_local_files(self, *, limit: int) -> int:
        release_at = self._clock()
        with self._session_factory() as database:
            candidates = list_due_linked_local_release_candidates(
                database,
                limit=limit,
                now=release_at,
            )
        released = 0
        for candidate in candidates:
            try:
                await asyncio.to_thread(
                    self._staging.delete,
                    candidate.local_storage_key,
                    expected_size=candidate.size_bytes,
                    expected_sha256=candidate.sha256,
                    missing_ok=True,
                )
                with self._session_factory() as database:
                    finalize_local_upload_release(
                        database,
                        corp_id=candidate.corp_id,
                        user_id=candidate.user_id,
                        submission_id=candidate.submission_id,
                        upload_id=candidate.upload_id,
                        expected_status_version=candidate.upload_status_version,
                        now=release_at,
                    )
                released += 1
            except ReimbursementSubmissionConflict:
                continue
            except Exception as exc:
                logger.error(
                    "Linked reimbursement local file could not be released",
                    extra={
                        "submission_id": candidate.submission_id,
                        "upload_id": candidate.upload_id,
                        "exception_type": type(exc).__name__,
                    },
                )
        with self._session_factory() as database:
            source_candidates = list_due_bundled_source_release_candidates(database, limit=limit)
        for candidate in source_candidates:
            try:
                await asyncio.to_thread(
                    self._staging.delete,
                    candidate.storage_key,
                    expected_size=candidate.size_bytes,
                    expected_sha256=candidate.sha256,
                    missing_ok=True,
                )
                with self._session_factory() as database:
                    finalize_bundled_source_release(database, candidate=candidate, now=release_at)
                released += 1
            except ReimbursementSubmissionConflict:
                continue
            except Exception as exc:
                logger.error(
                    "Bundled reimbursement source could not be released",
                    extra={
                        "submission_id": candidate.submission_id,
                        "draft_file_id": candidate.file_id,
                        "exception_type": type(exc).__name__,
                    },
                )
        return released


class SubmissionMaintenance(Protocol):
    """Durable work that remains due after a submission becomes terminal."""

    async def release_linked_local_files(self, *, limit: int) -> int: ...


class DurableOAReimbursementWorker:
    """Database-polling worker with no process-local job queue.

    Each call claims at most one durable row. If the process exits, the database
    lease expires and another worker resumes from the last persisted checkpoint.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        state: SubmissionStatePort,
        processor: OAReimbursementProcessor,
        maintenance: SubmissionMaintenance,
        lease_seconds: int,
        poll_interval_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_seconds < 5:
            raise ValueError("lease_seconds must be at least five seconds")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._worker_id = worker_id
        self._state = state
        self._processor = processor
        self._maintenance = maintenance
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock or _utc_now

    async def run_once(self) -> bool:
        try:
            await self._maintenance.release_linked_local_files(limit=20)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # SUBMITTED is never rolled back because local deletion failed.
            # The independent durable scan will see the same row next time.
            logger.error(
                "Linked reimbursement local-file cleanup failed",
                extra={"exception_type": type(exc).__name__},
            )
        lease = await asyncio.to_thread(
            self._state.claim_due,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            now=self._clock(),
        )
        if lease is None:
            return False
        try:
            await self._processor.process(lease)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A processor must persist expected business failures itself. An
            # unexpected bug is intentionally left to lease-expiry recovery.
            logger.error(
                "Reimbursement worker iteration failed",
                extra={
                    "submission_id": lease.submission_id,
                    "exception_type": type(exc).__name__,
                },
            )
        return True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if await self.run_once():
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass


class OAReimbursementProcessor:
    """Resumable OA submission state machine around unsafe remote mutations."""

    def __init__(
        self,
        *,
        state: SubmissionStatePort,
        materializer: SubmissionMaterializer,
        workflow: DingTalkWorkflowClient,
        storage: DingTalkStorageClient,
        lease_seconds: int,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        reconciliation_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds < 5:
            raise ValueError("lease_seconds must be at least five seconds")
        if retry_base_seconds < 1 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry delays are invalid")
        if reconciliation_seconds < 30:
            raise ValueError("reconciliation_seconds must be at least thirty seconds")
        self._state = state
        self._materializer = materializer
        self._workflow = workflow
        self._storage = storage
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._reconciliation_seconds = reconciliation_seconds
        self._clock = clock or _utc_now

    async def process(self, lease: SubmissionLease) -> None:
        job = await self._state_call(self._state.load_job, lease)
        if job.status is ReimbursementSubmissionStatus.QUEUED:
            lease = await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.VALIDATING,
            )
            job = await self._state_call(self._state.load_job, lease)

        if job.status is ReimbursementSubmissionStatus.FAILED_RETRYABLE:
            if lease.status is not ReimbursementSubmissionStatus.FAILED_RETRYABLE:
                raise RuntimeError("lease and persisted submission status diverged")
            resume = _required_resume_status(job)
            lease = await self._transition(lease, to_status=resume)
            job = await self._state_call(self._state.load_job, lease)

        if job.status is ReimbursementSubmissionStatus.VALIDATING:
            await self._run_validation(lease, job)
            return
        if job.status is ReimbursementSubmissionStatus.GENERATING_EXCEL:
            await self._run_excel_generation(lease, job)
            return
        if job.status is ReimbursementSubmissionStatus.UPLOADING:
            await self._run_uploads_and_create(lease, job)
            return
        if job.status is ReimbursementSubmissionStatus.OA_CREATING:
            # A newly claimed OA_CREATING row means the previous owner crossed
            # the create checkpoint and disappeared. Replaying create is unsafe.
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.RECONCILING,
                error_code="OA_CREATE_OUTCOME_UNKNOWN",
                error_message="审批创建结果需要核对",
            )
            return
        if job.status is ReimbursementSubmissionStatus.RECONCILING:
            await self._run_reconciliation(lease, job)
            return
        if job.status is ReimbursementSubmissionStatus.VERIFYING:
            await self._run_verification(lease, job)
            return
        if job.status is ReimbursementSubmissionStatus.ORPHAN_CLEANUP:
            await self._run_orphan_cleanup(lease, job)

    async def _run_validation(self, lease: SubmissionLease, job: SubmissionJob) -> None:
        lease = await self._renew(lease)

        async def heartbeat() -> None:
            nonlocal lease
            lease = await self._renew(lease)

        try:
            await self._materializer.validate(job, heartbeat=heartbeat)
        except ApiError as exc:
            if _is_retryable_validation_error(exc):
                await self._schedule_retry(
                    lease,
                    resume_status=ReimbursementSubmissionStatus.VALIDATING,
                    code=exc.code,
                    message="正在重新核对钉钉审批信息",
                    attempt_count=job.attempt_count,
                )
                return
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.FAILED_FINAL,
                error_code=exc.code,
                error_message=exc.message,
            )
            return
        lease = await self._transition(
            lease,
            to_status=ReimbursementSubmissionStatus.GENERATING_EXCEL,
        )
        generated_job = await self._state_call(self._state.load_job, lease)
        await self._run_excel_generation(lease, generated_job)

    async def _run_excel_generation(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
    ) -> None:
        try:
            lease = await self._renew(lease)
            lease = await self._state_call(
                self._state.ensure_generated_excel,
                lease,
                snapshot_json=job.snapshot_json,
                snapshot_sha256=job.snapshot_sha256,
            )
        except ApiError as exc:
            await self._handle_pre_create_failure(
                lease,
                resume_status=ReimbursementSubmissionStatus.GENERATING_EXCEL,
                error=exc,
                attempt_count=job.attempt_count,
            )
            return
        lease = await self._transition(
            lease,
            to_status=ReimbursementSubmissionStatus.UPLOADING,
        )
        uploading_job = await self._state_call(self._state.load_job, lease)
        await self._run_uploads_and_create(lease, uploading_job)

    async def _run_uploads_and_create(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
    ) -> None:
        try:
            lease = await self._renew(lease)
            space = await self._storage.get_approval_space(job.originator_user_id)
            job = await self._state_call(self._state.load_job, lease)
            for upload in job.uploads:
                if upload.status is ReimbursementUploadStatus.COMMITTED:
                    outcome = await self._storage.probe_attachment(
                        union_id=job.originator_union_id,
                        expected=upload.as_attachment(),
                    )
                    if outcome is AttachmentProbe.EXACT:
                        continue
                    if outcome is AttachmentProbe.MISMATCH:
                        await self._transition(
                            lease,
                            to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                            error_code="DINGTALK_ATTACHMENT_MISMATCH",
                            error_message="已上传附件与锁定快照不一致",
                        )
                        return
                    await self._transition(
                        lease,
                        to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                        error_code="DINGTALK_COMMITTED_FILE_MISSING",
                        error_message="已提交附件在钉盘中不存在，需要人工核对",
                    )
                    return
                if upload.status is ReimbursementUploadStatus.COMMIT_UNCERTAIN:
                    await self._transition(
                        lease,
                        to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                        error_code="DINGTALK_STORAGE_COMMIT_UNKNOWN",
                        error_message="附件提交结果需要人工核对",
                    )
                    return
                lease = await self._upload_one(lease, job, upload, space)
        except _UploadCommitUncertain:
            return
        except _RetryableUploadFailure as failure:
            # ``_upload_one`` renews the submission lease around each remote
            # mutation.  When it fails, the caller's pre-call lease is stale;
            # retry scheduling must use the latest durable lease carried out
            # with the failure.
            await self._handle_pre_create_failure(
                failure.lease,
                resume_status=ReimbursementSubmissionStatus.UPLOADING,
                error=failure.error,
                attempt_count=job.attempt_count,
            )
            return
        except ApiError as exc:
            await self._handle_pre_create_failure(
                lease,
                resume_status=ReimbursementSubmissionStatus.UPLOADING,
                error=exc,
                attempt_count=job.attempt_count,
            )
            return

        try:
            job = await self._state_call(self._state.load_job, lease)
            attachments = tuple(upload.as_attachment() for upload in job.uploads)
            command = self._materializer.build_create_command(job, attachments)
            request_json = self._materializer.serialize_create_command(command)
            request_hash = self._materializer.create_command_hash(command)
        except ApiError as exc:
            await self._handle_pre_create_failure(
                lease,
                resume_status=ReimbursementSubmissionStatus.UPLOADING,
                error=exc,
                attempt_count=job.attempt_count,
            )
            return
        deadline = self._clock() + timedelta(seconds=self._reconciliation_seconds)
        lease = await self._state_call(
            self._state.checkpoint_oa_create,
            lease,
            request_json=request_json,
            request_hash=request_hash,
            reconciliation_deadline_at=deadline,
        )
        await self._create_once(lease, command)

    async def _upload_one(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
        upload: SubmissionUpload,
        space: ApprovalSpace,
    ) -> SubmissionLease:
        content = await asyncio.to_thread(self._materializer.read_upload, job, upload)
        approval_file = upload.as_approval_file(content)
        prepared = await self._storage.prepare_upload(
            space=space,
            union_id=job.originator_union_id,
            file=approval_file,
        )
        lease = await self._renew(lease)
        upload = await self._state_call(self._state.begin_upload_put, lease, upload)
        try:
            await self._storage.put_prepared_upload(prepared=prepared)
        except asyncio.CancelledError:
            await self._persist_cancellation(
                self._state.mark_upload_retryable,
                lease,
                upload,
                error_code="DINGTALK_STORAGE_PUT_INTERRUPTED",
            )
            raise
        except ApiError as exc:
            await self._state_call(
                self._state.mark_upload_retryable,
                lease,
                upload,
                error_code=exc.code,
            )
            raise _RetryableUploadFailure(lease=lease, error=exc) from None
        upload = await self._state_call(self._state.record_upload_put_done, lease, upload)
        lease = await self._renew(lease)
        upload = await self._state_call(self._state.begin_upload_commit, lease, upload)
        try:
            attachment = await self._storage.commit_prepared_upload(prepared=prepared)
            _validate_committed_attachment(approval_file, attachment)
        except asyncio.CancelledError:
            await self._persist_cancellation(
                self._state.mark_upload_commit_uncertain,
                lease,
                upload,
                error_code="DINGTALK_STORAGE_COMMIT_INTERRUPTED",
            )
            raise
        except DingTalkStorageCommitOutcomeUnknown as exc:
            await self._state_call(
                self._state.mark_upload_commit_uncertain,
                lease,
                upload,
                error_code=exc.code,
            )
            raise _UploadCommitUncertain from None
        except ApiError as exc:
            # A definitive commit rejection created no dentry. A fresh signed
            # ticket and PUT may be acquired safely on the next attempt.
            await self._state_call(
                self._state.mark_upload_retryable,
                lease,
                upload,
                error_code=exc.code,
            )
            raise _RetryableUploadFailure(lease=lease, error=exc) from None
        await self._state_call(
            self._state.record_upload_committed,
            lease,
            upload,
            attachment,
        )
        return lease

    async def _create_once(
        self,
        lease: SubmissionLease,
        command: CreateProcessInstanceCommand,
    ) -> None:
        lease = await self._renew(lease)
        try:
            created = await self._workflow.create_process_instance(command)
        except asyncio.CancelledError:
            await self._persist_cancellation(
                self._state.transition,
                lease,
                to_status=ReimbursementSubmissionStatus.RECONCILING,
                error_code="OA_CREATE_INTERRUPTED",
                error_message="审批创建结果需要核对",
            )
            raise
        except DingTalkProcessInstanceCreateOutcomeUnknown as exc:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.RECONCILING,
                error_code=exc.code,
                error_message="审批创建结果需要核对",
            )
            return
        except DingTalkProcessInstanceCreateRejected as exc:
            lease = await self._state_call(
                self._state.confirm_orphan_cleanup,
                lease,
                confirmation_code=exc.code,
                now=self._clock(),
            )
            job = await self._state_call(self._state.load_job, lease)
            await self._run_orphan_cleanup(lease, job)
            return
        except ApiError as exc:
            # Permission/authentication rejection is returned without an OA ID
            # and is safe for orphan cleanup; no automatic create replay occurs.
            lease = await self._state_call(
                self._state.confirm_orphan_cleanup,
                lease,
                confirmation_code=exc.code,
                now=self._clock(),
            )
            job = await self._state_call(self._state.load_job, lease)
            await self._run_orphan_cleanup(lease, job)
            return
        lease = await self._state_call(
            self._state.record_oa_instance,
            lease,
            process_instance_id=created.instance_id,
        )
        job = await self._state_call(self._state.load_job, lease)
        await self._run_verification(lease, job)

    async def _run_reconciliation(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
    ) -> None:
        if job.oa_create_started_at is None or job.reconciliation_deadline_at is None:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code="OA_RECONCILIATION_CHECKPOINT_MISSING",
                error_message="审批创建核对信息不完整",
            )
            return
        deadline = _as_utc(job.reconciliation_deadline_at)

        async def heartbeat() -> None:
            nonlocal lease
            if self._clock() >= deadline:
                raise ApiError(
                    "OA_RECONCILIATION_DEADLINE_REACHED",
                    "审批创建安全核对期已结束",
                    409,
                )
            lease = await self._renew(lease)

        try:
            command = self._checkpointed_command(job)
        except ApiError as exc:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code=exc.code,
                error_message="审批创建检查点损坏，需要人工核对",
            )
            return
        try:
            candidates = await self._matching_candidates(
                job,
                command,
                heartbeat=heartbeat,
            )
        except ApiError as exc:
            await self._reschedule_reconciliation(lease, job, exc.code)
            return

        if len(candidates) == 1:
            lease = await self._state_call(
                self._state.record_oa_instance,
                lease,
                process_instance_id=candidates[0].instance_id,
            )
            resolved = await self._state_call(self._state.load_job, lease)
            await self._verify_loaded_instance(lease, resolved, candidates[0], command)
            return
        if len(candidates) > 1:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code="OA_RECONCILIATION_AMBIGUOUS",
                error_message="发现多张可能匹配的审批，需要人工核对",
                increment_reconciliation=True,
            )
            return
        await self._reschedule_reconciliation(lease, job, "OA_RECONCILIATION_NOT_FOUND")

    async def _matching_candidates(
        self,
        job: SubmissionJob,
        command: CreateProcessInstanceCommand,
        *,
        heartbeat: WorkHeartbeat,
    ) -> tuple[WorkflowProcessInstance, ...]:
        started = _as_utc(job.oa_create_started_at)
        query_start = started - _RECONCILIATION_CLOCK_SKEW
        query_end = min(
            self._clock() + _RECONCILIATION_CLOCK_SKEW,
            query_start + timedelta(days=120),
        )
        next_token = 0
        matches: list[WorkflowProcessInstance] = []
        seen: set[str] = set()
        for _ in range(_MAX_RECONCILIATION_PAGES):
            page = await _call_with_heartbeat(
                heartbeat,
                lambda token=next_token: self._workflow.list_process_instance_ids(
                    process_code=job.process_code,
                    start_time=_epoch_millis(query_start),
                    end_time=_epoch_millis(query_end),
                    next_token=token,
                    max_results=_RECONCILIATION_PAGE_SIZE,
                    user_ids=(job.originator_user_id,),
                    statuses=("RUNNING", "TERMINATED", "COMPLETED"),
                ),
            )
            for instance_id in page.instance_ids:
                if instance_id in seen:
                    continue
                seen.add(instance_id)
                instance = await _call_with_heartbeat(
                    heartbeat,
                    lambda candidate_id=instance_id: self._workflow.get_process_instance(
                        candidate_id
                    ),
                )
                if self._materializer.matches_instance(command, instance):
                    matches.append(instance)
                    if len(matches) > 1:
                        return tuple(matches)
            if page.next_token is None:
                return tuple(matches)
            next_token = page.next_token
        raise ApiError(
            "OA_RECONCILIATION_TOO_MANY_RESULTS",
            "待核对审批数量超过安全上限",
            502,
        )

    async def _reschedule_reconciliation(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
        error_code: str,
    ) -> None:
        deadline = job.reconciliation_deadline_at
        if deadline is None or self._clock() >= _as_utc(deadline):
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code=error_code,
                error_message="在安全核对期内未找到唯一审批，请人工处理",
                increment_reconciliation=True,
            )
            return
        delay = _bounded_retry_delay(
            job.attempt_count,
            base=self._retry_base_seconds,
            maximum=self._retry_max_seconds,
        )
        await self._transition(
            lease,
            to_status=ReimbursementSubmissionStatus.RECONCILING,
            next_attempt_at=min(self._clock() + timedelta(seconds=delay), _as_utc(deadline)),
            error_code=error_code,
            error_message="审批创建结果仍在核对",
            increment_attempt=True,
            increment_reconciliation=True,
        )

    async def _run_verification(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
    ) -> None:
        if not job.process_instance_id:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code="OA_INSTANCE_ID_MISSING",
                error_message="审批编号缺失，需要人工核对",
            )
            return
        try:
            command = self._checkpointed_command(job)
        except ApiError as exc:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code=exc.code,
                error_message="审批创建检查点损坏，需要人工核对",
            )
            return
        try:
            instance = await self._workflow.get_process_instance(job.process_instance_id)
        except ApiError as exc:
            if _is_retryable_remote_error(exc):
                await self._schedule_retry(
                    lease,
                    resume_status=ReimbursementSubmissionStatus.VERIFYING,
                    code=exc.code,
                    message="审批已创建，正在重新核对内容",
                    attempt_count=job.attempt_count,
                )
            else:
                await self._transition(
                    lease,
                    to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                    error_code=exc.code,
                    error_message="审批已创建但无法继续自动回读，需要人工核对",
                )
            return
        await self._verify_loaded_instance(lease, job, instance, command)

    async def _verify_loaded_instance(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
        instance: WorkflowProcessInstance,
        command: CreateProcessInstanceCommand,
    ) -> None:
        if not self._materializer.matches_instance(command, instance):
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code="OA_READBACK_MISMATCH",
                error_message="审批回读内容与提交快照不一致",
            )
            return
        if instance.instance_id != job.process_instance_id:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code="OA_INSTANCE_ID_MISMATCH",
                error_message="审批编号核对不一致",
            )
            return
        await self._state_call(
            self._state.complete_submission,
            lease,
            process_instance_id=instance.instance_id,
            business_id=instance.business_id,
            approval_url=self._materializer.approval_url(instance.instance_id),
            now=self._clock(),
        )

    async def _run_orphan_cleanup(
        self,
        lease: SubmissionLease,
        job: SubmissionJob,
        *,
        final_error_code: str = "OA_CREATE_REJECTED",
        final_error_message: str = "审批未发起，已清理未关联附件",
    ) -> None:
        if job.process_instance_id is not None:
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code="ORPHAN_CLEANUP_HAS_OA",
                error_message="审批已经存在，禁止清理附件",
            )
            return
        for upload in job.uploads:
            if upload.status in {
                ReimbursementUploadStatus.CLEANED,
                ReimbursementUploadStatus.DISCARDED,
            }:
                continue
            if upload.status not in {
                ReimbursementUploadStatus.COMMITTED,
                ReimbursementUploadStatus.CLEANUP_PENDING,
            }:
                continue
            if upload.status is ReimbursementUploadStatus.COMMITTED:
                upload = await self._state_call(
                    self._state.begin_upload_cleanup,
                    lease,
                    upload,
                )
            try:
                outcome = await self._storage.recycle_attachment(
                    union_id=job.originator_union_id,
                    attachment=upload.as_attachment(),
                )
            except ApiError as exc:
                if _is_retryable_remote_error(exc):
                    await self._schedule_retry(
                        lease,
                        resume_status=ReimbursementSubmissionStatus.ORPHAN_CLEANUP,
                        code=exc.code,
                        message="孤儿附件清理失败，稍后重试",
                        attempt_count=job.attempt_count,
                    )
                else:
                    await self._transition(
                        lease,
                        to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                        error_code=exc.code,
                        error_message="孤儿附件无法继续自动清理，需要人工核对",
                    )
                return
            if outcome is AttachmentRecycleOutcome.RETRY_LATER:
                await self._schedule_retry(
                    lease,
                    resume_status=ReimbursementSubmissionStatus.ORPHAN_CLEANUP,
                    code="DINGTALK_STORAGE_CLEANUP_PENDING",
                    message="孤儿附件仍在清理，稍后重试",
                    attempt_count=job.attempt_count,
                )
                return
            await self._state_call(
                self._state.record_upload_cleaned,
                lease,
                upload,
                now=self._clock(),
            )
        await self._transition(
            lease,
            to_status=ReimbursementSubmissionStatus.FAILED_FINAL,
            error_code=final_error_code,
            error_message=final_error_message,
        )

    def _checkpointed_command(self, job: SubmissionJob) -> CreateProcessInstanceCommand:
        if not job.oa_request_json or not job.oa_request_hash:
            raise ApiError(
                "OA_CREATE_CHECKPOINT_MISSING",
                "审批创建快照缺失，需要人工核对",
                409,
            )
        return self._materializer.parse_create_command(
            job.oa_request_json,
            expected_sha256=job.oa_request_hash,
        )

    async def _handle_pre_create_failure(
        self,
        lease: SubmissionLease,
        *,
        resume_status: ReimbursementSubmissionStatus,
        error: ApiError,
        attempt_count: int,
    ) -> None:
        """Classify a pre-create failure from durable side effects, then converge."""

        if _is_retryable_pre_create_error(error):
            await self._schedule_retry(
                lease,
                resume_status=resume_status,
                code=error.code,
                message="报销提交暂时失败，正在稍后重试",
                attempt_count=attempt_count,
            )
            return

        job = await self._state_call(self._state.load_job, lease)
        uncertain_statuses = {
            ReimbursementUploadStatus.COMMITTING,
            ReimbursementUploadStatus.COMMIT_UNCERTAIN,
            ReimbursementUploadStatus.LINKED,
        }
        has_oa_checkpoint = bool(
            job.process_instance_id
            or job.oa_create_started_at
            or job.oa_request_json
            or job.oa_request_hash
        )
        if has_oa_checkpoint or any(upload.status in uncertain_statuses for upload in job.uploads):
            await self._transition(
                lease,
                to_status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
                error_code=error.code,
                error_message="提交失败且远端结果无法安全确认，需要人工核对",
            )
            return

        if any(
            upload.status
            in {
                ReimbursementUploadStatus.COMMITTED,
                ReimbursementUploadStatus.CLEANUP_PENDING,
            }
            for upload in job.uploads
        ):
            lease = await self._state_call(
                self._state.confirm_orphan_cleanup,
                lease,
                confirmation_code=error.code,
                now=self._clock(),
            )
            cleanup_job = await self._state_call(self._state.load_job, lease)
            await self._run_orphan_cleanup(
                lease,
                cleanup_job,
                final_error_code=error.code,
                final_error_message=error.message,
            )
            return

        await self._transition(
            lease,
            to_status=ReimbursementSubmissionStatus.FAILED_FINAL,
            error_code=error.code,
            error_message=error.message,
        )

    async def _schedule_retry(
        self,
        lease: SubmissionLease,
        *,
        resume_status: ReimbursementSubmissionStatus,
        code: str,
        message: str,
        attempt_count: int,
    ) -> None:
        delay = _bounded_retry_delay(
            attempt_count,
            base=self._retry_base_seconds,
            maximum=self._retry_max_seconds,
        )
        await self._transition(
            lease,
            to_status=ReimbursementSubmissionStatus.FAILED_RETRYABLE,
            resume_status=resume_status,
            next_attempt_at=self._clock() + timedelta(seconds=delay),
            error_code=code,
            error_message=message,
            increment_attempt=True,
        )

    async def _renew(self, lease: SubmissionLease) -> SubmissionLease:
        return await self._state_call(
            self._state.renew,
            lease,
            lease_seconds=self._lease_seconds,
            now=self._clock(),
        )

    async def _transition(
        self,
        lease: SubmissionLease,
        *,
        to_status: ReimbursementSubmissionStatus,
        next_attempt_at: datetime | None = None,
        resume_status: ReimbursementSubmissionStatus | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
        increment_reconciliation: bool = False,
    ) -> SubmissionLease:
        return await self._state_call(
            self._state.transition,
            lease,
            to_status=to_status,
            next_attempt_at=next_attempt_at,
            resume_status=resume_status,
            error_code=error_code,
            error_message=error_message,
            increment_attempt=increment_attempt,
            increment_reconciliation=increment_reconciliation,
        )

    @staticmethod
    async def _state_call(function: Callable[..., _T], *args: object, **kwargs: object) -> _T:
        return await asyncio.to_thread(function, *args, **kwargs)

    @staticmethod
    async def _persist_cancellation(
        function: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task


class _UploadCommitUncertain(RuntimeError):
    pass


class _RetryableUploadFailure(RuntimeError):
    """Retryable remote failure paired with the latest renewed DB lease."""

    def __init__(self, *, lease: SubmissionLease, error: ApiError) -> None:
        super().__init__(error.code)
        self.lease = lease
        self.error = error


def _validate_committed_attachment(
    source: ApprovalFile,
    committed: ApprovalAttachment,
) -> None:
    """Treat malformed commit metadata as an unknowable remote mutation."""

    name = committed.file_name
    file_type = committed.file_type
    normalized_type = file_type.strip().lower().lstrip(".") if isinstance(file_type, str) else ""
    separator = name.rfind(".") if isinstance(name, str) else -1
    name_type = name[separator + 1 :].lower() if separator > 0 else ""
    source_type = source.file_type.lower()
    type_matches_source = normalized_type == source_type or {
        normalized_type,
        source_type,
    } <= {"jpeg", "jpg"}
    name_matches_type = name_type == normalized_type or {
        name_type,
        normalized_type,
    } <= {"jpeg", "jpg"}
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > 255
        or not normalized_type
        or len(normalized_type) > 32
        or isinstance(committed.file_size, bool)
        or committed.file_size != source.size
        or not type_matches_source
        or not name_matches_type
    ):
        raise DingTalkStorageCommitOutcomeUnknown(
            http_status=200,
            possible_space_id=(committed.space_id if isinstance(committed.space_id, str) else None),
            possible_file_id=(committed.file_id if isinstance(committed.file_id, str) else None),
        )


def strict_instance_matches_command(
    command: CreateProcessInstanceCommand,
    instance: WorkflowProcessInstance,
) -> bool:
    """Fail closed unless every submitted form value is present and exact."""

    if instance.originator_user_id != command.originator_user_id:
        return False
    if instance.originator_department_id != str(command.department_id):
        return False

    by_id = {
        value.component_id: value
        for value in instance.form_values
        if value.component_id is not None
    }
    by_name: dict[str, list[WorkflowFormValue]] = {}
    for value in instance.form_values:
        by_name.setdefault(value.name, []).append(value)
    for expected in command.form_values:
        if expected.component_id is not None:
            actual = by_id.get(expected.component_id)
        else:
            named = by_name.get(expected.name, [])
            actual = named[0] if len(named) == 1 else None
        if actual is None:
            return False
        if actual.name != expected.name or not _form_value_matches(expected, actual):
            return False
        if expected.component_type is not None and actual.component_type != expected.component_type:
            return False
        if expected.biz_alias is not None and actual.biz_alias != expected.biz_alias:
            return False
    return True


def _form_value_matches(
    expected: CreateWorkflowFormValue,
    actual: WorkflowFormValue,
) -> bool:
    """Compare structured controls semantically and plain controls byte-for-byte."""

    if expected.component_type == "RelateField":
        return _relate_field_matches(expected.value, actual)
    if expected.component_type == "DDDateRangeField":
        from app.services.oa_date_range import parse_date_range

        dates = parse_date_range(expected.value)
        return dates is not None and dates == parse_date_range(actual.value)
    if expected.component_type not in _JSON_FORM_COMPONENT_TYPES:
        return actual.value == expected.value
    if actual.value is None:
        return False
    try:
        return _strict_json_value(actual.value) == _strict_json_value(expected.value)
    except ValueError:
        return False


def _relate_field_matches(expected_raw: str, actual: WorkflowFormValue) -> bool:
    """Accept DingTalk's title display value only when extValue proves the exact IDs."""

    try:
        expected = _strict_json_value(expected_raw)
        if (
            not isinstance(expected, list)
            or any(not isinstance(item, str) or not item for item in expected)
            or len(expected) != len(set(expected))
        ):
            return False
        if actual.value is not None and _strict_json_value(actual.value) == expected:
            return True
        if actual.ext_value is None:
            return False
        ext_value = _strict_json_value(actual.ext_value)
        if not isinstance(ext_value, dict) or set(ext_value) != {"list"}:
            return False
        entries = ext_value["list"]
        if not isinstance(entries, list):
            return False
        actual_ids = []
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            instance_id = entry.get("procInstId")
            if not isinstance(instance_id, str) or not instance_id:
                return False
            actual_ids.append(instance_id)
        return actual_ids == expected
    except ValueError:
        return False


def _strict_json_value(raw: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        return json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError):
        raise ValueError("invalid JSON form value") from None


def _is_retryable_validation_error(error: ApiError) -> bool:
    if not isinstance(error, DingTalkOpenAPIError):
        return False
    if error.code == "DINGTALK_PERMISSION_MISSING":
        return False
    return (
        error.http_status is None
        or error.http_status in {408, 425, 429}
        or error.http_status >= 500
    )


def _is_retryable_pre_create_error(error: ApiError) -> bool:
    """Allow retries only for explicitly transient local or remote failures."""

    if error.code in {
        "REIMBURSEMENT_EXCEL_GENERATION_PENDING",
        "REIMBURSEMENT_STAGING_UNAVAILABLE",
    }:
        return True
    return _is_retryable_remote_error(error)


def _is_retryable_remote_error(error: ApiError) -> bool:
    if not isinstance(
        error,
        (
            DingTalkOpenAPIError,
            DingTalkStorageRecycleOutcomeUnknown,
            DingTalkStorageUploadError,
        ),
    ):
        return False
    if error.code == "DINGTALK_PERMISSION_MISSING":
        return False
    return (
        error.http_status is None
        or error.http_status in {408, 425, 429}
        or error.http_status >= 500
    )


def _validate_job_identity(
    job: SubmissionJob,
    snapshot: ReimbursementSnapshot,
) -> None:
    if (
        snapshot.draft_id != job.draft_id
        or snapshot.template.process_code != job.process_code
        or snapshot.identity.user_id != job.originator_user_id
        or snapshot.identity.union_id != job.originator_union_id
        or snapshot.identity.department_id != str(job.department_id)
    ):
        raise ApiError(
            "REIMBURSEMENT_SNAPSHOT_IDENTITY_MISMATCH",
            "报销提交身份与锁定快照不一致",
            409,
        )


def _validate_original_manifest(
    _snapshot: ReimbursementSnapshot,
    uploads: Sequence[SubmissionUpload],
) -> None:
    if any(item.role == ReimbursementUploadRole.ORIGINAL.value for item in uploads):
        raise ApiError(
            "REIMBURSEMENT_ATTACHMENT_MANIFEST_CHANGED",
            "票据汇总提交不应包含单独原始附件",
            409,
        )


def _quota_lease(lease: SubmissionLease) -> QuotaSubmissionLease:
    return QuotaSubmissionLease(
        corp_id=lease.corp_id,
        user_id=lease.user_id,
        submission_id=lease.submission_id,
        lease_token=lease.token,
        expected_status_version=lease.status_version,
    )


def _work_lease(
    lease: QuotaSubmissionLease,
    status: ReimbursementSubmissionStatus,
) -> SubmissionLease:
    return SubmissionLease(
        submission_id=lease.submission_id,
        corp_id=lease.corp_id,
        user_id=lease.user_id,
        token=lease.lease_token,
        status_version=lease.expected_status_version,
        status=status,
    )


def replace_lease_terminal(
    lease: SubmissionLease,
    status: ReimbursementSubmissionStatus,
    status_version: int,
) -> SubmissionLease:
    return replace(
        lease,
        status=status,
        status_version=status_version,
    )


def replace_upload_checkpoint(
    upload: SubmissionUpload,
    checkpoint: UploadCheckpoint,
) -> SubmissionUpload:
    if checkpoint.upload_id != upload.id:
        raise ReimbursementSubmissionConflict("upload checkpoint identity changed")
    return replace(
        upload,
        status=ReimbursementUploadStatus(checkpoint.upload_status),
        status_version=checkpoint.status_version,
    )


def _work_upload(upload: ReimbursementUpload) -> SubmissionUpload:
    return SubmissionUpload(
        id=upload.id,
        status_version=upload.status_version,
        status=ReimbursementUploadStatus(upload.upload_status),
        file_name=upload.file_name,
        file_type=upload.file_type,
        media_type=upload.media_type,
        size_bytes=upload.size_bytes,
        sha256=upload.sha256,
        sort_order=upload.sort_order,
        role=upload.role,
        local_storage_key=upload.local_storage_key,
        source_draft_file_id=upload.source_draft_file_id,
        space_id=upload.space_id,
        file_id=upload.file_id,
    )


def _required_resume_status(job: SubmissionJob) -> ReimbursementSubmissionStatus:
    # A FAILED_RETRYABLE row's resume target belongs to the state record. Ports
    # may expose it through an implementation-specific attribute without adding
    # it to older persisted snapshots.
    raw = job.resume_status
    if isinstance(raw, ReimbursementSubmissionStatus):
        return raw
    if isinstance(raw, str):
        try:
            return ReimbursementSubmissionStatus(raw)
        except ValueError:
            pass
    raise RuntimeError("retryable submission has no valid resume status")


def _bounded_retry_delay(attempt_count: int, *, base: int, maximum: int) -> int:
    exponent = max(0, min(int(attempt_count), 20))
    return min(maximum, base * (2**exponent))


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _epoch_millis(value: datetime) -> int:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(aware.timestamp() * 1000)
