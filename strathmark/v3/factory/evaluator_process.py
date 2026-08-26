"""Canonical bounded file exchange for a separately hosted frozen evaluator process."""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import (
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
)
from strathmark.v3.factory.candidates import (
    CandidateBuilder,
    CandidateBundle,
    FactoryRole,
    RoleSnapshot,
)
from strathmark.v3.factory.evaluator import (
    AuditGenerationRegistry,
    EvaluationGate,
    FrozenEvaluationHarness,
    FrozenEvaluator,
    SignedEvaluationReport,
    evaluation_report_from_manifest,
    verify_evaluation_report,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
)

MAX_EVALUATOR_REQUEST_BYTES = 1024 * 1024
MAX_EVALUATOR_RESPONSE_BYTES = 256 * 1024


class EvaluatorExchangeError(RuntimeError):
    """Evaluator exchange is malformed, oversized, conflicting, or incomplete."""


@dataclass(frozen=True, slots=True)
class EvaluatorProcessRequest:
    candidate: CandidateBundle
    harness: FrozenEvaluationHarness
    metrics: Mapping[str, float]
    observed_audit_snapshot_digest: str
    created_at: str
    request_digest: str

    @classmethod
    def create(
        cls,
        *,
        candidate: CandidateBundle,
        harness: FrozenEvaluationHarness,
        metrics: Mapping[str, float],
        observed_audit_snapshot_digest: str,
        created_at: str,
    ) -> EvaluatorProcessRequest:
        normalized = {name: float(metrics[name]) for name in sorted(metrics)}
        shell = cls(
            candidate,
            harness,
            MappingProxyType(normalized),
            observed_audit_snapshot_digest,
            created_at,
            "0" * 64,
        )
        return cls(
            candidate,
            harness,
            shell.metrics,
            observed_audit_snapshot_digest,
            created_at,
            canonical_digest(shell.body()),
        )

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-evaluator-process-request-v1",
            "candidate": _candidate_value(self.candidate),
            "harness": self.harness.body(),
            "metrics": {
                name: canonical_decimal_string(self.metrics[name]) for name in sorted(self.metrics)
            },
            "observed_audit_snapshot_digest": self.observed_audit_snapshot_digest,
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.body(), "request_digest": self.request_digest}


@dataclass(frozen=True, slots=True)
class EvaluatorProcessResponse:
    request_digest: str
    report: SignedEvaluationReport

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-evaluator-process-response-v1",
            "request_digest": self.request_digest,
            "report_manifest": self.report.manifest.to_dict(),
        }


def write_evaluator_request(path: str | Path, request: EvaluatorProcessRequest) -> None:
    if not isinstance(
        request, EvaluatorProcessRequest
    ) or request.request_digest != canonical_digest(request.body()):
        raise EvaluatorExchangeError("evaluator request digest differs")
    _publish(Path(path), canonical_bytes(request.to_dict()), MAX_EVALUATOR_REQUEST_BYTES)


def run_evaluator_request(
    request_path: str | Path,
    response_path: str | Path,
    *,
    registry_path: str | Path,
    signer: P256Signer,
) -> EvaluatorProcessResponse:
    request = _read_request(Path(request_path))
    destination = Path(response_path)
    registry = AuditGenerationRegistry(registry_path)
    response = _read_evaluator_response(destination, missing_ok=True)
    if response is not None:
        if response.request_digest != request.request_digest:
            raise EvaluatorExchangeError("evaluator response conflicts with request")
        try:
            verified = verify_evaluation_report(
                response.report,
                trust_store=IntegrityTrustStore((signer.identity,)),
                expected_candidate=request.candidate,
                expected_harness=request.harness,
            )
        except Exception as exc:
            raise EvaluatorExchangeError(
                "existing evaluator response is untrusted or misbound"
            ) from exc
        if verified != response.report:
            raise EvaluatorExchangeError("existing evaluator response verification differs")
        recovered = _recover_durable_result(registry, request, signer)
        if recovered is None or recovered != response.report:
            raise EvaluatorExchangeError(
                "existing evaluator response differs from durable evaluator result"
            )
        return response
    recovered = _recover_durable_result(registry, request, signer)
    if recovered is not None:
        response = EvaluatorProcessResponse(request.request_digest, recovered)
        _publish(
            destination,
            canonical_bytes(response.to_dict()),
            MAX_EVALUATOR_RESPONSE_BYTES,
        )
        return response
    report = FrozenEvaluator(
        request.harness,
        registry,
        signer=signer,
    ).evaluate(
        request.candidate,
        metrics=request.metrics,
        observed_audit_snapshot_digest=request.observed_audit_snapshot_digest,
        created_at=request.created_at,
        request_digest=request.request_digest,
    )
    response = EvaluatorProcessResponse(request.request_digest, report)
    _publish(destination, canonical_bytes(response.to_dict()), MAX_EVALUATOR_RESPONSE_BYTES)
    return response


