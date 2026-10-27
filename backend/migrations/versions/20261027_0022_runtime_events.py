"""Bounded, sanitized operational records.

Revision ID: 20261027_0022
Revises: 20261022_0021
"""

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory
from alembic.util import CommandError

revision = "20261027_0022"
down_revision = "20261022_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("module", sa.String(16), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.CheckConstraint("byte_size > 0 AND byte_size <= 8192", name="ck_runtime_event_size"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_runtime_events_occurred_at", "runtime_events", ["occurred_at", "id"])
    op.create_index("ix_runtime_events_request_id", "runtime_events", ["request_id", "id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM runtime_events")):
        raise RuntimeError(
            "Cannot downgrade while operational evidence exists; restore a consistent backup"
        )
    # SQLite commits migration steps independently. Reject a downgrade that
    # 0021 would refuse before dropping this revision's table or changing head.
    context = op.get_context()
    destination = context.opts.get("destination_rev")
    target = down_revision if destination == "-1" else None
    if target is None and context.config is not None and isinstance(destination, str):
        try:
            resolved = ScriptDirectory.from_config(context.config).get_revision(destination)
            target = resolved.revision if resolved else None
        except CommandError:
            pass
    if target != down_revision and op.get_bind().scalar(
        sa.text("SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 10")
    ):
        raise RuntimeError(
            "Cannot downgrade below 0021 while version 10 submission snapshots exist; "
            "use the explicit previous revision for a safe one-step downgrade"
        )
    op.drop_table("runtime_events")
