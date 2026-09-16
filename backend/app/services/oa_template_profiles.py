from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import (
    DingTalkWorkflowClient,
    FormComponent,
    FormOption,
    FormSchema,
    form_schema_from_dict,
)
from app.models.oa_template_profile import OaTemplateProfile, utc_now
from app.services.oa_template_compatibility import (
    SchemaChangeKind,
    classify_schema_change,
)

REIMBURSEMENT_PROFILE_KEY: Final = "reimbursement"

COMPATIBLE = "COMPATIBLE"
DRIFTED = "DRIFTED"
UNCONFIGURED = "UNCONFIGURED"
CATALOG_CHANGED = "CATALOG_CHANGED"

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PROFILE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_CONFIG_CAS_ATTEMPTS = 3
_MAX_TRAVEL_PROFILES = 20
_TRAVEL_TABLE_COMPONENT_TYPES: Final = frozenset({"DDTableField", "TableField"})
_TRAVEL_PROFILE_JSON_KEYS = frozenset(
    {
        "profileKey",
        "displayName",
        "processCode",
        "schemaFingerprint",
        "confirmedSchemaFingerprint",
        "schema",
        "mappings",
        "travelTypeOption",
    }
)


@dataclass(frozen=True, slots=True)
class LogicalFieldSpec:
    key: str
    label: str
    supported_component_types: frozenset[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "supportedComponentTypes": sorted(self.supported_component_types),
        }


@dataclass(frozen=True, slots=True)
class ReimbursementTemplateContract:
    process_code: str
    schema: FormSchema
    mappings: dict[str, str]
    confirmed_schema_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class TravelTemplateContract:
    profile_key: str
    display_name: str
    process_code: str
    schema: FormSchema
    start_date_component_id: str
    end_date_component_id: str
    travel_type_option: FormOption
    company_component_id: str | None = None
    budget_code_component_id: str | None = None
    travel_type_component_id: str | None = None
    travel_type_mappings: dict[str, FormOption] | None = None
    confirmed_schema_fingerprint: str | None = None

    @property
    def mappings(self) -> dict[str, str]:
        mappings = {
            "startDate": self.start_date_component_id,
            "endDate": self.end_date_component_id,
        }
        if self.company_component_id:
            mappings["company"] = self.company_component_id
        if self.budget_code_component_id:
            mappings["budgetCode"] = self.budget_code_component_id
        if self.travel_type_component_id:
            mappings["travelType"] = self.travel_type_component_id
        return mappings


@dataclass(frozen=True, slots=True)
class OaTemplateCatalogContract:
    config_version: int
    reimbursement: ReimbursementTemplateContract
    travel_profiles: tuple[TravelTemplateContract, ...]

    @property
    def allowed_travel_process_codes(self) -> tuple[str, ...]:
        return tuple(profile.process_code for profile in self.travel_profiles)

    def travel_profile(self, profile_key: str) -> TravelTemplateContract:
        for profile in self.travel_profiles:
            if profile.profile_key == profile_key:
                return profile
        raise ApiError(
            "TRAVEL_APPROVAL_TEMPLATE_NOT_ALLOWED",
            "所选出差审批不属于允许关联的出差模板",
            400,
        )


@dataclass(frozen=True, slots=True)
class _CatalogCasState:
    process_code: str
    config_version: int
    schema_fingerprint: str
    travel_profiles_json: str
    compatibility_status: str


@dataclass(frozen=True, slots=True)
class TravelProfileInspection:
    profile_key: str
    display_name: str
    process_code: str


@dataclass(frozen=True, slots=True)
class TravelProfileConfirmation:
    profile_key: str
    display_name: str
    process_code: str
    schema_fingerprint: str
    mappings: dict[str, str]
    travel_type_option: FormOption
    travel_type_mappings: dict[str, FormOption] | None = None


REIMBURSEMENT_LOGICAL_FIELD_SPECS: Final[tuple[LogicalFieldSpec, ...]] = (
    LogicalFieldSpec("company", "所属公司", frozenset({"DDSelectField"})),
    LogicalFieldSpec("budgetCode", "预算代码", frozenset({"DDSelectField"})),
    LogicalFieldSpec("travelType", "出差类别", frozenset({"DDSelectField"})),
    LogicalFieldSpec("startDate", "开始日期", frozenset({"DDDateField", "DDDateRangeField"})),
    LogicalFieldSpec("endDate", "结束日期", frozenset({"DDDateField", "DDDateRangeField"})),
    LogicalFieldSpec("durationDays", "时长（天）", frozenset({"NumberField", "DDDateRangeField"})),
    LogicalFieldSpec(
        "description",
        "明细说明",
        frozenset({"TextField", "TextareaField"}),
    ),
    LogicalFieldSpec(
        "reimbursementAmount",
        "报销金额",
        frozenset({"MoneyField", "NumberField"}),
    ),
    LogicalFieldSpec("relatedApprovals", "关联审批单", frozenset({"RelateField"})),
    LogicalFieldSpec("attachments", "附件", frozenset({"DDAttachment"})),
)
TRAVEL_LOGICAL_FIELD_SPECS: Final[tuple[LogicalFieldSpec, ...]] = (
    LogicalFieldSpec(
        "startDate",
        "出差开始日期",
        frozenset({"DDDateField", "DDDateRangeField", *_TRAVEL_TABLE_COMPONENT_TYPES}),
    ),
    LogicalFieldSpec(
        "endDate",
        "出差结束日期",
        frozenset({"DDDateField", "DDDateRangeField", *_TRAVEL_TABLE_COMPONENT_TYPES}),
    ),
    LogicalFieldSpec("company", "所属公司", frozenset({"DDSelectField", "TextField"})),
    LogicalFieldSpec("budgetCode", "预算代码", frozenset({"DDSelectField", "TextField"})),
    LogicalFieldSpec(
        "travelType", "出差类别来源（按选项对应时使用）", frozenset({"DDSelectField"})
    ),
)
LOGICAL_FIELD_SPECS = REIMBURSEMENT_LOGICAL_FIELD_SPECS


