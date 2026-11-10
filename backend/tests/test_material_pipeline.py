from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from conftest import mock_login
from sqlalchemy import delete, update
from sqlalchemy.orm import Session, sessionmaker
from test_material_classification import INVOICE, _auto_upload
from test_reimbursement_files import _insert_draft

from app.core.errors import ApiError
from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile, utc_now
from app.models.session import UserSession
from app.ocr.engine import FakeOcrEngine
from app.ocr.types import OcrLine
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services import reimbursement_files
from app.services.reimbursement_drafts import DraftActor, validate_draft_file_references
from app.services.reimbursement_files import recognize_draft_file


def _ocr(client, csrf, draft_id, file_id, revision, *, overlap=True):
    return client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": revision, "tripYear": 2026, "allowUploadOverlap": overlap},
    )


def _setup(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        ocr_enabled=True,
        ocr_engine=FakeOcrEngine({"*": [OcrLine(INVOICE, 1)]}),
    )
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    first = _auto_upload(client, csrf, draft_id, name="first.png")
    return client, csrf, draft_id, first


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("change", ["input", "retired", "recovery", "locked", "expired"])
def test_ocr_completion_claims_current_state_before_writing(
    client_factory, monkeypatch, overlap, change
):
    client, csrf, draft_id, first = _setup(client_factory)
    file_id = first["file"]["id"]
    if not overlap:
        # Ordinary UI retry of a file whose classification needs confirmation.
        with client.app.state.database_session_factory() as database:
            file = database.get(ReimbursementDraftFile, file_id)
            file.ocr_status = "FAILED"
            file.ocr_result_json = json.dumps({
                "_materialClassification": {"status": "needs_confirmation", "kind": "unknown"}
            })
            database.commit()

    ready_to_finish = False
    concurrent_input = None
    original_ocr = client.app.state.ocr_service.recognize_material_file
    original_require = reimbursement_files.require_owned_draft

    async def recognize(*args, **kwargs):
        nonlocal ready_to_finish
        result = await original_ocr(*args, **kwargs)
        ready_to_finish = True
        return result

    def change_after_completion_read(database, **kwargs):
        nonlocal ready_to_finish, concurrent_input
        draft = original_require(database, **kwargs)
        if ready_to_finish:
            ready_to_finish = False
            # Commit from a second SQLite Session after the completion SELECT.
            # This is the micro-window a read-only revision check cannot protect.
            changes = {}
            if change == "input":
                value = json.loads(draft.input_json)
                value["items"][0]["amount"] = "99.00"
                concurrent_input = json.dumps(value, ensure_ascii=False, sort_keys=True)
                changes = {"input_json": concurrent_input, "revision": draft.revision + 1}
            elif change == "retired":
                changes = {"workspace_retired": True}
            elif change == "recovery":
                changes = {"recovery_pending": True}
            elif change == "locked":
                changes = {"status": "LOCKED", "locked_at": utc_now()}
            else:
                changes = {"expires_at": utc_now() - timedelta(seconds=1)}
            with client.app.state.database_session_factory() as other:
                changed = other.execute(
                    update(ReimbursementDraft)
                    .where(
                        ReimbursementDraft.id == draft_id,
                        ReimbursementDraft.revision == draft.revision,
                    )
                    .values(**changes)
                )
                assert changed.rowcount == 1
                other.commit()
        return draft

    monkeypatch.setattr(client.app.state.ocr_service, "recognize_material_file", recognize)
    monkeypatch.setattr(reimbursement_files, "require_owned_draft", change_after_completion_read)
    response = _ocr(client, csrf, draft_id, file_id, 2, overlap=overlap)

    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, file_id)
        if change == "input":
            assert draft.input_json == concurrent_input
            assert draft.revision == (3 if overlap else 4)
        assert file.processing_role == "ATTACHMENT_ONLY"
        assert file.ocr_status == "FAILED"
        assert "OCR_RESULT_CONFLICT" in file.ocr_result_json
    assert response.status_code == 409, response.text


