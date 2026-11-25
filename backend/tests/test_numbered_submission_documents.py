from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select
from test_oa_reimbursement_payload import _source
from test_reimbursement_submissions import _create, _new_ready_draft
from test_subsidy_only_migration import seed_verifying

from app.database.base import Base
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import ReimbursementDraft, ReimbursementSubmission, utc_now
from app.services.oa_reimbursement_payload import (
    build_snapshot,
    parse_snapshot,
    serialize_snapshot,
    snapshot_sha256,
)
from app.services.reimbursement_drafts import DraftActor
from app.services.reimbursement_submissions import create_submission

ACTOR = DraftActor("synthetic-corp", "synthetic-user", "100", "合成部门")


@pytest.fixture
def database(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'numbering.db'}")
    Base.metadata.create_all(engine)
    try:
        with create_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


def create_numbered(database, draft_id, *, actor=ACTOR, now=None):
    source = _source()
    identity = source.identity.model_copy(
        update={
            "corp_id": actor.corp_id,
            "user_id": actor.user_id,
            "name": "合成员工",
            "department_id": actor.department_id,
            "department_name": actor.department_name,
        }
    )
    snapshot = build_snapshot(replace(source, draft_id=draft_id, identity=identity))
    return create_submission(
        database,
        actor=actor,
        draft_id=draft_id,
        expected_revision=4,
        originator_union_id=identity.union_id,
        originator_name=identity.name,
        idempotency_key="synthetic-document-request",
        snapshot_version=snapshot.snapshot_version,
        schema_fingerprint=snapshot.template.schema_fingerprint,
        form_snapshot_json=serialize_snapshot(snapshot),
        snapshot_sha256=snapshot_sha256(snapshot),
        now=now,
    )


def read_naming(database, submission_id):
    row = database.get(ReimbursementSubmission, submission_id)
    return parse_snapshot(row.form_snapshot_json, expected_sha256=row.snapshot_sha256)


def test_numbering_includes_both_types_and_legacy_attempts_and_is_scoped_to_beneficiary(database):
    # An existing general claim and legacy travel attempt both occupy today's sequence.
    seed_verifying(
        database,
        version=12,
        kind="general",
        mode="bundle_and_excel",
        roles=("GENERATED_PDF", "GENERATED_EXCEL"),
        files=({"draftFileId": "synthetic-file"},),
    )
    old, _ = _new_ready_draft(database, actor=ACTOR)
    _create(database, old, actor=ACTOR)
    draft, _ = _new_ready_draft(database, actor=ACTOR)
    created = create_numbered(database, draft.id)
    snapshot = read_naming(database, created.submission_id)
    assert snapshot.document_naming.sequence == 3
    assert snapshot.excel.file_name.endswith("-3.xlsx")
    frozen_json = database.get(ReimbursementSubmission, created.submission_id).form_snapshot_json
    retried = create_numbered(database, draft.id, now=utc_now() + timedelta(days=1))
    assert not retried.created and retried.submission_id == created.submission_id
    assert (
        database.get(ReimbursementSubmission, created.submission_id).form_snapshot_json
        == frozen_json
    )
    for actor in (replace(ACTOR, user_id="another-user"), replace(ACTOR, corp_id="another-corp")):
        other, _ = _new_ready_draft(database, actor=actor)
        result = create_numbered(database, other.id, actor=actor)
        assert read_naming(database, result.submission_id).document_naming.sequence == 1


def test_numbering_uses_shanghai_midnight_and_ignores_operator_and_department(database):
    before = datetime(2026, 11, 25, 15, 59, 59, tzinfo=UTC)
    names = []
    for moment, department, operator in (
        (before, "100", None),
        (before, "200", "synthetic-agent"),
        (before + timedelta(seconds=1), "100", None),
    ):
        actor = replace(ACTOR, department_id=department, operator_user_id=operator)
        draft, _ = _new_ready_draft(database, actor=actor)
        result = create_numbered(database, draft.id, actor=actor, now=moment)
        names.append(read_naming(database, result.submission_id).document_naming)
    assert [(item.submission_date, item.sequence) for item in names] == [
        (date(2026, 11, 25), 1),
        (date(2026, 11, 25), 2),
        (date(2026, 11, 26), 1),
    ]


def test_concurrent_claims_receive_distinct_numbers_and_failed_claims_do_not_consume_one(database):
    first, _ = _new_ready_draft(database, actor=ACTOR)
    second, _ = _new_ready_draft(database, actor=ACTOR)
    draft_ids = (first.id, second.id)
    factory = create_session_factory(database.get_bind())
    barrier = Barrier(2)

    def submit(draft_id):
        with factory() as session:
            barrier.wait(timeout=5)
            return create_numbered(session, draft_id).submission_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, draft_ids))
    database.expire_all()
    assert sorted(read_naming(database, item).document_naming.sequence for item in results) == [
        1,
        2,
    ]
    assert database.scalar(
        select(ReimbursementSubmission).where(ReimbursementSubmission.draft_id == first.id)
    )
    # A stale revision is rejected before issuing or persisting another name.
    stale, _ = _new_ready_draft(database, actor=ACTOR)
    database.get(ReimbursementDraft, stale.id).revision += 1
    database.commit()
    from app.core.errors import ApiError

    with pytest.raises(ApiError):
        create_numbered(database, stale.id)
    assert len(database.scalars(select(ReimbursementSubmission)).all()) == 2
