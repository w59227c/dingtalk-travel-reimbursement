from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from conftest import mock_login
from sqlalchemy import event
from sqlalchemy.orm import Session
from test_reimbursement_drafts import _input, _install_catalog
from test_reimbursement_files import _insert_draft, _upload
from test_reimbursement_models import new_draft, new_submission

from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile, utc_now
from app.services.reimbursement_quota import DraftFileOwner, ReimbursementReservationConflict
from app.services.reimbursement_staging import StagingIntegrityError, StagingLayoutError


def setup_workspace(client_factory, monkeypatch, **settings):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True, **settings)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    draft_id = _insert_draft(client)
    response = _upload(client, headers["X-CSRF-Token"], draft_id, revision=1)
    assert response.status_code == 201, response.text
    file = response.json()["data"]["file"]
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraftFile, file["id"])
        path = client.app.state.reimbursement_staging.root / stored.storage_key
    return client, headers, draft_id, file["id"], path


def replace(client, headers, draft_id, revision=2):
    return client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={
            "expectedRevision": 0,
            "replacesDraftId": draft_id,
            "replacesDraftRevision": revision,
            "input": {**_input(), "items": [], "trip": None},
        },
    )


def test_new_workspace_purges_only_old_files_and_preserves_replaced_state(
    client_factory,
    monkeypatch,
):
    client, headers, draft_id, file_id, path = setup_workspace(client_factory, monkeypatch)
    response = replace(client, headers, draft_id)
    assert response.status_code == 201, response.text
    current = response.json()["data"]
    assert current["id"] != draft_id
    assert not path.exists()
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0
    with client.app.state.database_session_factory() as database:
        old = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, file_id)
        assert old.workspace_retired and old.revision == 2
        assert old.expires_at > utc_now() + timedelta(hours=20)
        assert file.file_status == "PURGED" and file.purged_at is not None
        assert json.loads(old.input_json)["items"][0]["amount"] == "44.89"
    rejected = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        headers=headers,
        json={"expectedRevision": 2, "input": _input()},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REPLACED"
    assert (
        client.get("/api/reimbursements/workspace").json()["data"]["items"][0]["id"]
        == current["id"]
    )
    client.app.state.cleanup_retired_reimbursements(draft_id)
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0
    uploaded = _upload(client, headers["X-CSRF-Token"], current["id"], revision=1)
    assert uploaded.status_code == 201
    client.app.state.cleanup_retired_reimbursements()
    with client.app.state.database_session_factory() as database:
        new_file = database.get(ReimbursementDraftFile, uploaded.json()["data"]["file"]["id"])
        assert new_file.file_status == "ACTIVE"
        assert (client.app.state.reimbursement_staging.root / new_file.storage_key).exists()
        assert (
            client.app.state.reimbursement_quota.usage().reserved_bytes == new_file.reserved_bytes
        )


def test_failed_new_workspace_never_cleans_or_retires_old_files(client_factory, monkeypatch):
    client, headers, draft_id, file_id, path = setup_workspace(client_factory, monkeypatch)
    assert replace(client, headers, draft_id, revision=3).status_code == 409

    def reject_new_draft(database, *_args):
        if any(isinstance(value, ReimbursementDraft) for value in database.new):
            raise RuntimeError("synthetic new-workspace failure")

    event.listen(Session, "before_flush", reject_new_draft)
    try:
        with pytest.raises(RuntimeError, match="synthetic new-workspace"):
            replace(client, headers, draft_id)
    finally:
        event.remove(Session, "before_flush", reject_new_draft)
    assert path.exists()
    with client.app.state.database_session_factory() as database:
        assert not database.get(ReimbursementDraft, draft_id).workspace_retired
        assert database.get(ReimbursementDraftFile, file_id).file_status == "ACTIVE"
        assert database.query(ReimbursementDraft).count() == 1
        database.get(ReimbursementDraft, draft_id).owner_user_id = "synthetic-other-user"
        database.commit()
    assert replace(client, headers, draft_id).status_code == 404
    assert path.exists()


@pytest.mark.parametrize("failure", ["disk", "finalize"])
def test_cleanup_failure_keeps_new_workspace_and_retries_after_restart(
    client_factory,
    monkeypatch,
    failure,
):
    client, headers, draft_id, file_id, path = setup_workspace(client_factory, monkeypatch)
    quota = client.app.state.reimbursement_quota
    target = client.app.state.reimbursement_staging if failure == "disk" else quota
    method = "delete" if failure == "disk" else "_finalize_deleting_draft_file"
    original = getattr(target, method)

    def fail(*_args, **_kwargs):
        if failure == "disk":
            raise StagingIntegrityError("synthetic disk failure")
        raise ReimbursementReservationConflict("synthetic finalization failure")

    monkeypatch.setattr(target, method, fail)
    response = replace(client, headers, draft_id)
    assert response.status_code == 201, response.text
    current_id = response.json()["data"]["id"]
    assert quota.usage().reserved_bytes > 0
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraftFile, file_id).file_status == "DELETING"
    monkeypatch.setattr(target, method, original)
    restarted = client_factory(auth_mock_enabled=True)
    mock_login(restarted)
    assert not path.exists()
    assert restarted.app.state.reimbursement_quota.usage().reserved_bytes == 0
    with restarted.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraftFile, file_id).file_status == "PURGED"
        assert database.get(ReimbursementDraft, current_id).workspace_retired is False


