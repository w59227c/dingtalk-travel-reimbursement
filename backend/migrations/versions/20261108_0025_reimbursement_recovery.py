"""Keep a durable, idempotent copy of a definitively failed reimbursement.

Revision ID: 20261108_0025
Revises: 20261028_0024
"""

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory
from alembic.util import CommandError

revision = "20261108_0025"
down_revision = "20261028_0024"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite supports a nullable REFERENCES column without rebuilding the table
    # or disturbing its existing submission/snapshot protection triggers.
    op.execute(
        sa.text(
            "ALTER TABLE reimbursement_drafts ADD COLUMN recovery_source_draft_id "
            "VARCHAR(36) REFERENCES reimbursement_drafts(id) ON DELETE RESTRICT"
        )
    )
    op.add_column(
        "reimbursement_drafts",
        sa.Column(
            "recovery_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "reimbursement_drafts",
        sa.Column("workspace_retired", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.execute(
        sa.text("""
        UPDATE reimbursement_drafts SET workspace_retired = 1 WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY corp_id, owner_user_id, department_id, department_name
                    ORDER BY created_at DESC, id DESC
                ) AS position FROM reimbursement_drafts
            ) WHERE position > 1
        )
    """)
    )
    op.create_index(
        "uq_reimbursement_drafts_recovery_source",
        "reimbursement_drafts",
        ["recovery_source_draft_id"],
        unique=True,
    )


def downgrade():
    # Preflight downstream guards before any SQLite DDL (DDL is not rolled back
    # by all supported pysqlite configurations).
    context = op.get_context()
    destination = context.opts.get("destination_rev")
    target = None
    if context.config is not None and isinstance(destination, str):
        scripts = ScriptDirectory.from_config(context.config)
        try:
            if destination.startswith("-") and destination[1:].isdigit():
                resolved = scripts.get_revision(context.get_current_revision())
                for _ in range(int(destination[1:])):
                    resolved = scripts.get_revision(resolved.down_revision) if resolved else None
            else:
                resolved = scripts.get_revision(destination)
            target = resolved.revision if resolved else None
        except CommandError:
            pass
    if target != down_revision and op.get_bind().scalar(
        sa.text("SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 11")
    ):
        raise RuntimeError("Cannot downgrade while version 11 attendance snapshots exist")
    relative_multi = (
        isinstance(destination, str) and destination.startswith("-") and destination != "-1"
    )
    if (
        relative_multi
        or target
        not in {
            down_revision,
            "20261028_0023",
            "20261027_0022",
            "20261022_0021",
        }
    ) and op.get_bind().scalar(
        sa.text("SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 10")
    ):
        raise RuntimeError("Cannot downgrade below 0021 while version 10 snapshots exist")
    count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM reimbursement_drafts "
            "WHERE recovery_source_draft_id IS NOT NULL "
            "OR recovery_pending != 0 OR workspace_retired != 0"
        )
    )
    if count:
        raise CommandError("Cannot discard reimbursement recovery relationships or progress")
    op.drop_index("uq_reimbursement_drafts_recovery_source", table_name="reimbursement_drafts")
    op.drop_column("reimbursement_drafts", "workspace_retired")
    op.drop_column("reimbursement_drafts", "recovery_pending")
    op.drop_column("reimbursement_drafts", "recovery_source_draft_id")
