from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.core.config import get_settings
from app.database.session import create_database_engine
from app.services.readiness import _database_ready


@pytest.mark.parametrize("old", [False, True])
def test_runtime_events_migration_preserves_old_data_and_protects_evidence(
    tmp_path, monkeypatch, old
):
    database_url = f"sqlite:///{tmp_path / 'runtime-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = create_database_engine(database_url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    try:
        if old:
            command.upgrade(config, "20261022_0021")
            assert not _database_ready(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO settings (key,value) "
                        "VALUES ('synthetic-setting','preserve-me')"
                    )
                )
        command.upgrade(config, "head")
        assert _database_ready(engine)
        assert {index["name"] for index in inspect(engine).get_indexes("runtime_events")} == {
            "ix_runtime_events_occurred_at",
            "ix_runtime_events_request_id",
        }
        if old:
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT value FROM settings WHERE key='synthetic-setting'")
                    )
                    == "preserve-me"
                )
        command.downgrade(config, "20261022_0021")
        assert not _database_ready(engine)
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO runtime_events "
                    "(occurred_at,module,event,result,request_id,details_json,byte_size) "
                    "VALUES ('2026-10-27 00:00:00','system','request','success',"
                    "'synthetic-request','{}',514)"
                )
            )
        with pytest.raises(RuntimeError, match="operational evidence"):
            command.downgrade(config, "20261022_0021")
        assert _database_ready(engine)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM runtime_events")) == 1
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize("target", ["20261022_0021", "-1"])
def test_empty_runtime_table_allows_previous_revision_with_v10_snapshot(
    tmp_path, monkeypatch, target
):
    from test_subsidy_only_migration import seed_verifying

    from app.database.session import create_session_factory
    from app.models.reimbursement import ReimbursementSubmission

    database_url = f"sqlite:///{tmp_path / 'safe-downgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = create_database_engine(database_url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    try:
        command.upgrade(config, "head")
        with create_session_factory(engine)() as database:
            submission = seed_verifying(database)
            submission_id = submission.id
            snapshot = submission.form_snapshot_json
        command.downgrade(config, target)
        with create_session_factory(engine)() as database:
            assert (
                database.get(ReimbursementSubmission, submission_id).form_snapshot_json == snapshot
            )
        command.upgrade(config, "head")
        assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
