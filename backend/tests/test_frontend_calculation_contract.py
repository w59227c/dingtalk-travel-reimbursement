from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import mock_login


def test_actual_frontend_totals_serialization_reaches_backend_calculation(client_factory):
    """Exercise the real TS serializer against FastAPI, not two matching mocks."""
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    node = shutil.which("node")
    if node is None:
        pytest.skip("cross-stack contract requires Node.js")
    script = r"""
import { pathToFileURL } from 'node:url';

const moduleUrl = pathToFileURL('src/api/calculateTotalsPayload.ts');
const { buildCalculateTotalsPayload } = await import(moduleUrl.href);
const payload = buildCalculateTotalsPayload({
  tripType: 'project', startDate: '2026-09-01', endDate: '2026-09-30',
  startTime: '09:00', endTime: '18:00', policyConfirmed: false,
}, [{
  id: 'ocr-test-file', source: 'ocr', category: 'local_transport',
  date: '2026-09-01', displayDate: '2026-09-01', description: '机场至酒店',
  amount: '44.89', receiptCount: 1, sourceFileId: 'test-file',
  itineraryFileIds: ['support-file'], transportType: 'ride_hailing',
  requiresItinerary: true, cnyAmountConfirmed: false,
}]);
process.stdout.write(JSON.stringify(payload));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "--experimental-strip-types", "-e", script],
        cwd=frontend,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    payload = json.loads(completed.stdout)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = client.post("/api/calculate/totals", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["expenseTotal"] == "44.89"
    assert result["subsidy"]["calendarDays"] == 30
    assert result["subsidy"]["effectiveDays"] == "30.0"
    assert "sourceFileId" not in payload["items"][0]

    # The exact previous payload explains the user's 422, independently of OCR.
    payload["items"][0].pop("id")
    payload["items"][0].pop("source")
    previous = client.post("/api/calculate/totals", json=payload, headers=headers)
    assert previous.status_code == 422
