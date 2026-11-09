from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from conftest import mock_login

from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import (
    FormOption,
    WorkflowFormValue,
    WorkflowInstanceIdPage,
    WorkflowProcessInstance,
)
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftRelatedApproval,
    ReimbursementDraftStatus,
    ReimbursementOcrStatus,
    ReimbursementSubmission,
    ReimbursementSubmissionStatus,
    ReimbursementUpload,
    utc_now,
)
from app.schemas.reimbursements import ReimbursementDraftInput, RelatedApprovalSelectionInput
from app.services import reimbursement_drafts


def _catalog():
    return SimpleNamespace(
        config_version=12,
        reimbursement=SimpleNamespace(
            process_code="PROC-REIMBURSEMENT",
            mappings={"company": "company-id", "budgetCode": "budget-id"},
            schema=SimpleNamespace(
                fingerprint="a" * 64,
                components=(
                    SimpleNamespace(
                        component_id="company-id",
                        options=(
                            FormOption(
                                value="北京",
                                label="北京公司",
                                key="company-beijing",
                            ),
                        ),
                    ),
                    SimpleNamespace(
                        component_id="budget-id",
                        options=(
                            FormOption(
                                value="26007",
                                label="MES 项目",
                                key="budget-26007",
                            ),
                        ),
                    ),
                ),
            ),
        ),
        travel_profiles=(),
    )


def _catalog_with_travel(*, travel_type: str = "境内商务出差"):
    catalog = _catalog()
    catalog.travel_profiles = (
        SimpleNamespace(
            profile_key="domestic",
            display_name="境内出差",
            process_code="PROC-DOMESTIC",
            schema=SimpleNamespace(fingerprint="d" * 64),
            start_date_component_id="travel-start",
            end_date_component_id="travel-end",
            company_component_id="source-company",
            budget_code_component_id="source-budget",
            travel_type_option=FormOption(
                value=travel_type,
                label=travel_type,
                key="travel-domestic",
            ),
        ),
    )
    return catalog


def _input() -> dict[str, object]:
    return {
        "ocrDispositionVersion": 1,
        "companyValue": "北京",
        "budgetCodeValue": "26007",
        "project": {"mode": "manual", "text": "MES 项目"},
        "trip": {
            "tripType": "business",
            "startDate": "2026-09-01",
            "startTime": "09:00",
            "endDate": "2026-09-02",
            "endTime": "18:00",
        },
        "items": [
            {
                "category": "other",
                "date": "2026-09-01",
                "displayDate": "2026-09-01",
                "description": "打车费",
                "amount": "44.89",
                "receiptCount": 1,
                "itineraryFileIds": [],
                "itineraryAutoMatchDisabled": False,
                "paymentProofFileIds": [],
                "hotelBillFileIds": [],
                "railType": "unknown",
                "requiresItinerary": False,
                "cnyAmountConfirmed": False,
                "requiresCnyConfirmation": False,
            }
        ],
        "dismissedOcrFileIds": [],
    }


def _without_accounting(value):
    return {
        **value,
        "companyValue": "",
        "budgetCodeValue": "",
        "project": None,
        "accountingSourceVerified": False,
    }


def _install_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: _catalog(),
    )


def _assert_unresolved_provenance(client, draft_id: str) -> None:
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        with pytest.raises(ApiError, match="请确认每张已识别票据"):
            reimbursement_drafts.validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=ReimbursementDraftInput.model_validate_json(draft.input_json),
                require_terminal_disposition=True,
            )


def _replacement_input(client):
    current = client.get("/api/reimbursements/workspace").json()["data"]["items"]
    return (
        {"replacesDraftId": current[0]["id"], "replacesDraftRevision": current[0]["revision"]}
        if current
        else {}
    )


def _create(client, headers: dict[str, str], body: dict[str, object] | None = None):
    return client.post(
        "/api/reimbursements/drafts",
        json={
            "expectedRevision": 0,
            "input": body or _input(),
            **_replacement_input(client),
        },
        headers=headers,
    )


def _selection(instance_id: str = "travel-1") -> dict[str, object]:
    return {
        "processInstanceId": instance_id,
        "profileKey": "domestic",
        "queryWindow": {"from": "2026-05-01", "to": "2026-08-28"},
    }


def _travel_instance(
    instance_id: str = "travel-1",
    *,
    start_date: str = "2026-09-01",
    end_date: str = "2026-09-02",
    department_id: str = "100",
) -> WorkflowProcessInstance:
    return WorkflowProcessInstance(
        instance_id=instance_id,
        title="当前员工提交的境内出差申请",
        business_id=f"BIZ-{instance_id}",
        originator_user_id="mock-user",
        originator_department_id=department_id,
        status="COMPLETED",
        result="agree",
        created_at="2026-08-01T08:00:00+08:00",
        finished_at="2026-08-02T08:00:00+08:00",
        form_values=(
            WorkflowFormValue(
                "source-company", "所属公司", "DDSelectField", "北京公司", None, None
            ),
            WorkflowFormValue("source-budget", "预算代码", "DDSelectField", "MES 项目", None, None),
            WorkflowFormValue(
                component_id="travel-start",
                name="开始日期",
                component_type="DDDateField",
                value=start_date,
                ext_value=None,
                biz_alias=None,
            ),
            WorkflowFormValue(
                component_id="travel-end",
                name="结束日期",
                component_type="DDDateField",
                value=end_date,
                ext_value=None,
                biz_alias=None,
            ),
        ),
    )


class FakeTravelWorkflow:
    def __init__(
        self,
        instance_ids: tuple[str, ...] = ("travel-1",),
        *,
        start_date: str = "2026-09-01",
        end_date: str = "2026-09-02",
        department_id: str = "100",
    ) -> None:
        self.instance_ids = instance_ids
        self.start_date = start_date
        self.end_date = end_date
        self.department_id = department_id
        self.list_calls: list[dict[str, object]] = []
        self.detail_calls: list[str] = []

    async def list_process_instance_ids(self, **kwargs):
        self.list_calls.append(kwargs)
        return WorkflowInstanceIdPage(self.instance_ids, None)

    async def get_process_instance(self, instance_id: str):
        self.detail_calls.append(instance_id)
        return _travel_instance(
            instance_id,
            start_date=self.start_date,
            end_date=self.end_date,
            department_id=self.department_id,
        )


