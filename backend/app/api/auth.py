from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ApiError
from app.database.session import get_db
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.models.session import UserSession, utc_now
from app.schemas.common import success
from app.schemas.reimbursements import RelatedApprovalSelectionInput
from app.services.application_settings import get_app_title
from app.services.dingtalk import DepartmentIdentity, DingTalkIdentity, DingTalkService
from app.services.file_coordination import (
    FileOperationBusy,
    SessionFileCoordinator,
    SessionFilesRetired,
)
from app.services.oa_template_profiles import require_submission_ready_catalog
from app.services.sessions import (
    CurrentSession,
    create_session,
    get_current_session,
    require_csrf,
    rotate_csrf,
    selectable_departments,
    serialize_departments,
    session_payload,
)
from app.services.temp_files import delete_session_files
from app.services.travel_approvals import (
    TravelApprovalQueryWindow,
    TravelApprovalSelection,
    reverify_travel_approval_selection,
)

router = APIRouter(tags=["authentication"])
logger = logging.getLogger(__name__)


class DingTalkLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_code: str = Field(alias="authCode", min_length=1, max_length=1024)


class DepartmentSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_id: str = Field(alias="departmentId", min_length=1, max_length=128)


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _login_response(
    response: Response,
    request: Request,
    database: Session,
    identity: DingTalkIdentity,
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    record, session_token, csrf_token = create_session(
        database,
        settings,
        identity,
        request.cookies.get(settings.session_cookie_name),
    )
    _set_session_cookie(response, settings, session_token)
    return success(
        session_payload(
            CurrentSession(
                record=record,
                departments=selectable_departments(identity.departments),
            ),
            csrf_token,
        )
    )


@router.get("/config/public")
def public_config(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    return success(
        {
            "corpId": settings.dingtalk_corp_id,
            "clientId": settings.dingtalk_client_id,
            "authMockEnabled": settings.app_env in {"development", "test"}
            and settings.auth_mock_enabled,
            "oaSubmissionEnabled": settings.dingtalk_oa_worker_enabled,
            "appTitle": get_app_title(database),
            "uploadLimits": {
                "maxFiles": settings.session_max_files,
                "maxFileBytes": settings.upload_max_file_bytes,
                "maxSessionBytes": settings.session_max_bytes,
            },
            "expenseLimits": {
                "maxItems": settings.expense_max_items,
            },
        }
    )


@router.post("/auth/dingtalk")
async def dingtalk_login(
    body: DingTalkLoginRequest,
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    service: DingTalkService = request.app.state.dingtalk_service
    identity = await service.get_identity(body.auth_code)
    return _login_response(response, request, database, identity)


@router.post("/auth/mock")
def mock_login(
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    if settings.app_env not in {"development", "test"} or not settings.auth_mock_enabled:
        raise ApiError("NOT_FOUND", "请求的资源不存在", 404)
    departments = tuple(
        DepartmentIdentity(id=department_id, name=name)
        for department_id, name in settings.mock_departments
    )
    if not departments:
        raise ApiError("AUTH_MOCK_INVALID", "开发免登部门配置无效", 500)
    identity = DingTalkIdentity(
        user_id=settings.auth_mock_user_id,
        union_id=f"mock-union-id:{settings.auth_mock_user_id}",
        name=settings.auth_mock_user_name,
        departments=departments,
    )
    return _login_response(response, request, database, identity)


@router.get("/me")
def me(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    csrf_token = rotate_csrf(database, settings, current.record)
    return success(session_payload(current, csrf_token))


@router.post("/me/department")
def select_department(
    body: DepartmentSelectionRequest,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    selected = next(
        (item for item in current.departments if item.id == body.department_id),
        None,
    )
    if selected is None:
        raise ApiError("INVALID_DEPARTMENT", "只能选择当前用户所属的部门", 400)
    current.record.current_department_id = selected.id
    current.record.current_department_name = selected.name
    database.commit()
    return success({"selectedDepartment": {"id": selected.id, "name": selected.name}})


@router.post("/me/department/from-travel-approval")
async def select_department_from_travel_approval(
    body: RelatedApprovalSelectionInput,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    """Bind reimbursement scope to the department recorded by a verified travel OA."""

    catalog = require_submission_ready_catalog(database)
    workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
    service: DingTalkService = request.app.state.dingtalk_service
    selection = TravelApprovalSelection(
        profile_key=body.profile_key,
        process_instance_id=body.process_instance_id,
        query_window=TravelApprovalQueryWindow.from_dates(
            body.query_window.from_date,
            body.query_window.to_date,
        ),
    )
    session_id_hash = current.record.session_id_hash
    current_departments = current.departments
    current_user_id = current.record.dingtalk_user_id
    database.rollback()
    verified = await reverify_travel_approval_selection(
        workflow,
        catalog,
        current_user_id=current_user_id,
        selections=(selection,),
    )
    selected = await service.get_department_identity(verified.department_id)
    if selected.name.startswith("其他"):
        raise ApiError(
            "TRAVEL_APPROVAL_DEPARTMENT_INVALID",
            "出差审批的所在部门不能用于报销，请重新选择",
            422,
        )

    database.expire_all()
    record = database.get(UserSession, session_id_hash)
    if record is None or record.expires_at <= utc_now():
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
    record.departments_json = serialize_departments((selected, *current_departments))
    record.current_department_id = selected.id
    record.current_department_name = selected.name
    database.commit()
    return success({"selectedDepartment": {"id": selected.id, "name": selected.name}})


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    session_id_hash = current.record.session_id_hash
    coordinator: SessionFileCoordinator = request.app.state.file_coordinator
    try:
        async with coordinator.async_session_lease(
            session_id_hash,
            retire=True,
            wait_seconds=settings.logout_file_wait_seconds,
        ):
            database.delete(current.record)
            database.commit()
            try:
                delete_session_files(settings, session_id_hash)
            except OSError as exc:
                logger.warning(
                    "Temporary session cleanup failed during logout",
                    extra={"exception_type": type(exc).__name__},
                )
    except SessionFilesRetired:
        # A duplicate logout is already effectively complete.
        database.delete(current.record)
        database.commit()
    except FileOperationBusy as exc:
        raise ApiError("FILE_OPERATION_BUSY", "临时票据正在处理中，请稍后重试退出", 429) from exc
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return success({})