def test_upload_finishes_while_single_ocr_slot_is_busy_and_both_results_persist(
    client_factory, monkeypatch
):
    client, csrf, draft_id, first = _setup(client_factory)
    entered, release = threading.Event(), threading.Event()
    runner = client.app.state.process_runner
    original_run = runner._run_sync
    calls = []

    async def blocking_worker(function, *args, **kwargs):
        calls.append(function.__name__)
        entered.set()
        await asyncio.to_thread(release.wait)
        return await original_run(function, *args, **kwargs)

    monkeypatch.setattr(runner, "_run_sync", blocking_worker)
    with client.app.state.database_session_factory() as database:
        initial_input = database.get(ReimbursementDraft, draft_id).input_json
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_ocr, client, csrf, draft_id, first["file"]["id"], 2)
        try:
            assert entered.wait(5), "OCR must occupy the heavy process slot"
            second = _auto_upload(client, csrf, draft_id, revision=2, name="second.png")
            assert not future.done(), "upload B must finish before OCR A is released"
            assert second["revision"] == 3
            assert second["file"]["ocrResult"] is None
            assert "_pipeline" not in json.dumps(second)
            duplicate = _ocr(client, csrf, draft_id, first["file"]["id"], 3)
            assert duplicate.status_code == 409
            assert duplicate.json()["error"]["code"] == "REIMBURSEMENT_FILE_PIPELINE_NOT_ALLOWED"
            with client.app.state.database_session_factory() as database:
                draft = database.get(ReimbursementDraft, draft_id)
                assert draft.revision == 3 and draft.input_json == initial_input
                with pytest.raises(ApiError, match="识别完成"):
                    validate_draft_file_references(
                        database,
                        draft_id=draft_id,
                        draft_input=ReimbursementDraftInput.model_validate_json(initial_input),
                        require_submission_proofs=True,
                    )
        finally:
            release.set()
        response = future.result(10)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["revision"] == 3
    # An old revision from before B uploaded is safe only on the new-file path.
    recognized_b = _ocr(client, csrf, draft_id, second["file"]["id"], 2)
    assert recognized_b.status_code == 200, recognized_b.text
    assert recognized_b.json()["data"]["revision"] == 3
    files = client.get(f"/api/reimbursements/drafts/{draft_id}/files").json()["data"]
    assert len(calls) == 2
    assert files["revision"] == 3
    assert all(file["ocrStatus"] == "COMPLETE" for file in files["items"])
    assert all(file["ocrResult"]["amount"] == "12.30" for file in files["items"])
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, draft_id).input_json == initial_input


@pytest.mark.parametrize("change", ["input", "role", "delete", "locked", "department", "logout"])
def test_pipeline_completion_never_overwrites_changed_state(client_factory, monkeypatch, change):
    client, csrf, draft_id, first = _setup(client_factory)
    file_id = first["file"]["id"]
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.ocr_service.recognize_material_file

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.to_thread(release.wait)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.ocr_service, "recognize_material_file", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_ocr, client, csrf, draft_id, file_id, 2)
        try:
            assert entered.wait(5)
            with client.app.state.database_session_factory() as database:
                draft = database.get(ReimbursementDraft, draft_id)
                file = database.get(ReimbursementDraftFile, file_id)
                if change == "input":
                    value = json.loads(draft.input_json)
                    value["items"][0]["description"] = "用户新修改，不能被旧OCR覆盖"
                    draft.input_json = json.dumps(value, ensure_ascii=False)
                    draft.revision += 1
                elif change == "role":
                    file.attachment_kind = "payment_proof"
                    file.ocr_status = "NOT_REQUESTED"
                    file.ocr_result_json = None
                elif change == "delete":
                    file.file_status = "PURGED"
                    file.purged_at = utc_now()
                elif change == "locked":
                    draft.status = "LOCKED"
                    draft.locked_at = utc_now()
                elif change == "department":
                    session = database.query(UserSession).one()
                    session.current_department_id = "other-department"
                else:
                    database.execute(delete(UserSession))
                expected_input = draft.input_json
                database.commit()
        finally:
            release.set()
        response = future.result(10)
    assert response.status_code in {401, 404, 409}, response.text
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, file_id)
        assert draft.input_json == expected_input
        assert file.ocr_status != "COMPLETE"
        if change == "role":
            assert file.attachment_kind == "payment_proof" and file.ocr_result_json is None
        if change == "delete":
            assert file.file_status == "PURGED"


def test_pipeline_admission_rejects_edited_input_references_and_future_revision(client_factory):
    client, csrf, draft_id, first = _setup(client_factory)
    file_id = first["file"]["id"]
    future = _ocr(client, csrf, draft_id, file_id, 100)
    assert future.status_code == 409
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        value = json.loads(draft.input_json)
        value["items"][0]["description"] = "更新后的输入"
        draft.input_json = json.dumps(value, ensure_ascii=False)
        draft.revision += 1
        database.commit()
    assert _ocr(client, csrf, draft_id, file_id, 2).status_code == 409
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file.ocr_status == "NOT_REQUESTED"
        value["dismissedOcrFileIds"] = [file_id]
        database.get(ReimbursementDraft, draft_id).input_json = json.dumps(value)
        database.commit()
    reference = _ocr(client, csrf, draft_id, file_id, 3)
    assert reference.status_code == 409
    assert reference.json()["error"]["code"] == "REIMBURSEMENT_FILE_PIPELINE_NOT_ALLOWED"


def test_ocr_can_finish_while_another_upload_is_still_writing(client_factory, monkeypatch):
    client, csrf, draft_id, first = _setup(client_factory)
    entered, release = threading.Event(), threading.Event()
    staging = client.app.state.reimbursement_staging
    original_write = staging.write_stream

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(staging, "write_stream", blocked_write)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _auto_upload, client, csrf, draft_id, revision=2, name="second.png"
        )
        try:
            assert entered.wait(5)
            completed = _ocr(client, csrf, draft_id, first["file"]["id"], 2)
            assert completed.status_code == 200, completed.text
            assert completed.json()["data"]["revision"] == 3
            assert not future.done()
        finally:
            release.set()
        uploaded = future.result(10)
    assert uploaded["revision"] == 3
    assert uploaded["file"]["status"] == "ACTIVE"


