from __future__ import annotations

import asyncio
import io
import multiprocessing
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
)
from starlette.datastructures import UploadFile

from app.core.errors import ApiError
from app.ocr import workers
from app.ocr.pdf_inspection import PdfLimits, inspect_single_page_pdf
from app.ocr.workers import (
    apply_worker_limits,
    extract_pdf_text_worker,
    inspect_pdf_worker,
    recognize_document_worker,
    validate_image_worker,
)
from app.services import process_jobs
from app.services.file_coordination import (
    FileOperationBusy,
    SessionFileCoordinator,
    SessionFilesRetired,
    UploadBusy,
)
from app.services.ocr_service import OcrService
from app.services.process_jobs import (
    KillableProcessRunner,
    ProcessJobBusy,
    ProcessJobResourceLimit,
    ProcessJobTimeout,
)
from app.services.temp_files import (
    StoredFile,
    cleanup_expired_temp_files,
    session_directory,
    store_upload,
    validate_new_file,
)


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, format="PNG")
    return output.getvalue()


def isolated_profile_probe(profile: str, raw_limits: dict[str, int]) -> dict[str, object]:
    """Picklable child entry point used to verify real runner isolation."""

    apply_worker_limits(raw_limits)
    address_space_soft: int | None = None
    if sys.platform == "linux":
        import resource

        address_space_soft = int(resource.getrlimit(resource.RLIMIT_AS)[0])
    return {
        "pid": os.getpid(),
        "profile": profile,
        "requestedMemory": raw_limits["memory_bytes"],
        "addressSpaceSoft": address_space_soft,
    }


def isolated_echo(value: str) -> str:
    return value


def isolated_error() -> None:
    raise RuntimeError("child detail must not escape")


def isolated_eof() -> None:
    os._exit(17)


def isolated_sleep(seconds: float) -> None:
    time.sleep(seconds)


def open_file_descriptor_count() -> int | None:
    for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
        if directory.is_dir():
            return len(list(directory.iterdir()))
    return None


async def assert_no_spawn_resource_growth(
    baseline_children: set[int | None],
    baseline_fds: int | None,
) -> None:
    await asyncio.sleep(0.02)
    current_children = {child.pid for child in multiprocessing.active_children()}
    assert current_children <= baseline_children
    current_fds = open_file_descriptor_count()
    if baseline_fds is not None and current_fds is not None:
        assert current_fds <= baseline_fds + 2


@pytest.mark.asyncio
async def test_process_timeout_keeps_admission_until_cancelled_worker_unwinds() -> None:
    cancelled = anyio.Event()
    allow_discard = anyio.Event()
    received_options: dict[str, object] = {}

    async def fake_run_sync(_function, *_args, **options):
        received_options.update(options)
        try:
            await anyio.sleep_forever()
        except BaseException:
            cancelled.set()
            with anyio.CancelScope(shield=True):
                await allow_discard.wait()
            raise

    runner = KillableProcessRunner(fake_run_sync)
    first = asyncio.create_task(runner.run(str, "receipt", timeout_seconds=0.01))
    await cancelled.wait()
    with pytest.raises(ProcessJobBusy):
        await runner.run(str, "second", timeout_seconds=1)
    allow_discard.set()
    with pytest.raises(ProcessJobTimeout):
        await first
    assert received_options["cancellable"] is True
    assert received_options["limiter"] is not None


@pytest.mark.asyncio
async def test_fresh_process_isolates_ocr_then_file_worker_profiles(settings_factory) -> None:
    settings = settings_factory()
    runner = KillableProcessRunner()
    ocr = await runner.run(
        isolated_profile_probe,
        "ocr",
        settings.ocr_worker_limits,
        timeout_seconds=10,
    )
    file_validation = await runner.run(
        isolated_profile_probe,
        "file",
        settings.file_worker_limits,
        timeout_seconds=10,
    )

    assert ocr["profile"] == "ocr"
    assert file_validation["profile"] == "file"
    assert ocr["pid"] != file_validation["pid"]
    assert ocr["requestedMemory"] == 5 * 1024 * 1024 * 1024
    assert file_validation["requestedMemory"] == 512 * 1024 * 1024
    if sys.platform == "linux":
        assert ocr["addressSpaceSoft"] == ocr["requestedMemory"]
        assert file_validation["addressSpaceSoft"] == file_validation["requestedMemory"]
    await runner.close()


