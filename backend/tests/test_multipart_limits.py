from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from starlette.datastructures import Headers

from app.core.errors import ApiError
from app.services.file_coordination import SessionFileCoordinator
from app.services.multipart_uploads import parse_upload_files, prepare_spool_directory
from app.services.temp_files import (
    cleanup_expired_temp_files,
    new_upload_budget,
    retained_usage,
)


async def multipart_chunks(
    boundary: str,
    parts: list[tuple[str, str | None, int]],
    *,
    chunk_size: int = 64 * 1024,
) -> AsyncGenerator[bytes, None]:
    for field_name, filename, size in parts:
        yield f"--{boundary}\r\n".encode()
        if filename is None:
            yield f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode()
        else:
            yield (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
        remaining = size
        while remaining:
            amount = min(chunk_size, remaining)
            yield b"x" * amount
            remaining -= amount
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode()


async def parse_parts(settings, parts: list[tuple[str, str | None, int]]):
    boundary = "expense-boundary"
    budget = new_upload_budget(settings, "a" * 64, 0)
    return await parse_upload_files(
        Headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        multipart_chunks(boundary, parts),
        settings,
        budget,
    )


def assert_no_spool_files(settings) -> None:
    spool = prepare_spool_directory(settings)
    assert not any(path.is_file() for path in spool.iterdir())


@pytest.mark.asyncio
async def test_body_read_cancellation_closes_spool_and_releases_upload_admission(
    settings_factory,
) -> None:
    settings = settings_factory(upload_spool_memory_bytes=32)
    boundary = "cancel-boundary"
    body_consumed = asyncio.Event()

    async def stalled_body() -> AsyncGenerator[bytes, None]:
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files[]"; filename="receipt.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + b"x" * 256
        body_consumed.set()
        await asyncio.Event().wait()

    coordinator = SessionFileCoordinator()
    namespace = "c" * 64

    async def parse_while_admitted() -> None:
        async with coordinator.async_upload_lease(namespace):
            await parse_upload_files(
                Headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
                stalled_body(),
                settings,
                new_upload_budget(settings, namespace, 0),
            )

    task = asyncio.create_task(parse_while_admitted())
    await body_consumed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_no_spool_files(settings)
    assert coordinator.active_session_states == 0
    async with coordinator.async_upload_lease(namespace):
        pass


@pytest.mark.asyncio
async def test_chunked_file_over_20_mib_is_rejected_during_parse(settings_factory) -> None:
    settings = settings_factory(upload_spool_memory_bytes=1024)
    with pytest.raises(ApiError) as error:
        await parse_parts(
            settings,
            [("files[]", "large.png", 20 * 1024 * 1024 + 1)],
        )
    assert error.value.code == "FILE_TOO_LARGE"
    assert error.value.status_code == 413
    assert_no_spool_files(settings)


@pytest.mark.asyncio
async def test_second_file_part_is_rejected_and_spools_removed(
    settings_factory,
) -> None:
    settings = settings_factory(upload_spool_memory_bytes=1024)
    with pytest.raises(ApiError) as error:
        await parse_parts(
            settings,
            [("files[]", "first.png", 1), ("files[]", "second.png", 1)],
        )
    assert error.value.code == "TOO_MANY_FILES"
    assert error.value.status_code == 413
    assert_no_spool_files(settings)


@pytest.mark.asyncio
async def test_file_part_and_field_count_limits_are_stable(settings_factory) -> None:
    settings = settings_factory(upload_spool_memory_bytes=1)
    with pytest.raises(ApiError) as file_error:
        await parse_parts(
            settings,
            [("files[]", f"{index}.png", 1) for index in range(31)],
        )
    assert file_error.value.code == "TOO_MANY_FILES"
    assert file_error.value.status_code == 413
    assert_no_spool_files(settings)

    with pytest.raises(ApiError) as field_error:
        await parse_parts(settings, [(f"field-{index}", None, 1) for index in range(50)])
    assert field_error.value.code == "TOO_MANY_FIELDS"
    assert field_error.value.status_code == 413
    assert_no_spool_files(settings)


def test_orphan_spool_is_counted_and_cleaned(
    settings_factory,
) -> None:
    settings = settings_factory(
        upload_max_file_bytes=100,
        upload_max_request_bytes=1_000,
        session_max_bytes=1_000,
        temp_storage_max_bytes=1_000,
    )
    spool = prepare_spool_directory(settings)
    orphan = spool / "upload-orphan.spool"
    orphan.write_bytes(b"x" * 700)
    _, global_bytes = retained_usage(settings, "a" * 64)
    assert global_bytes == 700
    old = datetime.now(UTC) - timedelta(minutes=settings.upload_ttl_minutes + 1)
    os.utime(orphan, (old.timestamp(), old.timestamp()))
    assert cleanup_expired_temp_files(settings) == 1
    assert not orphan.exists()
