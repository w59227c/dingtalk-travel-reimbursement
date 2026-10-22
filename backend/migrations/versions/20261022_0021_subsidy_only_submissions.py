"""Allow frozen Excel-only subsidy submissions without weakening historical manifests.

Revision ID: 20261022_0021
Revises: 20261020_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261022_0021"
down_revision: str | None = "20261020_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
             WHEN NEW.snapshot_version = 10 AND json_valid(NEW.form_snapshot_json) THEN
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


def _historical_trigger(operation: str) -> str:
    return f"""
    CREATE TRIGGER trg_reimbursement_submissions_bundle_manifest_{operation.lower()}
    BEFORE {operation} ON reimbursement_submissions
    WHEN NEW.snapshot_version >= 2 AND NEW.status = 'SUBMITTED'
         AND (
             (SELECT count(*) FROM reimbursement_uploads WHERE submission_id = NEW.id) != 2
             OR NOT EXISTS (
                 SELECT 1 FROM reimbursement_uploads
                 WHERE submission_id = NEW.id AND role = 'GENERATED_PDF'
                   AND sort_order = 0 AND upload_status = 'LINKED'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM reimbursement_uploads
                 WHERE submission_id = NEW.id AND role = 'GENERATED_EXCEL'
                   AND sort_order = 1 AND upload_status = 'LINKED'
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement requires PDF and Excel');
    END
    """


def _drop_triggers() -> None:
    for operation in ("insert", "update"):
        op.execute(f"DROP TRIGGER trg_reimbursement_submissions_bundle_manifest_{operation}")


def upgrade() -> None:
    _drop_triggers()
    for ddl in _TRIGGERS:
        op.execute(ddl)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 10")
    ):
        raise RuntimeError(
            "Cannot downgrade while version 10 submission snapshots exist; "
            "restore a consistent pre-upgrade backup instead of discarding audit evidence"
        )
    _drop_triggers()
    for operation in ("INSERT", "UPDATE"):
        op.execute(_historical_trigger(operation))
