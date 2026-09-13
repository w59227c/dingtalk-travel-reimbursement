from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.base import Base
from app.database.session import create_database_engine
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftRelatedApproval,
    ReimbursementDraftStatus,
    ReimbursementSubmission,
    ReimbursementSubmissionStatus,
    ReimbursementUpload,
    ReimbursementUploadLocalStatus,
    ReimbursementUploadRole,
    ReimbursementUploadStatus,
    utc_now,
)
from app.services.reimbursement_drafts import DraftActor
from app.services.reimbursement_quota import (
    DraftFileOwner,
    ReimbursementQuotaCoordinator,
    ReimbursementQuotaExceeded,
    ReimbursementReservationConflict,
    SubmissionLease,
)
from app.services.reimbursement_staging import (
    ReimbursementStaging,
    StagedObject,
    StagingLayoutError,
    StagingObjectNotFound,
)

_HASH = "a" * 64


def _new_draft() -> ReimbursementDraft:
    return ReimbursementDraft(
        corp_id="corp-test",
        owner_user_id="employee-1",
        status=ReimbursementDraftStatus.DRAFT.value,
        revision=1,
        department_id="department-1",
        department_name="测试部门",
        template_process_code="PROC-REIMBURSEMENT",
        template_config_version=1,
        schema_fingerprint=_HASH,
        input_json="{}",
        related_instance_ids_json="[]",
        expires_at=utc_now() + timedelta(days=1),
    )


def _new_generating_submission(draft: ReimbursementDraft) -> ReimbursementSubmission:
    now = utc_now()
    return ReimbursementSubmission(
        draft_id=draft.id,
        corp_id=draft.corp_id,
        originator_user_id=draft.owner_user_id,
        originator_union_id="union-1",
        originator_name="测试员工",
        department_id=draft.department_id,
        department_name=draft.department_name,
        template_process_code=draft.template_process_code,
        template_config_version=draft.template_config_version,
        schema_fingerprint=draft.schema_fingerprint,
        form_snapshot_json=draft.input_json,
        related_instance_ids_json=draft.related_instance_ids_json,
        snapshot_sha256="b" * 64,
        idempotency_key_hash="c" * 64,
        status=ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
        status_version=3,
        lease_owner="worker-1",
        lease_token="lease-token-1",
        lease_expires_at=now + timedelta(minutes=10),
    )


def _create_active_deleting_file(tmp_path: Path, content: bytes):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id

    maximum_bytes = max(100, len(content))
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=maximum_bytes,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=maximum_bytes)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="deleting.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=len(content),
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(owner, reservation)
    staged = staging.write_bytes(reservation.staging, content)
    coordinator.finalize(owner, reservation, staged)
    with Session(engine) as database:
        file = database.get(ReimbursementDraftFile, reservation.record_id)
        assert file is not None
        file.file_status = ReimbursementDraftFileStatus.DELETING.value
        database.commit()
    return engine, staging, coordinator, draft_id, reservation, staged


def test_concurrent_database_reservations_admit_only_one_writer(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'quota.db'}"
    setup_engine = create_database_engine(database_url)
    Base.metadata.create_all(setup_engine)
    with setup_engine.begin() as connection:
        draft = _new_draft()
        with Session(bind=connection) as database:
            database.add(draft)
            database.flush()
            draft_id = draft.id
    setup_engine.dispose()

    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    first_engine = create_database_engine(database_url)
    second_engine = create_database_engine(database_url)
    coordinators = (
        ReimbursementQuotaCoordinator(first_engine, staging, max_bytes=100),
        ReimbursementQuotaCoordinator(second_engine, staging, max_bytes=100),
    )
    owner = DraftFileOwner(
        corp_id="corp-test",
        user_id="employee-1",
        draft_id=draft_id,
        expected_revision=1,
    )
    barrier = Barrier(2)

    def reserve(index: int):
        barrier.wait(timeout=5)
        return coordinators[index].reserve_draft_file(
            owner,
            sort_order=index,
            processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
            original_name=f"发票-{index}.pdf",
            extension="pdf",
            media_type="application/pdf",
            reserved_bytes=60,
            expires_at=utc_now() + timedelta(minutes=5),
        )

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve, index) for index in range(2)]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except Exception as exc:  # noqa: BLE001 - winner is intentionally nondeterministic
                outcomes.append(exc)

    assert sum(isinstance(item, ReimbursementReservationConflict) for item in outcomes) == 1
    with first_engine.connect() as connection:
        rows = connection.execute(select(ReimbursementDraftFile)).all()
    assert len(rows) == 1
    assert coordinators[0].usage().reserved_bytes == 60
    assert not list((tmp_path / "staging").rglob("*.pdf"))

    first_engine.dispose()
    second_engine.dispose()