async def inspect_template_catalog(
    database: Session,
    workflow: DingTalkWorkflowClient,
    *,
    reimbursement_process_code: str,
    travel_profiles: list[TravelProfileInspection],
) -> dict[str, object]:
    normalized_profiles = _validate_profile_headers(
        reimbursement_process_code,
        travel_profiles,
    )
    reimbursement_schema = await workflow.get_form_schema(reimbursement_process_code)
    travel_schemas = [
        await workflow.get_form_schema(profile.process_code) for profile in normalized_profiles
    ]

    profile = _fresh_profile(database)
    configured_travel_profiles = _display_travel_profiles(profile) if profile is not None else []
    same_catalog = bool(
        profile is not None
        and profile.process_code == reimbursement_process_code
        and [
            (item.get("profileKey"), item.get("processCode")) for item in configured_travel_profiles
        ]
        == [(item.profile_key, item.process_code) for item in normalized_profiles]
    )
    refreshed: OaTemplateCatalogContract | None = None
    if same_catalog and profile is not None:
        try:
            configured_contract = _validated_persisted_catalog(
                profile,
                require_compatible_status=False,
            )
        except ApiError:
            configured_contract = None
        if configured_contract is not None:
            refreshed = _compatible_refreshed_catalog(
                configured_contract,
                reimbursement_schema=reimbursement_schema,
                travel_schemas=travel_schemas,
            )
    compatible = refreshed is not None
    status = (
        UNCONFIGURED
        if profile is None
        else COMPATIBLE
        if compatible
        else CATALOG_CHANGED
        if not same_catalog
        else DRIFTED
    )
    if same_catalog and profile is not None:
        expected_state = _catalog_cas_state(profile)
        expected_config_version = profile.config_version
        updated = (
            _replace_compatible_catalog(
                database,
                expected_state=expected_state,
                refreshed=refreshed,
            )
            if configured_contract is not None and refreshed is not None
            else _record_catalog_status(
                database,
                expected=expected_state,
                compatibility_status=DRIFTED,
            )
        )
        if not updated:
            raise _configuration_changed_error()
        profile = _fresh_profile(database)
        if (
            profile is None
            or profile.config_version != expected_config_version
            or profile.process_code != reimbursement_process_code
        ):
            raise _configuration_changed_error()

    stored_by_key = {str(item.get("profileKey")): item for item in configured_travel_profiles}
    travel_data: list[dict[str, object]] = []
    for requested, schema in zip(normalized_profiles, travel_schemas, strict=True):
        stored = stored_by_key.get(requested.profile_key)
        travel_data.append(
            {
                "profileKey": requested.profile_key,
                "displayName": requested.display_name,
                "processCode": requested.process_code,
                "schema": _schema_api_data(schema, TRAVEL_LOGICAL_FIELD_SPECS),
                "logicalFields": [item.as_dict() for item in TRAVEL_LOGICAL_FIELD_SPECS],
                "mappings": (
                    _display_mapping_value(stored.get("mappings"))
                    if stored is not None and stored.get("processCode") == requested.process_code
                    else None
                ),
                "travelTypeOption": (
                    _display_option_value(stored.get("travelTypeOption"))
                    if stored is not None and stored.get("processCode") == requested.process_code
                    else None
                ),
                **(
                    {"travelTypeMappings": stored["travelTypeMappings"]}
                    if stored is not None
                    and stored.get("processCode") == requested.process_code
                    and "travelTypeMappings" in stored
                    else {}
                ),
                "confirmedSchemaFingerprint": (
                    stored.get("confirmedSchemaFingerprint")
                    if stored is not None and stored.get("processCode") == requested.process_code
                    else None
                ),
            }
        )

    is_ready = bool(profile is not None and compatible and _profile_submission_ready(profile))
    return {
        "configured": profile is not None,
        "compatibilityStatus": status,
        "requiresConfirmation": not is_ready,
        "isSubmissionReady": is_ready,
        "configuredConfigVersion": profile.config_version if profile is not None else None,
        "reimbursement": {
            "processCode": reimbursement_process_code,
            "schema": _schema_api_data(
                reimbursement_schema,
                REIMBURSEMENT_LOGICAL_FIELD_SPECS,
            ),
            "logicalFields": [item.as_dict() for item in REIMBURSEMENT_LOGICAL_FIELD_SPECS],
            "mappings": (
                _display_mapping(profile.mapping_json)
                if profile is not None and profile.process_code == reimbursement_process_code
                else None
            ),
            "confirmedSchemaFingerprint": (
                profile.confirmed_schema_fingerprint
                if profile is not None and profile.process_code == reimbursement_process_code
                else None
            ),
        },
        "travelProfiles": travel_data,
    }


