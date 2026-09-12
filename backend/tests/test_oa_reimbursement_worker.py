from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIError
from app.integrations.dingtalk.storage import (
    ApprovalAttachment,
    ApprovalSpace,
    AttachmentProbe,
    AttachmentRecycleOutcome,
    DingTalkStorageCommitOutcomeUnknown,
)
from app.integrations.dingtalk.workflow import (
    CreatedProcessInstance,
    CreateProcessInstanceCommand,
    CreateWorkflowFormValue,
    DingTalkProcessInstanceCreateOutcomeUnknown,
    WorkflowFormValue,
    WorkflowInstanceIdPage,
    WorkflowProcessInstance,
)
from app.models.reimbursement import (
    ReimbursementSubmissionStatus,
    ReimbursementUploadStatus,
)
from app.services.oa_reimbursement import (
    DurableOAReimbursementWorker,
    OAReimbursementProcessor,
    SubmissionJob,
    SubmissionLease,
    SubmissionUpload,
    strict_instance_matches_command,
)
from app.services.reimbursement_submissions import ReimbursementSubmissionConflict

NOW = datetime(2026, 9, 4, 8, 0, 0)
CONTENT = b"locked attachment bytes"


def _command() -> CreateProcessInstanceCommand:
    return CreateProcessInstanceCommand(
        process_code="PROC-TEST",
        originator_user_id="user-1",
        department_id=42,
        microapp_agent_id=123,
        form_values=(
            CreateWorkflowFormValue(
                component_id="TextField_1",
                name="说明",
                component_type="TextField",
                biz_alias="description",
                value="出差报销",
            ),
        ),
    )


def _instance(instance_id: str = "instance-1") -> WorkflowProcessInstance:
    return WorkflowProcessInstance(
        instance_id=instance_id,
        title="差旅费报销",
        business_id="202609040001",
        originator_user_id="user-1",
        originator_department_id="42",
        status="RUNNING",
        result=None,
        created_at="2026-09-04T08:00:01Z",
        finished_at=None,
        form_values=(
            WorkflowFormValue(
                component_id="TextField_1",
                name="说明",
                component_type="TextField",
                value="出差报销",
                ext_value=None,
                biz_alias="description",
            ),
        ),
    )


def _upload(
    status: ReimbursementUploadStatus = ReimbursementUploadStatus.PENDING,
) -> SubmissionUpload:
    return SubmissionUpload(
        id="upload-1",
        status_version=1,
        status=status,
        file_name="发票.pdf",
        file_type="pdf",
        media_type="application/pdf",
        size_bytes=len(CONTENT),
        sha256=hashlib.sha256(CONTENT).hexdigest(),
        space_id="space-1" if status is ReimbursementUploadStatus.COMMITTED else None,
        file_id="file-1" if status is ReimbursementUploadStatus.COMMITTED else None,
    )


def _job(
    status: ReimbursementSubmissionStatus,
    *,
    upload: SubmissionUpload | None = None,
    process_instance_id: str | None = None,
    checkpointed: bool = False,
    reconciliation_deadline_at: datetime | None = None,
) -> SubmissionJob:
    command_json = json.dumps({"command": "locked"}, separators=(",", ":"))
    return SubmissionJob(
        id="submission-1",
        draft_id="draft-1",
        status=status,
        resume_status=None,
        process_code="PROC-TEST",
        originator_user_id="user-1",
        originator_union_id="union-1",
        department_id=42,
        snapshot_json='{"snapshot":1}',
        snapshot_sha256="a" * 64,
        oa_request_json=command_json if checkpointed else None,
        oa_request_hash=(
            hashlib.sha256(command_json.encode()).hexdigest() if checkpointed else None
        ),
        created_at=NOW,
        oa_create_started_at=NOW if checkpointed else None,
        reconciliation_deadline_at=(
            reconciliation_deadline_at or NOW + timedelta(minutes=15) if checkpointed else None
        ),
        process_instance_id=process_instance_id,
        attempt_count=0,
        uploads=((upload or _upload()),),
    )


def _lease(status: ReimbursementSubmissionStatus) -> SubmissionLease:
    return SubmissionLease(
        submission_id="submission-1",
        corp_id="corp-1",
        user_id="user-1",
        token="lease-token",
        status_version=1,
        status=status,
    )


