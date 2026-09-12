from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import (
    DingTalkWorkflowClient,
    FormOption,
    FormSchema,
    WorkflowProcessInstance,
)
from app.services.subsidy_calculation import subsidy_purpose_for_travel_type

_DINGTALK_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_PAGE_SIZE = 20
_MAX_LISTED_IDS = 200
_MAX_PAGES_PER_PROFILE = 50
_DETAIL_CONCURRENCY = 5
_MAX_WINDOW_DAYS = 180
_DINGTALK_MAX_QUERY_DAYS = 120


class ReimbursementTemplateLike(Protocol):
    process_code: str
    schema: FormSchema
    mappings: dict[str, str]


class TravelTemplateLike(Protocol):
    profile_key: str
    display_name: str
    process_code: str
    schema: FormSchema
    start_date_component_id: str
    end_date_component_id: str
    travel_type_option: FormOption
    travel_type_mappings: dict[str, FormOption] | None
    company_component_id: str | None
    budget_code_component_id: str | None


class OaTemplateCatalogLike(Protocol):
    config_version: int
    reimbursement: ReimbursementTemplateLike
    travel_profiles: tuple[TravelTemplateLike, ...]


@dataclass(frozen=True, slots=True)
class TravelApprovalQueryWindow:
    from_date: date
    to_date: date
    start_time_ms: int
    end_time_ms: int

    @classmethod
    def from_dates(cls, from_date: date, to_date: date) -> TravelApprovalQueryWindow:
        if not isinstance(from_date, date) or not isinstance(to_date, date):
            raise ValueError("query window dates are required")
        if to_date < from_date or (to_date - from_date).days >= _MAX_WINDOW_DAYS:
            raise ApiError(
                "TRAVEL_APPROVAL_QUERY_WINDOW_INVALID",
                "出差审批查询范围必须为连续且不超过 180 天",
                422,
            )
        start = datetime.combine(from_date, time.min, tzinfo=_DINGTALK_TIME_ZONE)
        end_exclusive = datetime.combine(
            to_date + timedelta(days=1),
            time.min,
            tzinfo=_DINGTALK_TIME_ZONE,
        )
        return cls(
            from_date=from_date,
            to_date=to_date,
            start_time_ms=int(start.timestamp() * 1000),
            end_time_ms=int(end_exclusive.timestamp() * 1000) - 1,
        )

    @classmethod
    def latest(cls, *, today: date | None = None) -> TravelApprovalQueryWindow:
        end_date = today or datetime.now(_DINGTALK_TIME_ZONE).date()
        return cls.from_dates(
            end_date - timedelta(days=_MAX_WINDOW_DAYS - 1),
            end_date,
        )

    def as_dict(self) -> dict[str, str]:
        return {"from": self.from_date.isoformat(), "to": self.to_date.isoformat()}


@dataclass(frozen=True, slots=True)
class ListedTravelApproval:
    instance_id: str
    expected_originator_user_id: str
    profile_key: str
    profile_display_name: str
    source_process_code: str
    schema_fingerprint: str
    travel_type_option: FormOption
    start_date_component_id: str
    end_date_component_id: str
    query_window: TravelApprovalQueryWindow
    source_schema: FormSchema | None = None
    company_component_id: str | None = None
    budget_code_component_id: str | None = None
    travel_type_component_id: str | None = None
    travel_type_mappings: dict[str, FormOption] | None = None
    source_travel_type_value: str | None = None


@dataclass(frozen=True, slots=True)
class TravelApprovalCandidate:
    listed: ListedTravelApproval
    instance: WorkflowProcessInstance
    start_date: date
    end_date: date
    company_option: FormOption | None = None
    budget_code_option: FormOption | None = None
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "processInstanceId": self.instance.instance_id,
            "profileKey": self.listed.profile_key,
            "profileDisplayName": self.listed.profile_display_name,
            "sourceProcessCode": self.listed.source_process_code,
            "travelTypeOption": self.listed.travel_type_option.as_dict(),
            "subsidyTripType": _subsidy_trip_type_value(self.listed.travel_type_option),
            "title": self.instance.title,
            "businessId": self.instance.business_id,
            "startDate": self.start_date.isoformat(),
            "endDate": self.end_date.isoformat(),
            "createdAt": self.instance.created_at,
            "finishedAt": self.instance.finished_at,
            "companyOption": self.company_option.as_dict() if self.company_option else None,
            "budgetCodeOption": self.budget_code_option.as_dict()
            if self.budget_code_option
            else None,
            "unavailableReason": self.unavailable_reason,
        }
        if self.listed.source_travel_type_value is not None:
            result["sourceTravelTypeValue"] = self.listed.source_travel_type_value
        return result


