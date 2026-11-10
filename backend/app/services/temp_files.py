from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from starlette.datastructures import UploadFile

from app.core.config import Settings
from app.core.errors import ApiError
from app.ocr.workers import inspect_pdf_worker, validate_image_worker
from app.services.file_coordination import SessionFileCoordinator
from app.services.process_jobs import (
    KillableProcessRunner,
    ProcessJobBusy,
    ProcessJobResourceLimit,
    ProcessJobTimeout,
)

_CHUNK_SIZE = 1024 * 1024
_FILE_EXTENSIONS = ("jpg", "png", "pdf")
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF-"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


@dataclass(frozen=True, slots=True)
class StoredFile:
    temp_id: str
    path: Path
    extension: str
    media_type: str
    size: int
    original_name: str


@dataclass(frozen=True, slots=True)
class ValidatedUploadType:
    original_name: str
    extension: str
    media_type: str


@dataclass(slots=True)
class UploadBudget:
    session_files: int
    session_bytes: int
    global_bytes: int


@dataclass(frozen=True, slots=True)
class RetainedUsage:
    files: int
    bytes: int


def close_upload_file(upload: UploadFile) -> None:
    """Close an upload spool synchronously so cancellation cannot interrupt cleanup."""

    upload.file.close()


def _session_namespace(session_id_hash: str) -> str:
    invalid_character = any(char not in "0123456789abcdef" for char in session_id_hash)
    if len(session_id_hash) != 64 or invalid_character:
        raise ApiError("TEMP_STORAGE_INVALID", "临时文件空间无效", 500)
    return session_id_hash


def session_directory(settings: Settings, session_id_hash: str) -> Path:
    return settings.temp_dir / _session_namespace(session_id_hash)


def spool_directory(settings: Settings) -> Path:
    return settings.temp_dir / ".spool"


def _safe_original_name(raw_name: str | None) -> str:
    if not raw_name:
        return "票据"
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or "票据"