def _recover_durable_result(
    registry: AuditGenerationRegistry,
    request: EvaluatorProcessRequest,
    signer: P256Signer,
) -> SignedEvaluationReport | None:
    try:
        return registry.recover_evaluation(
            request.harness,
            request.candidate,
            request_digest=request.request_digest,
            trust_store=IntegrityTrustStore((signer.identity,)),
        )
    except Exception as exc:
        raise EvaluatorExchangeError(
            "durable evaluator result is untrusted, misbound, or conflicting"
        ) from exc


def read_evaluator_response(path: str | Path) -> EvaluatorProcessResponse:
    try:
        response = _read_evaluator_response(Path(path), missing_ok=False)
    except FileNotFoundError as exc:
        raise EvaluatorExchangeError("evaluator exchange is unreadable") from exc
    assert response is not None
    return response


def _read_evaluator_response(path: Path, *, missing_ok: bool) -> EvaluatorProcessResponse | None:
    try:
        value = _read_json(path, MAX_EVALUATOR_RESPONSE_BYTES)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if (
        set(value) != {"schema_version", "request_digest", "report_manifest"}
        or value.get("schema_version") != "strathmark-v3-evaluator-process-response-v1"
    ):
        raise EvaluatorExchangeError("evaluator response schema differs")
    try:
        manifest = SignedManifest.from_dict(value["report_manifest"])
        report = evaluation_report_from_manifest(manifest)
    except Exception as exc:
        raise EvaluatorExchangeError("evaluator report payload is malformed") from exc
    return EvaluatorProcessResponse(value["request_digest"], report)


def _read_request(path: Path) -> EvaluatorProcessRequest:
    value = _read_json(path, MAX_EVALUATOR_REQUEST_BYTES)
    expected = {
        "schema_version",
        "candidate",
        "harness",
        "metrics",
        "observed_audit_snapshot_digest",
        "created_at",
        "request_digest",
    }
    if set(value) != expected or value.get("schema_version") != (
        "strathmark-v3-evaluator-process-request-v1"
    ):
        raise EvaluatorExchangeError("evaluator request schema differs")
    candidate = _candidate_from_value(value["candidate"])
    harness = _harness_from_value(value["harness"])
    metrics = value["metrics"]
    if not isinstance(metrics, dict):
        raise EvaluatorExchangeError("evaluator metrics are malformed")
    request = EvaluatorProcessRequest.create(
        candidate=candidate,
        harness=harness,
        metrics={name: float(item) for name, item in metrics.items()},
        observed_audit_snapshot_digest=value["observed_audit_snapshot_digest"],
        created_at=value["created_at"],
    )
    if request.request_digest != value["request_digest"]:
        raise EvaluatorExchangeError("evaluator request is not canonical or digest-bound")
    return request


def _candidate_value(candidate: CandidateBundle) -> dict[str, object]:
    return {
        "manifest": candidate.manifest_value(include_display_name=True),
        "artifact_payloads": {
            name: base64.b64encode(payload).decode("ascii")
            for name, payload in candidate.artifact_payloads.items()
        },
    }