class FakeState:
    def __init__(self, job: SubmissionJob) -> None:
        self.job = job
        self.events: list[str] = []
        self.claims: list[SubmissionLease] = []

    def claim_due(self, **_kwargs):
        return self.claims.pop(0) if self.claims else None

    def renew(self, lease, **_kwargs):
        self.events.append("renew")
        return lease

    def load_job(self, _lease):
        return self.job

    def transition(self, lease, *, to_status, resume_status=None, **_kwargs):
        self.events.append(f"status:{to_status.value}")
        self.job = replace(self.job, status=to_status, resume_status=resume_status)
        return replace(
            lease,
            status=to_status,
            status_version=lease.status_version + 1,
        )

    def ensure_generated_excel(self, lease, **_kwargs):
        self.events.append("excel")
        return lease

    def begin_upload_put(self, _lease, upload):
        self.events.append("put:checkpoint")
        changed = replace(
            upload,
            status=ReimbursementUploadStatus.PUTTING,
            status_version=upload.status_version + 1,
        )
        self._replace_upload(changed)
        return changed

    def record_upload_put_done(self, _lease, upload):
        self.events.append("put:done")
        changed = replace(
            upload,
            status=ReimbursementUploadStatus.PUT_DONE,
            status_version=upload.status_version + 1,
        )
        self._replace_upload(changed)
        return changed

    def begin_upload_commit(self, _lease, upload):
        self.events.append("commit:checkpoint")
        changed = replace(
            upload,
            status=ReimbursementUploadStatus.COMMITTING,
            status_version=upload.status_version + 1,
        )
        self._replace_upload(changed)
        return changed

    def record_upload_committed(self, _lease, upload, attachment):
        self.events.append("commit:done")
        changed = replace(
            upload,
            status=ReimbursementUploadStatus.COMMITTED,
            status_version=upload.status_version + 1,
            space_id=attachment.space_id,
            file_id=attachment.file_id,
            file_name=attachment.file_name,
            file_type=attachment.file_type,
            size_bytes=attachment.file_size,
        )
        self._replace_upload(changed)
        return changed

    def mark_upload_retryable(self, _lease, upload, **_kwargs):
        self.events.append("put:retryable")
        self._replace_upload(replace(upload, status=ReimbursementUploadStatus.PENDING))

    def mark_upload_commit_uncertain(self, _lease, upload, **_kwargs):
        self.events.append("commit:uncertain")
        self._replace_upload(replace(upload, status=ReimbursementUploadStatus.COMMIT_UNCERTAIN))
        self.job = replace(self.job, status=ReimbursementSubmissionStatus.MANUAL_REVIEW)

    def mark_committed_upload_missing(self, _lease, upload):
        self.events.append("committed:missing")
        self._replace_upload(
            replace(
                upload,
                status=ReimbursementUploadStatus.PENDING,
                space_id=None,
                file_id=None,
            )
        )

    def checkpoint_oa_create(
        self,
        lease,
        *,
        request_json,
        request_hash,
        reconciliation_deadline_at,
    ):
        self.events.append("oa:checkpoint")
        self.job = replace(
            self.job,
            status=ReimbursementSubmissionStatus.OA_CREATING,
            oa_request_json=request_json,
            oa_request_hash=request_hash,
            oa_create_started_at=NOW,
            reconciliation_deadline_at=reconciliation_deadline_at,
        )
        return replace(lease, status=ReimbursementSubmissionStatus.OA_CREATING)

    def record_oa_instance(self, lease, *, process_instance_id):
        self.events.append(f"oa:instance:{process_instance_id}")
        self.job = replace(
            self.job,
            status=ReimbursementSubmissionStatus.VERIFYING,
            process_instance_id=process_instance_id,
        )
        return replace(lease, status=ReimbursementSubmissionStatus.VERIFYING)

    def complete_submission(self, _lease, *, process_instance_id, **_kwargs):
        self.events.append(f"submitted:{process_instance_id}")
        self.job = replace(self.job, status=ReimbursementSubmissionStatus.SUBMITTED)

    def confirm_orphan_cleanup(self, lease, *, confirmation_code, **_kwargs):
        self.events.append(f"orphan:{confirmation_code}")
        self.job = replace(self.job, status=ReimbursementSubmissionStatus.ORPHAN_CLEANUP)
        return replace(lease, status=ReimbursementSubmissionStatus.ORPHAN_CLEANUP)

    def begin_upload_cleanup(self, _lease, upload):
        self.events.append("cleanup:checkpoint")
        changed = replace(upload, status=ReimbursementUploadStatus.CLEANUP_PENDING)
        self._replace_upload(changed)
        return changed

    def record_upload_cleaned(self, _lease, upload, **_kwargs):
        self.events.append("cleanup:done")
        self._replace_upload(replace(upload, status=ReimbursementUploadStatus.CLEANED))

    def _replace_upload(self, changed):
        self.job = replace(self.job, uploads=(changed,))