def _extension_from_name(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix not in {"jpg", "jpeg", "png", "pdf"}:
        raise ApiError("UNSUPPORTED_FILE", "仅支持 JPG、JPEG、PNG 或单页 PDF", 400)
    return "jpg" if suffix == "jpeg" else suffix


def _extension_from_magic(first_bytes: bytes) -> str:
    if first_bytes.startswith(_JPEG_MAGIC):
        return "jpg"
    if first_bytes.startswith(_PNG_MAGIC):
        return "png"
    if first_bytes.startswith(_PDF_MAGIC):
        return "pdf"
    raise ApiError("UNSUPPORTED_FILE", "文件内容不是受支持的图片或 PDF", 400)


def validate_upload_type(raw_name: str | None, first_bytes: bytes) -> ValidatedUploadType:
    """Validate a receipt name against its content signature.

    Both temporary uploads and durable reimbursement uploads use this boundary
    so they cannot drift into accepting different file types.
    """

    original_name = _safe_original_name(raw_name)
    declared_extension = _extension_from_name(original_name)
    actual_extension = _extension_from_magic(first_bytes)
    if actual_extension != declared_extension:
        raise ApiError("FILE_TYPE_MISMATCH", "文件扩展名与内容不一致", 400)
    return ValidatedUploadType(
        original_name=original_name,
        extension=actual_extension,
        media_type={
            "jpg": "image/jpeg",
            "png": "image/png",
            "pdf": "application/pdf",
        }[actual_extension],
    )


def validate_upload_name(raw_name: str | None, *, expected_extension: str) -> str:
    """Normalize a display name and require it to retain the trusted file type."""

    original_name = _safe_original_name(raw_name)
    if _extension_from_name(original_name) != expected_extension:
        raise ApiError("FILE_TYPE_MISMATCH", "文件名必须保留原文件类型", 400)
    return original_name


def _ensure_private_directory(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise ApiError("TEMP_STORAGE_INVALID", "临时文件空间无效", 500) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise ApiError("TEMP_STORAGE_INVALID", "临时文件空间无效", 500)
    try:
        return os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise ApiError("TEMP_STORAGE_INVALID", "临时文件空间无效", 500) from exc


def _worker_error(result: object) -> ApiError | None:
    if isinstance(result, dict) and result.get("ok"):
        return None
    if isinstance(result, dict):
        return ApiError(
            str(result.get("code", "INVALID_PDF")),
            str(result.get("message", "PDF 文件损坏或格式无效")),
            int(result.get("status", 400)),
        )
    return ApiError("INVALID_PDF", "PDF 文件损坏或格式无效", 400)


async def validate_new_file(
    path: Path,
    extension: str,
    settings: Settings,
    process_runner: KillableProcessRunner,
    *,
    supporting_pdf: bool = False,
) -> int:
    """Perform expensive image/PDF validation away from the event loop."""

    try:
        if extension == "pdf":
            result = await process_runner.run(
                inspect_pdf_worker,
                str(path),
                settings.pdf_limits,
                settings.file_worker_limits,
                *((30,) if supporting_pdf else ()),
                timeout_seconds=settings.pdf_preflight_timeout_seconds,
            )
        else:
            result = await process_runner.run(
                validate_image_worker,
                str(path),
                extension,
                {
                    "max_pixels": settings.image_max_pixels,
                    "max_dimension": settings.image_max_dimension,
                },
                settings.file_worker_limits,
                timeout_seconds=settings.image_validation_timeout_seconds,
            )
    except ProcessJobBusy as exc:
        raise ApiError("UPLOAD_BUSY", "本地文件检查繁忙，请稍后重试", 429) from exc
    except ProcessJobTimeout as exc:
        code = "PDF_VALIDATION_TIMEOUT" if extension == "pdf" else "IMAGE_VALIDATION_TIMEOUT"
        raise ApiError(code, "票据文件检查超时，请重试", 504) from exc
    except ProcessJobResourceLimit as exc:
        raise ApiError("PROCESS_RESOURCE_LIMIT", "票据文件处理超过资源限制", 422) from exc
    error = _worker_error(result)
    if error is not None:
        if error.code in {"WORKER_LIMIT_SETUP_FAILED", "WORKER_RESOURCE_LIMIT"}:
            raise ApiError("PROCESS_RESOURCE_LIMIT", "票据文件处理资源不可用", 422)
        raise error
    return int(result.get("pageCount", 1))


def _directory_usage(directory: Path) -> RetainedUsage:
    files = 0
    retained_bytes = 0
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return RetainedUsage(files=0, bytes=0)
    for entry in entries:
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(entry_stat.st_mode):
            files += 1
            retained_bytes += entry_stat.st_size
    return RetainedUsage(files=files, bytes=retained_bytes)


def retained_usage(settings: Settings, session_id_hash: str) -> tuple[RetainedUsage, int]:
    """Count final, partial and managed-spool files without following links."""

    root = settings.temp_dir
    session = _directory_usage(session_directory(settings, session_id_hash))
    global_bytes = 0
    try:
        root_entries = list(os.scandir(root))
    except FileNotFoundError:
        return session, 0
    for root_entry in root_entries:
        try:
            root_stat = root_entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(root_stat.st_mode):
            global_bytes += root_stat.st_size
        elif stat.S_ISDIR(root_stat.st_mode):
            global_bytes += _directory_usage(Path(root_entry.path)).bytes
    return session, global_bytes


def new_upload_budget(
    settings: Settings,
    session_id_hash: str,
    incoming_files: int,
) -> UploadBudget:
    session, global_bytes = retained_usage(settings, session_id_hash)
    if session.files + incoming_files > settings.session_max_files:
        raise ApiError(
            "SESSION_FILE_LIMIT",
            f"当前会话最多保留 {settings.session_max_files} 个票据文件",
            413,
        )
    if session.bytes >= settings.session_max_bytes:
        raise ApiError("SESSION_STORAGE_LIMIT", "当前会话临时文件总量已达上限", 413)
    if global_bytes >= settings.temp_storage_max_bytes:
        raise ApiError("TEMP_STORAGE_FULL", "服务器临时空间繁忙，请稍后重试", 503)
    return UploadBudget(
        session_files=session.files,
        session_bytes=session.bytes,
        global_bytes=global_bytes,
    )


def charge_upload_bytes(
    file_bytes: int,
    chunk_bytes: int,
    budget: UploadBudget,
    settings: Settings,
) -> None:
    next_file_bytes = file_bytes + chunk_bytes
    budget.session_bytes += chunk_bytes
    budget.global_bytes += chunk_bytes
    if next_file_bytes > settings.upload_max_file_bytes:
        raise ApiError("FILE_TOO_LARGE", "单个文件超过大小限制", 413)
    if budget.session_bytes > settings.session_max_bytes:
        raise ApiError("SESSION_STORAGE_LIMIT", "当前会话临时文件总量超过限制", 413)
    if budget.global_bytes > settings.temp_storage_max_bytes:
        raise ApiError("TEMP_STORAGE_FULL", "服务器临时空间不足，请稍后重试", 503)


async def store_upload(
    upload: UploadFile,
    settings: Settings,
    session_id_hash: str,
    budget: UploadBudget | None,
    process_runner: KillableProcessRunner,
) -> StoredFile:
    original_name = _safe_original_name(upload.filename)
    directory = session_directory(settings, session_id_hash)
    directory_fd = _ensure_private_directory(directory)
    temp_id = str(uuid4())
    partial_name = f".{temp_id}.part"
    final_name: str | None = None
    file_bytes = 0
    first_bytes = bytearray()
    try:
        descriptor = os.open(
            partial_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as stream:
            while chunk := await upload.read(_CHUNK_SIZE):
                if budget is not None:
                    charge_upload_bytes(file_bytes, len(chunk), budget, settings)
                file_bytes += len(chunk)
                if len(first_bytes) < 16:
                    first_bytes.extend(chunk[: 16 - len(first_bytes)])
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if file_bytes == 0:
            raise ApiError("EMPTY_FILE", "不能上传空文件", 400)
        upload_type = validate_upload_type(original_name, bytes(first_bytes))
        actual_extension = upload_type.extension
        final_name = f"{temp_id}.{actual_extension}"
        os.rename(
            partial_name,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        final = directory / final_name
        await validate_new_file(final, actual_extension, settings, process_runner)
        return StoredFile(
            temp_id=temp_id,
            path=final,
            extension=actual_extension,
            media_type=upload_type.media_type,
            size=file_bytes,
            original_name=original_name,
        )
    except BaseException:
        for name in (partial_name, final_name):
            if name is None:
                continue
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(directory_fd)
        close_upload_file(upload)


def _normalized_temp_id(temp_id: str) -> str:
    try:
        normalized_id = str(UUID(temp_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError("TEMP_FILE_NOT_FOUND", "临时票据不存在或已过期", 404) from exc
    if normalized_id != temp_id.lower():
        raise ApiError("TEMP_FILE_NOT_FOUND", "临时票据不存在或已过期", 404)
    return normalized_id


def delete_session_file(settings: Settings, session_id_hash: str, temp_id: str) -> None:
    normalized_id = _normalized_temp_id(temp_id)
    directory = session_directory(settings, session_id_hash)
    try:
        directory_fd = os.open(directory, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise ApiError("TEMP_FILE_NOT_FOUND", "临时票据不存在或已过期", 404) from exc
    removed = False
    try:
        for extension in _FILE_EXTENSIONS:
            name = f"{normalized_id}.{extension}"
            try:
                file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            os.unlink(name, dir_fd=directory_fd)
            removed = True
        if not removed:
            raise ApiError("TEMP_FILE_NOT_FOUND", "临时票据不存在或已过期", 404)
    finally:
        os.close(directory_fd)
    try:
        if not any(directory.iterdir()):
            directory.rmdir()
    except FileNotFoundError:
        pass


def delete_session_files(settings: Settings, session_id_hash: str) -> None:
    directory = session_directory(settings, session_id_hash)
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(directory_stat.st_mode):
        directory.unlink(missing_ok=True)
    elif stat.S_ISDIR(directory_stat.st_mode):
        shutil.rmtree(directory)


def cleanup_expired_temp_files(
    settings: Settings,
    coordinator: SessionFileCoordinator | None = None,
) -> int:
    root = settings.temp_dir
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise RuntimeError("TEMP_DIR must not be a symbolic link")
    coordinator = coordinator or SessionFileCoordinator()
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.upload_ttl_minutes)
    removed = 0
    for entry in list(root.iterdir()):
        try:
            entry_stat = entry.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                entry.unlink(missing_ok=True)
                removed += 1
                continue
            if not stat.S_ISDIR(entry_stat.st_mode):
                if datetime.fromtimestamp(entry_stat.st_mtime, UTC) <= cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
                continue
            if entry.name == ".spool":
                with coordinator.try_upload_cleanup_lease() as acquired:
                    if not acquired:
                        continue
                    for child in list(entry.iterdir()):
                        try:
                            child_stat = child.lstat()
                        except FileNotFoundError:
                            continue
                        if stat.S_ISLNK(child_stat.st_mode) or (
                            stat.S_ISREG(child_stat.st_mode)
                            and datetime.fromtimestamp(child_stat.st_mtime, UTC) <= cutoff
                        ):
                            child.unlink(missing_ok=True)
                            removed += 1
                continue
            if len(entry.name) != 64 or any(char not in "0123456789abcdef" for char in entry.name):
                continue
            with coordinator.try_cleanup_lease(entry.name) as acquired:
                if not acquired:
                    continue
                for child in list(entry.iterdir()):
                    try:
                        child_stat = child.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(child_stat.st_mode):
                        child.unlink(missing_ok=True)
                        removed += 1
                    elif (
                        stat.S_ISREG(child_stat.st_mode)
                        and datetime.fromtimestamp(child_stat.st_mtime, UTC) <= cutoff
                    ):
                        child.unlink(missing_ok=True)
                        removed += 1
                try:
                    if not any(entry.iterdir()):
                        entry.rmdir()
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            continue
    return removed
