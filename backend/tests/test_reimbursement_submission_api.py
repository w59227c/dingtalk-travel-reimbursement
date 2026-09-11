from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import mock_login

from app.api import reimbursement_submissions as api
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftStatus,
    ReimbursementSubmission,
    utc_now,
)
from app.schemas.reimbursement_submissions import SubmitReimbursementRequest
from app.services.reimbursement_drafts import DraftActor
from app.services.reimbursement_submissions import create_submission


@pytest.mark.parametrize("instance_id", [None, "existing-oa"])
def test_recheck_only_resumes_known_instance_with_csrf_and_keeps_original_draft(
    client_factory, instance_id
):
    from app.integrations.dingtalk.workflow import (
        CreateProcessInstanceCommand,
        CreateWorkflowFormValue,
    )
    from app.services.oa_reimbursement_payload import (
        create_command_sha256,
        serialize_create_command,
    )

    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="owner-1",
        auth_mock_departments="100:测试部门",
        dingtalk_oa_worker_enabled=False,
    )
    login = mock_login(client)
    draft_id, submission_id = _persist_submission_for_mock_user(client)
    command = CreateProcessInstanceCommand(
        "PROC-REIMBURSEMENT", "owner-1", 100, 123, (CreateWorkflowFormValue("金额", "199.00"),)
    )
    with client.app.state.database_session_factory() as db:
        record = db.get(ReimbursementSubmission, submission_id)
        record.status = "MANUAL_REVIEW"
        record.process_instance_id = instance_id
        record.oa_request_json = serialize_create_command(command)
        record.oa_request_hash = create_command_sha256(command)
        record.oa_create_started_at = utc_now()
        db.commit()
        locked = db.get(ReimbursementDraft, draft_id)
        before = (locked.revision, locked.locked_at, locked.input_json)
    url = f"/api/oa/reimbursements/submissions/{submission_id}/recheck"
    assert client.post(url).status_code == 403
    headers = {"X-CSRF-Token": login["csrfToken"]}
    assert client.post(url, headers=headers).status_code == 503
    # Enable only the endpoint gate, not a background worker in the isolated test.
    client.app.state.settings.dingtalk_oa_worker_enabled = True
    response = client.post(url, headers=headers)
    if instance_id is None:
        assert response.status_code == 409
    else:
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "VERIFYING"
        assert response.json()["data"]["processInstanceId"] == instance_id
        again = client.post(url, headers=headers)
        assert again.json()["data"]["statusVersion"] == response.json()["data"]["statusVersion"]
    with client.app.state.database_session_factory() as db:
        locked = db.get(ReimbursementDraft, draft_id)
        assert (locked.revision, locked.locked_at, locked.input_json) == before


@pytest.mark.asyncio
async def test_submit_refresh_returns_existing_before_rebuilding_locked_snapshot(
    monkeypatch,
) -> None:
    expected_actor = object()
    database = object()
    existing = SimpleNamespace(
        id="submission-existing",
        draft_id="draft-1",
        status="UPLOADING",
        status_version=7,
        attempt_count=1,
        process_instance_id=None,
        business_id=None,
        approval_url=None,
        last_error_code=None,
        last_error_message=None,
        created_at=datetime(2026, 9, 4, 8, 0, 0),
        updated_at=datetime(2026, 9, 4, 8, 1, 0),
        submitted_at=None,
    )
    first_key = "11111111-1111-4111-8111-111111111111"
    keys = iter((first_key, first_key, "22222222-2222-4222-8222-222222222222"))

    monkeypatch.setattr(api, "draft_actor", lambda _current: expected_actor)

    def find_owned(received_database, *, actor: object, draft_id: str):
        assert received_database is database
        assert actor is expected_actor
        assert draft_id == "draft-1"
        return existing

    monkeypatch.setattr(api, "find_owned_submission_for_draft", find_owned)
    monkeypatch.setattr(
        api,
        "collect_snapshot_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing submissions must not rebuild a locked snapshot")
        ),
    )

    results = []
    for revision in (3, 3, 999):
        results.append(
            await api.submit_reimbursement(
                "draft-1",
                SubmitReimbursementRequest(expectedRevision=revision),
                SimpleNamespace(),
                database,
                object(),
                next(keys),
            )
        )

    assert [item["data"]["submissionId"] for item in results] == [
        "submission-existing",
        "submission-existing",
        "submission-existing",
    ]


