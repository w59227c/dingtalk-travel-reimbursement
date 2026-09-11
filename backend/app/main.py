from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response

from app.api.auth import router as auth_router
from app.api.calculations import router as calculations_router
from app.api.health import router as health_router
from app.api.oa_reimbursements import router as oa_reimbursements_router
from app.api.oa_templates import router as oa_templates_router
from app.api.receipt_keywords import router as receipt_keywords_router
from app.api.reimbursement_files import router as reimbursement_files_router
from app.api.reimbursement_submissions import router as reimbursement_submissions_router
from app.api.reimbursements import router as reimbursements_router
from app.api.settings import router as settings_router
from app.core.config import Settings, get_settings
from app.core.errors import ApiError, error_response, install_error_handlers
from app.core.logging import configure_logging
from app.core.request_id import bind_request_id, request_id_from_header, reset_request_id
from app.database.session import create_database_engine, create_session_factory
from app.integrations.dingtalk.client import DingTalkOpenAPIClient
from app.integrations.dingtalk.storage import DingTalkStorageClient
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.ocr.engine import FakeOcrEngine
from app.ocr.types import LocalOcrEngine
from app.services.dingtalk import DingTalkService
from app.services.file_coordination import SessionFileCoordinator
from app.services.multipart_uploads import prepare_spool_directory
from app.services.oa_reimbursement import (
    DatabaseSubmissionState,
    DurableOAReimbursementWorker,
    LinkedLocalFileMaintenance,
    OAReimbursementProcessor,
    SnapshotSubmissionMaterializer,
)
from app.services.ocr_service import OcrService
from app.services.process_jobs import KillableProcessRunner
from app.services.reimbursement_quota import ReimbursementQuotaCoordinator
from app.services.reimbursement_staging import ReimbursementStaging
from app.services.sessions import SessionCleanupGate, purge_expired_sessions
from app.services.temp_files import cleanup_expired_temp_files
from app.services.warm_ocr_process import WarmOcrProcess

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    dingtalk_transport: httpx.AsyncBaseTransport | None = None,
    dingtalk_upload_transport: httpx.AsyncBaseTransport | None = None,
    ocr_engine: LocalOcrEngine | None = None,
) -> FastAPI:
    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)
    if ocr_engine is not None and ocr_engine.is_fake and runtime_settings.app_env == "production":
        raise ValueError("A fake OCR engine cannot be injected in production")
    reimbursement_staging = ReimbursementStaging(
        runtime_settings.reimbursement_staging_dir,
        max_object_bytes=runtime_settings.upload_max_file_bytes,
    )
    database_engine = create_database_engine(runtime_settings.database_url)
    database_session_factory = create_session_factory(database_engine)
    reimbursement_quota = ReimbursementQuotaCoordinator(
        database_engine,
        reimbursement_staging,
        max_bytes=runtime_settings.reimbursement_staging_max_bytes,
    )
    dingtalk_client = DingTalkOpenAPIClient(runtime_settings, transport=dingtalk_transport)
    dingtalk_service = DingTalkService(dingtalk_client)
    dingtalk_workflow = DingTalkWorkflowClient(dingtalk_client)
    dingtalk_storage = DingTalkStorageClient(
        dingtalk_client,
        runtime_settings,
        upload_transport=dingtalk_upload_transport,
    )
    session_cleanup_gate = SessionCleanupGate(runtime_settings.session_cleanup_interval_seconds)
    file_coordinator = SessionFileCoordinator(
        file_wait_seconds=runtime_settings.file_operation_wait_seconds,
        upload_wait_seconds=runtime_settings.upload_admission_wait_seconds,
    )
    if runtime_settings.ocr_fake_enabled:
        ocr_engine = ocr_engine or FakeOcrEngine()
    warm_ocr_process = WarmOcrProcess()
    process_runner = KillableProcessRunner(
        warm_ocr_process,
        admission_timeout_seconds=runtime_settings.process_job_admission_wait_seconds,
    )
    # One bounded image/PDF validation process may overlap the single OCR
    # process. Validation retains its 512 MiB profile and never loads models.
    file_validation_runner = KillableProcessRunner(
        admission_timeout_seconds=runtime_settings.process_job_admission_wait_seconds
    )
    ocr_service = OcrService(runtime_settings, ocr_engine, process_runner)
    oa_worker_id = f"{socket.gethostname()[:48]}:{os.getpid()}:{uuid4().hex}"
    oa_materializer = SnapshotSubmissionMaterializer(
        workflow=dingtalk_workflow,
        staging=reimbursement_staging,
        excel_template_path=runtime_settings.excel_template_path,
        approval_url_factory=runtime_settings.dingtalk_approval_url,
    )
    oa_submission_state = DatabaseSubmissionState(
        session_factory=database_session_factory,
        worker_id=oa_worker_id,
        quota=reimbursement_quota,
        staging=reimbursement_staging,
        materializer=oa_materializer,
        generated_reservation_seconds=max(
            runtime_settings.dingtalk_oa_worker_lease_seconds * 2,
            int(runtime_settings.dingtalk_storage_upload_timeout_seconds) + 60,
        ),
    )
    oa_processor = OAReimbursementProcessor(
        state=oa_submission_state,
        materializer=oa_materializer,
        workflow=dingtalk_workflow,
        storage=dingtalk_storage,
        lease_seconds=runtime_settings.dingtalk_oa_worker_lease_seconds,
        retry_base_seconds=runtime_settings.dingtalk_oa_worker_retry_base_seconds,
        retry_max_seconds=runtime_settings.dingtalk_oa_worker_retry_max_seconds,
        reconciliation_seconds=runtime_settings.dingtalk_oa_worker_reconciliation_seconds,
    )
    oa_maintenance = LinkedLocalFileMaintenance(
        session_factory=database_session_factory,
        staging=reimbursement_staging,
    )
    oa_worker = DurableOAReimbursementWorker(
        worker_id=oa_worker_id,
        state=oa_submission_state,
        processor=oa_processor,
        maintenance=oa_maintenance,
        lease_seconds=runtime_settings.dingtalk_oa_worker_lease_seconds,
        poll_interval_seconds=runtime_settings.dingtalk_oa_worker_poll_interval_seconds,
    )

    def cleanup_expired_reimbursements() -> None:
        try:
            reclaimed = reimbursement_quota.reclaim_expired()
        except Exception as exc:
            logger.error(
                "Reimbursement staging cleanup failed",
                extra={"exception_type": type(exc).__name__},
            )
            return
        if reclaimed:
            logger.info(
                "Reclaimed expired reimbursement staging records",
                extra={"reclaimed_count": reclaimed},
            )

    async def periodic_temp_cleanup(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=runtime_settings.temp_cleanup_interval_seconds,
                )
            except TimeoutError:
                try:
                    await asyncio.to_thread(
                        cleanup_expired_temp_files,
                        runtime_settings,
                        file_coordinator,
                    )
                except Exception as exc:
                    logger.error(
                        "Periodic temporary-file cleanup failed",
                        extra={"exception_type": type(exc).__name__},
                    )
                await asyncio.to_thread(cleanup_expired_reimbursements)

    async def stop_periodic_temp_cleanup(
        stop: asyncio.Event,
        task: asyncio.Task[None],
    ) -> None:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop_oa_submission_worker(
        stop: asyncio.Event,
        task: asyncio.Task[None],
    ) -> None:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with AsyncExitStack() as resources:
            resources.callback(database_engine.dispose)
            resources.push_async_callback(dingtalk_client.close)
            resources.push_async_callback(dingtalk_storage.close)
            resources.push_async_callback(warm_ocr_process.close)
            resources.push_async_callback(process_runner.close)
            resources.push_async_callback(file_validation_runner.close)
            resources.push_async_callback(ocr_service.close)

            Path(runtime_settings.temp_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
            prepare_spool_directory(runtime_settings)
            reimbursement_staging.prepare()
            cleanup_expired_temp_files(runtime_settings, file_coordinator)
            cleanup_expired_reimbursements()
            with database_session_factory() as database:
                purge_expired_sessions(database)
            session_cleanup_gate.mark_completed()
            await ocr_service.ensure_ready()
            cleanup_stop = asyncio.Event()
            cleanup_task = asyncio.create_task(periodic_temp_cleanup(cleanup_stop))
            resources.push_async_callback(
                stop_periodic_temp_cleanup,
                cleanup_stop,
                cleanup_task,
            )
            if runtime_settings.dingtalk_oa_worker_enabled:
                oa_worker_stop = asyncio.Event()
                oa_worker_task = asyncio.create_task(oa_worker.run(oa_worker_stop))
                resources.push_async_callback(
                    stop_oa_submission_worker,
                    oa_worker_stop,
                    oa_worker_task,
                )
            yield

    application = FastAPI(
        title="DingTalk Travel Reimbursement API",
        version="0.2.0",
        docs_url="/api/docs" if runtime_settings.app_env != "production" else None,
        openapi_url="/api/openapi.json" if runtime_settings.app_env != "production" else None,
        lifespan=lifespan,
    )
    application.state.settings = runtime_settings
    application.state.database_engine = database_engine
    application.state.database_session_factory = database_session_factory
    application.state.dingtalk_service = dingtalk_service
    application.state.dingtalk_client = dingtalk_client
    application.state.dingtalk_workflow = dingtalk_workflow
    application.state.dingtalk_storage = dingtalk_storage
    application.state.reimbursement_staging = reimbursement_staging
    application.state.reimbursement_quota = reimbursement_quota
    application.state.session_cleanup_gate = session_cleanup_gate
    application.state.ocr_service = ocr_service
    application.state.process_runner = process_runner
    application.state.file_validation_runner = file_validation_runner
    application.state.file_coordinator = file_coordinator
    application.state.oa_reimbursement_materializer = oa_materializer
    application.state.oa_reimbursement_state = oa_submission_state
    application.state.oa_reimbursement_processor = oa_processor
    application.state.oa_reimbursement_maintenance = oa_maintenance
    application.state.oa_reimbursement_worker = oa_worker

    @application.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        request_id = request_id_from_header(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        token = bind_request_id(request_id)
        started_at = perf_counter()
        try:
            path_parts = request.url.path.split("/")
            is_reimbursement_file_upload = (
                len(path_parts) == 6
                and path_parts[1:4] == ["api", "reimbursements", "drafts"]
                and bool(path_parts[4])
                and path_parts[5] == "files"
            )
            if request.method == "POST" and is_reimbursement_file_upload:
                declared_length = request.headers.get("Content-Length")
                if declared_length:
                    try:
                        if int(declared_length) > runtime_settings.upload_max_request_bytes:
                            return error_response(
                                request,
                                "REQUEST_TOO_LARGE",
                                "上传请求体超过限制",
                                413,
                            )
                    except ValueError:
                        return error_response(
                            request,
                            "MALFORMED_REQUEST",
                            "请求长度无效",
                            400,
                        )
                received_bytes = 0
                original_receive = request._receive

                async def limited_receive():
                    nonlocal received_bytes
                    message = await original_receive()
                    if message.get("type") == "http.request":
                        received_bytes += len(message.get("body", b""))
                        if received_bytes > runtime_settings.upload_max_request_bytes:
                            raise ApiError("REQUEST_TOO_LARGE", "上传请求体超过限制", 413)
                    return message

                request._receive = limited_receive
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "Request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                },
            )
            return response
        finally:
            reset_request_id(token)

    install_error_handlers(application)
    application.include_router(health_router, prefix="/api")
    application.include_router(auth_router, prefix="/api")
    application.include_router(receipt_keywords_router, prefix="/api")
    application.include_router(settings_router, prefix="/api")
    application.include_router(calculations_router, prefix="/api")
    application.include_router(oa_templates_router, prefix="/api")
    application.include_router(oa_reimbursements_router, prefix="/api")
    application.include_router(reimbursements_router, prefix="/api")
    application.include_router(reimbursement_files_router, prefix="/api")
    application.include_router(reimbursement_submissions_router, prefix="/api")
    return application


app = create_app()
