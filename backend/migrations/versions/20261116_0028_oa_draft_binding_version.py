"""Preserve existing draft bindings when confirming additive OA mappings.

Revision ID: 20261116_0028
Revises: 20261116_0027
"""

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory
from alembic.util import CommandError

revision = "20261116_0028"
down_revision = "20261116_0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("oa_template_profiles", sa.Column("draft_binding_version", sa.Integer()))


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM oa_template_profiles WHERE draft_binding_version IS NOT NULL"
    )):
        raise CommandError("Cannot downgrade a catalog with a preserved OA draft binding")
    context = op.get_context()
    if context.opts.get("destination_rev") not in {down_revision, "-1"}:
        previous = ScriptDirectory.from_config(context.config).get_revision(down_revision)
        previous.module._preflight_downgrade()
    op.drop_column("oa_template_profiles", "draft_binding_version")
