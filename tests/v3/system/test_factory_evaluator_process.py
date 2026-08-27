from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from strathmark.v3.contracts.canonical import canonical_bytes
from strathmark.v3.factory import evaluator_process
from strathmark.v3.factory.evaluator import (
    AuditGenerationRegistry,
    FrozenEvaluator,
    verify_evaluation_report,
)
from strathmark.v3.factory.evaluator_process import (
    MAX_EVALUATOR_REQUEST_BYTES,
    EvaluatorExchangeError,
    EvaluatorProcessRequest,
    EvaluatorProcessResponse,
    read_evaluator_response,
    run_evaluator_request,
    write_evaluator_request,
)
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from tests.v3.evals.test_factory_audit_isolation import DIGESTS, _candidate, _harness


class _CountingSigner:
    def __init__(self, key_id: str) -> None:
        self._inner = P256EphemeralSigner.generate(key_id)
        self.sign_calls = 0

    @property
    def identity(self):
        return self._inner.identity

    def sign(self, payload: bytes) -> bytes:
        self.sign_calls += 1
        return self._inner.sign(payload)


def test_separate_evaluator_exchange_is_canonical_bounded_signed_and_exact_retry(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    harness = _harness()
    signer = P256EphemeralSigner.generate("integrity-key:process-evaluator")
    request = EvaluatorProcessRequest.create(
        candidate=candidate,
        harness=harness,
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:10:00.000Z",
    )
    request_path = tmp_path / "evaluator-inbox" / "request.json"
    response_path = tmp_path / "evaluator-outbox" / "response.json"
    write_evaluator_request(request_path, request)

    first = run_evaluator_request(
        request_path,
        response_path,
        registry_path=tmp_path / "sealed-audit-registry",
        signer=signer,
    )
    retry = run_evaluator_request(
        request_path,
        response_path,
        registry_path=tmp_path / "sealed-audit-registry",
        signer=signer,
    )

    assert retry == first == read_evaluator_response(response_path)
    assert (
        verify_evaluation_report(
            first.report,
            trust_store=IntegrityTrustStore((signer.identity,)),
            expected_candidate=candidate,
            expected_harness=harness,
        )
        == first.report
    )
    assert first.report.promotion_authorized is False
    assert tuple((tmp_path / "evaluator-outbox").iterdir()) == (response_path,)


def test_evaluator_exchange_rejects_noncanonical_oversized_or_conflicting_material(
    tmp_path: Path,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:process-evaluator-reject")
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema_version": "wrong"}', encoding="utf-8")
    with pytest.raises(EvaluatorExchangeError):
        run_evaluator_request(
            malformed,
            tmp_path / "response.json",
            registry_path=tmp_path / "registry",
            signer=signer,
        )

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (2 * 1024 * 1024) + b"}")
    with pytest.raises(EvaluatorExchangeError, match="bounded"):
        run_evaluator_request(
            oversized,
            tmp_path / "oversized-response.json",
            registry_path=tmp_path / "oversized-registry",
            signer=signer,
        )


def test_evaluator_exchange_executes_in_a_separate_python_process(tmp_path: Path) -> None:
    request_path = tmp_path / "process-request.json"
    response_path = tmp_path / "process-response.json"
    write_evaluator_request(
        request_path,
        EvaluatorProcessRequest.create(
            candidate=_candidate(),
            harness=_harness(),
            metrics={"coverage": 0.94, "normalized_crps": 0.21},
            observed_audit_snapshot_digest=DIGESTS[22],
            created_at="2026-08-25T08:11:00.000Z",
        ),
    )
    code = (
        "from strathmark.v3.factory.evaluator_process import run_evaluator_request;"
        "from strathmark.v3.infrastructure.integrity import P256EphemeralSigner;"
        "import sys;"
        "run_evaluator_request(sys.argv[1],sys.argv[2],registry_path=sys.argv[3],"
        "signer=P256EphemeralSigner.generate('integrity-key:child-evaluator'))"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(request_path),
            str(response_path),
            str(tmp_path / "child-registry"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert read_evaluator_response(response_path).request_digest != "0" * 64


@pytest.mark.parametrize("hostile_kind", ("untrusted", "wrong_candidate", "wrong_harness"))
def test_exact_retry_rejects_untrusted_or_misbound_existing_response(
    tmp_path: Path,
    hostile_kind: str,
) -> None:
    candidate = _candidate()
    harness = _harness()
    trusted_signer = P256EphemeralSigner.generate("integrity-key:trusted-process-evaluator")
    request = EvaluatorProcessRequest.create(
        candidate=candidate,
        harness=harness,
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:12:00.000Z",
    )
    request_path = tmp_path / "hostile-request.json"
    response_path = tmp_path / "hostile-response.json"
    write_evaluator_request(request_path, request)

    report_candidate = candidate
    report_harness = harness
    report_signer = trusted_signer
    if hostile_kind == "untrusted":
        report_signer = P256EphemeralSigner.generate("integrity-key:untrusted-evaluator")
    elif hostile_kind == "wrong_candidate":
        report_candidate = _candidate(name="wrong-candidate")
    else:
        report_harness = replace(harness, harness_code_digest=DIGESTS[27])
    report = FrozenEvaluator(
        report_harness,
        AuditGenerationRegistry(tmp_path / f"hostile-registry-{hostile_kind}"),
        signer=report_signer,
    ).evaluate(
        report_candidate,
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=report_harness.audit_snapshot_digest,
        created_at="2026-08-25T08:12:00.000Z",
    )
    response_path.write_bytes(
        canonical_bytes(EvaluatorProcessResponse(request.request_digest, report).to_dict())
    )

    with pytest.raises(EvaluatorExchangeError, match="existing evaluator response"):
        run_evaluator_request(
            request_path,
            response_path,
            registry_path=tmp_path / "trusted-registry",
            signer=trusted_signer,
        )


def test_request_is_parsed_from_one_canonical_file_snapshot(tmp_path: Path, monkeypatch) -> None:
    request_path = tmp_path / "single-read-request.json"
    response_path = tmp_path / "single-read-response.json"
    write_evaluator_request(
        request_path,
        EvaluatorProcessRequest.create(
            candidate=_candidate(),
            harness=_harness(),
            metrics={"coverage": 0.94, "normalized_crps": 0.21},
            observed_audit_snapshot_digest=DIGESTS[22],
            created_at="2026-08-25T08:13:00.000Z",
        ),
    )
    original_stat = Path.stat
    original_read_bytes = Path.read_bytes

    def no_request_path_stat(path: Path, *args, **kwargs):
        if path == request_path:
            raise AssertionError("request size must come from the opened descriptor")
        return original_stat(path, *args, **kwargs)

    def no_request_path_read(path: Path) -> bytes:
        if path == request_path:
            raise AssertionError("request must be read from one bounded descriptor")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "stat", no_request_path_stat)
    monkeypatch.setattr(Path, "read_bytes", no_request_path_read)
    run_evaluator_request(
        request_path,
        response_path,
        registry_path=tmp_path / "single-read-registry",
        signer=P256EphemeralSigner.generate("integrity-key:single-read-evaluator"),
    )


def test_response_publication_crash_and_deletion_recover_exact_signed_result(
    tmp_path: Path, monkeypatch
) -> None:
    candidate = _candidate()
    harness = _harness()
    signer = _CountingSigner("integrity-key:crash-recovery-evaluator")
    request = EvaluatorProcessRequest.create(
        candidate=candidate,
        harness=harness,
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:14:00.000Z",
    )
    request_path = tmp_path / "crash-request.json"
    response_path = tmp_path / "crash-response.json"
    registry_path = tmp_path / "crash-registry"
    write_evaluator_request(request_path, request)

    original_publish = evaluator_process._publish
    fail_response_publish = True

    def faulted_publish(path: Path, payload: bytes, limit: int) -> None:
        if path == response_path and fail_response_publish:
            raise OSError("injected crash after durable audit result")
        original_publish(path, payload, limit)

    monkeypatch.setattr(evaluator_process, "_publish", faulted_publish)
    with pytest.raises(OSError, match="injected crash"):
        run_evaluator_request(
            request_path,
            response_path,
            registry_path=registry_path,
            signer=signer,
        )
    assert signer.sign_calls == 2
    assert not response_path.exists()

    fail_response_publish = False
    recovered = run_evaluator_request(
        request_path,
        response_path,
        registry_path=registry_path,
        signer=signer,
    )
    assert signer.sign_calls == 2
    assert (
        verify_evaluation_report(
            recovered.report,
            trust_store=IntegrityTrustStore((signer.identity,)),
            expected_candidate=candidate,
            expected_harness=harness,
        )
        == recovered.report
    )

    response_path.unlink()
    recovered_after_deletion = run_evaluator_request(
        request_path,
        response_path,
        registry_path=registry_path,
        signer=signer,
    )
    assert recovered_after_deletion == recovered
    assert signer.sign_calls == 2

    response_path.unlink()
    changed_request_path = tmp_path / "changed-request.json"
    write_evaluator_request(
        changed_request_path,
        EvaluatorProcessRequest.create(
            candidate=candidate,
            harness=harness,
            metrics={"coverage": 0.93, "normalized_crps": 0.22},
            observed_audit_snapshot_digest=DIGESTS[22],
            created_at=request.created_at,
        ),
    )
    with pytest.raises(EvaluatorExchangeError, match="durable evaluator result"):
        run_evaluator_request(
            changed_request_path,
            response_path,
            registry_path=registry_path,
            signer=signer,
        )
    assert signer.sign_calls == 2
    assert not response_path.exists()


@pytest.mark.parametrize("hostile_kind", ("untrusted", "wrong_candidate", "wrong_harness"))
def test_recovery_rejects_untrusted_or_misbound_durable_result(
    tmp_path: Path,
    hostile_kind: str,
) -> None:
    candidate = _candidate()
    harness = _harness()
    trusted_signer = P256EphemeralSigner.generate("integrity-key:trusted-recovery-evaluator")
    request = EvaluatorProcessRequest.create(
        candidate=candidate,
        harness=harness,
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:15:00.000Z",
    )
    request_path = tmp_path / f"recovery-{hostile_kind}-request.json"
    response_path = tmp_path / f"recovery-{hostile_kind}-response.json"
    registry_path = tmp_path / f"recovery-{hostile_kind}-registry"
    write_evaluator_request(request_path, request)

    report_candidate = candidate
    report_harness = harness
    report_signer = trusted_signer
    if hostile_kind == "untrusted":
        report_signer = P256EphemeralSigner.generate("integrity-key:untrusted-recovery")
    elif hostile_kind == "wrong_candidate":
        report_candidate = _candidate(name="wrong-recovery-candidate")
    else:
        report_harness = replace(harness, harness_code_digest=DIGESTS[28])
    FrozenEvaluator(
        report_harness,
        AuditGenerationRegistry(registry_path),
        signer=report_signer,
    ).evaluate(
        report_candidate,
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=report_harness.audit_snapshot_digest,
        created_at=request.created_at,
        request_digest=request.request_digest,
    )

    with pytest.raises(EvaluatorExchangeError, match="durable evaluator result"):
        run_evaluator_request(
            request_path,
            response_path,
            registry_path=registry_path,
            signer=trusted_signer,
        )
    assert not response_path.exists()


def test_existing_oversized_output_is_read_with_a_bounded_descriptor(
    tmp_path: Path, monkeypatch
) -> None:
    request = EvaluatorProcessRequest.create(
        candidate=_candidate(),
        harness=_harness(),
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:16:00.000Z",
    )
    output = tmp_path / "oversized-existing-request.json"
    output.write_bytes(b"x" * (MAX_EVALUATOR_REQUEST_BYTES + 2))
    original_open = Path.open
    original_fstat = evaluator_process.os.fstat
    read_sizes: list[int] = []

    class _BoundedRead:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                raise AssertionError("existing output was read without a byte bound")
            read_sizes.append(size)
            return self._handle.read(size)

    def monitored_open(path: Path, mode: str = "r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return _BoundedRead(handle) if path == output and mode == "rb" else handle

    def underestimated_fstat(descriptor: int):
        result = original_fstat(descriptor)
        if result.st_size == MAX_EVALUATOR_REQUEST_BYTES + 2:
            return SimpleNamespace(st_size=MAX_EVALUATOR_REQUEST_BYTES)
        return result

    monkeypatch.setattr(Path, "open", monitored_open)
    monkeypatch.setattr(evaluator_process.os, "fstat", underestimated_fstat)

    with pytest.raises(EvaluatorExchangeError, match="bounded"):
        write_evaluator_request(output, request)
    assert read_sizes == [MAX_EVALUATOR_REQUEST_BYTES + 1]


def test_opened_request_snapshot_survives_path_replacement(tmp_path: Path, monkeypatch) -> None:
    original = EvaluatorProcessRequest.create(
        candidate=_candidate(),
        harness=_harness(),
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:17:00.000Z",
    )
    replacement = EvaluatorProcessRequest.create(
        candidate=_candidate(name="replacement-candidate"),
        harness=_harness(),
        metrics={"coverage": 0.91, "normalized_crps": 0.24},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:18:00.000Z",
    )
    request_path = tmp_path / "replaceable-request.json"
    snapshot_path = tmp_path / "opened-snapshot.json"
    replacement_path = tmp_path / "replacement.json"
    response_path = tmp_path / "replacement-response.json"
    write_evaluator_request(request_path, original)
    write_evaluator_request(snapshot_path, original)
    write_evaluator_request(replacement_path, replacement)
    original_open = Path.open
    original_stat = Path.stat
    replaced = False
    request_path_stats = 0

    def tracked_stat(path: Path, *args, **kwargs):
        nonlocal request_path_stats
        if path == request_path:
            request_path_stats += 1
        return original_stat(path, *args, **kwargs)

    def replacing_open(path: Path, mode: str = "r", *args, **kwargs):
        nonlocal replaced
        if path == request_path and mode == "rb" and not replaced:
            handle = original_open(snapshot_path, mode, *args, **kwargs)
            replacement_path.replace(request_path)
            replaced = True
            return handle
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", tracked_stat)
    monkeypatch.setattr(Path, "open", replacing_open)
    response = run_evaluator_request(
        request_path,
        response_path,
        registry_path=tmp_path / "replacement-registry",
        signer=P256EphemeralSigner.generate("integrity-key:replacement-evaluator"),
    )

    assert replaced is True
    assert request_path_stats == 0
    assert response.request_digest == original.request_digest
