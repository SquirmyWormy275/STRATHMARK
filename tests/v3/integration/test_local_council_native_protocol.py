import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from strathmark.v3.assessors.output_validation import LLM_OUTPUT_SCHEMA_VERSION, REQUIRED_QUANTILES
from strathmark.v3.factory.local_council_cli import LocalCouncilClient
from tests.v3.property.test_ml_temporal_features import _observation, _packet


@pytest.fixture
def local_native_provider(tmp_path):
    binary = tmp_path / "synthetic-runtime"
    binary.write_bytes(b"synthetic local runtime")
    members = [
        {
            "member_id": "member_" + str(i),
            "provider_id": "provider_" + str(i),
            "family": "family_" + str(i),
            "model_id": "model:" + str(i),
            "model_digest": str(i + 1) * 64,
            "quantization": "Q4_K_M",
        }
        for i in range(3)
    ]
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/version":
                self.respond({"version": "0.32.15"})
            else:
                self.respond(
                    {
                        "models": [
                            {
                                "name": m["model_id"],
                                "digest": m["model_digest"],
                                "details": {"quantization_level": m["quantization"]},
                            }
                            for m in members
                        ]
                    }
                )

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(payload)
            packet = json.loads(
                payload["prompt"].split("UNTRUSTED_JSON_DATA\n")[1].split("\nResponse schema:")[0]
            )
            output = {
                "schema_version": LLM_OUTPUT_SCHEMA_VERSION,
                "state": "committed",
                "quantiles": [
                    {"probability": p, "time_ms": 30000 + i * 2000}
                    for i, p in enumerate(REQUIRED_QUANTILES)
                ],
                "evidence_refs": [o["evidence_ref"] for o in packet["observations"]],
                "warnings": [],
                "fact_codes": ["observed_raw_time"],
                "abstention_reason": None,
            }
            self.respond({"model": payload["model"], "done": True, "response": json.dumps(output)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "origin": f"http://127.0.0.1:{server.server_port}",
        "runtime_binary": str(binary),
        "runtime_version": "ollama:0.32.15",
        "runtime_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "members": members,
    }
    try:
        yield config, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_native_envelope_schema_sampling_and_local_mixture(local_native_provider, monkeypatch):
    config, calls = local_native_provider
    monkeypatch.setenv("HTTP_PROXY", "http://external.invalid:8080")
    client = LocalCouncilClient(config)
    result = client.evaluate(_packet((_observation(1, 40000, day=1),)))
    assert len(calls) == 3
    assert all(
        p["stream"] is False and p["format"]["type"] == "object" and p["options"]["seed"] == 1729
        for p in calls
    )
    assert result["availability"] == "normal"
    assert result["numeric_promotion"] is False
    assert result["authority_class"] == "local_development_candidate"
    assert result["distribution"] is not None
    assert all(len(m["attempts"]) == 1 for m in result["members"])
    for payload, receipt in zip(calls, result["members"], strict=True):
        packet = receipt["provider_packet"]
        assert packet["schema_version"] == "strathmark-v3-local-legacy-provider-packet-v2"
        assert all("issued_mark" not in observation for observation in packet["observations"])
        wire_packet = json.loads(
            payload["prompt"].split("UNTRUSTED_JSON_DATA\n")[1].split("\nResponse schema:")[0]
        )
        assert wire_packet == packet


def test_member_schema_binds_references_and_both_response_states(local_native_provider):
    config, calls = local_native_provider
    client = LocalCouncilClient(config)
    result = client.evaluate(_packet((_observation(1, 40000, day=1),)))
    for payload, receipt in zip(calls, result["members"], strict=True):
        committed, abstained = payload["format"]["anyOf"]
        refs = [o["evidence_ref"] for o in receipt["provider_packet"]["observations"]]
        assert committed["properties"]["evidence_refs"] == {"const": refs}
        quantiles = committed["properties"]["quantiles"]
        assert quantiles["minItems"] == quantiles["maxItems"] == 7
        assert [q["properties"]["probability"]["const"] for q in quantiles["prefixItems"]] == list(
            REQUIRED_QUANTILES
        )
        assert abstained["properties"]["quantiles"] == {"const": []}
        assert abstained["properties"]["evidence_refs"] == {"const": []}
        assert abstained["properties"]["fact_codes"] == {"const": []}
        assert committed["properties"]["fact_codes"] == {
            "const": ["declared_time_conversion", "observed_raw_time"]
        }


def test_empty_history_schema_allows_only_abstention(local_native_provider):
    config, calls = local_native_provider
    client = LocalCouncilClient(config)
    client.evaluate_member(_packet(()), client.members[0])
    assert all(p["format"]["properties"]["state"] == {"const": "abstained"} for p in calls)


def test_relevant_context_history_keeps_older_matching_results():
    from dataclasses import replace

    from strathmark.v3.factory.local_council_cli import select_local_history

    exact = _observation(1, 180000, day=1)
    recent_other_event = tuple(
        replace(
            _observation(i, 30000, day=i),
            context=replace(exact.context, event_code="standing_block"),
        )
        for i in range(2, 16)
    )
    selected = select_local_history((exact, *recent_other_event), exact.context)
    assert exact in selected
    assert len(selected) == 12
    assert selected == tuple(
        sorted(selected, key=lambda o: (o.occurred_at_utc, o.observation_sequence))
    )


def test_declared_conversion_preserves_raw_fact_and_marks_missing_properties():
    from dataclasses import replace

    from strathmark.v3.factory.local_council_cli import add_declared_conversions

    original = _observation(1, 90000, day=1)
    target = replace(original.context, size_mm=600)
    projected = {"observations": [{"observation_sequence": 1, "raw_time_ms": 90000}]}
    facts = add_declared_conversions(projected, _packet((original,), target=target))
    assert projected["observations"][0]["raw_time_ms"] == 90000
    assert projected["observations"][0]["target_equivalent_time_ms"] == 360000
    assert "declared_time_conversion" in facts
    unsupported = replace(target, material_code="unknown", properties=())
    projected = {"observations": [{"observation_sequence": 1, "raw_time_ms": 90000}]}
    facts = add_declared_conversions(projected, _packet((original,), target=unsupported))
    assert projected["observations"][0]["target_equivalent_time_ms"] is None
    assert facts == ("observed_raw_time",)


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON",
        b"\xff",
        b"[]",
        b'{"model":"another","done":true,"response":"{}"}',
        b'{"model":"model:0","done":false,"response":"{}"}',
        b'{"model":"model:0","done":true}',
        b'{"model":"model:0","done":true,"response":42}',
    ],
)
def test_invalid_envelopes_keep_each_attempt_and_raw_bytes(local_native_provider, monkeypatch, raw):
    import base64

    from strathmark.v3.contracts.canonical import canonical_digest

    config, _ = local_native_provider
    client = LocalCouncilClient(config)
    original = client.request
    requests = []

    def request(method, path, payload=None, **kwargs):
        if method == "POST":
            requests.append(payload)
            return raw
        return original(method, path, payload, **kwargs)

    monkeypatch.setattr(client, "request", request)
    receipt = client.evaluate_member(_packet((_observation(1, 40000, day=1),)), client.members[0])
    assert receipt["distribution"] is None
    assert len(receipt["attempts"]) == len(requests) == 2
    for attempt, payload in zip(receipt["attempts"], requests, strict=True):
        retained = (
            attempt["raw_envelope"].encode()
            if "raw_envelope" in attempt
            else base64.b64decode(attempt["raw_envelope_base64"])
        )
        assert retained == raw
        assert attempt["raw_envelope_sha256"] == hashlib.sha256(raw).hexdigest()
        assert attempt["request_digest"] == canonical_digest(payload)
        assert attempt["validator_code"]


@pytest.mark.parametrize("field", ["model_id", "model_digest"])
def test_duplicate_models_rejected_before_inference(local_native_provider, field):
    config, calls = local_native_provider
    config["members"][1][field] = config["members"][0][field]
    with pytest.raises(ValueError, match="distinct"):
        LocalCouncilClient(config)
    assert calls == []


def test_changed_binary_fails_before_inference(local_native_provider):
    config, calls = local_native_provider
    client = LocalCouncilClient(config)
    from pathlib import Path

    Path(config["runtime_binary"]).write_bytes(b"changed runtime")
    with pytest.raises(ValueError, match="checksum"):
        client.evaluate(_packet((_observation(1, 40000, day=1),)))
    assert calls == []


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1",
        "http://example.com",
        "http://localhost",
        "http://127.0.0.1/path",
        "http://user:password@127.0.0.1",
    ],
)
def test_nonlocal_origins_rejected_without_a_provider_call(origin):
    with pytest.raises(ValueError):
        LocalCouncilClient({"origin": origin})