@pytest.mark.asyncio
async def test_close_cancels_active_and_waiting_jobs_without_starting_waiter() -> None:
    active_started = anyio.Event()
    active_cancelled = anyio.Event()
    allow_cleanup = anyio.Event()
    starts: list[str] = []

    async def fake_run_sync(_function, name, **_options):
        starts.append(name)
        active_started.set()
        try:
            await anyio.sleep_forever()
        except BaseException:
            active_cancelled.set()
            with anyio.CancelScope(shield=True):
                await allow_cleanup.wait()
            raise

    runner = KillableProcessRunner(fake_run_sync, admission_timeout_seconds=5)
    active = asyncio.create_task(runner.run(str, "active", timeout_seconds=30))
    await active_started.wait()
    waiting = asyncio.create_task(runner.run(str, "waiting", timeout_seconds=30))
    await asyncio.sleep(0)
    closing = asyncio.create_task(runner.close())
    await active_cancelled.wait()
    assert starts == ["active"]
    assert not closing.done()
    allow_cleanup.set()
    await closing
    for task in (active, waiting):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert runner._admission.borrowed_tokens == 0
    assert runner._run_tasks == set()
    with pytest.raises(ProcessJobBusy):
        await runner.run(str, "after-close", timeout_seconds=1)


@pytest.mark.asyncio
async def test_close_caller_cancellation_is_rethrown_after_job_cleanup() -> None:
    active_started = anyio.Event()
    active_cancelled = anyio.Event()
    allow_cleanup = anyio.Event()

    async def fake_run_sync(_function, *_args, **_options):
        active_started.set()
        try:
            await anyio.sleep_forever()
        except BaseException:
            active_cancelled.set()
            with anyio.CancelScope(shield=True):
                await allow_cleanup.wait()
            raise

    runner = KillableProcessRunner(fake_run_sync)
    active = asyncio.create_task(runner.run(str, "active", timeout_seconds=30))
    await active_started.wait()
    closing = asyncio.create_task(runner.close())
    await active_cancelled.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done(), "close must finish child cleanup before propagating cancellation"
    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(asyncio.CancelledError):
        await active
    assert runner._admission.borrowed_tokens == 0
    assert runner._run_tasks == set()


@pytest.mark.asyncio
async def test_process_cleanup_has_bounded_join_fallback_without_add_reader(
    monkeypatch,
) -> None:
    class FakeProcess:
        exitcode: int | None = None
        terminated = False
        killed = False
        joins: list[float | None] = []
        closes = 0

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def join(self, timeout=None) -> None:
            self.joins.append(timeout)
            if timeout == 2:
                self.exitcode = 0

        def close(self) -> None:
            assert self.exitcode is not None
            self.closes += 1

    async def unsupported_wait(_process) -> None:
        raise process_jobs._FreshProcessFailure("add_reader unavailable")

    monkeypatch.setattr(process_jobs, "_wait_for_process_exit", unsupported_wait)
    process = FakeProcess()
    await process_jobs._shield_process_cleanup(process)  # type: ignore[arg-type]
    assert process.terminated is True
    assert process.killed is False
    assert process.joins == [2, None]
    assert process.closes == 1


@pytest.mark.asyncio
async def test_real_spawn_paths_reap_children_and_descriptors_without_gc() -> None:
    baseline_children = {child.pid for child in multiprocessing.active_children()}
    baseline_fds = open_file_descriptor_count()
    runner = KillableProcessRunner()

    for index in range(3):
        assert await runner.run(
            isolated_echo,
            str(index),
            timeout_seconds=5,
        ) == str(index)
        await assert_no_spawn_resource_growth(baseline_children, baseline_fds)

    for function in (isolated_error, isolated_eof):
        for _ in range(2):
            with pytest.raises(ProcessJobResourceLimit):
                await runner.run(function, timeout_seconds=5)
            await assert_no_spawn_resource_growth(baseline_children, baseline_fds)

    for _ in range(2):
        with pytest.raises(ProcessJobTimeout):
            await runner.run(isolated_sleep, 2.0, timeout_seconds=0.02)
        await assert_no_spawn_resource_growth(baseline_children, baseline_fds)

    for _ in range(2):
        task = asyncio.create_task(runner.run(isolated_sleep, 2.0, timeout_seconds=5))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await assert_no_spawn_resource_growth(baseline_children, baseline_fds)

    await runner.close()
    await assert_no_spawn_resource_growth(baseline_children, baseline_fds)


