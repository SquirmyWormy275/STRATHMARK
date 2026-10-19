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
from types import SimpleNamespace
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO regression")
def test_fifo_and_symlink_are_rejected_before_opening(tmp_path, monkeypatch):
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    regular = tmp_path / "regular"
    regular.write_bytes(b"input")
    alias = tmp_path / "alias"
    alias.symlink_to(regular)
    monkeypatch.setattr(
        os, "open", lambda *_args, **_kwargs: pytest.fail("unsafe input was opened")
    )
    for path in (fifo, alias):
        with pytest.raises(ValueError, match="regular-file"):
            read_bounded_file(path)


@pytest.mark.parametrize("entry", ["legacy", "linux"])
def test_administrative_workbooks_use_archive_bounds_before_parser(tmp_path, monkeypatch, entry):
    import openpyxl

    import import_legacy
    from strathmark.v3.linux_lifecycle import LinuxCompetitionRuntime

    workbook = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(workbook, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"x" * 1_000_000)
    monkeypatch.setattr(
        import_legacy.pd, "ExcelFile", lambda *_a, **_kw: pytest.fail("unbounded parser")
    )
    monkeypatch.setattr(
        openpyxl, "load_workbook", lambda *_a, **_kw: pytest.fail("unbounded parser")
    )
    with pytest.raises(ValueError, match="compression"):
        if entry == "legacy":
            import_legacy.load_and_validate_workbook(str(workbook))
        else:
            runtime = object.__new__(LinuxCompetitionRuntime)
            runtime.store = SimpleNamespace(root=tmp_path / "authority")
            runtime._history_snapshot(
                {"scope_id": "tournament:synthetic"},
                {"workbook": str(workbook), "cutoff_at_utc": "2026-10-19T00:00:00Z"},
            )
    assert not list((tmp_path / "authority").rglob("history.xlsx"))


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


@pytest.mark.parametrize("attribute", ["cell", "dimension", "row"])
def test_long_coordinate_is_rejected_before_numeric_work(tmp_path, monkeypatch, attribute):
    value = "Z" * 100_000 + "1"
    element = {
        "cell": f'<c r="{value}"/>',
        "dimension": f'<dimension ref="A1:{value}"/>',
        "row": f'<row r="{"9" * 100_000}"/>',
    }[attribute]
    path = tmp_path / "long.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", f"<worksheet>{element}</worksheet>")
    import strathmark.input_limits as limits

    monkeypatch.setattr(
        limits,
        "ord",
        lambda *_a: pytest.fail("unbounded coordinate arithmetic began"),
        raising=False,
    )
    with pytest.raises(ValueError, match=f"{attribute}.*budget"):
        bounded_workbook(path)


@pytest.mark.parametrize("coordinate", ["IV100000", "a1"])
def test_valid_boundary_coordinates_remain_readable(tmp_path, coordinate):
    path = tmp_path / "boundary.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet><dimension ref="A1:{coordinate}"/><c r="{coordinate}"/></worksheet>',
        )
    assert bounded_workbook(path).read() == path.read_bytes()


@pytest.mark.parametrize("forged_count", [False, True])
def test_member_count_checked_before_zipinfo_allocation(tmp_path, monkeypatch, forged_count):
    import struct

    path = tmp_path / "many.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1025):
            archive.writestr(f"entry{index}", b"")
    raw = bytearray(path.read_bytes())
    if forged_count:
        struct.pack_into("<HH", raw, len(raw) - 22 + 8, 1, 1)
        path.write_bytes(raw)
    monkeypatch.setattr(zipfile, "ZipInfo", lambda *_a, **_k: pytest.fail("allocated ZipInfo"))
    with pytest.raises(ValueError, match="member|directory"):
        bounded_workbook(path)


def test_directory_byte_limit_runs_before_zipinfo_allocation(tmp_path, monkeypatch):
    path = tmp_path / "metadata.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(18):
            entry = zipfile.ZipInfo(str(index))
            entry.comment = b"x" * 65535
            archive.writestr(entry, b"")
    monkeypatch.setattr(zipfile, "ZipInfo", lambda *_a, **_k: pytest.fail("allocated ZipInfo"))
    with pytest.raises(ValueError, match="directory"):
        bounded_workbook(path)


def test_support_bundle_member_preflight_precedes_zip_allocation(monkeypatch):
    from strathmark.v3.application.operations import verify_support_bundle

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for index in range(1025):
            archive.writestr(f"entry{index}", b"")
    monkeypatch.setattr(zipfile, "ZipInfo", lambda *_a, **_k: pytest.fail("allocated ZipInfo"))
    with pytest.raises(ValueError, match="member|directory|bundle"):
        verify_support_bundle(stream.getvalue(), trust_store=None)