class FakeMaterializer:
    async def validate(self, _job, *, heartbeat):
        del heartbeat
        return None

    def read_upload(self, _job, _upload):
        return CONTENT

    def build_create_command(self, _job, _attachments):
        return _command()

    def serialize_create_command(self, _command):
        return json.dumps({"command": "locked"}, separators=(",", ":"))

    def create_command_hash(self, command):
        return hashlib.sha256(self.serialize_create_command(command).encode()).hexdigest()

    def parse_create_command(self, value, *, expected_sha256):
        assert hashlib.sha256(value.encode()).hexdigest() == expected_sha256
        return _command()

    def matches_instance(self, command, instance):
        return strict_instance_matches_command(command, instance)

    def approval_url(self, process_instance_id):
        return f"dingtalk://approval/{process_instance_id}"


class FailingValidationMaterializer(FakeMaterializer):
    def __init__(self, error):
        self.error = error

    async def validate(self, _job, *, heartbeat):
        del heartbeat
        raise self.error


class HeartbeatBoundaryMaterializer(FakeMaterializer):
    def __init__(self) -> None:
        self.remote_events: list[str] = []

    async def validate(self, _job, *, heartbeat):
        await heartbeat()
        self.remote_events.append("membership-page")
        await heartbeat()
        self.remote_events.append("instance-detail")


class LeaseLosingState(FakeState):
    def __init__(self, job: SubmissionJob, *, fail_renew_at: int) -> None:
        super().__init__(job)
        self.fail_renew_at = fail_renew_at
        self.renew_calls = 0

    def renew(self, lease, **_kwargs):
        self.renew_calls += 1
        if self.renew_calls == self.fail_renew_at:
            raise ReimbursementSubmissionConflict("heartbeat lease CAS failed")
        return replace(lease, status_version=lease.status_version + 1)


class ExpiringLeaseState(FakeState):
    def __init__(self, job: SubmissionJob, *, clock) -> None:
        super().__init__(job)
        self.clock = clock
        self.current_version = 1
        self.expires_at = NOW + timedelta(seconds=5)
        self.renew_calls = 0

    def _require_current(self, lease) -> None:
        if lease.status_version != self.current_version or self.clock() >= self.expires_at:
            raise ReimbursementSubmissionConflict("submission lease is stale or expired")

    def renew(self, lease, *, lease_seconds, now):
        self._require_current(lease)
        assert now == self.clock()
        self.renew_calls += 1
        self.current_version += 1
        self.expires_at = now + timedelta(seconds=lease_seconds)
        return replace(lease, status_version=self.current_version)

    def transition(self, lease, *, to_status, resume_status=None, **_kwargs):
        self._require_current(lease)
        changed = super().transition(
            lease,
            to_status=to_status,
            resume_status=resume_status,
        )
        self.current_version = changed.status_version
        return changed


class CorruptCheckpointMaterializer(FakeMaterializer):
    def parse_create_command(self, _value, *, expected_sha256):
        del expected_sha256
        raise ApiError("OA_CREATE_CHECKPOINT_INVALID", "checkpoint is corrupt", 409)


class FailingReadMaterializer(FakeMaterializer):
    def __init__(self, error: ApiError) -> None:
        self.error = error
        self.read_calls = 0

    def read_upload(self, _job, _upload):
        self.read_calls += 1
        raise self.error


