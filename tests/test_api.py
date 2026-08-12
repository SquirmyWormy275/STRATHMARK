"""Tests for strathmark/api.py — FastAPI REST endpoints."""

from datetime import date

import pytest

pytest.importorskip(
    "fastapi",
    reason="FastAPI not installed -- install with: pip install -e '.[api]'",
)

from fastapi.testclient import TestClient  # noqa: E402

from strathmark.api import (  # noqa: E402
    _SIMULATION_SLOTS,
    app,
    get_ledger,
    get_prediction_provider,
    get_store,
)
from strathmark.ledger import PredictionLedger  # noqa: E402
from strathmark.prediction_v2 import ForecastInterval, PredictiveDistribution  # noqa: E402
from strathmark.predictor import PredictionBundle, StaticPredictionProvider  # noqa: E402
from strathmark.store import ResultStore  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Use an isolated store and explicit API token for every API test."""
    store = ResultStore(db_path=tmp_path / "api-results.db")
    ledger = PredictionLedger(tmp_path / "api-ledger.db")
    monkeypatch.setenv("STRATHMARK_API_TOKEN", "test-api-token")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_ledger] = lambda: ledger
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def v2_provider():
    class Core:
        model_version = "api-core"
        source_checksum = "c" * 64

        class calibration:
            version = "api-cal"

        def __init__(self):
            self.cutoffs = []

        def predict(self, request, *, history=None, wood_df=None):
            self.cutoffs.append(request.prediction_as_of)
            return PredictiveDistribution(
                median=44.0,
                log_location=3.78,
                log_scale=0.2,
                interval=ForecastInterval(33.0, 58.0, calibration_state="calibrated"),
                source="conditional_population_prior",
                history_count=0,
                effective_history_weight=0.0,
                model_version=self.model_version,
                calibration_version="api-cal",
            )

    core = Core()
    return core, StaticPredictionProvider(PredictionBundle(core=core, source="api-test"))


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

    def test_health_reports_engine_components_separately(self, client, v2_provider):
        _, provider = v2_provider
        app.dependency_overrides[get_prediction_provider] = lambda: provider

        data = client.get("/health").json()

        assert data["prediction_engine"]["core"]["available"] is True
        assert data["prediction_engine"]["core"]["version"] == "api-core"
        assert "residual" in data["prediction_engine"]
        assert "calibration" in data["prediction_engine"]
        assert "cutoff" in data["prediction_engine"]
        assert "degraded" in data["prediction_engine"]
        assert "ollama_available" in data


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

    def test_public_calculate_is_stateless(self, client):
        response = client.post(
            "/calculate",
            json={
                "competitors": [{"name": "Alice", "competitor_id": "athlete-a"}],
                "wood": {"species": "Pine", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
                "prediction_as_of": "2026-08-11",
            },
        )
        assert response.status_code == 200
        assert response.json()[0]["prediction_id"] is None
        assert response.json()[0]["ledger_recorded"] is None

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

    def test_additive_identity_cutoff_and_provenance_fields(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [{"name": "A", "competitor_id": "C-1", "history": []}],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
                "prediction_as_of": "2026-08-11",
            },
        )

        assert resp.status_code == 200
        result = resp.json()[0]
        assert "interval" in result
        assert "engine_version" in result
        assert "prediction_id" in result
        assert "ledger_recorded" in result

    def test_duplicate_name_keyed_override_is_rejected(self, client):
        resp = client.post(
            "/calculate",
            json={
                "competitors": [
                    {"name": "Alex", "competitor_id": "C-1", "history": []},
                    {"name": "Alex", "competitor_id": "C-2", "history": []},
                ],
                "wood": {"species": "poplar", "diameter_mm": 300, "quality": 5},
                "event_code": "SB",
                "manual_overrides": {"Alex": 40.0},
            },
        )

        assert resp.status_code == 422
        assert "ambiguous" in resp.json()["detail"].lower()


class TestTrustedLedgerEndpoints:
    @staticmethod
    def _request(request_id="trusted-field", diameter_mm=300):
        return {
            "request_id": request_id,
            "competitors": [
                {"name": "Alice", "competitor_id": "athlete-a", "gender": "F"},
                {"name": "Bob", "competitor_id": "athlete-b", "gender": "M"},
            ],
            "wood": {"species": "Pine", "diameter_mm": diameter_mm, "quality": 5},
            "event_code": "SB",
            "prediction_as_of": "2026-08-11",
        }

    def test_ledger_calculate_requires_fail_closed_bearer(self, client, monkeypatch):
        assert client.post("/ledger/calculate", json=self._request()).status_code == 401
        monkeypatch.delenv("STRATHMARK_API_TOKEN")
        assert client.post("/ledger/calculate", json=self._request()).status_code == 503

    def test_ledger_calculate_requires_stable_ids(self, client, api_headers):
        payload = self._request()
        payload["competitors"][0].pop("competitor_id")
        response = client.post("/ledger/calculate", json=payload, headers=api_headers)
        assert response.status_code == 422
        assert "competitor_id" in response.json()["detail"]

    def test_ledger_calculate_records_and_retries_original_ids(
        self, client, api_headers, v2_provider
    ):
        _, provider = v2_provider
        app.dependency_overrides[get_prediction_provider] = lambda: provider

        first = client.post("/ledger/calculate", json=self._request(), headers=api_headers)
        retry = client.post("/ledger/calculate", json=self._request(), headers=api_headers)

        assert first.status_code == 200
        assert retry.status_code == 200
        assert [row["prediction_id"] for row in retry.json()] == [
            row["prediction_id"] for row in first.json()
        ]
        assert all(row["ledger_recorded"] is True for row in first.json())
        assert all(row["ledger_status"] == "duplicate" for row in retry.json())
        assert all("optimizer_metadata" in row for row in first.json())

    def test_ledger_calculate_key_payload_conflict_is_409(self, client, api_headers):
        assert (
            client.post("/ledger/calculate", json=self._request(), headers=api_headers).status_code
            == 200
        )
        changed = self._request(diameter_mm=325)
        response = client.post("/ledger/calculate", json=changed, headers=api_headers)
        assert response.status_code == 409

    def test_settle_requires_auth_and_appends_correction(self, client, api_headers, monkeypatch):
        monkeypatch.setenv("STRATHMARK_LEDGER_ACTOR", "api-official")
        calculated = client.post(
            "/ledger/calculate", json=self._request(), headers=api_headers
        ).json()
        prediction_id = calculated[0]["prediction_id"]
        payload = {
            "competitor_id": calculated[0]["competitor_id"],
            "event_code": "SB",
            "actual_time": 45.0,
        }
        url = f"/ledger/predictions/{prediction_id}/settle"
        assert client.post(url, json=payload).status_code == 401

        first = client.post(url, json=payload, headers=api_headers)
        retry = client.post(url, json=payload, headers=api_headers)
        assert first.status_code == 200
        assert first.json()["actor"] == "api-official"
        assert retry.json()["status"] == "duplicate"

        changed = dict(payload, actual_time=44.5)
        assert client.post(url, json=changed, headers=api_headers).status_code == 409
        changed["reason"] = "Timing review"
        correction = client.post(url, json=changed, headers=api_headers)
        assert correction.status_code == 200
        assert correction.json()["revision"] == 2
        assert correction.json()["supersedes_settlement_id"] == first.json()["settlement_id"]


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

    def test_predict_consumes_cutoff_and_is_publicly_stateless(
        self, client, v2_provider, monkeypatch
    ):
        core, provider = v2_provider
        app.dependency_overrides[get_prediction_provider] = lambda: provider
        monkeypatch.setattr(
            "strathmark.api.get_store",
            lambda: (_ for _ in ()).throw(AssertionError("public prediction touched store")),
        )

        resp = client.post(
            "/predict",
            json={
                "competitor": {
                    "name": "A",
                    "history": [
                        {
                            "event_code": "SB",
                            "time_seconds": 45.0,
                            "species": "S01",
                            "diameter_mm": 300,
                            "quality": 1,
                            "result_date": "2025-05-01",
                            "heat_id": "inactive",
                        }
                    ],
                    "division": "inactive",
                    "tournament_time": 9.0,
                },
                "wood": {"species": "S01", "diameter_mm": 300, "quality": 10},
                "event_code": "SB",
                "prediction_as_of": "2025-06-01",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert core.cutoffs == [date(2025, 6, 1)]
        assert data["best"]["method_used"] == "baseline"
        assert data["all_predictions"]["llm"] is None


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