def _candidate_from_value(value: Any) -> CandidateBundle:
    try:
        manifest = value["manifest"]
        encoded = value["artifact_payloads"]
        if set(value) != {"manifest", "artifact_payloads"} or not isinstance(manifest, dict):
            raise ValueError
        payloads = {
            name: base64.b64decode(encoded[name], validate=True) for name in sorted(encoded)
        }
        roles = tuple(
            RoleSnapshot(FactoryRole(item["role"]), item["generation_id"], item["digest"])
            for item in manifest["role_snapshots"]
        )
        candidate = CandidateBuilder(
            allowed_local_models=tuple(manifest["local_model_ids"]),
            allowed_cloud_models=tuple(manifest["cloud_model_ids"]),
        ).build(
            display_name=manifest["display_name"],
            code_revision=manifest["code_revision"],
            code_digest=manifest["code_digest"],
            dependency_lock_digest=manifest["dependency_lock_digest"],
            data_snapshot_digest=manifest["data_snapshot_digest"],
            role_snapshots=roles,
            component_digests=manifest["component_digests"],
            artifact_payloads=payloads,
            local_model_ids=tuple(manifest["local_model_ids"]),
            cloud_model_ids=tuple(manifest["cloud_model_ids"]),
            compatibility_contract_digest=manifest["compatibility_contract_digest"],
            rollback_parent_digest=manifest["rollback_parent_digest"],
        )
        if candidate.candidate_digest != manifest.get(
            "candidate_digest", candidate.candidate_digest
        ):
            raise ValueError
        return candidate
    except Exception as exc:
        raise EvaluatorExchangeError("evaluator candidate is malformed") from exc


def _harness_from_value(value: Any) -> FrozenEvaluationHarness:
    try:
        expected = {
            "schema_version",
            "generation_id",
            "audit_snapshot_digest",
            "harness_code_digest",
            "precommit_digest",
            "gates",
            "frozen_at",
        }
        if not isinstance(value, dict) or set(value) not in (
            expected,
            expected | {"selection_metric"},
        ):
            raise ValueError
        return FrozenEvaluationHarness.create(
            generation_id=value["generation_id"],
            audit_snapshot_digest=value["audit_snapshot_digest"],
            harness_code_digest=value["harness_code_digest"],
            precommit_digest=value["precommit_digest"],
            gates=tuple(
                EvaluationGate(item["name"], item["comparator"], float(item["threshold"]))
                for item in value["gates"]
            ),
            frozen_at=value["frozen_at"],
            selection_metric=value.get("selection_metric"),
        )
    except Exception as exc:
        raise EvaluatorExchangeError("evaluator harness is malformed") from exc


def _read_json(path: Path, limit: int) -> dict[str, Any]:
    try:
        raw = _read_bounded_bytes(path, limit)
        value = json.loads(raw)
    except FileNotFoundError:
        raise
    except EvaluatorExchangeError:
        raise
    except Exception as exc:
        raise EvaluatorExchangeError("evaluator exchange is unreadable") from exc
    if not isinstance(value, dict):
        raise EvaluatorExchangeError("evaluator exchange must be an object")
    if canonical_bytes(value) != raw:
        raise EvaluatorExchangeError("evaluator exchange is not canonical")
    return value


def _read_bounded_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size <= 0 or size > limit:
            raise EvaluatorExchangeError("evaluator exchange exceeds its bounded size")
        raw = handle.read(limit + 1)
    if not raw or len(raw) > limit:
        raise EvaluatorExchangeError("evaluator exchange exceeds its bounded size")
    return raw


def _publish(path: Path, payload: bytes, limit: int) -> None:
    if not payload or len(payload) > limit:
        raise EvaluatorExchangeError("evaluator exchange exceeds its bounded size")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = _read_bounded_bytes(path, limit)
    except FileNotFoundError:
        pass
    else:
        if existing != payload:
            raise EvaluatorExchangeError("evaluator exchange conflicts with existing output")
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _windows_publish_no_clobber(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if _read_bounded_bytes(path, limit) != payload:
                    raise EvaluatorExchangeError(
                        "evaluator exchange conflicts with existing output"
                    )
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _windows_publish_no_clobber(source: Path, destination: Path) -> None:
    import ctypes
    from ctypes import wintypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    if move(str(source), str(destination), 0x8):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise EvaluatorExchangeError("evaluator exchange output already exists")
    raise OSError(error, "atomic evaluator exchange publication failed", str(destination))


__all__ = [
    "EvaluatorExchangeError",
    "EvaluatorProcessRequest",
    "EvaluatorProcessResponse",
    "read_evaluator_response",
    "run_evaluator_request",
    "write_evaluator_request",
]