def _add_active_file(
    client,
    draft_id: str,
    *,
    ocr_status: str = "COMPLETE",
    processing_role: str = ReimbursementDraftFileRole.EXPENSE_SOURCE.value,
    record_disposition: bool = True,
) -> str:
    with client.app.state.database_session_factory() as database:
        sort_order = (
            database.query(ReimbursementDraftFile)
            .filter(ReimbursementDraftFile.draft_id == draft_id)
            .count()
        )
        file = ReimbursementDraftFile(
            draft_id=draft_id,
            sort_order=sort_order,
            processing_role=processing_role,
            file_status=ReimbursementDraftFileStatus.ACTIVE.value,
            storage_key=f"drafts/{draft_id}/receipt-{sort_order}.pdf",
            part_storage_key=None,
            reserved_bytes=100,
            reservation_expires_at=None,
            original_name="发票.pdf",
            extension=".pdf",
            media_type="application/pdf",
            size_bytes=100,
            sha256="f" * 64,
            ocr_status=ocr_status,
            ocr_result_json=None,
        )
        database.add(file)
        database.flush()
        if ocr_status == ReimbursementOcrStatus.COMPLETE.value:
            file.ocr_result_json = json.dumps(
                {
                    "fileId": file.id,
                    "type": "taxi",
                    "categoryId": "other",
                    "categoryName": "其他",
                    "date": "2026-09-01",
                    "description": "打车费",
                    "amount": "44.89",
                    "receiptCount": 1,
                    "source": "ocr",
                    "confidence": "0.95",
                    "warnings": [],
                    "status": "recognized",
                    "error": None,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        elif ocr_status == ReimbursementOcrStatus.FAILED.value:
            file.ocr_result_json = json.dumps(
                {
                    "fileId": file.id,
                    "type": "other",
                    "categoryId": "other",
                    "categoryName": "其他",
                    "date": None,
                    "description": None,
                    "amount": None,
                    "receiptCount": 1,
                    "source": "ocr",
                    "confidence": "0.00",
                    "warnings": ["MANUAL_REVIEW_REQUIRED"],
                    "status": "failed",
                    "error": {"code": "OCR_FAILED", "message": "识别失败"},
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        if record_disposition and ocr_status in {
            ReimbursementOcrStatus.COMPLETE.value,
            ReimbursementOcrStatus.FAILED.value,
        }:
            draft = database.get(ReimbursementDraft, draft_id)
            assert draft is not None
            input_data = json.loads(draft.input_json)
            input_data["items"][0]["sourceFileId"] = file.id
            draft.input_json = json.dumps(
                input_data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        database.commit()
        return file.id


def test_create_list_read_and_update_draft_are_persistent_and_canonical(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}

    created = client.post(
        "/api/reimbursements/drafts",
        json={"expectedRevision": 0, "input": _input()},
        headers=headers,
    )

    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    assert draft["revision"] == 1
    assert draft["status"] == "DRAFT"
    assert draft["department"] == {"id": "100", "name": "测试部门"}
    assert draft["template"] == {
        "processCode": "PROC-REIMBURSEMENT",
        "configVersion": 12,
        "schemaFingerprint": "a" * 64,
    }
    assert draft["input"] == _without_accounting(_input())
    assert draft["totals"] == {
        "expenseTotal": "44.89",
        "subsidyTotal": "200.00",
        "totalAmount": "244.89",
        "receiptCount": 1,
        "uppercaseAmount": "贰佰肆拾肆元捌角玖分",
        "subsidy": {
            "tripType": "business",
            "calendarDays": 2,
            "effectiveDays": "2.0",
            "dailyRate": "100.00",
            "total": "200.00",
        },
        "subsidies": [
            {
                "tripType": "business",
                "calendarDays": 2,
                "effectiveDays": "2.0",
                "dailyRate": "100.00",
                "total": "200.00",
            }
        ],
    }
    draft_id = draft["id"]

    listed = client.get("/api/reimbursements/drafts")
    detail = client.get(f"/api/reimbursements/drafts/{draft_id}")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]["items"]] == [draft_id]
    assert detail.status_code == 200
    assert detail.json()["data"]["input"] == _without_accounting(_input())

    changed = _input()
    changed["items"][0]["amount"] = "50.00"
    updated = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 1, "input": changed},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["revision"] == 2
    assert updated.json()["data"]["totals"]["totalAmount"] == "250.00"

    with client.app.state.database_session_factory() as database:
        stored = database.get(reimbursement_drafts.ReimbursementDraft, draft_id)
        assert stored is not None
        assert stored.revision == 2
        assert stored.input_json == json.dumps(
            _without_accounting(changed),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def test_open_editable_draft_is_atomic_and_preserves_saved_content(
    client_factory, monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    blank = {
        "expectedRevision": 0,
        "reuseExisting": True,
        "input": {**_input(), "items": [], "trip": None},
    }
    barrier = Barrier(2)

    def open_draft():
        barrier.wait(timeout=5)
        return client.post(
            "/api/reimbursements/drafts",
            json=blank,
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(open_draft) for _ in range(2)]
        first, second = [request.result(timeout=10) for request in requests]
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    draft = first.json()["data"]
    assert second.json()["data"] == draft
    assert client.get("/api/reimbursements/drafts").json()["data"]["total"] == 1

    changed = _input()
    changed["items"][0]["amount"] = "50.00"
    updated = client.put(
        f"/api/reimbursements/drafts/{draft['id']}",
        json={"expectedRevision": 1, "input": changed},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    resumed = client.post(
        "/api/reimbursements/drafts",
        json=blank,
        headers=headers,
    )
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["data"] == updated.json()["data"]


def test_open_current_draft_skips_records_outside_current_scope(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    url = "/api/reimbursements/drafts"
    payload = {"expectedRevision": 0, "input": _input()}
    excluded_records = [
        {"corp_id": "different-corp"},
        {"owner_user_id": "different-employee"},
        {"department_id": "200"},
    ]
    for overrides in excluded_records:
        created = client.post(url, json=payload, headers=headers)
        assert created.status_code == 201, created.text
        with client.app.state.database_session_factory() as database:
            record = database.get(ReimbursementDraft, created.json()["data"]["id"])
            assert record is not None
            for field, value in overrides.items():
                setattr(record, field, value)
            database.commit()

    opened = client.post(url, json={**payload, "reuseExisting": True}, headers=headers)
    assert opened.status_code == 201, opened.text
    assert opened.json()["data"]["status"] == "DRAFT"
    assert opened.json()["data"]["templateConfigVersion"] == 12
    assert opened.json()["data"]["template"]["processCode"] == "PROC-REIMBURSEMENT"
    assert client.get(url).json()["data"]["total"] == 1


def _add_history_submission(database, draft, *, status="QUEUED", resume_status=None):
    database.add(draft)
    database.flush()
    completed = status == "SUBMITTED"
    known_instance = completed or status == "VERIFYING" or resume_status == "VERIFYING"
    checkpoint = known_instance or status in {"OA_CREATING", "RECONCILING", "MANUAL_REVIEW"}
    observed_at = draft.locked_at or utc_now()
    submission = ReimbursementSubmission(
        draft_id=draft.id,
        corp_id=draft.corp_id,
        originator_user_id=draft.owner_user_id,
        originator_union_id="synthetic-union",
        originator_name="测试员工",
        department_id=draft.department_id,
        department_name=draft.department_name,
        template_process_code=draft.template_process_code,
        template_config_version=draft.template_config_version,
        schema_fingerprint=draft.schema_fingerprint,
        form_snapshot_json=json.dumps(
            {
                "snapshotVersion": 11,
                "attachmentMode": "bundle_and_excel",
                "originalFiles": [{"draftFileId": "synthetic-original"}],
            }
        ),
        related_instance_ids_json="[]",
        snapshot_sha256="a" * 64,
        idempotency_key_hash="b" * 64,
        status="VERIFYING" if completed else status,
        resume_status=resume_status,
        oa_create_started_at=observed_at if checkpoint else None,
        oa_request_hash="c" * 64 if checkpoint else None,
        process_instance_id=f"synthetic-instance-{draft.id}" if known_instance else None,
        reconciliation_deadline_at=(
            observed_at + timedelta(minutes=5) if status == "RECONCILING" else None
        ),
        orphan_confirmed_at=observed_at if status == "ORPHAN_CLEANUP" else None,
        orphan_confirmation_code="synthetic-not-created" if status == "ORPHAN_CLEANUP" else None,
    )
    database.add(submission)
    database.flush()
    if completed:
        # A successful record must satisfy the real linked-attachment DB guards.
        for index, role, extension, media_type in (
            (0, "GENERATED_PDF", "pdf", "application/pdf"),
            (
                1,
                "GENERATED_EXCEL",
                "xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ):
            database.add(
                ReimbursementUpload(
                    submission_id=submission.id,
                    draft_id=draft.id,
                    role=role,
                    sort_order=index,
                    local_storage_key=f"generated/{submission.id}/synthetic.{extension}",
                    local_status="READY",
                    reserved_bytes=1,
                    file_name=f"synthetic.{extension}",
                    file_type=extension,
                    media_type=media_type,
                    size_bytes=1,
                    sha256="d" * 64,
                    upload_status="LINKED",
                    space_id="synthetic-space",
                    file_id=f"synthetic-file-{submission.id}-{index}",
                    linked_at=observed_at,
                )
            )
        database.flush()
        submission.status = "SUBMITTED"
        submission.submitted_at = observed_at
        submission.business_id = f"synthetic-business-{draft.id}"
        submission.approval_url = "https://example.invalid/approval"
        database.flush()


def test_submission_history_only_returns_ten_latest_submissions_in_current_scope(
    client_factory, monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    url = "/api/reimbursements/submission-history"
    assert client.get(url).status_code == 401
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    assert client.get(url).json()["data"] == []
    created = client.post(
        "/api/reimbursements/drafts", json={"expectedRevision": 0, "input": _input()},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    now = utc_now()
    with client.app.state.database_session_factory() as database:
        source = database.get(ReimbursementDraft, created.json()["data"]["id"])
        assert source is not None
        scope = {
            "corp_id": source.corp_id,
            "owner_user_id": source.owner_user_id,
            "department_id": source.department_id,
            "department_name": source.department_name,
            "template_process_code": source.template_process_code,
            "template_config_version": source.template_config_version,
            "schema_fingerprint": source.schema_fingerprint,
            "expires_at": source.expires_at,
        }
        for index in range(12):
            record = ReimbursementDraft(
                **scope, id=f"synthetic-submission-{index:02d}", status="LOCKED",
                locked_at=now - timedelta(days=20 - index // 2),
                updated_at=now - timedelta(days=index),
            )
            _add_history_submission(database, record, status="SUBMITTED")
        for index in range(55):
            database.add(ReimbursementDraft(**scope, id=f"synthetic-draft-{index:02d}"))
        # Newer unfinished and failed tasks must not crowd out successful records.
        for index in range(12):
            record = ReimbursementDraft(
                **scope, id=f"synthetic-failure-{index:02d}", status="LOCKED", locked_at=now,
            )
            _add_history_submission(
                database,
                record,
                status="QUEUED" if index % 2 else "FAILED_FINAL",
            )
        for field, value in (
            ("corp_id", "another-corp"), ("owner_user_id", "another-employee"),
            ("department_id", "200"), ("department_name", "其他测试部门"),
        ):
            record = ReimbursementDraft(
                **{**scope, field: value}, status="LOCKED", locked_at=now,
            )
            _add_history_submission(database, record, status="SUBMITTED")
        database.commit()

    response = client.get(url)
    assert response.status_code == 200, response.text
    records = response.json()["data"]
    assert [record["id"] for record in records] == [
        f"synthetic-submission-{index:02d}" for index in range(11, 1, -1)
    ]
    assert all(set(record) == {"id", "lockedAt", "relatedApprovalCount"} for record in records)
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, "synthetic-submission-00") is not None

    for endpoint in (url, "/api/reimbursements/drafts"):
        for scope in ({"expectedDepartmentId": "200"}, {"expectedDepartmentName": "另一部门"}):
            stale = client.get(endpoint, params=scope)
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"]["code"] == "REIMBURSEMENT_DEPARTMENT_CHANGED"
    stale_create = client.post(
        "/api/reimbursements/drafts",
        json={
            "expectedRevision": 0,
            "reuseExisting": True,
            "expectedDepartmentId": "200",
            "input": _input(),
        },
        headers=headers,
    )
    assert stale_create.status_code == 409, stale_create.text
    assert stale_create.json()["error"]["code"] == "REIMBURSEMENT_DEPARTMENT_CHANGED"


def test_submission_history_only_shows_success_and_preserves_other_tasks(
    client_factory,
    monkeypatch,
):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    cases = [
        ("FAILED_FINAL", None, False),
        ("MANUAL_REVIEW", None, False),
        ("FAILED_RETRYABLE", "UPLOADING", False),
        ("FAILED_RETRYABLE", "VERIFYING", False),
        ("QUEUED", None, False),
        ("VALIDATING", None, False),
        ("GENERATING_EXCEL", None, False),
        ("UPLOADING", None, False),
        ("OA_CREATING", None, False),
        ("RECONCILING", None, False),
        ("VERIFYING", None, False),
        ("ORPHAN_CLEANUP", None, False),
        ("SUBMITTED", None, True),
    ]
    visible_ids = []
    for status, resume_status, visible in cases:
        created = client.post(
            "/api/reimbursements/drafts",
            json={"expectedRevision": 0, "input": _input(), **_replacement_input(client)},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["data"]["id"]
        with client.app.state.database_session_factory() as database:
            draft = database.get(ReimbursementDraft, draft_id)
            draft.status = "LOCKED"
            draft.locked_at = utc_now()
            _add_history_submission(database, draft, status=status, resume_status=resume_status)
            database.commit()
        if visible:
            visible_ids.insert(0, draft_id)
    response = client.get("/api/reimbursements/submission-history")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["data"]] == visible_ids
    with client.app.state.database_session_factory() as database:
        assert database.query(ReimbursementSubmission).count() == len(cases)


@pytest.mark.parametrize(
    "status,resume_status,restore_pending",
    [
        ("QUEUED", None, True),
        ("UPLOADING", None, True),
        ("FAILED_RETRYABLE", "UPLOADING", True),
        ("FAILED_RETRYABLE", "VERIFYING", True),
        ("OA_CREATING", None, True),
        ("RECONCILING", None, True),
        ("VERIFYING", None, True),
        ("MANUAL_REVIEW", None, True),
        ("ORPHAN_CLEANUP", None, True),
        ("FAILED_FINAL", None, False),
        ("SUBMITTED", None, False),
    ],
)
def test_workspace_keeps_editable_draft_and_lists_unfinished_tasks(
    client_factory,
    monkeypatch,
    status,
    resume_status,
    restore_pending,
):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    draft_ids = []
    for _ in range(2):
        created = client.post(
            "/api/reimbursements/drafts",
            json={"expectedRevision": 0, "input": _input(), **_replacement_input(client)},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        draft_ids.append(created.json()["data"]["id"])
    pending_id, editing_id = draft_ids
    with client.app.state.database_session_factory() as database:
        pending = database.get(ReimbursementDraft, pending_id)
        pending.status = "LOCKED"
        pending.locked_at = utc_now() - timedelta(days=2)
        pending.updated_at = pending.locked_at
        _add_history_submission(database, pending, status=status, resume_status=resume_status)
        database.commit()

    response = client.get("/api/reimbursements/drafts", params={"limit": 1})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["items"][0]["id"] == editing_id
    workspace = client.get("/api/reimbursements/workspace")
    assert workspace.status_code == 200, workspace.text
    assert [item["id"] for item in workspace.json()["data"]["items"]] == ([editing_id])


def test_employee_original_currency_edit_intent_is_persisted(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    value = _input()
    value["items"][0].update(
        originalCurrency="USD",
        originalAmount="123.45",
        originalDetailsEdited=True,
    )

    created = client.post(
        "/api/reimbursements/drafts",
        json={"expectedRevision": 0, "input": value},
        headers=headers,
    )

    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    assert draft["input"]["items"][0]["originalDetailsEdited"] is True
    loaded = client.get(f"/api/reimbursements/drafts/{draft['id']}")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["data"]["input"]["items"][0]["originalDetailsEdited"] is True


def test_canonical_draft_input_preserves_multiple_subsidy_trips() -> None:
    raw = _input()
    raw["trip"] = None
    raw["trips"] = [
        {
            "tripType": "business",
            "relatedApprovalId": "approval-1",
            "startDate": "2026-09-01",
            "startTime": "09:00",
            "endDate": "2026-09-03",
            "endTime": "18:00",
        },
        {
            "tripType": "business",
            "relatedApprovalId": "approval-2",
            "startDate": "2026-09-08",
            "startTime": "18:00",
            "endDate": "2026-09-10",
            "endTime": "09:00",
        },
    ]

    canonical = reimbursement_drafts._canonical_input_data(
        ReimbursementDraftInput.model_validate(raw)
    )

    assert canonical["trip"] is None
    assert canonical["trips"] == raw["trips"]


def test_autosave_preserves_unresolved_ocr_but_review_requires_disposition(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    _add_active_file(client, draft_id, record_disposition=False)

    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 1, "input": _input()},
        headers=headers,
    )

    assert saved.status_code == 200
    _assert_unresolved_provenance(client, draft_id)
    loaded = client.get(f"/api/reimbursements/drafts/{draft_id}")
    assert loaded.json()["data"]["revision"] == 2


def test_draft_rejects_duplicate_or_overlapping_ocr_file_dispositions(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}

    duplicate = _input()
    duplicate["items"] = [
        {**duplicate["items"][0], "sourceFileId": "file-1"},
        {
            **duplicate["items"][0],
            "description": "另一条费用",
            "sourceFileId": "file-1",
        },
    ]
    duplicate_response = _create(client, headers, duplicate)
    assert duplicate_response.status_code == 422

    overlapping = _input()
    overlapping["items"][0]["sourceFileId"] = "file-1"
    overlapping["dismissedOcrFileIds"] = ["file-1"]
    overlapping_response = _create(client, headers, overlapping)
    assert overlapping_response.status_code == 422


def test_update_validates_ocr_file_provenance_inside_the_owned_draft(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    source_draft_id = _create(client, headers).json()["data"]["id"]
    target_draft_id = _create(client, headers).json()["data"]["id"]
    foreign_file_id = _add_active_file(
        client,
        source_draft_id,
        record_disposition=False,
    )

    linked_to_other_draft = _input()
    linked_to_other_draft["items"][0]["sourceFileId"] = foreign_file_id
    rejected_link = client.put(
        f"/api/reimbursements/drafts/{target_draft_id}",
        json={"expectedRevision": 1, "input": linked_to_other_draft},
        headers=headers,
    )
    assert rejected_link.status_code == 422
    assert rejected_link.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_FILE_REFERENCE_INVALID"

    dismissed_from_other_draft = _input()
    dismissed_from_other_draft["dismissedOcrFileIds"] = [foreign_file_id]
    rejected_dismissal = client.put(
        f"/api/reimbursements/drafts/{target_draft_id}",
        json={"expectedRevision": 1, "input": dismissed_from_other_draft},
        headers=headers,
    )
    assert rejected_dismissal.status_code == 422
    assert (
        rejected_dismissal.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_FILE_REFERENCE_INVALID"
    )

    attachment_id = _add_active_file(
        client,
        target_draft_id,
        processing_role=ReimbursementDraftFileRole.ATTACHMENT_ONLY.value,
        record_disposition=False,
    )
    linked_to_attachment = _input()
    linked_to_attachment["items"][0]["sourceFileId"] = attachment_id
    rejected_role = client.put(
        f"/api/reimbursements/drafts/{target_draft_id}",
        json={"expectedRevision": 1, "input": linked_to_attachment},
        headers=headers,
    )
    assert rejected_role.status_code == 422
    assert rejected_role.json()["error"]["code"] == ("REIMBURSEMENT_DRAFT_FILE_REFERENCE_INVALID")

    own_file_id = _add_active_file(client, target_draft_id, record_disposition=False)
    valid = _input()
    valid["items"][0]["sourceFileId"] = own_file_id
    accepted = client.put(
        f"/api/reimbursements/drafts/{target_draft_id}",
        json={"expectedRevision": 1, "input": valid},
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["input"]["items"][0]["sourceFileId"] == own_file_id


def test_owner_can_delete_an_unlocked_draft_at_the_expected_revision(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]

    deleted = client.delete(
        f"/api/reimbursements/drafts/{draft_id}",
        params={"expectedRevision": 1},
        headers=headers,
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"] == {"deletedDraftId": draft_id}
    assert client.get(f"/api/reimbursements/drafts/{draft_id}").status_code == 404


def test_draft_with_a_submission_cannot_be_deleted(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        assert draft is not None
        database.add(
            ReimbursementSubmission(
                draft_id=draft.id,
                corp_id=draft.corp_id,
                originator_user_id=draft.owner_user_id,
                originator_union_id="union-1",
                originator_name="测试员工",
                department_id=draft.department_id,
                department_name=draft.department_name,
                template_process_code=draft.template_process_code,
                template_config_version=draft.template_config_version,
                schema_fingerprint=draft.schema_fingerprint,
                form_snapshot_json=draft.input_json,
                related_instance_ids_json=draft.related_instance_ids_json,
                snapshot_sha256="b" * 64,
                idempotency_key_hash="c" * 64,
                status=ReimbursementSubmissionStatus.QUEUED.value,
                status_version=1,
            )
        )
        database.commit()

    deleted = client.delete(
        f"/api/reimbursements/drafts/{draft_id}",
        params={"expectedRevision": 1},
        headers=headers,
    )

    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_IN_USE"
    assert client.get(f"/api/reimbursements/drafts/{draft_id}").status_code == 200


def test_draft_owner_is_not_enumerable(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    owner = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="owner-1",
        auth_mock_departments="100:财务部",
    )
    owner_login = mock_login(owner)
    owner_headers = {"X-CSRF-Token": owner_login["csrfToken"]}
    created = _create(owner, owner_headers)
    assert created.status_code == 201, created.text
    draft_id = created.json()["data"]["id"]

    stranger = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="stranger-1",
        auth_mock_departments="100:财务部",
    )
    stranger_login = mock_login(stranger)
    stranger_headers = {"X-CSRF-Token": stranger_login["csrfToken"]}
    assert stranger.get(f"/api/reimbursements/drafts/{draft_id}").status_code == 404
    assert (
        stranger.put(
            f"/api/reimbursements/drafts/{draft_id}",
            json={"expectedRevision": 1, "input": _input()},
            headers=stranger_headers,
        ).status_code
        == 404
    )
    assert (
        stranger.delete(
            f"/api/reimbursements/drafts/{draft_id}",
            params={"expectedRevision": 1},
            headers=stranger_headers,
        ).status_code
        == 404
    )

    other_corp = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="owner-1",
        auth_mock_departments="100:财务部",
        dingtalk_corp_id="corp-other",
    )
    other_corp_login = mock_login(other_corp)
    assert other_corp.get(f"/api/reimbursements/drafts/{draft_id}").status_code == 404
    assert (
        other_corp.delete(
            f"/api/reimbursements/drafts/{draft_id}",
            params={"expectedRevision": 1},
            headers={"X-CSRF-Token": other_corp_login["csrfToken"]},
        ).status_code
        == 404
    )


def test_stale_revision_and_invalid_oa_options_leave_the_draft_unchanged(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    created = _create(client, headers)
    assert created.status_code == 201
    draft_id = created.json()["data"]["id"]

    first = _input()
    first["items"][0]["amount"] = "50.00"
    assert (
        client.put(
            f"/api/reimbursements/drafts/{draft_id}",
            json={"expectedRevision": 1, "input": first},
            headers=headers,
        ).status_code
        == 200
    )

    stale = _input()
    stale["items"][0]["amount"] = "999.00"
    conflict = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 1, "input": stale},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REVISION_CONFLICT"
    delete_conflict = client.delete(
        f"/api/reimbursements/drafts/{draft_id}",
        params={"expectedRevision": 1},
        headers=headers,
    )
    assert delete_conflict.status_code == 409
    assert delete_conflict.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REVISION_CONFLICT"

    invalid = dict(first)
    invalid["companyValue"] = "客户端伪造公司"
    invalid["accountingSourceVerified"] = True
    rejected = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 2, "input": invalid},
        headers=headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["input"]["companyValue"] == ""
    assert rejected.json()["data"]["input"]["accountingSourceVerified"] is False

    unchanged = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert unchanged["revision"] == 3
    assert unchanged["input"] == _without_accounting(first)


def test_create_rejects_client_totals_identity_and_nonzero_initial_revision(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}

    for extra in (
        {"ownerUserId": "attacker"},
        {"corpId": "other-corp"},
        {"totals": {"totalAmount": "0.01"}},
    ):
        body = _input() | extra
        assert _create(client, headers, body).status_code == 422
    wrong_revision = client.post(
        "/api/reimbursements/drafts",
        json={"expectedRevision": 1, "input": _input()},
        headers=headers,
    )
    assert wrong_revision.status_code == 422
    boolean_revision = client.post(
        "/api/reimbursements/drafts",
        json={"expectedRevision": False, "input": _input()},
        headers=headers,
    )
    assert boolean_revision.status_code == 422

    with client.app.state.database_session_factory() as database:
        assert database.query(ReimbursementDraft).count() == 0


def test_expired_and_locked_drafts_are_visible_but_cannot_be_changed(
    client_factory,
    monkeypatch,
) -> None:
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    expired_id = _create(client, headers).json()["data"]["id"]
    locked_id = _create(client, headers).json()["data"]["id"]
    with client.app.state.database_session_factory() as database:
        expired = database.get(ReimbursementDraft, expired_id)
        locked = database.get(ReimbursementDraft, locked_id)
        assert expired is not None and locked is not None
        expired.expires_at = utc_now() - timedelta(seconds=1)
        locked.status = ReimbursementDraftStatus.LOCKED.value
        locked.locked_at = utc_now()
        locked.expires_at = utc_now() - timedelta(seconds=1)
        database.commit()

    listed = client.get("/api/reimbursements/drafts")
    assert listed.status_code == 200, listed.text
    listed_by_id = {item["id"]: item for item in listed.json()["data"]["items"]}
    assert listed_by_id[expired_id]["status"] == "EXPIRED"
    assert listed_by_id[locked_id]["status"] == "LOCKED"

    expired_detail = client.get(f"/api/reimbursements/drafts/{expired_id}")
    assert expired_detail.status_code == 200
    assert expired_detail.json()["data"]["status"] == "EXPIRED"
    expired_update = client.put(
        f"/api/reimbursements/drafts/{expired_id}",
        json={"expectedRevision": 1, "input": _input()},
        headers=headers,
    )
    locked_update = client.put(
        f"/api/reimbursements/drafts/{locked_id}",
        json={"expectedRevision": 1, "input": _input()},
        headers=headers,
    )
    assert expired_update.status_code == 409
    assert expired_update.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REPLACED"
    assert locked_update.status_code == 409
    assert locked_update.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_LOCKED"
    locked_delete = client.delete(
        f"/api/reimbursements/drafts/{locked_id}",
        params={"expectedRevision": 1},
        headers=headers,
    )
    assert locked_delete.status_code == 409
    assert locked_delete.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_LOCKED"


def test_catalog_drift_blocks_mutation_without_hiding_or_changing_existing_draft(
    client_factory,
    monkeypatch,
) -> None:
    selected_catalog = {"value": _catalog()}
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: selected_catalog["value"],
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]

    drifted = _catalog()
    drifted.config_version = 13
    drifted.reimbursement.schema.fingerprint = "b" * 64
    selected_catalog["value"] = drifted
    rejected = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 1, "input": _input()},
        headers=headers,
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED"

    readable = client.get(f"/api/reimbursements/drafts/{draft_id}")
    assert readable.status_code == 200
    assert readable.json()["data"]["revision"] == 1
    assert readable.json()["data"]["template"]["configVersion"] == 12


def test_removed_current_budget_option_is_rejected_without_invalidating_catalog() -> None:
    catalog = _catalog()
    catalog.reimbursement.schema.components = tuple(
        SimpleNamespace(component_id=item.component_id, options=())
        if item.component_id == "budget-id"
        else item
        for item in catalog.reimbursement.schema.components
    )

    with pytest.raises(ApiError) as caught:
        reimbursement_drafts._require_exact_option(catalog, "budgetCode", "26007")

    assert caught.value.code == "REIMBURSEMENT_BUDGET_OPTION_INVALID"
    assert caught.value.status_code == 422


def test_draft_can_be_read_after_application_restart(client_factory, monkeypatch) -> None:
    _install_catalog(monkeypatch)
    first = client_factory(auth_mock_enabled=True, auth_mock_user_id="restart-user")
    first_login = mock_login(first)
    created = _create(first, {"X-CSRF-Token": first_login["csrfToken"]})
    assert created.status_code == 201
    draft_id = created.json()["data"]["id"]

    restarted = client_factory(auth_mock_enabled=True, auth_mock_user_id="restart-user")
    mock_login(restarted)
    response = restarted.get(f"/api/reimbursements/drafts/{draft_id}")

    assert response.status_code == 200
    assert response.json()["data"]["input"] == _without_accounting(_input())
    assert response.json()["data"]["revision"] == 1


def test_related_approvals_are_reverified_and_atomically_replace_the_snapshot(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    workflow = FakeTravelWorkflow()
    client.app.state.dingtalk_workflow = workflow

    response = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["revision"] == 2
    assert data["relatedApprovalSummary"] == {
        "count": 1,
        "startDate": "2026-09-01",
        "endDate": "2026-09-02",
    }
    assert data["relatedApprovals"][0] == {
        "processInstanceId": "travel-1",
            "profileKey": "domestic",
            "sourceProcessCode": "PROC-DOMESTIC",
            "originatorDepartmentId": "100",
        "title": "当前员工提交的境内出差申请",
        "businessId": "BIZ-travel-1",
        "startDate": "2026-09-01",
        "endDate": "2026-09-02",
        "queryWindow": {
            "startTimeMs": 1777564800000,
            "endTimeMs": 1787932799999,
        },
        "verifiedAt": data["relatedApprovals"][0]["verifiedAt"],
    }
    assert workflow.list_calls[0]["process_code"] == "PROC-DOMESTIC"
    assert workflow.list_calls[0]["user_ids"] == ("mock-user",)
    assert workflow.list_calls[0]["statuses"] == ("COMPLETED",)
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraft, draft_id)
        rows = database.query(ReimbursementDraftRelatedApproval).all()
        assert stored is not None
        assert stored.related_instance_ids_json == '["travel-1"]'
        assert len(rows) == 1
        assert rows[0].travel_schema_fingerprint == "d" * 64


def test_related_accounting_cannot_be_overridden_and_clear_preserves_work(
    client_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: _catalog_with_travel(),
    )
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    initial = _create(client, headers, {**_input(), "accountingSourceVerified": True}).json()[
        "data"
    ]
    assert initial["input"]["companyValue"] == ""
    assert initial["input"]["accountingSourceVerified"] is False
    draft_id = initial["id"]
    file_id = _add_active_file(client, draft_id)
    client.app.state.dingtalk_workflow = FakeTravelWorkflow()
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    derived = related.json()["data"]["input"]
    assert (derived["companyValue"], derived["budgetCodeValue"]) == ("北京", "26007")
    assert derived["project"] == {"mode": "manual", "text": "MES 项目"}
    assert derived["accountingSourceVerified"] is True
    spoofed = {**derived, "companyValue": "fake", "budgetCodeValue": "fake", "project": None}
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 2, "input": spoofed},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["input"] == derived
    cleared = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 3, "selections": []},
        headers=headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["input"] == _without_accounting(derived)
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraftFile, file_id) is not None


def test_related_request_rejects_duplicate_or_claimed_source_fields_before_network(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    workflow = FakeTravelWorkflow()
    client.app.state.dingtalk_workflow = workflow

    duplicate = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection(), _selection()]},
        headers=headers,
    )
    claimed = _selection()
    claimed["processCode"] = "PROC-EVIL"
    malicious = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [claimed]},
        headers=headers,
    )
    assert duplicate.status_code == 422
    assert malicious.status_code == 422
    assert workflow.list_calls == []


def test_catalog_change_during_related_reverification_rolls_back_without_revision_change(
    client_factory,
    monkeypatch,
) -> None:
    original = _catalog_with_travel()
    changed = _catalog_with_travel()
    changed.config_version = 13
    changed.reimbursement.schema.fingerprint = "b" * 64
    calls = {"count": 0}

    def catalog_at_boundary(_database):
        calls["count"] += 1
        return original if calls["count"] <= 2 else changed

    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        catalog_at_boundary,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    workflow = FakeTravelWorkflow()
    client.app.state.dingtalk_workflow = workflow

    response = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED"
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraft, draft_id)
        assert stored is not None
        assert stored.revision == 1
        assert stored.related_instance_ids_json == "[]"
        assert database.query(ReimbursementDraftRelatedApproval).count() == 0


def test_draft_expiring_during_related_reverification_is_not_updated(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]

    class ExpiringWorkflow(FakeTravelWorkflow):
        async def list_process_instance_ids(self, **kwargs):
            with client.app.state.database_session_factory() as database:
                draft = database.get(ReimbursementDraft, draft_id)
                assert draft is not None
                draft.expires_at = utc_now() + timedelta(milliseconds=5)
                database.commit()
            await asyncio.sleep(0.02)
            return await super().list_process_instance_ids(**kwargs)

    client.app.state.dingtalk_workflow = ExpiringWorkflow()
    response = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_EXPIRED"
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraft, draft_id)
        assert stored is not None
        assert stored.revision == 1
        assert stored.related_instance_ids_json == "[]"
        assert database.query(ReimbursementDraftRelatedApproval).count() == 0


@pytest.mark.asyncio
async def test_related_reverification_runs_without_an_open_database_transaction(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    draft_id = _create(client, {"X-CSRF-Token": login["csrfToken"]}).json()["data"]["id"]
    database = client.app.state.database_session_factory()
    actor = reimbursement_drafts.DraftActor(
        corp_id="corp-fixed",
        user_id="mock-user",
        department_id="100",
        department_name="测试部门",
    )

    class TransactionCheckingWorkflow(FakeTravelWorkflow):
        async def list_process_instance_ids(self, **kwargs):
            assert not database.in_transaction()
            return await super().list_process_instance_ids(**kwargs)

    try:
        result = await reimbursement_drafts.replace_related_approvals(
            database,
            TransactionCheckingWorkflow(),
            actor=actor,
            draft_id=draft_id,
            expected_revision=1,
            selections=[RelatedApprovalSelectionInput.model_validate(_selection())],
        )
    finally:
        database.close()

    assert result["revision"] == 2


@pytest.mark.asyncio
async def test_related_approval_rejects_another_current_department(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    draft_id = _create(
        client,
        {"X-CSRF-Token": login["csrfToken"]},
    ).json()["data"]["id"]
    database = client.app.state.database_session_factory()
    actor = reimbursement_drafts.DraftActor(
        corp_id="corp-fixed",
        user_id="mock-user",
        department_id="100",
        department_name="测试部门",
        available_department_ids=("100", "200"),
    )

    try:
        with pytest.raises(ApiError) as caught:
            await reimbursement_drafts.replace_related_approvals(
                database,
                FakeTravelWorkflow(department_id="200"),
                actor=actor,
                draft_id=draft_id,
                expected_revision=1,
                selections=[RelatedApprovalSelectionInput.model_validate(_selection())],
            )
    finally:
        database.close()

    assert caught.value.code == "TRAVEL_APPROVAL_DEPARTMENT_MISMATCH"


@pytest.mark.asyncio
async def test_related_approval_allows_and_records_a_historical_department(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    draft_id = _create(
        client,
        {"X-CSRF-Token": login["csrfToken"]},
    ).json()["data"]["id"]
    database = client.app.state.database_session_factory()
    actor = reimbursement_drafts.DraftActor(
        corp_id="corp-fixed",
        user_id="mock-user",
        department_id="100",
        department_name="测试部门",
        available_department_ids=("100", "200"),
    )

    try:
        result = await reimbursement_drafts.replace_related_approvals(
            database,
            FakeTravelWorkflow(department_id="historical-department"),
            actor=actor,
            draft_id=draft_id,
            expected_revision=1,
            selections=[RelatedApprovalSelectionInput.model_validate(_selection())],
        )
    finally:
        database.close()

    assert result["relatedApprovals"][0]["originatorDepartmentId"] == (
        "historical-department"
    )


def test_review_requires_verified_approval_and_active_file_then_marks_ready(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]

    no_prerequisites = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 1},
        headers=headers,
    )
    assert no_prerequisites.status_code == 409
    assert no_prerequisites.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_NOT_READY"
    assert client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["revision"] == 1

    client.app.state.dingtalk_workflow = FakeTravelWorkflow()
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text

    still_missing_file = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )
    assert still_missing_file.status_code == 409
    assert still_missing_file.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_NOT_READY"
    _add_active_file(client, draft_id)

    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["data"]["status"] == "REVIEW_READY"
    assert reviewed.json()["data"]["revision"] == 3

    changed = reviewed.json()["data"]["input"]
    changed["items"][0]["amount"] = "50.00"
    edited = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 3, "input": changed},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["data"]["status"] == "DRAFT"
    assert edited.json()["data"]["revision"] == 4


