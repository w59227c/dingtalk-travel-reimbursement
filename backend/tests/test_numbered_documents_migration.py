from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event
from sqlalchemy.exc import OperationalError
from test_subsidy_only_migration import seed_verifying

from app.core.config import get_settings
from app.database.session import create_database_engine, create_session_factory
from app.services.readiness import _database_ready


@pytest.mark.parametrize("populated", [False, True])
def test_0030_preserves_old_snapshots_and_protects_numbered_history(
    settings_factory, monkeypatch, tmp_path, populated
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'numbered.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        command.upgrade(config, "20261119_0029")
        with factory() as session:
            old = seed_verifying(session, version=11)
            old_id, old_json = old.id, old.form_snapshot_json
        command.upgrade(config, "head")
        assert _database_ready(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert (
                connection.exec_driver_sql(
                    "SELECT form_snapshot_json FROM reimbursement_submissions WHERE id=?", (old_id,)
                ).scalar_one()
                == old_json
            )
        if populated:
            with factory() as session:
                seed_verifying(session, version=13)
            with pytest.raises(RuntimeError, match="Cannot downgrade numbered"):
                command.downgrade(config, "20261119_0029")
            assert _database_ready(engine)
        else:
            command.downgrade(config, "20261119_0029")
            command.upgrade(config, "head")
            assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_0030_trigger_replacement_is_atomic(settings_factory, monkeypatch, tmp_path):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'atomic.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)

    def fail_second_trigger(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().startswith(
            "CREATE TRIGGER trg_reimbursement_submissions_bundle_manifest_update"
        ):
            raise OperationalError(statement, {}, RuntimeError("synthetic DDL failure"))

    try:
        command.upgrade(config, "20261119_0029")
        with engine.connect() as connection:
            before = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).all()
        event.listen(Engine, "before_cursor_execute", fail_second_trigger)
        try:
            with pytest.raises(OperationalError, match="synthetic DDL failure"):
                command.upgrade(config, "head")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_second_trigger)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "20261119_0029"
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
                ).all()
                == before
            )
        command.upgrade(config, "head")
        assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