@dataclass(frozen=True, slots=True)
class TravelApprovalSelection:
    profile_key: str
    process_instance_id: str
    query_window: TravelApprovalQueryWindow


@dataclass(frozen=True, slots=True)
class VerifiedTravelSelection:
    approvals: tuple[TravelApprovalCandidate, ...]
    department_id: str
    travel_type_option: FormOption
    start_date: date
    end_date: date
    company_option: FormOption
    budget_code_option: FormOption


def requested_query_window(
    from_date: date | None,
    to_date: date | None,
    *,
    today: date | None = None,
) -> TravelApprovalQueryWindow:
    if from_date is None and to_date is None:
        return TravelApprovalQueryWindow.latest(today=today)
    if from_date is None or to_date is None:
        raise ApiError(
            "TRAVEL_APPROVAL_QUERY_WINDOW_INVALID",
            "自定义查询范围必须同时填写开始和结束日期",
            422,
        )
    return TravelApprovalQueryWindow.from_dates(from_date, to_date)


def runtime_options(catalog: OaTemplateCatalogLike) -> dict[str, object]:
    reimbursement = catalog.reimbursement
    company_options = _mapped_options(reimbursement, "company")
    budget_options = _mapped_options(reimbursement, "budgetCode")
    return {
        "templateConfigVersion": catalog.config_version,
        "reimbursementProcessCode": reimbursement.process_code,
        "companyOptions": [option.as_dict() for option in company_options],
        "budgetCodeOptions": [option.as_dict() for option in budget_options],
        "travelProfiles": [
            {
                "profileKey": profile.profile_key,
                "displayName": profile.display_name,
                "processCode": profile.process_code,
                "schemaFingerprint": profile.schema.fingerprint,
                "travelTypeOption": profile.travel_type_option.as_dict(),
                "subsidyTripType": _subsidy_trip_type_value(profile.travel_type_option),
                **(
                    {
                        "travelTypeMappings": {
                            key: value.as_dict()
                            for key, value in profile.travel_type_mappings.items()
                        },
                        "subsidyTripTypeMappings": {
                            key: _subsidy_trip_type_value(value)
                            for key, value in profile.travel_type_mappings.items()
                        },
                    }
                    if getattr(profile, "travel_type_mappings", None) is not None
                    else {}
                ),
            }
            for profile in catalog.travel_profiles
        ],
    }


def _subsidy_trip_type_value(option: FormOption) -> str | None:
    purpose = subsidy_purpose_for_travel_type(option.label)
    return purpose.value if purpose is not None else None


async def list_current_user_travel_approvals(
    workflow: DingTalkWorkflowClient,
    catalog: OaTemplateCatalogLike,
    *,
    current_user_id: str,
    query_window: TravelApprovalQueryWindow,
    query: str = "",
) -> tuple[TravelApprovalCandidate, ...]:
    user_id = _required_text(current_user_id, field="current_user_id")
    _validate_window(query_window)
    listed = await _list_approval_references(
        workflow,
        catalog.travel_profiles,
        current_user_id=user_id,
        query_window=query_window,
    )
    resolved = await _resolve_details(workflow, tuple(listed.values()), catalog)
    eligible = tuple(
        candidate
        for candidate in resolved
        if candidate is not None and _matches_query(candidate, query)
    )
    return tuple(
        sorted(
            eligible,
            key=lambda item: (
                item.start_date,
                item.instance.created_at,
                item.instance.instance_id,
            ),
            reverse=True,
        )
    )


