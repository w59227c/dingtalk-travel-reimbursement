"""Allow safe cleanup suspension and preserve every administrator recovery decision.

Revision ID: 20261109_0026
Revises: 20261108_0025
"""

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory
from alembic.util import CommandError

revision = "20261109_0026"
down_revision = "20261108_0025"
branch_labels = None
depends_on = None

_NEW_GUARD = """
CREATE TRIGGER trg_reimbursement_submissions_cleanup_guard_update
    BEFORE UPDATE ON reimbursement_submissions
    WHEN EXISTS (
        SELECT 1 FROM reimbursement_uploads AS upload
        WHERE upload.submission_id = OLD.id
          AND upload.upload_status IN ('CLEANUP_PENDING', 'CLEANED')
    ) AND (
        NEW.id IS NOT OLD.id
        OR NEW.process_instance_id IS NOT NULL
        OR NEW.orphan_confirmed_at IS NULL
        OR NEW.orphan_confirmation_code IS NULL
        OR NOT (
            NEW.status IN ('ORPHAN_CLEANUP', 'MANUAL_REVIEW')
            OR (NEW.status = 'FAILED_RETRYABLE' AND NEW.resume_status IS 'ORPHAN_CLEANUP')
            OR (NEW.status = 'FAILED_FINAL' AND NOT EXISTS (
                SELECT 1 FROM reimbursement_uploads AS unfinished
                WHERE unfinished.submission_id = OLD.id
                  AND unfinished.upload_status NOT IN (
                      'PENDING', 'PUTTING', 'PUT_DONE', 'CLEANED', 'DISCARDED'
                  )
            ))
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'OA instance and remote cleanup cannot coexist');
    END
"""

_OLD_GUARD = """
CREATE TRIGGER trg_reimbursement_submissions_cleanup_guard_update
    BEFORE UPDATE ON reimbursement_submissions
    WHEN EXISTS (
             SELECT 1
             FROM reimbursement_uploads AS upload
             WHERE upload.submission_id = OLD.id
               AND upload.upload_status IN ('CLEANUP_PENDING', 'CLEANED')
         )
         AND (
             NEW.id IS NOT OLD.id
             OR NEW.process_instance_id IS NOT NULL
             OR (
                 NEW.status IS NOT 'ORPHAN_CLEANUP'
                 AND (
                     NEW.status IS NOT 'FAILED_FINAL'
                     OR EXISTS (
                         SELECT 1
                         FROM reimbursement_uploads AS unfinished
                         WHERE unfinished.submission_id = OLD.id
                           AND unfinished.upload_status NOT IN (
                               'PENDING', 'PUTTING', 'PUT_DONE', 'CLEANED', 'DISCARDED'
                           )
                     )
                 )
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'OA instance and remote cleanup cannot coexist');
    END
"""


def _replace_guard(sql):
    op.execute("DROP TRIGGER IF EXISTS trg_reimbursement_submissions_cleanup_guard_update")
    op.execute(sql)


def upgrade():
    with op.batch_alter_table("reimbursement_submission_recovery_audits") as batch:
        batch.drop_constraint(
            "uq_reimbursement_submission_recovery_audits_submission", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_reimbursement_submission_recovery_audits_decision",
            ["submission_id", "status_version_before"],
        )
    _replace_guard(_NEW_GUARD)


def _count(sql):
    return op.get_bind().scalar(sa.text(sql))


def _preflight_downgrade():
    # pysqlite may commit DDL before a later migration refuses a downgrade.
    # Preserve the current schema and all evidence before touching either table or trigger.
    context = op.get_context()
    destination = context.opts.get("destination_rev")
    scripts = ScriptDirectory.from_config(context.config)
    relative = isinstance(destination, str) and destination.startswith("-")
    try:
        if relative:
            target = scripts.get_revision(context.get_current_revision())
            for _ in range(int(destination[1:])):
                target = scripts.get_revision(target.down_revision) if target else None
        else:
            target = scripts.get_revision(destination)
        rank = int(target.revision.rsplit("_", 1)[1]) if target else 0
    except (CommandError, TypeError, ValueError):
        raise CommandError(
            "Cannot safely resolve downgrade target; use an explicit revision"
        ) from None
    relative_multi = relative and destination != "-1"
    if (rank < 24 or relative_multi) and _count(
        "SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 11"
    ):
        raise RuntimeError("Cannot downgrade while version 11 attendance snapshots exist")
    if (rank < 21 or relative_multi) and _count(
        "SELECT count(*) FROM reimbursement_submissions WHERE snapshot_version >= 10"
    ):
        raise RuntimeError("Cannot downgrade below 0021 while version 10 snapshots exist")
    if rank < 25 and _count(
        "SELECT count(*) FROM reimbursement_drafts WHERE recovery_source_draft_id IS NOT NULL "
        "OR recovery_pending != 0 OR workspace_retired != 0"
    ):
        raise CommandError("Cannot discard reimbursement recovery relationships or progress")
    if rank < 18 and _count("SELECT count(*) FROM reimbursement_submission_recovery_audits"):
        raise RuntimeError("Cannot discard administrator recovery audit evidence")
    if _count(
        "SELECT count(*) FROM (SELECT submission_id FROM reimbursement_submission_recovery_audits "
        "GROUP BY submission_id HAVING count(*) > 1)"
    ):
        raise CommandError("Cannot discard repeated administrator recovery decisions")
    if _count(
        "SELECT count(*) FROM reimbursement_submissions AS task WHERE "
        "task.status NOT IN ('ORPHAN_CLEANUP', 'FAILED_FINAL') AND EXISTS "
        "(SELECT 1 FROM reimbursement_uploads AS upload WHERE upload.submission_id = task.id "
        "AND upload.upload_status IN ('CLEANUP_PENDING', 'CLEANED'))"
    ):
        raise CommandError("Cannot downgrade while remote cleanup is suspended")


def downgrade():
    _preflight_downgrade()
    with op.batch_alter_table("reimbursement_submission_recovery_audits") as batch:
        batch.drop_constraint(
            "uq_reimbursement_submission_recovery_audits_decision", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_reimbursement_submission_recovery_audits_submission",
            ["submission_id"],
        )
    _replace_guard(_OLD_GUARD)
