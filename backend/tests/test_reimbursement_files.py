from __future__ import annotations

import asyncio
import io
import json
import stat
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import mock_login
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import delete, func, select, update

from app.api import reimbursement_files as reimbursement_files_api
from app.core.errors import ApiError
from app.database.session import get_db
from app.excel.template_contract import EXCEL_TEMPLATE
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementSubmission,
    ReimbursementUpload,
    utc_now,
)
from app.models.session import UserSession
from app.ocr.engine import FakeOcrEngine
from app.ocr.types import OcrLine
from app.services.file_coordination import SessionFilesRetired, UploadBusy
from app.services.reimbursement_drafts import DraftActor
from app.services.reimbursement_files import recognize_draft_file
from app.services.reimbursement_quota import DraftFileOwner
from app.services.reimbursement_staging import StagingLayoutError
from app.services.sessions import require_fresh_active_file_session


def _image_bytes(
    image_format: str = "PNG",
    color: str | tuple[int, int, int] = "white",
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 20), color).save(output, format=image_format)
    return output.getvalue()


def _named_image_bytes(name: str) -> bytes:
    name_seed = sum(name.encode("utf-8")) % 256
    return _image_bytes(
        color=(name_seed, (name_seed * 3) % 256, (name_seed * 7) % 256),
    )


def _draft_input(*, amount: str = "44.89") -> dict[str, object]:
    return {
        "ocrDispositionVersion": 1,
        "companyValue": "北京",
        "budgetCodeValue": "26007",
        "project": {"mode": "manual", "text": "P-001 示例项目"},
        "trip": {
            "tripType": "business",
            "startDate": "2026-09-01",
            "startTime": "09:00",
            "endDate": "2026-09-02",
            "endTime": "18:00",
        },
        "items": [
            {
                "category": "other",
                "date": "2026-09-01",
                "displayDate": "2026-09-01",
                "description": "打车费",
                "amount": amount,
                "receiptCount": 1,
            }
        ],
        "dismissedOcrFileIds": [],
    }


def _insert_draft(client, *, revision: int = 1, amount: str = "44.89") -> str:
    with client.app.state.database_session_factory() as database:
        draft = ReimbursementDraft(
            corp_id="corp-fixed",
            owner_user_id="mock-user",
            status="DRAFT",
            revision=revision,
            department_id="100",
            department_name="测试部门",
            template_process_code="PROC-REIMBURSEMENT",
            template_config_version=1,
            schema_fingerprint="a" * 64,
            input_json=json.dumps(
                _draft_input(amount=amount),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            related_instance_ids_json="[]",
            expires_at=utc_now() + timedelta(days=1),
        )
        database.add(draft)
        database.commit()
        return draft.id


def _upload(
    client,
    csrf: str,
    draft_id: str,
    *,
    revision: int,
    name: str = "receipt.png",
    content: bytes | None = None,
    content_type: str = "application/octet-stream",
    role: str = "EXPENSE_SOURCE",
    attachment_kind: str = "other",
):
    if content is None:
        content = _named_image_bytes(name)
    return client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        params={
            "expectedRevision": revision,
            "role": role,
            "attachmentKind": attachment_kind,
        },
        headers={"X-CSRF-Token": csrf},
        files=[("files[]", (name, content, content_type))],
    )


def test_upload_is_durable_private_and_recovers_after_restart(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)

    uploaded = _upload(client, csrf, draft_id, revision=1, content_type="text/plain")

    assert uploaded.status_code == 201, uploaded.text
    data = uploaded.json()["data"]
    assert data["revision"] == 2
    assert data["file"]["role"] == "EXPENSE_SOURCE"
    assert data["file"]["ocrStatus"] == "NOT_REQUESTED"
    assert data["file"]["ocrResult"] is None
    assert "storage" not in uploaded.text.lower()
    file_id = data["file"]["id"]

    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        stored = database.get(ReimbursementDraftFile, file_id)
        assert draft is not None and draft.revision == 2
        assert stored is not None and stored.file_status == "ACTIVE"
        storage_path = client.app.state.reimbursement_staging.root / stored.storage_key
        assert stat.S_IMODE(storage_path.stat().st_mode) == 0o600

    restarted = client_factory(auth_mock_enabled=True)
    mock_login(restarted)
    listed = restarted.get(f"/api/reimbursements/drafts/{draft_id}/files")
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == {
        "draftId": draft_id,
        "revision": 2,
        "items": [data["file"]],
    }