async def reverify_travel_approval_selection(
    workflow: DingTalkWorkflowClient,
    catalog: OaTemplateCatalogLike,
    *,
    current_user_id: str,
    selections: tuple[TravelApprovalSelection, ...],
    expected_department_id: str | None = None,
) -> VerifiedTravelSelection:
    """Re-prove list membership before trusting selected instance details."""

    if not selections:
        raise ApiError(
            "TRAVEL_APPROVAL_SELECTION_REQUIRED",
            "请至少选择一张已通过的出差审批单",
            422,
        )
    user_id = _required_text(current_user_id, field="current_user_id")
    seen_ids: set[str] = set()
    selections_by_window: dict[TravelApprovalQueryWindow, list[TravelApprovalSelection]] = {}
    for selection in selections:
        _validate_window(selection.query_window)
        instance_id = _required_text(
            selection.process_instance_id,
            field="process_instance_id",
        )
        if instance_id in seen_ids:
            raise ApiError(
                "TRAVEL_APPROVAL_SELECTION_DUPLICATE",
                "同一张出差审批单不能重复选择",
                422,
            )
        seen_ids.add(instance_id)
        selections_by_window.setdefault(selection.query_window, []).append(selection)

    proven: list[ListedTravelApproval] = []
    for query_window, window_selections in selections_by_window.items():
        listed = await _list_approval_references(
            workflow,
            catalog.travel_profiles,
            current_user_id=user_id,
            query_window=query_window,
        )
        for selection in window_selections:
            reference = listed.get(selection.process_instance_id)
            if reference is None or reference.profile_key != selection.profile_key:
                raise ApiError(
                    "TRAVEL_APPROVAL_MEMBERSHIP_CHANGED",
                    "所选出差审批已不在当前用户可关联的审批列表中，请重新选择",
                    409,
                )
            proven.append(reference)

    resolved = await _resolve_details(workflow, tuple(proven), catalog)
    if any(item is None for item in resolved):
        raise ApiError(
            "TRAVEL_APPROVAL_MEMBERSHIP_CHANGED",
            "所选出差审批状态或日期已变化，请重新选择",
            409,
        )
    approvals = tuple(item for item in resolved if item is not None)
    department_ids = {
        _required_text(item.instance.originator_department_id, field="originator_department_id")
        for item in approvals
    }
    if len(department_ids) != 1:
        raise ApiError(
            "TRAVEL_APPROVAL_DEPARTMENT_MISMATCH",
            "所选出差审批的所在部门不同，不能放在同一张报销单中",
            422,
        )
    department_id = next(iter(department_ids))
    if expected_department_id is not None and department_id != _required_text(
        expected_department_id,
        field="expected_department_id",
    ):
        raise ApiError(
            "TRAVEL_APPROVAL_DEPARTMENT_MISMATCH",
            "所选出差审批不属于本次报销部门，请重新选择",
            422,
        )
    travel_type_values = {item.listed.travel_type_option.value for item in approvals}
    source_travel_type_values = [
        item.listed.source_travel_type_value
        for item in approvals
        if item.listed.source_travel_type_value is not None
    ]
    if (
        len(travel_type_values) != 1
        or (source_travel_type_values and len(source_travel_type_values) != len(approvals))
        or len(set(source_travel_type_values)) > 1
    ):
        raise ApiError(
            "TRAVEL_APPROVAL_TYPE_MISMATCH",
            "所选出差审批的出差类别不同，不能放在同一张报销单中",
            422,
        )
    for item in approvals:
        if (
            item.unavailable_reason
            or item.company_option is None
            or item.budget_code_option is None
        ):
            raise ApiError(
                "TRAVEL_APPROVAL_FIELDS_UNAVAILABLE",
                item.unavailable_reason or "出差审批缺少所属公司或预算代码，请联系管理员",
                422,
            )
    if len({(item.company_option, item.budget_code_option) for item in approvals}) != 1:
        raise ApiError(
            "TRAVEL_APPROVAL_ACCOUNTING_MISMATCH",
            "所选出差审批的所属公司或预算代码不同，不能放在同一张报销单中",
            422,
        )
    return VerifiedTravelSelection(
        approvals=approvals,
        department_id=department_id,
        travel_type_option=approvals[0].listed.travel_type_option,
        start_date=min(item.start_date for item in approvals),
        end_date=max(item.end_date for item in approvals),
        company_option=approvals[0].company_option,
        budget_code_option=approvals[0].budget_code_option,
    )


