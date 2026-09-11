"""Add administrator-managed subsidy rates for each trip type.

Revision ID: 20260902_0003
Revises: 20260901_0002
Create Date: 2026-09-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260902_0003"
down_revision: str | None = "20260901_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES "
        "('subsidy_business_per_day', '100.00'), "
        "('subsidy_short_term_project_per_day', '100.00'), "
        "('subsidy_long_term_project_per_day', '150.00'), "
        "('subsidy_same_city_project_per_day', '50.00'), "
        "('subsidy_internal_per_day', '100.00')"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'subsidy_business_per_day', "
        "'subsidy_short_term_project_per_day', "
        "'subsidy_long_term_project_per_day', "
        "'subsidy_same_city_project_per_day', "
        "'subsidy_internal_per_day'"
        ")"
    )
