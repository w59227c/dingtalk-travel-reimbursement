from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import create_database_engine
from app.services.readiness import _database_ready

PRIOR = "20261116_0027"


@pytest.mark.parametrize("existing_catalog", [False, True])
def test_oa_draft_binding_migration(settings_factory, tmp_path, monkeypatch, existing_catalog):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'catalog.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        if existing_catalog:
            command.upgrade(config, PRIOR)
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO oa_template_profiles (
                        profile_key, process_code, template_name, schema_fingerprint,
                        confirmed_schema_fingerprint, schema_json, mapping_json,
                        config_version, allowed_travel_process_codes_json, travel_profiles_json,
                        compatibility_status, confirmed_by_user_id, last_checked_at, confirmed_at,
                        created_at, updated_at
                    ) VALUES (
                        'reimbursement', 'PROC-TEST', '合成模板', 'test', 'test', '{}', '{}',
                        7, '[]', '[]', 'COMPATIBLE', 'synthetic-admin', CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                """))
        command.upgrade(config, "head")
        assert _database_ready(engine)
        command.downgrade(config, PRIOR)
        command.upgrade(config, "head")
        assert _database_ready(engine)
        if existing_catalog:
            with engine.begin() as connection:
                assert connection.execute(text(
                    "SELECT config_version, draft_binding_version FROM oa_template_profiles"
                )).one() == (7, None)
                connection.execute(text(
                    "UPDATE oa_template_profiles SET config_version = 8, draft_binding_version = 7"
                ))
            with pytest.raises(CommandError, match="preserved OA draft binding"):
                command.downgrade(config, PRIOR)
            assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