@pytest.mark.parametrize(
    "terminal_status",
    [
        ReimbursementOcrStatus.COMPLETE.value,
        ReimbursementOcrStatus.FAILED.value,
    ],
)
def test_review_requires_an_exact_disposition_for_each_terminal_ocr_file(
    client_factory,
    monkeypatch,
    terminal_status: str,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    client.app.state.dingtalk_workflow = FakeTravelWorkflow()
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    file_id = _add_active_file(
        client,
        draft_id,
        ocr_status=terminal_status,
        record_disposition=False,
    )

    missing = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_NOT_READY"

    dismissed = _input()
    dismissed["dismissedOcrFileIds"] = [file_id]
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        json={"expectedRevision": 2, "input": dismissed},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text

    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 3},
        headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["data"]["status"] == "REVIEW_READY"


def test_review_blocks_unrecognized_expense_source_but_not_plain_attachment(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    client.app.state.dingtalk_workflow = FakeTravelWorkflow()
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    file_id = _add_active_file(
        client,
        draft_id,
        ocr_status=ReimbursementOcrStatus.NOT_REQUESTED.value,
        record_disposition=False,
    )

    unrecognized = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )
    assert unrecognized.status_code == 409
    assert unrecognized.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_NOT_READY"

    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file is not None
        file.processing_role = ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
        database.commit()
    attachment_only = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )
    assert attachment_only.status_code == 200, attachment_only.text


