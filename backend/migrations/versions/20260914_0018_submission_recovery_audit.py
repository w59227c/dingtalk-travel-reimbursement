"""Record immutable administrator submission-recovery decisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_0018"
down_revision: str | None = "20260909_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reimbursement_submission_recovery_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("submission_id", sa.String(length=36), nullable=False),
        sa.Column("corp_id", sa.String(length=128), nullable=False),
        sa.Column("admin_user_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("verification_note", sa.String(length=500), nullable=False),
        sa.Column("process_instance_id", sa.String(length=128), nullable=True),
        sa.Column("status_before", sa.String(length=32), nullable=False),
        sa.Column("status_after", sa.String(length=32), nullable=False),
        sa.Column("status_version_before", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "action IN ('ATTACH_INSTANCE', 'CONFIRM_NOT_CREATED')",
            name="ck_reimbursement_submission_recovery_audits_action",
        ),
        sa.CheckConstraint(
            "status_before = 'MANUAL_REVIEW' AND "
            "status_after IN ('VERIFYING', 'ORPHAN_CLEANUP')",
            name="ck_reimbursement_submission_recovery_audits_statuses",
        ),
        sa.CheckConstraint(
            "(action = 'ATTACH_INSTANCE' AND process_instance_id IS NOT NULL) OR "
            "(action = 'CONFIRM_NOT_CREATED' AND process_instance_id IS NULL)",
            name="ck_reimbursement_submission_recovery_audits_instance",
        ),
        sa.CheckConstraint(
            "length(verification_note) BETWEEN 1 AND 500",
            name="ck_reimbursement_submission_recovery_audits_note_length",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["reimbursement_submissions.id"],
            name="fk_reimbursement_submission_recovery_audits_submission",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id",
            name="uq_reimbursement_submission_recovery_audits_submission",
        ),
    )
    op.create_index(
        "ix_reimbursement_submission_recovery_audits_corp_created",
        "reimbursement_submission_recovery_audits",
        ["corp_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM reimbursement_submission_recovery_audits")
    ):
        raise RuntimeError(
            "Cannot discard administrator recovery audit evidence; restore a prior backup"
        )
    op.drop_index(
        "ix_reimbursement_submission_recovery_audits_corp_created",
        table_name="reimbursement_submission_recovery_audits",
    )
    op.drop_table("reimbursement_submission_recovery_audits")