@pytest.mark.asyncio
async def test_cancelled_image_worker_keeps_admission_and_file_until_termination(
    settings_factory,
) -> None:
    worker_started = anyio.Event()
    worker_cancelled = anyio.Event()
    allow_termination = anyio.Event()
    call_count = 0

    async def fake_run_sync(_function, *_args, **_options):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            return {"ok": True}
        worker_started.set()
        try:
            await anyio.sleep_forever()
        except BaseException:
            worker_cancelled.set()
            with anyio.CancelScope(shield=True):
                await allow_termination.wait()
            raise

    settings = settings_factory()
    runner = KillableProcessRunner(fake_run_sync, admission_timeout_seconds=0.01)
    upload = UploadFile(file=io.BytesIO(png_bytes()), filename="receipt.png")
    task = asyncio.create_task(store_upload(upload, settings, "e" * 64, None, runner))
    await worker_started.wait()
    task.cancel()
    await worker_cancelled.wait()

    directory = session_directory(settings, "e" * 64)
    retained = [path for path in directory.iterdir() if path.is_file()]
    assert len(retained) == 1
    assert retained[0].suffix == ".png"
    assert not any(path.name.endswith(".part") for path in retained)
    with pytest.raises(ProcessJobBusy):
        await runner.run(str, "second", timeout_seconds=1)

    allow_termination.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(path.is_file() for path in directory.iterdir())
    assert upload.file.closed
    assert await runner.run(str, "third", timeout_seconds=1) == {"ok": True}


def test_cleanup_uses_file_age_and_skips_an_active_session(
    settings_factory,
    tmp_path: Path,
) -> None:
    settings = settings_factory(temp_dir=tmp_path / "temp", upload_ttl_minutes=4)
    settings.temp_dir.mkdir()
    namespace = "a" * 64
    directory = settings.temp_dir / namespace
    directory.mkdir()
    old_file = directory / "old.part"
    old_file.write_bytes(b"old")
    fresh_file = directory / "fresh.part"
    fresh_file.write_bytes(b"fresh")
    old = datetime.now(UTC) - timedelta(minutes=10)
    os.utime(old_file, (old.timestamp(), old.timestamp()))
    os.utime(directory, (old.timestamp(), old.timestamp()))
    coordinator = SessionFileCoordinator()

    with coordinator.session_lease(namespace):
        assert cleanup_expired_temp_files(settings, coordinator) == 0
        assert old_file.exists()
    assert cleanup_expired_temp_files(settings, coordinator) == 1
    assert not old_file.exists()
    assert fresh_file.exists()
    assert directory.exists(), "fresh file must survive an old directory mtime"


@pytest.mark.asyncio
async def test_logout_retirement_waits_for_active_ocr_lease() -> None:
    coordinator = SessionFileCoordinator()
    namespace = "b" * 64
    operation_started = anyio.Event()
    release_operation = anyio.Event()
    retirement_completed = anyio.Event()
    rejected_after_retire = anyio.Event()

    async def active_operation() -> None:
        async with coordinator.async_session_lease(namespace):
            operation_started.set()
            await release_operation.wait()

    async def retire() -> None:
        async with coordinator.async_session_lease(namespace, retire=True):
            retirement_completed.set()

    async def queued_after_retire() -> None:
        with pytest.raises(SessionFilesRetired):
            async with coordinator.async_session_lease(namespace):
                pass
        rejected_after_retire.set()

    operation = asyncio.create_task(active_operation())
    await operation_started.wait()
    logout = asyncio.create_task(retire())
    await asyncio.sleep(0.02)
    assert not retirement_completed.is_set()
    queued = asyncio.create_task(queued_after_retire())
    await asyncio.sleep(0.02)
    assert coordinator.active_session_states == 1
    release_operation.set()
    await operation
    await logout
    await queued
    assert rejected_after_retire.is_set()
    assert coordinator.active_session_states == 0