def test_upload_rejects_duplicate_content_without_changing_the_draft(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    content = _image_bytes()

    first = _upload(
        client,
        csrf,
        draft_id,
        revision=1,
        name="原始票据.png",
        content=content,
    )
    duplicate = _upload(
        client,
        csrf,
        draft_id,
        revision=2,
        name="改名后的同一票据.png",
        content=content,
        role="ATTACHMENT_ONLY",
    )

    assert first.status_code == 201, first.text
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"] == {
        "code": "REIMBURSEMENT_FILE_DUPLICATE",
        "message": "该文件已在本次报销中上传，已跳过：原始票据.png",
    }

    listed = client.get(f"/api/reimbursements/drafts/{draft_id}/files")
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["revision"] == 2
    assert [item["name"] for item in listed.json()["data"]["items"]] == ["原始票据.png"]

    with client.app.state.database_session_factory() as database:
        assert (
            database.scalar(
                select(func.count())
                .select_from(ReimbursementDraftFile)
                .where(ReimbursementDraftFile.draft_id == draft_id)
            )
            == 1
        )


def test_upload_allows_a_reused_name_when_the_content_changed(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)

    first = _upload(
        client,
        csrf,
        draft_id,
        revision=1,
        name="发票.png",
        content=_image_bytes(color="white"),
    )
    changed = _upload(
        client,
        csrf,
        draft_id,
        revision=2,
        name="发票.png",
        content=_image_bytes(color="black"),
    )

    assert first.status_code == 201, first.text
    assert changed.status_code == 201, changed.text
    assert changed.json()["data"]["revision"] == 3


