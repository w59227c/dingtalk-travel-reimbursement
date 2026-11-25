from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.domain.categories import EXPENSE_CATEGORIES, ExpenseCategory
from app.excel.template_contract import expense_category_list_formula
from app.integrations.dingtalk.workflow import (
    DingTalkWorkflowClient,
    FormOption,
    FormSchema,
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
