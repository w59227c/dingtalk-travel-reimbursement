from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import (
    DingTalkWorkflowClient,
    WorkflowProcessInstance,
)
from app.services.oa_template_profiles import load_readonly_submission_catalog
from app.services.travel_approvals import (
    OaTemplateCatalogLike,
    TravelApprovalCandidate,
    TravelApprovalQueryWindow,
    TravelTemplateLike,
    evaluate_travel_approval_candidate,
    list_current_user_travel_approvals,
    matches_travel_approval_query,
    travel_approval_dates,
    travel_approval_reference,
)

IdentifierKind = Literal["auto", "business_id", "instance_id"]
logger = logging.getLogger(__name__)
_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_TIMEOUT_SECONDS = 60
_LOOKUP_LIMIT = 60
_REASONS = {
    "INSTANCE_ID_MISMATCH": "详情实例与列表实例不一致",
    "ORIGINATOR_MISMATCH": "发起人与查询员工不一致",
    "NOT_COMPLETED": "审批尚未完成或已撤销",
    "NOT_AGREED": "审批结果不是同意",
    "DATE_FIELDS_INVALID": "当前日期映射无法解析审批日期",
    "KEYWORD_MISMATCH": "标题和审批编号不匹配查询关键词",
}
_STAGES = {
    "catalog": "当前模板核对",
    "employee_list": "员工审批列表查询",
    "employee_details": "员工审批详情读取",
    "lookup_list": "目标审批短时回查",
    "lookup_details": "目标审批详情读取",
}


def _check(key: str, label: str, status: str, message: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "message": message}


class _Reads:
    """Request-local observation/cache; never logs or persists identities or form values."""

    def __init__(self, workflow: DingTalkWorkflowClient, catalog: OaTemplateCatalogLike) -> None:
        self.workflow = workflow
        self.catalog = catalog
        self.stage = "employee_list"
        self.formal = True
        self.list_requests = 0
        self.listed: dict[str, TravelTemplateLike] = {}
        self.instances: dict[str, WorkflowProcessInstance] = {}
        self.formal_details: set[str] = set()

    async def list_process_instance_ids(self, **kwargs):
        self.stage = "employee_list"
        self.list_requests += 1
        page = await self.workflow.list_process_instance_ids(**kwargs)
        profile = next(
            profile
            for profile in self.catalog.travel_profiles
            if profile.process_code == kwargs["process_code"]
        )
        self.listed.update(dict.fromkeys(page.instance_ids, profile))
        return page

    async def get_process_instance(self, instance_id: str) -> WorkflowProcessInstance:
        if instance_id not in self.instances:
            self.stage = "employee_details" if self.formal else "lookup_details"
            self.instances[instance_id] = await self.workflow.get_process_instance(instance_id)
        if self.formal:
            self.formal_details.add(instance_id)
        return self.instances[instance_id]

    def counts(
        self,
        user_id: str,
        window: TravelApprovalQueryWindow,
        keyword: str,
        candidates: tuple[TravelApprovalCandidate, ...] | None,
    ) -> dict[str, object]:
        rejected: Counter[str] = Counter()
        evaluated = 0
        for instance_id in self.formal_details:
            profile = self.listed.get(instance_id)
            if profile is None:
                continue
            reference = travel_approval_reference(
                profile, instance_id=instance_id, current_user_id=user_id, query_window=window
            )
            try:
                candidate, reason = evaluate_travel_approval_candidate(
                    reference, self.instances[instance_id], self.catalog
                )
            except ApiError:
                continue
            evaluated += 1
            if reason:
                rejected[reason] += 1
            elif candidate and not matches_travel_approval_query(candidate, keyword):
                rejected["KEYWORD_MISMATCH"] += 1
        return {
            "templates": len(self.catalog.travel_profiles),
            "listRequests": self.list_requests,
            "listed": len(self.listed),
            "detailsRead": len(self.formal_details),
            "evaluated": evaluated,
            "excluded": sum(rejected.values()),
            "returned": len(candidates) if candidates is not None else None,
            "unselectable": sum(bool(item.unavailable_reason) for item in candidates)
            if candidates is not None
            else None,
            "exclusions": [
                {"code": code, "reason": _REASONS[code], "count": count}
                for code, count in sorted(rejected.items())
            ],
        }


