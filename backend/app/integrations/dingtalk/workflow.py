from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIClient, DingTalkOpenAPIError

FORM_SCHEMA_PATH = "/v1.0/workflow/forms/schemas/processCodes"
PROCESS_INSTANCE_IDS_PATH = "/v1.0/workflow/processes/instanceIds/query"
PROCESS_INSTANCES_PATH = "/v1.0/workflow/processInstances"
_LAYOUT_CONTAINER_TYPES = frozenset({"FieldGroup"})
_SUBTABLE_CONTAINER_TYPES = frozenset({"DDTableField", "TableField"})
_INVALID_PROCESS_CODE_NAMES = frozenset({"formnotexist", "aflowprocesscodeiserror"})
_CREATE_REJECTED_UPSTREAM_CODES = frozenset(
    {
        "formconvertererror",
        "illegalcomponent",
        "invalidagentid",
        "invalidparameter",
        "needauth",
        "processcodeerror",
        "processinstanceinvalidparameter",
        "processsetupnopermission",
        "targetselectapprovermissing",
        "targetselectapproverscopeerror",
    }
)
_SAFE_UPSTREAM_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_PROCESS_CODE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PROCESS_INSTANCE_STATUSES = frozenset({"RUNNING", "TERMINATED", "COMPLETED"})
_SIGNED_INT64_MAX = 9_223_372_036_854_775_807
_MAX_INSTANCE_QUERY_SPAN_MILLIS = 120 * 24 * 60 * 60 * 1000
_MAX_PROCESS_CODE_LENGTH = 128
_MAX_WORKFLOW_IDENTIFIER_LENGTH = 512
_MAX_FORM_COMPONENT_NAME_LENGTH = 255
_MAX_FORM_COMPONENT_VALUE_LENGTH = 2 * 1024 * 1024


class DingTalkProcessInstanceCreateRejected(ApiError):
    """A mutation that DingTalk definitively rejected before creating an OA."""

    __slots__ = ("http_status", "upstream_code")

    def __init__(
        self,
        *,
        http_status: int | None,
        upstream_code: str | None,
    ) -> None:
        self.http_status = http_status
        self.upstream_code = upstream_code
        super().__init__(
            "OA_CREATE_REJECTED",
            "钉钉拒绝发起审批，请检查审批模板、表单内容和发起人权限",
            502,
        )


class DingTalkProcessInstanceCreateOutcomeUnknown(ApiError):
    """A mutation may have succeeded and therefore must never be retried blindly."""

    __slots__ = ("http_status", "upstream_code")

    def __init__(
        self,
        *,
        http_status: int | None,
        upstream_code: str | None,
    ) -> None:
        self.http_status = http_status
        self.upstream_code = upstream_code
        super().__init__(
            "OA_CREATE_OUTCOME_UNKNOWN",
            "审批提交结果正在确认，请勿重复提交",
            503,
        )


@dataclass(frozen=True, slots=True)
class WorkflowInstanceIdPage:
    instance_ids: tuple[str, ...]
    next_token: int | None


@dataclass(frozen=True, slots=True)
class WorkflowFormValue:
    component_id: str | None
    name: str
    component_type: str | None
    value: str | None
    ext_value: str | None
    biz_alias: str | None


@dataclass(frozen=True, slots=True)
class WorkflowProcessInstance:
    instance_id: str
    title: str
    business_id: str
    originator_user_id: str
    originator_department_id: str
    status: str
    result: str | None
    created_at: str
    finished_at: str | None
    form_values: tuple[WorkflowFormValue, ...]


@dataclass(frozen=True, slots=True)
class CreateWorkflowFormValue:
    name: str
    value: str
    component_id: str | None = None
    component_type: str | None = None
    biz_alias: str | None = None


@dataclass(frozen=True, slots=True)
class CreateProcessInstanceCommand:
    process_code: str
    originator_user_id: str
    department_id: int
    microapp_agent_id: int
    form_values: tuple[CreateWorkflowFormValue, ...]


@dataclass(frozen=True, slots=True)
class CreatedProcessInstance:
    instance_id: str


@dataclass(frozen=True, slots=True)
class FormOption:
    value: str
    label: str
    key: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"value": self.value, "label": self.label, "key": self.key}


@dataclass(frozen=True, slots=True)
class RelatedTemplatePolicy:
    mode: str
    process_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "processCodes": list(self.process_codes)}