def test_session_state_registry_does_not_grow_for_sequential_namespaces() -> None:
    coordinator = SessionFileCoordinator()
    for index in range(2_000):
        with coordinator.session_lease(f"{index:064x}"):
            assert coordinator.active_session_states == 1
    assert coordinator.active_session_states == 0


def test_session_state_reference_is_released_when_lease_body_raises() -> None:
    coordinator = SessionFileCoordinator()
    with pytest.raises(RuntimeError, match="boom"):
        with coordinator.session_lease("c" * 64):
            raise RuntimeError("boom")
    assert coordinator.active_session_states == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_releases_registry_reference_after_lock_unwinds() -> None:
    coordinator = SessionFileCoordinator()
    namespace = "d" * 64

    async def wait_for_lease() -> None:
        async with coordinator.async_session_lease(namespace):
            pass

    with coordinator.session_lease(namespace):
        waiter = asyncio.create_task(wait_for_lease())
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert coordinator.active_session_states == 1
        assert coordinator._states[namespace].references == 1

    for _ in range(100):
        if coordinator.active_session_states == 0:
            break
        await asyncio.sleep(0.01)
    assert coordinator.active_session_states == 0


@pytest.mark.asyncio
async def test_async_file_and_upload_admission_have_bounded_busy_results() -> None:
    coordinator = SessionFileCoordinator(file_wait_seconds=0.01, upload_wait_seconds=0.01)
    namespace = "f" * 64
    with coordinator.session_lease(namespace):
        with pytest.raises(FileOperationBusy):
            async with coordinator.async_session_lease(namespace):
                pass
        assert coordinator._states[namespace].references == 1
    assert coordinator.active_session_states == 0

    with coordinator.try_upload_cleanup_lease() as acquired:
        assert acquired
        with pytest.raises(UploadBusy):
            async with coordinator.async_upload_lease(namespace):
                pass
    assert coordinator.active_session_states == 0


@pytest.mark.asyncio
async def test_expensive_image_validation_is_delegated_off_event_loop(
    settings_factory,
    tmp_path: Path,
) -> None:
    image = tmp_path / "receipt.png"
    image.write_bytes(png_bytes())
    delegated: list[tuple[object, tuple[object, ...], float]] = []

    class RecordingRunner:
        async def run(self, function, *args, timeout_seconds):
            delegated.append((function, args, timeout_seconds))
            return {"ok": True}

    await validate_new_file(
        image,
        "png",
        settings_factory(),
        process_runner=RecordingRunner(),  # type: ignore[arg-type]
    )
    assert len(delegated) == 1
    function, args, timeout_seconds = delegated[0]
    assert function is validate_image_worker
    assert args[0] == str(image)
    assert timeout_seconds == 30


def test_pdf_page_and_embedded_image_caps(tmp_path: Path) -> None:
    limits = PdfLimits(
        render_dpi=200,
        max_render_pixels=1_000_000,
        max_dimension=2_000,
        max_xobjects=2,
        max_embedded_pixels=10_000,
    )
    huge_page = tmp_path / "huge.pdf"
    with huge_page.open("wb") as stream:
        writer = PdfWriter()
        writer.add_blank_page(width=10_000, height=10_000)
        writer.write(stream)
    with pytest.raises(ApiError, match="PDF 页面渲染尺寸") as page_error:
        inspect_single_page_pdf(huge_page, limits, extract_text=False)
    assert page_error.value.code == "PDF_PAGE_TOO_LARGE"

    image_heavy = tmp_path / "image-heavy.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    image = StreamObject()
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(150),
            NameObject("/Height"): NumberObject(150),
        }
    )
    image_ref = writer._add_object(image)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref})}
    )
    with image_heavy.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(ApiError, match="总像素") as complexity_error:
        inspect_single_page_pdf(image_heavy, limits, extract_text=False)
    assert complexity_error.value.code == "PDF_TOO_COMPLEX"


