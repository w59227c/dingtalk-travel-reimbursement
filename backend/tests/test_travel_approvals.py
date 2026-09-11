from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import (
    FormOption,
    WorkflowFormValue,
    WorkflowInstanceIdPage,
    WorkflowProcessInstance,
)
from app.services.travel_approvals import (
    TravelApprovalQueryWindow,
    TravelApprovalSelection,
    list_current_user_travel_approvals,
    requested_query_window,
    reverify_travel_approval_selection,
    runtime_options,
    travel_accounting_options,
    travel_source_component_id,
)


def _profile(
    key: str,
    process_code: str,
    *,
    travel_type: str = "市外项目出差（短期）",
):
    return SimpleNamespace(
        profile_key=key,
        display_name=f"{key}出差",
        process_code=process_code,
        schema=SimpleNamespace(fingerprint=(key[0] * 64)),
        start_date_component_id="start-id",
        end_date_component_id="end-id",
        company_component_id="source-company",
        budget_code_component_id="source-budget",
        travel_type_option=FormOption(
            value=travel_type,
            label=travel_type,
            key=f"option-{key}",
        ),
    )


def _catalog(*profiles):
    companies = (
        FormOption(value="北京", label="北京公司", key="company-1"),
        FormOption(value="无锡", label="无锡公司", key="company-2"),
    )
    budgets = (FormOption(value="26007", label="26007 项目", key="budget-1"),)
    reimbursement = SimpleNamespace(
        process_code="PROC-REIMBURSEMENT",
        mappings={"company": "company-id", "budgetCode": "budget-id"},
        schema=SimpleNamespace(
            components=(
                SimpleNamespace(component_id="company-id", options=companies),
                SimpleNamespace(component_id="budget-id", options=budgets),
            )
        ),
    )
    return SimpleNamespace(
        config_version=7,
        reimbursement=reimbursement,
        travel_profiles=tuple(profiles),
    )


def _instance(
    instance_id: str,
    *,
    user_id: str = "employee-1",
    status: str = "COMPLETED",
    result: str | None = "agree",
    start_date: str | None = "2026-08-10",
    end_date: str | None = "2026-08-12",
    title: str = "测试员工提交的境内出差申请",
    business_id: str | None = None,
) -> WorkflowProcessInstance:
    values = [
        WorkflowFormValue("source-company", "所属公司", "DDSelectField", "北京公司", None, None),
        WorkflowFormValue("source-budget", "预算代码", "DDSelectField", "26007 项目", None, None),
    ]
    if start_date is not None:
        values.append(
            WorkflowFormValue(
                component_id="start-id",
                name="开始日期",
                component_type="DDDateField",
                value=start_date,
                ext_value=None,
                biz_alias=None,
            )
        )
    if end_date is not None:
        values.append(
            WorkflowFormValue(
                component_id="end-id",
                name="结束日期",
                component_type="DDDateField",
                value=end_date,
                ext_value=None,
                biz_alias=None,
            )
        )
    return WorkflowProcessInstance(
        instance_id=instance_id,
        title=title,
        business_id=business_id or f"BIZ-{instance_id}",
        originator_user_id=user_id,
        originator_department_id="100",
        status=status,
        result=result,
        created_at="2026-08-01T08:00:00+08:00",
        finished_at="2026-08-02T08:00:00+08:00",
        form_values=tuple(values),
    )


def _itinerary_instance(
    instance_id: str,
    value: object,
) -> WorkflowProcessInstance:
    return replace(
        _instance(instance_id, start_date=None, end_date=None),
        form_values=(
            *_instance(instance_id, start_date=None, end_date=None).form_values,
            WorkflowFormValue(
                component_id="itinerary-id",
                name="行程",
                component_type="TableField",
                value=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                ext_value=None,
                biz_alias="itinerary",
            ),
        ),
    )


class FakeWorkflow:
    def __init__(self, pages, details, *, detail_delay: float = 0) -> None:
        self.pages = pages
        self.details = details
        self.detail_delay = detail_delay
        self.list_calls: list[dict[str, object]] = []
        self.detail_calls: list[str] = []
        self.active_details = 0
        self.maximum_active_details = 0

    async def list_process_instance_ids(self, **kwargs):
        self.list_calls.append(kwargs)
        page = self.pages[(kwargs["process_code"], kwargs["next_token"])]
        return page() if callable(page) else page

    async def get_process_instance(self, instance_id: str):
        self.detail_calls.append(instance_id)
        self.active_details += 1
        self.maximum_active_details = max(
            self.maximum_active_details,
            self.active_details,
        )
        try:
            if self.detail_delay:
                await asyncio.sleep(self.detail_delay)
            return self.details[instance_id]
        finally:
            self.active_details -= 1