def _persist_submission_for_mock_user(client) -> tuple[str, str]:
    actor = DraftActor(
        corp_id=client.app.state.settings.dingtalk_corp_id,
        user_id="owner-1",
        department_id="100",
        department_name="测试部门",
    )
    with client.app.state.database_session_factory() as database:
        draft = ReimbursementDraft(
            corp_id=actor.corp_id,
            owner_user_id=actor.user_id,
            status=ReimbursementDraftStatus.REVIEW_READY.value,
            revision=1,
            department_id=actor.department_id,
            department_name=actor.department_name,
            template_process_code="PROC-REIMBURSEMENT",
            template_config_version=1,
            schema_fingerprint="a" * 64,
            input_json="{}",
            related_instance_ids_json="[]",
            expires_at=utc_now() + timedelta(days=7),
        )
        database.add(draft)
        database.flush()
        database.add(
            ReimbursementDraftFile(
                draft_id=draft.id,
                sort_order=0,
                processing_role=ReimbursementDraftFileRole.ATTACHMENT_ONLY.value,
                file_status=ReimbursementDraftFileStatus.ACTIVE.value,
                storage_key=f"drafts/{draft.id}/invoice.pdf",
                part_storage_key=None,
                reserved_bytes=10,
                reservation_expires_at=None,
                original_name="发票.pdf",
                extension="pdf",
                media_type="application/pdf",
                size_bytes=10,
                sha256="b" * 64,
            )
        )
        database.commit()
        result = create_submission(
            database,
            actor=actor,
            draft_id=draft.id,
            expected_revision=1,
            originator_union_id="union-owner-1",
            originator_name="测试员工",
            idempotency_key="submission-recovery-test",
            snapshot_version=6,
            form_snapshot_json="{}",
            snapshot_sha256=hashlib.sha256(b"{}").hexdigest(),
        )
        return draft.id, result.submission_id


def test_submission_can_be_recovered_with_worker_disabled_without_mutation(
    client_factory,
) -> None:
    owner = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="owner-1",
        auth_mock_departments="100:测试部门",
        dingtalk_oa_worker_enabled=False,
    )
    login = mock_login(owner)
    draft_id, submission_id = _persist_submission_for_mock_user(owner)
    with owner.app.state.database_session_factory() as database:
        before = database.get(ReimbursementSubmission, submission_id)
        before_draft = database.get(ReimbursementDraft, draft_id)
        immutable_submission = (before.status, before.status_version, before.updated_at)
        immutable_draft = (
            before_draft.status,
            before_draft.revision,
            before_draft.locked_at,
            before_draft.updated_at,
        )

    replay = owner.post(
        f"/api/oa/reimbursements/{draft_id}/submit",
        json={"expectedRevision": 999},
        headers={
            "X-CSRF-Token": login["csrfToken"],
            "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
        },
    )
    recovered = owner.get(f"/api/oa/reimbursements/drafts/{draft_id}/submission")
    by_id = owner.get(f"/api/oa/reimbursements/submissions/{submission_id}")

    assert recovered.status_code == 200, recovered.text
    assert replay.status_code == 202, replay.text
    assert replay.json()["data"] == recovered.json()["data"]
    assert recovered.json()["data"] == by_id.json()["data"]
    assert recovered.json()["data"]["submissionId"] == submission_id
    with owner.app.state.database_session_factory() as database:
        after = database.get(ReimbursementSubmission, submission_id)
        after_draft = database.get(ReimbursementDraft, draft_id)
        assert (after.status, after.status_version, after.updated_at) == immutable_submission
        assert (
            after_draft.status,
            after_draft.revision,
            after_draft.locked_at,
            after_draft.updated_at,
        ) == immutable_draft

    # The recovery read is intentionally independent from the CSRF token.
    assert login["csrfToken"]


def test_submission_recovery_is_authenticated_and_identity_scoped(client_factory) -> None:
    owner = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="owner-1",
        auth_mock_departments="100:测试部门",
    )
    mock_login(owner)
    draft_id, _ = _persist_submission_for_mock_user(owner)

    anonymous = client_factory(auth_mock_enabled=True)
    assert anonymous.get(f"/api/oa/reimbursements/drafts/{draft_id}/submission").status_code == 401

    stranger = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="stranger-1",
        auth_mock_departments="100:测试部门",
    )
    mock_login(stranger)
    hidden_user = stranger.get(f"/api/oa/reimbursements/drafts/{draft_id}/submission")
    assert hidden_user.status_code == 404
    assert hidden_user.json()["error"]["code"] == "REIMBURSEMENT_SUBMISSION_NOT_FOUND"

    other_corp = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="owner-1",
        auth_mock_departments="100:测试部门",
        dingtalk_corp_id="corp-other",
    )
    mock_login(other_corp)
    hidden_corp = other_corp.get(f"/api/oa/reimbursements/drafts/{draft_id}/submission")
    assert hidden_corp.status_code == 404
    assert hidden_corp.json()["error"]["code"] == "REIMBURSEMENT_SUBMISSION_NOT_FOUND"


def test_submission_recovery_returns_stable_not_found_for_unsubmitted_draft(
    client_factory,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    mock_login(client)

    missing = client.get(
        "/api/oa/reimbursements/drafts/00000000-0000-4000-8000-000000000000/submission"
    )

    assert missing.status_code == 404
    assert missing.json()["error"] == {
        "code": "REIMBURSEMENT_SUBMISSION_NOT_FOUND",
        "message": "提交记录不存在",
    }