def test_pdf_decoded_content_stream_cap_rejects_decompression_bomb(tmp_path: Path) -> None:
    bomb = tmp_path / "decoded-content-bomb.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    decoded = DecodedStreamObject()
    decoded.set_data(b"q\n" + b"0 0 m 1 1 l S\n" * 20_000 + b"Q\n")
    page[NameObject("/Contents")] = writer._add_object(decoded.flate_encode())
    with bomb.open("wb") as stream:
        writer.write(stream)

    limits = PdfLimits(
        render_dpi=72,
        max_render_pixels=1_000_000,
        max_dimension=2_000,
        max_xobjects=10,
        max_embedded_pixels=10_000,
        max_decoded_page_bytes=1_024,
    )
    with pytest.raises(ApiError) as error:
        inspect_single_page_pdf(bomb, limits, extract_text=False)
    assert error.value.code == "PDF_TOO_COMPLEX"
    assert "解码内容" in error.value.message


def test_pdf_many_small_content_streams_do_not_consume_xobject_budget(tmp_path: Path) -> None:
    """Regression for legitimate invoice PDFs with 138 tiny page streams."""

    path = tmp_path / "many-small-content-streams.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    contents = ArrayObject()
    for _ in range(138):
        stream = DecodedStreamObject()
        stream.set_data(b"q\nQ\n")
        contents.append(writer._add_object(stream))
    page[NameObject("/Contents")] = contents
    with path.open("wb") as output:
        writer.write(output)

    limits = PdfLimits(
        render_dpi=72,
        max_render_pixels=1_000_000,
        max_dimension=2_000,
        max_xobjects=100,
        max_content_streams=512,
        max_embedded_pixels=10_000,
        max_decoded_page_bytes=1_024,
    )
    inspection = inspect_single_page_pdf(path, limits, extract_text=False)
    assert inspection.embedded_images == 0

    with pytest.raises(ApiError) as error:
        inspect_single_page_pdf(
            path,
            PdfLimits(
                render_dpi=72,
                max_render_pixels=1_000_000,
                max_dimension=2_000,
                max_xobjects=100,
                max_content_streams=100,
                max_embedded_pixels=10_000,
                max_decoded_page_bytes=1_024,
            ),
            extract_text=False,
        )
    assert error.value.code == "PDF_TOO_COMPLEX"
    assert "内容流过多" in error.value.message