def _window() -> TravelApprovalQueryWindow:
    return TravelApprovalQueryWindow.from_dates(date(2026, 5, 1), date(2026, 8, 28))


def test_runtime_options_preserve_exact_oa_values_and_catalog_version() -> None:
    catalog = _catalog(_profile("domestic", "PROC-DOMESTIC"))

    result = runtime_options(catalog)

    assert result == {
        "templateConfigVersion": 7,
        "reimbursementProcessCode": "PROC-REIMBURSEMENT",
        "companyOptions": [
            {"value": "北京", "label": "北京公司", "key": "company-1"},
            {"value": "无锡", "label": "无锡公司", "key": "company-2"},
        ],
        "budgetCodeOptions": [{"value": "26007", "label": "26007 项目", "key": "budget-1"}],
        "travelProfiles": [
            {
                "profileKey": "domestic",
                "displayName": "domestic出差",
                "processCode": "PROC-DOMESTIC",
                "schemaFingerprint": "d" * 64,
                "travelTypeOption": {
                    "value": "市外项目出差（短期）",
                    "label": "市外项目出差（短期）",
                    "key": "option-domestic",
                },
                "subsidyTripType": "project",
            }
        ],
    }


def test_accounting_resolves_source_option_key_by_exact_label_across_forms() -> None:
    source_option = FormOption("source-value", "北京公司", "source-key")
    source_schema = SimpleNamespace(
        components=(
            SimpleNamespace(
                component_id="source-company",
                options=(source_option,),
            ),
        )
    )
    instance = _instance("trip")
    instance = replace(
        instance,
        form_values=tuple(
            replace(value, value="source-key") if value.component_id == "source-company" else value
            for value in instance.form_values
        ),
    )
    company, budget, reason = travel_accounting_options(
        instance,
        source_schema=source_schema,
        company_component_id="source-company",
        budget_code_component_id="source-budget",
        company_options=_catalog().reimbursement.schema.components[0].options,
        budget_options=_catalog().reimbursement.schema.components[1].options,
    )
    assert reason is None
    assert company.value == "北京"
    assert budget.value == "26007"


def test_unconfigured_source_mapping_requires_one_exact_visible_field_label() -> None:
    profile = _profile("domestic", "PROC-TRAVEL")
    profile.company_component_id = None
    component = SimpleNamespace(
        component_id="real-company", label="所属公司", component_type="DDSelectField"
    )
    profile.schema = SimpleNamespace(components=(component,))
    assert travel_source_component_id(profile, "company") == "real-company"
    profile.schema.components = (
        component,
        SimpleNamespace(component_id="ambiguous", label="所属公司", component_type="DDSelectField"),
    )
    assert travel_source_component_id(profile, "company") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing", "ambiguous_target", "unknown_target"])
async def test_unavailable_accounting_is_visible_but_cannot_be_selected(problem) -> None:
    profile = _profile("domestic", "PROC-A")
    catalog = _catalog(profile)
    instance = _instance("trip")
    if problem == "missing":
        instance = replace(
            instance,
            form_values=tuple(
                value for value in instance.form_values if value.component_id != "source-company"
            ),
        )
    elif problem == "ambiguous_target":
        catalog.reimbursement.schema.components[0].options += (
            FormOption("other-company", "北京公司", "ambiguous"),
        )
    else:
        instance = replace(
            instance,
            form_values=tuple(
                replace(value, value="unknown") if value.component_id == "source-company" else value
                for value in instance.form_values
            ),
        )
    workflow = FakeWorkflow(
        {("PROC-A", 0): WorkflowInstanceIdPage(("trip",), None)}, {"trip": instance}
    )
    candidates = await list_current_user_travel_approvals(
        workflow, catalog, current_user_id="employee-1", query_window=_window()
    )
    assert len(candidates) == 1
    assert "所属公司" in candidates[0].as_dict()["unavailableReason"]
    with pytest.raises(ApiError) as error:
        await reverify_travel_approval_selection(
            workflow,
            catalog,
            current_user_id="employee-1",
            selections=(TravelApprovalSelection("domestic", "trip", _window()),),
        )
    assert error.value.code == "TRAVEL_APPROVAL_FIELDS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_selection_rejects_different_companies_even_with_same_travel_type() -> None:
    catalog = _catalog(_profile("domestic", "PROC-A"))
    second = _instance("second")
    second = replace(
        second,
        form_values=tuple(
            replace(value, value="无锡公司") if value.component_id == "source-company" else value
            for value in second.form_values
        ),
    )
    workflow = FakeWorkflow(
        {("PROC-A", 0): WorkflowInstanceIdPage(("first", "second"), None)},
        {"first": _instance("first"), "second": second},
    )
    with pytest.raises(ApiError) as error:
        await reverify_travel_approval_selection(
            workflow,
            catalog,
            current_user_id="employee-1",
            selections=tuple(
                TravelApprovalSelection("domestic", key, _window()) for key in ("first", "second")
            ),
        )
    assert error.value.code == "TRAVEL_APPROVAL_ACCOUNTING_MISMATCH"


