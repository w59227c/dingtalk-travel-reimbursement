from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from threading import Event, get_ident
from uuid import uuid4

import pytest
from conftest import mock_login
from sqlalchemy import select
from test_oa_reimbursement_integration import (
    LocalWorkflowBoundary,
    _persist_ready_draft,
    _schemas,
)

from app.domain.categories import ExpenseCategory
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementSubmission,
    utc_now,
)
from app.ocr.types import ItinerarySummary, ItineraryTrip, ParsedExpense, ParsedItinerary
from app.services.ocr_service import parsed_expense_payload, parsed_itinerary_payload
from app.services.reimbursement_staging import (
    StagingIntegrityError,
    StagingObjectNotFound,
    StagingReservation,
)
from app.services.reimbursement_submissions import claim_due_submission, fail_submission_final

pytest_plugins = ["test_oa_reimbursement_integration"]


def failed_form(client, *, recognized_materials=False):
    reimbursement, travel, travel_type = _schemas()
    workflow = LocalWorkflowBoundary(reimbursement, travel)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    identifier, files = _persist_ready_draft(client, workflow, travel_type)
    if recognized_materials:
        with client.app.state.database_session_factory() as database:
            receipt = database.get(ReimbursementDraftFile, files[0])
            receipt.ocr_result_json = json.dumps(
                parsed_expense_payload(
                    receipt.id,
                    ParsedExpense(
                        receipt_type="taxi",
                        category=ExpenseCategory.LOCAL_TRANSPORT,
                        date=date(2026, 9, 1),
                        description="合成机场至酒店",
                        amount=Decimal("44.89"),
                        confidence=0.95,
                        transport_type="taxi",
                    ),
                )
            )
            itinerary = database.get(ReimbursementDraftFile, files[1])
            itinerary.attachment_kind = "itinerary"
            itinerary.ocr_status = "COMPLETE"
            itinerary.ocr_result_json = json.dumps(
                parsed_itinerary_payload(
                    itinerary.id,
                    ParsedItinerary(
                        source="pdf_text",
                        page_count=1,
                        processed_page_count=1,
                        complete=True,
                        summary=ItinerarySummary(
                            currency="CNY",
                            amount=Decimal("44.89"),
                            start_date=date(2026, 9, 1),
                            end_date=date(2026, 9, 1),
                        ),
                        trips=(
                            ItineraryTrip(
                                page=1,
                                row=1,
                                date=date(2026, 9, 1),
                                amount=Decimal("44.89"),
                                origin="合成机场",
                                destination="合成酒店",
                            ),
                        ),
                    ),
                )
            )
            draft = database.get(ReimbursementDraft, identifier)
            data = json.loads(draft.input_json)
            data["items"][0]["itineraryFileIds"] = [itinerary.id]
            draft.input_json = json.dumps(data)
            database.commit()
    submitted = client.post(
        f"/api/oa/reimbursements/{identifier}/submit",
        json={"expectedRevision": 4},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert submitted.status_code == 202, submitted.text
    with client.app.state.database_session_factory() as database:
        claim = claim_due_submission(database, worker_id="recovery-test", lease_seconds=180)
        assert claim is not None
        fail_submission_final(
            database,
            lease=claim.lease,
            worker_id="recovery-test",
            error_code="LOCAL_VALIDATION",
            error_message="合成失败，未创建审批",
        )
        database.commit()
        revision = database.get(ReimbursementDraft, identifier).revision
    return identifier, revision, files, headers


def recover(client, identifier, revision, headers):
    return client.post(
        f"/api/reimbursements/drafts/{identifier}/recover",
        json={"expectedRevision": revision},
        headers=headers,
    )


def test_recovery_preserves_content_and_original_and_is_idempotent(enabled_manual_worker_client):
    client = enabled_manual_worker_client
    identifier, revision, source_files, headers = failed_form(client)
    with client.app.state.database_session_factory() as database:
        source_input = database.get(ReimbursementDraft, identifier).input_json
        task = database.scalar(select(ReimbursementSubmission))
        snapshot, frozen_hash = task.form_snapshot_json, task.snapshot_sha256
    result = recover(client, identifier, revision, headers)
    assert result.status_code == 200, result.text
    target = result.json()["data"]
    assert target["id"] != identifier
    assert target["recoverySourceDraftId"] == identifier
    assert target["recoveryPending"] is False
    assert target["input"]["accountingSourceVerified"] is False
    assert target["input"]["trip"] == json.loads(source_input)["trip"]
    before, after = json.loads(source_input)["items"][0], target["input"]["items"][0]
    assert after["amount"] == before["amount"]
    assert after["description"] == before["description"]
    assert after["sourceFileId"] not in source_files
    assert target["relatedApprovalCount"] == 1
    with client.app.state.database_session_factory() as database:
        source = database.get(ReimbursementDraft, identifier)
        assert source.input_json == source_input and source.status == "LOCKED"
        task = database.scalar(select(ReimbursementSubmission))
        assert task.form_snapshot_json == snapshot and task.snapshot_sha256 == frozen_hash
        assert task.status == "FAILED_FINAL"
        copies = database.scalars(
            select(ReimbursementDraftFile).where(
                ReimbursementDraftFile.draft_id == target["id"],
            )
        ).all()
        assert len(copies) == len(source_files)
        assert all(file.file_status == "ACTIVE" for file in copies)
        for copy in copies:
            with client.app.state.reimbursement_staging.open_verified(
                copy.storage_key,
                expected_size=copy.size_bytes,
                expected_sha256=copy.sha256,
            ):
                pass
        edited = database.get(ReimbursementDraft, target["id"])
        value = json.loads(edited.input_json)
        value["items"][0]["description"] = "恢复后修改"
        edited.input_json = json.dumps(value)
        edited.revision += 1
        database.commit()
    repeated = recover(client, identifier, revision, headers)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["id"] == target["id"]
    assert repeated.json()["data"]["input"]["items"][0]["description"] == "恢复后修改"
    workspace = client.get("/api/reimbursements/workspace").json()["data"]
    assert [row["id"] for row in workspace["items"]] == [target["id"]]
    assert client.get("/api/reimbursements/submission-history").json()["data"] == []


def test_recovery_rebinds_receipt_and_itinerary_ocr_without_changing_source(
    enabled_manual_worker_client,
):
    client = enabled_manual_worker_client
    identifier, revision, source_files, headers = failed_form(client, recognized_materials=True)
    with client.app.state.database_session_factory() as database:
        original_results = {
            file.original_name: (file.id, file.ocr_result_json)
            for file in database.scalars(
                select(ReimbursementDraftFile).where(
                    ReimbursementDraftFile.draft_id == identifier,
                )
            )
        }
    result = recover(client, identifier, revision, headers)
    assert result.status_code == 200, result.text
    target_id = result.json()["data"]["id"]
    restored = client.get(f"/api/reimbursements/drafts/{target_id}/files").json()["data"]["items"]
    assert len(restored) == 2
    restored_itinerary = next(file for file in restored if file["attachmentKind"] == "itinerary")
    assert result.json()["data"]["input"]["items"][0]["itineraryFileIds"] == [
        restored_itinerary["id"]
    ]
    for file in restored:
        expected = json.loads(original_results[file["name"]][1])
        expected["fileId"] = file["id"]
        assert file["id"] not in source_files
        assert file["ocrStatus"] == "COMPLETE"
        assert file["ocrResult"] == expected
    repeated = recover(client, identifier, revision, headers)
    assert repeated.status_code == 200
    assert repeated.json()["data"]["id"] == target_id
    assert (
        client.get(f"/api/reimbursements/drafts/{target_id}/files").json()["data"]["items"]
        == restored
    )
    with client.app.state.database_session_factory() as database:
        for source_id, original_json in original_results.values():
            assert database.get(ReimbursementDraftFile, source_id).ocr_result_json == original_json


def test_recovered_form_can_fail_again_and_recover_its_latest_snapshot(
    enabled_manual_worker_client,
):
    client = enabled_manual_worker_client
    original_id, original_revision, original_files, headers = failed_form(client)
    first = recover(client, original_id, original_revision, headers).json()["data"]
    approval = first["relatedApprovals"][0]
    today = utc_now().date()
    linked = client.put(
        f"/api/reimbursements/drafts/{first['id']}/related-approvals",
        json={
            "expectedRevision": first["revision"],
            "selections": [{
                "processInstanceId": approval["processInstanceId"],
                "profileKey": approval["profileKey"],
                "queryWindow": {
                    "from": (today - timedelta(days=89)).isoformat(),
                    "to": today.isoformat(),
                },
            }],
        },
        headers=headers,
    )
    assert linked.status_code == 200, linked.text
    latest = linked.json()["data"]
    value = latest["input"]
    value["items"][0]["description"] = "第二次提交前保存的说明"
    saved = client.put(
        f"/api/reimbursements/drafts/{first['id']}",
        json={"expectedRevision": latest["revision"], "input": value},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    reviewed = client.post(
        f"/api/reimbursements/drafts/{first['id']}/review",
        json={"expectedRevision": saved.json()["data"]["revision"]},
        headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    submitted = client.post(
        f"/api/oa/reimbursements/{first['id']}/submit",
        json={"expectedRevision": reviewed.json()["data"]["revision"]},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert submitted.status_code == 202, submitted.text
    with client.app.state.database_session_factory() as database:
        claim = claim_due_submission(database, worker_id="repeat-recovery-test", lease_seconds=180)
        assert claim is not None
        fail_submission_final(
            database,
            lease=claim.lease,
            worker_id="repeat-recovery-test",
            error_code="LOCAL_VALIDATION",
            error_message="第二次合成失败，未创建审批",
        )
        database.commit()
        revision = database.get(ReimbursementDraft, first["id"]).revision
        evidence = {
            task.draft_id: (task.form_snapshot_json, task.snapshot_sha256)
            for task in database.scalars(select(ReimbursementSubmission)).all()
        }
    result = recover(client, first["id"], revision, headers)
    assert result.status_code == 200, result.text
    second = result.json()["data"]
    assert second["id"] not in {original_id, first["id"]}
    assert second["recoverySourceDraftId"] == first["id"]
    assert second["recoveryPending"] is False
    assert second["input"]["items"][0]["description"] == "第二次提交前保存的说明"
    assert recover(client, first["id"], revision, headers).json()["data"]["id"] == second["id"]
    workspace = client.get("/api/reimbursements/workspace").json()["data"]
    assert [row["id"] for row in workspace["items"]] == [second["id"]]
    assert client.get("/api/reimbursements/submission-history").json()["data"] == []
    with client.app.state.database_session_factory() as database:
        assert evidence == {
            task.draft_id: (task.form_snapshot_json, task.snapshot_sha256)
            for task in database.scalars(select(ReimbursementSubmission)).all()
        }
        copied_files = database.scalars(select(ReimbursementDraftFile).where(
            ReimbursementDraftFile.draft_id == second["id"],
        )).all()
        assert len(copied_files) == len(original_files)
        assert not set(original_files).intersection(file.id for file in copied_files)
        for file in copied_files:
            assert file.file_status == "ACTIVE"
            with client.app.state.reimbursement_staging.open_verified(
                file.storage_key, expected_size=file.size_bytes, expected_sha256=file.sha256,
            ):
                pass
    assert client.app.state.dingtalk_workflow.create_calls == 0


def test_missing_original_preserves_amount_and_reports_reupload(
    enabled_manual_worker_client, monkeypatch
):
    client = enabled_manual_worker_client
    identifier, revision, files, headers = failed_form(client)
    staging = client.app.state.reimbursement_staging
    with client.app.state.database_session_factory() as database:
        missing = database.get(ReimbursementDraftFile, files[0]).storage_key
    original_open = staging.open_verified

    def missing_open(key, **kwargs):
        if key == missing:
            raise StagingObjectNotFound("synthetic missing original")
        return original_open(key, **kwargs)

    monkeypatch.setattr(staging, "open_verified", missing_open)
    result = recover(client, identifier, revision, headers)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "REIMBURSEMENT_RECOVERY_FILES_UNAVAILABLE"
    with client.app.state.database_session_factory() as database:
        source = database.get(ReimbursementDraft, identifier)
        assert json.loads(source.input_json)["items"][0]["amount"] != "0.00"
        target = database.scalar(
            select(ReimbursementDraft).where(
                ReimbursementDraft.recovery_source_draft_id == identifier,
            )
        )
        assert target.recovery_pending


def test_interrupted_copy_resumes_same_target_and_blocks_partial_editing(
    enabled_manual_worker_client, monkeypatch
):
    client = enabled_manual_worker_client
    identifier, revision, _, headers = failed_form(client)
    staging = client.app.state.reimbursement_staging
    original_copy = staging.copy
    count = 0

    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise StagingObjectNotFound("synthetic transient copy error")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(staging, "copy", interrupted)
    result = recover(client, identifier, revision, headers)
    assert result.status_code >= 400
    with client.app.state.database_session_factory() as database:
        target = database.scalar(
            select(ReimbursementDraft).where(
                ReimbursementDraft.recovery_source_draft_id == identifier,
            )
        )
        assert target.recovery_pending
        target_id, target_revision = target.id, target.revision
    blocked = client.put(
        f"/api/reimbursements/drafts/{target_id}",
        headers=headers,
        json={
            "expectedRevision": target_revision,
            "input": client.get(f"/api/reimbursements/drafts/{target_id}").json()["data"]["input"],
        },
    )
    assert blocked.status_code == 409, blocked.text
    deletion = client.delete(
        f"/api/reimbursements/drafts/{target_id}",
        headers=headers,
        params={"expectedRevision": target_revision},
    )
    assert deletion.status_code == 409, deletion.text
    preview = client.post(
        f"/api/reimbursements/drafts/{target_id}/excel-preview",
        headers=headers,
        json={"expectedRevision": target_revision},
    )
    assert preview.status_code == 409, preview.text
    submitted = client.post(
        f"/api/oa/reimbursements/{target_id}/submit",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"expectedRevision": target_revision},
    )
    assert submitted.status_code == 409, submitted.text
    monkeypatch.setattr(staging, "copy", original_copy)
    resumed = recover(client, identifier, revision, headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["id"] == target_id
    assert resumed.json()["data"]["recoveryPending"] is False


@pytest.mark.parametrize("case", ["revision", "csrf", "uncertain"])
def test_recovery_rejects_unsafe_requests(enabled_manual_worker_client, case):
    client = enabled_manual_worker_client
    identifier, revision, _, headers = failed_form(client)
    if case == "revision":
        revision -= 1
    elif case == "csrf":
        headers = {}
    else:
        with client.app.state.database_session_factory() as database:
            task = database.scalar(select(ReimbursementSubmission))
            task.oa_create_started_at = task.created_at
            database.commit()
    result = recover(client, identifier, revision, headers)
    assert result.status_code in {403, 409}, result.text
    with client.app.state.database_session_factory() as database:
        assert (
            database.scalar(
                select(ReimbursementDraft).where(
                    ReimbursementDraft.recovery_source_draft_id == identifier,
                )
            )
            is None
        )


def test_concurrent_recovery_reuses_pending_copy(enabled_manual_worker_client, monkeypatch):
    client = enabled_manual_worker_client
    identifier, revision, _, headers = failed_form(client)
    staging = client.app.state.reimbursement_staging
    copying, release = Event(), Event()
    original_copy = staging.copy

    def paused_copy(*args, **kwargs):
        copying.set()
        assert release.wait(timeout=10)
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(staging, "copy", paused_copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(recover, client, identifier, revision, headers)
        try:
            assert copying.wait(timeout=10)
            second = recover(client, identifier, revision, headers)
            assert second.status_code == 200, second.text
            assert second.json()["data"]["recoveryPending"] is True
        finally:
            release.set()
        completed = first.result(timeout=10)
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["id"] == second.json()["data"]["id"]
    assert completed.json()["data"]["recoveryPending"] is False
    with client.app.state.database_session_factory() as database:
        assert database.query(ReimbursementDraft).count() == 2


def _interrupt_recovery(client, monkeypatch, *, published=False):
    identifier, revision, _, headers = failed_form(client)
    staging = client.app.state.reimbursement_staging
    real_copy = staging.copy
    copies = 0

    def interrupted(*args, **kwargs):
        nonlocal copies
        copies += 1
        if copies == 2:
            if published:
                real_copy(*args, **kwargs)
            raise StagingObjectNotFound("synthetic interrupted recovery")
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(staging, "copy", interrupted)
    assert recover(client, identifier, revision, headers).status_code >= 400
    with client.app.state.database_session_factory() as database:
        target = database.scalar(
            select(ReimbursementDraft).where(
                ReimbursementDraft.recovery_source_draft_id == identifier,
            )
        )
        target_id = target.id
    return identifier, revision, headers, target_id, real_copy


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("fresh_lease", [False, True])
def test_resumes_material_copy_after_startup_cleanup(
    enabled_manual_worker_client,
    monkeypatch,
    published,
    fresh_lease,
):
    client = enabled_manual_worker_client
    identifier, revision, headers, target_id, real_copy = _interrupt_recovery(
        client,
        monkeypatch,
        published=published,
    )
    with client.app.state.database_session_factory() as database:
        source_json = database.get(ReimbursementDraft, identifier).input_json
        snapshot_hash = database.scalar(select(ReimbursementSubmission)).snapshot_sha256
        target = database.get(ReimbursementDraft, target_id)
        if fresh_lease:
            # Simulate process death before the request's exception cleanup ran.
            target.updated_at = utc_now()
            database.commit()
        target_revision = target.revision
        files_before = [
            (file.id, file.file_status, file.part_storage_key)
            for file in database.scalars(
                select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == target_id)
            )
        ]
    assert client.app.state.reimbursement_quota.reclaim_interrupted_local_writes() == 0
    with client.app.state.database_session_factory() as database:
        target = database.get(ReimbursementDraft, target_id)
        assert target.revision > target_revision
        assert [
            (file.id, file.file_status, file.part_storage_key)
            for file in database.scalars(
                select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == target_id)
            )
        ] == files_before
    resumed_copies = []

    def resume_copy(*args, **kwargs):
        resumed_copies.append(args[1].storage_key)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(client.app.state.reimbursement_staging, "copy", resume_copy)
    resumed = recover(client, identifier, revision, headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["id"] == target_id
    assert resumed.json()["data"]["recoveryPending"] is False
    assert len(resumed_copies) == (0 if published else 1)
    with client.app.state.database_session_factory() as database:
        for file in database.scalars(
            select(ReimbursementDraftFile).where(
                ReimbursementDraftFile.draft_id == target_id,
            )
        ):
            assert file.file_status == "ACTIVE"
            with client.app.state.reimbursement_staging.open_verified(
                file.storage_key, expected_size=file.size_bytes, expected_sha256=file.sha256
            ):
                pass
        assert database.get(ReimbursementDraft, identifier).input_json == source_json
        assert database.scalar(select(ReimbursementSubmission)).snapshot_sha256 == snapshot_hash


def test_expired_recovery_reservations_still_get_reclaimed(
    enabled_manual_worker_client, monkeypatch
):
    client = enabled_manual_worker_client
    identifier, _, _, target_id, _ = _interrupt_recovery(client, monkeypatch)
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraft, target_id).expires_at = utc_now() - timedelta(seconds=1)
        database.commit()
    assert client.app.state.reimbursement_quota.reclaim_interrupted_local_writes() == 1
    assert client.app.state.reimbursement_quota.reclaim_expired() == 1
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, target_id) is None
        assert database.get(ReimbursementDraft, identifier).status == "LOCKED"


@pytest.mark.parametrize("interrupted", [False, True])
def test_lease_takeover_does_not_publish_another_writers_partial_file(
    enabled_manual_worker_client, monkeypatch, interrupted
):
    client = enabled_manual_worker_client
    identifier, revision, _, headers = failed_form(client)
    staging = client.app.state.reimbursement_staging
    old_ready, new_partial, old_finished = Event(), Event(), Event()
    copy_threads = []
    real_copy, real_link, real_fdopen = staging.copy, os.link, os.fdopen

    def counted_copy(*args, **kwargs):
        copy_threads.append(get_ident())
        return real_copy(*args, **kwargs)

    def paused_link(*args, **kwargs):
        if get_ident() == copy_threads[0] and not old_ready.is_set():
            old_ready.set()
            assert new_partial.wait(10)
            try:
                return real_link(*args, **kwargs)
            finally:
                old_finished.set()
        return real_link(*args, **kwargs)

    class TakeoverWriter:
        def __init__(self, writer):
            self.writer = writer

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.writer.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.writer, name)

        def write(self, content):
            self.writer.write(content[:1])
            self.writer.flush()
            new_partial.set()
            assert old_finished.wait(10)
            if interrupted:
                raise StagingIntegrityError("synthetic takeover copy interruption")
            self.writer.write(content[1:])

    def takeover_fdopen(fd, *args, **kwargs):
        writer = real_fdopen(fd, *args, **kwargs)
        if len(copy_threads) == 2 and get_ident() == copy_threads[1] and args == ("wb",):
            return TakeoverWriter(writer)
        return writer

    monkeypatch.setattr(staging, "copy", counted_copy)
    monkeypatch.setattr(os, "link", paused_link)
    monkeypatch.setattr(os, "fdopen", takeover_fdopen)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(recover, client, identifier, revision, headers)
        assert old_ready.wait(10)
        with client.app.state.database_session_factory() as database:
            target = database.scalar(
                select(ReimbursementDraft).where(
                    ReimbursementDraft.recovery_source_draft_id == identifier
                )
            )
            target_id = target.id
            target.updated_at = utc_now() - timedelta(seconds=100)
            source_json = database.get(ReimbursementDraft, identifier).input_json
            frozen_hash = database.scalar(select(ReimbursementSubmission)).snapshot_sha256
            database.commit()
        second = pool.submit(recover, client, identifier, revision, headers)
        first.result(timeout=15)
        second_result = second.result(timeout=15)
    if not interrupted:
        assert second_result.status_code == 200, second_result.text
    monkeypatch.setattr(os, "link", real_link)
    monkeypatch.setattr(os, "fdopen", real_fdopen)
    resumed = recover(client, identifier, revision, headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["id"] == target_id
    assert resumed.json()["data"]["recoveryPending"] is False
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, identifier).input_json == source_json
        assert database.scalar(select(ReimbursementSubmission)).snapshot_sha256 == frozen_hash
        for file in database.scalars(
            select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == target_id)
        ):
            assert file.file_status == "ACTIVE"
            with staging.open_verified(
                file.storage_key, expected_size=file.size_bytes, expected_sha256=file.sha256
            ):
                pass