@pytest.mark.parametrize("protected", ["current", "locked", "task", "recovery", "source", "copy"])
def test_cleanup_preserves_current_locked_submitted_or_recovering_materials(
    client_factory,
    monkeypatch,
    protected,
):
    client, _headers, draft_id, file_id, path = setup_workspace(client_factory, monkeypatch)
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        draft.workspace_retired = protected != "current"
        if protected == "locked":
            draft.status, draft.locked_at = "LOCKED", utc_now()
        elif protected == "task":
            database.add(new_submission(draft))
        elif protected == "recovery":
            draft.recovery_pending = True
        elif protected == "source":
            target = new_draft()
            target.recovery_source_draft_id = draft_id
            target.recovery_pending = True
            database.add(target)
        database.commit()
    if protected == "copy":
        with client.app.state.reimbursement_staging.recovery_guard(draft_id):
            client.app.state.cleanup_retired_reimbursements(draft_id)
    else:
        client.app.state.cleanup_retired_reimbursements(draft_id)
    assert path.exists()
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraftFile, file_id).file_status == "ACTIVE"


def test_new_workspace_waits_for_inflight_upload_before_purging(
    client_factory,
    monkeypatch,
):
    from app.models.reimbursement import ReimbursementDraftFileRole

    client, headers, draft_id, _file_id, path = setup_workspace(client_factory, monkeypatch)
    quota, staging = client.app.state.reimbursement_quota, client.app.state.reimbursement_staging
    owner = DraftFileOwner("corp-fixed", "mock-user", draft_id, 2)
    reservation = quota.reserve_draft_file(
        owner,
        sort_order=1,
        processing_role=ReimbursementDraftFileRole.ATTACHMENT_ONLY,
        original_name="synthetic-inflight.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=64,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    quota.mark_writing(owner, reservation)
    response = replace(client, headers, draft_id, revision=3)
    assert response.status_code == 201, response.text
    assert path.exists()
    written = staging.write_bytes(reservation.staging, b"synthetic inflight bytes")
    quota.finalize(owner, reservation, written)
    client.app.state.cleanup_retired_reimbursements(draft_id)
    assert not path.exists()
    assert not (staging.root / written.storage_key).exists()
    assert quota.usage().reserved_bytes == 0


def test_new_workspace_waits_for_ocr_and_never_accepts_its_late_result(
    client_factory,
    monkeypatch,
):
    client, headers, draft_id, file_id, path = setup_workspace(client_factory, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    async def blocked_ocr(*_args, **_kwargs):
        entered.set()
        await asyncio.to_thread(release.wait)
        raise RuntimeError("synthetic finished OCR")

    monkeypatch.setattr(client.app.state.ocr_service, "recognize_file", blocked_ocr)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            client.post,
            f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
            headers=headers,
            json={"expectedRevision": 2},
        )
        try:
            assert entered.wait(5)
            response = replace(client, headers, draft_id, revision=3)
            assert response.status_code == 201, response.text
            assert path.exists()
        finally:
            release.set()
        assert pending.result(10).status_code == 409
    client.app.state.cleanup_retired_reimbursements(draft_id)
    assert not path.exists()
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0


def test_two_cleanup_workers_release_quota_once(client_factory, monkeypatch):
    client, _headers, draft_id, _file_id, path = setup_workspace(client_factory, monkeypatch)
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraft, draft_id).workspace_retired = True
        database.commit()
    quota = client.app.state.reimbursement_quota
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(quota.reclaim_retired_draft_files, ocr_timeout_seconds=300)
            for _ in range(2)
        ]
        assert sum(result.result(10) for result in results) == 1
    assert not path.exists()
    assert quota.usage().reserved_bytes == 0