def test_query_window_defaults_to_180_calendar_days_and_rejects_a_larger_span() -> None:
    window = requested_query_window(None, None, today=date(2026, 9, 4))

    assert window.from_date == date(2026, 3, 9)
    assert window.to_date == date(2026, 9, 4)
    assert window.end_time_ms - window.start_time_ms < 180 * 24 * 60 * 60 * 1000

    with pytest.raises(ApiError) as caught:
        TravelApprovalQueryWindow.from_dates(date(2026, 3, 8), date(2026, 9, 4))
    assert caught.value.code == "TRAVEL_APPROVAL_QUERY_WINDOW_INVALID"

    with pytest.raises(ApiError):
        requested_query_window(date(2026, 9, 1), None)


@pytest.mark.asyncio
async def test_listing_splits_a_180_day_window_into_dingtalk_safe_requests() -> None:
    class WindowWorkflow(FakeWorkflow):
        async def list_process_instance_ids(self, **kwargs):
            self.list_calls.append(kwargs)
            instance_id = "older" if len(self.list_calls) == 1 else "newer"
            return WorkflowInstanceIdPage((instance_id,), None)

    workflow = WindowWorkflow(
        {},
        {"older": _instance("older"), "newer": _instance("newer")},
    )
    window = requested_query_window(None, None, today=date(2026, 9, 4))

    candidates = await list_current_user_travel_approvals(
        workflow,
        _catalog(_profile("domestic", "PROC-A")),
        current_user_id="employee-1",
        query_window=window,
    )

    assert {item.instance.instance_id for item in candidates} == {"older", "newer"}
    assert len(workflow.list_calls) == 2
    first, second = workflow.list_calls
    assert [first["next_token"], second["next_token"]] == [0, 0]
    assert first["end_time"] + 1 == second["start_time"]
    assert all(
        call["end_time"] - call["start_time"] < 120 * 24 * 60 * 60 * 1000
        for call in workflow.list_calls
    )


@pytest.mark.asyncio
async def test_listing_pages_each_profile_uses_only_current_identity_and_filters_details() -> None:
    profiles = (_profile("domestic", "PROC-A"), _profile("abroad", "PROC-B"))
    workflow = FakeWorkflow(
        {
            ("PROC-A", 0): WorkflowInstanceIdPage(("valid-a", "other-user"), 20),
            ("PROC-A", 20): WorkflowInstanceIdPage(("running", "refused"), None),
            ("PROC-B", 0): WorkflowInstanceIdPage(("missing-date", "target-b"), None),
        },
        {
            "valid-a": _instance("valid-a"),
            "other-user": _instance("other-user", user_id="attacker"),
            "running": _instance("running", status="RUNNING"),
            "refused": _instance("refused", result="refuse"),
            "missing-date": _instance("missing-date", start_date=None),
            "target-b": _instance(
                "target-b",
                title="TARGET 出差",
                business_id="TARGET-2026",
            ),
        },
    )

    candidates = await list_current_user_travel_approvals(
        workflow,
        _catalog(*profiles),
        current_user_id="employee-1",
        query_window=_window(),
        query="target",
    )

    assert [item.instance.instance_id for item in candidates] == ["target-b"]
    assert candidates[0].listed.source_process_code == "PROC-B"
    assert candidates[0].as_dict()["companyOption"] == {
        "value": "北京",
        "label": "北京公司",
        "key": "company-1",
    }
    assert candidates[0].as_dict()["budgetCodeOption"] == {
        "value": "26007",
        "label": "26007 项目",
        "key": "budget-1",
    }
    assert candidates[0].as_dict()["unavailableReason"] is None
    assert not hasattr(candidates[0].instance, "process_code")
    assert len(workflow.list_calls) == 3
    assert all(call["user_ids"] == ("employee-1",) for call in workflow.list_calls)
    assert all(call["statuses"] == ("COMPLETED",) for call in workflow.list_calls)
    assert all(call["max_results"] == 20 for call in workflow.list_calls)
    assert set(workflow.detail_calls) == {
        "valid-a",
        "other-user",
        "running",
        "refused",
        "missing-date",
        "target-b",
    }