async def _list_approval_references(
    workflow: DingTalkWorkflowClient,
    profiles: tuple[TravelTemplateLike, ...],
    *,
    current_user_id: str,
    query_window: TravelApprovalQueryWindow,
) -> dict[str, ListedTravelApproval]:
    listed: dict[str, ListedTravelApproval] = {}
    for profile in profiles:
        page_count = 0
        for upstream_window in _dingtalk_query_windows(query_window):
            next_token = 0
            while True:
                if page_count >= _MAX_PAGES_PER_PROFILE:
                    raise _result_limit_error()
                page_count += 1
                page = await workflow.list_process_instance_ids(
                    process_code=profile.process_code,
                    start_time=upstream_window.start_time_ms,
                    end_time=upstream_window.end_time_ms,
                    next_token=next_token,
                    max_results=_PAGE_SIZE,
                    user_ids=(current_user_id,),
                    statuses=("COMPLETED",),
                )
                for instance_id in page.instance_ids:
                    existing = listed.get(instance_id)
                    if existing is not None:
                        code = (
                            "TRAVEL_APPROVAL_SOURCE_AMBIGUOUS"
                            if existing.source_process_code != profile.process_code
                            else "TRAVEL_APPROVAL_LIST_INVALID"
                        )
                        raise ApiError(
                            code,
                            "钉钉返回的出差审批来源不唯一，请联系管理员检查模板配置",
                            409,
                        )
                    if len(listed) >= _MAX_LISTED_IDS:
                        raise _result_limit_error()
                    listed[instance_id] = ListedTravelApproval(
                        instance_id=instance_id,
                        expected_originator_user_id=current_user_id,
                        profile_key=profile.profile_key,
                        profile_display_name=profile.display_name,
                        source_process_code=profile.process_code,
                        schema_fingerprint=profile.schema.fingerprint,
                        travel_type_option=profile.travel_type_option,
                        start_date_component_id=profile.start_date_component_id,
                        end_date_component_id=profile.end_date_component_id,
                        query_window=query_window,
                        source_schema=profile.schema,
                        company_component_id=travel_source_component_id(profile, "company"),
                        budget_code_component_id=travel_source_component_id(profile, "budgetCode"),
                        travel_type_component_id=getattr(
                            profile, "travel_type_component_id", None
                        ),
                        travel_type_mappings=getattr(
                            profile, "travel_type_mappings", None
                        ),
                    )
                if page.next_token is None:
                    break
                next_token = page.next_token
    return listed


def _dingtalk_query_windows(
    query_window: TravelApprovalQueryWindow,
) -> tuple[TravelApprovalQueryWindow, ...]:
    ranges = dingtalk_process_query_ranges(
        query_window.start_time_ms,
        query_window.end_time_ms,
    )
    return tuple(
        TravelApprovalQueryWindow(
            from_date=datetime.fromtimestamp(start / 1000, _DINGTALK_TIME_ZONE).date(),
            to_date=datetime.fromtimestamp(end / 1000, _DINGTALK_TIME_ZONE).date(),
            start_time_ms=start,
            end_time_ms=end,
        )
        for start, end in ranges
    )


def dingtalk_process_query_ranges(
    start_time_ms: int,
    end_time_ms: int,
) -> tuple[tuple[int, int], ...]:
    """Split one inclusive business window into DingTalk-safe query ranges."""

    if start_time_ms < 0 or end_time_ms < start_time_ms:
        raise ValueError("invalid process instance query time range")
    max_duration_ms = _DINGTALK_MAX_QUERY_DAYS * 24 * 60 * 60 * 1000
    ranges: list[tuple[int, int]] = []
    start = start_time_ms
    while start <= end_time_ms:
        end = min(start + max_duration_ms - 1, end_time_ms)
        ranges.append((start, end))
        start = end + 1
    return tuple(ranges)


