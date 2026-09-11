from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_oa_reimbursement_payload import _component, _schema, _source
from test_travel_approvals import FakeWorkflow, _catalog, _instance, _profile, _window

from app.api.oa_templates import TravelCatalogConfirmation
from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import FormOption, WorkflowFormValue, WorkflowInstanceIdPage
from app.services.oa_reimbursement_payload import build_snapshot, parse_snapshot, serialize_snapshot
from app.services.oa_template_profiles import (
    _deserialize_travel_profiles,
    _serialize_travel_profiles,
    validate_travel_type_mappings,
)
from app.services.subsidy_calculation import subsidy_purpose_for_travel_type
from app.services.travel_approvals import (
    TravelApprovalSelection,
    list_current_user_travel_approvals,
    reverify_travel_approval_selection,
    travel_accounting_options,
    travel_instance_type_option,
)


def _dynamic_source():
    source = _source()
    profile = source.catalog.travel_profiles[0]
    source_options = (
        FormOption("商务出差", "商务出差", "business"),
        FormOption("公司内部出差（长期）", "公司内部出差（长期）", "internal"),
    )
    target = profile.travel_type_option
    profile = replace(
        profile,
        schema=_schema(
            profile.process_code,
            (
                *profile.schema.components,
                _component("source-type", "DDSelectField", "出差类别", options=source_options),
            ),
        ),
        travel_type_component_id="source-type",
        travel_type_mappings={option.value: target for option in source_options},
    )
    related = replace(
        source.related_approvals[0],
        schema_fingerprint=profile.schema.fingerprint,
        source_travel_type_value="商务出差",
    )
    return replace(
        source,
        catalog=replace(source.catalog, travel_profiles=(profile,)),
        related_approvals=(related,),
    )


@pytest.mark.parametrize(
    ("source_label", "target_label", "expected_policy"),
    [
        ("商务出差", "境内商务出差", "business"),
        ("市外项目出差（长期）", "境内市外项目出差（长期）", "project"),
        ("市外项目出差（短期）", "境内市外项目出差（短期）", "project"),
        ("市内项目出差（长/短期）", "境内市内项目出差（长/短期）", "same_city_project"),
        ("公司内部出差（长期）", "公司内部出差（长期）", "internal"),
        ("公司内部出差（短期）", "公司内部出差（短期）", "internal"),
        ("境外长期出差", "境外商务出差（长期）", "overseas"),
        ("境外短期出差", "境外商务出差（短期）", "overseas"),
    ],
)
def test_source_travel_categories_map_to_reimbursement_categories(
    source_label,
    target_label,
    expected_policy,
):
    source_option = FormOption(source_label, source_label, f"source-{expected_policy}")
    target_option = FormOption(target_label, target_label, f"target-{expected_policy}")
    source_schema = SimpleNamespace(
        components=(SimpleNamespace(component_id="source-type", options=(source_option,)),)
    )
    instance = replace(
        _instance("source-category"),
        form_values=(
            WorkflowFormValue(
                "source-type",
                "出差类别",
                "DDSelectField",
                source_label,
                None,
                None,
            ),
        ),
    )

    mapped, canonical_source, reason = travel_instance_type_option(
        instance,
        source_schema=source_schema,
        component_id="source-type",
        mappings={source_label: target_option},
        fixed_option=target_option,
    )

    assert reason is None
    assert mapped == target_option
    assert canonical_source == source_label
    assert subsidy_purpose_for_travel_type(target_label).value == expected_policy


def test_dynamic_config_roundtrip_and_api_contract():
    source = _dynamic_source()
    profile = source.catalog.travel_profiles[0]
    raw = _serialize_travel_profiles([profile])
    restored = _deserialize_travel_profiles(
        raw,
        reimbursement_schema=source.catalog.reimbursement.schema,
        reimbursement_mapping=source.catalog.reimbursement.mappings,
        allow_empty=False,
    )
    assert restored == [profile]
    parsed = TravelCatalogConfirmation.model_validate(
        {
            "profileKey": profile.profile_key,
            "displayName": profile.display_name,
            "processCode": profile.process_code,
            "schemaFingerprint": profile.schema.fingerprint,
            "mappings": profile.mappings,
            "travelTypeOption": profile.travel_type_option.as_dict(),
            "travelTypeMappings": {
                key: option.as_dict() for key, option in profile.travel_type_mappings.items()
            },
        }
    ).as_confirmation()
    assert parsed.travel_type_mappings == profile.travel_type_mappings
    assert parsed.mappings["travelType"] == "source-type"
    with pytest.raises(ApiError):
        validate_travel_type_mappings(
            profile.schema,
            profile.mappings,
            {},
            source.catalog.reimbursement.schema,
            source.catalog.reimbursement.mappings,
        )


def test_dynamic_snapshot_roundtrip_uses_current_version():
    snapshot = build_snapshot(_dynamic_source())
    assert snapshot.snapshot_version == 6
    assert parse_snapshot(serialize_snapshot(snapshot)) == snapshot
    assert snapshot.related_approvals[0].source_travel_type_value == "商务出差"


