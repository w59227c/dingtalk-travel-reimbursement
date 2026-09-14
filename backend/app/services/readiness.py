from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine, inspect, text

from app.core.config import Settings
from app.ocr.model_artifacts import model_directory_ready
from app.services.excel_generator import load_validated_template
from app.services.reimbursement_staging import StagingArea


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    checks: dict[str, str]


_EXPECTED_ALEMBIC_REVISION = "20260914_0018"
_REQUIRED_COLUMNS = {
    "oa_template_profiles": {
        "profile_key",
        "process_code",
        "template_name",
        "schema_fingerprint",
        "confirmed_schema_fingerprint",
        "schema_json",
        "mapping_json",
        "config_version",
        "allowed_travel_process_codes_json",
        "travel_profiles_json",
        "compatibility_status",
        "confirmed_by_user_id",
        "last_checked_at",
        "confirmed_at",
        "created_at",
        "updated_at",
    },
    "settings": {"key", "value"},
    "receipt_keyword_mappings": {
        "id",
        "keyword",
        "normalized_keyword",
        "category_id",
    },
    "sessions": {
        "session_id_hash",
        "dingtalk_user_id",
        "dingtalk_union_id",
        "name",
        "corp_id",
        "departments_json",
        "current_department_id",
        "current_department_name",
        "csrf_token_hash",
        "is_admin",
        "created_at",
        "expires_at",
        "last_seen_at",
    },
    "reimbursement_drafts": {
        "id",
        "corp_id",
        "owner_user_id",
        "status",
        "revision",
        "template_process_code",
        "template_config_version",
        "schema_fingerprint",
        "expires_at",
        "locked_at",
    },
    "reimbursement_draft_files": {
        "id",
        "draft_id",
        "processing_role",
        "attachment_kind",
        "file_status",
        "storage_key",
        "part_storage_key",
        "reserved_bytes",
        "reservation_expires_at",
        "size_bytes",
        "sha256",
        "ocr_status",
    },
    "reimbursement_draft_related_approvals": {
        "source_travel_type_value",
        "id",
        "draft_id",
        "corp_id",
        "owner_user_id",
        "sort_order",
        "process_instance_id",
        "travel_profile_key",
        "process_code",
        "catalog_config_version",
        "travel_schema_fingerprint",
        "listed_from_ms",
        "listed_to_ms",
        "travel_start_date",
        "travel_end_date",
        "title",
        "business_id",
        "instance_created_at",
        "verified_at",
        "created_at",
        "updated_at",
    },
    "reimbursement_submissions": {
        "id",
        "draft_id",
        "corp_id",
        "originator_user_id",
        "idempotency_key_hash",
        "status",
        "resume_status",
        "status_version",
        "snapshot_version",
        "oa_create_started_at",
        "oa_request_json",
        "oa_request_hash",
        "process_instance_id",
        "business_id",
        "approval_url",
        "submitted_at",
    },
    "reimbursement_submission_recovery_audits": {
        "id",
        "submission_id",
        "corp_id",
        "admin_user_id",
        "action",
        "verification_note",
        "process_instance_id",
        "status_before",
        "status_after",
        "status_version_before",
        "created_at",
    },
    "reimbursement_uploads": {
        "id",
        "submission_id",
        "draft_id",
        "source_draft_file_id",
        "role",
        "local_storage_key",
        "local_part_storage_key",
        "local_status",
        "upload_status",
        "status_version",
        "space_id",
        "file_id",
        "linked_at",
        "cleaned_at",
        "local_deleted_at",
    },
}


def _database_ready(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            if connection.execute(text("SELECT 1")).scalar_one() != 1:
                return False
            inspector = inspect(connection)
            table_names = set(inspector.get_table_names())
            if not {*_REQUIRED_COLUMNS, "alembic_version"}.issubset(table_names):
                return False
            for table_name, expected_columns in _REQUIRED_COLUMNS.items():
                actual_columns = {
                    str(column["name"]) for column in inspector.get_columns(table_name)
                }
                if not expected_columns.issubset(actual_columns):
                    return False
            revisions = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            )
            return revisions == [_EXPECTED_ALEMBIC_REVISION]
    except Exception:
        return False


@lru_cache(maxsize=8)
def _validated_template_fingerprint(
    path_text: str,
    device: int,
    inode: int,
    size: int,
    modified_ns: int,
    changed_ns: int,
) -> bool:
    # The stat fields are deliberate cache-key material. Validation remains
    # strict, while the 10-second readiness probe does not repeatedly parse an
    # unchanged workbook. The bounded cache prevents unbounded path growth.
    del device, inode, size, modified_ns, changed_ns
    try:
        workbook, _ = load_validated_template(Path(path_text))
    except Exception:
        return False
    workbook.close()
    return True


def _template_ready(template_path: Path) -> bool:
    try:
        template_stat = template_path.stat()
    except OSError:
        return False
    return _validated_template_fingerprint(
        str(template_path),
        template_stat.st_dev,
        template_stat.st_ino,
        template_stat.st_size,
        template_stat.st_mtime_ns,
        template_stat.st_ctime_ns,
    )