async def _resolve_details(
    workflow: DingTalkWorkflowClient,
    listed: tuple[ListedTravelApproval, ...],
    catalog: OaTemplateCatalogLike,
) -> tuple[TravelApprovalCandidate | None, ...]:
    if not listed:
        return ()
    semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

    async def resolve(reference: ListedTravelApproval) -> TravelApprovalCandidate | None:
        async with semaphore:
            instance = await workflow.get_process_instance(reference.instance_id)
        candidate = _eligible_candidate(reference, instance)
        if candidate is None:
            return None
        company, budget, reason = travel_accounting_options(
            instance,
            source_schema=reference.source_schema,
            company_component_id=reference.company_component_id,
            budget_code_component_id=reference.budget_code_component_id,
            company_options=_mapped_options(catalog.reimbursement, "company"),
            budget_options=_mapped_options(catalog.reimbursement, "budgetCode"),
        )
        option, source_value, type_reason = travel_instance_type_option(
            instance,
            source_schema=reference.source_schema,
            component_id=reference.travel_type_component_id,
            mappings=reference.travel_type_mappings,
            fixed_option=reference.travel_type_option,
        )
        return replace(
            candidate,
            listed=replace(
                reference,
                travel_type_option=option or reference.travel_type_option,
                source_travel_type_value=source_value,
            ),
            company_option=company,
            budget_code_option=budget,
            unavailable_reason=reason or type_reason,
        )

    tasks = [asyncio.create_task(resolve(reference)) for reference in listed]
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            if task.cancelled():
                raise asyncio.CancelledError
            failure = task.exception()
            if failure is not None:
                raise failure
        return tuple(task.result() for task in tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _eligible_candidate(
    listed: ListedTravelApproval,
    instance: WorkflowProcessInstance,
) -> TravelApprovalCandidate | None:
    if (
        instance.instance_id != listed.instance_id
        or instance.originator_user_id != listed.expected_originator_user_id
        or instance.status != "COMPLETED"
        or instance.result != "agree"
    ):
        return None
    dates = travel_approval_dates(
        instance,
        start_date_component_id=listed.start_date_component_id,
        end_date_component_id=listed.end_date_component_id,
    )
    if dates is None:
        return None
    start_date, end_date = dates
    return TravelApprovalCandidate(
        listed=listed,
        instance=instance,
        start_date=start_date,
        end_date=end_date,
    )


def travel_source_component_id(profile: TravelTemplateLike, logical_field: str) -> str | None:
    attribute = "company_component_id" if logical_field == "company" else "budget_code_component_id"
    configured = getattr(profile, attribute, None)
    if configured:
        return configured
    label = "所属公司" if logical_field == "company" else "预算代码"
    matches = [
        component.component_id
        for component in getattr(profile.schema, "components", ())
        if component.label == label
        and component.component_type in {"DDSelectField", "TextField"}
        and not any(
            getattr(component, flag, False)
            for flag in (
                "in_subtable",
                "disabled",
                "hidden",
                "ancestor_disabled",
                "ancestor_hidden",
            )
        )
    ]
    return matches[0] if len(matches) == 1 else None


def travel_instance_type_option(
    instance: WorkflowProcessInstance,
    *,
    source_schema: FormSchema | None,
    component_id: str | None,
    mappings: dict[str, FormOption] | None,
    fixed_option: FormOption,
) -> tuple[FormOption | None, str | None, str | None]:
    if component_id is None and mappings is None:
        return fixed_option, None, None
    values = [value.value for value in instance.form_values if value.component_id == component_id]
    source = next(
        (
            item
            for item in getattr(source_schema, "components", ())
            if item.component_id == component_id
        ),
        None,
    )
    if len(values) != 1 or not isinstance(values[0], str) or source is None or not mappings:
        return None, None, "出差审批的出差类别缺失或尚未配置对应关系"
    raw = values[0].strip()
    matches = [
        option for option in source.options if raw in {option.value, option.label, option.key}
    ]
    if len(matches) != 1 or matches[0].value not in mappings:
        return None, None, "出差审批的出差类别无法唯一对应报销类别"
    source_value = matches[0].value
    return mappings[source_value], source_value, None


def travel_accounting_options(
    instance: WorkflowProcessInstance,
    *,
    source_schema: FormSchema | None,
    company_component_id: str | None,
    budget_code_component_id: str | None,
    company_options: tuple[FormOption, ...],
    budget_options: tuple[FormOption, ...],
) -> tuple[FormOption | None, FormOption | None, str | None]:
    """Resolve source selections to unique current OA options without fuzzy matching."""
    resolved: list[FormOption] = []
    for label, component_id, targets in (
        ("所属公司", company_component_id, company_options),
        ("预算代码", budget_code_component_id, budget_options),
    ):
        if not component_id:
            return None, None, f"出差模板尚未配置{label}来源，请联系管理员"
        values = [
            value.value for value in instance.form_values if value.component_id == component_id
        ]
        if len(values) != 1 or not isinstance(values[0], str) or not values[0].strip():
            return None, None, f"出差审批的{label}缺失或不唯一"
        raw = values[0].strip()
        tokens = {raw}
        source = next(
            (
                item
                for item in getattr(source_schema, "components", ())
                if item.component_id == component_id
            ),
            None,
        )
        if source is not None and source.options:
            options = [
                option
                for option in source.options
                if raw in {option.value, option.label, option.key}
            ]
            if len(options) != 1:
                return None, None, f"出差审批的{label}无法唯一对应来源模板选项"
            # Option keys are scoped to their form; they are never reused across forms.
            tokens = (
                {options[0].label} if label == "预算代码" else {options[0].value, options[0].label}
            )
        if label == "预算代码":
            # Compare the entire label; never infer a budget from its numeric prefix.
            tokens = {"".join(token.split()) for token in tokens}
            matches = [option for option in targets if "".join(option.label.split()) in tokens]
        else:
            matches = [option for option in targets if tokens & {option.value, option.label}]
        if len(matches) != 1:
            return None, None, f"出差审批的{label}无法唯一对应当前报销表单选项"
        resolved.append(matches[0])
    return resolved[0], resolved[1], None


def travel_approval_dates(
    instance: WorkflowProcessInstance,
    *,
    start_date_component_id: str,
    end_date_component_id: str,
) -> tuple[date, date] | None:
    """Read one approved trip period from direct dates or a native itinerary table."""

    if start_date_component_id == end_date_component_id:
        from app.services.oa_date_range import parse_date_range

        values = [v for v in instance.form_values if v.component_id == start_date_component_id]
        if len(values) == 1 and values[0].component_type == "DDDateRangeField":
            return parse_date_range(values[0].value)
        return _itinerary_dates(instance, start_date_component_id)
    start_date = _component_date(instance, start_date_component_id)
    end_date = _component_date(instance, end_date_component_id)
    if start_date is None or end_date is None or end_date < start_date:
        return None
    return start_date, end_date


def _component_date(instance: WorkflowProcessInstance, component_id: str) -> date | None:
    value = next(
        (
            form_value.value
            for form_value in instance.form_values
            if form_value.component_id == component_id
        ),
        None,
    )
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _itinerary_dates(
    instance: WorkflowProcessInstance,
    component_id: str,
) -> tuple[date, date] | None:
    component = next(
        (
            form_value
            for form_value in instance.form_values
            if form_value.component_id == component_id
        ),
        None,
    )
    if (
        component is None
        or component.component_type not in {"TableField", "DDTableField"}
        or component.value is None
    ):
        return None
    try:
        rows = json.loads(component.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(rows, list) or not rows:
        return None

    starts: list[date] = []
    ends: list[date] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("rowValue"), list):
            return None
        row_values = row["rowValue"]
        start = _itinerary_row_date(row_values, "startTime")
        end = _itinerary_row_date(row_values, "endTime")
        if start is None or end is None or end < start:
            return None
        starts.append(start)
        ends.append(end)
    return min(starts), max(ends)


def _itinerary_row_date(values: list[object], biz_alias: str) -> date | None:
    matches = [
        value for value in values if isinstance(value, dict) and value.get("bizAlias") == biz_alias
    ]
    if len(matches) != 1:
        return None
    match = matches[0]
    key = match.get("key")
    raw_value = match.get("value")
    if (
        not isinstance(key, str)
        or not key.startswith("DDDateField")
        or not isinstance(raw_value, str)
    ):
        return None
    parts = raw_value.strip().split()
    if not 1 <= len(parts) <= 2 or (len(parts) == 2 and parts[1] not in {"上午", "下午"}):
        return None
    try:
        return date.fromisoformat(parts[0])
    except ValueError:
        return None


def _mapped_options(
    reimbursement: ReimbursementTemplateLike,
    logical_field: str,
) -> tuple[FormOption, ...]:
    component_id = reimbursement.mappings.get(logical_field)
    component = next(
        (item for item in reimbursement.schema.components if item.component_id == component_id),
        None,
    )
    if component is None or not component.options:
        raise ApiError(
            "OA_TEMPLATE_CONFIRMATION_REQUIRED",
            "审批模板选项配置已经失效，请管理员重新检查并确认",
            409,
        )
    return component.options


def _validate_window(query_window: TravelApprovalQueryWindow) -> None:
    expected = TravelApprovalQueryWindow.from_dates(
        query_window.from_date,
        query_window.to_date,
    )
    if query_window != expected:
        raise ApiError(
            "TRAVEL_APPROVAL_QUERY_WINDOW_INVALID",
            "出差审批查询时间范围无效",
            422,
        )


def _matches_query(candidate: TravelApprovalCandidate, query: str) -> bool:
    normalized = query.strip().casefold()
    if not normalized:
        return True
    return normalized in candidate.instance.title.casefold() or normalized in (
        candidate.instance.business_id.casefold()
    )


def _required_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _result_limit_error() -> ApiError:
    return ApiError(
        "TRAVEL_APPROVAL_RESULT_LIMIT_EXCEEDED",
        "出差审批数量过多，请缩小查询日期范围",
        422,
    )