def test_review_rejects_related_approval_with_disjoint_trip_dates(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_input = _input()
    draft_input["items"][0]["date"] = "2026-08-10"
    draft_input["items"][0]["displayDate"] = "2026-08-10"
    draft_id = _create(client, headers, draft_input).json()["data"]["id"]
    client.app.state.dingtalk_workflow = FakeTravelWorkflow(
        start_date="2026-08-10",
        end_date="2026-08-12",
    )
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    _add_active_file(client, draft_id)

    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )

    assert reviewed.status_code == 409
    assert reviewed.json()["error"]["code"] == "REIMBURSEMENT_TRAVEL_DATE_MISMATCH"
    unchanged = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert unchanged["status"] == "DRAFT"
    assert unchanged["revision"] == 2


def test_review_requires_selected_approvals_to_cover_the_subsidy_period(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    client.app.state.dingtalk_workflow = FakeTravelWorkflow(
        start_date="2026-09-02",
        end_date="2026-09-03",
    )
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    _add_active_file(client, draft_id)

    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )

    assert reviewed.status_code == 409
    assert reviewed.json()["error"]["code"] == "REIMBURSEMENT_TRAVEL_DATE_MISMATCH"
    assert "补助日期" in reviewed.json()["error"]["message"]


def test_review_uses_expense_item_dates_when_the_draft_has_no_trip(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_input = _input()
    draft_input["trip"] = None
    draft_id = _create(client, headers, draft_input).json()["data"]["id"]
    client.app.state.dingtalk_workflow = FakeTravelWorkflow(
        start_date="2026-08-10",
        end_date="2026-08-12",
    )
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    _add_active_file(client, draft_id)

    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )

    assert reviewed.status_code == 409
    assert reviewed.json()["error"]["code"] == "REIMBURSEMENT_TRAVEL_DATE_MISMATCH"


