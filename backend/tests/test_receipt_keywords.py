from __future__ import annotations

from app.domain.categories import ExpenseCategory
from app.models.receipt_keyword import ReceiptKeywordMapping
from app.ocr.parsers import ReceiptParserRegistry
from app.ocr.types import OcrLine, ParseContext
from app.services.receipt_keywords import load_receipt_keyword_rules
from tests.conftest import mock_login


def test_receipt_keyword_crud_is_admin_only_and_validated(client_factory) -> None:
    user = client_factory(auth_mock_enabled=True)
    user_login = mock_login(user)
    assert user.get("/api/admin/receipt-keywords").status_code == 403
    assert (
        user.post(
            "/api/admin/receipt-keywords",
            json={"keyword": "文具", "categoryId": "office"},
            headers={"X-CSRF-Token": user_login["csrfToken"]},
        ).status_code
        == 403
    )

    admin = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="admin-1",
        admin_user_ids="admin-1",
    )
    csrf = mock_login(admin)["csrfToken"]
    created = admin.post(
        "/api/admin/receipt-keywords",
        json={"keyword": " 文具 ", "categoryId": "office"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201
    mapping = created.json()["data"]
    assert mapping == {
        "id": mapping["id"],
        "keyword": "文具",
        "categoryId": "office",
        "categoryName": "办公费",
    }
    assert admin.get("/api/admin/receipt-keywords").json()["data"] == [mapping]

    duplicate = admin.post(
        "/api/admin/receipt-keywords",
        json={"keyword": "  文具  ", "categoryId": "hospitality"},
        headers={"X-CSRF-Token": csrf},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "RECEIPT_KEYWORD_EXISTS"

    for invalid in (
        {"keyword": "水", "categoryId": "utilities"},
        {"keyword": "任意关键词", "categoryId": "subsidy"},
        {"keyword": "任意关键词", "categoryId": "other"},
        {"keyword": "任意关键词", "categoryId": "unknown"},
    ):
        assert (
            admin.post(
                "/api/admin/receipt-keywords",
                json=invalid,
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 422
        )

    updated = admin.put(
        f"/api/admin/receipt-keywords/{mapping['id']}",
        json={"keyword": "办公用品", "categoryId": "office"},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["keyword"] == "办公用品"

    deleted = admin.delete(
        f"/api/admin/receipt-keywords/{mapping['id']}",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 200
    assert admin.get("/api/admin/receipt-keywords").json()["data"] == []


def test_seeded_keyword_can_be_edited_moved_and_deleted(client_factory) -> None:
    admin = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="admin-1",
        admin_user_ids="admin-1",
    )
    csrf = mock_login(admin)["csrfToken"]
    with admin.app.state.database_session_factory() as database:
        mapping = ReceiptKeywordMapping(
            keyword="酒店",
            normalized_keyword="酒店",
            category_id="lodging",
        )
        database.add(mapping)
        database.commit()
        mapping_id = mapping.id

    listed = admin.get("/api/admin/receipt-keywords").json()["data"]
    assert listed == [
        {
            "id": mapping_id,
            "keyword": "酒店",
            "categoryId": "lodging",
            "categoryName": "住宿费",
        }
    ]

    updated = admin.put(
        f"/api/admin/receipt-keywords/{mapping_id}",
        json={"keyword": "酒店住宿", "categoryId": "hospitality"},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200
    assert updated.json()["data"] == {
        **listed[0],
        "keyword": "酒店住宿",
        "categoryId": "hospitality",
        "categoryName": "招待费",
    }

    deleted = admin.delete(
        f"/api/admin/receipt-keywords/{mapping_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 200
    assert admin.get("/api/admin/receipt-keywords").json()["data"] == []


def test_transport_keyword_groups_are_editable_and_control_recognition(client_factory):
    admin = client_factory(auth_mock_enabled=True, auth_mock_user_id="bootstrap-admin")
    headers = {"X-CSRF-Token": mock_login(admin)["csrfToken"]}
    body = {"keyword": "示例出行", "categoryId": "local_transport", "transportType": "ride_hailing"}
    assert admin.post("/api/admin/receipt-keywords", json=body).status_code == 403
    created = admin.post("/api/admin/receipt-keywords", headers=headers, json=body)
    assert created.status_code == 201
    mapping = created.json()["data"]
    assert mapping["transportType"] == "ride_hailing"
    document = [
        OcrLine(value, 1)
        for value in ("电子发票 旅客运输服务", "销售方名称：示例出行公司", "价税合计 ¥20.00")
    ]
    for transport_type, required in (("ride_hailing", True), ("taxi", False)):
        body["transportType"] = transport_type
        response = admin.put(
            f"/api/admin/receipt-keywords/{mapping['id']}", headers=headers, json=body
        )
        assert response.status_code == 200
        with admin.app.state.database_session_factory() as database:
            rules = load_receipt_keyword_rules(database)
        parsed = ReceiptParserRegistry(keyword_rules=rules).parse(document, ParseContext(2026))
        assert parsed.category is ExpenseCategory.LOCAL_TRANSPORT
        assert parsed.transport_type == transport_type
        assert parsed.requires_itinerary is required
    body["transportType"] = "other"
    assert (
        admin.put(
            f"/api/admin/receipt-keywords/{mapping['id']}", headers=headers, json=body
        ).json()["data"]["transportType"]
        == "other"
    )
    with admin.app.state.database_session_factory() as database:
        rules = load_receipt_keyword_rules(database)
    parsed = ReceiptParserRegistry(keyword_rules=rules).parse(document, ParseContext(2026))
    assert parsed.category is ExpenseCategory.LOCAL_TRANSPORT
    assert parsed.transport_type is None and not parsed.requires_itinerary
    for invalid in ({**body, "transportType": "unknown"}, {**body, "categoryId": "office"}):
        assert (
            admin.put(
                f"/api/admin/receipt-keywords/{mapping['id']}", headers=headers, json=invalid
            ).status_code
            == 422
        )
    body.pop("transportType")
    body["categoryId"] = "office"
    moved = admin.put(f"/api/admin/receipt-keywords/{mapping['id']}", headers=headers, json=body)
    assert moved.status_code == 200 and "transportType" not in moved.json()["data"]
    with admin.app.state.database_session_factory() as database:
        rules = load_receipt_keyword_rules(database)
    parsed = ReceiptParserRegistry(keyword_rules=rules).parse(document, ParseContext(2026))
    assert parsed.category is ExpenseCategory.OFFICE and not parsed.requires_itinerary
    assert (
        admin.delete(f"/api/admin/receipt-keywords/{mapping['id']}", headers=headers).status_code
        == 200
    )
    with admin.app.state.database_session_factory() as database:
        assert load_receipt_keyword_rules(database) == ()
