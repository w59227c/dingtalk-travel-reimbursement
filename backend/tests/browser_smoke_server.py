"""Disposable loopback server for the repository browser smoke command."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import app.core.config as config
from app.core.config import Settings
from app.integrations.dingtalk.approval_summary import ApprovalSummaryField
from app.integrations.dingtalk.workflow import WorkflowInstanceIdPage, WorkflowVisibleTemplate


class SmokeSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


def create_smoke_app(root: Path, dist: Path):
    settings = SmokeSettings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite:///{root / 'smoke.db'}",
        temp_dir=root / "temp",
        reimbursement_staging_dir=root / "staging",
        session_secret=secrets.token_urlsafe(32),
        session_cookie_secure=False,
        auth_mock_enabled=True,
        auth_mock_user_id="mock-user",
        auth_mock_user_name="合成验收员工",
        auth_mock_departments="100:合成验收部门",
        admin_user_ids="mock-user",
        dingtalk_client_id="synthetic-browser-client",
        dingtalk_client_secret="synthetic-browser-secret",
        dingtalk_corp_id="synthetic-browser-corporation",
        dingtalk_agent_id=123456,
        dingtalk_oa_worker_enabled=False,
        attendance_calendar_sync_enabled=False,
        ocr_enabled=True,
        ocr_fake_enabled=True,
    )
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        # Keep the module-level app and Alembic away from the developer's .env and data.
        from app.main import create_app

        migration = Config()
        migration.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
        )
        command.upgrade(migration, "head")
    finally:
        config.get_settings = previous

    from test_general_reimbursement import (
        PURCHASE_CODE,
        PURCHASE_INSTANCE,
        GeneralWorkflow,
        configure_profiles,
    )
    from test_oa_reimbursement_integration import TRAVEL_INSTANCE_ID, TRAVEL_PROCESS_CODE
    from test_receipt_bundle import image_bytes, pdf_bytes

    class SmokeWorkflow(GeneralWorkflow):
        ids = tuple(f"synthetic-browser-{index:03d}" for index in range(121))

        async def list_enterprise_templates(self):
            return (
                WorkflowVisibleTemplate(PURCHASE_CODE, "合成采购申请"),
                WorkflowVisibleTemplate(TRAVEL_PROCESS_CODE, "合成出差申请"),
            ), True

        async def list_process_instance_ids(self, *, process_code, next_token, **query):
            if process_code != PURCHASE_CODE:
                return await super().list_process_instance_ids(
                    process_code=process_code, next_token=next_token, **query
                )
            created_ms = int(self.approval_time.timestamp() * 1000)
            if not query["start_time"] <= created_ms <= query["end_time"]:
                return WorkflowInstanceIdPage((), None)
            ids = (*self.ids, "synthetic-other-user")
            end = min(next_token + query["max_results"], len(ids))
            return WorkflowInstanceIdPage(ids[next_token:end], end if end < len(ids) else None)

        async def get_process_instance(self, instance_id):
            if instance_id in self.ids:
                index = self.ids.index(instance_id)
                status, result = (
                    ("RUNNING", None), ("COMPLETED", "agree"),
                    ("COMPLETED", "refuse"), ("TERMINATED", None),
                )[index % 4]
                return replace(
                    await super().get_process_instance(PURCHASE_INSTANCE),
                    instance_id=instance_id,
                    title=f"合成采购审批 {index:03d}",
                    business_id=f"SYNTHETIC-BROWSER-{index:03d}",
                    status=status,
                    result=result,
                )
            instance = await super().get_process_instance(instance_id)
            if instance_id == TRAVEL_INSTANCE_ID:
                today = self.approval_time.date()
                values = {
                    "travel-start-id": (today - timedelta(days=3)).isoformat(),
                    "travel-end-id": (today - timedelta(days=1)).isoformat(),
                }
                return replace(instance, form_values=tuple(
                    replace(field, value=values[field.component_id])
                    if field.component_id in values else field for field in instance.form_values
                ))
            return instance

        async def get_process_instance_summary(self, instance_id):
            summary = await super().get_process_instance_summary(instance_id)
            if instance_id in self.ids:
                return replace(summary, summary_fields=(
                    ApprovalSummaryField("所属公司", "合成验收公司"),
                    ApprovalSummaryField("预算项目", "SYNTHETIC-SEARCH-42"),
                    ApprovalSummaryField("申请事项", "合成办公耗材采购"),
                ))
            return summary

        async def create_process_instance(self, _command):
            raise AssertionError("Browser smoke must never create an OA instance")

    def deny_external(_request):
        return httpx.Response(403, json={"error": "Browser smoke blocks external integrations"})

    application = create_app(
        settings,
        dingtalk_transport=httpx.MockTransport(deny_external),
        dingtalk_upload_transport=httpx.MockTransport(deny_external),
    )
    workflow = SmokeWorkflow()
    asyncio.run(configure_profiles(application, workflow))

    @application.middleware("http")
    async def deny_submission(request, call_next):
        if request.method == "POST" and (
            request.url.path.endswith("/submit") or "/submissions" in request.url.path
        ):
            return JSONResponse({"error": "Browser smoke disables OA submission"}, status_code=403)
        return await call_next(request)

    today = datetime.now(UTC).date().isoformat()
    (root / "synthetic.pdf").write_bytes(pdf_bytes("Synthetic page 1", "Synthetic page 2"))
    (root / "synthetic.png").write_bytes(image_bytes(size=(360, 480)))
    (root / "synthetic-proof.png").write_bytes(image_bytes(size=(361, 480)))
    application.add_api_route("/__browser_smoke__", lambda: {
        "fixture": "isolated-browser-smoke", "date": today,
    })
    for route in ("/", "/m", "/admin/settings", "/m/settings"):
        application.add_api_route(route, lambda: FileResponse(dist / "index.html"), methods=["GET"])
    application.mount("/assets", StaticFiles(directory=dist / "assets"), name="smoke-assets")
    return application


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    application = create_smoke_app(root, dist)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        (root / "server.private.json").write_text(json.dumps({"port": listener.getsockname()[1]}))
        server = uvicorn.Server(uvicorn.Config(application, log_level="warning"))
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
