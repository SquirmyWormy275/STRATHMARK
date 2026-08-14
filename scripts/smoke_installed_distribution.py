"""Smoke-test an installed wheel or sdist outside the source checkout."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

INSTALLED_SHADOW_SMOKE = textwrap.dedent(
    r"""
    import json
    import os
    import time
    from datetime import date, datetime, timezone
    from pathlib import Path

    consumer = "missoula:service:installed-smoke"
    actor = "missoula:operator:installed-smoke"
    token = "installed-smoke-service-token"
    signing_key = "installed-smoke-attestation-key"
    database = Path.cwd() / "installed-shadow-smoke.db"
    os.environ["STRATHMARK_DB_PATH"] = str(database)
    os.environ["STRATHMARK_TRUSTED_TOPOLOGY"] = "offline-single-writer-durable"
    os.environ["STRATHMARK_SHADOW_SERVICE_CREDENTIALS"] = json.dumps({consumer: token})
    os.environ["STRATHMARK_SHADOW_ATTESTATION_KEYS"] = json.dumps({consumer: signing_key})
    for name in (
        "STRATHMARK_SUPABASE_URL",
        "STRATHMARK_SUPABASE_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
    ):
        os.environ.pop(name, None)

    from fastapi.testclient import TestClient
    from strathmark.api import app, get_ledger, get_shadow_service, get_store
    from strathmark.auth import (
        ACTOR_ATTESTATION_SCHEMA_VERSION,
        REQUEST_DIGEST_SCHEMA_VERSION,
        SHADOW_ATTESTATION_AUDIENCE,
        canonical_shadow_request_digest,
        sign_actor_attestation,
    )
    from strathmark.consumer_contract import (
        EXPECTED_SHADOW_CONSUMER_PATHS,
        load_shadow_consumer_contract,
        shadow_consumer_contract_digest,
    )
    from strathmark.predictor import FilePredictionProvider
    from strathmark.shadow import (
        OBSERVATION_SCHEMA_VERSION,
        SHADOW_TARGET_SINGLE_ELAPSED,
        ShadowPredictionService,
    )
    from strathmark.store import (
        EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        EvidenceSnapshotPayload,
        ResultStore,
        canonical_evidence_source_digest,
    )

    contract = load_shadow_consumer_contract()
    digest = shadow_consumer_contract_digest(document=contract)
    assert set(contract["paths"]) == EXPECTED_SHADOW_CONSUMER_PATHS
    assert len(digest) == 64
    live_openapi = app.openapi()
    for path, path_item in contract["paths"].items():
        assert live_openapi["paths"][path] == path_item
    for component_kind, components in contract["components"].items():
        for name, component in components.items():
            assert live_openapi["components"][component_kind][name] == component
    cutoff = date.today()
    captured_at = datetime.now(timezone.utc)
    source_id = "installed-smoke:history-export:empty"
    source_digest = canonical_evidence_source_digest(
        source_id=source_id,
        cutoff=cutoff,
        captured_at=captured_at,
        rows=(),
    )
    evidence = EvidenceSnapshotPayload(
        schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        source_id=source_id,
        cutoff=cutoff,
        captured_at=captured_at,
        rows=(),
        source_digest=source_digest,
    )

    class OfflineSource:
        def load_snapshot(self, *, cutoff):
            assert cutoff == evidence.cutoff
            return evidence

    store = ResultStore(database)
    store.refresh_evidence_snapshot(OfflineSource(), cutoff=cutoff)
    ledger = store.prediction_ledger()
    provider = FilePredictionProvider()
    assert provider.snapshot(cutoff).core is not None
    service = ShadowPredictionService(ledger, result_store=store, prediction_provider=provider)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_ledger] = lambda: ledger
    app.dependency_overrides[get_shadow_service] = lambda: service

    def headers(action, revision, request, nonce):
        now = int(time.time())
        claims = {
            "schema_version": ACTOR_ATTESTATION_SCHEMA_VERSION,
            "consumer_id": consumer,
            "actor_id": actor,
            "roles": ["judge"],
            "action": action,
            "subject_revision": revision,
            "request_digest_schema_version": REQUEST_DIGEST_SCHEMA_VERSION,
            "request_digest": canonical_shadow_request_digest(request),
            "audience": SHADOW_ATTESTATION_AUDIENCE,
            "nonce": nonce,
            "issued_at": now,
            "expires_at": now + 30,
        }
        return {
            "Authorization": f"Bearer {token}",
            "X-STRATHMARK-Actor-Attestation": sign_actor_attestation(claims, signing_key),
        }

    calculate = {
        "schema_version": "strathmark.shadow-calculate.v1",
        "consumer_id": consumer,
        "tournament_id": "missoula:tournament:installed-smoke",
        "event_occurrence_id": "missoula:event:installed-smoke-sb",
        "field_run_id": "missoula:field-run:installed-smoke",
        "operator_id": actor,
        "request_id": "missoula:request:installed-smoke",
        "run_revision": "missoula:run-revision:installed-smoke",
        "event_code": "SB",
        "target_contract": SHADOW_TARGET_SINGLE_ELAPSED,
        "prediction_as_of": cutoff.isoformat(),
        "schedule_fingerprint": "1" * 64,
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "observation_fingerprint": "2" * 64,
        "competitors": [
            {"competitor_id": "missoula:competitor:installed-smoke", "gender": "M"}
        ],
        "wood": {"species": "Pine", "diameter_mm": 300, "quality": 7},
        "timeout_ms": 5000,
    }
    calculate_schema = contract["components"]["schemas"]["CalculateRequest"]
    assert calculate["schema_version"] == calculate_schema["properties"]["schema_version"]["const"]
    client = TestClient(app)
    calculated = client.post(
        "/v1/shadow/calculate",
        json=calculate,
        headers=headers(
            "shadow.calculate",
            calculate["run_revision"],
            calculate,
            "installed-smoke-calculate-001",
        ),
    )
    assert calculated.status_code == 200, calculated.text
    calculated_json = calculated.json()
    assert calculated_json["trusted"] is True
    assert calculated_json["receipt"]["status"]["ready_for_review"] is True

    lookup = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": consumer,
        "request_id": calculate["request_id"],
        "run_revision": calculate["run_revision"],
    }
    recovered = client.post(
        "/v1/shadow/receipts/lookup",
        json=lookup,
        headers=headers(
            "shadow.receipt.lookup",
            lookup["run_revision"],
            lookup,
            "installed-smoke-lookup-001",
        ),
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["receipt"]["core_json"] == calculated_json["receipt"]["core_json"]
    assert recovered.json()["receipt"]["status"]["ready_for_review"] is True
    app.dependency_overrides.clear()
    print("installed distribution authenticated offline shadow smoke OK")
    """
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("wheel", "sdist"), required=True)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--offline",
        action="store_true",
        help="reuse installed dependencies and disable package-index access",
    )
    args = parser.parse_args(argv)
    pattern = "*.whl" if args.kind == "wheel" else "*.tar.gz"
    candidates = sorted(args.dist_dir.resolve().glob(pattern))
    if len(candidates) != 1:
        raise SystemExit(
            f"expected exactly one {args.kind} in {args.dist_dir}, found {len(candidates)}"
        )

    with tempfile.TemporaryDirectory(prefix=f"strathmark-{args.kind}-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv_command = [sys.executable, "-m", "venv"]
        if args.offline:
            venv_command.append("--system-site-packages")
        subprocess.run([*venv_command, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install_command = [str(python), "-m", "pip", "install"]
        if args.offline:
            install_command.extend(
                ["--no-index", "--no-deps", "--no-build-isolation", "--no-cache-dir"]
            )
        subprocess.run(
            [*install_command, f"{candidates[0]}[api]"],
            check=True,
            cwd=root,
        )
        if not args.offline:
            subprocess.run([str(python), "-m", "pip", "check"], check=True, cwd=root)
        subprocess.run(
            [
                str(python),
                "-c",
                INSTALLED_SHADOW_SMOKE,
            ],
            check=True,
            cwd=root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