def _created_at(instance: WorkflowProcessInstance) -> datetime | None:
    try:
        value = datetime.fromisoformat(instance.created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=_TIME_ZONE) if value.tzinfo is None else value


def _number_minute(identifier: str) -> datetime | None:
    if not re.fullmatch(r"\d{16,32}", identifier):
        return None
    try:
        return datetime.strptime(identifier[:12], "%Y%m%d%H%M").replace(tzinfo=_TIME_ZONE)
    except ValueError:
        return None


@dataclass(frozen=True)
class _Located:
    instance: WorkflowProcessInstance
    profile: TravelTemplateLike | None


async def _short_lookup(
    reads: _Reads,
    identifier: str,
    kind: str,
    minute: datetime,
) -> _Located | None:
    start = minute.replace(second=0, microsecond=0)
    end = start + timedelta(minutes=1) - timedelta(milliseconds=1)
    visited: set[str] = set()
    for profile in reads.catalog.travel_profiles:
        token = 0
        for _ in range(3):
            reads.stage = "lookup_list"
            page = await reads.workflow.list_process_instance_ids_for_diagnostics(
                process_code=profile.process_code,
                start_time=int(start.timestamp() * 1000),
                end_time=int(end.timestamp() * 1000),
                next_token=token,
                max_results=20,
            )
            for instance_id in page.instance_ids:
                if instance_id in visited:
                    continue
                visited.add(instance_id)
                if len(visited) > _LOOKUP_LIMIT:
                    raise _lookup_limit_error()
                if kind == "instance_id":
                    if instance_id == identifier:
                        return _Located(await reads.get_process_instance(instance_id), profile)
                else:
                    instance = await reads.get_process_instance(instance_id)
                    if instance.business_id == identifier:
                        return _Located(instance, profile)
            if page.next_token is None:
                break
            token = page.next_token
        else:
            raise _lookup_limit_error()
    return None


def _lookup_limit_error() -> ApiError:
    return ApiError(
        "DIAGNOSIS_LOOKUP_LIMIT_EXCEEDED",
        "短时回查结果过多，尚未完成定位；请使用审批实例 ID 重试",
        422,
    )


async def _locate(reads: _Reads, identifier: str, kind: str) -> _Located | None:
    matches = [
        instance
        for instance in reads.instances.values()
        if (instance.business_id if kind == "business_id" else instance.instance_id) == identifier
    ]
    if len(matches) > 1:
        raise ApiError("DIAGNOSIS_IDENTIFIER_AMBIGUOUS", "审批编号对应多条记录，请使用实例 ID", 409)
    if matches:
        instance = matches[0]
        return _Located(instance, reads.listed.get(instance.instance_id))
    if kind == "business_id":
        minute = _number_minute(identifier)
        return await _short_lookup(reads, identifier, kind, minute) if minute else None
    instance = await reads.get_process_instance(identifier)
    minute = _created_at(instance)
    located = await _short_lookup(reads, identifier, kind, minute) if minute else None
    return located or _Located(instance, None)