class FailingExcelState(FakeState):
    def __init__(self, job: SubmissionJob, error: ApiError) -> None:
        super().__init__(job)
        self.error = error
        self.excel_calls = 0

    def ensure_generated_excel(self, lease, **_kwargs):
        del lease
        self.excel_calls += 1
        raise self.error


class FakeStorage:
    def __init__(
        self,
        *,
        commit_error=None,
        committed_attachment: ApprovalAttachment | None = None,
        recycle_error: ApiError | None = None,
        probe=AttachmentProbe.EXACT,
    ) -> None:
        self.events: list[str] = []
        self.commit_error = commit_error
        self.committed_attachment = committed_attachment
        self.recycle_error = recycle_error
        self.probe = probe

    async def get_approval_space(self, _user_id):
        self.events.append("space")
        return ApprovalSpace("space-1")

    async def prepare_upload(self, **_kwargs):
        self.events.append("prepare")
        return object()

    async def put_prepared_upload(self, **_kwargs):
        self.events.append("put")

    async def commit_prepared_upload(self, **_kwargs):
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error
        return self.committed_attachment or ApprovalAttachment(
            "space-1",
            "file-1",
            "发票.pdf",
            len(CONTENT),
            "pdf",
        )

    async def probe_attachment(self, **_kwargs):
        self.events.append("probe")
        return self.probe

    async def recycle_attachment(self, **_kwargs):
        self.events.append("recycle")
        if self.recycle_error is not None:
            raise self.recycle_error
        return AttachmentRecycleOutcome.CLEANED


class FakeWorkflow:
    def __init__(self, *, create_error=None, get_error=None, candidates=()) -> None:
        self.create_error = create_error
        self.get_error = get_error
        self.candidates = tuple(candidates)
        self.created = 0
        self.queried = 0

    async def create_process_instance(self, _command):
        self.created += 1
        if self.create_error is not None:
            raise self.create_error
        return CreatedProcessInstance("instance-1")

    async def get_process_instance(self, instance_id):
        if self.get_error is not None:
            raise self.get_error
        for candidate in self.candidates:
            if candidate.instance_id == instance_id:
                return candidate
        return _instance(instance_id)

    async def list_process_instance_ids(self, **_kwargs):
        self.queried += 1
        return WorkflowInstanceIdPage(
            tuple(item.instance_id for item in self.candidates),
            None,
        )


class SlowPagedReconciliationWorkflow(FakeWorkflow):
    def __init__(self, *, advance_clock) -> None:
        super().__init__()
        self.advance_clock = advance_clock
        self.list_calls = 0
        self.detail_calls = 0

    async def list_process_instance_ids(self, *, next_token, **_kwargs):
        self.list_calls += 1
        self.advance_clock(timedelta(seconds=2))
        if next_token == 0:
            return WorkflowInstanceIdPage(("unrelated-1",), 1)
        return WorkflowInstanceIdPage(("unrelated-2",), None)

    async def get_process_instance(self, instance_id):
        self.detail_calls += 1
        self.advance_clock(timedelta(seconds=2))
        return replace(_instance(instance_id), originator_user_id="someone-else")


class FakeMaintenance:
    def __init__(self) -> None:
        self.calls = 0

    async def release_linked_local_files(self, *, limit):
        assert limit == 20
        self.calls += 1
        return 0


def _processor(state, workflow, storage) -> OAReimbursementProcessor:
    return OAReimbursementProcessor(
        state=state,
        materializer=FakeMaterializer(),
        workflow=workflow,
        storage=storage,
        lease_seconds=30,
        clock=lambda: NOW,
    )


async def test_upload_checkpoints_precede_remote_mutations_and_oa_is_verified() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.UPLOADING))
    storage = FakeStorage()
    workflow = FakeWorkflow()

    await _processor(state, workflow, storage).process(
        _lease(ReimbursementSubmissionStatus.UPLOADING)
    )

    assert workflow.created == 1
    assert state.job.status is ReimbursementSubmissionStatus.SUBMITTED
    assert state.events.index("put:checkpoint") < state.events.index("put:done")
    assert state.events.index("commit:checkpoint") < state.events.index("commit:done")
    assert state.events.index("oa:checkpoint") < state.events.index("oa:instance:instance-1")
    assert storage.events == ["space", "prepare", "put", "commit"]


