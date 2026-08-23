from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import pytest

from strathmark.v3.assessors.base import (
    EvidenceOrigin,
    FormulaInputPacket,
)
from strathmark.v3.assessors.formula import FormulaManifest, assess_formula
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from tests.v3.unit.test_formula import context, evidence, formula_input, observation


@dataclass(frozen=True)
class WorkbookCell:
    value: object
    formula: str | None


def golden_input() -> FormulaInputPacket:
    packet = evidence(
        observation(1, 38200, occurred_at_utc="2026-08-29T12:00:00.000Z"),
        observation(
            2,
            40100,
            observed_context=context(size=275),
            occurred_at_utc="2025-08-29T12:00:00.000Z",
            tournament="authority-a",
        ),
        observation(
            3,
            36500,
            observed_context=context("standing_block", 300),
            occurred_at_utc="2024-08-29T12:00:00.000Z",
            tournament="legacy-a",
        ),
    )
    return formula_input(
        packet,
        origins=(
            EvidenceOrigin.ISSUED_RESULT_RECEIPT,
            EvidenceOrigin.VERIFIED_HISTORICAL_IMPORT,
            EvidenceOrigin.ISSUED_RESULT_RECEIPT,
        ),
        authoritative_tournaments=("authority-a",),
        legacy_tournaments=("legacy-a",),
    )


def test_formula_golden_replay_is_byte_stable_causal_and_trace_locked() -> None:
    manifest = FormulaManifest.load("benchmarks/v3/formula_manifest.json")
    packet = golden_input()
    first = assess_formula(packet, manifest)
    second = assess_formula(packet, manifest)
    golden = json.loads(Path("benchmarks/v3/formula_golden.json").read_text(encoding="utf-8"))
    assert canonical_bytes(first.to_dict()) == canonical_bytes(second.to_dict())
    assert first.forecast.evidence_digest == packet.digest == golden["input_digest"]
    assert first.manifest_digest == golden["manifest_digest"]
    assert first.assessment_digest == golden["assessment_digest"]
    assert first.center_ms == golden["center_ms"]
    assert first.uncertainty_ms == golden["uncertainty_ms"]
    assert first.log_center == golden["log_center"]
    assert first.log_scale == golden["log_scale"]
    assert first.effective_sample_size == golden["effective_sample_size"]
    assert first.personal_weight == golden["personal_weight"]
    assert first.forecast.distribution.to_dict() == golden["distribution"]  # type: ignore[union-attr]
    assert canonical_digest([row.to_dict() for row in first.trace]) == golden["trace_digest"]
    assert all(
        int(row.details.get("observation_sequence", "0"))
        <= packet.evidence.tournament_event_sequence
        for row in first.trace
    )
    sealed = next(row for row in first.trace if row.stage == "canonical_bytes")
    assert bytes.fromhex(sealed.details["canonical_hex"]) == canonical_bytes(packet.to_dict())
    assert bytes.fromhex(sealed.details["manifest_canonical_hex"]) == canonical_bytes(
        manifest.to_dict()
    )


def test_workbook_recalculated_values_match_python_to_one_millisecond() -> None:
    workbook = Path("benchmarks/v3/formula_golden.xlsx")
    golden = json.loads(Path("benchmarks/v3/formula_golden.json").read_text(encoding="utf-8"))
    distribution = _xlsx_cells(workbook, "Distribution")
    transforms = _xlsx_cells(workbook, "Transforms")
    governor = _xlsx_cells(workbook, "Governor Projection")
    inputs = _xlsx_cells(workbook, "Inputs")
    irls = _xlsx_cells(workbook, "IRLS Trace")
    assert distribution["B12"].value == golden["center_ms"]
    assert distribution["B13"].value == golden["uncertainty_ms"]
    assert float(distribution["B4"].value) == pytest.approx(float(golden["log_center"]), abs=1e-12)
    assert float(distribution["B11"].value) == pytest.approx(float(golden["log_scale"]), abs=1e-12)
    assert [distribution[f"G{row}"].value for row in range(4, 9)] == [
        point["time_ms"] for point in golden["distribution"]["quantiles"]
    ]
    python_rows = [
        row
        for row in assess_formula(
            golden_input(), FormulaManifest.load("benchmarks/v3/formula_manifest.json")
        ).trace
        if row.stage == "observation"
    ]
    assert [round(float(transforms[f"G{row}"].value), 12) for row in range(4, 7)] == [
        round(float(row.value), 12) for row in python_rows
    ]
    assert [governor[f"C{row}"].value for row in range(5, 8)] == [0, 365, 730]
    assert [governor[f"D{row}"].value for row in range(5, 8)] == [
        "issued_official",
        "verified_historical",
        "issued_official",
    ]
    packet = golden_input()
    assert inputs["H4"].value == packet.governor_receipt.signer_key_id
    assert inputs["H5"].value == packet.governor_receipt.signed_manifest_body_digest
    assert inputs["H6"].value == packet.evidence.content_digest
    assert inputs["H7"].value == packet.governor_receipt.tournament_epoch_content_digest
    assert all(governor[f"O{row}"].value is True for row in range(5, 8))
    assert all(governor[f"P{row}"].value == packet.evidence.content_digest for row in range(5, 8))
    active = [int(irls[f"N{row}"].value) for row in range(24, 44) if int(irls[f"N{row}"].value) > 0]
    assert active == list(range(1, 15))