async def confirm_template_catalog(
    database: Session,
    workflow: DingTalkWorkflowClient,
    *,
    reimbursement_process_code: str,
    expected_config_version: int | None,
    reimbursement_schema_fingerprint: str,
    reimbursement_mappings: dict[str, str],
    travel_profiles: list[TravelProfileConfirmation],
    administrator_user_id: str,
) -> dict[str, object]:
    if not _FINGERPRINT_PATTERN.fullmatch(reimbursement_schema_fingerprint):
        raise _mapping_error("报销模板 Schema 指纹格式无效")
    normalized_headers = _validate_profile_headers(
        reimbursement_process_code,
        [
            TravelProfileInspection(
                profile_key=item.profile_key,
                display_name=item.display_name,
                process_code=item.process_code,
            )
            for item in travel_profiles
        ],
    )
    confirmation_by_key = {item.profile_key.strip(): item for item in travel_profiles}

    # A partial remote success can never leave a partially confirmed local catalog.
    reimbursement_schema = await workflow.get_form_schema(reimbursement_process_code)
    travel_schemas = [
        await workflow.get_form_schema(profile.process_code) for profile in normalized_headers
    ]

    if reimbursement_schema.fingerprint != reimbursement_schema_fingerprint:
        raise _schema_changed_error()
    normalized_reimbursement_mapping = validate_template_mapping(
        reimbursement_schema,
        reimbursement_mappings,
    )

    contracts: list[TravelTemplateContract] = []
    for header, schema in zip(normalized_headers, travel_schemas, strict=True):
        confirmation = confirmation_by_key[header.profile_key]
        if not _FINGERPRINT_PATTERN.fullmatch(confirmation.schema_fingerprint):
            raise _mapping_error(f"{header.display_name}的 Schema 指纹格式无效")
        if schema.fingerprint != confirmation.schema_fingerprint:
            raise _schema_changed_error()
        mapping = validate_travel_template_mapping(schema, confirmation.mappings)
        exact_option = validate_exact_travel_type_option(
            reimbursement_schema,
            normalized_reimbursement_mapping,
            confirmation.travel_type_option,
        )
        contracts.append(
            TravelTemplateContract(
                profile_key=header.profile_key,
                display_name=header.display_name,
                process_code=header.process_code,
                schema=schema,
                start_date_component_id=mapping["startDate"],
                end_date_component_id=mapping["endDate"],
                travel_type_option=exact_option,
                company_component_id=mapping.get("company"),
                budget_code_component_id=mapping.get("budgetCode"),
                travel_type_component_id=mapping.get("travelType"),
                travel_type_mappings=validate_travel_type_mappings(
                    schema,
                    mapping,
                    confirmation.travel_type_mappings,
                    reimbursement_schema,
                    normalized_reimbursement_mapping,
                ),
            )
        )

    allowed_process_codes = tuple(item.process_code for item in contracts)
    validate_related_approval_configuration(
        reimbursement_schema,
        normalized_reimbursement_mapping,
        reimbursement_process_code=reimbursement_process_code,
        allowed_travel_process_codes=list(allowed_process_codes),
    )

    now = utc_now()
    values = {
        "process_code": reimbursement_process_code,
        "template_name": reimbursement_schema.template_name,
        "schema_fingerprint": reimbursement_schema.fingerprint,
        "confirmed_schema_fingerprint": reimbursement_schema.fingerprint,
        "schema_json": _serialize_schema(reimbursement_schema),
        "mapping_json": _serialize_mapping(normalized_reimbursement_mapping),
        "allowed_travel_process_codes_json": _serialize_process_codes(allowed_process_codes),
        "travel_profiles_json": _serialize_travel_profiles(contracts),
        "compatibility_status": COMPATIBLE,
        "confirmed_by_user_id": administrator_user_id,
        "last_checked_at": now,
        "confirmed_at": now,
        "updated_at": now,
    }

    profile = _fresh_profile(database)
    if expected_config_version is None:
        if profile is not None:
            raise _configuration_changed_error()
        database.add(
            OaTemplateProfile(
                profile_key=REIMBURSEMENT_PROFILE_KEY,
                config_version=1,
                created_at=now,
                **values,
            )
        )
        try:
            database.commit()
        except IntegrityError:
            database.rollback()
            raise _configuration_changed_error() from None
    else:
        if profile is None or profile.config_version != expected_config_version:
            raise _configuration_changed_error()
        result = database.execute(
            update(OaTemplateProfile)
            .where(
                OaTemplateProfile.profile_key == REIMBURSEMENT_PROFILE_KEY,
                OaTemplateProfile.config_version == expected_config_version,
            )
            .values(config_version=expected_config_version + 1, **values),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount != 1:
            database.rollback()
            raise _configuration_changed_error()
        database.commit()

    saved = _fresh_profile(database)
    if saved is None:
        raise _configuration_changed_error()
    return template_catalog_data(saved)


def current_template_catalog(database: Session) -> dict[str, object]:
    profile = _fresh_profile(database)
    if profile is None:
        return {
            "configured": False,
            "configVersion": None,
            "compatibilityStatus": UNCONFIGURED,
            "isSubmissionReady": False,
            "requiresConfirmation": True,
            "catalog": None,
        }
    try:
        data = template_catalog_data(profile)
    except ApiError:
        return {
            "configured": True,
            "configVersion": profile.config_version,
            "compatibilityStatus": profile.compatibility_status,
            "isSubmissionReady": False,
            "requiresConfirmation": True,
            "catalog": None,
        }
    return {
        "configured": True,
        "configVersion": profile.config_version,
        "compatibilityStatus": profile.compatibility_status,
        "isSubmissionReady": bool(data["isSubmissionReady"]),
        "requiresConfirmation": not bool(data["isSubmissionReady"]),
        "catalog": data,
    }


def require_submission_ready_catalog(database: Session) -> OaTemplateCatalogContract:
    profile = _fresh_profile(database)
    if profile is None:
        raise ApiError(
            "OA_TEMPLATE_NOT_CONFIGURED",
            "报销审批模板目录尚未配置，请联系管理员",
            409,
        )
    if profile.compatibility_status != COMPATIBLE:
        raise _confirmation_required_error()
    try:
        return _validated_persisted_catalog(profile)
    except ApiError:
        raise _confirmation_required_error() from None


async def load_fresh_submission_catalog(
    database_session_factory: sessionmaker[Session],
    workflow: DingTalkWorkflowClient,
) -> OaTemplateCatalogContract:
    for _attempt in range(_MAX_CONFIG_CAS_ATTEMPTS):
        with database_session_factory() as snapshot_database:
            profile = _fresh_profile(snapshot_database)
            if profile is None:
                raise ApiError(
                    "OA_TEMPLATE_NOT_CONFIGURED",
                    "报销审批模板目录尚未配置，请联系管理员",
                    409,
                )
            try:
                contract = _validated_persisted_catalog(
                    profile,
                    require_compatible_status=False,
                )
            except ApiError:
                raise _confirmation_required_error() from None
            config_version = contract.config_version
            reimbursement_process_code = contract.reimbursement.process_code
            expected_state = _catalog_cas_state(profile)
            travel_process_codes = tuple(
                profile.process_code for profile in contract.travel_profiles
            )

        reimbursement_schema = await workflow.get_form_schema(reimbursement_process_code)
        travel_schemas = [
            await workflow.get_form_schema(process_code) for process_code in travel_process_codes
        ]
        refreshed = _compatible_refreshed_catalog(
            contract,
            reimbursement_schema=reimbursement_schema,
            travel_schemas=travel_schemas,
        )
        compatible = refreshed is not None
        with database_session_factory() as observation_database:
            observed = (
                _replace_compatible_catalog(
                    observation_database,
                    expected_state=expected_state,
                    refreshed=refreshed,
                )
                if refreshed is not None
                else _record_catalog_status(
                    observation_database,
                    expected=expected_state,
                    compatibility_status=DRIFTED,
                )
            )
        if not observed:
            continue
        if not compatible:
            raise _confirmation_required_error()

        with database_session_factory() as final_database:
            current = require_submission_ready_catalog(final_database)
            if (
                current.config_version == config_version
                and current.reimbursement.process_code == reimbursement_process_code
                and tuple(item.process_code for item in current.travel_profiles)
                == travel_process_codes
            ):
                return current
    raise _configuration_changed_error()


def _compatible_refreshed_catalog(
    configured: OaTemplateCatalogContract,
    *,
    reimbursement_schema: FormSchema,
    travel_schemas: list[FormSchema],
) -> OaTemplateCatalogContract | None:
    if len(travel_schemas) != len(configured.travel_profiles):
        return None
    reimbursement = configured.reimbursement
    if (
        classify_schema_change(
            reimbursement.schema,
            reimbursement_schema,
            mapped_component_ids=set(reimbursement.mappings.values()),
        )
        is SchemaChangeKind.INCOMPATIBLE
    ):
        return None
    try:
        reimbursement_mapping = validate_template_mapping(
            reimbursement_schema,
            reimbursement.mappings,
        )
        refreshed_profiles = tuple(
            _refresh_travel_contract(
                profile,
                observed,
                reimbursement_schema=reimbursement_schema,
                reimbursement_mapping=reimbursement_mapping,
            )
            for profile, observed in zip(
                configured.travel_profiles,
                travel_schemas,
                strict=True,
            )
        )
        validate_related_approval_configuration(
            reimbursement_schema,
            reimbursement_mapping,
            reimbursement_process_code=reimbursement.process_code,
            allowed_travel_process_codes=[item.process_code for item in refreshed_profiles],
        )
    except ApiError:
        return None
    return OaTemplateCatalogContract(
        config_version=configured.config_version,
        reimbursement=ReimbursementTemplateContract(
            process_code=reimbursement.process_code,
            schema=reimbursement_schema,
            mappings=reimbursement_mapping,
            confirmed_schema_fingerprint=(
                reimbursement.confirmed_schema_fingerprint or reimbursement.schema.fingerprint
            ),
        ),
        travel_profiles=refreshed_profiles,
    )


def _refresh_travel_contract(
    configured: TravelTemplateContract,
    observed: FormSchema,
    *,
    reimbursement_schema: FormSchema,
    reimbursement_mapping: dict[str, str],
) -> TravelTemplateContract:
    if (
        classify_schema_change(
            configured.schema,
            observed,
            mapped_component_ids=set(configured.mappings.values()),
        )
        is SchemaChangeKind.INCOMPATIBLE
    ):
        raise _mapping_error(f"{configured.display_name}的模板结构已变化")
    mapping = validate_travel_template_mapping(observed, configured.mappings)
    travel_type_option = _option_with_current_display(
        reimbursement_schema,
        reimbursement_mapping["travelType"],
        configured.travel_type_option.value,
    )
    travel_type_mappings: dict[str, FormOption] | None = None
    if configured.travel_type_mappings is not None:
        source_component_id = mapping.get("travelType")
        source_component = next(
            (
                item
                for item in observed.components
                if item.component_id == source_component_id
            ),
            None,
        )
        if source_component is None or {
            item.value for item in source_component.options
        } != set(configured.travel_type_mappings):
            raise _mapping_error(f"{configured.display_name}的出差类别来源选项已变化")
        travel_type_mappings = {
            source_value: _option_with_current_display(
                reimbursement_schema,
                reimbursement_mapping["travelType"],
                target.value,
            )
            for source_value, target in configured.travel_type_mappings.items()
        }
    return TravelTemplateContract(
        profile_key=configured.profile_key,
        display_name=configured.display_name,
        process_code=configured.process_code,
        schema=observed,
        start_date_component_id=mapping["startDate"],
        end_date_component_id=mapping["endDate"],
        travel_type_option=travel_type_option,
        company_component_id=mapping.get("company"),
        budget_code_component_id=mapping.get("budgetCode"),
        travel_type_component_id=mapping.get("travelType"),
        travel_type_mappings=travel_type_mappings,
        confirmed_schema_fingerprint=(
            configured.confirmed_schema_fingerprint or configured.schema.fingerprint
        ),
    )


def _option_with_current_display(
    schema: FormSchema,
    component_id: str,
    value: str,
) -> FormOption:
    component = next(
        (item for item in schema.components if item.component_id == component_id),
        None,
    )
    matches = [item for item in component.options if item.value == value] if component else []
    if len(matches) != 1:
        raise _mapping_error("已配置的出差类别选项已失效，请重新确认")
    return matches[0]


async def load_fresh_submission_template(
    database_session_factory: sessionmaker[Session],
    workflow: DingTalkWorkflowClient,
) -> OaTemplateCatalogContract:
    """Compatibility name for callers that predate the aggregate catalog."""

    return await load_fresh_submission_catalog(database_session_factory, workflow)


def validate_template_mapping(schema: FormSchema, mappings: dict[str, str]) -> dict[str, str]:
    normalized = _validate_mapping(
        schema,
        mappings,
        specs=REIMBURSEMENT_LOGICAL_FIELD_SPECS,
        reject_unmapped_required=True,
        reusable_component_types=frozenset({"DDDateRangeField"}),
    )
    components = {component.component_id: component for component in schema.components}
    date_ids = [normalized[key] for key in ("startDate", "endDate", "durationDays")]
    if any(components[key].component_type == "DDDateRangeField" for key in date_ids):
        if len(set(date_ids)) != 1:
            raise _mapping_error("开始日期、结束日期和时长必须映射到同一个日期区间控件")
    return normalized


def validate_travel_template_mapping(
    schema: FormSchema,
    mappings: dict[str, str],
) -> dict[str, str]:
    # Older confirmed catalogs remain readable. Missing new source mappings are
    # unavailable on candidates until an administrator confirms them.
    supplied_specs = tuple(
        spec
        for spec in TRAVEL_LOGICAL_FIELD_SPECS
        if spec.key in {"startDate", "endDate"} or spec.key in mappings
    )
    normalized = _validate_mapping(
        schema,
        mappings,
        specs=supplied_specs,
        reject_unmapped_required=False,
        reusable_component_types=_TRAVEL_TABLE_COMPONENT_TYPES | {"DDDateRangeField"},
    )
    component_by_id = {component.component_id: component for component in schema.components}
    uses_table = any(
        component_by_id[component_id].component_type
        in _TRAVEL_TABLE_COMPONENT_TYPES | {"DDDateRangeField"}
        for component_id in (normalized["startDate"], normalized["endDate"])
    )
    if uses_table and normalized["startDate"] != normalized["endDate"]:
        raise _mapping_error("出差开始日期和结束日期必须映射到同一个表格控件或同一个日期区间控件")
    return normalized


def _validate_mapping(
    schema: FormSchema,
    mappings: dict[str, str],
    *,
    specs: tuple[LogicalFieldSpec, ...],
    reject_unmapped_required: bool,
    reusable_component_types: frozenset[str] = frozenset(),
) -> dict[str, str]:
    field_by_key = {item.key: item for item in specs}
    supplied_keys = set(mappings)
    required_keys = set(field_by_key)
    if supplied_keys != required_keys:
        missing = sorted(required_keys - supplied_keys)
        unknown = sorted(supplied_keys - required_keys)
        if missing:
            raise _mapping_error(f"缺少字段对应关系：{', '.join(missing)}")
        raise _mapping_error(f"包含未知系统字段：{', '.join(unknown)}")

    normalized: dict[str, str] = {}
    used_component_ids: set[str] = set()
    component_by_id = {component.component_id: component for component in schema.components}
    for spec in specs:
        raw_component_id = mappings[spec.key]
        component_id = raw_component_id.strip() if isinstance(raw_component_id, str) else ""
        if not component_id:
            raise _mapping_error(f"{spec.label}尚未选择 OA 控件")
        component = component_by_id.get(component_id)
        if component is None:
            raise _mapping_error(f"{spec.label}对应的 OA 控件不存在")
        if (
            component_id in used_component_ids
            and component.component_type not in reusable_component_types
        ):
            raise _mapping_error("同一个 OA 控件不能对应多个系统字段")
        incompatibility = _component_incompatibility(component, spec)
        if incompatibility is not None:
            raise _mapping_error(f"{spec.label}{incompatibility}")
        used_component_ids.add(component_id)
        normalized[spec.key] = component_id

    if reject_unmapped_required:
        unsupported_required = [
            component.label
            for component in schema.components
            if not component.in_subtable
            and component.required
            and not component.disabled
            and not component.hidden
            and not component.ancestor_disabled
            and not component.ancestor_hidden
            and component.component_id not in used_component_ids
        ]
        if unsupported_required:
            raise _mapping_error("模板还有未映射的必填控件：" + "、".join(unsupported_required))
    return normalized


def validate_travel_type_mappings(
    schema: FormSchema,
    mappings: dict[str, str],
    options: dict[str, FormOption] | None,
    reimbursement_schema: FormSchema,
    reimbursement_mappings: dict[str, str],
) -> dict[str, FormOption] | None:
    component_id = mappings.get("travelType")
    if component_id is None and options is None:
        return None
    component = next(
        (item for item in schema.components if item.component_id == component_id), None
    )
    if component is None or not component.options or not isinstance(options, dict):
        raise _mapping_error("按出差类别对应时，必须选择来源控件并配置每个选项")
    source_values = [item.value for item in component.options]
    if len(source_values) != len(set(source_values)) or set(options) != set(source_values):
        raise _mapping_error("出差类别选项对应关系必须完整且唯一")
    return {
        key: validate_exact_travel_type_option(reimbursement_schema, reimbursement_mappings, option)
        for key, option in options.items()
    }


def _stored_type_mappings(stored: dict[str, object]) -> dict[str, FormOption] | None:
    raw = stored.get("travelTypeMappings")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _configuration_error()
    return {key: _option_from_value(value) for key, value in raw.items()}


def _profile_keys_valid(stored: dict[str, object]) -> bool:
    return set(stored) in (
        _TRAVEL_PROFILE_JSON_KEYS,
        _TRAVEL_PROFILE_JSON_KEYS | {"travelTypeMappings"},
    )


def validate_exact_travel_type_option(
    reimbursement_schema: FormSchema,
    reimbursement_mappings: dict[str, str],
    requested: FormOption,
) -> FormOption:
    component_id = reimbursement_mappings["travelType"]
    component = next(
        (item for item in reimbursement_schema.components if item.component_id == component_id),
        None,
    )
    if component is None or component.component_type != "DDSelectField":
        raise _mapping_error("出差类别对应的 OA 选择控件不存在")
    matches = [
        option
        for option in component.options
        if option.value == requested.value
        and option.label == requested.label
        and option.key == requested.key
    ]
    if len(matches) != 1:
        raise _mapping_error("出差模板对应的出差类别选项已失效，请重新选择")
    return matches[0]


def validate_related_approval_configuration(
    schema: FormSchema,
    mappings: dict[str, str],
    *,
    reimbursement_process_code: str,
    allowed_travel_process_codes: list[str],
) -> tuple[str, ...]:
    if not allowed_travel_process_codes:
        raise _relationship_error("至少配置一个允许关联的出差审批模板")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_process_code in allowed_travel_process_codes:
        process_code = raw_process_code.strip() if isinstance(raw_process_code, str) else ""
        if not _PROCESS_CODE_PATTERN.fullmatch(process_code):
            raise _relationship_error("出差审批模板 processCode 格式无效")
        if process_code == reimbursement_process_code:
            raise _relationship_error("报销模板不能同时作为允许关联的出差模板")
        if process_code in seen:
            raise _relationship_error("允许关联的出差审批模板不能重复")
        seen.add(process_code)
        normalized.append(process_code)

    component_by_id = {component.component_id: component for component in schema.components}
    relation_component = component_by_id[mappings["relatedApprovals"]]
    policy = relation_component.related_template_policy
    if policy is None:
        raise _relationship_error("映射控件不是关联审批控件")
    if policy.mode == "RESTRICTED":
        outside_policy = sorted(set(normalized) - set(policy.process_codes))
        if outside_policy:
            raise _relationship_error("出差审批模板不在关联控件声明的允许范围内")
    return tuple(normalized)


def template_catalog_data(profile: OaTemplateProfile) -> dict[str, object]:
    reimbursement_schema = _deserialize_schema(profile.schema_json)
    reimbursement_mapping = _deserialize_mapping(profile.mapping_json)
    travel_profiles = _deserialize_travel_profiles(
        profile.travel_profiles_json,
        reimbursement_schema=reimbursement_schema,
        reimbursement_mapping=reimbursement_mapping,
        allow_empty=True,
    )
    allowed_codes = _deserialize_process_codes(
        profile.allowed_travel_process_codes_json,
        allow_empty=True,
    )
    is_ready = _profile_submission_ready(profile)
    return {
        "configVersion": profile.config_version,
        "processCode": profile.process_code,
        "templateName": profile.template_name,
        "schemaFingerprint": profile.schema_fingerprint,
        "confirmedSchemaFingerprint": profile.confirmed_schema_fingerprint,
        "compatibilityStatus": profile.compatibility_status,
        "isSubmissionReady": is_ready,
        "requiresConfirmation": not is_ready,
        "reimbursement": {
            "processCode": profile.process_code,
            "schema": _schema_api_data(
                reimbursement_schema,
                REIMBURSEMENT_LOGICAL_FIELD_SPECS,
            ),
            "logicalFields": [item.as_dict() for item in REIMBURSEMENT_LOGICAL_FIELD_SPECS],
            "mappings": reimbursement_mapping,
        },
        "travelProfiles": [_travel_profile_api_data(item) for item in travel_profiles],
        "allowedTravelProcessCodes": allowed_codes,
        "lastCheckedAt": _timestamp(profile.last_checked_at),
        "confirmedAt": _timestamp(profile.confirmed_at),
        "updatedAt": _timestamp(profile.updated_at),
    }


def _travel_profile_api_data(profile: TravelTemplateContract) -> dict[str, object]:
    return {
        "profileKey": profile.profile_key,
        "displayName": profile.display_name,
        "processCode": profile.process_code,
        "schemaFingerprint": profile.schema.fingerprint,
        "confirmedSchemaFingerprint": (
            profile.confirmed_schema_fingerprint or profile.schema.fingerprint
        ),
        "schema": _schema_api_data(profile.schema, TRAVEL_LOGICAL_FIELD_SPECS),
        "logicalFields": [item.as_dict() for item in TRAVEL_LOGICAL_FIELD_SPECS],
        "mappings": profile.mappings,
        "travelTypeOption": profile.travel_type_option.as_dict(),
        **(
            {
                "travelTypeMappings": {
                    key: value.as_dict() for key, value in profile.travel_type_mappings.items()
                }
            }
            if profile.travel_type_mappings is not None
            else {}
        ),
    }


def _schema_api_data(
    schema: FormSchema,
    specs: tuple[LogicalFieldSpec, ...],
) -> dict[str, object]:
    data = schema.as_dict()
    components = []
    for component in schema.components:
        component_data = component.as_dict()
        component_data["compatibleLogicalFields"] = [
            spec.key for spec in specs if _component_supports(component, spec)
        ]
        components.append(component_data)
    data["components"] = components
    return data


def _component_supports(component: FormComponent, spec: LogicalFieldSpec) -> bool:
    return _component_incompatibility(component, spec) is None


def _component_incompatibility(
    component: FormComponent,
    spec: LogicalFieldSpec,
) -> str | None:
    is_travel_table = (
        spec.key in {"startDate", "endDate"}
        and component.component_type in _TRAVEL_TABLE_COMPONENT_TYPES
    )
    if component.in_subtable or (component.unsupported_container_ancestor and not is_travel_table):
        return "暂不支持映射到明细表或复杂业务组件的子控件"
    if (
        component.disabled
        or component.hidden
        or component.ancestor_disabled
        or component.ancestor_hidden
    ):
        return "不能映射到隐藏或禁用的 OA 控件"
    if component.component_type not in spec.supported_component_types:
        return f"不支持控件类型 {component.component_type}"
    if component.component_type == "DDSelectField" and not component.options:
        return "对应的选择控件没有可用选项"
    if (
        spec.key in {"startDate", "endDate", "durationDays"}
        and component.component_type in {"DDDateField", "DDDateRangeField"}
        and component.value_format != "yyyy-MM-dd"
    ):
        return "仅支持 yyyy-MM-dd 日期格式"
    return None


def _validate_profile_headers(
    reimbursement_process_code: str,
    profiles: list[TravelProfileInspection],
) -> list[TravelProfileInspection]:
    reimbursement_code = reimbursement_process_code.strip()
    if not _PROCESS_CODE_PATTERN.fullmatch(reimbursement_code):
        raise _relationship_error("报销审批模板 processCode 格式无效")
    if not profiles:
        raise _relationship_error("至少配置一个允许关联的出差审批模板")
    if len(profiles) > _MAX_TRAVEL_PROFILES:
        raise _relationship_error("允许关联的出差审批模板数量超过限制")

    normalized: list[TravelProfileInspection] = []
    seen_keys: set[str] = set()
    seen_codes: set[str] = set()
    for profile in profiles:
        profile_key = profile.profile_key.strip()
        display_name = profile.display_name.strip()
        process_code = profile.process_code.strip()
        if not _PROFILE_KEY_PATTERN.fullmatch(profile_key):
            raise _relationship_error("出差模板 profileKey 格式无效")
        if not display_name or len(display_name) > 128:
            raise _relationship_error("出差模板显示名称格式无效")
        if not _PROCESS_CODE_PATTERN.fullmatch(process_code):
            raise _relationship_error("出差审批模板 processCode 格式无效")
        if profile_key in seen_keys:
            raise _relationship_error("出差模板 profileKey 不能重复")
        if process_code in seen_codes:
            raise _relationship_error("出差审批模板 processCode 不能重复")
        if process_code == reimbursement_code:
            raise _relationship_error("报销模板不能同时作为出差模板")
        seen_keys.add(profile_key)
        seen_codes.add(process_code)
        normalized.append(
            TravelProfileInspection(
                profile_key=profile_key,
                display_name=display_name,
                process_code=process_code,
            )
        )
    return normalized


def _fresh_profile(database: Session) -> OaTemplateProfile | None:
    return database.scalar(
        select(OaTemplateProfile)
        .where(OaTemplateProfile.profile_key == REIMBURSEMENT_PROFILE_KEY)
        .execution_options(populate_existing=True)
    )


def _catalog_cas_state(profile: OaTemplateProfile) -> _CatalogCasState:
    return _CatalogCasState(
        process_code=profile.process_code,
        config_version=profile.config_version,
        schema_fingerprint=profile.schema_fingerprint,
        travel_profiles_json=profile.travel_profiles_json,
        compatibility_status=profile.compatibility_status,
    )


def _record_catalog_status(
    database: Session,
    *,
    expected: _CatalogCasState,
    compatibility_status: str,
) -> bool:
    result = database.execute(
        update(OaTemplateProfile)
        .where(
            OaTemplateProfile.profile_key == REIMBURSEMENT_PROFILE_KEY,
            OaTemplateProfile.process_code == expected.process_code,
            OaTemplateProfile.config_version == expected.config_version,
            OaTemplateProfile.schema_fingerprint == expected.schema_fingerprint,
            OaTemplateProfile.travel_profiles_json == expected.travel_profiles_json,
            OaTemplateProfile.compatibility_status == expected.compatibility_status,
        )
        .values(
            compatibility_status=compatibility_status,
            last_checked_at=utc_now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        database.rollback()
        return False
    database.commit()
    return True


def _replace_compatible_catalog(
    database: Session,
    *,
    expected_state: _CatalogCasState,
    refreshed: OaTemplateCatalogContract,
) -> bool:
    now = utc_now()
    reimbursement = refreshed.reimbursement
    result = database.execute(
        update(OaTemplateProfile)
        .where(
            OaTemplateProfile.profile_key == REIMBURSEMENT_PROFILE_KEY,
            OaTemplateProfile.process_code == expected_state.process_code,
            OaTemplateProfile.config_version == expected_state.config_version,
            OaTemplateProfile.schema_fingerprint == expected_state.schema_fingerprint,
            OaTemplateProfile.travel_profiles_json == expected_state.travel_profiles_json,
            OaTemplateProfile.compatibility_status == expected_state.compatibility_status,
        )
        .values(
            template_name=reimbursement.schema.template_name,
            schema_fingerprint=reimbursement.schema.fingerprint,
            schema_json=_serialize_schema(reimbursement.schema),
            mapping_json=_serialize_mapping(reimbursement.mappings),
            travel_profiles_json=_serialize_travel_profiles(list(refreshed.travel_profiles)),
            compatibility_status=COMPATIBLE,
            last_checked_at=now,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        database.rollback()
        return False
    database.commit()
    return True


def _serialize_schema(schema: FormSchema) -> str:
    return json.dumps(
        schema.as_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _deserialize_schema(raw: str) -> FormSchema:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise _configuration_error() from None
    if not isinstance(value, dict):
        raise _configuration_error()
    try:
        return form_schema_from_dict(value)
    except (TypeError, ValueError):
        raise _configuration_error() from None


def _serialize_mapping(mapping: dict[str, str]) -> str:
    return json.dumps(mapping, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _deserialize_mapping(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise _configuration_error() from None
    return _mapping_from_value(value)


def _mapping_from_value(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(component_id, str)
        for key, component_id in value.items()
    ):
        raise _configuration_error()
    return dict(value)


def _display_mapping(raw: str) -> dict[str, str] | None:
    try:
        return _deserialize_mapping(raw)
    except ApiError:
        return None


def _display_mapping_value(value: object) -> dict[str, str] | None:
    try:
        return _mapping_from_value(value)
    except ApiError:
        return None


def _serialize_process_codes(process_codes: tuple[str, ...]) -> str:
    return json.dumps(process_codes, ensure_ascii=False, separators=(",", ":"))


def _deserialize_process_codes(raw: str, *, allow_empty: bool = False) -> list[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise _configuration_error() from None
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or any(
            not isinstance(process_code, str) or not _PROCESS_CODE_PATTERN.fullmatch(process_code)
            for process_code in value
        )
        or len(set(value)) != len(value)
    ):
        raise _configuration_error()
    return value


def _serialize_travel_profiles(profiles: list[TravelTemplateContract]) -> str:
    value = [
        {
            "profileKey": profile.profile_key,
            "displayName": profile.display_name,
            "processCode": profile.process_code,
            "schemaFingerprint": profile.schema.fingerprint,
            "confirmedSchemaFingerprint": (
                profile.confirmed_schema_fingerprint or profile.schema.fingerprint
            ),
            "schema": profile.schema.as_dict(),
            "mappings": profile.mappings,
            "travelTypeOption": profile.travel_type_option.as_dict(),
            **(
                {
                    "travelTypeMappings": {
                        key: value.as_dict() for key, value in profile.travel_type_mappings.items()
                    }
                }
                if profile.travel_type_mappings is not None
                else {}
            ),
        }
        for profile in profiles
    ]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _deserialize_travel_profiles(
    raw: str,
    *,
    reimbursement_schema: FormSchema,
    reimbursement_mapping: dict[str, str],
    allow_empty: bool,
) -> list[TravelTemplateContract]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise _configuration_error() from None
    if not isinstance(value, list) or len(value) > _MAX_TRAVEL_PROFILES:
        raise _configuration_error()
    if not value and not allow_empty:
        raise _configuration_error()

    contracts: list[TravelTemplateContract] = []
    seen_keys: set[str] = set()
    seen_codes: set[str] = set()
    for raw_profile in value:
        if not isinstance(raw_profile, dict) or not _profile_keys_valid(raw_profile):
            raise _configuration_error()
        try:
            profile_key = raw_profile["profileKey"]
            display_name = raw_profile["displayName"]
            process_code = raw_profile["processCode"]
            schema_fingerprint = raw_profile["schemaFingerprint"]
            confirmed_fingerprint = raw_profile["confirmedSchemaFingerprint"]
            raw_schema = raw_profile["schema"]
            if (
                not isinstance(profile_key, str)
                or not isinstance(display_name, str)
                or not isinstance(process_code, str)
                or not isinstance(schema_fingerprint, str)
                or not isinstance(confirmed_fingerprint, str)
                or not isinstance(raw_schema, dict)
            ):
                raise TypeError
            schema = form_schema_from_dict(raw_schema)
            mapping = _mapping_from_value(raw_profile["mappings"])
            option = _option_from_value(raw_profile["travelTypeOption"])
        except (KeyError, TypeError, ValueError, ApiError):
            raise _configuration_error() from None
        if (
            not _PROFILE_KEY_PATTERN.fullmatch(profile_key)
            or not display_name.strip()
            or len(display_name) > 128
            or not _PROCESS_CODE_PATTERN.fullmatch(process_code)
            or not _FINGERPRINT_PATTERN.fullmatch(schema_fingerprint)
            or not _FINGERPRINT_PATTERN.fullmatch(confirmed_fingerprint)
            or schema.process_code != process_code
            or schema.fingerprint != schema_fingerprint
            or profile_key in seen_keys
            or process_code in seen_codes
        ):
            raise _configuration_error()
        normalized_mapping = validate_travel_template_mapping(schema, mapping)
        exact_option = validate_exact_travel_type_option(
            reimbursement_schema,
            reimbursement_mapping,
            option,
        )
        seen_keys.add(profile_key)
        seen_codes.add(process_code)
        contracts.append(
            TravelTemplateContract(
                profile_key=profile_key,
                display_name=display_name.strip(),
                process_code=process_code,
                schema=schema,
                start_date_component_id=normalized_mapping["startDate"],
                end_date_component_id=normalized_mapping["endDate"],
                travel_type_option=exact_option,
                company_component_id=normalized_mapping.get("company"),
                budget_code_component_id=normalized_mapping.get("budgetCode"),
                travel_type_component_id=normalized_mapping.get("travelType"),
                travel_type_mappings=validate_travel_type_mappings(
                    schema,
                    normalized_mapping,
                    _stored_type_mappings(raw_profile),
                    reimbursement_schema,
                    reimbursement_mapping,
                ),
                confirmed_schema_fingerprint=confirmed_fingerprint,
            )
        )
    return contracts


def _option_from_value(value: object) -> FormOption:
    if not isinstance(value, dict) or set(value) != {"value", "label", "key"}:
        raise _configuration_error()
    option_value = value.get("value")
    label = value.get("label")
    key = value.get("key")
    if (
        not isinstance(option_value, str)
        or not option_value
        or not isinstance(label, str)
        or not label
        or (key is not None and (not isinstance(key, str) or not key))
    ):
        raise _configuration_error()
    return FormOption(value=option_value, label=label, key=key)


def _display_option_value(value: object) -> dict[str, str | None] | None:
    try:
        return _option_from_value(value).as_dict()
    except ApiError:
        return None


def _display_travel_profiles(profile: OaTemplateProfile | None) -> list[dict[str, object]]:
    if profile is None:
        return []
    try:
        value = json.loads(profile.travel_profiles_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _validated_persisted_catalog(
    profile: OaTemplateProfile,
    *,
    require_compatible_status: bool = True,
) -> OaTemplateCatalogContract:
    if require_compatible_status and profile.compatibility_status != COMPATIBLE:
        raise _configuration_error()
    reimbursement_schema = _deserialize_schema(profile.schema_json)
    if (
        reimbursement_schema.process_code != profile.process_code
        or reimbursement_schema.fingerprint != profile.schema_fingerprint
    ):
        raise _configuration_error()
    reimbursement_mapping = validate_template_mapping(
        reimbursement_schema,
        _deserialize_mapping(profile.mapping_json),
    )
    travel_profiles = _deserialize_travel_profiles(
        profile.travel_profiles_json,
        reimbursement_schema=reimbursement_schema,
        reimbursement_mapping=reimbursement_mapping,
        allow_empty=False,
    )
    if any(item.process_code == profile.process_code for item in travel_profiles):
        raise _configuration_error()
    derived_codes = tuple(item.process_code for item in travel_profiles)
    stored_codes = tuple(_deserialize_process_codes(profile.allowed_travel_process_codes_json))
    if stored_codes != derived_codes:
        raise _configuration_error()
    validate_related_approval_configuration(
        reimbursement_schema,
        reimbursement_mapping,
        reimbursement_process_code=profile.process_code,
        allowed_travel_process_codes=list(derived_codes),
    )
    return OaTemplateCatalogContract(
        config_version=profile.config_version,
        reimbursement=ReimbursementTemplateContract(
            process_code=profile.process_code,
            schema=reimbursement_schema,
            mappings=reimbursement_mapping,
            confirmed_schema_fingerprint=profile.confirmed_schema_fingerprint,
        ),
        travel_profiles=tuple(travel_profiles),
    )


def _profile_submission_ready(profile: OaTemplateProfile) -> bool:
    try:
        _validated_persisted_catalog(profile)
    except ApiError:
        return False
    return True


def _timestamp(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _mapping_error(detail: str) -> ApiError:
    return ApiError("OA_TEMPLATE_MAPPING_INVALID", detail, 400)


def _relationship_error(detail: str) -> ApiError:
    return ApiError("OA_TEMPLATE_RELATIONSHIP_INVALID", detail, 400)


def _configuration_error() -> ApiError:
    return ApiError(
        "INVALID_SYSTEM_CONFIGURATION",
        "审批模板目录配置无效，请联系管理员重新确认",
        500,
    )


def _confirmation_required_error() -> ApiError:
    return ApiError(
        "OA_TEMPLATE_CONFIRMATION_REQUIRED",
        "审批模板目录已经变化，请管理员重新检查并确认",
        409,
    )


def _schema_changed_error() -> ApiError:
    return ApiError(
        "OA_TEMPLATE_SCHEMA_CHANGED",
        "审批模板已更新，请重新检查字段并确认",
        409,
    )


def _configuration_changed_error() -> ApiError:
    return ApiError(
        "OA_TEMPLATE_CONFIGURATION_CHANGED",
        "审批模板目录刚刚被其他管理员更新，请重试",
        409,
    )
