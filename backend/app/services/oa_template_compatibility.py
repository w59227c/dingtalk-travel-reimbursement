from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from app.integrations.dingtalk.workflow import FormComponent, FormSchema


class SchemaChangeKind(StrEnum):
    EXACT = "EXACT"
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"


def classify_schema_change(
    configured: FormSchema,
    observed: FormSchema,
    *,
    mapped_component_ids: set[str],
) -> SchemaChangeKind:
    """Classify a live schema without treating option/catalog churn as structure drift.

    Full fingerprints remain useful audit identities, but they also include
    ``modifiedAt`` and select options.  Those values are intentionally excluded
    from this structural contract.  A caller must still validate that any
    selected option values remain present in the observed schema.
    """

    if configured.fingerprint == observed.fingerprint:
        return SchemaChangeKind.EXACT
    if (
        configured.process_code != observed.process_code
        or configured.form_code != observed.form_code
        or configured.form_uuid != observed.form_uuid
        or configured.status != observed.status
    ):
        return SchemaChangeKind.INCOMPATIBLE

    if schema_contract_fingerprint(
        configured,
        mapped_component_ids=mapped_component_ids,
    ) != schema_contract_fingerprint(
        observed,
        mapped_component_ids=mapped_component_ids,
    ):
        return SchemaChangeKind.INCOMPATIBLE
    return SchemaChangeKind.COMPATIBLE


def schema_contract_fingerprint(
    schema: FormSchema,
    *,
    mapped_component_ids: set[str],
) -> str:
    """Return the compact structural contract used by locked submissions.

    Option labels/catalogs and the remote ``modifiedAt`` value intentionally do
    not participate.  Field labels and business aliases do: they are sent to OA
    and checked during strict readback, so changing either requires a new lock.
    """

    components = {item.component_id: item for item in schema.components}
    payload = {
        "processCode": schema.process_code,
        "formCode": schema.form_code,
        "formUuid": schema.form_uuid,
        "status": schema.status,
        "mappedComponents": [
            {
                "componentId": component_id,
                "contract": (
                    _component_contract(component)
                    if (component := components.get(component_id)) is not None
                    else None
                ),
            }
            for component_id in sorted(mapped_component_ids)
        ],
        "unmappedRequiredIds": sorted(
            _unmapped_required_ids(schema, mapped_component_ids)
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _component_contract(component: FormComponent) -> tuple[object, ...]:
    return (
        component.label,
        component.biz_alias,
        component.component_type,
        component.required,
        component.disabled,
        component.hidden,
        component.ancestor_disabled,
        component.ancestor_hidden,
        component.nested,
        component.in_subtable,
        component.unsupported_container_ancestor,
        component.value_format,
        component.unit,
        component.parent_component_id,
        (
            component.related_template_policy.mode,
            component.related_template_policy.process_codes,
        )
        if component.related_template_policy is not None
        else None,
    )


def _unmapped_required_ids(schema: FormSchema, mapped_component_ids: set[str]) -> set[str]:
    return {
        component.component_id
        for component in schema.components
        if component.component_id not in mapped_component_ids
        and not component.in_subtable
        and component.required
        and not component.disabled
        and not component.hidden
        and not component.ancestor_disabled
        and not component.ancestor_hidden
    }
