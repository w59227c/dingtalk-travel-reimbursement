"""Add Phase 2 auth fields, project uniqueness and default settings.

Revision ID: 20260901_0002
Revises: 20260901_0001
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0002"
down_revision: str | None = "20260901_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column("corp_id", sa.String(length=128), server_default="", nullable=False)
        )
        batch_op.add_column(
            sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    op.execute(
        "CREATE UNIQUE INDEX uq_projects_project_code_nocase "
        "ON projects(lower(project_code)) WHERE project_code IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_projects_project_name_nocase ON projects(lower(project_name))"
    )
    op.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('calculation_mode', 'half_day_12')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'calculation_mode'")
    op.drop_index("uq_projects_project_name_nocase", table_name="projects")
    op.drop_index("uq_projects_project_code_nocase", table_name="projects")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("is_admin")
        batch_op.drop_column("corp_id")