@dataclass(frozen=True, slots=True)
class FormComponent:
    component_id: str
    component_type: str
    label: str
    biz_alias: str | None
    required: bool
    disabled: bool
    hidden: bool
    ancestor_disabled: bool
    ancestor_hidden: bool
    nested: bool
    in_subtable: bool
    unsupported_container_ancestor: bool
    value_format: str | None
    unit: str | None
    options: tuple[FormOption, ...]
    parent_component_id: str | None
    related_template_policy: RelatedTemplatePolicy | None

    def as_dict(self) -> dict[str, object]:
        return {
            "componentId": self.component_id,
            "componentType": self.component_type,
            "label": self.label,
            "bizAlias": self.biz_alias,
            "required": self.required,
            "disabled": self.disabled,
            "hidden": self.hidden,
            "ancestorDisabled": self.ancestor_disabled,
            "ancestorHidden": self.ancestor_hidden,
            "nested": self.nested,
            "inSubtable": self.in_subtable,
            "unsupportedContainerAncestor": self.unsupported_container_ancestor,
            "format": self.value_format,
            "unit": self.unit,
            "options": [option.as_dict() for option in self.options],
            "parentComponentId": self.parent_component_id,
            "relatedTemplatePolicy": (
                self.related_template_policy.as_dict()
                if self.related_template_policy is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class FormSchema:
    process_code: str
    form_code: str | None
    form_uuid: str | None
    modified_at: str | None
    status: str
    template_name: str
    title: str
    components: tuple[FormComponent, ...]
    fingerprint: str

    def as_dict(self) -> dict[str, object]:
        return {
            "processCode": self.process_code,
            "formCode": self.form_code,
            "formUuid": self.form_uuid,
            "modifiedAt": self.modified_at,
            "status": self.status,
            "templateName": self.template_name,
            "title": self.title,
            "schemaFingerprint": self.fingerprint,
            "components": [component.as_dict() for component in self.components],
        }


class DingTalkWorkflowClient:
    """Typed Workflow facade over the shared organization-app HTTP client."""

    def __init__(self, client: DingTalkOpenAPIClient) -> None:
        self._client = client

    async def get_form_schema(self, process_code: str) -> FormSchema:
        # Official contract: GET with processCode in the query string and the
        # organization access token in x-acs-dingtalk-access-token.
        try:
            payload = await self._client.request_openapi_json(
                "GET",
                FORM_SCHEMA_PATH,
                params={"processCode": process_code},
            )
        except DingTalkOpenAPIError as exc:
            if _is_invalid_process_code(exc.upstream_code):
                raise _process_code_error() from None
            raise
        if _is_invalid_process_code(payload.get("code")):
            raise _process_code_error()
        return normalize_form_schema(process_code, payload)

    async def list_process_instance_ids(
        self,
        *,
        process_code: str,
        start_time: int,
        end_time: int,
        next_token: int,
        max_results: int,
        user_ids: tuple[str, ...],
        statuses: tuple[str, ...],
    ) -> WorkflowInstanceIdPage:
        request_body = _instance_id_query_body(
            process_code=process_code,
            start_time=start_time,
            end_time=end_time,
            next_token=next_token,
            max_results=max_results,
            user_ids=user_ids,
            statuses=statuses,
        )
        return await self._read_instance_id_page(request_body, next_token)

    async def list_process_instance_ids_for_diagnostics(
        self,
        *,
        process_code: str,
        start_time: int,
        end_time: int,
        next_token: int,
        max_results: int,
    ) -> WorkflowInstanceIdPage:
        """Admin discovery only; at most ten minutes, including unfinished instances."""
        body = _instance_query_body(
            process_code=process_code,
            start_time=start_time,
            end_time=end_time,
            next_token=next_token,
            max_results=max_results,
            statuses=("COMPLETED", "RUNNING", "TERMINATED"),
        )
        if end_time - start_time >= 10 * 60 * 1000:
            raise ValueError("diagnostic discovery window must not exceed ten minutes")
        return await self._read_instance_id_page(body, next_token)

    async def _read_instance_id_page(
        self,
        request_body: dict[str, object],
        next_token: int,
    ) -> WorkflowInstanceIdPage:
        try:
            payload = await self._client.request_openapi_json(
                "POST",
                PROCESS_INSTANCE_IDS_PATH,
                json=request_body,
                # Although this endpoint is POST, it is a read-only cursor
                # query. Retrying bounded transient failures is safe.
                retry_transient=True,
            )
        except DingTalkOpenAPIError as exc:
            if _is_invalid_instance_list_process_code(exc.upstream_code):
                raise _process_code_error() from None
            raise
        if _is_invalid_instance_list_process_code(payload.get("code")):
            raise _process_code_error()
        return normalize_instance_id_page(payload, requested_next_token=next_token)

    async def get_process_instance(self, process_instance_id: str) -> WorkflowProcessInstance:
        instance_id = _required_identifier(
            process_instance_id,
            field_name="process_instance_id",
        )
        payload = await self._client.request_openapi_json(
            "GET",
            PROCESS_INSTANCES_PATH,
            params={"processInstanceId": instance_id},
        )
        return normalize_process_instance(instance_id, payload)

    async def create_process_instance(
        self,
        command: CreateProcessInstanceCommand,
    ) -> CreatedProcessInstance:
        # Build the transmitted object from the same canonical serialization
        # that the reimbursement state machine persists and hashes.  This
        # prevents the mutation body and its audit checkpoint from drifting.
        request_body = json.loads(serialize_create_process_instance_command(command))
        create_error: ApiError | None = None
        payload: dict[str, Any] | None = None
        try:
            payload = await self._client.request_openapi_json(
                "POST",
                PROCESS_INSTANCES_PATH,
                json=request_body,
                # This mutation has no idempotency token at the upstream API.
                # A retry could create a second approval instance.
                retry_invalid_token=False,
                retry_transient=False,
            )
        except DingTalkOpenAPIError as exc:
            if exc.http_status == 401 or exc.code == "DINGTALK_PERMISSION_MISSING":
                create_error = exc
            elif _is_definitive_create_rejection(
                http_status=exc.http_status,
                upstream_code=exc.upstream_code,
            ):
                create_error = DingTalkProcessInstanceCreateRejected(
                    http_status=exc.http_status,
                    upstream_code=exc.upstream_code,
                )
            else:
                create_error = DingTalkProcessInstanceCreateOutcomeUnknown(
                    http_status=exc.http_status,
                    upstream_code=exc.upstream_code,
                )

        # Raise outside the exception handler so no upstream exception context
        # is retained by the public error object.
        if create_error is not None:
            raise create_error
        if payload is None:
            raise DingTalkProcessInstanceCreateOutcomeUnknown(
                http_status=None,
                upstream_code=None,
            )
        return normalize_created_process_instance(payload)


def normalize_instance_id_page(
    payload: dict[str, Any],
    *,
    requested_next_token: int,
) -> WorkflowInstanceIdPage:
    if payload.get("success") is not True:
        raise _instance_list_error()
    result = payload.get("result")
    if not isinstance(result, dict):
        raise _instance_list_error()
    raw_ids = result.get("list")
    if not isinstance(raw_ids, list):
        raise _instance_list_error()

    instance_ids: list[str] = []
    seen: set[str] = set()
    try:
        for raw_id in raw_ids:
            instance_id = _required_identifier(raw_id, field_name="process_instance_id")
            if instance_id in seen:
                raise ValueError
            seen.add(instance_id)
            instance_ids.append(instance_id)
        next_token = _response_next_token(result)
        if next_token is not None and next_token <= requested_next_token:
            raise ValueError
    except ValueError:
        raise _instance_list_error() from None
    return WorkflowInstanceIdPage(
        instance_ids=tuple(instance_ids),
        next_token=next_token,
    )


def normalize_process_instance(
    process_instance_id: str,
    payload: dict[str, Any],
) -> WorkflowProcessInstance:
    if not _successful_read_response(payload.get("success")):
        raise _instance_detail_error()
    result = payload.get("result")
    if not isinstance(result, dict):
        raise _instance_detail_error()

    raw_values = result.get("formComponentValues")
    if not isinstance(raw_values, list):
        raise _instance_detail_error()
    form_values: list[WorkflowFormValue] = []
    seen_component_ids: set[str] = set()
    try:
        for raw_value in raw_values:
            if not isinstance(raw_value, dict):
                raise ValueError
            component_id = _optional_bounded_text(
                raw_value.get("id"),
                max_length=_MAX_WORKFLOW_IDENTIFIER_LENGTH,
            )
            if component_id is not None:
                if component_id in seen_component_ids:
                    raise ValueError
                seen_component_ids.add(component_id)
            form_values.append(
                WorkflowFormValue(
                    component_id=component_id,
                    name=_required_bounded_text(
                        raw_value.get("name"),
                        max_length=_MAX_FORM_COMPONENT_NAME_LENGTH,
                    ),
                    component_type=_optional_bounded_text(
                        raw_value.get("componentType"),
                        max_length=128,
                    ),
                    value=_optional_form_value(raw_value.get("value")),
                    ext_value=_optional_form_value(raw_value.get("extValue")),
                    biz_alias=_optional_bounded_text(
                        raw_value.get("bizAlias"),
                        max_length=255,
                    ),
                )
            )

        status = _required_bounded_text(result.get("status"), max_length=64).upper()
        approval_result = _optional_bounded_text(result.get("result"), max_length=64)
        return WorkflowProcessInstance(
            instance_id=_required_identifier(
                process_instance_id,
                field_name="process_instance_id",
            ),
            title=_required_bounded_text(result.get("title"), max_length=1024),
            business_id=_required_identifier(
                result.get("businessId"),
                field_name="business_id",
            ),
            originator_user_id=_required_identifier(
                result.get("originatorUserId"),
                field_name="originator_user_id",
            ),
            originator_department_id=_required_identifier(
                result.get("originatorDeptId"),
                field_name="originator_department_id",
            ),
            status=status,
            result=approval_result.lower() if approval_result is not None else None,
            created_at=_required_bounded_text(result.get("createTime"), max_length=128),
            finished_at=_optional_bounded_text(result.get("finishTime"), max_length=128),
            form_values=tuple(form_values),
        )
    except ValueError:
        raise _instance_detail_error() from None


def normalize_created_process_instance(
    payload: dict[str, Any],
) -> CreatedProcessInstance:
    try:
        instance_id = _required_identifier(
            payload.get("instanceId"),
            field_name="process_instance_id",
        )
    except ValueError:
        upstream_code = _safe_payload_code(payload)
        if _is_known_create_rejection_code(upstream_code):
            raise DingTalkProcessInstanceCreateRejected(
                http_status=200,
                upstream_code=upstream_code,
            ) from None
        # A successful mutation response with no usable ID cannot prove that
        # DingTalk did not create the approval.
        raise DingTalkProcessInstanceCreateOutcomeUnknown(
            http_status=200,
            upstream_code=upstream_code,
        ) from None
    return CreatedProcessInstance(instance_id=instance_id)


def _instance_id_query_body(
    *,
    process_code: str,
    start_time: int,
    end_time: int,
    next_token: int,
    max_results: int,
    user_ids: tuple[str, ...],
    statuses: tuple[str, ...],
) -> dict[str, object]:
    normalized_user_ids = _identifier_tuple(user_ids, field_name="user_id", maximum=10)
    return _instance_query_body(
        process_code=process_code,
        start_time=start_time,
        end_time=end_time,
        next_token=next_token,
        max_results=max_results,
        statuses=statuses,
    ) | {"userIds": list(normalized_user_ids)}


def _instance_query_body(
    *,
    process_code: str,
    start_time: int,
    end_time: int,
    next_token: int,
    max_results: int,
    statuses: tuple[str, ...],
) -> dict[str, object]:
    normalized_process_code = _required_process_code(process_code)
    for name, value in (
        ("start_time", start_time),
        ("end_time", end_time),
        ("next_token", next_token),
        ("max_results", max_results),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if start_time < 0 or end_time < start_time:
        raise ValueError("invalid process instance query time range")
    if start_time > _SIGNED_INT64_MAX or end_time > _SIGNED_INT64_MAX:
        raise ValueError("process instance query timestamps exceed signed int64")
    if end_time - start_time > _MAX_INSTANCE_QUERY_SPAN_MILLIS:
        raise ValueError("process instance query time range exceeds 120 days")
    if next_token < 0 or next_token > _SIGNED_INT64_MAX:
        raise ValueError("next_token must be a non-negative signed int64")
    if not 1 <= max_results <= 20:
        raise ValueError("max_results must be between 1 and 20")
    normalized_statuses = tuple(
        item.upper() for item in _identifier_tuple(statuses, field_name="status", maximum=3)
    )
    if not set(normalized_statuses) <= _PROCESS_INSTANCE_STATUSES:
        raise ValueError("statuses contains an unsupported value")
    return {
        "processCode": normalized_process_code,
        "startTime": start_time,
        "endTime": end_time,
        "nextToken": next_token,
        "maxResults": max_results,
        "statuses": list(normalized_statuses),
    }


def serialize_create_process_instance_command(
    command: CreateProcessInstanceCommand,
) -> str:
    """Return the canonical JSON used for both OA creation and request hashing."""

    return json.dumps(
        _create_process_instance_body(command),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _create_process_instance_body(
    command: CreateProcessInstanceCommand,
) -> dict[str, object]:
    process_code = _required_process_code(command.process_code)
    originator_user_id = _required_identifier(
        command.originator_user_id,
        field_name="originator_user_id",
    )
    if (
        isinstance(command.department_id, bool)
        or not isinstance(command.department_id, int)
        or command.department_id <= 0
        or command.department_id > _SIGNED_INT64_MAX
    ):
        raise ValueError("department_id must be a positive integer")
    if (
        isinstance(command.microapp_agent_id, bool)
        or not isinstance(command.microapp_agent_id, int)
        or command.microapp_agent_id <= 0
        or command.microapp_agent_id > _SIGNED_INT64_MAX
    ):
        raise ValueError("microapp_agent_id must be a positive integer")
    if not isinstance(command.form_values, tuple) or not command.form_values:
        raise ValueError("form_values must be a non-empty tuple")

    values: list[dict[str, str]] = []
    seen_component_ids: set[str] = set()
    for item in command.form_values:
        if not isinstance(item, CreateWorkflowFormValue):
            raise ValueError("form_values contains an invalid item")
        name = _required_bounded_text(
            item.name,
            max_length=_MAX_FORM_COMPONENT_NAME_LENGTH,
        )
        if not isinstance(item.value, str) or len(item.value) > _MAX_FORM_COMPONENT_VALUE_LENGTH:
            raise ValueError("form component value is invalid")
        value: dict[str, str] = {"name": name, "value": item.value}
        component_id = _optional_bounded_text(
            item.component_id,
            max_length=_MAX_WORKFLOW_IDENTIFIER_LENGTH,
        )
        if component_id is not None:
            if component_id in seen_component_ids:
                raise ValueError("form component ids must be unique")
            seen_component_ids.add(component_id)
            value["id"] = component_id
        component_type = _optional_bounded_text(item.component_type, max_length=128)
        if component_type is not None:
            value["componentType"] = component_type
        biz_alias = _optional_bounded_text(item.biz_alias, max_length=255)
        if biz_alias is not None:
            value["bizAlias"] = biz_alias
        values.append(value)

    return {
        "processCode": process_code,
        "originatorUserId": originator_user_id,
        "deptId": command.department_id,
        "microappAgentId": command.microapp_agent_id,
        "formComponentValues": values,
    }


def normalize_form_schema(process_code: str, payload: dict[str, Any]) -> FormSchema:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise _schema_error()

    schema_content = _schema_content(result.get("schemaContent"))
    raw_items = schema_content.get("items")
    if not isinstance(raw_items, list):
        raise _schema_error()

    components: list[FormComponent] = []
    seen_ids: set[str] = set()
    for raw_item in raw_items:
        _collect_components(
            raw_item,
            parent_component_id=None,
            ancestor_disabled=False,
            ancestor_hidden=False,
            nesting_depth=0,
            in_subtable=False,
            unsupported_container_ancestor=False,
            components=components,
            seen_ids=seen_ids,
        )
    if not components:
        raise _schema_error()

    template_name = _optional_text(result.get("name")) or _optional_text(
        schema_content.get("title")
    )
    title = _optional_text(schema_content.get("title")) or template_name
    if not template_name or not title:
        raise _schema_error()

    try:
        form_code = _required_text(result.get("formCode"))
        form_uuid = _required_text(result.get("formUuid"))
        modified_at = _required_text(result.get("gmtModified"))
        status = _required_text(result.get("status"))
    except ValueError:
        raise _schema_error() from None
    if form_code != process_code or status != "PUBLISHED":
        raise _schema_error()
    fingerprint = schema_fingerprint(
        process_code,
        form_code,
        form_uuid,
        modified_at,
        status,
        components,
    )
    return FormSchema(
        process_code=process_code,
        form_code=form_code,
        form_uuid=form_uuid,
        modified_at=modified_at,
        status=status,
        template_name=template_name,
        title=title,
        components=tuple(components),
        fingerprint=fingerprint,
    )


def schema_fingerprint(
    process_code: str,
    form_code: str | None,
    form_uuid: str | None,
    modified_at: str | None,
    status: str,
    components: list[FormComponent] | tuple[FormComponent, ...],
) -> str:
    canonical_components: list[dict[str, object]] = []
    for component in sorted(components, key=lambda item: item.component_id):
        canonical_components.append(
            {
                "componentId": component.component_id,
                "componentType": component.component_type,
                "label": component.label,
                "bizAlias": component.biz_alias,
                "required": component.required,
                "disabled": component.disabled,
                "hidden": component.hidden,
                "ancestorDisabled": component.ancestor_disabled,
                "ancestorHidden": component.ancestor_hidden,
                "nested": component.nested,
                "inSubtable": component.in_subtable,
                "unsupportedContainerAncestor": component.unsupported_container_ancestor,
                "format": component.value_format,
                "unit": component.unit,
                "parentComponentId": component.parent_component_id,
                "relatedTemplatePolicy": (
                    component.related_template_policy.as_dict()
                    if component.related_template_policy is not None
                    else None
                ),
                "options": sorted(
                    (option.as_dict() for option in component.options),
                    key=lambda item: (item["value"] or "", item["label"] or ""),
                ),
            }
        )
    canonical = json.dumps(
        {
            "processCode": process_code,
            "formCode": form_code,
            "formUuid": form_uuid,
            "modifiedAt": modified_at,
            "status": status,
            "components": canonical_components,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def form_schema_from_dict(value: dict[str, Any]) -> FormSchema:
    """Restore a persisted normalized schema without accepting a raw OA payload."""

    try:
        process_code = _required_text(value["processCode"])
        form_code = _required_text(value["formCode"])
        form_uuid = _required_text(value["formUuid"])
        modified_at = _required_text(value["modifiedAt"])
        status = _required_text(value["status"])
        template_name = _required_text(value["templateName"])
        title = _required_text(value["title"])
        raw_components = value["components"]
        if not isinstance(raw_components, list):
            raise TypeError
        components: list[FormComponent] = []
        seen_ids: set[str] = set()
        for raw_component in raw_components:
            if not isinstance(raw_component, dict):
                raise TypeError
            component_id = _required_text(raw_component["componentId"])
            if component_id in seen_ids:
                raise TypeError
            seen_ids.add(component_id)
            raw_options = raw_component.get("options", [])
            if not isinstance(raw_options, list):
                raise TypeError
            options = tuple(
                FormOption(
                    value=_required_text(option["value"]),
                    label=_required_text(option["label"]),
                    key=_optional_text(option.get("key")),
                )
                for option in raw_options
                if isinstance(option, dict)
            )
            if len(options) != len(raw_options):
                raise TypeError
            components.append(
                FormComponent(
                    component_id=component_id,
                    component_type=_required_text(raw_component["componentType"]),
                    label=_required_text(raw_component["label"]),
                    biz_alias=_optional_text(raw_component.get("bizAlias")),
                    required=raw_component.get("required") is True,
                    disabled=raw_component.get("disabled") is True,
                    hidden=raw_component.get("hidden") is True,
                    ancestor_disabled=raw_component.get("ancestorDisabled") is True,
                    ancestor_hidden=raw_component.get("ancestorHidden") is True,
                    nested=raw_component.get("nested") is True,
                    in_subtable=raw_component.get("inSubtable") is True,
                    unsupported_container_ancestor=(
                        raw_component.get("unsupportedContainerAncestor") is True
                    ),
                    value_format=_optional_text(raw_component.get("format")),
                    unit=_optional_text(raw_component.get("unit")),
                    options=options,
                    parent_component_id=_optional_text(raw_component.get("parentComponentId")),
                    related_template_policy=_stored_related_template_policy(
                        raw_component.get("relatedTemplatePolicy")
                    ),
                )
            )
        if form_code != process_code or status != "PUBLISHED":
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise _stored_schema_error() from None

    fingerprint = schema_fingerprint(
        process_code,
        form_code,
        form_uuid,
        modified_at,
        status,
        components,
    )
    return FormSchema(
        process_code=process_code,
        form_code=form_code,
        form_uuid=form_uuid,
        modified_at=modified_at,
        status=status,
        template_name=template_name,
        title=title,
        components=tuple(components),
        fingerprint=fingerprint,
    )


def _schema_content(raw_value: object) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        try:
            decoded = json.loads(raw_value)
        except ValueError:
            raise _schema_error() from None
        if isinstance(decoded, dict):
            return decoded
    raise _schema_error()


def _collect_components(
    raw_item: object,
    parent_component_id: str | None,
    ancestor_disabled: bool,
    ancestor_hidden: bool,
    nesting_depth: int,
    in_subtable: bool,
    unsupported_container_ancestor: bool,
    components: list[FormComponent],
    seen_ids: set[str],
) -> None:
    if not isinstance(raw_item, dict):
        raise _schema_error()
    component_type = _optional_text(raw_item.get("componentName"))
    props = raw_item.get("props")
    if not component_type or not isinstance(props, dict):
        raise _schema_error()

    component_id = _optional_text(props.get("id"))
    children = raw_item.get("children", [])
    if children is None:
        children = []
    if not isinstance(children, list):
        raise _schema_error()
    disabled = props.get("disabled") is True
    hidden = props.get("hidden") is True

    if component_id:
        if component_id in seen_ids:
            raise _schema_error()
        seen_ids.add(component_id)
        components.append(
            FormComponent(
                component_id=component_id,
                component_type=component_type,
                label=_optional_text(props.get("label")) or component_id,
                biz_alias=_optional_text(props.get("bizAlias")),
                required=props.get("required") is True,
                disabled=disabled,
                hidden=hidden,
                ancestor_disabled=ancestor_disabled,
                ancestor_hidden=ancestor_hidden,
                nested=nesting_depth > 0,
                in_subtable=in_subtable,
                unsupported_container_ancestor=unsupported_container_ancestor,
                value_format=_optional_text(props.get("format")),
                unit=_optional_text(props.get("unit")),
                options=_normalize_options(props),
                parent_component_id=parent_component_id,
                related_template_policy=_related_template_policy(component_type, props),
            )
        )
    elif not children:
        raise _schema_error()

    child_parent_id = component_id or parent_component_id
    is_complex_container = bool(children) and component_type not in _LAYOUT_CONTAINER_TYPES
    for child in children:
        _collect_components(
            child,
            parent_component_id=child_parent_id,
            ancestor_disabled=ancestor_disabled or disabled,
            ancestor_hidden=ancestor_hidden or hidden,
            nesting_depth=nesting_depth + 1,
            in_subtable=in_subtable or component_type in _SUBTABLE_CONTAINER_TYPES,
            unsupported_container_ancestor=(unsupported_container_ancestor or is_complex_container),
            components=components,
            seen_ids=seen_ids,
        )


def _normalize_options(props: dict[str, Any]) -> tuple[FormOption, ...]:
    raw_options = props.get("options")
    if raw_options is None:
        raw_options = props.get("objOptions", [])
    if raw_options is None:
        return ()
    if not isinstance(raw_options, list):
        raise _schema_error()

    options: list[FormOption] = []
    seen_values: set[str] = set()
    seen_keys: set[str] = set()
    for raw_option in raw_options:
        if isinstance(raw_option, str):
            decoded_option: object = None
            try:
                decoded_option = json.loads(raw_option)
            except ValueError:
                if raw_option.lstrip().startswith(("{", "[")):
                    raise _schema_error() from None
            if isinstance(decoded_option, dict):
                raw_option = decoded_option
            else:
                value = raw_option.strip()
                label = value
                key = None
        if isinstance(raw_option, dict):
            value = _optional_text(raw_option.get("value")) or _optional_text(raw_option.get("key"))
            label = (
                _optional_text(raw_option.get("label"))
                or _optional_text(raw_option.get("text"))
                or value
            )
            key = _optional_text(raw_option.get("key"))
        elif not isinstance(raw_option, str):
            raise _schema_error()
        if not value or not label:
            raise _schema_error()
        if value in seen_values:
            raise _schema_error()
        if key is not None and key in seen_keys:
            raise _schema_error()
        seen_values.add(value)
        if key is not None:
            seen_keys.add(key)
        options.append(FormOption(value=value, label=label, key=key))
    return tuple(options)


def _related_template_policy(
    component_type: str,
    props: dict[str, Any],
) -> RelatedTemplatePolicy | None:
    if component_type != "RelateField":
        return None

    raw_templates = props.get("availableTemplates")
    if not isinstance(raw_templates, list) or not raw_templates:
        # The current official schema response does not declare this property.
        # Missing/empty therefore cannot safely be interpreted as allow-all.
        return RelatedTemplatePolicy(mode="UNKNOWN", process_codes=())

    process_codes: list[str] = []
    for raw_template in raw_templates:
        if isinstance(raw_template, str):
            process_code = raw_template.strip()
        elif isinstance(raw_template, dict):
            process_code = (
                _optional_text(raw_template.get("processCode"))
                or _optional_text(raw_template.get("code"))
                or ""
            )
        else:
            return RelatedTemplatePolicy(mode="UNKNOWN", process_codes=())
        if not process_code:
            return RelatedTemplatePolicy(mode="UNKNOWN", process_codes=())
        process_codes.append(process_code)
    return RelatedTemplatePolicy(
        mode="RESTRICTED",
        process_codes=tuple(sorted(set(process_codes))),
    )


def _stored_related_template_policy(raw: object) -> RelatedTemplatePolicy | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError
    mode = _required_text(raw.get("mode"))
    raw_codes = raw.get("processCodes")
    if mode not in {"UNKNOWN", "RESTRICTED"} or not isinstance(raw_codes, list):
        raise TypeError
    codes = tuple(_required_text(item) for item in raw_codes)
    if mode == "UNKNOWN" and codes:
        raise TypeError
    if mode == "RESTRICTED" and not codes:
        raise TypeError
    return RelatedTemplatePolicy(mode=mode, process_codes=codes)


def _response_next_token(result: dict[str, Any]) -> int | None:
    if "nextToken" not in result or result["nextToken"] is None:
        return None
    raw_token = result["nextToken"]
    if not isinstance(raw_token, str):
        raise ValueError
    token = raw_token.strip()
    if not token or not token.isascii() or not token.isdecimal():
        raise ValueError
    parsed = int(token)
    if parsed > _SIGNED_INT64_MAX:
        raise ValueError
    return parsed


def _successful_read_response(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _identifier_tuple(
    values: tuple[str, ...],
    *,
    field_name: str,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values or len(values) > maximum:
        raise ValueError(f"{field_name}s must be a non-empty bounded tuple")
    normalized = tuple(_required_identifier(item, field_name=field_name) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name}s must be unique")
    return normalized


def _required_identifier(value: object, *, field_name: str) -> str:
    try:
        normalized = _required_bounded_text(
            value,
            max_length=_MAX_WORKFLOW_IDENTIFIER_LENGTH,
        )
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError
        return normalized
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None


def _required_process_code(value: object) -> str:
    process_code = _required_bounded_text(value, max_length=_MAX_PROCESS_CODE_LENGTH)
    if not _PROCESS_CODE.fullmatch(process_code):
        raise ValueError("process_code is invalid")
    return process_code


def _required_bounded_text(value: object, *, max_length: int) -> str:
    normalized = _optional_bounded_text(value, max_length=max_length)
    if normalized is None:
        raise ValueError
    return normalized


def _optional_bounded_text(value: object, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError
    return normalized


def _optional_form_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_FORM_COMPONENT_VALUE_LENGTH:
        raise ValueError
    return value


def _is_definitive_create_rejection(
    *,
    http_status: int | None,
    upstream_code: str | None,
) -> bool:
    return bool(
        http_status is not None
        and 400 <= http_status < 500
        and http_status not in {401, 403, 408, 425, 429}
        and _is_known_create_rejection_code(upstream_code)
    )


def _is_known_create_rejection_code(value: object) -> bool:
    return bool(
        isinstance(value, str) and value.strip().casefold() in _CREATE_REJECTED_UPSTREAM_CODES
    )


def _safe_payload_code(payload: dict[str, Any]) -> str | None:
    value = payload.get("code", payload.get("errcode"))
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    return normalized if _SAFE_UPSTREAM_CODE.fullmatch(normalized) else None


def _required_text(value: object) -> str:
    normalized = _optional_text(value)
    if not normalized:
        raise ValueError
    return normalized


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _is_invalid_process_code(value: object) -> bool:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return normalized in _INVALID_PROCESS_CODE_NAMES or normalized.startswith("invalidparameter")


def _is_invalid_instance_list_process_code(value: object) -> bool:
    return bool(isinstance(value, str) and value.strip().casefold() == "invalidprocesscode")


def _process_code_error() -> ApiError:
    return ApiError(
        "OA_TEMPLATE_PROCESS_CODE_INVALID",
        "未找到对应的钉钉审批模板，请检查 processCode 是否正确且模板已发布",
        400,
    )


def _schema_error() -> ApiError:
    return ApiError(
        "DINGTALK_FORM_SCHEMA_INVALID",
        "钉钉审批模板返回的数据无效，请联系管理员",
        502,
    )


def _instance_list_error() -> ApiError:
    return ApiError(
        "DINGTALK_WORKFLOW_LIST_INVALID",
        "钉钉返回的审批列表数据无效，请稍后重试",
        502,
    )


def _instance_detail_error() -> ApiError:
    return ApiError(
        "DINGTALK_WORKFLOW_INSTANCE_INVALID",
        "钉钉返回的审批详情数据无效，请稍后重试",
        502,
    )


def _stored_schema_error() -> ApiError:
    return ApiError(
        "INVALID_SYSTEM_CONFIGURATION",
        "审批模板配置无效，请联系管理员重新确认",
        500,
    )
