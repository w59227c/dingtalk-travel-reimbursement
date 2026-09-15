"""Record the historical department of each linked travel approval."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0019"
down_revision: str | None = "20260914_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reimbursement_draft_related_approvals",
        sa.Column("originator_department_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT count(*) FROM reimbursement_draft_related_approvals "
            "WHERE originator_department_id IS NOT NULL"
        )
    ):
        raise RuntimeError(
            "Cannot discard verified travel-approval department evidence; "
            "restore a prior backup"
        )
    op.drop_column(
        "reimbursement_draft_related_approvals",
        "originator_department_id",
    )
