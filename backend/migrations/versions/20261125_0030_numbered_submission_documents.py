"""Allow frozen numbered documents while retaining exact attachment manifests."""

from collections.abc import Sequence

from alembic import op
from alembic.script import ScriptDirectory

revision: str = "20261125_0030"
down_revision: str = "20261119_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VERSIONS = "IN (10, 11, 12)"
_NEW_VERSIONS = "IN (10, 11, 12, 13)"
_OLD_EXCEL_ONLY = "NEW.snapshot_version < 12"
_NEW_EXCEL_ONLY = (
    "(NEW.snapshot_version < 12 OR (NEW.snapshot_version = 13 "
    "AND COALESCE(json_extract(NEW.form_snapshot_json, '$.kind'), 'travel') = 'travel'))"
)


def _replace_manifest_guards(*, upgrading: bool) -> None:
    connection = op.get_bind()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    for operation in ("insert", "update"):
        name = f"trg_reimbursement_submissions_bundle_manifest_{operation}"
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).first()
        before = (_OLD_VERSIONS, _OLD_EXCEL_ONLY) if upgrading else (_NEW_VERSIONS, _NEW_EXCEL_ONLY)
        after = (_NEW_VERSIONS, _NEW_EXCEL_ONLY) if upgrading else (_OLD_VERSIONS, _OLD_EXCEL_ONLY)
        if row is None or any(token not in row[0] for token in before):
            raise RuntimeError(
                "Submission manifest guard differs from the expected migration schema"
            )
        sql = row[0].replace(before[0], after[0]).replace(before[1], after[1])
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        connection.exec_driver_sql(sql)


def upgrade() -> None:
    _replace_manifest_guards(upgrading=True)


def downgrade() -> None:
    connection = op.get_bind()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.exec_driver_sql(
        "SELECT 1 FROM reimbursement_submissions WHERE snapshot_version >= 13 LIMIT 1"
    ).first():
        raise RuntimeError(
            "Cannot downgrade numbered submission snapshots; preserve their audit history"
        )
    context = op.get_context()
    if context.opts.get("destination_rev") not in {down_revision, "-1"}:
        previous = ScriptDirectory.from_config(context.config).get_revision(down_revision)
        previous.module._preflight_downgrade()
    _replace_manifest_guards(upgrading=False)
