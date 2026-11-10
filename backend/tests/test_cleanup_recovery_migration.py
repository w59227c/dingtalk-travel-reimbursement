from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from test_reimbursement_models import new_draft, new_ready_upload, new_submission

from app.core.config import get_settings
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import (
    ReimbursementSubmission,
    ReimbursementSubmissionRecoveryAudit,
    ReimbursementUploadRole,
    utc_now,
)
from app.services.readiness import _database_ready


def audit_for(task, version):
    return ReimbursementSubmissionRecoveryAudit(
        submission_id=task.id,
        corp_id=task.corp_id,
        admin_user_id="synthetic-admin",
        action="CONFIRM_NOT_CREATED",
        verification_note=f"Synthetic decision {version}",
        status_before="MANUAL_REVIEW",
        status_after="ORPHAN_CLEANUP",
        status_version_before=version,
    )


@pytest.mark.parametrize("existing", [False, True])
def test_cleanup_migration_preserves_evidence_and_guards_downgrades(
    settings_factory,
    tmp_path,
    monkeypatch,
    existing,
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'cleanup-migration.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        if existing:
            command.upgrade(config, "20261108_0025")
            with create_session_factory(engine)() as database:
                source = new_draft()
                source.status, source.locked_at = "LOCKED", utc_now()
                database.add(source)
                database.flush()
                task = new_submission(source)
                task.status = "ORPHAN_CLEANUP"
                task.orphan_confirmed_at = utc_now()
                task.orphan_confirmation_code = "ADMIN_CONFIRMED_NOT_CREATED"
                database.add(task)
                database.flush()
                upload = new_ready_upload(
                    task, sort_order=0, role=ReimbursementUploadRole.GENERATED_PDF
                )
                upload.upload_status = "CLEANUP_PENDING"
                upload.space_id, upload.file_id = "synthetic-space", "synthetic-file"
                upload.commit_started_at = upload.cleanup_started_at = utc_now()
                database.add_all([upload, audit_for(task, 1)])
                database.commit()
                identifier, frozen = task.id, task.form_snapshot_json
                task.status = "MANUAL_REVIEW"
                with pytest.raises(IntegrityError):
                    database.commit()
                database.rollback()
        command.upgrade(config, "head")
        assert _database_ready(engine)
        uniques = inspect(engine).get_unique_constraints("reimbursement_submission_recovery_audits")
        assert any(
            set(item["column_names"]) == {"submission_id", "status_version_before"}
            for item in uniques
        )
        if not existing:
            command.downgrade(config, "20261108_0025")
            command.upgrade(config, "head")
            assert _database_ready(engine)
            return
        with create_session_factory(engine)() as database:
            task = database.get(ReimbursementSubmission, identifier)
            assert task.form_snapshot_json == frozen
            assert database.query(ReimbursementSubmissionRecoveryAudit).one().verification_note == (
                "Synthetic decision 1"
            )
            task.status, task.status_version = "MANUAL_REVIEW", 2
            database.commit()
            task.status, task.process_instance_id = "VERIFYING", "forbidden-instance"
            with pytest.raises(IntegrityError):
                database.commit()
            database.rollback()
        with pytest.raises(CommandError, match="cleanup is suspended"):
            command.downgrade(config, "20261108_0025")
        assert _database_ready(engine)
        with create_session_factory(engine)() as database:
            task = database.get(ReimbursementSubmission, identifier)
            task.status = "ORPHAN_CLEANUP"
            database.add(audit_for(task, 2))
            database.commit()
        with pytest.raises(CommandError, match="repeated administrator recovery"):
            command.downgrade(config, "20261108_0025")
        assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
