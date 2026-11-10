import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import mock_login
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_oa_reimbursement_integration import (
    LocalStorageBoundary,
    LocalWorkflowBoundary,
    _persist_ready_draft,
    _schemas,
    _worker,
)

from app.core.errors import ApiError
from app.integrations.dingtalk.storage import DingTalkStorageRecycleOutcomeUnknown
from app.integrations.dingtalk.workflow import DingTalkProcessInstanceCreateRejected
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementSubmission,
    ReimbursementSubmissionRecoveryAudit,
    ReimbursementUpload,
    utc_now,
)

pytest_plugins = ["test_oa_reimbursement_integration"]


class RejectCreate(LocalWorkflowBoundary):
    async def create_process_instance(self, command):
        self.create_calls += 1
        raise DingTalkProcessInstanceCreateRejected(
            http_status=400, upstream_code="invalidParameter"
        )


class FailingRecycle(LocalStorageBoundary):
    def __init__(self, *, temporary):
        super().__init__()
        self.temporary = temporary
        self.fail = True

    async def recycle_attachment(self, *, union_id, attachment):
        if self.fail:
            if self.temporary:
                raise DingTalkStorageRecycleOutcomeUnknown(http_status=503)
            raise ApiError(
                "DINGTALK_PERMISSION_MISSING",
                "合成附件清理失败",
                403,
            )
        return await super().recycle_attachment(union_id=union_id, attachment=attachment)


@pytest.mark.parametrize("temporary", [False, True])
def test_cleanup_errors_resume_without_recreating_oa(enabled_manual_worker_client, temporary):
    client = enabled_manual_worker_client
    client.app.state.settings.admin_user_ids = "mock-user"
    reimbursement, travel, travel_type = _schemas()
    workflow, storage = RejectCreate(reimbursement, travel), FailingRecycle(temporary=temporary)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"], "Idempotency-Key": str(uuid4())}
    draft_id, source_ids = _persist_ready_draft(client, workflow, travel_type)
    submitted = client.post(
        f"/api/oa/reimbursements/{draft_id}/submit", headers=headers, json={"expectedRevision": 4}
    )
    assert submitted.status_code == 202, submitted.text
    submission_id = submitted.json()["data"]["submissionId"]
    clock = [utc_now()]
    worker = _worker(client, workflow, storage, clock=lambda: clock[0])
    assert asyncio.run(worker.run_once())
    with client.app.state.database_session_factory() as database:
        task = database.get(ReimbursementSubmission, submission_id)
        assert task.status == ("FAILED_RETRYABLE" if temporary else "MANUAL_REVIEW")
        assert task.last_error_code and task.last_error_message
        assert task.lease_token is None
        uploads = list(
            database.scalars(
                select(ReimbursementUpload)
                .where(
                    ReimbursementUpload.submission_id == task.id,
                )
                .order_by(ReimbursementUpload.sort_order)
            )
        )
        assert [file.upload_status for file in uploads] == ["CLEANUP_PENDING", "COMMITTED"]
        # Cleanup suspension must never allow attaching an OA or returning to creation.
        task.status = "VERIFYING"
        task.process_instance_id = "synthetic-forbidden-oa"
        with pytest.raises(IntegrityError):
            database.commit()
        database.rollback()
    if not temporary:
        url = f"/api/admin/oa/reimbursements/submissions/{submission_id}/recover"
        rejected = client.post(
            url,
            headers=headers,
            json={
                "action": "ATTACH_INSTANCE",
                "processInstanceId": "synthetic-forbidden-oa",
                "verificationNote": "合成核对：附件已开始清理",
            },
        )
        assert rejected.status_code == 409
        assert "附件已进入清理" in rejected.json()["error"]["message"]
        for attempt in range(2):
            resumed = client.post(
                url,
                headers=headers,
                json={
                    "action": "CONFIRM_NOT_CREATED",
                    "verificationNote": f"合成核对 {attempt + 1}：确认无审批，已检查清理权限",
                },
            )
            assert resumed.status_code == 200, resumed.text
            if attempt == 0:
                clock[0] = utc_now() + timedelta(seconds=10)
                assert asyncio.run(worker.run_once())
        with client.app.state.database_session_factory() as database:
            audits = list(
                database.scalars(
                    select(ReimbursementSubmissionRecoveryAudit)
                    .where(
                        ReimbursementSubmissionRecoveryAudit.submission_id == submission_id,
                    )
                    .order_by(ReimbursementSubmissionRecoveryAudit.status_version_before)
                )
            )
            assert len(audits) == 2
            assert audits[0].status_version_before < audits[1].status_version_before
            assert audits[0].verification_note != audits[1].verification_note
    storage.fail = False
    clock[0] += timedelta(seconds=10)
    assert asyncio.run(worker.run_once())
    with client.app.state.database_session_factory() as database:
        task = database.get(ReimbursementSubmission, submission_id)
        assert task.status == "FAILED_FINAL" and task.process_instance_id is None
        assert all(
            file.upload_status == "CLEANED"
            for file in database.scalars(
                select(ReimbursementUpload).where(ReimbursementUpload.submission_id == task.id),
            )
        )
        database.get(ReimbursementDraft, draft_id).expires_at = utc_now() - timedelta(seconds=1)
        database.commit()
    maintenance = client.app.state.oa_reimbursement_maintenance
    assert asyncio.run(maintenance.release_linked_local_files(limit=20)) >= len(source_ids)
    with client.app.state.database_session_factory() as database:
        assert all(
            file.file_status == "PURGED"
            for file in database.scalars(
                select(ReimbursementDraftFile).where(ReimbursementDraftFile.id.in_(source_ids)),
            )
        )
    assert workflow.create_calls == 1
    assert storage.commit_calls == 2