def test_locked_draft_lists_only_purged_file_metadata_referenced_by_submitted_input(
    client_factory,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    source = _upload(client, csrf, draft_id, revision=1, name="打车发票.png")
    itinerary = _upload(
        client,
        csrf,
        draft_id,
        revision=2,
        name="打车行程单.png",
        role="ATTACHMENT_ONLY",
    )
    deleted_before_submit = _upload(
        client,
        csrf,
        draft_id,
        revision=3,
        name="已删除材料.png",
        role="ATTACHMENT_ONLY",
    )
    source_id = source.json()["data"]["file"]["id"]
    itinerary_id = itinerary.json()["data"]["file"]["id"]
    deleted_id = deleted_before_submit.json()["data"]["file"]["id"]

    with client.app.state.database_session_factory() as database:
        draft_record = database.get(ReimbursementDraft, draft_id)
        assert draft_record is not None
        input_data = json.loads(draft_record.input_json)
        input_data["items"][0].update(
            {
                "sourceFileId": source_id,
                "requiresItinerary": True,
                "transportType": "ride_hailing",
                "itineraryFileIds": [itinerary_id],
            }
        )
        draft_record.input_json = json.dumps(
            input_data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        draft_record.status = "LOCKED"
        draft_record.locked_at = utc_now()
        for file_id, kind in (
            (source_id, "other"),
            (itinerary_id, "itinerary"),
            (deleted_id, "other"),
        ):
            file_record = database.get(ReimbursementDraftFile, file_id)
            assert file_record is not None
            file_record.attachment_kind = kind
            file_record.file_status = "PURGED"
            file_record.purged_at = utc_now()
        database.commit()

    listed = client.get(f"/api/reimbursements/drafts/{draft_id}/files")

    assert listed.status_code == 200, listed.text
    files = listed.json()["data"]["items"]
    assert [item["id"] for item in files] == [source_id, itinerary_id]
    assert [item["status"] for item in files] == ["PURGED", "PURGED"]


def test_upload_releases_sync_session_before_reading_multipart(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    database = client.app.state.database_session_factory()

    def database_override():
        yield database

    async def stop_at_multipart(*_args, **_kwargs):
        assert not database.in_transaction()
        raise ApiError("BOUNDARY_REACHED", "boundary reached", 418)

    client.app.dependency_overrides[get_db] = database_override
    monkeypatch.setattr(reimbursement_files_api, "parse_upload_files", stop_at_multipart)
    try:
        response = _upload(client, csrf, draft_id, revision=1)
    finally:
        client.app.dependency_overrides.pop(get_db, None)
        database.close()

    assert response.status_code == 418, response.text
    assert response.json()["error"]["code"] == "BOUNDARY_REACHED"


def test_upload_rechecks_session_inside_lease_and_holds_it_through_persist(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    database = client.app.state.database_session_factory()
    events: list[str] = []

    class RecordingCoordinator:
        active = False

        @asynccontextmanager
        async def async_upload_lease(self, session_id_hash: str):
            assert len(session_id_hash) == 64
            self.active = True
            events.append("lease")
            try:
                yield
            finally:
                self.active = False
                events.append("release")

    coordinator = RecordingCoordinator()

    def database_override():
        yield database

    def checked_fresh_session(session_factory, settings, session_id_hash):
        assert coordinator.active
        assert not database.in_transaction()
        events.append("fresh")
        return require_fresh_active_file_session(session_factory, settings, session_id_hash)

    async def stop_at_persist(**_kwargs):
        assert coordinator.active
        assert not database.in_transaction()
        assert events == ["lease", "fresh"]
        await asyncio.sleep(0)
        assert coordinator.active
        events.append("persist")
        raise ApiError("BOUNDARY_REACHED", "boundary reached", 418)

    client.app.state.file_coordinator = coordinator
    client.app.dependency_overrides[get_db] = database_override
    monkeypatch.setattr(
        reimbursement_files_api,
        "require_fresh_active_file_session",
        checked_fresh_session,
    )
    monkeypatch.setattr(reimbursement_files_api, "persist_draft_upload", stop_at_persist)
    try:
        response = _upload(client, csrf, draft_id, revision=1)
    finally:
        client.app.dependency_overrides.pop(get_db, None)
        database.close()

    assert response.status_code == 418, response.text
    assert response.json()["error"]["code"] == "BOUNDARY_REACHED"
    assert events == ["lease", "fresh", "persist", "release"]


def test_upload_rechecks_the_session_after_initial_authorization(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    original_require_owned_draft = reimbursement_files_api.require_owned_draft
    multipart_consumed = False

    def remove_session_after_initial_check(*args, **kwargs):
        draft = original_require_owned_draft(*args, **kwargs)
        with client.app.state.database_session_factory() as database:
            database.execute(delete(UserSession))
            database.commit()
        return draft

    async def record_multipart_consumption(*_args, **_kwargs):
        nonlocal multipart_consumed
        multipart_consumed = True
        raise AssertionError("multipart body must not be consumed for a retired session")

    monkeypatch.setattr(
        reimbursement_files_api,
        "require_owned_draft",
        remove_session_after_initial_check,
    )
    monkeypatch.setattr(
        reimbursement_files_api,
        "parse_upload_files",
        record_multipart_consumption,
    )

    response = _upload(client, csrf, draft_id, revision=1)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert multipart_consumed is False
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (SessionFilesRetired("session retired"), 401, "UNAUTHORIZED"),
        (UploadBusy("admission busy"), 429, "UPLOAD_BUSY"),
    ],
)
def test_upload_maps_admission_failures_before_consuming_multipart(
    client_factory,
    monkeypatch,
    failure: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    multipart_consumed = False

    class RejectingCoordinator:
        @asynccontextmanager
        async def async_upload_lease(self, _session_id_hash: str):
            raise failure
            yield

    async def record_multipart_consumption(*_args, **_kwargs):
        nonlocal multipart_consumed
        multipart_consumed = True
        raise AssertionError("multipart body must not be consumed without admission")

    client.app.state.file_coordinator = RejectingCoordinator()
    monkeypatch.setattr(
        reimbursement_files_api,
        "parse_upload_files",
        record_multipart_consumption,
    )

    response = _upload(client, csrf, draft_id, revision=1)

    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == expected_code
    assert multipart_consumed is False
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0


def test_upload_reuses_magic_validation_and_attachment_role_blocks_ocr(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True, ocr_enabled=True, ocr_engine=FakeOcrEngine())
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)

    mismatch = _upload(client, csrf, draft_id, revision=1, name="wrong.jpg")
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == "FILE_TYPE_MISMATCH"
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0

    attached = _upload(
        client,
        csrf,
        draft_id,
        revision=1,
        role="ATTACHMENT_ONLY",
    )
    assert attached.status_code == 201, attached.text
    file_id = attached.json()["data"]["file"]["id"]
    recognized = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2},
    )
    assert recognized.status_code == 409
    assert recognized.json()["error"]["code"] == "REIMBURSEMENT_FILE_OCR_NOT_ALLOWED"


def test_upload_quota_failure_creates_no_persistent_bytes(client_factory) -> None:
    client = client_factory(
        auth_mock_enabled=True,
        upload_max_file_bytes=4096,
        upload_max_request_bytes=8192,
        session_max_bytes=4096,
        temp_storage_max_bytes=16384,
        temp_storage_reserve_bytes=8192,
        reimbursement_staging_max_bytes=8192,
    )
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    quota_owner_draft_id = _insert_draft(client)
    quota = client.app.state.reimbursement_quota
    for sort_order in range(2):
        quota.reserve_draft_file(
            DraftFileOwner(
                "corp-fixed",
                "mock-user",
                quota_owner_draft_id,
                sort_order + 1,
            ),
            sort_order=sort_order,
            processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
            original_name=f"reserved-{sort_order}.png",
            extension="png",
            media_type="image/png",
            reserved_bytes=4096,
            expires_at=utc_now() + timedelta(minutes=5),
        )

    response = _upload(client, csrf, draft_id, revision=1)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REIMBURSEMENT_STORAGE_FULL"
    assert quota.usage().reserved_bytes == 8192
    assert not [
        path for path in client.app.state.reimbursement_staging.root.rglob("*") if path.is_file()
    ]
    with client.app.state.database_session_factory() as database:
        assert database.scalar(select(func.count()).select_from(ReimbursementDraftFile)) == 2


def test_upload_revision_drift_discards_written_object_and_reservation(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    quota = client.app.state.reimbursement_quota
    original_finalize = quota.finalize_draft_file

    def finalize_after_competing_update(owner, reservation, staged, *, actor):
        with client.app.state.database_session_factory() as database:
            database.execute(
                update(ReimbursementDraft)
                .where(ReimbursementDraft.id == draft_id)
                .values(revision=3)
            )
            database.commit()
        return original_finalize(owner, reservation, staged, actor=actor)

    monkeypatch.setattr(quota, "finalize_draft_file", finalize_after_competing_update)

    response = _upload(client, csrf, draft_id, revision=1)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REVISION_CONFLICT"
    assert quota.usage().reserved_bytes == 0
    assert not [
        path for path in client.app.state.reimbursement_staging.root.rglob("*") if path.is_file()
    ]
    with client.app.state.database_session_factory() as database:
        file = database.scalar(select(ReimbursementDraftFile))
        assert file is not None and file.file_status == "PURGED"
        draft = database.get(ReimbursementDraft, draft_id)
        assert draft is not None and draft.revision == 3


def test_batch_clear_uses_one_revision_and_preserves_manual_input(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    ids = []
    for index in range(3):
        response = _upload(client, csrf, draft_id, revision=index + 1, name=f"file{index}.png")
        assert response.status_code == 201, response.text
        ids.append(response.json()["data"]["file"]["id"])
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        data = _draft_input()
        manual = dict(data["items"][0])
        manual["description"] = "手工费用"
        manual["itineraryFileIds"] = [ids[1]]
        manual["paymentProofFileIds"] = [ids[2]]
        data["items"][0]["sourceFileId"] = ids[0]
        data["items"].append(manual)
        data["dismissedOcrFileIds"] = [ids[1]]
        draft.input_json = json.dumps(data)
        database.commit()
    url = f"/api/reimbursements/drafts/{draft_id}/files/clear"
    assert client.post(url, json={"expectedRevision": 4}).status_code == 403
    assert (
        client.post(url, headers={"X-CSRF-Token": csrf}, json={"expectedRevision": 3}).status_code
        == 409
    )
    cleared = client.post(url, headers={"X-CSRF-Token": csrf}, json={"expectedRevision": 4})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["revision"] == 5
    assert set(cleared.json()["data"]["deletedFileIds"]) == set(ids)
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0
    after = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["input"]
    assert len(after["items"]) == 1
    assert after["items"][0]["description"] == "手工费用"
    assert after["items"][0]["itineraryFileIds"] == []
    assert after["items"][0]["paymentProofFileIds"] == []
    assert after["dismissedOcrFileIds"] == []
    assert after["trip"] == data["trip"]
    again = client.post(url, headers={"X-CSRF-Token": csrf}, json={"expectedRevision": 5})
    assert again.status_code == 200
    assert again.json()["data"] == {"draftId": draft_id, "revision": 5, "deletedFileIds": []}
    assert _upload(client, csrf, draft_id, revision=5).status_code == 201


def test_batch_clear_parallel_cleanup_failure_is_resumable(client_factory, monkeypatch) -> None:
    from threading import Barrier, Lock

    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    for index in range(4):
        assert (
            _upload(
                client, csrf, draft_id, revision=index + 1, name=f"receipt{index}.png"
            ).status_code
            == 201
        )
    staging = client.app.state.reimbursement_staging
    original = staging.delete
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    active = 0
    peak = 0
    calls = 0

    def controlled_delete(*args, **kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            peak = max(peak, active)
            calls += 1
            call = calls
        try:
            barrier.wait()
            if call == 1:
                raise StagingLayoutError("simulated failure")
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(staging, "delete", controlled_delete)
    url = f"/api/reimbursements/drafts/{draft_id}/files/clear"
    failed = client.post(url, headers={"X-CSRF-Token": csrf}, json={"expectedRevision": 5})
    assert failed.status_code >= 400
    assert calls == 4 and peak == 2
    with client.app.state.database_session_factory() as database:
        files = list(
            database.scalars(
                select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == draft_id)
            )
        )
        assert sorted(file.file_status for file in files) == [
            "DELETING",
            "PURGED",
            "PURGED",
            "PURGED",
        ]
        assert database.get(ReimbursementDraft, draft_id).revision == 6
    assert client.app.state.reimbursement_quota.usage().reserved_bytes > 0
    monkeypatch.setattr(staging, "delete", original)
    retry = client.post(url, headers={"X-CSRF-Token": csrf}, json={"expectedRevision": 6})
    assert retry.status_code == 200, retry.text
    assert len(retry.json()["data"]["deletedFileIds"]) == 1
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0


def test_batch_clear_rejects_running_ocr_without_partial_changes(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    for index in range(2):
        uploaded = _upload(client, csrf, draft_id, revision=index + 1, name=f"{index}.png")
        assert uploaded.status_code == 201
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, uploaded.json()["data"]["file"]["id"])
        file.ocr_status = "RUNNING"
        database.commit()
    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/clear",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 3},
    )
    assert response.status_code == 409
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, draft_id).revision == 3
        assert all(
            file.file_status == "ACTIVE"
            for file in database.scalars(
                select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == draft_id)
            )
        )