@pytest.mark.asyncio
async def test_two_types_in_one_template_resolve_per_instance_and_missing_is_unavailable():
    profile = _profile("domestic", "PROC-DOMESTIC")
    business = FormOption("境内商务出差", "境内商务出差", "b")
    internal = FormOption("公司内部出差", "公司内部出差", "i")
    profile.travel_type_component_id = "source-type"
    profile.travel_type_mappings = {"商务出差": business, "公司内部出差（长期）": internal}
    profile.schema.components = (
        SimpleNamespace(
            component_id="source-type",
            options=(
                FormOption("商务出差", "商务出差", "source-b"),
                FormOption("公司内部出差（长期）", "公司内部出差（长期）", "source-i"),
            ),
        ),
    )
    details = {}
    for key, raw in (("a", "source-b"), ("b", "公司内部出差（长期）"), ("c", "未知")):
        item = _instance(key)
        details[key] = replace(
            item,
            form_values=(
                *item.form_values,
                WorkflowFormValue("source-type", "类别", "DDSelectField", raw, None, None),
            ),
        )
    workflow = FakeWorkflow(
        {("PROC-DOMESTIC", 0): WorkflowInstanceIdPage(tuple(details), None)}, details
    )
    candidates = await list_current_user_travel_approvals(
        workflow, _catalog(profile), current_user_id="employee-1", query_window=_window()
    )
    by_id = {item.instance.instance_id: item for item in candidates}
    assert by_id["a"].listed.travel_type_option == business
    assert by_id["a"].as_dict()["sourceTravelTypeValue"] == "商务出差"
    assert by_id["b"].listed.travel_type_option == internal
    assert by_id["c"].unavailable_reason
    selections = tuple(TravelApprovalSelection("domestic", key, _window()) for key in ("a", "b"))
    with pytest.raises(ApiError) as error:
        await reverify_travel_approval_selection(
            workflow, _catalog(profile), current_user_id="employee-1", selections=selections
        )
    assert error.value.code == "TRAVEL_APPROVAL_TYPE_MISMATCH"
    profile.travel_type_mappings["公司内部出差（长期）"] = business
    with pytest.raises(ApiError) as same_target_error:
        await reverify_travel_approval_selection(
            workflow, _catalog(profile), current_user_id="employee-1", selections=selections
        )
    assert same_target_error.value.code == "TRAVEL_APPROVAL_TYPE_MISMATCH"
    details["b"] = replace(
        details["b"],
        form_values=tuple(
            replace(value, value="source-b") if value.component_id == "source-type" else value
            for value in details["b"].form_values
        ),
    )
    verified = await reverify_travel_approval_selection(
        workflow, _catalog(profile), current_user_id="employee-1", selections=selections
    )
    assert verified.travel_type_option == business


@pytest.mark.asyncio
async def test_worker_rejects_changed_source_even_when_target_is_same(monkeypatch):
    from app.services.oa_reimbursement import SnapshotSubmissionMaterializer
    from app.services.oa_reimbursement_payload import snapshot_sha256

    source = _dynamic_source()
    snapshot = build_snapshot(source)
    profile = source.catalog.travel_profiles[0]
    item = snapshot.related_approvals[0]
    instance = replace(
        _instance(item.process_instance_id, user_id=snapshot.identity.user_id),
        form_values=(
            WorkflowFormValue(
                "travel-start", "开始", "DDDateField", str(item.start_date), None, None
            ),
            WorkflowFormValue("travel-end", "结束", "DDDateField", str(item.end_date), None, None),
            WorkflowFormValue(
                "source-type", "类别", "DDSelectField", "公司内部出差（长期）", None, None
            ),
        ),
    )

    class Workflow:
        async def get_form_schema(self, code):
            return (
                profile.schema
                if code == profile.process_code
                else source.catalog.reimbursement.schema
            )

        async def get_process_instance(self, instance_id):
            return instance

        async def list_process_instance_ids(self, **kwargs):
            return WorkflowInstanceIdPage((item.process_instance_id,), None)

    monkeypatch.setattr("app.services.oa_reimbursement.verify_excel_template", lambda *args: None)
    job = SimpleNamespace(
        draft_id=snapshot.draft_id,
        process_code=snapshot.template.process_code,
        originator_user_id=snapshot.identity.user_id,
        originator_union_id=snapshot.identity.union_id,
        department_id=int(snapshot.identity.department_id),
        snapshot_json=serialize_snapshot(snapshot),
        snapshot_sha256=snapshot_sha256(snapshot),
        uploads=(),
    )
    materializer = SnapshotSubmissionMaterializer(
        workflow=Workflow(), staging=None, excel_template_path="unused-in-test.xlsx"
    )

    async def heartbeat():
        pass

    with pytest.raises(ApiError) as error:
        await materializer.validate(job, heartbeat=heartbeat)
    assert error.value.code == "TRAVEL_APPROVAL_TYPE_CHANGED"


@pytest.mark.parametrize(
    "labels,expected",
    [
        (("26007项目",), True),
        (("26007\t项目",), True),
        (("26007 其他项目",), False),
        (("26007项目", "26007 项目"), False),
    ],
)
def test_budget_full_label_whitespace_matching_never_prefix_or_ambiguous(labels, expected):
    company, budget, reason = travel_accounting_options(
        _instance("a"),
        source_schema=None,
        company_component_id="source-company",
        budget_code_component_id="source-budget",
        company_options=_catalog().reimbursement.schema.components[0].options,
        budget_options=tuple(
            FormOption(label, label, str(index)) for index, label in enumerate(labels)
        ),
    )
    assert (reason is None) is expected
    assert (budget is not None) is expected


def test_same_budget_value_with_different_full_label_is_not_a_match():
    instance = _instance("a")
    source_schema = SimpleNamespace(
        components=(
            SimpleNamespace(
                component_id="source-budget", options=(FormOption("26007", "26007 项目", "source"),)
            ),
        )
    )
    _company, budget, reason = travel_accounting_options(
        instance,
        source_schema=source_schema,
        company_component_id="source-company",
        budget_code_component_id="source-budget",
        company_options=_catalog().reimbursement.schema.components[0].options,
        budget_options=(FormOption("26007", "26007 另一个项目", "target"),),
    )
    assert budget is None
    assert reason
