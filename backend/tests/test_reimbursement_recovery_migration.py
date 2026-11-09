from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from test_subsidy_only_migration import seed_verifying

from app.core.config import get_settings
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import ReimbursementDraft, ReimbursementSubmission
from app.services.readiness import _database_ready


@pytest.mark.parametrize("existing", [False, True])
def test_current_workspace_migration_preserves_snapshots_and_retirement(
    settings_factory,
    tmp_path,
    monkeypatch,
    existing,
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'recovery.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        identifiers = []
        if existing:
            command.upgrade(config, "20261028_0024")
            with create_session_factory(engine)() as database:
                for _ in range(2):
                    task = seed_verifying(database, version=11)
                    identifiers.append((task.id, task.draft_id, task.form_snapshot_json))
                database.commit()
        command.upgrade(config, "head")
        assert _database_ready(engine)
        assert {"workspace_retired", "recovery_pending", "recovery_source_draft_id"} <= {
            column["name"] for column in inspect(engine).get_columns("reimbursement_drafts")
        }
        if existing:
            with create_session_factory(engine)() as database:
                for task_id, _, snapshot in identifiers:
                    assert (
                        database.get(ReimbursementSubmission, task_id).form_snapshot_json
                        == snapshot
                    )
                active = database.query(ReimbursementDraft).filter_by(workspace_retired=False).all()
                assert len(active) == 1 and active[0].id == identifiers[-1][1]
            with pytest.raises(CommandError, match="recovery relationships"):
                command.downgrade(config, "20261028_0024")
            assert _database_ready(engine)
        else:
            command.downgrade(config, "20261028_0024")
            assert "workspace_retired" not in {
                column["name"] for column in inspect(engine).get_columns("reimbursement_drafts")
            }
            command.upgrade(config, "head")
            with create_session_factory(engine)() as database:
                task = seed_verifying(database, version=11)
                source = database.get(ReimbursementDraft, task.draft_id)
                fields = {
                    column.name: getattr(source, column.name)
                    for column in source.__table__.columns
                    if column.name not in {"id", "created_at", "updated_at"}
                }
                fields.update(
                    status="DRAFT",
                    revision=1,
                    locked_at=None,
                    recovery_source_draft_id=source.id,
                    recovery_pending=True,
                )
                target = ReimbursementDraft(**fields)
                database.add(target)
                database.commit()
                database.add(ReimbursementDraft(**fields))
                with pytest.raises(IntegrityError):
                    database.commit()
                database.rollback()
            with pytest.raises(CommandError, match="recovery relationships"):
                command.downgrade(config, "20261028_0024")
            assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
