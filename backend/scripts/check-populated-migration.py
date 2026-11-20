"""Synthetic old-database fixture for the final, read-only image startup check."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.services.temp_files import spool_directory

_DRAFT = "synthetic-migration-draft"
_FILE_COUNT = 100


def seed() -> None:
    settings = get_settings()
    # This setting affects only the seeding process, not the entrypoint which
    # follows it. Leave the image's configured spool directory absent.
    os.environ["SQLITE_TMPDIR"] = str(settings.temp_dir)
    command.upgrade(Config("/app/alembic.ini"), "20261116_0028")
    now = datetime.now(UTC)
    with sqlite3.connect(make_url(settings.database_url).database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "INSERT INTO reimbursement_drafts "
            "(id,corp_id,owner_user_id,status,revision,department_id,department_name,"
            "template_process_code,template_config_version,schema_fingerprint,input_json,"
            "related_instance_ids_json,expires_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _DRAFT,
                "synthetic-org",
                "synthetic-user",
                "DRAFT",
                1,
                "100",
                "合成部门",
                "PROC-SYNTHETIC",
                1,
                "a" * 64,
                '{"items":[]}',
                "[]",
                (now + timedelta(days=30)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        payload = json.dumps({"syntheticPayload": "x" * 16384})
        for index in range(_FILE_COUNT):
            db.execute(
                "INSERT INTO reimbursement_draft_files "
                "(id,draft_id,sort_order,processing_role,file_status,storage_key,reserved_bytes,"
                "original_name,extension,media_type,size_bytes,sha256,ocr_status,created_at,"
                "updated_at,attachment_kind,purged_at,ocr_result_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"synthetic-file-{index}",
                    _DRAFT,
                    index,
                    "ATTACHMENT_ONLY",
                    "PURGED",
                    f"synthetic/{index}.png",
                    1024,
                    f"synthetic-{index}.png",
                    ".png",
                    "image/png",
                    1024,
                    "b" * 64,
                    "COMPLETE",
                    now.isoformat(),
                    now.isoformat(),
                    "other",
                    now.isoformat(),
                    payload,
                ),
            )
        db.commit()
        # Match the safe partial state left by the originally released 0029.
        db.execute(
            "ALTER TABLE reimbursement_drafts ADD COLUMN kind VARCHAR(16) "
            "DEFAULT 'travel' NOT NULL CONSTRAINT ck_reimbursement_drafts_kind "
            "CHECK (kind IN ('travel', 'general'))"
        )
        db.execute("ALTER TABLE receipt_keyword_mappings ADD COLUMN welfare_type VARCHAR(16)")
    assert not spool_directory(settings).exists()
    print("Seeded a synthetic populated 0028 database with partial 0029 columns")


def verify() -> None:
    settings = get_settings()
    directory = spool_directory(settings)
    assert directory.is_dir() and not directory.is_symlink()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    with sqlite3.connect(make_url(settings.database_url).database) as db:
        head = ScriptDirectory.from_config(Config("/app/alembic.ini")).get_current_head()
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            db.execute("SELECT kind FROM reimbursement_drafts WHERE id=?", (_DRAFT,)).fetchone()[0]
            == "travel"
        )
        assert (
            db.execute(
                "SELECT count(*) FROM reimbursement_draft_files WHERE draft_id=?", (_DRAFT,)
            ).fetchone()[0]
            == _FILE_COUNT
        )
        assert (
            db.execute(
                "SELECT count(*) FROM reimbursement_draft_files "
                "WHERE draft_id=? AND length(ocr_result_json)>16384",
                (_DRAFT,),
            ).fetchone()[0]
            == _FILE_COUNT
        )
    print("Populated WAL database upgrade preserved 100 synthetic file records and constraints")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "verify"))
    args = parser.parse_args()
    (seed if args.action == "seed" else verify)()