def test_batch_clear_allows_stale_running_ocr_after_restart(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    stale_upload = _upload(client, csrf, draft_id, revision=1, name="stale.png")
    current_upload = _upload(client, csrf, draft_id, revision=2, name="current.png")
    assert stale_upload.status_code == 201
    assert current_upload.status_code == 201
    stale_file_id = stale_upload.json()["data"]["file"]["id"]
    current_file_id = current_upload.json()["data"]["file"]["id"]

    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, stale_file_id)
        file.ocr_status = "RUNNING"
        file.updated_at = utc_now() - timedelta(
            seconds=client.app.state.settings.ocr_timeout_seconds + 31
        )
        database.commit()

    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/clear",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 3},
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["data"]["deletedFileIds"]) == {
        stale_file_id,
        current_file_id,
    }
    with client.app.state.database_session_factory() as database:
        stale_file = database.get(ReimbursementDraftFile, stale_file_id)
        assert stale_file.file_status == "PURGED"
        assert stale_file.ocr_status == "FAILED"
        assert database.get(ReimbursementDraftFile, current_file_id).file_status == "PURGED"


def test_delete_allows_stale_running_ocr_after_restart(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    assert uploaded.status_code == 201
    file_id = uploaded.json()["data"]["file"]["id"]

    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        file.ocr_status = "RUNNING"
        file.updated_at = utc_now() - timedelta(
            seconds=client.app.state.settings.ocr_timeout_seconds + 31
        )
        database.commit()

    response = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        params={"expectedRevision": 2},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["deletedFileId"] == file_id
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file.file_status == "PURGED"
        assert file.ocr_status == "FAILED"


def test_listing_exposes_stale_running_ocr_without_mutating_from_get(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    assert uploaded.status_code == 201
    file_id = uploaded.json()["data"]["file"]["id"]

    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        file.ocr_status = "RUNNING"
        file.ocr_result_json = json.dumps({"operationId": "interrupted-operation"})
        file.updated_at = utc_now() - timedelta(
            seconds=client.app.state.settings.ocr_timeout_seconds + 31
        )
        database.commit()

    response = client.get(f"/api/reimbursements/drafts/{draft_id}/files")

    assert response.status_code == 200, response.text
    listed = response.json()["data"]["items"][0]
    assert listed["ocrStatus"] == "RUNNING"
    assert listed["ocrStale"] is True
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraftFile, file_id).ocr_status == "RUNNING"


def test_listing_exposes_stale_hotel_ocr_as_retryable(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(
        client,
        csrf,
        draft_id,
        revision=1,
        role="ATTACHMENT_ONLY",
        attachment_kind="hotel_bill",
    )
    assert uploaded.status_code == 201
    file_id = uploaded.json()["data"]["file"]["id"]
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        file.ocr_status = "RUNNING"
        file.ocr_result_json = json.dumps({"operationId": "interrupted-hotel"})
        file.updated_at = utc_now() - timedelta(
            seconds=client.app.state.settings.ocr_timeout_seconds + 31
        )
        database.commit()

    response = client.get(f"/api/reimbursements/drafts/{draft_id}/files")

    assert response.status_code == 200, response.text
    listed = response.json()["data"]["items"][0]
    assert listed["attachmentKind"] == "hotel_bill"
    assert listed["ocrStatus"] == "RUNNING"
    assert listed["ocrStale"] is True


def test_patch_and_delete_use_revision_cas_and_delete_physical_first(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    file_id = uploaded.json()["data"]["file"]["id"]

    wrong_name = client.patch(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2, "name": "changed.pdf"},
    )
    assert wrong_name.status_code == 400
    assert wrong_name.json()["error"]["code"] == "FILE_TYPE_MISMATCH"

    changed = client.patch(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        headers={"X-CSRF-Token": csrf},
        json={
            "expectedRevision": 2,
            "name": "行程单.png",
            "role": "ATTACHMENT_ONLY",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["revision"] == 3
    assert changed.json()["data"]["file"]["name"] == "行程单.png"

    stale = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        params={"expectedRevision": 2},
        headers={"X-CSRF-Token": csrf},
    )
    assert stale.status_code == 409

    deleted = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        params={"expectedRevision": 3},
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["revision"] == 4
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file is not None and file.file_status == "PURGED"
        assert not (client.app.state.reimbursement_staging.root / file.storage_key).exists()

    repeated = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        params={"expectedRevision": 4},
        headers={"X-CSRF-Token": csrf},
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"]["revision"] == 4


def test_delete_file_atomically_clears_linked_item_and_ocr_disposition(
    client_factory,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    first = _upload(client, csrf, draft_id, revision=1)
    second = _upload(client, csrf, draft_id, revision=2, name="second.png")
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    linked_file_id = first.json()["data"]["file"]["id"]
    dismissed_file_id = second.json()["data"]["file"]["id"]
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        assert draft is not None
        input_data = _draft_input()
        input_data["items"][0]["sourceFileId"] = linked_file_id
        input_data["dismissedOcrFileIds"] = [dismissed_file_id]
        draft.input_json = json.dumps(
            input_data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        database.commit()

    deleted_linked = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{linked_file_id}",
        params={"expectedRevision": 3},
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted_linked.status_code == 200, deleted_linked.text
    after_linked = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert after_linked["input"]["items"] == []
    assert after_linked["input"]["dismissedOcrFileIds"] == [dismissed_file_id]
    assert after_linked["totals"]["expenseTotal"] == "0.00"
    assert after_linked["totals"]["totalAmount"] == "200.00"

    deleted_dismissed = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{dismissed_file_id}",
        params={"expectedRevision": 4},
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted_dismissed.status_code == 200, deleted_dismissed.text
    after_dismissed = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert after_dismissed["input"]["dismissedOcrFileIds"] == []


def test_delete_releases_sync_session_before_physical_cleanup(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    file_id = uploaded.json()["data"]["file"]["id"]
    database = client.app.state.database_session_factory()

    def database_override():
        yield database

    async def stop_before_cleanup(**kwargs):
        assert not database.in_transaction()
        return kwargs["deletion"].revision

    client.app.dependency_overrides[get_db] = database_override
    monkeypatch.setattr(
        reimbursement_files_api,
        "complete_draft_file_delete",
        stop_before_cleanup,
    )
    try:
        response = client.delete(
            f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
            params={"expectedRevision": 2},
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        client.app.dependency_overrides.pop(get_db, None)
        database.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["revision"] == 3


def test_interrupted_delete_remains_visible_and_is_safely_resumable(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    file_id = uploaded.json()["data"]["file"]["id"]
    staging = client.app.state.reimbursement_staging
    original_delete = staging.delete

    def fail_delete(*_args, **_kwargs):
        raise StagingLayoutError("simulated local delete failure")

    monkeypatch.setattr(staging, "delete", fail_delete)
    failed = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        params={"expectedRevision": 2},
        headers={"X-CSRF-Token": csrf},
    )
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "REIMBURSEMENT_STORAGE_ERROR"

    visible = client.get(f"/api/reimbursements/drafts/{draft_id}/files")
    assert visible.status_code == 200
    assert visible.json()["data"]["revision"] == 3
    assert visible.json()["data"]["items"][0]["status"] == "DELETING"

    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        assert draft is not None
        draft.expires_at = utc_now() - timedelta(seconds=1)
        database.commit()

    monkeypatch.setattr(staging, "delete", original_delete)
    resumed = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        params={"expectedRevision": 3},
        headers={"X-CSRF-Token": csrf},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["revision"] == 3
    assert client.app.state.reimbursement_quota.usage().reserved_bytes == 0


def test_ocr_result_is_persisted_and_recovered_after_restart(client_factory) -> None:
    engine = FakeOcrEngine(
        {
            "*": [
                OcrLine("铁路电子客票 G123", 0.98),
                OcrLine("乘车日期 2026年6月30日", 0.97),
                OcrLine("北京南-合肥南", 0.96),
                OcrLine("票价 ￥454.00", 0.99),
            ]
        }
    )
    client = client_factory(auth_mock_enabled=True, ocr_enabled=True, ocr_engine=engine)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    file_id = uploaded.json()["data"]["file"]["id"]

    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2, "tripYear": 2026},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["revision"] == 3
    assert data["file"]["ocrStatus"] == "COMPLETE"
    assert data["file"]["ocrResult"]["amount"] == "454.00"
    assert "operationId" not in response.text

    restarted = client_factory(auth_mock_enabled=True, ocr_enabled=True, ocr_engine=engine)
    mock_login(restarted)
    listed = restarted.get(f"/api/reimbursements/drafts/{draft_id}/files")
    assert listed.status_code == 200
    recovered = listed.json()["data"]["items"][0]
    assert recovered["ocrStatus"] == "COMPLETE"
    assert recovered["ocrResult"] == data["file"]["ocrResult"]


def test_ocr_failure_and_symlink_are_persisted_without_content_or_path_leak(
    client_factory,
    tmp_path: Path,
) -> None:
    client = client_factory(auth_mock_enabled=True, ocr_enabled=True, ocr_engine=FakeOcrEngine())
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    file_id = uploaded.json()["data"]["file"]["id"]
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file is not None
        storage_path = client.app.state.reimbursement_staging.root / file.storage_key
    storage_path.unlink()
    secret = tmp_path / "do-not-read.txt"
    secret.write_text("VERY-SECRET-RECEIPT-CONTENT")
    storage_path.symlink_to(secret)

    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2},
    )

    assert response.status_code == 200, response.text
    file_data = response.json()["data"]["file"]
    assert file_data["ocrStatus"] == "FAILED"
    assert file_data["ocrResult"]["error"]["code"] == "OCR_FAILED"
    assert "VERY-SECRET" not in response.text
    assert str(client.app.state.reimbursement_staging.root) not in response.text
    assert not list((client.app.state.settings.temp_dir / ".spool").glob("ocr-*"))


def test_excel_preview_recalculates_from_saved_draft_without_submission_side_effects(
    client_factory,
    monkeypatch,
) -> None:
    from test_reimbursement_drafts import _catalog

    from app.services import reimbursement_files

    monkeypatch.setattr(
        reimbursement_files, "require_submission_ready_catalog", lambda _: _catalog()
    )
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client, amount="44.89")

    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/excel-preview",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 1},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    workbook = load_workbook(io.BytesIO(response.content), data_only=True)
    try:
        worksheet = workbook.active
        assert str(worksheet[EXCEL_TEMPLATE.total_amount_cell].value) == "244.89"
        assert worksheet[EXCEL_TEMPLATE.project_cell].value == "MES 项目"
    finally:
        workbook.close()
    with client.app.state.database_session_factory() as database:
        assert database.scalar(select(func.count()).select_from(ReimbursementSubmission)) == 0
        assert database.scalar(select(func.count()).select_from(ReimbursementUpload)) == 0


def test_excel_preview_recovers_stale_running_ocr_without_blocking(
    client_factory,
    monkeypatch,
) -> None:
    from test_reimbursement_drafts import _catalog

    from app.services import reimbursement_files

    monkeypatch.setattr(
        reimbursement_files, "require_submission_ready_catalog", lambda _: _catalog()
    )
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client, amount="44.89")
    uploaded = _upload(client, csrf, draft_id, revision=1)
    assert uploaded.status_code == 201
    file_id = uploaded.json()["data"]["file"]["id"]
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        file.ocr_status = "RUNNING"
        file.ocr_result_json = json.dumps({"operationId": "interrupted-operation"})
        file.updated_at = utc_now() - timedelta(
            seconds=client.app.state.settings.ocr_timeout_seconds + 31
        )
        database.commit()

    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/excel-preview",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2},
    )

    assert response.status_code == 200, response.text
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file.ocr_status == "FAILED"
        assert json.loads(file.ocr_result_json)["error"]["code"] == "OCR_INTERRUPTED"


def test_excel_preview_native_ticket_survives_external_download_without_session_cookie(
    client_factory,
    monkeypatch,
) -> None:
    from test_reimbursement_drafts import _catalog

    from app.services import reimbursement_files

    monkeypatch.setattr(
        reimbursement_files, "require_submission_ready_catalog", lambda _: _catalog()
    )
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client, amount="44.89")

    issued = client.post(
        f"/api/reimbursements/drafts/{draft_id}/excel-preview-ticket",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 1},
    )
    assert issued.status_code == 200, issued.text
    ticket = issued.json()["data"]

    client.cookies.clear()
    response = client.get(
        ticket["downloadUrl"],
        headers={"X-Reimbursement-Download-Token": ticket["downloadToken"]},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"


def test_excel_preview_native_ticket_rejects_tampering_without_session_cookie(
    client_factory,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    issued = client.post(
        f"/api/reimbursements/drafts/{draft_id}/excel-preview-ticket",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 1},
    )
    ticket = issued.json()["data"]
    token = ticket["downloadToken"]
    tampered = f"{token[:-1]}{'0' if token[-1] != '0' else '1'}"

    client.cookies.clear()
    response = client.get(
        ticket["downloadUrl"],
        headers={"X-Reimbursement-Download-Token": tampered},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "EXCEL_PREVIEW_TICKET_INVALID"


def test_excel_preview_releases_sync_session_before_generation(
    client_factory,
    monkeypatch,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    database = client.app.state.database_session_factory()

    def database_override():
        yield database

    async def stop_before_generation(**kwargs):
        assert not database.in_transaction()
        assert kwargs["employee_name"] == client.app.state.settings.auth_mock_user_name
        raise ApiError("BOUNDARY_REACHED", "boundary reached", 418)

    client.app.dependency_overrides[get_db] = database_override
    monkeypatch.setattr(
        reimbursement_files_api,
        "generate_draft_excel_preview",
        stop_before_generation,
    )
    try:
        response = client.post(
            f"/api/reimbursements/drafts/{draft_id}/excel-preview",
            headers={"X-CSRF-Token": csrf},
            json={"expectedRevision": 1},
        )
    finally:
        client.app.dependency_overrides.pop(get_db, None)
        database.close()

    assert response.status_code == 418, response.text
    assert response.json()["error"]["code"] == "BOUNDARY_REACHED"


@pytest.mark.asyncio
@pytest.mark.parametrize("itinerary", [False, True])
async def test_cancelled_ocr_finishes_as_failed_and_releases_worker_copy(
    client_factory,
    itinerary,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1)
    file_id = uploaded.json()["data"]["file"]["id"]
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        assert draft is not None
        draft.status = "REVIEW_READY"
        if itinerary:
            file = database.get(ReimbursementDraftFile, file_id)
            file.processing_role = "ATTACHMENT_ONLY"
            file.attachment_kind = "itinerary"
        database.commit()
    started = asyncio.Event()

    class BlockingOcrService:
        async def recognize_file(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        recognize_itinerary_file = recognize_file

    task = asyncio.create_task(
        recognize_draft_file(
            actor=DraftActor(
                corp_id="corp-fixed",
                user_id="mock-user",
                department_id="100",
                department_name="测试部门",
            ),
            draft_id=draft_id,
            file_id=file_id,
            expected_revision=2,
            reference_year=2026,
            settings=client.app.state.settings,
            session_factory=client.app.state.database_session_factory,
            staging=client.app.state.reimbursement_staging,
            ocr_service=BlockingOcrService(),  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, file_id)
        assert draft is not None and file is not None
        assert draft.revision == 3
        assert draft.status == "DRAFT"
        assert file.ocr_status == "RUNNING"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, file_id)
        assert draft is not None and draft.revision == 3
        assert file is not None and file.ocr_status == "FAILED"
        assert "OCR_CANCELLED" in str(file.ocr_result_json)
        if itinerary:
            assert json.loads(file.ocr_result_json)["kind"] == "itinerary"
    assert not list((client.app.state.settings.temp_dir / ".spool").glob("ocr-*"))