def test_old_strict_ocr_still_rejects_upload_revision_drift(client_factory):
    client, csrf, draft_id, first = _setup(client_factory)
    _auto_upload(client, csrf, draft_id, revision=2)
    response = _ocr(client, csrf, draft_id, first["file"]["id"], 2, overlap=False)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REVISION_CONFLICT"


def test_pipeline_flag_is_strict_boolean_and_old_files_cannot_opt_in(client_factory):
    client, csrf, draft_id, first = _setup(client_factory)
    file_id = first["file"]["id"]
    invalid = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2, "allowUploadOverlap": "true"},
    )
    assert invalid.status_code == 422
    assert _ocr(client, csrf, draft_id, file_id, 2).status_code == 200
    assert _ocr(client, csrf, draft_id, file_id, 2).status_code == 409


@pytest.mark.parametrize("phase", ["admission", "completion"])
def test_pipeline_guard_accepts_upload_revision_change_after_draft_read(
    client_factory, monkeypatch, phase
):
    client, csrf, draft_id, first = _setup(client_factory)
    original_guard = reimbursement_files._guard_ocr_draft
    calls = 0

    def reserve_between_read_and_guard(database, **kwargs):
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "admission" else 2):
            # Deterministically model the upload reservation's independent
            # committed metadata revision in this exact micro-window.
            with client.app.state.database_session_factory() as other:
                draft = other.get(ReimbursementDraft, draft_id)
                draft.revision += 1
                other.commit()
        return original_guard(database, **kwargs)

    monkeypatch.setattr(
        reimbursement_files, "_guard_ocr_draft", reserve_between_read_and_guard
    )
    result = _ocr(client, csrf, draft_id, first["file"]["id"], 2)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["revision"] == 3


def test_late_failure_cleanup_cannot_overwrite_a_new_marker_after_its_read(client_factory):
    client, _csrf, draft_id, first = _setup(client_factory)
    file_id = first["file"]["id"]
    original_factory = client.app.state.database_session_factory
    old_marker = json.dumps({"operationId": "old-operation"})
    new_marker = json.dumps({"operationId": "new-operation"})
    with original_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        file.ocr_status = "RUNNING"
        file.ocr_result_json = old_marker
        database.commit()

    class ReplacedAfterReadSession(Session):
        def scalar(self, *args, **kwargs):
            result = super().scalar(*args, **kwargs)
            if isinstance(result, ReimbursementDraftFile):
                with original_factory() as other:
                    other.get(ReimbursementDraftFile, file_id).ocr_result_json = new_marker
                    other.commit()
            return result

    racing_factory = sessionmaker(
        bind=client.app.state.database_engine, class_=ReplacedAfterReadSession
    )
    reimbursement_files._set_ocr_failed_without_revision(
        racing_factory,
        DraftActor("corp-fixed", "mock-user", "100", "测试部门"),
        draft_id,
        file_id,
        old_marker,
        "OCR_TEST_FAILURE",
        "old failure",
    )
    with original_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file.ocr_status == "RUNNING"
        assert file.ocr_result_json == new_marker


@pytest.mark.asyncio
async def test_pipeline_cancellation_after_another_upload_releases_copy_and_can_retry(
    client_factory,
):
    client, csrf, draft_id, first = _setup(client_factory)
    entered = asyncio.Event()

    class Blocked:
        async def recognize_material_file(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        recognize_draft_file(
            actor=DraftActor("corp-fixed", "mock-user", "100", "测试部门"),
            draft_id=draft_id,
            file_id=first["file"]["id"],
            expected_revision=2,
            reference_year=2026,
            settings=client.app.state.settings,
            session_factory=client.app.state.database_session_factory,
            staging=client.app.state.reimbursement_staging,
            ocr_service=Blocked(),
            allow_upload_overlap=True,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    second = _auto_upload(client, csrf, draft_id, revision=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, draft_id).revision == 3
        file = database.get(ReimbursementDraftFile, first["file"]["id"])
        assert file.ocr_status == "FAILED"
        assert (
            json.loads(file.ocr_result_json)["_materialClassification"]["status"]
            == "needs_confirmation"
        )
    assert not list((client.app.state.settings.temp_dir / ".spool").glob("ocr-*"))
    assert _ocr(client, csrf, draft_id, second["file"]["id"], 3).status_code == 200
    assert _ocr(client, csrf, draft_id, first["file"]["id"], 3).status_code == 409
    retried = _ocr(client, csrf, draft_id, first["file"]["id"], 3, overlap=False)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["revision"] == 4
