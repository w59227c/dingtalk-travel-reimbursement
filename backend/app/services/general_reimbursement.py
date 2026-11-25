from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.core.security import token_hash, tokens_match
from app.domain.categories import EXPENSE_CATEGORIES, ExpenseCategory
from app.excel.template_contract import expense_category_list_formula
from app.integrations.dingtalk.client import DingTalkOpenAPIError
from app.integrations.dingtalk.workflow import (
    DingTalkWorkflowClient,
    FormOption,
    FormSchema,
    WorkflowApprovalSummary,
    form_schema_from_dict,
)
from app.models.oa_template_profile import OaTemplateProfile, utc_now
from app.services.oa_template_compatibility import SchemaChangeKind, classify_schema_change
from app.services.oa_template_profiles import (
    LogicalFieldSpec,
    OaTemplateCatalogContract,
    ReimbursementTemplateContract,
)

PROFILE_KEY = "general_reimbursement"
GENERAL_FIELD_SPECS = (
    LogicalFieldSpec("company", "报销主体", frozenset({"DDSelectField"})),
    LogicalFieldSpec("budgetCode", "预算代码", frozenset({"DDSelectField"})),
    LogicalFieldSpec("subjects", "费用科目", frozenset({"DDMultiSelectField"})),
    LogicalFieldSpec("description", "明细说明", frozenset({"TextareaField"})),
    LogicalFieldSpec("reimbursementAmount", "金额（元）", frozenset({"MoneyField"})),
    LogicalFieldSpec("relatedApprovals", "关联审批单", frozenset({"RelateField"}), required=False),
    LogicalFieldSpec("attachments", "附件", frozenset({"DDAttachment"})),
)
_CATEGORIES = {
    **{category.name: category.id for category in EXPENSE_CATEGORIES if category.manual_selectable},
    "交通费": ExpenseCategory.LOCAL_TRANSPORT,
    "快递费": ExpenseCategory.EXPRESS,
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


def subject_category(label: str) -> ExpenseCategory:
    return _CATEGORIES.get(label, ExpenseCategory.OTHER)


def require_current_subject_categories(
    options: Sequence[FormOption],
    frozen_items: Iterable[tuple[str | None, ExpenseCategory]],
) -> None:
    categories = {option.value: subject_category(option.label) for option in options}
    for index, (value, category) in enumerate(frozen_items, 1):
        if value is None or categories.get(value) != category:
            raise ApiError(
                "REIMBURSEMENT_SUBJECT_CATEGORY_CHANGED",
                f"第 {index} 笔费用的科目类别已更新，请重新核对科目和所需材料",
                409,
            )


def general_options(catalog: OaTemplateCatalogContract, key: str):
    binding = catalog.reimbursement.mappings[key]
    return next(
        c.options for c in catalog.reimbursement.schema.components if c.component_id == binding
    )


def schema_bindings(schema: FormSchema) -> dict[str, str]:
    if schema.status != "PUBLISHED":
        raise ApiError("OA_GENERAL_TEMPLATE_INVALID", "请选择已发布的普通费用审批模板", 409)
    bindings = {}
    active = tuple(
        c
        for c in schema.components
        if not c.hidden and not c.disabled and not c.ancestor_hidden and not c.ancestor_disabled
    )
    for spec in GENERAL_FIELD_SPECS:
        matches = [
            c
            for c in active
            if c.label == spec.label
            and not c.nested
            and c.component_type in spec.supported_component_types
            and c.required == spec.required
        ]
        if len(matches) != 1:
            raise ApiError(
                "OA_GENERAL_TEMPLATE_INVALID", f"请核对费用报销模板的「{spec.label}」控件", 409
            )
        component = matches[0]
        if (
            spec.key == "relatedApprovals"
            and component.related_template_policy is not None
            and component.related_template_policy.mode == "RESTRICTED"
        ):
            raise ApiError(
                "OA_GENERAL_TEMPLATE_INVALID", "请将「关联审批单」设为允许关联所有审批模板", 409
            )
        if spec.key in {"company", "budgetCode", "subjects"} and (
            not component.options
            or len({option.value for option in component.options}) != len(component.options)
        ):
            raise ApiError("OA_GENERAL_TEMPLATE_INVALID", f"「{spec.label}」选项为空或重复", 409)
        if spec.key == "subjects" and not {"交通费", "快递费", "住宿费"}.issubset(
            {option.label for option in component.options}
        ):
            raise ApiError(
                "OA_GENERAL_TEMPLATE_INVALID", "费用科目需包含交通费、快递费和住宿费", 409
            )
        if spec.key == "subjects":
            labels = tuple(option.label for option in component.options)
            try:
                if len(labels) != len(set(labels)):
                    raise ValueError("duplicate labels")
                expense_category_list_formula(labels)
            except ValueError:
                raise ApiError(
                    "OA_GENERAL_TEMPLATE_INVALID", "费用科目选项无法用于 Excel 下拉列表", 409
                ) from None
        bindings[spec.key] = component.component_id
    if any(c.required and c.component_id not in bindings.values() for c in active):
        raise ApiError("OA_GENERAL_TEMPLATE_INVALID", "普通费用审批模板有未映射的必填字段", 409)
    return bindings


def _contract(schema: FormSchema, version: int) -> OaTemplateCatalogContract:
    return OaTemplateCatalogContract(
        config_version=version,
        reimbursement=ReimbursementTemplateContract(
            schema.process_code,
            schema,
            schema_bindings(schema),
        ),
        travel_profiles=(),
        is_general=True,
    )


def get_general_catalog(database: Session) -> OaTemplateCatalogContract:
    profile = database.get(OaTemplateProfile, PROFILE_KEY, populate_existing=True)
    if profile is None:
        raise ApiError(
            "OA_GENERAL_TEMPLATE_NOT_CONFIGURED", "请在系统设置中配置普通费用 OA 模板", 409
        )
    if profile.compatibility_status != "COMPATIBLE":
        raise ApiError(
            "OA_GENERAL_TEMPLATE_CHANGED", "普通费用 OA 模板已变化，请管理员重新确认", 409
        )
    schema = form_schema_from_dict(json.loads(profile.schema_json))
    catalog = _contract(schema, profile.config_version)
    if (
        schema.fingerprint != profile.schema_fingerprint
        or schema.process_code != profile.process_code
        or catalog.reimbursement.mappings != json.loads(profile.mapping_json)
    ):
        raise ApiError("OA_GENERAL_TEMPLATE_INVALID", "普通费用模板配置无效，请管理员重新确认", 409)
    return catalog


def confirm_general_catalog(
    database: Session,
    *,
    schema: FormSchema,
    expected_version: int,
    administrator: str,
) -> OaTemplateProfile:
    bindings = schema_bindings(schema)
    database.rollback()
    database.connection().exec_driver_sql("BEGIN IMMEDIATE")
    profile = database.get(OaTemplateProfile, PROFILE_KEY, populate_existing=True)
    if (profile.config_version if profile else 0) != expected_version:
        raise ApiError("OA_GENERAL_TEMPLATE_CHANGED", "模板配置已更新，请刷新后重试", 409)
    now = utc_now()
    version = expected_version + 1
    if profile is None:
        profile = OaTemplateProfile(profile_key=PROFILE_KEY)
        database.add(profile)
    else:
        previous = form_schema_from_dict(json.loads(profile.schema_json))
        if (
            profile.process_code == schema.process_code
            and bindings == json.loads(profile.mapping_json)
            and classify_schema_change(
                previous, schema, mapped_component_ids=set(bindings.values())
            )
            != SchemaChangeKind.INCOMPATIBLE
        ):
            version = expected_version
    profile.process_code = schema.process_code
    profile.template_name = schema.template_name
    profile.config_version = version
    profile.schema_fingerprint = schema.fingerprint
    profile.confirmed_schema_fingerprint = schema.fingerprint
    profile.schema_json = json.dumps(schema.as_dict(), ensure_ascii=False)
    profile.mapping_json = json.dumps(bindings)
    profile.allowed_travel_process_codes_json = "[]"
    profile.travel_profiles_json = "[]"
    profile.compatibility_status = "COMPATIBLE"
    profile.confirmed_by_user_id = administrator
    profile.confirmed_at = now
    profile.last_checked_at = now
    database.commit()
    return profile


async def refresh_general_catalog(
    factory: sessionmaker[Session],
    workflow: DingTalkWorkflowClient,
) -> OaTemplateCatalogContract:
    with factory() as database:
        catalog = get_general_catalog(database)
    template = catalog.reimbursement
    live = await workflow.get_form_schema(template.process_code)
    try:
        compatible = (
            classify_schema_change(
                template.schema,
                live,
                mapped_component_ids=set(template.mappings.values()),
            )
            != SchemaChangeKind.INCOMPATIBLE
            and schema_bindings(live) == template.mappings
        )
    except ApiError:
        compatible = False
    with factory() as database:
        condition = (
            OaTemplateProfile.profile_key == PROFILE_KEY,
            OaTemplateProfile.config_version == catalog.config_version,
            OaTemplateProfile.schema_fingerprint == template.schema.fingerprint,
            OaTemplateProfile.compatibility_status == "COMPATIBLE",
        )
        updated = database.execute(
            update(OaTemplateProfile)
            .where(*condition)
            .values(
                **(
                    {
                        "schema_fingerprint": live.fingerprint,
                        "schema_json": json.dumps(live.as_dict(), ensure_ascii=False),
                    }
                    if compatible
                    else {"compatibility_status": "DRIFTED"}
                ),
                last_checked_at=utc_now(),
            )
        )
        database.commit()
        if not compatible:
            raise ApiError("OA_GENERAL_TEMPLATE_CHANGED", "普通费用 OA 模板已变化，请重新检查", 409)
        current = get_general_catalog(database)
        # Another request can apply the same compatible change while this request
        # reads the remote schema. Accept the identical result without writing again.
        if updated.rowcount != 1 and (
            current.config_version != catalog.config_version
            or current.reimbursement.process_code != template.process_code
            or current.reimbursement.schema.fingerprint != live.fingerprint
        ):
            raise ApiError("OA_GENERAL_TEMPLATE_CHANGED", "普通费用 OA 模板已变化，请重新检查", 409)
        return current


async def verify_general_approvals(
    workflow: DingTalkWorkflowClient,
    *,
    user_id: str,
    instance_ids: list[str],
) -> tuple[WorkflowApprovalSummary, ...]:
    approved = []
    for instance_id in instance_ids:
        instance = await workflow.get_process_instance_summary(instance_id)
        if instance.instance_id != instance_id or instance.originator_user_id != user_id:
            raise ApiError("OA_GENERAL_APPROVAL_NOT_OWNED", "只能关联本人发起的审批", 422)
        if not _is_approved(instance):
            raise ApiError(
                "OA_GENERAL_APPROVAL_NOT_APPROVED",
                "只能关联已通过的审批，请移除未通过的关联后重试",
                422,
            )
        approved.append(instance)
    return tuple(approved)


def _is_approved(instance: WorkflowApprovalSummary) -> bool:
    return instance.status == "COMPLETED" and instance.result == "agree"


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
    known_codes: tuple[str, ...] = (),
    known_names: dict[str, str] | None = None,
    process_code: str = "",
) -> dict[str, object]:
    try:
        visible, directory_complete = await workflow.list_visible_templates(user_id)
    except DingTalkOpenAPIError:
        visible, directory_complete = (), False
    names = dict(known_names or {})
    names.update({item.process_code: item.name for item in visible if item.name})
    available_codes = tuple(sorted(set((*(item.process_code for item in visible), *known_codes))))
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
                "general-approved-approval-search-v4",
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
    ids: dict[str, None] = {}
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
                statuses=("COMPLETED",),
            )
        except ApiError:
            partial = True
            incomplete_reasons.add("list")
        else:
            ids.update(dict.fromkeys(batch.instance_ids))
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
    matches = [
        item
        for item in details
        if item is not None
        and item.originator_user_id == user_id
        and _is_approved(item)
        and (
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
    has_more = bool(codes) and window_index < len(windows)
    return {
        "templates": [
            {"processCode": code, "name": names.get(code) or "未命名审批单"}
            for code in available_codes
        ],
        "items": [approval_summary(item) for item in matches],
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
