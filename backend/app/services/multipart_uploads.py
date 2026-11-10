from __future__ import annotations

import asyncio
import io
import os
import stat
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.core.config import Settings
from app.core.errors import ApiError
from app.services.temp_files import (
    UploadBudget,
    charge_upload_bytes,
    close_upload_file,
    spool_directory,
)


class UploadMultipartError(MultiPartException):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _ManagedSpool:
    """A bounded-memory spool whose rolled file stays visible for quota/cleanup."""

    def __init__(self, directory: Path, memory_limit: int) -> None:
        self._directory = directory
        self._max_size = memory_limit
        self._rolled = False
        self._file: Any = io.BytesIO()
        self._path: Path | None = None
        self._closed = False

    @property
    def name(self) -> str | None:
        return str(self._path) if self._path is not None else None

    @property
    def closed(self) -> bool:
        return self._closed

    def _rollover(self) -> None:
        if self._rolled:
            return
        memory = self._file
        position = memory.tell()
        disk = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix="upload-",
            suffix=".spool",
            dir=self._directory,
            delete=False,
        )
        path = Path(disk.name)
        try:
            disk.write(memory.getvalue())
            disk.seek(position)
        except BaseException:
            disk.close()
            path.unlink(missing_ok=True)
            raise
        memory.close()
        self._file = disk
        self._path = path
        self._rolled = True

    def write(self, data: bytes) -> int:
        if not self._rolled:
            current_size = len(self._file.getbuffer())
            if current_size + len(data) > self._max_size:
                self._rollover()
        return int(self._file.write(data))

    def read(self, size: int = -1) -> bytes:
        return bytes(self._file.read(size))

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return int(self._file.seek(offset, whence))

    def tell(self) -> int:
        return int(self._file.tell())

    def flush(self) -> None:
        self._file.flush()

    def fileno(self) -> int:
        self._rollover()
        return int(self._file.fileno())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        path = self._path
        try:
            self._file.close()
        finally:
            if path is not None:
                path.unlink(missing_ok=True)


def prepare_spool_directory(settings: Settings) -> Path:
    directory = spool_directory(settings)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_stat = directory.lstat()
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise ApiError("TEMP_STORAGE_INVALID", "上传缓存空间无效", 500)
    try:
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise ApiError("TEMP_STORAGE_INVALID", "上传缓存空间无效", 500) from exc
    return directory


class BoundedUploadParser(MultiPartParser):
    """Starlette-compatible streaming parser that charges bytes in callbacks."""

    def __init__(
        self,
        headers: Headers,
        stream: AsyncGenerator[bytes, None],
        *,
        settings: Settings,
        budget: UploadBudget,
    ) -> None:
        super().__init__(
            headers,
            stream,
            max_files=1,
            max_fields=0,
            max_part_size=1,
        )
        self._settings = settings
        self._budget = budget
        self._spool_directory = prepare_spool_directory(settings)
        self._current_file_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_bytes = 0

    def on_headers_finished(self) -> None:
        try:
            super().on_headers_finished()
        except MultiPartException as exc:
            message = str(getattr(exc, "message", exc)).lower()
            if "too many files" in message:
                raise UploadMultipartError(
                    "TOO_MANY_FILES", "每次请求只能上传一个票据文件", 413
                ) from exc
            if "too many fields" in message:
                raise UploadMultipartError(
                    "TOO_MANY_FIELDS", "上传请求不允许普通表单字段", 413
                ) from exc
            raise

        if self._current_part.field_name != "files[]":
            raise UploadMultipartError("MALFORMED_MULTIPART", "上传请求包含不支持的字段", 400)
        upload = self._current_part.file
        if upload is None:
            raise UploadMultipartError("MALFORMED_MULTIPART", "上传请求包含无效文件字段", 400)
        if self._budget.session_files + self._current_files > self._settings.session_max_files:
            raise UploadMultipartError(
                "SESSION_FILE_LIMIT",
                f"当前会话最多保留 {self._settings.session_max_files} 个票据文件",
                413,
            )

        initial_spool = upload.file
        managed_spool = _ManagedSpool(
            self._spool_directory,
            min(
                self._settings.upload_spool_memory_bytes,
                self._settings.upload_max_file_bytes,
            ),
        )
        initial_spool.close()
        self._files_to_close_on_error[-1] = managed_spool  # type: ignore[list-item]
        upload.file = managed_spool  # type: ignore[assignment]

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is None:
            raise UploadMultipartError("TOO_MANY_FIELDS", "上传请求不允许普通表单字段", 413)
        chunk_bytes = end - start
        try:
            charge_upload_bytes(
                self._current_file_bytes,
                chunk_bytes,
                self._budget,
                self._settings,
            )
        except ApiError as exc:
            raise UploadMultipartError(exc.code, exc.message, exc.status_code) from exc
        self._current_file_bytes += chunk_bytes
        super().on_part_data(data, start, end)

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException:
            for spool in self._files_to_close_on_error:
                spool.close()
            raise


async def parse_upload_files(
    headers: Headers,
    stream: AsyncGenerator[bytes, None],
    settings: Settings,
    budget: UploadBudget,
) -> list[UploadFile]:
    parser = BoundedUploadParser(headers, stream, settings=settings, budget=budget)
    try:
        async with asyncio.timeout(settings.upload_receive_timeout_seconds):
            form = await parser.parse()
    except TimeoutError:
        raise ApiError("UPLOAD_RECEIVE_TIMEOUT", "材料上传超时，请检查网络后重试", 408) from None
    except UploadMultipartError as exc:
        raise ApiError(exc.code, exc.message, exc.status_code) from exc
    except MultiPartException as exc:
        raise ApiError("MALFORMED_MULTIPART", "上传请求无效", 400) from exc
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError("MALFORMED_MULTIPART", "上传请求无效", 400) from exc

    files = form.getlist("files[]")
    if not files:
        raise ApiError("NO_FILES", "请选择要上传的票据", 400)
    if any(not isinstance(item, UploadFile) for item in files):
        for item in files:
            if isinstance(item, UploadFile):
                close_upload_file(item)
        raise ApiError("MALFORMED_MULTIPART", "上传请求包含无效文件字段", 400)
    if len(files) != 1:
        for item in files:
            if isinstance(item, UploadFile):
                close_upload_file(item)
        raise ApiError("TOO_MANY_FILES", "每次请求只能上传一个票据文件", 413)
    return list(files)  # type: ignore[arg-type]