@pytest.mark.asyncio
async def test_native_itinerary_table_uses_earliest_start_and_latest_end() -> None:
    profile = _profile("native", "PROC-NATIVE")
    profile.start_date_component_id = "itinerary-id"
    profile.end_date_component_id = "itinerary-id"
    rows = [
        {
            "rowValue": [
                {
                    "bizAlias": "startTime",
                    "key": "DDDateField-J8TW2TVY",
                    "value": "2026-09-10 上午",
                },
                {
                    "bizAlias": "endTime",
                    "key": "DDDateField-J8TW2TVZ",
                    "value": "2026-09-18 下午",
                },
            ]
        },
        {
            "rowValue": [
                {
                    "bizAlias": "startTime",
                    "key": "DDDateField-J8TW2TVY",
                    "value": "2026-09-03 下午",
                },
                {
                    "bizAlias": "endTime",
                    "key": "DDDateField-J8TW2TVZ",
                    "value": "2026-09-05 上午",
                },
            ]
        },
    ]
    workflow = FakeWorkflow(
        {("PROC-NATIVE", 0): WorkflowInstanceIdPage(("native-trip",), None)},
        {"native-trip": _itinerary_instance("native-trip", rows)},
    )

    candidates = await list_current_user_travel_approvals(
        workflow,
        _catalog(profile),
        current_user_id="employee-1",
        query_window=_window(),
    )

    assert len(candidates) == 1
    assert candidates[0].start_date == date(2026, 9, 3)
    assert candidates[0].end_date == date(2026, 9, 18)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    (
        "not-json",
        [{"rowValue": []}],
        [
            {
                "rowValue": [
                    {
                        "bizAlias": "startTime",
                        "key": "DDDateField-J8TW2TVY",
                        "value": "2026-09-03 上午",
                    }
                ]
            }
        ],
    ),
)
async def test_native_itinerary_table_fails_closed_for_invalid_rows(value: object) -> None:
    profile = _profile("native", "PROC-NATIVE")
    profile.start_date_component_id = "itinerary-id"
    profile.end_date_component_id = "itinerary-id"
    workflow = FakeWorkflow(
        {("PROC-NATIVE", 0): WorkflowInstanceIdPage(("invalid-trip",), None)},
        {"invalid-trip": _itinerary_instance("invalid-trip", value)},
    )

    candidates = await list_current_user_travel_approvals(
        workflow,
        _catalog(profile),
        current_user_id="employee-1",
        query_window=_window(),
    )

    assert candidates == ()


@pytest.mark.asyncio
async def test_detail_reads_are_bounded_to_five_concurrent_requests() -> None:
    instance_ids = tuple(f"instance-{index}" for index in range(12))
    workflow = FakeWorkflow(
        {("PROC-A", 0): WorkflowInstanceIdPage(instance_ids, None)},
        {instance_id: _instance(instance_id) for instance_id in instance_ids},
        detail_delay=0.01,
    )

    candidates = await list_current_user_travel_approvals(
        workflow,
        _catalog(_profile("domestic", "PROC-A")),
        current_user_id="employee-1",
        query_window=_window(),
    )

    assert len(candidates) == 12
    assert workflow.maximum_active_details == 5


