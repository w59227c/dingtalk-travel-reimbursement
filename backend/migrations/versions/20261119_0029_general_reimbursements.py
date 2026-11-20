"""Keep separate travel and general reimbursement workspaces."""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory

revision: str = "20261119_0029"
down_revision: str = "20261116_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.driver_connection.in_transaction
    ):
        # sqlite3's legacy transaction mode otherwise commits leading ALTER
        # statements before the first UPDATE. Keep schema and revision atomic.
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _add_or_verify_column(table_name: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = next(
        (item for item in inspector.get_columns(table_name) if item["name"] == column.name),
        None,
    )
    if existing is None:
        op.add_column(table_name, column)
        return
    expected_default = "'travel'" if column.name == "kind" else None
    valid = (
        isinstance(existing["type"], sa.String)
        and existing["type"].length == 16
        and existing["nullable"] == column.nullable
        and existing["default"] == expected_default
    )
    if column.name == "kind":
        valid = valid and any(
            check["name"] == "ck_reimbursement_drafts_kind"
            and re.sub(r"\s+", "", check["sqltext"]) == "kindIN('travel','general')"
            for check in inspector.get_check_constraints(table_name)
        )
    if not valid:
        raise RuntimeError(
            f"Cannot resume 0029: {table_name}.{column.name} differs from the migration schema"
        )


def _update_file_kinds(*, upgrade: bool) -> None:
    connection = op.get_bind()
    triggers = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
        "AND name LIKE 'trg_reimbursement_%'"
    ).all()
    for name, _ in triggers:
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    table = sa.Table("reimbursement_draft_files", sa.MetaData(), autoload_with=connection)
    name = "ck_reimbursement_draft_files_attachment_kind"
    for constraint in list(table.constraints):
        if constraint.name == name:
            table.constraints.remove(constraint)
    kinds = "'itinerary', 'payment_proof', 'other', 'hotel_bill'"
    if upgrade:
        kinds += ", 'waybill'"
    table.append_column(
        sa.Column(
            "attachment_kind",
            sa.String(32),
            sa.CheckConstraint(f"attachment_kind IN ({kinds})", name=name),
            nullable=False,
            server_default="other",
        ),
        replace_existing=True,
    )
    with op.batch_alter_table("reimbursement_draft_files", copy_from=table, recreate="always"):
        pass
    for name, sql in triggers:
        if name.startswith("trg_reimbursement_submissions_bundle_manifest_"):
            if upgrade:
                sql = sql.replace("IN (10, 11)", "IN (10, 11, 12)").replace(
                    "WHEN 'excel_only' THEN", "WHEN 'excel_only' THEN NEW.snapshot_version < 12 AND"
                )
            else:
                sql = sql.replace("IN (10, 11, 12)", "IN (10, 11)").replace(
                    "WHEN 'excel_only' THEN NEW.snapshot_version < 12 AND", "WHEN 'excel_only' THEN"
                )
        connection.exec_driver_sql(sql)


def upgrade() -> None:
    _begin_sqlite_transaction()
    _add_or_verify_column(
        "reimbursement_drafts",
        sa.Column(
            "kind",
            sa.String(16),
            sa.CheckConstraint(
                "kind IN ('travel', 'general')", name="ck_reimbursement_drafts_kind"
            ),
            nullable=False,
            server_default="travel",
        ),
    )
    _add_or_verify_column("receipt_keyword_mappings", sa.Column("welfare_type", sa.String(16)))
    op.execute(
        "UPDATE receipt_keyword_mappings SET welfare_type = "
        "CASE WHEN normalized_keyword = '收派服务' THEN 'express' ELSE 'other' END "
        "WHERE category_id = 'employee_welfare' AND welfare_type IS NULL"
    )
    _update_file_kinds(upgrade=True)


def downgrade() -> None:
    _begin_sqlite_transaction()
    connection = op.get_bind()
    if any(
        connection.exec_driver_sql(query).scalar()
        for query in (
            "SELECT 1 FROM reimbursement_drafts WHERE kind = 'general' LIMIT 1",
            "SELECT 1 FROM reimbursement_submissions WHERE snapshot_version >= 12 LIMIT 1",
            "SELECT 1 FROM reimbursement_draft_files WHERE attachment_kind = 'waybill' LIMIT 1",
            "SELECT 1 FROM oa_template_profiles "
            "WHERE profile_key = 'general_reimbursement' LIMIT 1",
            "SELECT 1 FROM reimbursement_drafts WHERE EXISTS ("
            "SELECT 1 FROM json_each(input_json, '$.items') "
            "WHERE json_extract(value, '$.welfareType') IS NOT NULL) LIMIT 1",
            "SELECT 1 FROM reimbursement_submissions WHERE EXISTS ("
            "SELECT 1 FROM json_each(form_snapshot_json, '$.input.items') "
            "WHERE json_extract(value, '$.welfareType') IS NOT NULL) LIMIT 1",
            "SELECT 1 FROM receipt_keyword_mappings WHERE welfare_type IS NOT NULL AND "
            "(category_id != 'employee_welfare' OR welfare_type != "
            "CASE WHEN normalized_keyword = '收派服务' THEN 'express' ELSE 'other' END) LIMIT 1",
        )
    ):
        raise RuntimeError("Cannot discard reimbursement data; restore a prior backup")
    context = op.get_context()
    if context.opts.get("destination_rev") not in {down_revision, "-1"}:
        previous = ScriptDirectory.from_config(context.config).get_revision(down_revision)
        previous.module._preflight_downgrade()
    _update_file_kinds(upgrade=False)
    op.drop_column("receipt_keyword_mappings", "welfare_type")
    op.drop_column("reimbursement_drafts", "kind")
