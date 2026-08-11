"""Tests for strathmark/api.py — FastAPI REST endpoints."""

import pytest

pytest.importorskip(
    "fastapi",
    reason="FastAPI not installed -- install with: pip install -e '.[api]'",
)

from fastapi.testclient import TestClient  # noqa: E402

from strathmark.api import _SIMULATION_SLOTS, app, get_store  # noqa: E402
from strathmark.store import ResultStore  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Use an isolated store and explicit API token for every API test."""
    store = ResultStore(db_path=tmp_path / "api-results.db")
    monkeypatch.setenv("STRATHMARK_API_TOKEN", "test-api-token")
    app.dependency_overrides[get_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def api_headers():
    return {"Authorization": "Bearer test-api-token"}


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "ollama_available" in data
        assert "store_results_count" in data
        assert "store_path" not in data


class TestCalculateEndpoint:
    def test_valid_request(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [
                    {
                        "name": "Alice",
                        "history": [
                            {
                                "event_code": "SB",
                                "time_seconds": 45.0,
                                "species": "poplar",
                                "diameter_mm": 300,
                                "quality": 5,
                            }
                        ],
                    },
                    {
                        "name": "Bob",
                        "history": [
                            {
                                "event_code": "SB",
                                "time_seconds": 55.0,
                                "species": "poplar",
                                "diameter_mm": 300,
                                "quality": 5,
                            }
                        ],
                    },
                ],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        marks = [r["mark"] for r in data]
        assert all(m >= 3 for m in marks)

    def test_empty_competitors_returns_400(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
            },
        )
        assert resp.status_code == 400

    def test_missing_fields_returns_422(self, client):
        resp = client.post("/calculate", json={})
        assert resp.status_code == 422

    def test_invalid_event_code_returns_422(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [{"name": "A", "history": []}],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "INVALID",
            },
        )
        assert resp.status_code == 422

    def test_negative_time_rejected(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [
                    {
                        "name": "A",
                        "history": [
                            {
                                "event_code": "SB",
                                "time_seconds": -5.0,
                                "species": "poplar",
                                "diameter_mm": 300,
                                "quality": 5,
                            }
                        ],
                    },
                ],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
            },
        )
        assert resp.status_code == 422

    def test_zero_diameter_rejected(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [{"name": "A", "history": []}],
                "wood": {"species": "poplar", "diameter_mm": 0, "quality": 5},
                "event_code": "SB",
            },
        )
        assert resp.status_code == 422

    def test_quality_out_of_range_rejected(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [{"name": "A", "history": []}],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 0},
                "event_code": "SB",
            },
        )
        assert resp.status_code == 422

    def test_invalid_history_date_rejected(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [
                    {
                        "name": "A",
                        "history": [
                            {
                                "event_code": "SB",
                                "time_seconds": 30.0,
                                "species": "poplar",
                                "diameter_mm": 300,
                                "quality": 5,
                                "result_date": "not-a-date",
                            }
                        ],
                    }
                ],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
            },
        )
        assert resp.status_code == 422


class TestPredictEndpoint:
    def test_valid_request(self, client):
        resp = client.post(
            "/predict",
            json={
                "competitor": {
                    "name": "A",
                    "history": [
                        {
                            "event_code": "SB",
                            "time_seconds": 50.0,
                            "species": "poplar",
                            "diameter_mm": 300,
                            "quality": 5,
                        }
                    ],
                },
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "best" in data
        assert "all_predictions" in data
        assert data["best"]["predicted_time"] > 0

    def test_missing_fields_returns_422(self, client):
        resp = client.post("/predict", json={"competitor": {"name": "A"}})
        assert resp.status_code == 422


class TestSimulateEndpoint:
    def test_valid_request(self, client):
        resp = client.post(
            "/simulate",
            json={
                "competitors": [
                    {"name": "A", "mark": 3, "predicted_time": 50.0, "variance": 3.0},
                    {"name": "B", "mark": 13, "predicted_time": 40.0, "variance": 3.0},
                ],
                "num_simulations": 100,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "winner_counts" in data
        assert "winner_percentages" in data

    def test_single_competitor_rejected_at_the_boundary(self, client):
        resp = client.post(
            "/simulate",
            json={
                "competitors": [{"name": "A", "mark": 3, "predicted_time": 50.0}],
            },
        )
        assert resp.status_code == 422

    def test_zero_simulations_rejected_at_the_boundary(self, client):
        resp = client.post(
            "/simulate",
            json={
                "competitors": [
                    {"name": "A", "mark": 3, "predicted_time": 50.0},
                    {"name": "B", "mark": 13, "predicted_time": 40.0},
                ],
                "num_simulations": 0,
            },
        )
        assert resp.status_code == 422

    def test_oversized_simulation_rejected_at_the_boundary(self, client):
        competitors = [{"name": f"A{i}", "mark": 3, "predicted_time": 50.0} for i in range(17)]
        resp = client.post(
            "/simulate",
            json={"competitors": competitors, "num_simulations": 250_000},
        )
        assert resp.status_code == 422

    def test_simulation_returns_busy_when_server_capacity_is_full(self, client):
        assert _SIMULATION_SLOTS.acquire(blocking=False)
        assert _SIMULATION_SLOTS.acquire(blocking=False)
        try:
            resp = client.post(
                "/simulate",
                json={
                    "competitors": [
                        {"name": "A", "mark": 3, "predicted_time": 50.0},
                        {"name": "B", "mark": 13, "predicted_time": 40.0},
                    ],
                    "num_simulations": 10,
                },
            )
        finally:
            _SIMULATION_SLOTS.release()
            _SIMULATION_SLOTS.release()

        assert resp.status_code == 429


class TestResultsEndpoints:
    def test_record_and_retrieve(self, client, api_headers):
        import uuid

        unique_name = f"TestRunner-{uuid.uuid4().hex[:8]}"
        # Record
        resp = client.post(
            "/results",
            json={
                "competitor_name": unique_name,
                "event_code": "SB",
                "time_seconds": 42.0,
                "species": "poplar",
                "diameter_mm": 300,
                "quality": 5,
                "result_date": "2025-06-01",
                "competition_id": "audit-show-2025",
            },
            headers=api_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["inserted"] is True

        # Retrieve
        resp = client.get(f"/results/{unique_name}", headers=api_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_invalid_date_rejected(self, client, api_headers):
        resp = client.post(
            "/results",
            json={
                "competitor_name": "X",
                "event_code": "SB",
                "time_seconds": 50.0,
                "species": "poplar",
                "diameter_mm": 300,
                "quality": 5,
                "result_date": "not-a-date",
                "competition_id": "audit-show-2025",
            },
            headers=api_headers,
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        ("event_code", "time_seconds", "diameter_mm"),
        [
            ("INVALID", 50.0, 300),
            ("SB", 181.0, 300),
            ("SB", 50.0, 200),
        ],
    )
    def test_invalid_result_data_rejected(
        self, client, api_headers, event_code, time_seconds, diameter_mm
    ):
        resp = client.post(
            "/results",
            json={
                "competitor_name": "A",
                "event_code": event_code,
                "time_seconds": time_seconds,
                "species": "poplar",
                "diameter_mm": diameter_mm,
                "quality": 5,
                "competition_id": "audit-show-2025",
            },
            headers=api_headers,
        )
        assert resp.status_code == 422

    def test_results_require_an_api_token(self, client):
        resp = client.post(
            "/results",
            json={
                "competitor_name": "A",
                "event_code": "SB",
                "time_seconds": 50.0,
                "species": "poplar",
                "diameter_mm": 300,
                "quality": 5,
                "competition_id": "audit-show-2025",
            },
        )
        assert resp.status_code == 401

    def test_no_results_returns_empty(self, client, api_headers):
        resp = client.get("/results/NobodyExists", headers=api_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_wrong_method_returns_405(self, client):
        resp = client.get("/calculate")
        assert resp.status_code == 405

    def test_not_found_returns_404(self, client):
        resp = client.get("/nonexistent")
        assert resp.status_code == 404