@pytest.mark.asyncio
async def test_first_detail_failure_cancels_and_awaits_all_other_detail_tasks() -> None:
    instance_ids = ("slow-0", "slow-1", "slow-2", "slow-3", "failure", "queued-5")

    class FailingWorkflow(FakeWorkflow):
        def __init__(self) -> None:
            super().__init__(
                {("PROC-A", 0): WorkflowInstanceIdPage(instance_ids, None)},
                {},
            )
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled: set[str] = set()

        async def get_process_instance(self, instance_id: str):
            self.detail_calls.append(instance_id)
            self.active_details += 1
            self.maximum_active_details = max(
                self.maximum_active_details,
                self.active_details,
            )
            if self.active_details == 5:
                self.started.set()
            try:
                if instance_id == "failure":
                    await self.started.wait()
                    raise ApiError("DETAIL_FAILED", "详情读取失败", 502)
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.add(instance_id)
                    raise
                raise AssertionError("cancelled detail task resumed unexpectedly")
            finally:
                self.active_details -= 1

    workflow = FailingWorkflow()

    with pytest.raises(ApiError) as caught:
        await list_current_user_travel_approvals(
            workflow,
            _catalog(_profile("domestic", "PROC-A")),
            current_user_id="employee-1",
            query_window=_window(),
        )

    assert caught.value.code == "DETAIL_FAILED"
    assert workflow.maximum_active_details == 5
    assert workflow.active_details == 0
    assert set(workflow.detail_calls) == {
        "slow-0",
        "slow-1",
        "slow-2",
        "slow-3",
        "failure",
        "queued-5",
    }
    assert workflow.cancelled == {
        "slow-0",
        "slow-1",
        "slow-2",
        "slow-3",
        "queued-5",
    }
    await asyncio.sleep(0)
    assert workflow.active_details == 0


@pytest.mark.asyncio
async def test_duplicate_instance_across_profiles_fails_before_detail_reads() -> None:
    workflow = FakeWorkflow(
        {
            ("PROC-A", 0): WorkflowInstanceIdPage(("duplicate",), None),
            ("PROC-B", 0): WorkflowInstanceIdPage(("duplicate",), None),
        },
        {"duplicate": _instance("duplicate")},
    )

    with pytest.raises(ApiError) as caught:
        await list_current_user_travel_approvals(
            workflow,
            _catalog(
                _profile("domestic", "PROC-A"),
                _profile("abroad", "PROC-B"),
            ),
            current_user_id="employee-1",
            query_window=_window(),
        )

    assert caught.value.code == "TRAVEL_APPROVAL_SOURCE_AMBIGUOUS"
    assert workflow.detail_calls == []


@pytest.mark.asyncio
async def test_listing_over_global_cap_fails_without_starting_detail_reads() -> None:
    pages = {}
    details = {}
    for page_number in range(11):
        token = page_number * 20
        count = 20 if page_number < 10 else 1
        instance_ids = tuple(f"instance-{token + index}" for index in range(count))
        pages[("PROC-A", token)] = WorkflowInstanceIdPage(
            instance_ids,
            token + 20 if page_number < 10 else None,
        )
        details.update({instance_id: _instance(instance_id) for instance_id in instance_ids})
    workflow = FakeWorkflow(pages, details)

    with pytest.raises(ApiError) as caught:
        await list_current_user_travel_approvals(
            workflow,
            _catalog(_profile("domestic", "PROC-A")),
            current_user_id="employee-1",
            query_window=_window(),
        )

    assert caught.value.code == "TRAVEL_APPROVAL_RESULT_LIMIT_EXCEEDED"
    assert workflow.detail_calls == []


@pytest.mark.asyncio
async def test_reverification_proves_membership_and_aggregates_one_travel_type() -> None:
    window = _window()
    workflow = FakeWorkflow(
        {
            ("PROC-A", 0): WorkflowInstanceIdPage(("first",), None),
            ("PROC-B", 0): WorkflowInstanceIdPage(("second",), None),
        },
        {
            "first": _instance("first", start_date="2026-08-10", end_date="2026-08-12"),
            "second": _instance(
                "second",
                start_date="2026-08-01",
                end_date="2026-08-20",
            ),
        },
    )

    result = await reverify_travel_approval_selection(
        workflow,
        _catalog(
            _profile("domestic", "PROC-A"),
            _profile("project", "PROC-B"),
        ),
        current_user_id="employee-1",
        selections=(
            TravelApprovalSelection("domestic", "first", window),
            TravelApprovalSelection("project", "second", window),
        ),
    )

    assert result.start_date == date(2026, 8, 1)
    assert result.end_date == date(2026, 8, 20)
    assert result.department_id == "100"
    assert result.travel_type_option.value == "市外项目出差（短期）"
    assert [item.instance.instance_id for item in result.approvals] == ["first", "second"]