@pytest.mark.parametrize(
    "role", [ReimbursementUploadRole.GENERATED_EXCEL, ReimbursementUploadRole.GENERATED_PDF]
)
def test_generated_reservation_is_owned_by_the_active_submission_lease(
    tmp_path: Path,
    role: ReimbursementUploadRole,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.flush()
        submission = _new_generating_submission(draft)
        database.add(submission)
        database.commit()
        submission_id = submission.id

    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    lease = SubmissionLease(
        corp_id="corp-test",
        user_id="employee-1",
        submission_id=submission_id,
        lease_token="lease-token-1",
        expected_status_version=3,
    )

    reservation = coordinator.reserve_generated_upload(
        lease,
        sort_order=2,
        file_name=(
            "票据汇总.pdf" if role is ReimbursementUploadRole.GENERATED_PDF else "报销单.xlsx"
        ),
        role=role,
        reserved_bytes=80,
        expires_at=utc_now() + timedelta(minutes=5),
    )

    with Session(engine) as database:
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert upload is not None
        assert upload.role == role.value
        assert upload.file_type == (
            "pdf" if role is ReimbursementUploadRole.GENERATED_PDF else "xlsx"
        )
        assert upload.local_status == ReimbursementUploadLocalStatus.RESERVED.value
        assert upload.local_storage_key == reservation.staging.storage_key
        assert upload.local_part_storage_key == reservation.staging.part_storage_key
    assert coordinator.usage().reserved_bytes == 80

    engine.dispose()


def test_draft_file_reservation_moves_to_writing_and_finalizes_with_cas(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=20,
        expires_at=utc_now() + timedelta(minutes=5),
    )

    coordinator.mark_writing(owner, reservation)
    with Session(engine) as database:
        row = database.get(ReimbursementDraftFile, reservation.record_id)
        assert row is not None
        assert row.file_status == "WRITING"

    staged = staging.write_bytes(reservation.staging, b"verified-content")
    coordinator.finalize(owner, reservation, staged)

    with Session(engine) as database:
        row = database.get(ReimbursementDraftFile, reservation.record_id)
        assert row is not None
        assert row.file_status == "ACTIVE"
        assert row.part_storage_key is None
        assert row.reservation_expires_at is None
        assert row.size_bytes == len(b"verified-content")
        assert row.sha256 == staged.sha256
    with pytest.raises(ReimbursementReservationConflict):
        coordinator.finalize(owner, reservation, staged)

    engine.dispose()


def test_draft_file_reservation_claims_revision_and_reopens_review_ready_draft(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        draft.status = ReimbursementDraftStatus.REVIEW_READY.value
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)

    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=20,
        expires_at=utc_now() + timedelta(minutes=5),
    )

    with Session(engine) as database:
        draft = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, reservation.record_id)
        assert draft is not None and file is not None
        assert draft.revision == 2
        assert draft.status == ReimbursementDraftStatus.DRAFT.value
        assert file.file_status == ReimbursementDraftFileStatus.RESERVED.value

    engine.dispose()