def test_review_recovers_stale_running_ocr_and_keeps_linked_material(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    client.app.state.dingtalk_workflow = FakeTravelWorkflow()
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    file_id = _add_active_file(
        client,
        draft_id,
        ocr_status=ReimbursementOcrStatus.RUNNING.value,
        record_disposition=False,
    )
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        draft = database.get(ReimbursementDraft, draft_id)
        assert file is not None and draft is not None
        file.ocr_result_json = json.dumps({"operationId": "interrupted-operation"})
        file.updated_at = utc_now() - timedelta(
            seconds=client.app.state.settings.ocr_operation_timeout_seconds + 31
        )
        input_data = json.loads(draft.input_json)
        input_data["items"][0]["sourceFileId"] = file_id
        draft.input_json = json.dumps(
            input_data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        database.commit()

    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )

    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["data"]["status"] == "REVIEW_READY"
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file is not None
        assert file.ocr_status == ReimbursementOcrStatus.FAILED.value
        assert json.loads(file.ocr_result_json)["error"]["code"] == "OCR_INTERRUPTED"


def test_review_rejects_running_file_work_and_catalog_relation_drift_atomically(
    client_factory,
    monkeypatch,
) -> None:
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts,
        "require_submission_ready_catalog",
        lambda _database: catalog,
    )
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    client.app.state.dingtalk_workflow = FakeTravelWorkflow()
    related = client.put(
        f"/api/reimbursements/drafts/{draft_id}/related-approvals",
        json={"expectedRevision": 1, "selections": [_selection()]},
        headers=headers,
    )
    assert related.status_code == 200, related.text
    file_id = _add_active_file(
        client,
        draft_id,
        ocr_status=ReimbursementOcrStatus.RUNNING.value,
    )

    busy = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_NOT_READY"

    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        relation = database.query(ReimbursementDraftRelatedApproval).one()
        assert file is not None
        file.ocr_status = ReimbursementOcrStatus.COMPLETE.value
        file.ocr_result_json = "{}"
        relation.catalog_config_version += 1
        database.commit()

    drifted = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        json={"expectedRevision": 2},
        headers=headers,
    )
    assert drifted.status_code == 409
    assert drifted.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED"
    unchanged = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert unchanged["status"] == "DRAFT"
    assert unchanged["revision"] == 2