def test_recovery_rebuilds_a_corrupt_unfinalized_destination(
    enabled_manual_worker_client, monkeypatch
):
    client = enabled_manual_worker_client
    identifier, revision, headers, target_id, real_copy = _interrupt_recovery(client, monkeypatch)
    staging = client.app.state.reimbursement_staging
    with client.app.state.database_session_factory() as database:
        file = database.scalar(
            select(ReimbursementDraftFile).where(
                ReimbursementDraftFile.draft_id == target_id,
                ReimbursementDraftFile.file_status == "RESERVED",
            )
        )
        key = file.storage_key
        staging.write_bytes(
            StagingReservation(key, file.part_storage_key, file.reserved_bytes), b"bad"
        )
    monkeypatch.setattr(staging, "copy", real_copy)
    resumed = recover(client, identifier, revision, headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["id"] == target_id
    assert resumed.json()["data"]["recoveryPending"] is False
    with client.app.state.database_session_factory() as database:
        file = database.scalar(
            select(ReimbursementDraftFile).where(ReimbursementDraftFile.storage_key == key)
        )
        assert file.file_status == "ACTIVE"
        with staging.open_verified(key, expected_size=file.size_bytes, expected_sha256=file.sha256):
            pass


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_expired_cleanup_keeps_copy_ownership_until_the_writer_exits(
    enabled_manual_worker_client, monkeypatch, published, cleanup_failure
):
    client = enabled_manual_worker_client
    identifier, revision, source_files, headers = failed_form(client)
    source_usage = client.app.state.reimbursement_quota.usage().reserved_bytes
    staging = client.app.state.reimbursement_staging
    real_copy = staging.copy
    ready, release = Event(), Event()

    def paused_copy(source, destination, **kwargs):
        result = real_copy(source, destination, **kwargs) if published else None
        ready.set()
        assert release.wait(10)
        return result if published else real_copy(source, destination, **kwargs)

    monkeypatch.setattr(staging, "copy", paused_copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        copying = pool.submit(recover, client, identifier, revision, headers)
        try:
            assert ready.wait(10)
            with client.app.state.database_session_factory() as database:
                target = database.scalar(
                    select(ReimbursementDraft).where(
                        ReimbursementDraft.recovery_source_draft_id == identifier
                    )
                )
                target_id = target.id
                target.expires_at = utc_now() - timedelta(seconds=1)
                frozen_hash = database.scalar(select(ReimbursementSubmission)).snapshot_sha256
                database.commit()
            retained_usage = client.app.state.reimbursement_quota.usage().reserved_bytes
            assert retained_usage > source_usage
            assert client.app.state.reimbursement_quota.reclaim_expired() == 0
            assert client.app.state.reimbursement_quota.usage().reserved_bytes == retained_usage
            with client.app.state.database_session_factory() as database:
                assert database.get(ReimbursementDraft, target_id) is not None
                assert (
                    database.query(ReimbursementDraftFile).filter_by(draft_id=target_id).count()
                    == 2
                )
        finally:
            release.set()
        result = copying.result(timeout=10)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_EXPIRED"
    if cleanup_failure:
        real_discard = staging.discard_reservation
        attempts = 0

        def interrupted_discard(reservation):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise StagingIntegrityError("synthetic cleanup interruption")
            return real_discard(reservation)

        monkeypatch.setattr(staging, "discard_reservation", interrupted_discard)
        assert client.app.state.reimbursement_quota.reclaim_expired() == 0
        with client.app.state.database_session_factory() as database:
            assert database.get(ReimbursementDraft, target_id) is not None
    assert client.app.state.reimbursement_quota.reclaim_expired() == 1
    assert list((staging.root / "drafts" / target_id).iterdir()) == []
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == source_usage
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, target_id) is None
        assert database.scalar(select(ReimbursementSubmission)).snapshot_sha256 == frozen_hash
        for source_id in source_files:
            file = database.get(ReimbursementDraftFile, source_id)
            with staging.open_verified(
                file.storage_key, expected_size=file.size_bytes, expected_sha256=file.sha256
            ):
                pass


def test_cleanup_before_recovery_guard_prevents_any_late_file_write(
    enabled_manual_worker_client, monkeypatch
):
    client = enabled_manual_worker_client
    identifier, revision, _, headers = failed_form(client)
    staging = client.app.state.reimbursement_staging
    real_guard = staging.recovery_guard
    ready, release = Event(), Event()
    copies = []
    real_copy = staging.copy

    @contextmanager
    def delayed_guard(owner, *, exclusive=False):
        if not exclusive:
            ready.set()
            assert release.wait(10)
        with real_guard(owner, exclusive=exclusive):
            yield

    def record_copy(*args, **kwargs):
        copies.append(args[1])
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(staging, "recovery_guard", delayed_guard)
    monkeypatch.setattr(staging, "copy", record_copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        copying = pool.submit(recover, client, identifier, revision, headers)
        try:
            assert ready.wait(10)
            with client.app.state.database_session_factory() as database:
                target = database.scalar(
                    select(ReimbursementDraft).where(
                        ReimbursementDraft.recovery_source_draft_id == identifier
                    )
                )
                target_id = target.id
                target.expires_at = utc_now() - timedelta(seconds=1)
                database.commit()
            assert client.app.state.reimbursement_quota.reclaim_expired() == 1
        finally:
            release.set()
        result = copying.result(timeout=10)
    assert result.status_code == 404, result.text
    assert copies == []
    assert list((staging.root / "drafts" / target_id).iterdir()) == []


def test_recovery_stays_readonly_until_all_older_writers_have_exited(
    enabled_manual_worker_client, monkeypatch
):
    client = enabled_manual_worker_client
    identifier, revision, _, headers = failed_form(client)
    staging = client.app.state.reimbursement_staging
    source_usage = client.app.state.reimbursement_quota.usage().reserved_bytes
    real_copy = staging.copy
    ready, release = Event(), Event()
    destinations = []

    def paused_copy(source, destination, **kwargs):
        if not destinations:
            destinations.append(destination)
            ready.set()
            assert release.wait(10)
        return real_copy(source, destination, **kwargs)

    monkeypatch.setattr(staging, "copy", paused_copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(recover, client, identifier, revision, headers)
        try:
            assert ready.wait(10)
            with client.app.state.database_session_factory() as database:
                target = database.scalar(
                    select(ReimbursementDraft).where(
                        ReimbursementDraft.recovery_source_draft_id == identifier
                    )
                )
                target_id = target.id
                target.updated_at = utc_now() - timedelta(seconds=100)
                database.commit()
            second = recover(client, identifier, revision, headers)
            assert second.status_code == 200, second.text
            data = second.json()["data"]
            assert data["recoveryPending"] is True
            for suffix in ("excel-preview", "excel-preview-ticket"):
                preview = client.post(
                    f"/api/reimbursements/drafts/{target_id}/{suffix}",
                    json={"expectedRevision": data["revision"]}, headers=headers,
                )
                assert preview.status_code == 409, preview.text
                assert preview.json()["error"]["code"] == "REIMBURSEMENT_RECOVERY_PENDING"
            cleared = client.post(
                f"/api/reimbursements/drafts/{target_id}/files/clear",
                json={"expectedRevision": data["revision"]},
                headers=headers,
            )
            assert cleared.status_code == 409, cleared.text
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 409
    completed = recover(client, identifier, revision, headers)
    assert completed.status_code == 200, completed.text
    data = completed.json()["data"]
    assert data["id"] == target_id and data["recoveryPending"] is False
    cleared = client.post(
        f"/api/reimbursements/drafts/{target_id}/files/clear",
        json={"expectedRevision": data["revision"]},
        headers=headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert list((staging.root / "drafts" / target_id).iterdir()) == []
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == source_usage