def test_finalize_never_accepts_more_bytes_than_were_reserved(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=10,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(owner, reservation)

    oversized = StagedObject(
        storage_key=reservation.staging.storage_key,
        size_bytes=11,
        sha256="d" * 64,
    )
    with pytest.raises(ReimbursementReservationConflict):
        coordinator.finalize(owner, reservation, oversized)

    with Session(engine) as database:
        row = database.get(ReimbursementDraftFile, reservation.record_id)
        assert row is not None
        assert row.file_status == "WRITING"
        assert row.size_bytes is None

    engine.dispose()


def test_generated_upload_lifecycle_rejects_a_stale_worker_lease(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.flush()
        submission = _new_generating_submission(draft)
        database.add(submission)
        database.commit()
        submission_id = submission.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    lease = SubmissionLease(
        "corp-test",
        "employee-1",
        submission_id,
        "lease-token-1",
        3,
    )
    reservation = coordinator.reserve_generated_upload(
        lease,
        sort_order=0,
        file_name="差旅费报销单.xlsx",
        reserved_bytes=30,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    stale_lease = SubmissionLease(
        "corp-test",
        "employee-1",
        submission_id,
        "stale-token",
        3,
    )

    with pytest.raises(ReimbursementReservationConflict):
        coordinator.mark_writing(stale_lease, reservation)

    coordinator.mark_writing(lease, reservation)
    staged = staging.write_bytes(reservation.staging, b"workbook-bytes")
    coordinator.finalize(lease, reservation, staged)

    with Session(engine) as database:
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert upload is not None
        assert upload.local_status == ReimbursementUploadLocalStatus.READY.value
        assert upload.size_bytes == len(b"workbook-bytes")
        assert upload.sha256 == staged.sha256
        assert upload.local_part_storage_key is None

    engine.dispose()


def test_release_aborts_an_unwritten_draft_reservation_and_frees_quota(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=90,
        expires_at=utc_now() + timedelta(minutes=5),
    )

    coordinator.release(owner, reservation)

    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        row = database.get(ReimbursementDraftFile, reservation.record_id)
        assert row is not None
        assert row.file_status == "PURGED"
        assert row.purged_at is not None
        assert row.part_storage_key is None

    engine.dispose()


def test_release_refuses_remote_commit_but_linked_release_only_deletes_local_copy(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.flush()
        submission = _new_generating_submission(draft)
        database.add(submission)
        database.commit()
        submission_id = submission.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    lease = SubmissionLease(
        "corp-test",
        "employee-1",
        submission_id,
        "lease-token-1",
        3,
    )
    reservation = coordinator.reserve_generated_upload(
        lease,
        sort_order=0,
        file_name="差旅费报销单.xlsx",
        reserved_bytes=30,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(lease, reservation)
    staged = staging.write_bytes(reservation.staging, b"workbook-bytes")
    coordinator.finalize(lease, reservation, staged)
    with Session(engine) as database:
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert upload is not None
        upload.upload_status = "COMMITTING"
        upload.commit_started_at = utc_now()
        database.commit()

    with pytest.raises(ReimbursementReservationConflict):
        coordinator.release(lease, reservation)
    assert coordinator.usage().reserved_bytes == 30

    with Session(engine) as database:
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert upload is not None
        upload.upload_status = "COMMITTED"
        upload.space_id = "space-1"
        upload.file_id = "file-1"
        database.commit()

    with pytest.raises(ReimbursementReservationConflict):
        coordinator.release(lease, reservation)
    assert coordinator.usage().reserved_bytes == 30

    now = utc_now()
    with Session(engine) as database:
        submission = database.get(ReimbursementSubmission, submission_id)
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert submission is not None and upload is not None
        submission.status = ReimbursementSubmissionStatus.VERIFYING.value
        submission.oa_create_started_at = now
        submission.oa_request_hash = "e" * 64
        submission.process_instance_id = "instance-1"
        upload.upload_status = "LINKED"
        upload.space_id = "space-1"
        upload.file_id = "file-1"
        upload.linked_at = now
        database.commit()

    coordinator.release(lease, reservation)

    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert upload is not None
        assert upload.local_status == ReimbursementUploadLocalStatus.DELETED.value
        assert upload.local_deleted_at is not None
        assert upload.upload_status == "LINKED"
        assert upload.file_id == "file-1"
        assert upload.space_id == "space-1"
    with pytest.raises(StagingObjectNotFound):
        staging.read_bytes(
            reservation.staging.storage_key,
            expected_size=staged.size_bytes,
            expected_sha256=staged.sha256,
        )

    engine.dispose()


def test_expired_draft_write_reclaims_its_installed_but_unfinalized_object(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=70,
        expires_at=utc_now() + timedelta(minutes=1),
    )
    coordinator.mark_writing(owner, reservation)
    staged = staging.write_bytes(reservation.staging, b"unfinalized-content")

    assert coordinator.reclaim_expired(now=utc_now() + timedelta(minutes=2)) == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        record = database.get(ReimbursementDraftFile, reservation.record_id)
        assert record is not None
        assert record.file_status == ReimbursementDraftFileStatus.PURGED.value
        assert record.purged_at is not None
    with pytest.raises(StagingObjectNotFound):
        staging.read_bytes(
            reservation.staging.storage_key,
            expected_size=staged.size_bytes,
            expected_sha256=staged.sha256,
        )

    engine.dispose()


def test_startup_reclaims_unexpired_interrupted_draft_write(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=70,
        expires_at=utc_now() + timedelta(hours=1),
    )
    coordinator.mark_writing(owner, reservation)
    staging.write_bytes(reservation.staging, b"interrupted-content")

    assert coordinator.reclaim_interrupted_local_writes() == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        record = database.get(ReimbursementDraftFile, reservation.record_id)
        assert record is not None
        assert record.file_status == ReimbursementDraftFileStatus.PURGED.value

    engine.dispose()


def test_startup_reclaims_unexpired_interrupted_generated_write(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.flush()
        submission = _new_generating_submission(draft)
        database.add(submission)
        database.commit()
        submission_id = submission.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    lease = SubmissionLease(
        "corp-test",
        "employee-1",
        submission_id,
        "lease-token-1",
        3,
    )
    reservation = coordinator.reserve_generated_upload(
        lease,
        sort_order=0,
        file_name="差旅费报销单.xlsx",
        reserved_bytes=70,
        expires_at=utc_now() + timedelta(hours=1),
    )
    coordinator.mark_writing(lease, reservation)
    staging.write_bytes(reservation.staging, b"interrupted-workbook")

    assert coordinator.reclaim_interrupted_local_writes() == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert upload is not None
        assert upload.local_status == ReimbursementUploadLocalStatus.DELETED.value
        assert upload.upload_status == ReimbursementUploadStatus.DISCARDED.value

    engine.dispose()


def test_reclaim_continues_after_one_expired_reservation_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        bad_draft = _new_draft()
        good_draft = _new_draft()
        database.add_all([bad_draft, good_draft])
        database.commit()
        draft_ids = (bad_draft.id, good_draft.id)
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    reservations = []
    for draft_id in draft_ids:
        owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
        reservation = coordinator.reserve_draft_file(
            owner,
            sort_order=0,
            processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
            original_name=f"{draft_id}.pdf",
            extension="pdf",
            media_type="application/pdf",
            reserved_bytes=40,
            expires_at=utc_now() + timedelta(minutes=1),
        )
        coordinator.mark_writing(owner, reservation)
        staging.write_bytes(reservation.staging, draft_id.encode())
        reservations.append(reservation)

    original_discard = staging.discard_reservation
    bad_storage_key = reservations[0].staging.storage_key

    def fail_one_reservation(reservation):
        if reservation.storage_key == bad_storage_key:
            raise StagingLayoutError("simulated reservation cleanup failure")
        return original_discard(reservation)

    monkeypatch.setattr(staging, "discard_reservation", fail_one_reservation)
    reclaim_time = utc_now() + timedelta(minutes=2)

    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 40
    with Session(engine) as database:
        bad = database.get(ReimbursementDraftFile, reservations[0].record_id)
        good = database.get(ReimbursementDraftFile, reservations[1].record_id)
        assert bad is not None and good is not None
        assert bad.file_status == ReimbursementDraftFileStatus.WRITING.value
        assert good.file_status == ReimbursementDraftFileStatus.PURGED.value

    monkeypatch.setattr(staging, "discard_reservation", original_discard)
    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        bad = database.get(ReimbursementDraftFile, reservations[0].record_id)
        assert bad is not None
        assert bad.file_status == ReimbursementDraftFileStatus.PURGED.value

    engine.dispose()


def test_reclaim_before_database_migrations_is_a_safe_noop(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)

    assert coordinator.reclaim_expired() == 0

    engine.dispose()


def test_reclaim_completes_a_deleting_file_after_its_draft_expires(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=30,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(owner, reservation)
    staged = staging.write_bytes(reservation.staging, b"delete-after-expiry")
    coordinator.finalize(owner, reservation, staged)
    reclaim_time = utc_now() + timedelta(days=2)
    with Session(engine) as database:
        draft = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, reservation.record_id)
        assert draft is not None and file is not None
        draft.expires_at = reclaim_time - timedelta(seconds=1)
        file.file_status = ReimbursementDraftFileStatus.DELETING.value
        database.commit()

    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        assert database.get(ReimbursementDraft, draft_id) is None
        assert database.get(ReimbursementDraftFile, reservation.record_id) is None
    with pytest.raises(StagingObjectNotFound):
        staging.read_bytes(
            reservation.staging.storage_key,
            expected_size=staged.size_bytes,
            expected_sha256=staged.sha256,
        )

    engine.dispose()


def test_reclaim_continues_after_one_deleting_file_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        bad_draft = _new_draft()
        good_draft = _new_draft()
        database.add_all([bad_draft, good_draft])
        database.commit()
        draft_ids = (bad_draft.id, good_draft.id)
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    reservations = []
    for draft_id in draft_ids:
        owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
        reservation = coordinator.reserve_draft_file(
            owner,
            sort_order=0,
            processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
            original_name=f"{draft_id}.pdf",
            extension="pdf",
            media_type="application/pdf",
            reserved_bytes=40,
            expires_at=utc_now() + timedelta(minutes=5),
        )
        coordinator.mark_writing(owner, reservation)
        staged = staging.write_bytes(reservation.staging, draft_id.encode())
        coordinator.finalize(owner, reservation, staged)
        reservations.append(reservation)
    with Session(engine) as database:
        for reservation in reservations:
            record = database.get(ReimbursementDraftFile, reservation.record_id)
            assert record is not None
            record.file_status = ReimbursementDraftFileStatus.DELETING.value
        database.commit()

    original_delete = staging.delete
    bad_storage_key = reservations[0].staging.storage_key

    def fail_one_file(storage_key: str, **kwargs):
        if storage_key == bad_storage_key:
            raise StagingLayoutError("simulated deleting-file cleanup failure")
        return original_delete(storage_key, **kwargs)

    monkeypatch.setattr(staging, "delete", fail_one_file)
    reclaim_time = utc_now()

    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 40
    with Session(engine) as database:
        bad = database.get(ReimbursementDraftFile, reservations[0].record_id)
        good = database.get(ReimbursementDraftFile, reservations[1].record_id)
        assert bad is not None and good is not None
        assert bad.file_status == ReimbursementDraftFileStatus.DELETING.value
        assert good.file_status == ReimbursementDraftFileStatus.PURGED.value

    monkeypatch.setattr(staging, "delete", original_delete)
    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        bad = database.get(ReimbursementDraftFile, reservations[0].record_id)
        assert bad is not None
        assert bad.file_status == ReimbursementDraftFileStatus.PURGED.value

    engine.dispose()


def test_reclaim_removes_every_local_file_state_and_related_rows_for_an_expired_draft(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=200)
    reservations = []
    for sort_order, expected_revision in enumerate(range(1, 6)):
        owner = DraftFileOwner(
            "corp-test",
            "employee-1",
            draft_id,
            expected_revision,
        )
        reservation = coordinator.reserve_draft_file(
            owner,
            sort_order=sort_order,
            processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
            original_name=f"state-{sort_order}.pdf",
            extension="pdf",
            media_type="application/pdf",
            reserved_bytes=20,
            expires_at=utc_now() + timedelta(minutes=5),
        )
        reservations.append((owner, reservation))
        if sort_order == 1:
            coordinator.mark_writing(owner, reservation)
        elif sort_order in {2, 3}:
            coordinator.mark_writing(owner, reservation)
            staged = staging.write_bytes(
                reservation.staging,
                b"active" if sort_order == 2 else b"deleting",
            )
            coordinator.finalize(owner, reservation, staged)

    reclaim_time = utc_now() + timedelta(days=2)
    with Session(engine) as database:
        draft = database.get(ReimbursementDraft, draft_id)
        deleting = database.get(ReimbursementDraftFile, reservations[3][1].record_id)
        failed = database.get(ReimbursementDraftFile, reservations[4][1].record_id)
        assert draft is not None and deleting is not None and failed is not None
        draft.expires_at = reclaim_time - timedelta(seconds=1)
        deleting.file_status = ReimbursementDraftFileStatus.DELETING.value
        failed.file_status = ReimbursementDraftFileStatus.FAILED.value
        failed.part_storage_key = None
        failed.reservation_expires_at = None
        related = ReimbursementDraftRelatedApproval(
            draft_id=draft.id,
            corp_id=draft.corp_id,
            owner_user_id=draft.owner_user_id,
            sort_order=0,
            process_instance_id="travel-instance-1",
            travel_profile_key="travel-default",
            process_code="PROC-TRAVEL",
            catalog_config_version=1,
            travel_schema_fingerprint="d" * 64,
            listed_from_ms=1,
            listed_to_ms=2,
            travel_start_date=date(2026, 9, 1),
            travel_end_date=date(2026, 9, 2),
            title="上海出差",
            business_id="TRAVEL-1",
            instance_created_at=utc_now(),
            verified_at=utc_now(),
        )
        database.add(related)
        database.commit()
        related_id = related.id

    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 0
    assert not [path for path in staging.root.rglob("*") if path.is_file()]
    with Session(engine) as database:
        assert database.get(ReimbursementDraft, draft_id) is None
        assert (
            database.scalars(
                select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == draft_id)
            ).all()
            == []
        )
        assert database.get(ReimbursementDraftRelatedApproval, related_id) is None

    engine.dispose()


def test_reclaim_continues_after_one_expired_draft_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        bad_draft = _new_draft()
        good_draft = _new_draft()
        database.add_all([bad_draft, good_draft])
        database.commit()
        bad_draft_id = bad_draft.id
        good_draft_id = good_draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    reservations = {}
    for draft_id, contents in (
        (bad_draft_id, b"bad-draft"),
        (good_draft_id, b"good-draft"),
    ):
        owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
        reservation = coordinator.reserve_draft_file(
            owner,
            sort_order=0,
            processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
            original_name=f"{draft_id}.pdf",
            extension="pdf",
            media_type="application/pdf",
            reserved_bytes=20,
            expires_at=utc_now() + timedelta(minutes=5),
        )
        coordinator.mark_writing(owner, reservation)
        staged = staging.write_bytes(reservation.staging, contents)
        coordinator.finalize(owner, reservation, staged)
        reservations[draft_id] = reservation

    reclaim_time = utc_now() + timedelta(days=2)
    with Session(engine) as database:
        for draft_id in (bad_draft_id, good_draft_id):
            draft = database.get(ReimbursementDraft, draft_id)
            assert draft is not None
            draft.expires_at = reclaim_time - timedelta(seconds=1)
        database.commit()

    original_delete = staging.delete
    bad_storage_key = reservations[bad_draft_id].staging.storage_key

    def fail_one_draft(storage_key: str, **kwargs) -> None:
        if storage_key == bad_storage_key:
            raise StagingLayoutError("simulated local delete failure")
        original_delete(storage_key, **kwargs)

    monkeypatch.setattr(staging, "delete", fail_one_draft)

    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 20
    with Session(engine) as database:
        bad = database.get(ReimbursementDraft, bad_draft_id)
        bad_file = database.get(
            ReimbursementDraftFile,
            reservations[bad_draft_id].record_id,
        )
        assert bad is not None
        assert bad_file is not None
        assert bad.status == ReimbursementDraftStatus.EXPIRED.value
        assert bad_file.file_status == ReimbursementDraftFileStatus.DELETING.value
        bad.expires_at = reclaim_time + timedelta(days=1)
        assert database.get(ReimbursementDraft, good_draft_id) is None
        database.commit()

    monkeypatch.setattr(staging, "delete", original_delete)
    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        assert database.get(ReimbursementDraft, bad_draft_id) is None

    engine.dispose()


@pytest.mark.parametrize("process_instance_id", [None, "process-instance-1"])
def test_reclaim_never_touches_locked_or_submitted_drafts(
    tmp_path: Path,
    process_instance_id: str | None,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        locked_draft = _new_draft()
        submitted_draft = _new_draft()
        database.add_all([locked_draft, submitted_draft])
        database.commit()
        locked_draft_id = locked_draft.id
        submitted_draft_id = submitted_draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    staged_files = {}
    for draft_id, contents in (
        (locked_draft_id, b"locked"),
        (submitted_draft_id, b"submitted"),
    ):
        owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
        reservation = coordinator.reserve_draft_file(
            owner,
            sort_order=0,
            processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
            original_name=f"{draft_id}.pdf",
            extension="pdf",
            media_type="application/pdf",
            reserved_bytes=20,
            expires_at=utc_now() + timedelta(minutes=5),
        )
        coordinator.mark_writing(owner, reservation)
        staged = staging.write_bytes(reservation.staging, contents)
        coordinator.finalize(owner, reservation, staged)
        staged_files[draft_id] = (reservation, staged)

    reclaim_time = utc_now() + timedelta(days=2)
    with Session(engine) as database:
        locked = database.get(ReimbursementDraft, locked_draft_id)
        submitted = database.get(ReimbursementDraft, submitted_draft_id)
        assert locked is not None and submitted is not None
        for draft in (locked, submitted):
            draft.expires_at = reclaim_time - timedelta(seconds=1)
        locked.status = ReimbursementDraftStatus.LOCKED.value
        locked.locked_at = utc_now()
        for draft_id in (locked_draft_id, submitted_draft_id):
            file = database.get(ReimbursementDraftFile, staged_files[draft_id][0].record_id)
            assert file is not None
            file.file_status = ReimbursementDraftFileStatus.DELETING.value
        submission = _new_generating_submission(submitted)
        if process_instance_id is not None:
            submission.status = ReimbursementSubmissionStatus.MANUAL_REVIEW.value
            submission.process_instance_id = process_instance_id
        database.add(submission)
        database.commit()

    assert coordinator.reclaim_expired(now=reclaim_time) == 0
    assert coordinator.usage().reserved_bytes == 40
    with Session(engine) as database:
        assert database.get(ReimbursementDraft, locked_draft_id) is not None
        assert database.get(ReimbursementDraft, submitted_draft_id) is not None
    for reservation, staged in staged_files.values():
        assert staging.read_bytes(
            reservation.staging.storage_key,
            expected_size=staged.size_bytes,
            expected_sha256=staged.sha256,
        )

    engine.dispose()


def test_reclaim_hashes_large_deleting_file_without_holding_sqlite_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"large-deleting-file" * 128 * 1024
    engine, staging, coordinator, _draft_id, reservation, _staged = _create_active_deleting_file(
        tmp_path, content
    )
    original_delete = staging.delete
    observed_write_lock = False

    def observe_transaction_boundary(storage_key: str, **kwargs) -> bool:
        nonlocal observed_write_lock
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=1")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            observed_write_lock = True
            connection.rollback()
        return original_delete(storage_key, **kwargs)

    monkeypatch.setattr(staging, "delete", observe_transaction_boundary)

    assert coordinator.reclaim_expired(now=utc_now()) == 1
    assert observed_write_lock is True
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        file = database.get(ReimbursementDraftFile, reservation.record_id)
        assert file is not None
        assert file.file_status == ReimbursementDraftFileStatus.PURGED.value

    engine.dispose()


@pytest.mark.parametrize(
    "changed_boundary",
    ["status", "metadata", "owner", "locked", "expired", "submission"],
)
def test_reclaim_does_not_finalize_when_deleting_file_boundary_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_boundary: str,
) -> None:
    content = b"deleting-file"
    engine, staging, coordinator, draft_id, reservation, _staged = _create_active_deleting_file(
        tmp_path, content
    )
    original_delete = staging.delete

    def delete_then_change_boundary(storage_key: str, **kwargs) -> bool:
        deleted = original_delete(storage_key, **kwargs)
        with Session(engine) as database:
            draft = database.get(ReimbursementDraft, draft_id)
            file = database.get(ReimbursementDraftFile, reservation.record_id)
            assert draft is not None and file is not None
            if changed_boundary == "status":
                file.file_status = ReimbursementDraftFileStatus.ACTIVE.value
            elif changed_boundary == "metadata":
                file.sha256 = "f" * 64
            elif changed_boundary == "owner":
                draft.owner_user_id = "employee-2"
            elif changed_boundary == "locked":
                draft.status = ReimbursementDraftStatus.LOCKED.value
                draft.locked_at = utc_now()
            elif changed_boundary == "expired":
                draft.status = ReimbursementDraftStatus.EXPIRED.value
            else:
                database.add(_new_generating_submission(draft))
            database.commit()
        return deleted

    monkeypatch.setattr(staging, "delete", delete_then_change_boundary)

    assert coordinator.reclaim_expired(now=utc_now()) == 0
    assert coordinator.usage().reserved_bytes == len(content)
    with Session(engine) as database:
        file = database.get(ReimbursementDraftFile, reservation.record_id)
        assert file is not None
        assert file.file_status != ReimbursementDraftFileStatus.PURGED.value

    engine.dispose()


def test_concurrent_deleting_file_reclaims_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"concurrent-delete"
    engine, staging, coordinator, _draft_id, reservation, _staged = _create_active_deleting_file(
        tmp_path, content
    )
    original_delete = staging.delete
    delete_barrier = Barrier(2)

    def synchronized_delete(storage_key: str, **kwargs) -> bool:
        delete_barrier.wait(timeout=5)
        return original_delete(storage_key, **kwargs)

    monkeypatch.setattr(staging, "delete", synchronized_delete)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(coordinator.reclaim_expired, now=utc_now()) for _ in range(2)]
        reclaimed = [future.result(timeout=10) for future in futures]

    assert sorted(reclaimed) == [0, 1]
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        file = database.get(ReimbursementDraftFile, reservation.record_id)
        assert file is not None
        assert file.file_status == ReimbursementDraftFileStatus.PURGED.value

    engine.dispose()


def test_delete_owned_draft_cleans_active_and_unfinished_files_before_database_row(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    first_owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    active = coordinator.reserve_draft_file(
        first_owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="active.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=30,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(first_owner, active)
    active_object = staging.write_bytes(active.staging, b"active-content")
    coordinator.finalize(first_owner, active, active_object)
    second_owner = DraftFileOwner("corp-test", "employee-1", draft_id, 2)
    unfinished = coordinator.reserve_draft_file(
        second_owner,
        sort_order=1,
        processing_role=ReimbursementDraftFileRole.ATTACHMENT_ONLY,
        original_name="unfinished.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=30,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(second_owner, unfinished)
    staging.write_bytes(unfinished.staging, b"unfinished-content")
    actor = DraftActor(
        corp_id="corp-test",
        user_id="employee-1",
        department_id="department-1",
        department_name="测试部门",
    )

    deleted_id = coordinator.delete_owned_draft(
        actor=actor,
        draft_id=draft_id,
        expected_revision=3,
    )

    assert deleted_id == draft_id
    assert coordinator.usage().reserved_bytes == 0
    assert not [path for path in staging.root.rglob("*") if path.is_file()]
    with Session(engine) as database:
        assert database.get(ReimbursementDraft, draft_id) is None
        assert (
            database.scalars(
                select(ReimbursementDraftFile).where(ReimbursementDraftFile.draft_id == draft_id)
            ).all()
            == []
        )

    engine.dispose()


@pytest.mark.parametrize("retry_revision", [2, 3])
def test_delete_owned_draft_resumes_its_intent_after_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_revision: int,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="active.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=30,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(owner, reservation)
    staged = staging.write_bytes(reservation.staging, b"active-content")
    coordinator.finalize(owner, reservation, staged)
    actor = DraftActor(
        corp_id="corp-test",
        user_id="employee-1",
        department_id="department-1",
        department_name="测试部门",
    )
    original_delete = staging.delete

    def fail_delete(*_args, **_kwargs):
        raise StagingLayoutError("simulated local delete failure")

    monkeypatch.setattr(staging, "delete", fail_delete)
    with pytest.raises(ApiError) as failure:
        coordinator.delete_owned_draft(
            actor=actor,
            draft_id=draft_id,
            expected_revision=2,
        )
    assert failure.value.code == "REIMBURSEMENT_STORAGE_ERROR"
    with Session(engine) as database:
        draft = database.get(ReimbursementDraft, draft_id)
        file = database.get(ReimbursementDraftFile, reservation.record_id)
        assert draft is not None and file is not None
        assert draft.status == ReimbursementDraftStatus.EXPIRED.value
        assert draft.revision == 3
        assert file.file_status == ReimbursementDraftFileStatus.DELETING.value
    assert coordinator.usage().reserved_bytes == 30

    monkeypatch.setattr(staging, "delete", original_delete)
    assert (
        coordinator.delete_owned_draft(
            actor=actor,
            draft_id=draft_id,
            expected_revision=retry_revision,
        )
        == draft_id
    )
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        assert database.get(ReimbursementDraft, draft_id) is None

    engine.dispose()


@pytest.mark.parametrize(
    "role", [ReimbursementUploadRole.GENERATED_EXCEL, ReimbursementUploadRole.GENERATED_PDF]
)
def test_expired_reclaim_skips_an_active_submission_then_frees_its_stale_reservation(
    tmp_path: Path,
    role: ReimbursementUploadRole,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.flush()
        submission = _new_generating_submission(draft)
        database.add(submission)
        database.commit()
        submission_id = submission.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    lease = SubmissionLease(
        "corp-test",
        "employee-1",
        submission_id,
        "lease-token-1",
        3,
    )
    reservation = coordinator.reserve_generated_upload(
        lease,
        sort_order=0,
        file_name=(
            "票据汇总.pdf" if role is ReimbursementUploadRole.GENERATED_PDF else "报销单.xlsx"
        ),
        role=role,
        reserved_bytes=70,
        expires_at=utc_now() + timedelta(minutes=1),
    )
    reclaim_time = utc_now() + timedelta(minutes=2)

    assert coordinator.reclaim_expired(now=reclaim_time) == 0
    assert coordinator.usage().reserved_bytes == 70

    with Session(engine) as database:
        submission = database.get(ReimbursementSubmission, submission_id)
        assert submission is not None
        submission.lease_expires_at = reclaim_time - timedelta(seconds=1)
        database.commit()

    assert coordinator.reclaim_expired(now=reclaim_time) == 1
    assert coordinator.usage().reserved_bytes == 0
    with Session(engine) as database:
        upload = database.get(ReimbursementUpload, reservation.record_id)
        assert upload is not None
        assert upload.local_status == ReimbursementUploadLocalStatus.DELETED.value
        assert upload.upload_status == "DISCARDED"
        assert upload.local_deleted_at == reclaim_time

    engine.dispose()


def test_restart_rebuilds_usage_and_deduplicates_an_original_upload_manifest(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'quota.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        draft = _new_draft()
        database.add(draft)
        database.commit()
        draft_id = draft.id
    staging = ReimbursementStaging(
        (tmp_path / "staging").resolve(),
        max_object_bytes=100,
    )
    staging.prepare()
    coordinator = ReimbursementQuotaCoordinator(engine, staging, max_bytes=100)
    owner = DraftFileOwner("corp-test", "employee-1", draft_id, 1)
    reservation = coordinator.reserve_draft_file(
        owner,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        reserved_bytes=60,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    coordinator.mark_writing(owner, reservation)
    staged = staging.write_bytes(reservation.staging, b"source-document")
    coordinator.finalize(owner, reservation, staged)

    with Session(engine) as database:
        draft = database.get(ReimbursementDraft, draft_id)
        source = database.get(ReimbursementDraftFile, reservation.record_id)
        assert draft is not None and source is not None
        submission = _new_generating_submission(draft)
        database.add(submission)
        database.flush()
        database.add(
            ReimbursementUpload(
                submission_id=submission.id,
                draft_id=draft.id,
                source_draft_file_id=source.id,
                role=ReimbursementUploadRole.ORIGINAL.value,
                sort_order=0,
                local_storage_key=source.storage_key,
                local_part_storage_key=None,
                local_status=ReimbursementUploadLocalStatus.READY.value,
                reserved_bytes=source.reserved_bytes,
                reservation_expires_at=None,
                file_name=source.original_name,
                file_type=source.extension,
                media_type=source.media_type,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
                upload_status="PENDING",
                status_version=1,
                attempt_count=0,
            )
        )
        database.commit()
        submission_id = submission.id
    engine.dispose()

    restarted_engine = create_database_engine(database_url)
    restarted = ReimbursementQuotaCoordinator(restarted_engine, staging, max_bytes=100)
    assert restarted.usage().reserved_bytes == 60
    lease = SubmissionLease(
        "corp-test",
        "employee-1",
        submission_id,
        "lease-token-1",
        3,
    )
    with pytest.raises(ReimbursementQuotaExceeded):
        restarted.reserve_generated_upload(
            lease,
            sort_order=1,
            file_name="差旅费报销单.xlsx",
            reserved_bytes=50,
            expires_at=utc_now() + timedelta(minutes=5),
        )
    with Session(restarted_engine) as database:
        uploads = database.scalars(
            select(ReimbursementUpload).where(
                ReimbursementUpload.role == ReimbursementUploadRole.GENERATED_EXCEL.value
            )
        ).all()
        assert uploads == []
    assert not list((tmp_path / "staging").rglob("*.xlsx"))

    restarted_engine.dispose()