def _target_checks(
    located: _Located,
    reads: _Reads,
    user_id: str,
    window: TravelApprovalQueryWindow,
    keyword: str,
    candidates: tuple[TravelApprovalCandidate, ...],
) -> tuple[list[dict[str, str]], dict[str, object], str, str, list[str]]:
    instance, profile = located.instance, located.profile
    owner_matches = instance.originator_user_id == user_id
    listed = instance.instance_id in reads.listed
    checks = [
        _check("located", "目标审批定位", "passed", "已读取目标审批详情"),
        _check(
            "source",
            "来源模板",
            "passed" if profile else "unknown",
            f"属于已配置模板：{profile.display_name}"
            if profile
            else "详情可读，但短时回查未能确认所属模板；不能断言模板未配置",
        ),
        _check(
            "owner",
            "发起人",
            "passed" if owner_matches else "failed",
            "发起人与输入 userId 一致" if owner_matches else "发起人与输入 userId 不一致",
        ),
        _check(
            "status",
            "流程状态",
            "passed" if instance.status == "COMPLETED" else "failed",
            "审批流程已完成" if instance.status == "COMPLETED" else "审批仍在进行或已撤销",
        ),
        _check(
            "result",
            "审批结果",
            "passed" if instance.result == "agree" else "failed",
            "审批结果为同意" if instance.result == "agree" else "审批结果不是同意",
        ),
    ]
    created = _created_at(instance)
    in_window = (
        window.start_time_ms <= int(created.timestamp() * 1000) <= window.end_time_ms
        if created
        else None
    )
    checks.append(
        _check(
            "window",
            "查询日期范围",
            "passed" if in_window else "unknown" if in_window is None or listed else "failed",
            "审批发起时间在查询范围内"
            if in_window
            else "审批发起时间不在本次查询范围内"
            if in_window is False and not listed
            else "无法从回读时间独立确认范围，以钉钉实际列表为准",
        )
    )
    dates = None
    candidate = None
    rejection = None
    if profile:
        dates = travel_approval_dates(
            instance,
            start_date_component_id=profile.start_date_component_id,
            end_date_component_id=profile.end_date_component_id,
        )
        reference = travel_approval_reference(
            profile, instance_id=instance.instance_id, current_user_id=user_id, query_window=window
        )
        candidate, rejection = evaluate_travel_approval_candidate(
            reference, instance, reads.catalog
        )
    checks.append(
        _check(
            "dates",
            "日期字段解析",
            "passed" if dates else "failed" if profile else "unknown",
            "日期可解析，属于同日行程；同日不会被排除"
            if dates and dates[0] == dates[1]
            else "开始、结束日期均可解析"
            if dates
            else "当前映射无法解析日期，请核对控件、日期格式、时长和起止顺序"
            if profile
            else "来源模板未确认，暂不能验证日期映射",
        )
    )
    keyword_matches = matches_travel_approval_query(candidate, keyword) if candidate else None
    checks.extend(
        [
            _check(
                "keyword",
                "查询关键词",
                "passed"
                if keyword_matches
                else "failed"
                if keyword_matches is False
                else "skipped",
                "标题或编号匹配关键词（空关键词也通过）"
                if keyword_matches
                else "关键词与审批标题、编号均不匹配"
                if keyword_matches is False
                else "先通过身份、状态和日期校验后再验证关键词",
            ),
            _check(
                "accounting",
                "公司、预算和出差类别",
                "skipped"
                if candidate is None
                else "failed"
                if candidate.unavailable_reason
                else "passed",
                candidate.unavailable_reason
                if candidate and candidate.unavailable_reason
                else "来源选项可唯一对应当前报销模板"
                if candidate
                else "前述候选条件未通过，未将选项问题作为排除原因",
            ),
            _check(
                "listed",
                "钉钉员工列表是否返回",
                "passed" if listed else "failed",
                "员工查询返回了此审批实例"
                if listed
                else "本次员工查询未返回此实例；详情可读不等于它会出现在员工列表中",
            ),
        ]
    )
    returned = next(
        (item for item in candidates if item.instance.instance_id == instance.instance_id), None
    )
    hints: list[str] = []
    if returned:
        if returned.unavailable_reason:
            outcome, summary = "unselectable", "审批会出现在列表中，但当前不可选择"
            hints.append("处理选项映射问题后重新查询；这类问题不应让审批从列表中消失")
        else:
            outcome, summary = "visible", "审批在当前条件下可以查询并选择"
            hints.append("若员工页面仍为空，请对照员工实际查询日期、关键词及登录上下文")
    else:
        outcome = "excluded"
        if rejection:
            summary = _REASONS[rejection]
        elif in_window is False:
            summary = "审批发起时间不在本次查询范围内"
        elif keyword_matches is False:
            summary = _REASONS["KEYWORD_MISMATCH"]
        else:
            summary = "目标详情可读，但本次员工查询没有返回该审批"
        if not owner_matches:
            hints.append("核对输入的员工 userId 与目标审批是否对应；诊断不会切换员工身份")
        if in_window is False:
            hints.append("按审批发起时间调整查询范围；行程日期与审批发起时间可能不同")
        if rejection == "DATE_FIELDS_INVALID":
            hints.append("请在 OA 模板目录中核对日期字段；不要仅凭页面显示的日期判断接口格式")
        if keyword_matches is False:
            hints.append("清空原查询关键词后重试")
        if not hints:
            hints.append("核对来源模板、应用审批列表的数据范围；当前证据不能证明审批不存在")
    record = {
        "ownerMatches": owner_matches,
        "status": instance.status,
        "result": instance.result,
        "createdDate": created.astimezone(_TIME_ZONE).date().isoformat()
        if created and owner_matches
        else None,
        "startDate": dates[0].isoformat() if dates and owner_matches else None,
        "endDate": dates[1].isoformat() if dates and owner_matches else None,
        "sameDay": dates[0] == dates[1] if dates and owner_matches else None,
    }
    return checks, record, outcome, summary, hints


