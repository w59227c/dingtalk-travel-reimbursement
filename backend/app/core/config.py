from __future__ import annotations

import ipaddress
import os
import platform
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./data/app.db"
    temp_dir: Path = Path("/tmp/expense")
    reimbursement_staging_dir: Path = Path("/app/staging")
    reimbursement_staging_max_bytes: int = 4 * 1024 * 1024 * 1024
    reimbursement_draft_ttl_days: int = 30
    excel_template_path: Path = Path("app/templates/expense_template.xlsx")
    ocr_mode: Literal["local"] = "local"

    upload_ttl_minutes: int = 30
    temp_cleanup_interval_seconds: int = 600
    upload_max_file_bytes: int = 20 * 1024 * 1024
    upload_max_request_bytes: int = 25 * 1024 * 1024
    upload_spool_memory_bytes: int = 256 * 1024
    session_max_files: int = 200
    session_max_bytes: int = 100 * 1024 * 1024
    expense_max_items: int = 200
    temp_storage_max_bytes: int = 768 * 1024 * 1024
    temp_storage_reserve_bytes: int = 200 * 1024 * 1024
    image_max_pixels: int = 50_000_000
    image_max_dimension: int = 20_000
    pdf_render_dpi: int = 200
    pdf_max_xobjects: int = 100
    pdf_max_content_streams: int = 512
    pdf_max_embedded_pixels: int = 100_000_000
    pdf_max_resource_depth: int = 8
    pdf_max_decoded_page_bytes: int = 32 * 1024 * 1024
    pdf_preflight_timeout_seconds: int = 30
    image_validation_timeout_seconds: int = 30
    file_operation_wait_seconds: float = 5.0
    upload_admission_wait_seconds: float = 1.0
    process_job_admission_wait_seconds: float = 1.0
    logout_file_wait_seconds: float = 130.0
    file_worker_memory_limit_bytes: int = 512 * 1024 * 1024
    ocr_worker_memory_limit_bytes: int = 5 * 1024 * 1024 * 1024
    worker_file_size_limit_bytes: int = 64 * 1024 * 1024
    worker_open_files_limit: int = 128
    worker_cpu_seconds: int = 180

    # OCR is a required product capability. Deployments may explicitly disable
    # it for diagnosis, but every normal environment starts with local OCR on.
    ocr_enabled: bool = True
    ocr_fake_enabled: bool = False
    ocr_concurrency: int = 1
    ocr_queue_max_waiters: int = 8
    ocr_queue_wait_seconds: int = 150
    ocr_timeout_seconds: int = 120
    ocr_cpu_threads: int = 4
    ocr_engine: Literal["paddle_static"] = "paddle_static"
    ocr_detection_model_dir: Path | None = None
    ocr_recognition_model_dir: Path | None = None

    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    dingtalk_corp_id: str = ""
    dingtalk_agent_id: int | None = None
    dingtalk_workflow_instance_read_min_interval_seconds: float = 0.2
    dingtalk_storage_upload_timeout_seconds: float = 120.0
    dingtalk_storage_upload_host_suffixes: str = "trans.dingtalk.com"
    dingtalk_oa_worker_enabled: bool = False
    dingtalk_oa_worker_poll_interval_seconds: float = 1.0
    dingtalk_oa_worker_lease_seconds: int = 180
    dingtalk_oa_worker_retry_base_seconds: int = 5
    dingtalk_oa_worker_retry_max_seconds: int = 300
    dingtalk_oa_worker_reconciliation_seconds: int = 900
    dingtalk_approval_detail_url_template: str = ""
    session_secret: str = ""
    admin_user_ids: str = ""
    session_cookie_name: str = "expense_session"
    session_ttl_minutes: int = 480
    session_cleanup_interval_seconds: int = 300
    session_cookie_secure: bool = False

    auth_mock_enabled: bool = False
    auth_mock_user_id: str = "mock-user"
    auth_mock_user_name: str = "开发测试用户"
    auth_mock_departments: str = "100:测试部门"

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be a standard Python log level")
        return normalized

    @field_validator("database_url")
    @classmethod
    def require_sqlite(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("V1 DATABASE_URL must use SQLite")
        return value

    @field_validator("reimbursement_staging_dir")
    @classmethod
    def normalize_reimbursement_staging_dir(cls, value: Path) -> Path:
        normalized = Path(os.path.normpath(str(value)))
        if not normalized.is_absolute() or normalized == Path(normalized.anchor):
            raise ValueError("REIMBURSEMENT_STAGING_DIR must be an absolute non-root path")
        return normalized

    @field_validator("dingtalk_agent_id", mode="before")
    @classmethod
    def validate_dingtalk_agent_id(cls, value: object) -> int | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, bool):
            raise ValueError("DINGTALK_AGENT_ID must be a positive numeric AgentId")
        if isinstance(value, int):
            agent_id = value
        elif isinstance(value, str):
            normalized = value.strip()
            if not normalized.isascii() or not normalized.isdecimal():
                raise ValueError("DINGTALK_AGENT_ID must be a positive numeric AgentId")
            agent_id = int(normalized)
        else:
            raise ValueError("DINGTALK_AGENT_ID must be a positive numeric AgentId")
        if agent_id <= 0:
            raise ValueError("DINGTALK_AGENT_ID must be a positive numeric AgentId")
        return agent_id

    @field_validator("dingtalk_storage_upload_timeout_seconds")
    @classmethod
    def validate_dingtalk_storage_upload_timeout(cls, value: float) -> float:
        if not 5 <= value <= 900:
            raise ValueError("DINGTALK_STORAGE_UPLOAD_TIMEOUT_SECONDS must be between 5 and 900")
        return value

    @field_validator("dingtalk_workflow_instance_read_min_interval_seconds")
    @classmethod
    def validate_dingtalk_workflow_instance_read_min_interval(cls, value: float) -> float:
        if not 0.01 <= value <= 5:
            raise ValueError(
                "DINGTALK_WORKFLOW_INSTANCE_READ_MIN_INTERVAL_SECONDS must be between 0.01 and 5"
            )
        return value

    @field_validator("dingtalk_storage_upload_host_suffixes")
    @classmethod
    def validate_dingtalk_storage_upload_hosts(cls, value: str) -> str:
        if len(value) > 4096:
            raise ValueError(
                "DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES must contain comma-separated hosts"
            )
        host_pattern = re.compile(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        )
        suffixes: list[str] = []
        for raw_suffix in value.split(","):
            suffix = raw_suffix.strip().lower().lstrip(".")
            if (
                not suffix
                or len(suffix) > 253
                or not suffix.isascii()
                or not host_pattern.fullmatch(suffix)
            ):
                raise ValueError(
                    "DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES must contain comma-separated hosts"
                )
            try:
                ipaddress.ip_address(suffix)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES must contain comma-separated hosts"
                )
            if suffix not in suffixes:
                suffixes.append(suffix)
        if not suffixes or len(suffixes) > 20:
            raise ValueError(
                "DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES must contain between 1 and 20 hosts"
            )
        return ",".join(suffixes)

    @field_validator("dingtalk_approval_detail_url_template")
    @classmethod
    def validate_dingtalk_approval_detail_url_template(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        if len(normalized) > 2048 or normalized.count("{processInstanceId}") != 1:
            raise ValueError(
                "DINGTALK_APPROVAL_DETAIL_URL_TEMPLATE must contain exactly one "
                "{processInstanceId} placeholder and be at most 2048 characters"
            )
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"https", "dingtalk"} or not parsed.netloc:
            raise ValueError(
                "DINGTALK_APPROVAL_DETAIL_URL_TEMPLATE must be an absolute HTTPS or dingtalk URL"
            )
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("DINGTALK_APPROVAL_DETAIL_URL_TEMPLATE must not contain credentials")
        return normalized

    @field_validator("dingtalk_oa_worker_poll_interval_seconds")
    @classmethod
    def validate_dingtalk_oa_worker_poll_interval(cls, value: float) -> float:
        if not 0.1 <= value <= 60:
            raise ValueError("DINGTALK_OA_WORKER_POLL_INTERVAL_SECONDS must be between 0.1 and 60")
        return value

    @field_validator(
        "dingtalk_oa_worker_lease_seconds",
        "dingtalk_oa_worker_retry_base_seconds",
        "dingtalk_oa_worker_retry_max_seconds",
        "dingtalk_oa_worker_reconciliation_seconds",
    )
    @classmethod
    def validate_dingtalk_oa_worker_seconds(cls, value: int) -> int:
        if not 1 <= value <= 86_400:
            raise ValueError("DINGTALK_OA_WORKER timing values must be between 1 and 86400")
        return value

    @field_validator("session_ttl_minutes")
    @classmethod
    def validate_session_ttl(cls, value: int) -> int:
        if not 5 <= value <= 1440:
            raise ValueError("SESSION_TTL_MINUTES must be between 5 and 1440")
        return value

    @field_validator("session_cleanup_interval_seconds")
    @classmethod
    def validate_session_cleanup_interval(cls, value: int) -> int:
        if not 1 <= value <= 3600:
            raise ValueError("SESSION_CLEANUP_INTERVAL_SECONDS must be between 1 and 3600")
        return value

    @field_validator("upload_ttl_minutes")
    @classmethod
    def validate_upload_ttl(cls, value: int) -> int:
        if not 1 <= value <= 1440:
            raise ValueError("UPLOAD_TTL_MINUTES must be between 1 and 1440")
        return value

    @field_validator("temp_cleanup_interval_seconds")
    @classmethod
    def validate_temp_cleanup_interval(cls, value: int) -> int:
        if not 1 <= value <= 3600:
            raise ValueError("TEMP_CLEANUP_INTERVAL_SECONDS must be between 1 and 3600")
        return value

    @field_validator(
        "upload_max_file_bytes",
        "upload_max_request_bytes",
        "upload_spool_memory_bytes",
        "session_max_files",
        "session_max_bytes",
        "expense_max_items",
        "temp_storage_max_bytes",
        "temp_storage_reserve_bytes",
        "reimbursement_staging_max_bytes",
        "image_max_pixels",
        "image_max_dimension",
        "pdf_render_dpi",
        "pdf_max_xobjects",
        "pdf_max_content_streams",
        "pdf_max_embedded_pixels",
        "pdf_max_resource_depth",
        "pdf_max_decoded_page_bytes",
        "pdf_preflight_timeout_seconds",
        "image_validation_timeout_seconds",
        "file_worker_memory_limit_bytes",
        "ocr_worker_memory_limit_bytes",
        "worker_file_size_limit_bytes",
        "worker_open_files_limit",
        "worker_cpu_seconds",
        "ocr_concurrency",
        "ocr_timeout_seconds",
        "ocr_cpu_threads",
    )
    @classmethod
    def require_positive_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("upload and OCR limits must be positive")
        return value

    @field_validator("session_max_files", "expense_max_items")
    @classmethod
    def require_bounded_item_limit(cls, value: int) -> int:
        if value > 1_000:
            raise ValueError("file and expense item limits must not exceed 1000")
        return value

    @field_validator(
        "file_operation_wait_seconds",
        "upload_admission_wait_seconds",
        "process_job_admission_wait_seconds",
    )
    @classmethod
    def require_bounded_wait(cls, value: float) -> float:
        if not 0.01 <= value <= 30:
            raise ValueError("file admission waits must be between 0.01 and 30 seconds")
        return value

    @field_validator("logout_file_wait_seconds")
    @classmethod
    def require_bounded_logout_wait(cls, value: float) -> float:
        if not 1 <= value <= 600:
            raise ValueError("LOGOUT_FILE_WAIT_SECONDS must be between 1 and 600 seconds")
        return value

    @field_validator("ocr_queue_max_waiters")
    @classmethod
    def require_bounded_ocr_queue(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("OCR_QUEUE_MAX_WAITERS must be between 1 and 100")
        return value

    @field_validator("ocr_queue_wait_seconds")
    @classmethod
    def require_bounded_ocr_queue_wait(cls, value: int) -> int:
        if not 1 <= value <= 600:
            raise ValueError("OCR_QUEUE_WAIT_SECONDS must be between 1 and 600 seconds")
        return value

    @model_validator(mode="after")
    def validate_security_configuration(self) -> Settings:
        if self.app_env == "production":
            if self.auth_mock_enabled:
                raise ValueError("AUTH_MOCK_ENABLED must be false in production")
            if self.dingtalk_agent_id is None:
                raise ValueError("DINGTALK_AGENT_ID must be configured for production")
            required = {
                "DINGTALK_CLIENT_ID": self.dingtalk_client_id,
                "DINGTALK_CLIENT_SECRET": self.dingtalk_client_secret,
                "DINGTALK_CORP_ID": self.dingtalk_corp_id,
                "SESSION_SECRET": self.session_secret,
            }
            placeholders = {"change-me", "changeme", "placeholder", "example", "secret"}
            for name, raw_value in required.items():
                value = raw_value.strip()
                if not value or value.lower() in placeholders:
                    raise ValueError(f"{name} must be configured for production")
            if not self.admin_ids:
                raise ValueError("ADMIN_USER_IDS must contain at least one userId in production")
            if len(self.session_secret) < 32:
                raise ValueError("SESSION_SECRET must contain at least 32 characters")
            if self.ocr_fake_enabled:
                raise ValueError("OCR_FAKE_ENABLED must be false in production")
            if self.ocr_enabled:
                if self.ocr_detection_model_dir is None:
                    raise ValueError("OCR_DETECTION_MODEL_DIR is required when OCR is enabled")
                if self.ocr_recognition_model_dir is None:
                    raise ValueError("OCR_RECOGNITION_MODEL_DIR is required when OCR is enabled")
                machine = platform.machine().lower()
                if sys.platform != "linux" or machine not in {"x86_64", "amd64"}:
                    raise ValueError("production OCR requires Linux x86_64/amd64 worker limits")
        if self.upload_max_request_bytes < self.upload_max_file_bytes:
            raise ValueError("UPLOAD_MAX_REQUEST_BYTES must cover one file plus multipart overhead")
        if self.upload_spool_memory_bytes > 8 * 1024 * 1024:
            raise ValueError("UPLOAD_SPOOL_MEMORY_BYTES must not exceed 8 MiB")
        if self.session_max_bytes < self.upload_max_file_bytes:
            raise ValueError("SESSION_MAX_BYTES must cover one permitted file")
        if self.temp_storage_max_bytes < self.session_max_bytes:
            raise ValueError("TEMP_STORAGE_MAX_BYTES must cover one session quota")
        if self.temp_storage_reserve_bytes < self.upload_max_file_bytes * 2:
            raise ValueError("TEMP_STORAGE_RESERVE_BYTES must cover transfer and runtime headroom")
        if (
            self.temp_storage_max_bytes
            + self.upload_max_file_bytes
            + self.temp_storage_reserve_bytes
            >= 1024 * 1024 * 1024
        ):
            raise ValueError(
                "TEMP_STORAGE_MAX_BYTES plus transfer and reserve must stay below the 1 GiB tmpfs"
            )
        minimum_staging_bytes = self.session_max_bytes + self.upload_max_file_bytes
        if self.reimbursement_staging_max_bytes < minimum_staging_bytes:
            raise ValueError(
                "REIMBURSEMENT_STAGING_MAX_BYTES must cover one session plus generated output"
            )
        if self.reimbursement_staging_max_bytes > 1024 * 1024 * 1024 * 1024:
            raise ValueError("REIMBURSEMENT_STAGING_MAX_BYTES must not exceed 1 TiB")
        minimum_oa_lease_seconds = int(self.dingtalk_storage_upload_timeout_seconds) + 30
        if (
            self.dingtalk_oa_worker_enabled
            and self.dingtalk_oa_worker_lease_seconds < minimum_oa_lease_seconds
        ):
            raise ValueError(
                "DINGTALK_OA_WORKER_LEASE_SECONDS must cover the upload timeout plus "
                "checkpoint headroom"
            )
        if self.dingtalk_oa_worker_retry_max_seconds < self.dingtalk_oa_worker_retry_base_seconds:
            raise ValueError("DINGTALK_OA_WORKER_RETRY_MAX_SECONDS must be at least the retry base")
        if self.dingtalk_oa_worker_reconciliation_seconds < 30:
            raise ValueError("DINGTALK_OA_WORKER_RECONCILIATION_SECONDS must be at least 30")
        if not 1 <= self.reimbursement_draft_ttl_days <= 365:
            raise ValueError("REIMBURSEMENT_DRAFT_TTL_DAYS must be between 1 and 365")
        if self.app_env == "production":
            if not self.temp_dir.is_absolute():
                raise ValueError("TEMP_DIR must be absolute in production")
            staging_path = self.reimbursement_staging_dir.resolve(strict=False)
            temp_path = self.temp_dir.resolve(strict=False)
            if (
                staging_path == temp_path
                or staging_path in temp_path.parents
                or temp_path in staging_path.parents
            ):
                raise ValueError(
                    "REIMBURSEMENT_STAGING_DIR must be separate from temporary storage"
                )
        if self.ocr_concurrency != 1:
            raise ValueError("V1 OCR_CONCURRENCY must be exactly 1")
        if self.file_worker_memory_limit_bytes < 256 * 1024 * 1024:
            raise ValueError("FILE_WORKER_MEMORY_LIMIT_BYTES must be at least 256 MiB")
        if self.ocr_worker_memory_limit_bytes < self.file_worker_memory_limit_bytes:
            raise ValueError("OCR_WORKER_MEMORY_LIMIT_BYTES must cover the file worker limit")
        if self.worker_file_size_limit_bytes < self.upload_max_file_bytes:
            raise ValueError("WORKER_FILE_SIZE_LIMIT_BYTES must cover one uploaded file")
        if not 32 <= self.worker_open_files_limit <= 4096:
            raise ValueError("WORKER_OPEN_FILES_LIMIT must be between 32 and 4096")
        if self.worker_cpu_seconds < self.ocr_timeout_seconds:
            raise ValueError("WORKER_CPU_SECONDS must cover the OCR timeout")
        if self.pdf_max_decoded_page_bytes > self.file_worker_memory_limit_bytes // 4:
            raise ValueError("PDF_MAX_DECODED_PAGE_BYTES must fit safely inside the worker limit")
        if self.logout_file_wait_seconds < self.ocr_timeout_seconds + 5:
            raise ValueError("LOGOUT_FILE_WAIT_SECONDS must cover OCR timeout plus cleanup grace")
        if self.upload_ttl_minutes * 60 <= self.ocr_timeout_seconds + 60:
            raise ValueError("UPLOAD_TTL_MINUTES must exceed OCR timeout by more than 60 seconds")
        return self

    @property
    def pdf_limits(self) -> dict[str, int]:
        return {
            "render_dpi": self.pdf_render_dpi,
            "max_render_pixels": self.image_max_pixels,
            "max_dimension": self.image_max_dimension,
            "max_xobjects": self.pdf_max_xobjects,
            "max_content_streams": self.pdf_max_content_streams,
            "max_embedded_pixels": self.pdf_max_embedded_pixels,
            "max_resource_depth": self.pdf_max_resource_depth,
            "max_decoded_page_bytes": self.pdf_max_decoded_page_bytes,
        }

    def _worker_limits(self, memory_bytes: int) -> dict[str, int]:
        return {
            "memory_bytes": memory_bytes,
            "file_size_bytes": self.worker_file_size_limit_bytes,
            "open_files": self.worker_open_files_limit,
            "cpu_seconds": self.worker_cpu_seconds,
        }

    @property
    def file_worker_limits(self) -> dict[str, int]:
        return self._worker_limits(self.file_worker_memory_limit_bytes)

    @property
    def ocr_worker_limits(self) -> dict[str, int]:
        return self._worker_limits(self.ocr_worker_memory_limit_bytes)

    @property
    def ocr_operation_timeout_seconds(self) -> int:
        """Maximum queue plus worker time before a RUNNING marker can be stale."""

        return self.ocr_queue_wait_seconds + self.ocr_timeout_seconds

    @property
    def admin_ids(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.admin_user_ids.split(",") if item.strip())

    @property
    def mock_departments(self) -> tuple[tuple[str, str], ...]:
        departments: list[tuple[str, str]] = []
        for item in self.auth_mock_departments.split(","):
            department_id, separator, name = item.strip().partition(":")
            if separator and department_id and name.strip():
                departments.append((department_id, name.strip()))
        return tuple(departments)

    @property
    def storage_upload_host_suffixes(self) -> tuple[str, ...]:
        return tuple(self.dingtalk_storage_upload_host_suffixes.split(","))

    @property
    def reimbursement_staging_minimum_free_bytes(self) -> int:
        return self.session_max_bytes + self.upload_max_file_bytes

    def dingtalk_approval_url(self, process_instance_id: str) -> str:
        """Return a configured detail link or the known native approval entry point."""

        normalized_id = process_instance_id.strip()
        if not normalized_id or len(normalized_id) > 128:
            raise ValueError("process_instance_id must be non-empty and at most 128 characters")
        if self.dingtalk_approval_detail_url_template:
            return self.dingtalk_approval_detail_url_template.replace(
                "{processInstanceId}",
                quote(normalized_id, safe=""),
            )
        query = urlencode(
            {
                "app_id": "-4",
                "container_type": "work_platform",
                "corpid": self.dingtalk_corp_id,
                "ddtab": "true",
            }
        )
        return f"dingtalk://dingtalkclient/action/openapp?{query}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