async def test_unknown_create_enters_reconciliation_and_is_never_replayed() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.UPLOADING))
    workflow = FakeWorkflow(
        create_error=DingTalkProcessInstanceCreateOutcomeUnknown(
            http_status=None,
            upstream_code=None,
        )
    )
    processor = _processor(state, workflow, FakeStorage())

    await processor.process(_lease(ReimbursementSubmissionStatus.UPLOADING))
    assert state.job.status is ReimbursementSubmissionStatus.RECONCILING
    assert workflow.created == 1

    # A recovered OA_CREATING checkpoint also goes straight to reconciliation.
    state.job = replace(state.job, status=ReimbursementSubmissionStatus.OA_CREATING)
    await processor.process(_lease(ReimbursementSubmissionStatus.OA_CREATING))
    assert state.job.status is ReimbursementSubmissionStatus.RECONCILING
    assert workflow.created == 1


async def test_unknown_commit_becomes_manual_review_and_is_not_replayed() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.UPLOADING))
    storage = FakeStorage(commit_error=DingTalkStorageCommitOutcomeUnknown(http_status=None))
    workflow = FakeWorkflow()

    await _processor(state, workflow, storage).process(
        _lease(ReimbursementSubmissionStatus.UPLOADING)
    )

    assert workflow.created == 0
    assert storage.events.count("commit") == 1
    assert "commit:uncertain" in state.events
    assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW


@pytest.mark.parametrize(
    "committed_attachment",
    (
        ApprovalAttachment("space-1", "file-1", "发票.pdf", len(CONTENT) + 1, "pdf"),
        ApprovalAttachment("space-1", "file-1", "发票.png", len(CONTENT), "png"),
    ),
    ids=("size-mismatch", "type-mismatch"),
)
async def test_untrusted_commit_metadata_stops_for_manual_review(
    committed_attachment: ApprovalAttachment,
) -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.UPLOADING))
    storage = FakeStorage(committed_attachment=committed_attachment)
    workflow = FakeWorkflow()

    await _processor(state, workflow, storage).process(
        _lease(ReimbursementSubmissionStatus.UPLOADING)
    )

    assert workflow.created == 0
    assert storage.events.count("commit") == 1
    assert state.job.uploads[0].status is ReimbursementUploadStatus.COMMIT_UNCERTAIN
    assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW


async def test_definitive_nontransient_commit_rejection_is_not_retried() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.UPLOADING))
    storage = FakeStorage(
        commit_error=ApiError(
            "DINGTALK_STORAGE_COMMIT_REJECTED",
            "permission was definitively rejected",
            403,
        )
    )
    workflow = FakeWorkflow()

    await _processor(state, workflow, storage).process(
        _lease(ReimbursementSubmissionStatus.UPLOADING)
    )

    assert workflow.created == 0
    assert storage.events.count("commit") == 1
    assert state.job.status is ReimbursementSubmissionStatus.FAILED_FINAL


async def test_transient_staging_failure_keeps_bounded_retry_behavior() -> None:
    state = FailingExcelState(
        _job(ReimbursementSubmissionStatus.GENERATING_EXCEL),
        ApiError(
            "REIMBURSEMENT_STAGING_UNAVAILABLE",
            "staging is temporarily unavailable",
            503,
        ),
    )

    await _processor(state, FakeWorkflow(), FakeStorage()).process(
        _lease(ReimbursementSubmissionStatus.GENERATING_EXCEL)
    )

    assert state.job.status is ReimbursementSubmissionStatus.FAILED_RETRYABLE
    assert state.job.resume_status is ReimbursementSubmissionStatus.GENERATING_EXCEL
    assert state.excel_calls == 1


async def test_permanent_orphan_cleanup_error_requires_manual_review() -> None:
    committed = _upload(ReimbursementUploadStatus.COMMITTED)
    state = FakeState(_job(ReimbursementSubmissionStatus.ORPHAN_CLEANUP, upload=committed))
    storage = FakeStorage(
        recycle_error=ApiError(
            "DINGTALK_PERMISSION_MISSING",
            "cleanup permission is missing",
            403,
        )
    )

    await _processor(state, FakeWorkflow(), storage).process(
        _lease(ReimbursementSubmissionStatus.ORPHAN_CLEANUP)
    )

    assert storage.events == ["recycle"]
    assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW


async def test_permanent_readback_error_requires_manual_review() -> None:
    state = FakeState(
        _job(
            ReimbursementSubmissionStatus.VERIFYING,
            upload=_upload(ReimbursementUploadStatus.COMMITTED),
            process_instance_id="instance-1",
            checkpointed=True,
        )
    )
    workflow = FakeWorkflow(
        get_error=ApiError(
            "DINGTALK_PERMISSION_MISSING",
            "readback permission is missing",
            403,
        )
    )

    await _processor(state, workflow, FakeStorage()).process(
        _lease(ReimbursementSubmissionStatus.VERIFYING)
    )

    assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW


@pytest.mark.parametrize(
    "code",
    (
        "REIMBURSEMENT_SUBMISSION_CORRUPTED",
        "EXCEL_TEMPLATE_CHANGED",
        "REIMBURSEMENT_EXCEL_NAME_MISMATCH",
        "REIMBURSEMENT_EXCEL_TOO_LARGE",
        "REIMBURSEMENT_SNAPSHOT_INVALID",
    ),
)
async def test_permanent_excel_error_fails_once_without_a_second_attempt(code: str) -> None:
    state = FailingExcelState(
        _job(ReimbursementSubmissionStatus.GENERATING_EXCEL),
        ApiError(code, "permanent workbook failure", 409),
    )
    processor = _processor(state, FakeWorkflow(), FakeStorage())

    await processor.process(_lease(ReimbursementSubmissionStatus.GENERATING_EXCEL))

    assert state.job.status is ReimbursementSubmissionStatus.FAILED_FINAL
    assert state.excel_calls == 1


@pytest.mark.parametrize(
    "code",
    (
        "REIMBURSEMENT_LOCAL_FILE_INCOMPLETE",
        "REIMBURSEMENT_LOCAL_FILE_CHANGED",
        "REIMBURSEMENT_LOCAL_FILE_MISSING",
    ),
)
async def test_permanent_locked_file_error_fails_once_without_reupload(code: str) -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.UPLOADING))
    materializer = FailingReadMaterializer(ApiError(code, "locked file failed", 409))
    storage = FakeStorage()
    processor = OAReimbursementProcessor(
        state=state,
        materializer=materializer,
        workflow=FakeWorkflow(),
        storage=storage,
        lease_seconds=30,
        clock=lambda: NOW,
    )

    await processor.process(_lease(ReimbursementSubmissionStatus.UPLOADING))

    assert state.job.status is ReimbursementSubmissionStatus.FAILED_FINAL
    assert materializer.read_calls == 1
    assert storage.events == ["space"]


@pytest.mark.parametrize("candidate_count", [0, 1, 2])
async def test_reconciliation_handles_zero_one_or_many_candidates(candidate_count: int) -> None:
    committed = _upload(ReimbursementUploadStatus.COMMITTED)
    state = FakeState(
        _job(
            ReimbursementSubmissionStatus.RECONCILING,
            upload=committed,
            checkpointed=True,
        )
    )
    candidates = tuple(_instance(f"instance-{index}") for index in range(candidate_count))
    workflow = FakeWorkflow(candidates=candidates)

    await _processor(state, workflow, FakeStorage()).process(
        _lease(ReimbursementSubmissionStatus.RECONCILING)
    )

    if candidate_count == 0:
        assert state.job.status is ReimbursementSubmissionStatus.RECONCILING
    elif candidate_count == 1:
        assert state.job.status is ReimbursementSubmissionStatus.SUBMITTED
    else:
        assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW
    assert workflow.created == 0


async def test_reconciliation_heartbeats_keep_latest_short_lease_across_pages() -> None:
    clock_value = [NOW]

    def advance_clock(delta: timedelta) -> None:
        clock_value[0] += delta

    job = _job(
        ReimbursementSubmissionStatus.RECONCILING,
        upload=_upload(ReimbursementUploadStatus.COMMITTED),
        checkpointed=True,
        reconciliation_deadline_at=NOW + timedelta(seconds=30),
    )
    state = ExpiringLeaseState(job, clock=lambda: clock_value[0])
    workflow = SlowPagedReconciliationWorkflow(advance_clock=advance_clock)
    processor = OAReimbursementProcessor(
        state=state,
        materializer=FakeMaterializer(),
        workflow=workflow,
        storage=FakeStorage(),
        lease_seconds=5,
        clock=lambda: clock_value[0],
    )

    await processor.process(_lease(ReimbursementSubmissionStatus.RECONCILING))

    assert state.job.status is ReimbursementSubmissionStatus.RECONCILING
    assert workflow.list_calls == 2
    assert workflow.detail_calls == 2
    assert state.renew_calls >= 8
    assert workflow.created == 0


