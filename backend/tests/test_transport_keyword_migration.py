from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import create_database_engine
from app.services import readiness


@pytest.mark.parametrize("old_database", [False, True])
def test_transport_keyword_migration_preserves_custom_rules_and_guards_downgrade(
    settings_factory,
    tmp_path,
    monkeypatch,
    old_database,
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'transport-keywords.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        if old_database:
            command.upgrade(config, "20260915_0019")
            assert not readiness._database_ready(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO receipt_keyword_mappings "
                        "(keyword, normalized_keyword, category_id) "
                        "VALUES ('滴滴出行', '滴滴出行', 'office')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO receipt_keyword_mappings "
                        "(keyword, normalized_keyword, category_id) "
                        "VALUES ('滴滴科技', '滴滴科技', 'local_transport')"
                    )
                )
        command.upgrade(config, "head")
        assert readiness._database_ready(engine)
        with engine.connect() as connection:
            rows = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    text(
                        "SELECT normalized_keyword, category_id, transport_type "
                        "FROM receipt_keyword_mappings"
                    )
                )
            }
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261020_0020"
            )
        assert rows["滴滴畅行"] == ("local_transport", "ride_hailing")
        assert rows["滴滴科技"] == ("local_transport", "ride_hailing")
        assert rows["网约车"] == ("local_transport", "ride_hailing")
        for operator in (
            "南京领行科技",
            "长燃智享",
            "享道出行",
            "郑州哈拜网络科技",
            "杭州携华网络科技",
        ):
            assert rows[operator] == ("local_transport", "ride_hailing")
        assert rows["出租车"] == ("local_transport", "taxi")
        assert rows["燃油附加费"] == ("airfare", None)
        assert rows["客运服务"] == ("local_transport", None)
        assert rows["滴滴出行"] == (
            ("office", None) if old_database else ("local_transport", "ride_hailing")
        )
        with pytest.raises(RuntimeError, match="transport keyword groups"):
            command.downgrade(config, "20260915_0019")
        assert readiness._database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()