def test_workbook_contains_real_dependency_graph_not_copied_expected_outputs() -> None:
    workbook = Path("benchmarks/v3/formula_golden.xlsx")
    assumptions = _xlsx_cells(workbook, "Assumptions")
    governor = _xlsx_cells(workbook, "Governor Projection")
    transforms = _xlsx_cells(workbook, "Transforms")
    irls = _xlsx_cells(workbook, "IRLS Trace")
    distribution = _xlsx_cells(workbook, "Distribution")
    assert assumptions["I14"].formula == "'Inputs'!$B$4&\"|\"&'Inputs'!$B$5&\"|\"&'Inputs'!$B$6"
    assert "ROW($H$10)" in (assumptions["I16"].formula or "")
    assert assumptions["I18"].formula == "INDEX($N$5:$N$10,I16-4)"
    assert governor["C5"].formula == "'Inputs'!$E$4-'Inputs'!G13"
    assert "live_issued_race" in (governor["D5"].formula or "")
    assert "REVIEW_BLOCKED" in (governor["E5"].formula or "")
    assert "eligible_completion" in (governor["I5"].formula or "")
    assert governor["J5"].formula == "'Inputs'!O13"
    assert "'Inputs'!$H$8" in (governor["O5"].formula or "")
    assert governor["P5"].formula == "'Inputs'!$H$6"
    assert governor["Q5"].formula == "'Inputs'!$H$5"
    assert transforms["A4"].formula == "'Inputs'!A13"
    assert transforms["G4"].formula == "LN(F4)"
    assert transforms["O4"].formula == "I4*J4*K4*L4*M4/(1+N4)"
    assert "'Governor Projection'!C5" in (transforms["K4"].formula or "")
    assert "MINIFS($S$4:$S$9,$U$4:$U$9" in (irls["T12"].formula or "")
    assert "MINIFS($S$15:$S$20,$U$15:$U$20" in (irls["T13"].formula or "")
    assert irls["B25"].formula == "K24"
    assert "SUMIF($B$4:$B$9" in (irls["T4"].formula or "")
    assert all(
        irls[f"K{row}"].formula and "$B$4" in irls[f"K{row}"].formula for row in range(24, 44)
    )
    assert "'IRLS Trace'!$K$24:$K$43" in (distribution["B4"].formula or "")
    assert "SQRT((B7+B8+B9)*B10)" in (distribution["B11"].formula or "")
    assert distribution["B12"].formula == "ROUND(EXP(B4)*1000,0)"
    assert "INDEX('Assumptions'!$O$5:$O$10" in (distribution["B9"].formula or "")
    assert "'Assumptions'!$E$20" in (distribution["G4"].formula or "")
    assert distribution["B13"].formula == "(G8-G4)/2"
    assert distribution["D4"].formula == "'Assumptions'!D27"
    assert "'Inputs'!B13" in (distribution["E12"].formula or "")
    formulas = [cell.formula for cell in distribution.values() if cell.formula]
    assert (
        str(json.loads(Path("benchmarks/v3/formula_golden.json").read_text())["center_ms"])
        not in formulas
    )


