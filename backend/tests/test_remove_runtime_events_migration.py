from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from test_subsidy_only_migration import seed_verifying

from app.core.config import get_settings
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import ReimbursementSubmission, ReimbursementSubmissionRecoveryAudit
from app.services.readiness import _database_ready


@pytest.mark.parametrize("old_database", [False, True])
def test_retirement_removes_only_auxiliary_events_and_preserves_business_data(
    settings_factory, tmp_path, monkeypatch, old_database
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'retirement.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        if old_database:
            command.upgrade(config, "20261027_0022")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO runtime_events "
                        "(occurred_at,module,event,result,details_json,byte_size) "
                        "VALUES ('2026-10-27', 'system','test','success','{}',512)"
                    )
                )
            with create_session_factory(engine)() as database:
                submission = seed_verifying(database)
                identifier, snapshot = submission.id, submission.form_snapshot_json
                audit = ReimbursementSubmissionRecoveryAudit(
                    submission_id=identifier,
                    corp_id=submission.corp_id,
                    admin_user_id="synthetic-admin",
                    action="ATTACH_INSTANCE",
                    verification_note="合成核对依据",
                    process_instance_id=submission.process_instance_id,
                    status_before="MANUAL_REVIEW",
                    status_after="VERIFYING",
                    status_version_before=1,
                )
                database.add(audit)
                database.commit()
                audit_id = audit.id
        command.upgrade(config, "head")
        assert _database_ready(engine)
        assert "runtime_events" not in inspect(engine).get_table_names()
        if old_database:
            with create_session_factory(engine)() as database:
                submission = database.get(ReimbursementSubmission, identifier)
                assert submission.form_snapshot_json == snapshot
                assert submission.status == "VERIFYING"
                assert (
                    database.get(ReimbursementSubmissionRecoveryAudit, audit_id).verification_note
                    == "合成核对依据"
                )
            # A multi-revision downgrade must fail before any new DDL/data loss.
            with pytest.raises(RuntimeError, match="version 10 snapshots"):
                command.downgrade(config, "20261020_0020")
            assert _database_ready(engine)
            with pytest.raises(RuntimeError, match="version 10 snapshots"):
                command.downgrade(config, "-2")
            assert _database_ready(engine)
        command.downgrade(config, "20261027_0022")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM runtime_events")) == 0
        command.upgrade(config, "head")
        assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