async def test_reconciliation_deadline_stops_between_page_and_detail_calls() -> None:
    clock_value = [NOW]

    def advance_clock(delta: timedelta) -> None:
        clock_value[0] += delta

    job = _job(
        ReimbursementSubmissionStatus.RECONCILING,
        upload=_upload(ReimbursementUploadStatus.COMMITTED),
        checkpointed=True,
        reconciliation_deadline_at=NOW + timedelta(seconds=5),
    )
    state = ExpiringLeaseState(job, clock=lambda: clock_value[0])
    workflow = SlowPagedReconciliationWorkflow(advance_clock=advance_clock)
    processor = OAReimbursementProcessor(
        state=state,
        materializer=FakeMaterializer(),
        workflow=workflow,
        storage=FakeStorage(),
        lease_seconds=5,
        clock=lambda: clock_value[0],
    )

    await processor.process(_lease(ReimbursementSubmissionStatus.RECONCILING))

    assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW
    assert workflow.list_calls == 2
    assert workflow.detail_calls == 1
    assert workflow.created == 0


async def test_worker_claims_from_database_and_keeps_one_lease_across_phases() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.VALIDATING))
    state.claims.append(_lease(ReimbursementSubmissionStatus.VALIDATING))
    processor = _processor(state, FakeWorkflow(), FakeStorage())
    worker = DurableOAReimbursementWorker(
        worker_id="worker-1",
        state=state,
        processor=processor,
        maintenance=FakeMaintenance(),
        lease_seconds=30,
        poll_interval_seconds=0.01,
        clock=lambda: NOW,
    )

    assert await worker.run_once() is True
    assert state.job.status is ReimbursementSubmissionStatus.SUBMITTED
    assert state.events[:4] == [
        "renew",
        "status:GENERATING_EXCEL",
        "renew",
        "excel",
    ]
    assert await worker.run_once() is False


def test_readback_matching_is_exact_for_identity_department_and_form_values() -> None:
    command = _command()
    instance = _instance()

    assert strict_instance_matches_command(command, instance)
    assert not strict_instance_matches_command(
        command,
        replace(instance, originator_user_id="someone-else"),
    )
    assert not strict_instance_matches_command(
        command,
        replace(instance, originator_department_id="99"),
    )
    assert not strict_instance_matches_command(
        command,
        replace(
            instance,
            form_values=(replace(instance.form_values[0], value="different"),),
        ),
    )


def test_readback_matching_compares_structured_controls_as_strict_json() -> None:
    command = replace(
        _command(),
        form_values=(
            CreateWorkflowFormValue(
                component_id="DDAttachment_1",
                name="附件",
                component_type="DDAttachment",
                biz_alias="attachment",
                value='[{"fileId":"file-1","spaceId":"space-1"}]',
            ),
        ),
    )
    value = WorkflowFormValue(
        component_id="DDAttachment_1",
        name="附件",
        component_type="DDAttachment",
        value='[ { "spaceId": "space-1", "fileId": "file-1" } ]',
        ext_value=None,
        biz_alias="attachment",
    )
    instance = replace(_instance(), form_values=(value,))

    assert strict_instance_matches_command(command, instance)
    assert not strict_instance_matches_command(
        command,
        replace(
            instance,
            form_values=(
                replace(
                    value,
                    value='[{"spaceId":"space-1","fileId":"file-1","fileId":"other"}]',
                ),
            ),
        ),
    )


