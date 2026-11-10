import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import mock_login
from sqlalchemy import select
from test_oa_reimbursement_integration import (
    LocalStorageBoundary,
    LocalWorkflowBoundary,
    _persist_ready_draft,
    _schemas,
    _worker,
)
from test_reimbursement_recovery import failed_form

from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile, utc_now

pytest_plugins = ["test_oa_reimbursement_integration"]


def source_states(client, ids):
    with client.app.state.database_session_factory() as database:
        return list(
            database.scalars(
                select(ReimbursementDraftFile.file_status).where(
                    ReimbursementDraftFile.id.in_(ids),
                )
            )
        )


def test_safe_failed_sources_expire_without_deleting_the_snapshot(enabled_manual_worker_client):
    client = enabled_manual_worker_client
    draft_id, revision, source_ids, headers = failed_form(client)
    before = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    maintenance = client.app.state.oa_reimbursement_maintenance
    assert asyncio.run(maintenance.release_linked_local_files(limit=20)) == 0
    assert source_states(client, source_ids) == ["ACTIVE"] * len(source_ids)
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraft, draft_id).expires_at = utc_now() - timedelta(seconds=1)
        database.commit()
    quota_before = client.app.state.reimbursement_quota.usage().reserved_bytes
    assert asyncio.run(maintenance.release_linked_local_files(limit=20)) == len(source_ids)
    assert source_states(client, source_ids) == ["PURGED"] * len(source_ids)
    assert client.app.state.reimbursement_quota.usage().reserved_bytes < quota_before
    assert asyncio.run(maintenance.release_linked_local_files(limit=20)) == 0
    after = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert after["input"] == before["input"]
    assert after["status"] == "LOCKED"
    expired = client.post(
        f"/api/reimbursements/drafts/{draft_id}/recover",
        json={"expectedRevision": revision},
        headers=headers,
    )
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_EXPIRED"


def test_failed_sources_stay_available_while_their_recovery_is_pending(
    enabled_manual_worker_client,
    monkeypatch,
):
    from app.services.reimbursement_staging import ReimbursementStaging

    client = enabled_manual_worker_client
    draft_id, revision, source_ids, headers = failed_form(client)
    original_open = ReimbursementStaging.open_verified
    maintenance = client.app.state.oa_reimbursement_maintenance
    inspected = False

    def open_while_expiring(staging, key, **kwargs):
        nonlocal inspected
        if not inspected:
            inspected = True
            with client.app.state.database_session_factory() as database:
                database.get(ReimbursementDraft, draft_id).expires_at = utc_now() - timedelta(
                    seconds=1
                )
                database.commit()
            assert asyncio.run(maintenance.release_linked_local_files(limit=20)) == 0
            assert source_states(client, source_ids) == ["ACTIVE"] * len(source_ids)
        return original_open(staging, key, **kwargs)

    monkeypatch.setattr(ReimbursementStaging, "open_verified", open_while_expiring)
    recovered = client.post(
        f"/api/reimbursements/drafts/{draft_id}/recover",
        json={"expectedRevision": revision},
        headers=headers,
    )
    assert recovered.status_code == 200, recovered.text
    assert inspected and not recovered.json()["data"]["recoveryPending"]
    assert asyncio.run(maintenance.release_linked_local_files(limit=20)) == len(source_ids)
    new_id = recovered.json()["data"]["id"]
    assert source_states(client, source_ids) == ["PURGED"] * len(source_ids)
    with client.app.state.database_session_factory() as database:
        copies = list(
            database.scalars(
                select(ReimbursementDraftFile).where(
                    ReimbursementDraftFile.draft_id == new_id,
                )
            )
        )
        assert len(copies) == len(source_ids)
        assert all(file.file_status == "ACTIVE" for file in copies)


@pytest.mark.parametrize("unknown_commit", [False, True])
def test_expiry_never_purges_sources_during_submission_or_uncertain_commit(
    enabled_manual_worker_client,
    unknown_commit,
):
    client = enabled_manual_worker_client
    schemas = _schemas()
    workflow = LocalWorkflowBoundary(*schemas[:2])
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"], "Idempotency-Key": str(uuid4())}
    draft_id, files = _persist_ready_draft(client, workflow, schemas[2])
    response = client.post(
        f"/api/oa/reimbursements/{draft_id}/submit", json={"expectedRevision": 4}, headers=headers
    )
    assert response.status_code == 202, response.text
    if unknown_commit:
        assert asyncio.run(
            _worker(client, workflow, LocalStorageBoundary(unknown_commit_at=1)).run_once()
        )
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraft, draft_id).expires_at = utc_now() - timedelta(seconds=1)
        database.commit()
    maintenance = client.app.state.oa_reimbursement_maintenance
    assert asyncio.run(maintenance.release_linked_local_files(limit=20)) == 0
    assert source_states(client, files) == ["ACTIVE"] * len(files)
