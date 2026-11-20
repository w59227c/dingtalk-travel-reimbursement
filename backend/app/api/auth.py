from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ApiError
from app.database.session import get_db
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.schemas.common import success
from app.schemas.reimbursements import RelatedApprovalSelectionInput
from app.services.application_settings import get_admin_department_ids, get_app_title
from app.services.dingtalk import DepartmentIdentity, DingTalkIdentity, DingTalkService
from app.services.file_coordination import (
    FileOperationBusy,
    SessionFileCoordinator,
    SessionFilesRetired,
)
from app.services.oa_template_profiles import require_submission_ready_catalog
from app.services.reimbursement_context import (
    REIMBURSEMENT_CONTEXT_HEADER,
    begin_reimbursement_context,
    can_delegate_reimbursements,
    get_reimbursement_session,
    require_live_reimbursement_actor,
    require_reimbursement_csrf,
    restore_reimbursement_context,
    validate_reimbursement_operator,
    with_reimbursement_department,
)
from app.services.reimbursement_delegation_policy import (
    normalize_directory_id,
    require_delegation_permission_for_session,
)
from app.services.reimbursement_recovery import workspace_reimbursements
from app.services.reimbursement_workspaces import workspace_actor
from app.services.sessions import (
    CurrentSession,
    create_session,
    get_current_session,
    get_session_csrf,
    has_admin_access,
    require_csrf,
    selectable_departments,
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


class DepartmentFromTravelApprovalRequest(RelatedApprovalSelectionInput):
    keep_current_workspace: bool = Field(default=False, alias="keepCurrentWorkspace")
    current_department_id: str | None = Field(
        default=None, alias="currentDepartmentId", min_length=1, max_length=128
    )
    selected_department_id: str | None = Field(
        default=None,
        alias="selectedDepartmentId",
        min_length=1,
        max_length=128,
    )

    @field_validator("selected_department_id", "current_department_id", mode="before")
    @classmethod
    def normalize_selected_department_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ReimbursementContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(alias="userId", max_length=128)
    department_id: str | None = Field(default=None, alias="departmentId", max_length=128)

    @field_validator("user_id", "department_id")
    @classmethod
    def normalize_id(cls, value: str | None) -> str | None:
        return normalize_directory_id(value) if value is not None else None


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
            can_delegate=can_delegate_reimbursements(database, record),
            is_admin=record.is_admin
            or not get_admin_department_ids(database, record.corp_id).isdisjoint(
                item.id for item in identity.departments
            ),
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
async def me(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
) -> dict[str, object]:
    return await _me_response(request, database, current)


async def _me_response(
    request: Request, database: Session, current: CurrentSession
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    session_token = request.cookies.get(settings.session_cookie_name)
    if not session_token:
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
    csrf_token = get_session_csrf(
        database,
        settings,
        current.record,
        session_token,
    )
    try:
        is_admin = await has_admin_access(request, database, current)
    except ApiError as exc:
        if exc.status_code < 500:
            raise
        is_admin = False
    can_delegate = False
    try:
        if current.beneficiary is None:
            await validate_reimbursement_operator(request, database, current)
        else:
            require_live_reimbursement_actor(database, current, current.record.session_id_hash)
        can_delegate = True
    except ApiError as exc:
        if current.beneficiary is not None or (
            exc.code != "REIMBURSEMENT_DELEGATION_FORBIDDEN" and exc.status_code < 500
        ):
            raise
    return success(
        session_payload(
            current,
            csrf_token,
            can_delegate=can_delegate,
            is_admin=is_admin,
        )
    )


@router.post("/me/reimbursement-context")
async def select_reimbursement_context(
    body: ReimbursementContextRequest,
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    scoped = await begin_reimbursement_context(
        request, database, current, body.user_id, body.department_id
    )
    response.headers["Cache-Control"] = "no-store"
    return await _me_response(request, database, scoped)


@router.get("/me/reimbursement-employees")
async def get_reimbursement_employees(
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
    q: Annotated[str, Query(min_length=1, max_length=128)],
) -> dict[str, object]:
    session_id_hash = current.record.session_id_hash
    operator_id = current.record.dingtalk_user_id
    department_ids = await validate_reimbursement_operator(request, database, current)
    database.rollback()
    query = q.strip()
    directory: DingTalkService = request.app.state.dingtalk_service
    items = await directory.search_employee_entries(query) if query else []
    fresh = get_current_session(request, database)
    if fresh.record.session_id_hash != session_id_hash:
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
    require_delegation_permission_for_session(database, fresh.record, department_ids)
    response.headers["Cache-Control"] = "no-store"
    return success({"items": [item for item in items if item["userId"] != operator_id]})


class GeneralDepartmentRequest(BaseModel):
    department_id: str = Field(alias="departmentId", min_length=1, max_length=128)


@router.post("/me/department/for-general-reimbursement")
def select_general_department(
    body: GeneralDepartmentRequest,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    if current.beneficiary is not None:
        raise ApiError("OA_GENERAL_SELF_ONLY", "普通费用暂只支持本人报销", 403)
    department = next(
        (item for item in current.reimbursement_departments if item.id == body.department_id), None
    )
    if department is None:
        raise ApiError("REIMBURSEMENT_DEPARTMENT_INVALID", "所选部门不在当前可用部门中", 422)
    existing = workspace_reimbursements(database, actor=workspace_actor(current), kind="general")[
        "items"
    ]
    if existing and any(item["department"]["id"] != department.id for item in existing):
        raise ApiError(
            "REIMBURSEMENT_DEPARTMENT_CORRECTION_REQUIRED",
            "请在当前报销中更正办理部门，原内容和材料会保留",
            409,
        )
    current.record.current_department_id = department.id
    current.record.current_department_name = department.name
    database.commit()
    return success({"selectedDepartment": {"id": department.id, "name": department.name}})


@router.post("/me/department/from-travel-approval")
async def select_department_from_travel_approval(
    body: DepartmentFromTravelApprovalRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    """Resolve the current reimbursement department from a verified travel OA."""

    catalog = require_submission_ready_catalog(database)
    workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
    selection = TravelApprovalSelection(
        profile_key=body.profile_key,
        process_instance_id=body.process_instance_id,
        query_window=TravelApprovalQueryWindow.from_dates(
            body.query_window.from_date,
            body.query_window.to_date,
        ),
    )
    session_id_hash = current.record.session_id_hash
    current_user_id = current.reimbursement_user_id
    operator_id = current.reimbursement_operator_user_id
    database.rollback()
    verified = await reverify_travel_approval_selection(
        workflow,
        catalog,
        current_user_id=current_user_id,
        selections=(selection,),
        required_cc_user_id=operator_id,
    )
    # Approval verification is a remote request. The employee may have logged in
    # again while it was in flight, so do not resolve or persist a department
    # from the session snapshot captured by the request dependency.
    database.expire_all()
    fresh_current = await restore_reimbursement_context(
        request,
        database,
        get_current_session(request, database),
        request.headers.get(REIMBURSEMENT_CONTEXT_HEADER),
    )
    if fresh_current.record.session_id_hash != session_id_hash:
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
    departments = fresh_current.reimbursement_departments
    source_department = next(
        (item for item in departments if item.id == verified.department_id),
        None,
    )
    selected = source_department
    if source_department is not None and body.selected_department_id not in {
        None,
        source_department.id,
    }:
        raise ApiError(
            "REIMBURSEMENT_DEPARTMENT_SELECTION_NOT_ALLOWED",
            "出差审批属于当前部门，系统将自动使用该部门",
            422,
        )
    if selected is None and len(departments) == 1:
        selected = departments[0]
    if selected is None and body.selected_department_id is not None:
        selected = next(
            (item for item in departments if item.id == body.selected_department_id),
            None,
        )
        if selected is None:
            raise ApiError(
                "REIMBURSEMENT_DEPARTMENT_INVALID",
                "所选部门不在当前可用部门中，请重新进入应用",
                422,
            )
    if selected is None:
        return success(
            {
                "selectedDepartment": None,
                "selectionRequired": True,
                "departments": [{"id": item.id, "name": item.name} for item in departments],
            }
        )

    # Existing content must be corrected through the draft CAS endpoint. Legacy
    # clients cannot reserve another department's slot or silently replace it.
    actor = workspace_actor(fresh_current)
    existing = workspace_reimbursements(database, actor=actor)["items"]
    if existing and any(item["department"]["id"] != selected.id for item in existing):
        raise ApiError(
            "REIMBURSEMENT_DEPARTMENT_CORRECTION_REQUIRED",
            "请在当前报销中更正办理部门，原内容和材料会保留",
            409,
        )
    if fresh_current.beneficiary is None:
        record = fresh_current.record
        record.current_department_id = selected.id
        record.current_department_name = selected.name
        database.commit()
    elif fresh_current.beneficiary is not None:
        fresh_current = with_reimbursement_department(fresh_current, selected, request)
    return success(
        {
            "selectedDepartment": {"id": selected.id, "name": selected.name},
            "selectionRequired": False,
            "departments": [{"id": item.id, "name": item.name} for item in departments],
            **(
                {"reimbursementContext": fresh_current.reimbursement_context}
                if fresh_current.reimbursement_context
                else {}
            ),
        }
    )


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
