from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_oa_reimbursement_payload import _source
from test_reimbursement_drafts import _add_history_submission

from app.core.config import get_settings
from app.database.session import create_database_engine
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementSubmission,
    ReimbursementUpload,
    utc_now,
)
from app.services.oa_reimbursement_payload import parse_locked_submission_snapshot
from app.services.readiness import _database_ready

_PREVIOUS = "20261116_0028"


def test_0029_preserves_a_completed_travel_claim_and_guards_downgrade(
    settings_factory, tmp_path, monkeypatch
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'populated-travel.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    draft_id = "synthetic-travel-history"
    try:
        # Seed a compatible record and use Alembic to create the actual old schema.
        command.upgrade(config, "head")
        with Session(engine) as database:
            now = utc_now()
            saved = ReimbursementDraft(
                id=draft_id,
                corp_id="synthetic-org",
                owner_user_id="synthetic-employee",
                department_id="100",
                department_name="合成部门",
                template_process_code="PROC-REIMBURSEMENT",
                template_config_version=7,
                schema_fingerprint="a" * 64,
                input_json='{"items":[]}',
                expires_at=now + timedelta(days=1),
                status="LOCKED",
                locked_at=now,
            )
            _add_history_submission(database, saved, status="SUBMITTED")
            originals = _source().original_files
            for item in originals:
                database.add(
                    ReimbursementDraftFile(
                        id=item.draft_file_id,
                        draft_id=draft_id,
                        sort_order=item.sort_order,
                        processing_role=item.processing_role,
                        file_status="ACTIVE",
                        storage_key=item.storage_key,
                        original_name=item.file_name,
                        extension=f".{item.file_type}",
                        media_type=item.media_type,
                        reserved_bytes=item.size_bytes,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                    )
                )
            database.commit()
        command.downgrade(config, _PREVIOUS)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREVIOUS
            frozen = connection.execute(
                text("SELECT form_snapshot_json, snapshot_sha256 FROM reimbursement_submissions")
            ).one()
        command.upgrade(config, "head")
        with Session(engine) as database:
            restored = database.get(ReimbursementDraft, draft_id)
            submission = database.scalar(select(ReimbursementSubmission))
            assert restored.kind == "travel"
            assert (submission.form_snapshot_json, submission.snapshot_sha256) == frozen
            snapshot = parse_locked_submission_snapshot(restored, submission)
            files = database.scalars(
                select(ReimbursementDraftFile).order_by(ReimbursementDraftFile.sort_order)
            ).all()
            assert [(item.id, item.sha256) for item in files] == [
                (item.draft_file_id, item.sha256) for item in snapshot.original_files
            ]
            uploads = database.scalars(
                select(ReimbursementUpload).order_by(ReimbursementUpload.sort_order)
            ).all()
            assert [(item.role, item.upload_status) for item in uploads] == [
                ("GENERATED_PDF", "LINKED"),
                ("GENERATED_EXCEL", "LINKED"),
            ]
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            connection.exec_driver_sql(
                "INSERT INTO receipt_keyword_mappings "
                "(keyword, normalized_keyword, category_id, welfare_type) "
                "VALUES ('合成寄递词', '合成寄递词', 'employee_welfare', 'express')"
            )
        with pytest.raises(RuntimeError, match="Cannot discard reimbursement data"):
            command.downgrade(config, _PREVIOUS)
        assert _database_ready(engine)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM reimbursement_draft_files")) == 2
            assert connection.scalar(text("SELECT count(*) FROM reimbursement_uploads")) == 2
            assert connection.scalar(
                text("SELECT welfare_type FROM receipt_keyword_mappings "
                     "WHERE normalized_keyword = '合成寄递词'")
            ) == "express"
    finally:
        engine.dispose()
        get_settings.cache_clear()