def test_engine_verification_receipt_binds_workbook_formulas_and_mutation() -> None:
    workbook = Path("benchmarks/v3/formula_golden.xlsx")
    builder = Path("tools/benchmarks/build_formula_golden.mjs")
    receipt_path = Path("benchmarks/v3/formula_engine_verification.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    content = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    assert receipt["receipt_digest"] == _json_digest(content)
    assert receipt["workbook_sha256"] == hashlib.sha256(workbook.read_bytes()).hexdigest()
    assert receipt["builder_sha256"] == hashlib.sha256(builder.read_bytes()).hexdigest()
    assert receipt["formula_graph_sha256"] == _formula_graph_digest(workbook)
    assert receipt["formula_graph_sha256"] != _json_digest([])
    assert receipt["governor_binding_sha256"] == _json_digest(
        {
            "evidence_packet_digest": "18e7c2b3e7ed1e945d9b89edfd591a7cfa8efcb19d69af4f82a064459af97735",
            "epoch_content_digest": "9a37db8ae45d0118017df8ea5cc087f85b528d7d2b4983588fac7e8671b7c849",
            "signed_manifest_body_digest": "152e1ad207952255eacc67b8278507829d2cdad23fe04a0fbc2f7a885f7fccb3",
            "signer_key_id": "integrity-key:formula-test",
        }
    )
    assert (
        receipt["manifest_digest"]
        == FormulaManifest.load("benchmarks/v3/formula_manifest.json").digest
    )
    golden = json.loads(Path("benchmarks/v3/formula_golden.json").read_text(encoding="utf-8"))
    assert receipt["baseline_outputs"]["center_ms"] == golden["center_ms"]
    assert receipt["baseline_outputs"]["uncertainty_ms"] == golden["uncertainty_ms"]
    assert receipt["mutation_outputs"]["center_ms"] != golden["center_ms"]
    assert receipt["restored_outputs"] == receipt["baseline_outputs"]
    assert receipt["formula_error_count"] == 0
    assert set(receipt["render_sha256"]) == {
        "Assumptions",
        "Inputs",
        "Governor Projection",
        "Transforms",
        "IRLS Trace",
        "Distribution",
    }


def test_designated_verification_runs_independent_artifact_engine(tmp_path: Path) -> None:
    required = os.environ.get("STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION", "1") != "0"
    node, node_modules = _artifact_engine_paths()
    if not node.is_file() or not node_modules.is_dir():
        if required:
            pytest.fail("independent Formula workbook engine is required but unavailable")
        pytest.skip("independent Formula workbook engine is unavailable and gate is disabled")
    junction = tmp_path / "node_modules"
    if os.name == "nt":
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(node_modules)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert linked.returncode == 0, linked.stderr or linked.stdout
    else:
        junction.symlink_to(node_modules, target_is_directory=True)
    environment = os.environ.copy()
    environment.update(
        {
            "STRATHMARK_ARTIFACT_NODE_MODULES": str(node_modules),
            "STRATHMARK_ARTIFACT_WORKDIR": str(tmp_path),
            "STRATHMARK_REPO_ROOT": str(Path.cwd()),
        }
    )
    verified = subprocess.run(
        [str(node), "tools/benchmarks/build_formula_golden.mjs", "--verify"],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout
    assert '"status": "verified"' in verified.stdout


def test_required_engine_gate_does_not_silently_accept_missing_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRATHMARK_ARTIFACT_NODE", "Z:/missing/node.exe")
    monkeypatch.setenv("STRATHMARK_ARTIFACT_NODE_MODULES", "Z:/missing/node_modules")
    node, node_modules = _artifact_engine_paths()
    assert not node.is_file()
    assert not node_modules.is_dir()


def _artifact_engine_paths() -> tuple[Path, Path]:
    runtime = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
    )
    return (
        Path(
            os.environ.get(
                "STRATHMARK_ARTIFACT_NODE",
                runtime / "bin" / ("node.exe" if os.name == "nt" else "node"),
            )
        ),
        Path(os.environ.get("STRATHMARK_ARTIFACT_NODE_MODULES", runtime / "node_modules")),
    )


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _formula_graph_digest(path: Path) -> str:
    graph: list[list[str]] = []
    cell_pattern = re.compile(r'<(?:x:)?c\b[^>]*\br="([^"]+)"[^>]*>[\s\S]*?</(?:x:)?c>')
    formula_pattern = re.compile(r"<(?:x:)?f[^>]*>([\s\S]*?)</(?:x:)?f>")
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        for name in names:
            xml = archive.read(name).decode("utf-8")
            for cell in cell_pattern.finditer(xml):
                formula = formula_pattern.search(cell.group(0))
                if formula:
                    graph.append([name, cell.group(1), formula.group(1)])
    return _json_digest(graph)


def _xlsx_cells(path: Path, sheet_name: str) -> dict[str, WorkbookCell]:
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    with zipfile.ZipFile(path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships.findall(f"{{{package_rel}}}Relationship")
        }
        sheet = next(
            item
            for item in workbook.findall(f".//{{{main}}}sheet")
            if item.attrib["name"] == sheet_name
        )
        target = targets[sheet.attrib[f"{{{office_rel}}}id"]]
        member = (
            target.lstrip("/")
            if target.lstrip("/").startswith("xl/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        root = ElementTree.fromstring(archive.read(member))
    cells: dict[str, WorkbookCell] = {}
    for cell in root.findall(f".//{{{main}}}c"):
        address = cell.attrib["r"]
        formula_node = cell.find(f"{{{main}}}f")
        inline = cell.find(f"{{{main}}}is/{{{main}}}t")
        raw = cell.find(f"{{{main}}}v")
        if inline is not None:
            value: object = inline.text or ""
        elif raw is None:
            value = ""
        elif cell.attrib.get("t") == "b":
            value = raw.text == "1"
        elif cell.attrib.get("t") == "str":
            value = raw.text or ""
        else:
            number = float(raw.text or "0")
            value = int(number) if number.is_integer() else number
        cells[address] = WorkbookCell(
            value, None if formula_node is None else formula_node.text or ""
        )
    return cells