@pytest.mark.asyncio
async def test_reverification_allows_a_gap_between_selected_approvals() -> None:
    window = _window()
    workflow = FakeWorkflow(
        {("PROC-A", 0): WorkflowInstanceIdPage(("first", "second"), None)},
        {
            "first": _instance("first", start_date="2026-08-01", end_date="2026-08-02"),
            "second": _instance("second", start_date="2026-08-04", end_date="2026-08-05"),
        },
    )

    result = await reverify_travel_approval_selection(
        workflow,
        _catalog(_profile("domestic", "PROC-A")),
        current_user_id="employee-1",
        selections=(
            TravelApprovalSelection("domestic", "first", window),
            TravelApprovalSelection("domestic", "second", window),
        ),
    )

    assert [item.instance.instance_id for item in result.approvals] == ["first", "second"]
    assert result.start_date == date(2026, 8, 1)
    assert result.end_date == date(2026, 8, 5)


@pytest.mark.asyncio
async def test_reverification_rejects_approvals_from_different_departments() -> None:
    window = _window()
    workflow = FakeWorkflow(
        {("PROC-A", 0): WorkflowInstanceIdPage(("first", "second"), None)},
        {
            "first": _instance("first"),
            "second": replace(_instance("second"), originator_department_id="200"),
        },
    )

    with pytest.raises(ApiError) as caught:
        await reverify_travel_approval_selection(
            workflow,
            _catalog(_profile("domestic", "PROC-A")),
            current_user_id="employee-1",
            selections=(
                TravelApprovalSelection("domestic", "first", window),
                TravelApprovalSelection("domestic", "second", window),
            ),
        )

    assert caught.value.code == "TRAVEL_APPROVAL_DEPARTMENT_MISMATCH"


@pytest.mark.asyncio
async def test_reverification_rejects_an_approval_outside_the_draft_department() -> None:
    window = _window()
    workflow = FakeWorkflow(
        {("PROC-A", 0): WorkflowInstanceIdPage(("trip",), None)},
        {"trip": _instance("trip")},
    )

    with pytest.raises(ApiError) as caught:
        await reverify_travel_approval_selection(
            workflow,
            _catalog(_profile("domestic", "PROC-A")),
            current_user_id="employee-1",
            selections=(TravelApprovalSelection("domestic", "trip", window),),
            expected_department_id="200",
        )

    assert caught.value.code == "TRAVEL_APPROVAL_DEPARTMENT_MISMATCH"


@pytest.mark.asyncio
async def test_reverification_rejects_mixed_travel_types() -> None:
    window = _window()
    workflow = FakeWorkflow(
        {
            ("PROC-A", 0): WorkflowInstanceIdPage(("first",), None),
            ("PROC-B", 0): WorkflowInstanceIdPage(("second",), None),
        },
        {"first": _instance("first"), "second": _instance("second")},
    )

    with pytest.raises(ApiError) as caught:
        await reverify_travel_approval_selection(
            workflow,
            _catalog(
                _profile("domestic", "PROC-A", travel_type="境内出差"),
                _profile("abroad", "PROC-B", travel_type="境外出差"),
            ),
            current_user_id="employee-1",
            selections=(
                TravelApprovalSelection("domestic", "first", window),
                TravelApprovalSelection("abroad", "second", window),
            ),
        )

    assert caught.value.code == "TRAVEL_APPROVAL_TYPE_MISMATCH"


@pytest.mark.asyncio
async def test_reverification_rejects_an_instance_outside_its_claimed_profile() -> None:
    window = _window()
    workflow = FakeWorkflow(
        {
            ("PROC-A", 0): WorkflowInstanceIdPage((), None),
            ("PROC-B", 0): WorkflowInstanceIdPage(("instance-1",), None),
        },
        {"instance-1": _instance("instance-1")},
    )

    with pytest.raises(ApiError) as caught:
        await reverify_travel_approval_selection(
            workflow,
            _catalog(
                _profile("domestic", "PROC-A"),
                _profile("project", "PROC-B"),
            ),
            current_user_id="employee-1",
            selections=(TravelApprovalSelection("domestic", "instance-1", window),),
        )

    assert caught.value.code == "TRAVEL_APPROVAL_MEMBERSHIP_CHANGED"


def test_query_window_value_cannot_be_forged_after_validation() -> None:
    valid = _window()
    forged = TravelApprovalQueryWindow(
        from_date=valid.from_date,
        to_date=valid.to_date,
        start_time_ms=0,
        end_time_ms=1,
    )
    workflow = FakeWorkflow({}, {})

    with pytest.raises(ApiError) as caught:
        asyncio.run(
            list_current_user_travel_approvals(
                workflow,
                _catalog(_profile("domestic", "PROC-A")),
                current_user_id="employee-1",
                query_window=forged,
            )
        )

    assert caught.value.code == "TRAVEL_APPROVAL_QUERY_WINDOW_INVALID"
    assert workflow.list_calls == []