def test_pdf_malformed_optional_text_layer_falls_back_to_image_ocr(tmp_path: Path) -> None:
    """A renderable ticket must not be rejected only because its text layer is non-standard."""

    path = tmp_path / "duplicate-font-file-declarations.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)

    font_file = DecodedStreamObject()
    font_file.set_data(b"synthetic-font-data")
    font_file_ref = writer._add_object(font_file)
    descriptor = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontName"): NameObject("/Synthetic"),
            NameObject("/Flags"): NumberObject(0),
            NameObject("/FontBBox"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(1000), NumberObject(1000)]
            ),
            NameObject("/ItalicAngle"): NumberObject(0),
            NameObject("/Ascent"): NumberObject(800),
            NameObject("/Descent"): NumberObject(-200),
            NameObject("/CapHeight"): NumberObject(700),
            NameObject("/FontFile2"): font_file_ref,
            NameObject("/FontFile3"): font_file_ref,
        }
    )
    descriptor_ref = writer._add_object(descriptor)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/TrueType"),
            NameObject("/BaseFont"): NameObject("/Synthetic"),
            NameObject("/FirstChar"): NumberObject(32),
            NameObject("/LastChar"): NumberObject(32),
            NameObject("/Widths"): ArrayObject([NumberObject(500)]),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            NameObject("/FontDescriptor"): descriptor_ref,
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    contents = DecodedStreamObject()
    contents.set_data(b"BT /F1 12 Tf 10 10 Td (x) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(contents)
    with path.open("wb") as output:
        writer.write(output)

    inspection = inspect_single_page_pdf(
        path,
        PdfLimits(
            render_dpi=72,
            max_render_pixels=1_000_000,
            max_dimension=2_000,
            max_xobjects=10,
            max_embedded_pixels=10_000,
        ),
        extract_text=True,
    )

    assert inspection.text == ""
    assert inspection.layout_text == ""


def test_worker_limits_preserve_hard_limits_and_can_raise_soft_limit(monkeypatch) -> None:
    class FakeResource:
        RLIM_INFINITY = -1
        RLIMIT_AS = 1
        RLIMIT_FSIZE = 2
        RLIMIT_NOFILE = 3
        RLIMIT_CORE = 4
        RLIMIT_CPU = 5
        RUSAGE_SELF = 6

        def __init__(self) -> None:
            self.limits = {
                self.RLIMIT_AS: (256, 4_096),
                self.RLIMIT_FSIZE: (128, 2_048),
                self.RLIMIT_NOFILE: (64, 1_024),
                self.RLIMIT_CORE: (1, self.RLIM_INFINITY),
                self.RLIMIT_CPU: (10, 1_000),
            }
            self.original_hard = {key: value[1] for key, value in self.limits.items()}

        def getrlimit(self, resource_id):
            return self.limits[resource_id]

        def setrlimit(self, resource_id, value):
            self.limits[resource_id] = value

        @staticmethod
        def getrusage(_usage):
            return SimpleNamespace(ru_utime=2.1, ru_stime=1.1)

    fake_resource = FakeResource()
    monkeypatch.setattr("app.ocr.workers.sys.platform", "linux")
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    shared = {"file_size_bytes": 1_024, "open_files": 128, "cpu_seconds": 180}
    apply_worker_limits({"memory_bytes": 512, **shared})
    apply_worker_limits({"memory_bytes": 2_048, **shared})

    assert fake_resource.limits[fake_resource.RLIMIT_AS][0] == 2_048
    assert fake_resource.limits[fake_resource.RLIMIT_FSIZE][0] == 1_024
    assert fake_resource.limits[fake_resource.RLIMIT_NOFILE][0] == 128
    assert fake_resource.limits[fake_resource.RLIMIT_CORE][0] == 0
    assert all(
        fake_resource.limits[key][1] == hard for key, hard in fake_resource.original_hard.items()
    )


@pytest.mark.asyncio
async def test_resource_termination_has_stable_file_and_ocr_errors(
    settings_factory,
    tmp_path: Path,
) -> None:
    class ResourceLimitedRunner:
        async def run(self, *_args, **_kwargs):
            raise ProcessJobResourceLimit("worker exited")

    image = tmp_path / "receipt.png"
    image.write_bytes(png_bytes())
    with pytest.raises(ApiError) as file_error:
        await validate_new_file(
            image,
            "png",
            settings_factory(),
            ResourceLimitedRunner(),  # type: ignore[arg-type]
        )
    assert file_error.value.code == "PROCESS_RESOURCE_LIMIT"

    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    service = OcrService(settings, None, ResourceLimitedRunner())  # type: ignore[arg-type]
    stored = StoredFile(
        temp_id="1",
        path=image,
        extension="png",
        media_type="image/png",
        size=image.stat().st_size,
        original_name="receipt.png",
    )
    with pytest.raises(ApiError) as ocr_error:
        await service.recognize_file(stored, reference_year=2026)
    assert ocr_error.value.code == "OCR_FAILED"


def test_pdf_worker_wrappers_sanitize_memory_error(
    settings_factory,
    monkeypatch,
) -> None:
    secret_path = "/private/receipt-sensitive.pdf"

    def exhaust_memory(*_args, **_kwargs):
        raise MemoryError("secret allocation detail")

    monkeypatch.setattr(workers, "inspect_single_page_pdf", exhaust_memory)
    settings = settings_factory()
    for wrapper in (inspect_pdf_worker, extract_pdf_text_worker):
        result = wrapper(secret_path, settings.pdf_limits, settings.file_worker_limits)
        assert result == {
            "ok": False,
            "code": "WORKER_RESOURCE_LIMIT",
            "message": "本地文件处理超过资源限制",
            "status": 422,
        }
        assert secret_path not in repr(result)
        assert "secret allocation detail" not in repr(result)


def test_image_worker_wrapper_sanitizes_memory_error(
    settings_factory,
    monkeypatch,
) -> None:
    secret_path = "/private/receipt-sensitive.png"

    def exhaust_memory(*_args, **_kwargs):
        raise MemoryError("secret allocation detail")

    monkeypatch.setattr(workers, "os_open", exhaust_memory)
    settings = settings_factory()
    result = validate_image_worker(
        secret_path,
        "png",
        {"max_pixels": 100, "max_dimension": 10},
        settings.file_worker_limits,
    )
    assert result["code"] == "WORKER_RESOURCE_LIMIT"
    assert result["status"] == 422
    assert secret_path not in repr(result)
    assert "secret allocation detail" not in repr(result)


def test_ocr_worker_wrapper_sanitizes_memory_error(
    settings_factory,
    monkeypatch,
) -> None:
    secret_path = "/private/receipt-sensitive.png"

    class ExhaustedEngine:
        def __init__(self, _settings):
            raise MemoryError("secret model allocation detail")

    workers._ENGINES.clear()
    monkeypatch.setattr(workers, "PaddleLocalOcrEngine", ExhaustedEngine)
    settings = settings_factory()
    result = recognize_document_worker(
        secret_path,
        "png",
        {
            "ocr_detection_model_dir": "/models/det",
            "ocr_recognition_model_dir": "/models/rec",
            "ocr_engine": "paddle_static",
            "ocr_cpu_threads": 1,
        },
        settings.pdf_limits,
        settings.ocr_worker_limits,
    )
    assert result["code"] == "WORKER_RESOURCE_LIMIT"
    assert result["status"] == 422
    assert secret_path not in repr(result)
    assert "secret model allocation detail" not in repr(result)


@pytest.mark.asyncio
async def test_worker_memory_marker_maps_to_public_file_and_ocr_errors(
    settings_factory,
    tmp_path: Path,
) -> None:
    class MemoryMarkerRunner:
        async def run(self, *_args, **_kwargs):
            return {
                "ok": False,
                "code": "WORKER_RESOURCE_LIMIT",
                "message": "本地文件处理超过资源限制",
                "status": 422,
            }

    image = tmp_path / "receipt.png"
    image.write_bytes(png_bytes())
    runner = MemoryMarkerRunner()
    with pytest.raises(ApiError) as file_error:
        await validate_new_file(
            image,
            "png",
            settings_factory(),
            runner,  # type: ignore[arg-type]
        )
    assert file_error.value.code == "PROCESS_RESOURCE_LIMIT"

    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    service = OcrService(settings, None, runner)  # type: ignore[arg-type]
    stored = StoredFile(
        temp_id="2",
        path=image,
        extension="png",
        media_type="image/png",
        size=image.stat().st_size,
        original_name="receipt.png",
    )
    with pytest.raises(ApiError) as ocr_error:
        await service.recognize_file(stored, reference_year=2026)
    assert ocr_error.value.code == "OCR_FAILED"


def _pdf_image(writer: PdfWriter, width: int, height: int):
    image = StreamObject()
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(width),
            NameObject("/Height"): NumberObject(height),
        }
    )
    return writer._add_object(image)