def test_new_current_requires_confirmation_and_revision_and_retires_old(
    client_factory, monkeypatch,
):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    first = _create(client, headers).json()["data"]
    blank = {"expectedRevision": 0, "input": {**_input(), "items": [], "trip": None}}
    assert client.post("/api/reimbursements/drafts", json=blank, headers=headers).status_code == 409
    stale = {
        **blank, "replacesDraftId": first["id"], "replacesDraftRevision": first["revision"] + 1,
    }
    assert client.post("/api/reimbursements/drafts", json=stale, headers=headers).status_code == 409
    replacement = client.post("/api/reimbursements/drafts", json={
        **stale, "replacesDraftRevision": first["revision"],
    }, headers=headers)
    assert replacement.status_code == 201, replacement.text
    current = replacement.json()["data"]
    assert current["id"] != first["id"] and current["input"]["items"] == []
    workspace = client.get("/api/reimbursements/workspace").json()["data"]
    assert workspace["items"][0]["id"] == current["id"]
    rejected = client.put(f"/api/reimbursements/drafts/{first['id']}", json={
        "expectedRevision": first["revision"], "input": _input(),
    }, headers=headers)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REPLACED"
    repeated = client.post("/api/reimbursements/drafts", json={
        **stale, "replacesDraftRevision": first["revision"],
    }, headers=headers)
    assert repeated.status_code == 409
    assert client.get("/api/reimbursements/workspace").json()["data"]["total"] == 1