def test_periodic_cleanup_retries_with_oa_worker_disabled(client_factory, monkeypatch):
    client, headers, draft_id, file_id, path = setup_workspace(
        client_factory,
        monkeypatch,
        temp_cleanup_interval_seconds=1,
        dingtalk_oa_worker_enabled=False,
    )
    staging = client.app.state.reimbursement_staging
    quota = client.app.state.reimbursement_quota
    original_delete, original_finalize = staging.delete, quota._finalize_deleting_draft_file
    allowed, finished = threading.Event(), threading.Event()

    def delete_when_allowed(*args, **kwargs):
        if not allowed.is_set():
            raise StagingIntegrityError("synthetic temporary deletion failure")
        return original_delete(*args, **kwargs)

    def finalize(*args, **kwargs):
        result = original_finalize(*args, **kwargs)
        if result:
            finished.set()
        return result

    monkeypatch.setattr(staging, "delete", delete_when_allowed)
    monkeypatch.setattr(quota, "_finalize_deleting_draft_file", finalize)
    assert replace(client, headers, draft_id).status_code == 201
    assert path.exists()
    allowed.set()
    assert finished.wait(5), "the independent periodic sweep must retry without an OA worker"
    assert not path.exists()
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraftFile, file_id).file_status == "PURGED"


def test_retired_files_with_stale_ocr_do_not_wait_for_original_ttl(client_factory, monkeypatch):
    client, _headers, draft_id, file_id, path = setup_workspace(client_factory, monkeypatch)
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraft, draft_id).workspace_retired = True
        file = database.get(ReimbursementDraftFile, file_id)
        file.ocr_status = "RUNNING"
        file.updated_at = utc_now() - timedelta(hours=1)
        database.commit()
    client.app.state.cleanup_retired_reimbursements(draft_id)
    assert not path.exists()
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file.file_status == "PURGED"
        assert file.ocr_status == "FAILED"


def test_retired_cleanup_rechecks_state_after_candidate_scan(client_factory, monkeypatch):
    from contextlib import contextmanager

    client, _headers, draft_id, file_id, path = setup_workspace(client_factory, monkeypatch)
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraft, draft_id).workspace_retired = True
        database.commit()
    staging = client.app.state.reimbursement_staging
    original = staging.recovery_guard

    @contextmanager
    def changed_guard(*args, **kwargs):
        with original(*args, **kwargs):
            with client.app.state.database_session_factory() as database:
                draft = database.get(ReimbursementDraft, draft_id)
                draft.status, draft.locked_at = "LOCKED", utc_now()
                database.commit()
            yield

    monkeypatch.setattr(staging, "recovery_guard", changed_guard)
    client.app.state.cleanup_retired_reimbursements(draft_id)
    assert path.exists()
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraftFile, file_id).file_status == "ACTIVE"


@pytest.mark.parametrize("failure", ["disk", "guard", "os"])
@pytest.mark.parametrize("limit", [1, 20])
def test_failed_cleanup_does_not_starve_older_pending_work(
    client_factory, monkeypatch, failure, limit
):
    client, headers, first_id, first_file_id, first_path = setup_workspace(
        client_factory, monkeypatch
    )
    second_id = _insert_draft(client)
    second = _upload(client, headers["X-CSRF-Token"], second_id, revision=1).json()["data"]["file"]
    with client.app.state.database_session_factory() as database:
        for identifier, hours in ((first_id, 2), (second_id, 1)):
            draft = database.get(ReimbursementDraft, identifier)
            draft.workspace_retired = True
            draft.updated_at = utc_now() - timedelta(hours=hours)
        second_file = database.get(ReimbursementDraftFile, second["id"])
        second_file.updated_at = utc_now() - timedelta(hours=1)
        second_path = client.app.state.reimbursement_staging.root / second_file.storage_key
        database.commit()
    staging, quota = client.app.state.reimbursement_staging, client.app.state.reimbursement_quota
    original_delete = staging.delete
    original_guard = staging.recovery_guard

    def fail_first(key, **kwargs):
        if key == str(first_path.relative_to(staging.root)):
            raise StagingIntegrityError("synthetic persistent failure")
        return original_delete(key, **kwargs)

    def fail_guard(identifier, **kwargs):
        if identifier == first_id:
            if failure == "os":
                raise OSError("synthetic unreadable directory")
            raise StagingLayoutError("synthetic unreadable directory")
        return original_guard(identifier, **kwargs)

    monkeypatch.setattr(
        staging,
        "delete" if failure == "disk" else "recovery_guard",
        fail_first if failure == "disk" else fail_guard,
    )
    assert quota.reclaim_retired_draft_files(ocr_timeout_seconds=300, limit=limit) == (
        0 if limit == 1 else 1
    )
    if limit == 1:
        assert quota.reclaim_retired_draft_files(ocr_timeout_seconds=300, limit=1) == 1
    assert first_path.exists() and not second_path.exists()
    with client.app.state.database_session_factory() as database:
        first = database.get(ReimbursementDraftFile, first_file_id)
        assert first.file_status == ("DELETING" if failure == "disk" else "ACTIVE")
        assert quota.usage().reserved_bytes == first.reserved_bytes
