import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event, inspect, select, text
from sqlalchemy.exc import OperationalError
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


@pytest.mark.parametrize("partial", ["kind", "both"])
def test_0029_resumes_columns_left_by_an_interrupted_upgrade(
    settings_factory, tmp_path, monkeypatch, partial
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'partial.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        command.upgrade(config, _PREVIOUS)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE reimbursement_drafts ADD COLUMN kind VARCHAR(16) "
                "DEFAULT 'travel' NOT NULL CONSTRAINT ck_reimbursement_drafts_kind "
                "CHECK (kind IN ('travel', 'general'))"
            )
            if partial == "both":
                connection.exec_driver_sql(
                    "ALTER TABLE receipt_keyword_mappings ADD COLUMN welfare_type VARCHAR(16)"
                )
                connection.exec_driver_sql(
                    "INSERT INTO receipt_keyword_mappings "
                    "(keyword, normalized_keyword, category_id, welfare_type) "
                    "VALUES ('合成自定义福利', '合成自定义福利', 'employee_welfare', 'express')"
                )
        command.upgrade(config, "head")
        assert _database_ready(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            ddl = connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE name='reimbursement_draft_files'")
            )
            assert "'waybill'" in ddl
            if partial == "both":
                assert (
                    connection.scalar(
                        text(
                            "SELECT welfare_type FROM receipt_keyword_mappings "
                            "WHERE normalized_keyword='合成自定义福利'"
                        )
                    )
                    == "express"
                )
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_0029_rolls_back_all_ddl_when_rebuilding_files_fails(
    settings_factory, tmp_path, monkeypatch
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'interrupted.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    injected = False

    def fail_drop(_connection, _cursor, statement, _parameters, _context, _executemany):
        nonlocal injected
        if statement.strip() == "DROP TABLE reimbursement_draft_files" and not injected:
            injected = True
            raise OperationalError(
                statement, {}, sqlite3.OperationalError("synthetic disk I/O error")
            )

    try:
        command.upgrade(config, _PREVIOUS)
        with engine.connect() as connection:
            before_triggers = connection.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
            ).all()
        event.listen(Engine, "before_cursor_execute", fail_drop)
        try:
            with pytest.raises(OperationalError, match="synthetic disk I/O error"):
                command.upgrade(config, "head")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_drop)
        assert injected
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREVIOUS
            assert "kind" not in {
                c["name"] for c in inspect(connection).get_columns("reimbursement_drafts")
            }
            assert "welfare_type" not in {
                c["name"] for c in inspect(connection).get_columns("receipt_keyword_mappings")
            }
            assert (
                connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
                ).all()
                == before_triggers
            )
            assert (
                "_alembic_tmp_reimbursement_draft_files"
                not in inspect(connection).get_table_names()
            )
        command.upgrade(config, "head")
        assert _database_ready(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "definition",
    [
        "kind TEXT DEFAULT 'travel' NOT NULL CHECK (kind IN ('travel', 'general'))",
        "kind VARCHAR(16) DEFAULT 'travel' NOT NULL",
        "kind VARCHAR(16) DEFAULT 'travel' CHECK (kind IN ('travel', 'general'))",
        "welfare_type VARCHAR(8)",
    ],
)
def test_0029_refuses_an_unrecognized_partial_column(
    settings_factory, tmp_path, monkeypatch, definition
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'wrong-column.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    table = (
        "receipt_keyword_mappings"
        if definition.startswith("welfare_type")
        else "reimbursement_drafts"
    )
    try:
        command.upgrade(config, _PREVIOUS)
        with engine.begin() as connection:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {definition}")
        with engine.connect() as connection:
            before = connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'trigger') ORDER BY name"
                )
            ).all()
        with pytest.raises(RuntimeError, match="Cannot resume 0029"):
            command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREVIOUS
            assert (
                connection.execute(
                    text(
                        "SELECT name, sql FROM sqlite_master "
                        "WHERE type IN ('table', 'trigger') ORDER BY name"
                    )
                ).all()
                == before
            )
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize("partial", [False, True])
def test_0029_preserves_a_completed_travel_claim_and_guards_downgrade(
    settings_factory, tmp_path, monkeypatch, partial
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
        if partial:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE reimbursement_drafts ADD COLUMN kind VARCHAR(16) "
                    "DEFAULT 'travel' NOT NULL CONSTRAINT ck_reimbursement_drafts_kind "
                    "CHECK (kind IN ('travel', 'general'))"
                )
                connection.exec_driver_sql(
                    "ALTER TABLE receipt_keyword_mappings ADD COLUMN welfare_type VARCHAR(16)"
                )
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
            assert (
                connection.scalar(
                    text(
                        "SELECT welfare_type FROM receipt_keyword_mappings "
                        "WHERE normalized_keyword = '合成寄递词'"
                    )
                )
                == "express"
            )
    finally:
        engine.dispose()
        get_settings.cache_clear()
