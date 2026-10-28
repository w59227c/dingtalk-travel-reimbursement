"""Support immutable attendance-date submission snapshots.

Revision ID: 20261028_0024
Revises: 20261028_0023
"""

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory
from alembic.util import CommandError

revision = "20261028_0024"
down_revision = "20261028_0023"
branch_labels = None
depends_on = None

# Frozen SQL: migrations must not depend on future model changes.
_TRIGGERS = tuple(
    f"""
    CREATE TRIGGER trg_reimbursement_submissions_bundle_manifest_{operation.lower()}
    BEFORE {operation} ON reimbursement_submissions
    WHEN NEW.snapshot_version >= 2 AND NEW.status = 'SUBMITTED'
         AND COALESCE(
             CASE
             WHEN NEW.snapshot_version < 10 THEN
                 (SELECT count(*) FROM reimbursement_uploads WHERE submission_id = NEW.id) = 2
                 AND EXISTS (
                     SELECT 1 FROM reimbursement_uploads
                     WHERE submission_id = NEW.id AND role = 'GENERATED_PDF'
                       AND sort_order = 0 AND upload_status = 'LINKED'
                 )
                 AND EXISTS (
                     SELECT 1 FROM reimbursement_uploads
                     WHERE submission_id = NEW.id AND role = 'GENERATED_EXCEL'
                       AND sort_order = 1 AND upload_status = 'LINKED'
                 )
             WHEN NEW.snapshot_version IN (10, 11) AND json_valid(NEW.form_snapshot_json) THEN
                 json_extract(NEW.form_snapshot_json, '$.snapshotVersion') = NEW.snapshot_version
                 AND CASE json_extract(NEW.form_snapshot_json, '$.attachmentMode')
                 WHEN 'excel_only' THEN
                     json_type(NEW.form_snapshot_json, '$.originalFiles') = 'array'
                     AND json_array_length(NEW.form_snapshot_json, '$.originalFiles') = 0
                     AND json_type(NEW.form_snapshot_json, '$.input.items') = 'array'
                     AND json_array_length(NEW.form_snapshot_json, '$.input.items') = 0
                     AND CAST(json_extract(
                         NEW.form_snapshot_json, '$.totals.subsidyTotal'
                     ) AS REAL) > 0
                     AND (SELECT count(*) FROM reimbursement_uploads
                          WHERE submission_id = NEW.id) = 1
                     AND EXISTS (
                         SELECT 1 FROM reimbursement_uploads
                         WHERE submission_id = NEW.id AND role = 'GENERATED_EXCEL'
                           AND sort_order = 0 AND upload_status = 'LINKED'
                     )
                 WHEN 'bundle_and_excel' THEN
                     json_type(NEW.form_snapshot_json, '$.originalFiles') = 'array'
                     AND json_array_length(NEW.form_snapshot_json, '$.originalFiles') > 0
                     AND (SELECT count(*) FROM reimbursement_uploads
                          WHERE submission_id = NEW.id) = 2
                     AND EXISTS (
                         SELECT 1 FROM reimbursement_uploads
                         WHERE submission_id = NEW.id AND role = 'GENERATED_PDF'
                           AND sort_order = 0 AND upload_status = 'LINKED'
                     )
                     AND EXISTS (
                         SELECT 1 FROM reimbursement_uploads
                         WHERE submission_id = NEW.id AND role = 'GENERATED_EXCEL'
                           AND sort_order = 1 AND upload_status = 'LINKED'
                     )
                 ELSE 0 END
             ELSE 0 END, 0
         ) != 1
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement uploads must match its snapshot');
    END
    """
    for operation in ("INSERT", "UPDATE")
)


def _replace_triggers(*, attendance: bool) -> None:
    for operation in ("insert", "update"):
        op.execute(f"DROP TRIGGER trg_reimbursement_submissions_bundle_manifest_{operation}")
    for ddl in _TRIGGERS:
        op.execute(ddl if attendance else ddl.replace("IN (10, 11)", "= 10"))


def upgrade() -> None:
    _replace_triggers(attendance=True)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 11")
    ):
        raise RuntimeError("Cannot downgrade while version 11 attendance snapshots exist")
    context = op.get_context()
    destination = context.opts.get("destination_rev")
    target = down_revision if destination == "-1" else None
    if target is None and context.config is not None and isinstance(destination, str):
        try:
            resolved = ScriptDirectory.from_config(context.config).get_revision(destination)
            target = resolved.revision if resolved else None
        except CommandError:
            pass
    if target not in {down_revision, "20261027_0022", "20261022_0021"} and op.get_bind().scalar(
        sa.text("SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 10")
    ):
        raise RuntimeError("Cannot downgrade below 0021 while version 10 snapshots exist")
    _replace_triggers(attendance=False)