def _temp_storage_ready(temp_dir: Path) -> bool:
    try:
        directory_stat = temp_dir.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        return False
    return os.access(temp_dir, os.W_OK | os.X_OK)


def _open_directory_no_follow(directory: Path) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if no_follow == 0 or not directory.is_absolute():
        raise OSError("safe directory traversal is unavailable")
    descriptor = os.open(directory.anchor, os.O_RDONLY | directory_flag | no_follow)
    try:
        for component in directory.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | directory_flag | no_follow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _reimbursement_staging_ready(settings: Settings) -> bool:
    root_descriptor: int | None = None
    probe_descriptor: int | None = None
    probe_name = f".readiness-{uuid4().hex}.probe"
    probe_created = False
    try:
        root_descriptor = _open_directory_no_follow(settings.reimbursement_staging_dir)
        root_stat = os.fstat(root_descriptor)
        effective_user_id = getattr(os, "geteuid", lambda: root_stat.st_uid)()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != effective_user_id
            or stat.S_IMODE(root_stat.st_mode) != 0o700
        ):
            return False

        area_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for area in StagingArea:
            area_descriptor = os.open(area.value, area_flags, dir_fd=root_descriptor)
            try:
                area_stat = os.fstat(area_descriptor)
                if (
                    not stat.S_ISDIR(area_stat.st_mode)
                    or area_stat.st_uid != effective_user_id
                    or stat.S_IMODE(area_stat.st_mode) != 0o700
                ):
                    return False
            finally:
                os.close(area_descriptor)

        probe_descriptor = os.open(
            probe_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        probe_created = True
        if os.write(probe_descriptor, b"ready") != 5:
            return False
        os.fsync(probe_descriptor)
        os.close(probe_descriptor)
        probe_descriptor = None
        os.unlink(probe_name, dir_fd=root_descriptor)
        probe_created = False
        os.fsync(root_descriptor)

        filesystem = os.fstatvfs(root_descriptor)
        available_bytes = filesystem.f_bavail * filesystem.f_frsize
        minimum_bytes = settings.reimbursement_staging_minimum_free_bytes
        return (
            settings.reimbursement_staging_max_bytes >= minimum_bytes
            and available_bytes >= minimum_bytes
        )
    except OSError:
        return False
    finally:
        if probe_descriptor is not None:
            try:
                os.close(probe_descriptor)
            except OSError:
                pass
        if probe_created and root_descriptor is not None:
            try:
                os.unlink(probe_name, dir_fd=root_descriptor)
                os.fsync(root_descriptor)
            except OSError:
                pass
        if root_descriptor is not None:
            try:
                os.close(root_descriptor)
            except OSError:
                pass


def _ocr_readiness(settings: Settings) -> tuple[bool, str]:
    # Local OCR is enabled by default and participates in readiness. An explicit
    # disable remains available only for diagnosis and focused tests.
    if not settings.ocr_enabled:
        return True, "disabled"
    if settings.ocr_fake_enabled:
        return settings.app_env != "production", "development_fake"
    if not model_directory_ready(settings.ocr_detection_model_dir, "detection"):
        return False, "not_ready"
    if not model_directory_ready(settings.ocr_recognition_model_dir, "recognition"):
        return False, "not_ready"
    try:
        expected_versions = {
            "opencv-contrib-python": "4.10.0.84",
            "paddleocr": "3.7.0",
            "paddlepaddle": "3.3.1",
            "pypdfium2": "5.13.0",
        }
        versions_match = all(
            metadata.version(package) == expected for package, expected in expected_versions.items()
        )
    except metadata.PackageNotFoundError:
        return False, "not_ready"
    return versions_match, "configured" if versions_match else "not_ready"


def _dingtalk_configuration_readiness(settings: Settings) -> tuple[bool, str]:
    if settings.app_env in {"development", "test"} and settings.auth_mock_enabled:
        return True, "development_mock"
    configured = bool(
        settings.dingtalk_client_id.strip()
        and settings.dingtalk_client_secret.strip()
        and settings.dingtalk_corp_id.strip()
        and settings.dingtalk_agent_id is not None
    )
    return configured, "configured" if configured else "not_ready"


def check_readiness(settings: Settings, engine: Engine) -> ReadinessReport:
    """Run bounded, non-recognition checks without returning paths or exception details."""

    database_ok = _database_ready(engine)
    template_ok = _template_ready(settings.excel_template_path)
    temp_ok = _temp_storage_ready(settings.temp_dir)
    staging_ok = _reimbursement_staging_ready(settings)
    ocr_ok, ocr_status = _ocr_readiness(settings)
    dingtalk_ok, dingtalk_status = _dingtalk_configuration_readiness(settings)
    checks = {
        "database": "ok" if database_ok else "not_ready",
        "excelTemplate": "ok" if template_ok else "not_ready",
        "tempStorage": "ok" if temp_ok else "not_ready",
        "reimbursementStaging": "ok" if staging_ok else "not_ready",
        "ocr": ocr_status,
        "dingtalkConfiguration": dingtalk_status,
    }
    return ReadinessReport(
        ready=database_ok and template_ok and temp_ok and staging_ok and ocr_ok and dingtalk_ok,
        checks=checks,
    )