async def diagnose_travel_approval(
    database_session_factory: sessionmaker[Session],
    workflow: DingTalkWorkflowClient,
    *,
    user_id: str,
    identifier: str,
    identifier_kind: IdentifierKind,
    query_window: TravelApprovalQueryWindow,
    keyword: str = "",
) -> dict[str, object]:
    report: dict[str, object] = {
        "checkedAt": datetime.now(UTC).isoformat(),
        "queryWindow": query_window.as_dict(),
        "outcome": "incomplete",
        "summary": "诊断未完成",
        "checks": [],
        "counts": None,
        "record": None,
        "hints": [],
    }
    checks: list[dict[str, str]] = []
    report["checks"] = checks
    reads = None
    candidates = None
    stage = "catalog"
    kind = identifier_kind
    if kind == "auto":
        kind = "business_id" if re.fullmatch(r"\d{16,32}", identifier) else "instance_id"
    report["identifierKind"] = kind
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            catalog = await load_readonly_submission_catalog(database_session_factory, workflow)
            report["configVersion"] = catalog.config_version
            checks.append(_check("catalog", _STAGES[stage], "passed", "当前模板结构和选项核对通过"))
            reads = _Reads(workflow, catalog)
            candidates = await list_current_user_travel_approvals(
                reads, catalog, current_user_id=user_id, query_window=query_window, query=keyword
            )
            checks.append(
                _check(
                    "employee_query",
                    "复现员工查询",
                    "passed",
                    f"查询已完成，返回 {len(candidates)} 张审批",
                )
            )
            reads.formal = False
            located = await _locate(reads, identifier, kind)
            if located is None:
                report.update(
                    outcome="not_found",
                    summary="本次查询和有界回查均未定位到该审批，不能据此断言审批不存在",
                    hints=["核对编号和员工 userId，调整日期范围，或改用审批实例 ID"],
                )
                checks.append(
                    _check(
                        "located",
                        "目标审批定位",
                        "unknown",
                        "未找到可验证的目标详情，无法判断其状态、日期或具体排除原因",
                    )
                )
            else:
                target_checks, record, outcome, summary, hints = _target_checks(
                    located, reads, user_id, query_window, keyword, candidates
                )
                checks.extend(target_checks)
                report.update(outcome=outcome, summary=summary, record=record, hints=hints)
    except (ApiError, TimeoutError) as error:
        stage = reads.stage if reads else stage
        incomplete = isinstance(error, TimeoutError) or getattr(error, "code", "") in {
            "TRAVEL_APPROVAL_RESULT_LIMIT_EXCEEDED",
            "DIAGNOSIS_LOOKUP_LIMIT_EXCEEDED",
            "DINGTALK_RATE_LIMITED",
        }
        message = (
            "诊断超时，已有结果不完整；请缩小日期范围后重试"
            if isinstance(error, TimeoutError)
            else error.message
        )
        report.update(
            outcome="incomplete" if incomplete else "blocked",
            summary=message,
            error={
                "code": getattr(error, "code", "DIAGNOSIS_TIMEOUT"),
                "upstreamHttpStatus": getattr(error, "http_status", None),
                "upstreamErrorCode": getattr(error, "upstream_code", None),
            },
            hints=["本次未完成全部检查，不代表审批不存在；处理提示后重试"],
        )
        checks.append(_check(stage, _STAGES[stage], "unknown" if incomplete else "failed", message))
    except Exception as error:
        logger.warning(
            "Approval diagnostic did not complete",
            extra={"exception_type": type(error).__name__},
        )
        stage = reads.stage if reads else stage
        report.update(
            outcome="incomplete",
            summary="诊断遇到未预期错误，不能据此判断审批是否可见",
            error={
                "code": "DIAGNOSIS_FAILED",
                "upstreamHttpStatus": None,
                "upstreamErrorCode": None,
            },
            hints=["请保留本次诊断编号供管理员排查"],
        )
        checks.append(_check(stage, _STAGES[stage], "unknown", "当前步骤未完成"))
    if reads:
        report["counts"] = reads.counts(user_id, query_window, keyword, candidates)
    return report
