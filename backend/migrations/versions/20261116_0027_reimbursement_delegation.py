"""Preserve reimbursement operator separately from the beneficiary.

Revision ID: 20261116_0027
Revises: 20261109_0026
"""

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory
from alembic.util import CommandError

revision = "20261116_0027"
down_revision = "20261109_0026"
branch_labels = None
depends_on = None

_TABLES = ("reimbursement_drafts", "reimbursement_submissions")


def upgrade():
    # SQLite can add/drop a nullable column without rebuilding tables or
    # disturbing existing snapshot, attachment and recovery triggers.
    for table in _TABLES:
        op.add_column(table, sa.Column("operator_user_id", sa.String(128), nullable=True))
        op.execute(f"""
            CREATE TRIGGER trg_{table}_operator_immutable
            BEFORE UPDATE OF operator_user_id ON {table}
            WHEN NEW.operator_user_id IS NOT OLD.operator_user_id
            BEGIN
                SELECT RAISE(ABORT, 'Reimbursement operator is immutable');
            END
        """)
    op.execute("""
        CREATE TRIGGER trg_reimbursement_drafts_operator_insert
        BEFORE INSERT ON reimbursement_drafts
        WHEN (
            NEW.operator_user_id IS NOT NULL
            AND (
                NEW.operator_user_id = NEW.owner_user_id
                OR length(trim(NEW.operator_user_id)) NOT BETWEEN 1 AND 128
            )
        ) OR (
            NEW.recovery_source_draft_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM reimbursement_drafts AS source
                WHERE source.id = NEW.recovery_source_draft_id
                  AND source.corp_id = NEW.corp_id
                  AND source.owner_user_id = NEW.owner_user_id
                  AND source.operator_user_id IS NEW.operator_user_id
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'Invalid reimbursement operator');
        END
    """)
    op.execute("""
        CREATE TRIGGER trg_reimbursement_submissions_operator_insert
        BEFORE INSERT ON reimbursement_submissions
        WHEN NOT EXISTS (
            SELECT 1 FROM reimbursement_drafts AS draft
            WHERE draft.id = NEW.draft_id
              AND draft.corp_id = NEW.corp_id
              AND draft.owner_user_id = NEW.originator_user_id
              AND draft.operator_user_id IS NEW.operator_user_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'Submission operator differs from draft operator');
        END
    """)


def _preflight_downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text(
        "SELECT 1 FROM settings WHERE key GLOB 'reimbursement_delegation:*' LIMIT 1"
    )):
        raise CommandError("Cannot discard reimbursement delegation settings")
    if bind.scalar(sa.text(
        "SELECT 1 FROM settings WHERE key GLOB 'admin_department_ids:*' "
        "AND value != '[]' LIMIT 1"
    )):
        raise CommandError("Cannot discard department administrator settings")
    for table in _TABLES:
        if bind.scalar(sa.text(
            f"SELECT 1 FROM {table} WHERE operator_user_id IS NOT NULL LIMIT 1"
        )):
            raise CommandError("Cannot discard reimbursement delegation records or audit evidence")
    context = op.get_context()
    destination = context.opts.get("destination_rev")
    if destination not in {down_revision, "-1"}:
        # Check 0026 and its earlier migration guards before any SQLite DDL.
        previous = ScriptDirectory.from_config(context.config).get_revision(down_revision)
        previous.module._preflight_downgrade()


def downgrade():
    _preflight_downgrade()
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_operator_insert")
        op.execute(f"DROP TRIGGER trg_{table}_operator_immutable")
        op.drop_column(table, "operator_user_id")
