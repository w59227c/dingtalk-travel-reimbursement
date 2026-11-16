from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy.exc import IntegrityError
from test_reimbursement_models import new_draft, new_submission

from app.core.config import get_settings
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import ReimbursementDraft, ReimbursementSubmission, utc_now
from app.models.setting import Setting
from app.services.readiness import _database_ready

PRIOR = "20261109_0026"


def test_delegation_migration_preserves_legacy_and_protects_operator(
    settings_factory, tmp_path, monkeypatch,
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'delegation.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        command.upgrade(config, PRIOR)
        with factory() as database:
            personal = new_draft()
            personal.status, personal.locked_at = "LOCKED", utc_now()
            database.add(personal)
            database.flush()
            existing = new_submission(personal)
            database.add(existing)
            database.commit()
            personal_id, existing_id = personal.id, existing.id
            original_snapshot = existing.form_snapshot_json

        command.upgrade(config, "head")
        assert _database_ready(engine)
        with factory() as database:
            assert database.get(ReimbursementDraft, personal_id).operator_user_id is None
            existing = database.get(ReimbursementSubmission, existing_id)
            assert existing.operator_user_id is None
            assert existing.form_snapshot_json == original_snapshot
        command.downgrade(config, PRIOR)
        command.upgrade(config, "head")

        with factory() as database:
            draft = new_draft()
            draft.operator_user_id = "synthetic-operator"
            draft.status, draft.locked_at = "LOCKED", utc_now()
            database.add(draft)
            database.commit()
            draft.operator_user_id = "different-operator"
            with pytest.raises(IntegrityError, match="operator is immutable"):
                database.commit()
            database.rollback()
            wrong = new_submission(draft)
            database.add(wrong)
            with pytest.raises(IntegrityError, match="operator differs"):
                database.commit()
            database.rollback()
            valid = new_submission(draft)
            valid.operator_user_id = draft.operator_user_id
            database.add(valid)
            database.commit()
            valid.operator_user_id = None
            with pytest.raises(IntegrityError, match="operator is immutable"):
                database.commit()
            database.rollback()
        with pytest.raises(CommandError, match="delegation records or audit evidence"):
            command.downgrade(config, PRIOR)
        assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize("department_grants", ["[]", '["101"]'])
def test_downgrade_preserves_department_administrator_policy(
    department_grants, settings_factory, tmp_path, monkeypatch,
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'admin-departments.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    key = "admin_department_ids:synthetic-corp"
    try:
        command.upgrade(config, "head")
        with factory() as database:
            database.add(Setting(key=key, value=department_grants))
            database.commit()

        if department_grants == "[]":
            command.downgrade(config, PRIOR)
        else:
            with pytest.raises(CommandError, match="department administrator settings"):
                command.downgrade(config, PRIOR)
            assert _database_ready(engine)
        with factory() as database:
            assert database.get(Setting, key).value == department_grants
    finally:
        engine.dispose()
        get_settings.cache_clear()
