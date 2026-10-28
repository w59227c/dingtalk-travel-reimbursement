from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from test_subsidy_only_migration import mark_submitted, seed_verifying

from app.core.config import get_settings
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import ReimbursementSubmission
from app.services.readiness import _database_ready


@pytest.mark.parametrize("upgrade_existing", [False, True])
def test_attendance_upgrade_preserves_history_and_blocks_lossy_downgrade(
    settings_factory, tmp_path, monkeypatch, upgrade_existing
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'attendance.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        old_id = old_json = None
        if upgrade_existing:
            command.upgrade(config, "20261028_0023")
            with create_session_factory(engine)() as database:
                old = seed_verifying(database, version=10)
                mark_submitted(database, old)
                old_id, old_json = old.id, old.form_snapshot_json
        command.upgrade(config, "head")
        assert _database_ready(engine)
        with create_session_factory(engine)() as database:
            if old_id:
                assert database.get(ReimbursementSubmission, old_id).form_snapshot_json == old_json
            current = seed_verifying(database, version=11)
            mark_submitted(database, current)
            identifier, frozen = current.id, current.form_snapshot_json
        for target in ("20261028_0023", "-1", "20261020_0020"):
            with pytest.raises(RuntimeError, match="version 11"):
                command.downgrade(config, target)
            assert _database_ready(engine)
        with create_session_factory(engine)() as database:
            assert database.get(ReimbursementSubmission, identifier).form_snapshot_json == frozen
            unknown = seed_verifying(database, version=12)
            with pytest.raises(IntegrityError):
                mark_submitted(database, unknown)
            database.rollback()
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_attendance_migration_can_downgrade_an_empty_database(
    settings_factory, tmp_path, monkeypatch
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        command.upgrade(config, "head")
        command.downgrade(config, "20261028_0023")
        assert not _database_ready(engine)
        command.upgrade(config, "head")
        assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
