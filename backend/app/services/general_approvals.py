"""Employee approval discovery and verification, independent of reimbursement templates."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.errors import ApiError
from app.core.security import token_hash, tokens_match
from app.integrations.dingtalk.client import DingTalkOpenAPIError
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient, WorkflowApprovalSummary

_SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


async def verify_general_approvals(
    workflow: DingTalkWorkflowClient,
    *,
    user_id: str,
    instance_ids: list[str],
) -> tuple[WorkflowApprovalSummary, ...]:
    verified = []
    for instance_id in instance_ids:
        instance = await workflow.get_process_instance_summary(instance_id)
        if instance.instance_id != instance_id or instance.originator_user_id != user_id:
            raise ApiError("OA_GENERAL_APPROVAL_NOT_OWNED", "只能关联本人发起的审批", 422)
        verified.append(instance)
    return tuple(verified)


def approval_summary(instance: WorkflowApprovalSummary) -> dict[str, object]:
    return {
        "processInstanceId": instance.instance_id,
        "title": instance.title,
        "businessId": instance.business_id,
        "status": instance.status,
        "result": instance.result,
        "createdAt": instance.created_at,
        **(
            {
                "summaryFields": [
                    {"label": field.label, "value": field.value}
                    for field in instance.summary_fields
                ]
            }
            if instance.summary_fields
            else {}
        ),
    }


def general_approval_date_range(
    from_date: date | None,
    to_date: date | None,
    *,
    days: int,
    today: date | None = None,
) -> tuple[date, date]:
    today = today or datetime.now(UTC).astimezone(_SHANGHAI).date()
    if from_date is None and to_date is None:
        return today - timedelta(days=days - 1), today
    if from_date is None or to_date is None or from_date > to_date:
        raise ApiError(
            "GENERAL_APPROVAL_QUERY_WINDOW_INVALID",
            "请选择有效的开始日期和结束日期",
            422,
        )
    earliest = today - timedelta(days=364)
    if from_date < earliest or to_date > today:
        raise ApiError(
            "GENERAL_APPROVAL_QUERY_WINDOW_INVALID",
            f"审批发起日期须在 {earliest} 至 {today} 内（上海时间）",
            422,
        )
    return from_date, to_date


def approval_windows(first: date, last: date) -> list[tuple[int, int]]:
    windows = []
    while last >= first:
        start = max(first, last - timedelta(days=119))
        windows.append(
            (
                int(datetime.combine(start, time.min, _SHANGHAI).timestamp() * 1000),
                int(
                    datetime.combine(last + timedelta(days=1), time.min, _SHANGHAI).timestamp()
                    * 1000
                )
                - 1,
            )
        )
        last = start - timedelta(days=1)
    return windows


@dataclass(frozen=True)
class ApprovalSearchPosition:
    window_index: int = 0
    template_index: int = 0
    page_token: int = 0
    partial: bool = False
    incomplete_reasons: tuple[str, ...] = ()

    def encode(self, scope: str, secret: str) -> str:
        data = json.dumps(
            [
                self.window_index,
                self.template_index,
                self.page_token,
                self.partial,
                self.incomplete_reasons,
            ]
        )
        value = base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")
        return f"{value}.{token_hash(f'{scope}.{value}', secret)}"

    @classmethod
    def decode(cls, cursor: str, scope: str, secret: str) -> ApprovalSearchPosition:
        try:
            value, signature = cursor.split(".")
            if not tokens_match(f"{scope}.{value}", signature, secret):
                raise ValueError("invalid signature")
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            values = json.loads(decoded)
            if len(values) == 5:
                values[4] = tuple(values[4])
            return cls(*values)
        except (TypeError, ValueError):
            raise ApiError(
                "GENERAL_APPROVAL_SEARCH_CURSOR_INVALID",
                "查询条件或审批目录已变化，请重新查询",
                422,
            ) from None


async def find_general_approvals(
    workflow: DingTalkWorkflowClient,
    *,
    user_id: str,
    date_range: tuple[date, date],
    keyword: str,
    session_id: str,
    secret: str,
    cursor: str | None = None,
    process_code: str = "",
) -> dict[str, object]:
    try:
        visible, directory_complete = await workflow.list_enterprise_templates()
    except DingTalkOpenAPIError as exc:
        if exc.code == "DINGTALK_PERMISSION_MISSING":
            raise ApiError(
                "GENERAL_APPROVAL_DIRECTORY_PERMISSION_MISSING",
                "钉钉应用缺少审批模板读取权限，请联系管理员开通后重试",
                503,
            ) from None
        visible, directory_complete = (), False
    names = {item.process_code: item.name for item in visible if item.name}
    available_codes = tuple(sorted({item.process_code for item in visible}))
    if process_code and process_code not in available_codes:
        raise ApiError(
            "GENERAL_APPROVAL_TEMPLATE_UNAVAILABLE",
            "所选审批类别暂不可用，请选择其他类别或全部审批单后重试",
            503 if not directory_complete else 422,
        )
    codes = (process_code,) if process_code else available_codes
    windows = approval_windows(*date_range)
    scope = token_hash(
        json.dumps(
            [
                "general-approval-search-v6",
                session_id,
                user_id,
                windows,
                keyword,
                process_code,
                codes,
            ]
        ),
        secret,
    )
    position = ApprovalSearchPosition()
    if cursor:
        try:
            position = ApprovalSearchPosition.decode(cursor, scope, secret)
        except ApiError:
            if not directory_complete:
                raise ApiError(
                    "GENERAL_APPROVAL_SEARCH_DIRECTORY_UNAVAILABLE",
                    "审批目录暂不可用，已保留查询进度，请稍后重试",
                    503,
                ) from None
            raise
    window_index, template_index, page_token = (
        position.window_index,
        position.template_index,
        position.page_token,
    )
    partial = position.partial or not directory_complete
    incomplete_reasons = set(position.incomplete_reasons)
    if not directory_complete:
        incomplete_reasons.add("directory")
    ids: dict[str, str] = {}
    requests = 0
    # Bound each request while retaining the exact upstream position for the next batch.
    while codes and window_index < len(windows) and requests < 32 and len(ids) < 120:
        first, last = windows[window_index]
        requests += 1
        try:
            batch = await workflow.list_process_instance_ids(
                process_code=codes[template_index],
                start_time=first,
                end_time=last,
                next_token=page_token,
                max_results=20,
                user_ids=(user_id,),
                statuses=("COMPLETED", "RUNNING", "TERMINATED"),
            )
        except ApiError:
            partial = True
            incomplete_reasons.add("list")
        else:
            for instance_id in batch.instance_ids:
                ids.setdefault(instance_id, codes[template_index])
            if batch.next_token is not None and batch.next_token > page_token:
                page_token = batch.next_token
                continue
            if batch.next_token is not None:
                partial = True
                incomplete_reasons.add("pagination")
        page_token = 0
        template_index += 1
        if template_index == len(codes):
            template_index = 0
            window_index += 1

    upstream_limit = asyncio.Semaphore(4)

    async def read(instance_id: str) -> WorkflowApprovalSummary | None:
        try:
            async with upstream_limit:
                instance = await workflow.get_process_instance_summary(instance_id)
            return instance if instance.instance_id == instance_id else None
        except ApiError as exc:
            logger.warning(
                "General approval detail could not be read",
                extra={"error_code": exc.code},
            )
            return None

    details = await asyncio.gather(*(read(instance_id) for instance_id in ids))
    partial = partial or any(item is None for item in details)
    if any(item is None for item in details):
        incomplete_reasons.add("detail")
    owned = [
        item
        for item in details
        if item is not None
        and item.originator_user_id == user_id
    ]
    matches = [
        item for item in owned
        if (
            not keyword
            or keyword.casefold()
            in " ".join(
                (
                    item.title,
                    item.business_id,
                    *(field.label + " " + field.value for field in item.summary_fields),
                )
            ).casefold()
        )
    ]
    matches.sort(key=lambda item: (item.created_at, item.instance_id), reverse=True)
    matched_codes = {ids[item.instance_id] for item in owned}
    has_more = bool(codes) and window_index < len(windows)
    return {
        "templates": [
            {"processCode": code, "name": names.get(code) or "未命名审批单"}
            for code in available_codes if code in matched_codes
        ],
        "items": [
            {**approval_summary(item), "processCode": ids[item.instance_id]} for item in matches
        ],
        "complete": not has_more and not partial,
        "incompleteReasons": sorted(incomplete_reasons),
        **(
            {
                "nextCursor": ApprovalSearchPosition(
                    window_index,
                    template_index,
                    page_token,
                    partial,
                    tuple(sorted(incomplete_reasons)),
                ).encode(scope, secret)
            }
            if has_more
            else {}
        ),
    }
