from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import mock_login
from sqlalchemy import select
from test_general_reimbursement import GeneralWorkflow, configure_profiles
from test_reimbursement_drafts import (
    FakeTravelWorkflow,
    _catalog_with_travel,
    _install_catalog,
    _selection,
)
from test_reimbursement_files import _upload

from app.api import reimbursements
from app.models.reimbursement import ReimbursementDraft, utc_now
from app.models.session import UserSession
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services import reimbursement_drafts
from app.services.reimbursement_drafts import DraftActor, create_reimbursement_draft


def setup(client_factory, *, general=False):
    client = client_factory(
        auth_mock_enabled=True, auth_mock_departments="100:测试部门,200:第二部门"
    )
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post(
        "/api/me/department/for-general-reimbursement",
        json={"departmentId": "100"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    workflow = GeneralWorkflow()
    asyncio.run(configure_profiles(client.app, workflow))
    response = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={
            "expectedRevision": 0,
            "kind": "general" if general else "travel",
            "input": {"items": []},
        },
    )
    assert response.status_code == 201, response.text
    return client, headers, response.json()["data"]


@pytest.mark.parametrize("kind", ["travel", "general"])
@pytest.mark.parametrize("reuse_existing", [False, True])
@pytest.mark.parametrize("replace_current", [False, True])
def test_creation_honors_page_department_after_another_tab_changes_session(
    client_factory, kind, reuse_existing, replace_current
):
    client, headers, previous = setup(
        client_factory, general=(kind == "general") == replace_current
    )
    with client.app.state.database_session_factory() as database:
        session = database.scalar(select(UserSession))
        session.current_department_id = "200"
        session.current_department_name = "第二部门"
        database.commit()
    body = {
        "expectedRevision": 0,
        "kind": kind,
        "reuseExisting": reuse_existing,
        "expectedDepartmentId": "100",
        "expectedDepartmentName": "测试部门",
        "input": {"items": []},
    }
    if replace_current:
        body.update(replacesDraftId=previous["id"], replacesDraftRevision=previous["revision"])
    response = client.post("/api/reimbursements/drafts", headers=headers, json=body)
    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert created["department"] == {"id": "100", "name": "测试部门"}
    assert created["id"] != previous["id"]
    workspace = client.get("/api/reimbursements/workspace", params={"kind": kind}).json()["data"]
    assert workspace["total"] == 1
    assert workspace["items"][0]["id"] == created["id"]


@pytest.mark.parametrize("general", [False, True])
def test_reusing_current_draft_ignores_stale_page_department(client_factory, general):
    client, headers, previous = setup(client_factory, general=general)
    response = client.post(
        "/api/reimbursements/drafts", headers=headers,
        json={
            "expectedRevision": 0, "kind": "general" if general else "travel",
            "reuseExisting": True, "expectedDepartmentId": "removed",
            "expectedDepartmentName": "已撤销的旧页面部门", "input": {"items": []},
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"] == previous


@pytest.mark.parametrize("replace_current", [False, True])
def test_invalid_creation_department_does_not_create_or_retire_draft(
    client_factory, replace_current
):
    client, headers, previous = setup(client_factory, general=replace_current)
    body = {
        "expectedRevision": 0, "kind": "general", "reuseExisting": True,
        "expectedDepartmentId": "100", "expectedDepartmentName": "伪造的部门名称",
        "input": {"items": []},
    }
    if replace_current:
        body.update(replacesDraftId=previous["id"], replacesDraftRevision=previous["revision"])
    response = client.post("/api/reimbursements/drafts", headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_DEPARTMENT"
    with client.app.state.database_session_factory() as database:
        records = database.scalars(select(ReimbursementDraft)).all()
        assert len(records) == 1
        assert records[0].id == previous["id"]
        assert records[0].revision == previous["revision"]
        assert not records[0].workspace_retired


@pytest.mark.parametrize("general", [False, True])
def test_department_correction_keeps_record_content_files_and_revision(
    client_factory, monkeypatch, general
):
    client, headers, draft = setup(client_factory, general=general)
    uploaded = _upload(client, headers["X-CSRF-Token"], draft["id"], revision=draft["revision"])
    assert uploaded.status_code == 201, uploaded.text
    draft = client.get(f"/api/reimbursements/drafts/{draft['id']}").json()["data"]
    original_items = draft["input"]["items"]
    if not general:
        catalog = _catalog_with_travel()
        monkeypatch.setattr(reimbursements, "require_submission_ready_catalog", lambda db: catalog)
        monkeypatch.setattr(
            reimbursement_drafts, "require_submission_ready_catalog", lambda db: catalog
        )
        with client.app.state.database_session_factory() as database:
            stored = database.get(ReimbursementDraft, draft["id"])
            stored.template_process_code = catalog.reimbursement.process_code
            stored.template_config_version = catalog.config_version
            stored.schema_fingerprint = catalog.reimbursement.schema.fingerprint
            database.commit()
        client.app.state.dingtalk_workflow = FakeTravelWorkflow(department_id="200")
    body = {"expectedRevision": draft["revision"], "departmentId": "200"}
    if not general:
        body["selection"] = _selection()
    response = client.put(
        f"/api/reimbursements/drafts/{draft['id']}/department", json=body, headers=headers
    )
    assert response.status_code == 200, response.text
    corrected = response.json()["data"]["draft"]
    assert corrected["id"] == draft["id"]
    assert corrected["revision"] == draft["revision"] + 1
    assert corrected["department"] == {"id": "200", "name": "第二部门"}
    assert corrected["input"]["items"] == original_items
    assert client.get("/api/me").json()["data"]["selectedDepartment"] == corrected["department"]
    files = client.get(f"/api/reimbursements/drafts/{draft['id']}/files").json()["data"]["items"]
    assert files[0]["id"] == uploaded.json()["data"]["file"]["id"]
    stale = client.put(
        f"/api/reimbursements/drafts/{draft['id']}/department", json=body, headers=headers
    )
    assert stale.status_code == 409
    assert (
        client.get(
            "/api/reimbursements/workspace", params={"kind": "general" if general else "travel"}
        ).json()["data"]["total"]
        == 1
    )


def test_cross_department_concurrent_creation_has_only_one_slot(client_factory, monkeypatch):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    barrier = Barrier(2)

    def create(department):
        actor = DraftActor("corp-fixed", "mock-user", department, department)
        with client.app.state.database_session_factory() as database:
            barrier.wait()
            return create_reimbursement_draft(
                database,
                actor=actor,
                draft_input=ReimbursementDraftInput(items=[]),
                ttl_days=7,
                max_items=100,
                reuse_existing=True,
            )["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, ["100", "200"]))
    assert ids[0] == ids[1]
    with client.app.state.database_session_factory() as database:
        assert len(database.scalars(select(ReimbursementDraft)).all()) == 1


def test_legacy_drafts_require_explicit_versioned_selection(client_factory):
    client, headers, draft = setup(client_factory, general=True)
    with client.app.state.database_session_factory() as database:
        source = database.get(ReimbursementDraft, draft["id"])
        other = ReimbursementDraft(
            **{
                column.name: getattr(source, column.name)
                for column in ReimbursementDraft.__table__.columns
                if column.name != "id"
            }
        )
        other.department_id, other.department_name = "200", "第二部门"
        database.add(other)
        database.commit()
        other_id = other.id
    workspace = client.get("/api/reimbursements/workspace?kind=general").json()["data"]
    assert workspace["total"] == 2
    create = client.post(
        "/api/reimbursements/drafts",
        json={
            "expectedRevision": 0,
            "kind": "general",
            "reuseExisting": True,
            "input": {"items": []},
        },
        headers=headers,
    )
    assert create.status_code == 409
    body = {
        "draftId": draft["id"],
        "kind": "general",
        "candidates": [{"id": d["id"], "revision": d["revision"]} for d in workspace["items"]],
    }
    stale = {**body, "candidates": [{"id": draft["id"], "revision": draft["revision"]}]}
    assert (
        client.post("/api/reimbursements/workspace/open", json=stale, headers=headers).status_code
        == 409
    )
    with client.app.state.database_session_factory() as database:
        assert not database.get(ReimbursementDraft, other_id).workspace_retired
    assert (
        client.post("/api/reimbursements/workspace/open", json=body, headers=headers).status_code
        == 200
    )
    # Retrying a lost response is idempotent and does not choose/recreate another record.
    assert (
        client.post("/api/reimbursements/workspace/open", json=body, headers=headers).status_code
        == 200
    )
    assert client.get("/api/reimbursements/workspace?kind=general").json()["data"]["total"] == 1
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, other_id).workspace_retired
        assert database.get(ReimbursementDraft, other_id).input_json


@pytest.mark.parametrize(
    "route", ["/api/reimbursements/workspace/open", "/api/reimbursements/drafts/fake/department"]
)
def test_workspace_writes_require_authentication_and_csrf(client_factory, route):
    client = client_factory(auth_mock_enabled=True)
    method = client.post if route.endswith("open") else client.put
    assert method(route, json={}).status_code == 401
    mock_login(client)
    assert method(route, json={}).status_code == 403


def test_each_kind_keeps_its_current_record_across_department_changes(client_factory):
    client, headers, general = setup(client_factory, general=True)
    root = "/api/reimbursements/drafts"
    travel_response = client.post(
        root, headers=headers, json={"expectedRevision": 0, "input": {"items": []}}
    )
    assert travel_response.status_code == 201
    travel = travel_response.json()["data"]
    changed = client.put(
        f"{root}/{general['id']}/department",
        headers=headers,
        json={"expectedRevision": general["revision"], "departmentId": "200"},
    )
    assert changed.status_code == 200, changed.text
    for kind, original in (("general", general), ("travel", travel)):
        current = client.get("/api/reimbursements/workspace", params={"kind": kind}).json()["data"]
        assert [d["id"] for d in current["items"]] == [original["id"]]
    assert (
        client.get("/api/reimbursements/workspace").json()["data"]["items"][0]["department"]["id"]
        == "100"
    )
    reused = client.post(
        root,
        headers=headers,
        json={"expectedRevision": 0, "reuseExisting": True, "input": {"items": []}},
    )
    assert reused.status_code == 201
    assert reused.json()["data"]["id"] == travel["id"]


def test_obsolete_department_can_be_restored_and_corrected_without_losing_content(client_factory):
    client, headers, general = setup(client_factory, general=True)
    with client.app.state.database_session_factory() as database:
        record = database.get(ReimbursementDraft, general["id"])
        record.department_id, record.department_name = "former", "已撤销的合成部门"
        database.commit()
    opened = client.post(
        "/api/reimbursements/workspace/open",
        headers=headers,
        json={
            "kind": "general",
            "draftId": general["id"],
            "candidates": [{"id": general["id"], "revision": general["revision"]}],
        },
    )
    assert opened.status_code == 200, opened.text
    root = f"/api/reimbursements/drafts/{general['id']}"
    read = client.get(root)
    assert read.status_code == 200, read.text
    assert read.json()["data"]["department"]["id"] == "former"
    saved = client.put(
        root,
        headers=headers,
        json={"expectedRevision": general["revision"], "input": {"items": []}},
    )
    assert saved.status_code == 200, saved.text
    corrected = client.put(
        root + "/department",
        headers=headers,
        json={"expectedRevision": saved.json()["data"]["revision"], "departmentId": "200"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data"]["draft"]["id"] == general["id"]
    assert corrected.json()["data"]["draft"]["department"]["id"] == "200"


@pytest.mark.parametrize("general", [False, True])
@pytest.mark.parametrize("department_id", ["100", "200"])
def test_renamed_department_remains_editable_and_supports_explicit_cas_correction(
    client_factory, monkeypatch, general, department_id
):
    client, headers, draft = setup(client_factory, general=general)
    if not general:
        catalog = _catalog_with_travel()
        monkeypatch.setattr(reimbursements, "require_submission_ready_catalog", lambda db: catalog)
        monkeypatch.setattr(
            reimbursement_drafts, "require_submission_ready_catalog", lambda db: catalog
        )
        with client.app.state.database_session_factory() as database:
            stored = database.get(ReimbursementDraft, draft["id"])
            stored.template_process_code = catalog.reimbursement.process_code
            stored.template_config_version = catalog.config_version
            stored.schema_fingerprint = catalog.reimbursement.schema.fingerprint
            database.commit()
        client.app.state.dingtalk_workflow = FakeTravelWorkflow(department_id=department_id)
    uploaded = _upload(
        client,
        headers["X-CSRF-Token"],
        draft["id"],
        revision=draft["revision"],
        role="ATTACHMENT_ONLY",
    )
    assert uploaded.status_code == 201, uploaded.text
    draft = client.get(f"/api/reimbursements/drafts/{draft['id']}").json()["data"]
    original_input = draft["input"]
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraft, draft["id"])
        stored.department_id, stored.department_name = department_id, "同一部门的旧名称"
        database.commit()
    root = f"/api/reimbursements/drafts/{draft['id']}"
    if department_id == "200":
        # Department is not an ownership boundary.
        assert client.get(root).status_code == 200
    opened = client.post(
        "/api/reimbursements/workspace/open",
        headers=headers,
        json={
            "kind": "general" if general else "travel",
            "draftId": draft["id"],
            "candidates": [{"id": draft["id"], "revision": draft["revision"]}],
        },
    )
    assert opened.status_code == 200, opened.text
    current_department = {
        "id": department_id,
        "name": "测试部门" if department_id == "100" else "第二部门",
    }
    assert opened.json()["data"]["selectedDepartment"] == current_department
    restored = client.get(root)
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["input"] == original_input
    assert restored.json()["data"]["revision"] == draft["revision"]
    assert restored.json()["data"]["department"]["name"] == "同一部门的旧名称"
    files = client.get(root + "/files")
    assert files.status_code == 200, files.text
    assert [f["id"] for f in files.json()["data"]["items"]] == [
        uploaded.json()["data"]["file"]["id"]
    ]
    saved = client.put(
        root, headers=headers, json={"expectedRevision": draft["revision"], "input": original_input}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["department"]["name"] == "同一部门的旧名称"
    body = {"expectedRevision": saved.json()["data"]["revision"], "departmentId": department_id}
    if not general:
        body["selection"] = _selection()
    corrected = client.put(root + "/department", headers=headers, json=body)
    assert corrected.status_code == 200, corrected.text
    result = corrected.json()["data"]["draft"]
    assert result["id"] == draft["id"]
    assert result["department"] == current_department
    assert result["input"]["items"] == original_input["items"]
    assert result["revision"] == saved.json()["data"]["revision"] + 1
    assert client.put(root + "/department", headers=headers, json=body).status_code == 409
    assert client.get(root).status_code == 200


@pytest.mark.parametrize("general", [False, True])
@pytest.mark.parametrize("rename", [False, True])
def test_expired_obsolete_department_can_be_explicitly_replaced_without_editing_old_record(
    client_factory, general, rename
):
    from datetime import timedelta

    client, headers, draft = setup(client_factory, general=general)
    kind = "general" if general else "travel"
    with client.app.state.database_session_factory() as database:
        previous = database.get(ReimbursementDraft, draft["id"])
        previous.department_id = "100" if rename else "former"
        previous.department_name = "同一部门的旧名称" if rename else "已撤销部门"
        previous.expires_at = utc_now() - timedelta(days=1)
        original_input, original_department = previous.input_json, previous.department_name
        database.commit()
    opened = client.post(
        "/api/reimbursements/workspace/open",
        headers=headers,
        json={
            "kind": kind,
            "draftId": draft["id"],
            "candidates": [{"id": draft["id"], "revision": draft["revision"]}],
        },
    )
    assert opened.status_code == 200, opened.text
    path = f"/api/reimbursements/drafts/{draft['id']}"
    restored = client.get(path)
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["status"] == "EXPIRED"
    correction = client.put(
        path + "/department",
        headers=headers,
        json={"expectedRevision": draft["revision"], "departmentId": "100"},
    )
    assert correction.status_code == 409
    assert correction.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_EXPIRED"
    body = {
        "kind": kind,
        "expectedRevision": 0,
        "input": {"items": []},
        "replacesDraftId": draft["id"],
        "replacesDraftRevision": draft["revision"],
    }
    stale = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={**body, "replacesDraftRevision": draft["revision"] + 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REVISION_CONFLICT"
    with client.app.state.database_session_factory() as database:
        assert not database.get(ReimbursementDraft, draft["id"]).workspace_retired
    created = client.post("/api/reimbursements/drafts", headers=headers, json=body)
    assert created.status_code == 201, created.text
    current = created.json()["data"]
    assert current["id"] != draft["id"]
    assert current["status"] == "DRAFT"
    assert current["input"]["items"] == []
    assert current["department"] == {"id": "100", "name": "测试部门"}
    with client.app.state.database_session_factory() as database:
        previous = database.get(ReimbursementDraft, draft["id"])
        assert previous.workspace_retired
        assert previous.input_json == original_input
        assert previous.department_name == original_department
    workspace = client.get("/api/reimbursements/workspace", params={"kind": kind}).json()["data"]
    assert [item["id"] for item in workspace["items"]] == [current["id"]]
    assert client.post("/api/reimbursements/drafts", headers=headers, json=body).status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("corp_id", "other-corporation"),
        ("owner_user_id", "other-employee"),
        ("operator_user_id", "other-operator"),
    ],
)
def test_replacement_does_not_cross_owned_scope_when_department_is_obsolete(
    client_factory, field, value
):
    client, headers, draft = setup(client_factory)
    with client.app.state.database_session_factory() as database:
        previous = database.get(ReimbursementDraft, draft["id"])
        previous.department_id, previous.department_name = "former", "旧部门"
        setattr(previous, field, value)
        database.commit()
    result = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={
            "expectedRevision": 0,
            "input": {"items": []},
            "replacesDraftId": draft["id"],
            "replacesDraftRevision": draft["revision"],
        },
    )
    assert result.status_code == 404, result.text
    with client.app.state.database_session_factory() as database:
        assert not database.get(ReimbursementDraft, draft["id"]).workspace_retired
        assert database.query(ReimbursementDraft).count() == 1


def test_expired_session_during_travel_verification_cannot_correct_a_draft(
    client_factory, monkeypatch
):
    from datetime import timedelta
    from types import SimpleNamespace

    from app.models.session import UserSession, utc_now

    client, headers, draft = setup(client_factory)

    async def expire_session(*args, **kwargs):
        with client.app.state.database_session_factory() as database:
            session = database.scalar(select(UserSession))
            session.expires_at = utc_now() - timedelta(seconds=1)
            database.commit()
        return SimpleNamespace(department_id="200")

    monkeypatch.setattr(reimbursements, "reverify_travel_approval_selection", expire_session)
    response = client.put(
        f"/api/reimbursements/drafts/{draft['id']}/department",
        headers=headers,
        json={"expectedRevision": draft["revision"], "selection": _selection()},
    )
    assert response.status_code == 401, response.text
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraft, draft["id"])
        assert stored.department_id == "100" and stored.revision == draft["revision"]


@pytest.mark.parametrize("historical", [False, True])
def test_manual_department_fallback_reverifies_and_preserves_all_saved_approvals(
    client_factory, monkeypatch, historical
):
    client, headers, draft = setup(client_factory)
    catalog = _catalog_with_travel()
    monkeypatch.setattr(reimbursements, "require_submission_ready_catalog", lambda db: catalog)
    monkeypatch.setattr(
        reimbursement_drafts, "require_submission_ready_catalog", lambda db: catalog
    )
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraft, draft["id"])
        stored.template_process_code = catalog.reimbursement.process_code
        stored.template_config_version = catalog.config_version
        stored.schema_fingerprint = catalog.reimbursement.schema.fingerprint
        database.commit()
    workflow = FakeTravelWorkflow(
        ("travel-1", "travel-2"), department_id="former-department" if historical else "100"
    )
    client.app.state.dingtalk_workflow = workflow
    path = f"/api/reimbursements/drafts/{draft['id']}"
    selected = client.put(
        path + "/related-approvals",
        headers=headers,
        json={
            "expectedRevision": draft["revision"],
            "selections": [_selection("travel-1"), _selection("travel-2")],
        },
    )
    assert selected.status_code == 200, selected.text
    saved = selected.json()["data"]
    workflow.detail_calls.clear()
    result = client.put(
        path + "/department",
        headers=headers,
        json={
            "expectedRevision": saved["revision"],
            "departmentId": "200",
        },
    )
    assert workflow.detail_calls == ["travel-1", "travel-2"]
    assert result.status_code == (200 if historical else 422), result.text
    if historical:
        corrected = result.json()["data"]["draft"]
        assert corrected["id"] == saved["id"]
        assert corrected["department"]["id"] == "200"
        assert [a["processInstanceId"] for a in corrected["relatedApprovals"]] == [
            "travel-1",
            "travel-2",
        ]
    else:
        assert result.json()["error"]["code"] == "REIMBURSEMENT_DEPARTMENT_SELECTION_NOT_ALLOWED"
        assert client.get(path).json()["data"]["revision"] == saved["revision"]


@pytest.mark.parametrize("single_department", [False, True])
@pytest.mark.parametrize("remaining", ["travel-2", "historical"])
def test_approval_changes_derive_department_atomically_and_preserve_work(
    client_factory, monkeypatch, single_department, remaining
):
    from dataclasses import replace

    from app.models.session import UserSession

    client, headers, draft = setup(client_factory)
    catalog = _catalog_with_travel()
    monkeypatch.setattr(
        reimbursement_drafts, "require_submission_ready_catalog", lambda db: catalog
    )
    with client.app.state.database_session_factory() as database:
        stored = database.get(ReimbursementDraft, draft["id"])
        stored.template_process_code = catalog.reimbursement.process_code
        stored.template_config_version = catalog.config_version
        stored.schema_fingerprint = catalog.reimbursement.schema.fingerprint
        if single_department:
            database.scalar(
                select(UserSession)
            ).departments_json = '[{"id":"100","name":"测试部门"}]'
        database.commit()

    class GroupWorkflow(FakeTravelWorkflow):
        async def get_process_instance(self, instance_id):
            instance = await super().get_process_instance(instance_id)
            return replace(
                instance,
                originator_department_id={
                    "travel-1": "100",
                    "travel-2": "200",
                    "historical": "former",
                }[instance_id],
            )

    client.app.state.dingtalk_workflow = GroupWorkflow(("travel-1", "travel-2", "historical"))
    uploaded = _upload(
        client,
        headers["X-CSRF-Token"],
        draft["id"],
        revision=draft["revision"],
        role="ATTACHMENT_ONLY",
    )
    assert uploaded.status_code == 201, uploaded.text
    root = f"/api/reimbursements/drafts/{draft['id']}"
    original = client.get(root).json()["data"]
    with_expense = client.put(root, headers=headers, json={
        "expectedRevision": original["revision"],
        "input": {**original["input"], "items": [{
            "category": "office", "date": "2026-09-01", "displayDate": "9月1日",
            "description": "部门更新后保留的办公费", "amount": "30.00", "receiptCount": 1,
        }]},
    })
    assert with_expense.status_code == 200, with_expense.text
    original = with_expense.json()["data"]
    first = client.put(
        root + "/related-approvals",
        headers=headers,
        json={
            "expectedRevision": original["revision"],
            "selections": [_selection("travel-1"), _selection("travel-2")],
        },
    )
    assert first.status_code == 200, first.text
    first = first.json()["data"]
    body = {"expectedRevision": first["revision"], "selections": [_selection(remaining)]}
    updated = client.put(root + "/related-approvals", headers=headers, json=body)
    assert updated.status_code == 200, updated.text
    saved = updated.json()["data"]
    expected = "200" if not single_department and remaining == "travel-2" else "100"
    assert saved["department"] == {
        "id": expected,
        "name": "第二部门" if expected == "200" else "测试部门",
    }
    assert [a["processInstanceId"] for a in saved["relatedApprovals"]] == [remaining]
    assert saved["revision"] == first["revision"] + 1
    assert saved["input"]["items"] == original["input"]["items"]
    assert [f["id"] for f in client.get(root + "/files").json()["data"]["items"]] == [
        uploaded.json()["data"]["file"]["id"]
    ]
    # A stale selection cannot change the department independently of the group.
    assert client.put(root + "/related-approvals", headers=headers, json=body).status_code == 409
    assert client.get(root).json()["data"] == saved
    cleared = client.put(
        root + "/related-approvals",
        headers=headers,
        json={
            "expectedRevision": saved["revision"],
            "selections": [],
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["relatedApprovals"] == []
    assert cleared.json()["data"]["department"] == saved["department"]
    with client.app.state.database_session_factory() as database:
        assert len(database.scalars(select(ReimbursementDraft)).all()) == 1
