from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

from conftest import mock_login


def writable(settings):
    return {
        key: deepcopy(settings[key])
        for key in (
            "subsidyRates",
            "calculationMode",
            "appTitle",
            "adminUserIds",
        )
    } | {"expectedRevision": settings.get("revision", "0" * 64)}


def test_stale_settings_cannot_roll_back_other_tabs_title_or_administrators(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="synthetic-admin",
        admin_user_ids="synthetic-admin",
    )
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    initial = client.get("/api/admin/settings").json()["data"]
    tab_a, tab_b = writable(initial), writable(initial)
    tab_a.update(appTitle="Synthetic updated title", adminUserIds=["synthetic-added-admin"])
    saved = client.put("/api/admin/settings", json=tab_a, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["revision"] != tab_a["expectedRevision"]
    tab_b["subsidyRates"]["business"] = "110.00"
    stale = client.put("/api/admin/settings", json=tab_b, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "SETTINGS_REVISION_CONFLICT"
    current = client.get("/api/admin/settings").json()["data"]
    assert current["appTitle"] == tab_a["appTitle"]
    assert current["adminUserIds"] == tab_a["adminUserIds"]
    assert current["subsidyRates"]["business"] == initial["subsidyRates"]["business"]
    assert "revision" not in client.get("/api/settings").json()["data"]


def test_retry_of_an_already_saved_settings_payload_is_idempotent(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="synthetic-admin",
        admin_user_ids="synthetic-admin",
    )
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    body = writable(client.get("/api/admin/settings").json()["data"])
    body["appTitle"] = "Synthetic new title"
    first = client.put("/api/admin/settings", json=body, headers=headers)
    assert first.status_code == 200, first.text
    repeated = client.put("/api/admin/settings", json=body, headers=headers)
    assert repeated.status_code == 200
    assert repeated.json()["data"] == first.json()["data"]
    del body["expectedRevision"]
    assert client.put("/api/admin/settings", json=body, headers=headers).status_code == 422


def test_simultaneous_settings_writers_cannot_both_commit_the_same_revision(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="synthetic-admin",
        admin_user_ids="synthetic-admin",
    )
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    initial = client.get("/api/admin/settings").json()["data"]
    bodies = [writable(initial), writable(initial)]
    bodies[0]["appTitle"] = "Synthetic concurrent title"
    bodies[1]["subsidyRates"]["business"] = "120.00"
    ready = Barrier(2)

    def save(body):
        ready.wait(timeout=5)
        return client.put("/api/admin/settings", json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(save, bodies))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()["data"] for response in responses if response.status_code == 200)
    assert client.get("/api/admin/settings").json()["data"] == winner