def test_readback_matching_accepts_dingtalk_related_titles_only_with_exact_ext_ids() -> None:
    command = replace(
        _command(),
        form_values=(
            CreateWorkflowFormValue(
                component_id="RelateField_1",
                name="关联审批单",
                component_type="RelateField",
                value='["travel-instance-1"]',
            ),
        ),
    )
    value = WorkflowFormValue(
        component_id="RelateField_1",
        name="关联审批单",
        component_type="RelateField",
        value='["员工提交的出差"]',
        ext_value='{"list":[{"procInstId":"travel-instance-1"}]}',
        biz_alias=None,
    )
    instance = replace(_instance(), form_values=(value,))

    assert strict_instance_matches_command(command, instance)
    assert not strict_instance_matches_command(
        command,
        replace(
            instance,
            form_values=(
                replace(
                    value,
                    ext_value='{"list":[{"procInstId":"different-instance"}]}',
                ),
            ),
        ),
    )
    assert not strict_instance_matches_command(
        command,
        replace(instance, form_values=(replace(value, ext_value=None),)),
    )


async def test_transient_validation_failure_is_retried_from_validation_phase() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.VALIDATING))
    processor = OAReimbursementProcessor(
        state=state,
        materializer=FailingValidationMaterializer(DingTalkOpenAPIError(http_status=503)),
        workflow=FakeWorkflow(),
        storage=FakeStorage(),
        lease_seconds=30,
        clock=lambda: NOW,
    )

    await processor.process(_lease(ReimbursementSubmissionStatus.VALIDATING))

    assert state.job.status is ReimbursementSubmissionStatus.FAILED_RETRYABLE
    assert state.job.resume_status is ReimbursementSubmissionStatus.VALIDATING


async def test_invalid_local_validation_fails_instead_of_remaining_stuck() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.VALIDATING))
    processor = OAReimbursementProcessor(
        state=state,
        materializer=FailingValidationMaterializer(ValueError("invalid local query range")),
        workflow=FakeWorkflow(),
        storage=FakeStorage(),
        lease_seconds=30,
        clock=lambda: NOW,
    )

    await processor.process(_lease(ReimbursementSubmissionStatus.VALIDATING))

    assert state.job.status is ReimbursementSubmissionStatus.FAILED_FINAL


async def test_validation_heartbeat_cas_failure_stops_before_next_remote_boundary() -> None:
    state = LeaseLosingState(
        _job(ReimbursementSubmissionStatus.VALIDATING),
        fail_renew_at=3,
    )
    materializer = HeartbeatBoundaryMaterializer()
    processor = OAReimbursementProcessor(
        state=state,
        materializer=materializer,
        workflow=FakeWorkflow(),
        storage=FakeStorage(),
        lease_seconds=30,
        clock=lambda: NOW,
    )

    with pytest.raises(ReimbursementSubmissionConflict, match="heartbeat lease CAS failed"):
        await processor.process(_lease(ReimbursementSubmissionStatus.VALIDATING))

    assert materializer.remote_events == ["membership-page"]
    assert not any(event.startswith("status:") for event in state.events)


@pytest.mark.parametrize(
    "status,process_instance_id",
    [
        (ReimbursementSubmissionStatus.RECONCILING, None),
        (ReimbursementSubmissionStatus.VERIFYING, "instance-1"),
    ],
)
async def test_corrupt_local_oa_checkpoint_stops_for_manual_review(
    status: ReimbursementSubmissionStatus,
    process_instance_id: str | None,
) -> None:
    state = FakeState(
        _job(
            status,
            process_instance_id=process_instance_id,
            checkpointed=True,
        )
    )
    workflow = FakeWorkflow()
    processor = OAReimbursementProcessor(
        state=state,
        materializer=CorruptCheckpointMaterializer(),
        workflow=workflow,
        storage=FakeStorage(),
        lease_seconds=30,
        clock=lambda: NOW,
    )

    await processor.process(_lease(status))

    assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW
    assert workflow.queried == 0


async def test_worker_run_stops_without_busy_waiting() -> None:
    state = FakeState(_job(ReimbursementSubmissionStatus.VALIDATING))
    processor = _processor(state, FakeWorkflow(), FakeStorage())
    worker = DurableOAReimbursementWorker(
        worker_id="worker-1",
        state=state,
        processor=processor,
        maintenance=FakeMaintenance(),
        lease_seconds=30,
        poll_interval_seconds=10,
        clock=lambda: NOW,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