def _pdf_form(writer: PdfWriter, xobjects: DictionaryObject | None = None):
    form = StreamObject()
    resources = DictionaryObject()
    if xobjects is not None:
        resources[NameObject("/XObject")] = xobjects
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(10), NumberObject(10)]
            ),
            NameObject("/Resources"): resources,
        }
    )
    return form, writer._add_object(form)


def _write_page_with_xobjects(path: Path, writer: PdfWriter, xobjects: DictionaryObject) -> None:
    page = writer.add_blank_page(width=100, height=100)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): xobjects})
    with path.open("wb") as stream:
        writer.write(stream)


def test_pdf_nested_forms_count_images_and_reject_huge_nested_image(tmp_path: Path) -> None:
    limits = PdfLimits(
        render_dpi=72,
        max_render_pixels=1_000_000,
        max_dimension=2_000,
        max_xobjects=10,
        max_embedded_pixels=10_000,
        max_resource_depth=4,
    )
    benign = tmp_path / "benign-nested.pdf"
    writer = PdfWriter()
    image_ref = _pdf_image(writer, 10, 10)
    _, inner_ref = _pdf_form(
        writer,
        DictionaryObject({NameObject("/Im0"): image_ref}),
    )
    _, outer_ref = _pdf_form(
        writer,
        DictionaryObject({NameObject("/Fm1"): inner_ref}),
    )
    _write_page_with_xobjects(
        benign,
        writer,
        DictionaryObject({NameObject("/Fm0"): outer_ref}),
    )
    inspection = inspect_single_page_pdf(benign, limits, extract_text=False)
    assert inspection.embedded_images == 1
    assert inspection.embedded_pixels == 100

    huge = tmp_path / "huge-nested.pdf"
    writer = PdfWriter()
    huge_ref = _pdf_image(writer, 100_000, 100_000)
    _, form_ref = _pdf_form(
        writer,
        DictionaryObject({NameObject("/Im0"): huge_ref}),
    )
    _write_page_with_xobjects(
        huge,
        writer,
        DictionaryObject({NameObject("/Fm0"): form_ref}),
    )
    with pytest.raises(ApiError) as huge_error:
        inspect_single_page_pdf(huge, limits, extract_text=False)
    assert huge_error.value.code == "PDF_PAGE_TOO_LARGE"


