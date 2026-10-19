"""Hostile-input regressions for the public and local parser boundaries."""

import asyncio
import gzip
import hashlib
import io
import os
import tarfile
import time
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from strathmark.input_limits import bounded_workbook, read_bounded_file, tar_stream_digests
from strathmark.private_storage import private_file


def test_sparse_oversized_file_is_rejected_before_read(tmp_path):
    path = tmp_path / "large.bin"
    with path.open("wb") as stream:
        stream.truncate(67_108_865)
    with pytest.raises(ValueError, match="byte budget"):
        read_bounded_file(path)


@pytest.mark.parametrize("ref", ["A1:XFD1048576", "A1:A100001", "A1:IW1"])
def test_workbook_dimension_bombs_are_rejected(tmp_path, ref):
    path = tmp_path / "bad.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml", f'<worksheet><dimension ref="{ref}"/></worksheet>'
        )
    with pytest.raises(ValueError, match="dimension"):
        bounded_workbook(path)


def test_high_ratio_workbook_is_rejected_before_xml_parsing(tmp_path):
    path = tmp_path / "compressed.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"x" * 1_000_000)
    with pytest.raises(ValueError, match="compression"):
        bounded_workbook(path)


def test_tar_rejects_oversized_member_without_reading_payload():
    item = tarfile.TarInfo("huge")
    item.size = 67_108_865
    encoded = gzip.compress(item.tobuf() + b"\0" * 1024)
    with pytest.raises(ValueError, match="member budget"):
        tar_stream_digests(io.BytesIO(encoded))


def test_tar_accepts_small_safe_archive():
    encoded = io.BytesIO()
    with tarfile.open(fileobj=encoded, mode="w:gz") as archive:
        item = tarfile.TarInfo("snapshot.json")
        item.size = 2
        archive.addfile(item, io.BytesIO(b"{}"))
    assert tar_stream_digests(io.BytesIO(encoded.getvalue())) == {
        "snapshot.json": hashlib.sha256(b"{}").hexdigest()
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_private_storage_does_not_depend_on_umask(tmp_path):
    old = os.umask(0o022)
    try:
        path = private_file(tmp_path / "authority" / "store.sqlite3")
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    sidecar = Path(f"{path}-wal")
    sidecar.write_bytes(b"existing-wal")
    sidecar.chmod(0o644)
    private_file(path)
    assert sidecar.stat().st_mode & 0o777 == 0o600
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="links"):
        private_file(alias)


def test_cwd_models_cannot_override_the_packaged_model(tmp_path, monkeypatch):
    from strathmark.predictor import FilePredictionProvider

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRATHMARK_PREDICTION_CORE_ARTIFACT", raising=False)
    monkeypatch.delenv("STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT", raising=False)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "prediction_v2_core.json").write_text("malicious")
    assert FilePredictionProvider().snapshot(date(2026, 10, 19)).source == "package"


def test_explicit_core_requires_an_independent_digest_pin(tmp_path, monkeypatch):
    import strathmark.predictor as module

    path = tmp_path / "core.json"
    path.write_bytes((Path(module.__file__).parent / "models/prediction_v2_core.json").read_bytes())
    monkeypatch.setenv("STRATHMARK_PREDICTION_CORE_ARTIFACT", str(path))
    monkeypatch.delenv("STRATHMARK_PREDICTION_CORE_SHA256", raising=False)
    assert module.FilePredictionProvider().snapshot(date(2026, 10, 19)).core is None
    monkeypatch.setenv(
        "STRATHMARK_PREDICTION_CORE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert module.FilePredictionProvider().snapshot(date(2026, 10, 19)).core is not None


def test_ollama_stream_is_closed_on_overflow_and_deadline(monkeypatch):
    from strathmark.llm import _bounded_ollama_json

    response = Mock(headers={"Content-Length": "1048577"})
    with pytest.raises(ValueError, match="byte budget"):
        _bounded_ollama_json(response, started_at=time.monotonic())
    response.close.assert_called_once()
    response.iter_content.assert_not_called()
    response = Mock(headers={}, iter_content=lambda **_: iter([b"{}"] * 10))
    with pytest.raises(TimeoutError, match="deadline"):
        _bounded_ollama_json(response, started_at=time.monotonic() - 21)
    response.close.assert_called_once()


@pytest.mark.parametrize("declared", [True, False])
def test_public_body_limit_runs_before_json_parse(declared):
    pytest.importorskip("fastapi")
    from strathmark.request_limits import BoundedRequestMiddleware

    called = []
    output = []

    async def downstream(*args):
        called.append(True)

    async def receive():
        return {"type": "http.request", "body": b"invalid-json" * 20}

    async def send(message):
        output.append(message)

    headers = [(b"content-length", b"240")] if declared else []
    asyncio.run(
        BoundedRequestMiddleware(downstream, max_bytes=100)(
            {"type": "http", "path": "/predict", "headers": headers}, receive, send
        )
    )
    assert output[0]["status"] == 413
    assert not called


def test_history_limits_reject_per_competitor_aggregate_and_extra_fields():
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from strathmark.api import CalculateRequest, CompetitorSchema

    row = dict(event_code="UH", time_seconds=30, species="Aspen", diameter_mm=300, quality=7)
    with pytest.raises(ValidationError):
        CompetitorSchema(name="A", history=[row] * 1001)
    with pytest.raises(ValidationError):
        CompetitorSchema(name="A", unknown="ignored before")
    payload = dict(
        competitors=[dict(name=str(i), history=[row] * 801) for i in range(5)],
        wood=dict(species="Aspen", diameter_mm=300, quality=7),
        event_code="UH",
    )
    with pytest.raises(ValidationError, match="4000"):
        CalculateRequest(**payload)