def test_pdf_resource_cycle_depth_and_total_object_caps_are_stable(tmp_path: Path) -> None:
    base_limits = dict(
        render_dpi=72,
        max_render_pixels=1_000_000,
        max_dimension=2_000,
        max_embedded_pixels=10_000,
    )

    cyclic = tmp_path / "cycle.pdf"
    writer = PdfWriter()
    form, form_ref = _pdf_form(writer)
    form[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Self"): form_ref})}
    )
    _write_page_with_xobjects(
        cyclic,
        writer,
        DictionaryObject({NameObject("/Fm0"): form_ref}),
    )
    with pytest.raises(ApiError) as cycle_error:
        inspect_single_page_pdf(
            cyclic,
            PdfLimits(**base_limits, max_xobjects=10, max_resource_depth=8),
            extract_text=False,
        )
    assert cycle_error.value.code == "PDF_TOO_COMPLEX"

    deep = tmp_path / "deep.pdf"
    writer = PdfWriter()
    _, leaf_ref = _pdf_form(writer)
    _, middle_ref = _pdf_form(
        writer,
        DictionaryObject({NameObject("/Leaf"): leaf_ref}),
    )
    _, root_ref = _pdf_form(
        writer,
        DictionaryObject({NameObject("/Middle"): middle_ref}),
    )
    _write_page_with_xobjects(
        deep,
        writer,
        DictionaryObject({NameObject("/Root"): root_ref}),
    )
    with pytest.raises(ApiError) as depth_error:
        inspect_single_page_pdf(
            deep,
            PdfLimits(**base_limits, max_xobjects=10, max_resource_depth=1),
            extract_text=False,
        )
    assert depth_error.value.code == "PDF_TOO_COMPLEX"

    crowded = tmp_path / "crowded.pdf"
    writer = PdfWriter()
    refs = [_pdf_form(writer)[1] for _ in range(3)]
    _write_page_with_xobjects(
        crowded,
        writer,
        DictionaryObject({NameObject(f"/Fm{index}"): ref for index, ref in enumerate(refs)}),
    )
    with pytest.raises(ApiError) as count_error:
        inspect_single_page_pdf(
            crowded,
            PdfLimits(**base_limits, max_xobjects=2, max_resource_depth=8),
            extract_text=False,
        )
    assert count_error.value.code == "PDF_TOO_COMPLEX"


def test_backend_container_is_non_root_read_only_and_proxy_only() -> None:
    repository = Path(__file__).parents[2]
    dockerfile = (repository / "backend" / "Dockerfile").read_text()
    compose = (repository / "docker-compose.yml").read_text()
    user_line = dockerfile.index("USER 10001:10001")
    entrypoint_line = dockerfile.index("ENTRYPOINT")
    assert user_line < entrypoint_line
    assert 'CMD ["uvicorn", "app.main:app"' in dockerfile
    assert 'exec "$@"' in (repository / "backend" / "docker-entrypoint.sh").read_text()
    assert "chown expense:expense /app/data /tmp/expense" in dockerfile
    assert "chmod -R a-w /app/app /app/migrations" in dockerfile
    backend_block, web_block = compose.split("\n  web:", 1)
    assert "read_only: true" in backend_block
    assert "mem_limit:" in backend_block
    assert "pids_limit:" in backend_block
    assert 'max-size: "10m"' in backend_block
    assert 'max-file: "3"' in backend_block
    assert "/tmp/expense:uid=10001,gid=10001,mode=0700" in backend_block
    assert "ports:" not in backend_block
    assert "ports:" in web_block
