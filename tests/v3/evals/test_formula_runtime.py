"""Portable mode cannot accidentally qualify a mismatched artifact runtime."""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts import verify_formula_runtime
from tests.v3.evals import test_formula_replay


def test_formula_runtime_rejects_newer_tool_before_rebuilding(tmp_path, monkeypatch):
    node = tmp_path / "node"
    node.touch()
    modules = tmp_path / "node_modules"
    package = modules / "@oai/artifact-tool/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": "2.8.59"}))
    monkeypatch.setattr(
        verify_formula_runtime.subprocess, "check_output", lambda *args, **kwargs: "v24.19.0\n"
    )
    manifest, digest = _approved_manifest(tmp_path, node, modules)
    with pytest.raises(ValueError, match="artifact_tool_version requires 2.8.52, observed 2.8.59"):
        verify_formula_runtime.verify_runtime(
            node, modules, manifest=manifest, expected_manifest_sha256=digest
        )


def test_portable_mode_skips_even_when_a_local_artifact_runtime_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION", "0")

    def must_not_probe():
        raise AssertionError("portable tests must not use an incidental machine-local tool")

    monkeypatch.setattr(test_formula_replay, "_artifact_engine_paths", must_not_probe)
    with pytest.raises(pytest.skip.Exception, match="disabled for this portable run"):
        test_formula_replay.test_designated_verification_runs_independent_artifact_engine(tmp_path)


def test_required_mode_is_default_and_fails_when_tools_are_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION", raising=False)
    monkeypatch.setattr(
        test_formula_replay,
        "_artifact_engine_paths",
        lambda: (tmp_path / "missing-node", tmp_path / "missing-modules"),
    )
    with pytest.raises(pytest.fail.Exception, match="required but unavailable"):
        test_formula_replay.test_designated_verification_runs_independent_artifact_engine(tmp_path)


def _approved_manifest(tmp_path, node, modules):
    # Test-only approval represents an independent known-good fixture, never a
    # runtime mechanism for blessing an installed designated tool.
    manifest = tmp_path / "approved.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": verify_formula_runtime.SCHEMA,
                "node_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
                "module_files": verify_formula_runtime._module_inventory(modules),
            }
        )
    )
    return manifest, hashlib.sha256(manifest.read_bytes()).hexdigest()


@pytest.fixture
def approved_tools(tmp_path):
    node = tmp_path / "node"
    node.write_bytes(b"original approved executable")
    modules = tmp_path / "node_modules"
    package = modules / "@oai/artifact-tool/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": "2.8.52"}))
    (package.parent / "index.js").write_text("export const verified = true;")
    manifest, digest = _approved_manifest(tmp_path, node, modules)
    return node, modules, manifest, digest


@pytest.mark.parametrize("target", ["node", "module", "extra", "manifest"])
def test_same_version_tool_substitution_is_rejected_before_execution(
    approved_tools, monkeypatch, target
):
    node, modules, manifest, digest = approved_tools
    if target == "node":
        node.write_bytes(b"shim reporting v24.19.0")
    elif target == "module":
        (modules / "@oai/artifact-tool/index.js").write_text("export const verified = false;")
    elif target == "extra":
        (modules / "injected.js").write_text("process.exit(0)")
    else:
        manifest.write_text(manifest.read_text() + " ")

    def must_not_execute(*args, **kwargs):
        raise AssertionError("unapproved bytes must be refused before executing Node")

    monkeypatch.setattr(verify_formula_runtime.subprocess, "check_output", must_not_execute)
    with pytest.raises(ValueError, match="differ.*approved"):
        verify_formula_runtime.verify_runtime(
            node, modules, manifest=manifest, expected_manifest_sha256=digest
        )


def test_tool_changed_during_version_probe_is_rejected(approved_tools, monkeypatch):
    node, modules, manifest, digest = approved_tools

    def substitute(*args, **kwargs):
        (modules / "@oai/artifact-tool/index.js").write_text("changed while running")
        return "v24.19.0\n"

    monkeypatch.setattr(verify_formula_runtime.subprocess, "check_output", substitute)
    with pytest.raises(ValueError, match="module bytes differ"):
        verify_formula_runtime.verify_runtime(
            node, modules, manifest=manifest, expected_manifest_sha256=digest
        )


def test_matching_version_without_independent_manifest_is_not_qualified(
    approved_tools, monkeypatch
):
    node, modules, _manifest, _digest = approved_tools
    monkeypatch.delenv("STRATHMARK_FORMULA_TOOLCHAIN_MANIFEST", raising=False)
    monkeypatch.delenv("STRATHMARK_FORMULA_TOOLCHAIN_SHA256", raising=False)
    with pytest.raises(ValueError, match="independently approved"):
        verify_formula_runtime.verify_runtime(node, modules)


def test_formula_children_ignore_ambient_node_code(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/forged.cjs")
    monkeypatch.setenv("NODE_PATH", "/tmp/forged-modules")
    assert "NODE_OPTIONS" not in verify_formula_runtime.formula_environment()
    assert "NODE_PATH" not in verify_formula_runtime.formula_environment()


def test_designated_replay_rechecks_bytes_after_the_workbook_process(
    approved_tools, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    node, modules, manifest, digest = approved_tools
    monkeypatch.setenv("STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION", "1")
    monkeypatch.setenv("STRATHMARK_FORMULA_TOOLCHAIN_MANIFEST", str(manifest))
    monkeypatch.setenv("STRATHMARK_FORMULA_TOOLCHAIN_SHA256", digest)
    monkeypatch.setattr(test_formula_replay, "_artifact_engine_paths", lambda: (node, modules))
    monkeypatch.setattr(
        verify_formula_runtime.subprocess, "check_output", lambda *args, **kwargs: "v24.19.0\n"
    )

    def forged_replay(argv, **kwargs):
        if argv[0] != "cmd":
            (modules / "@oai/artifact-tool/index.js").write_text("changed during workbook proof")
        return SimpleNamespace(returncode=0, stdout='{"status": "verified"}', stderr="")

    monkeypatch.setattr(test_formula_replay.subprocess, "run", forged_replay)
    scratch = tmp_path / "replay"
    scratch.mkdir()
    with pytest.raises(ValueError, match="module bytes differ"):
        test_formula_replay.test_designated_verification_runs_independent_artifact_engine(scratch)
    assert not (scratch / "formula-toolchain-verification.json").exists()


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("target", ["node", "module", "directory"])
def test_peer_writable_formula_inputs_are_refused_before_execution(
    approved_tools, monkeypatch, target
):
    node, modules, manifest, digest = approved_tools
    path = {"node": node, "module": modules / "@oai/artifact-tool/index.js", "directory": modules}[
        target
    ]
    before = path.stat().st_mode & 0o777
    path.chmod(0o777 if path.is_dir() else 0o666)
    monkeypatch.setattr(
        verify_formula_runtime.subprocess,
        "check_output",
        lambda *args, **kwargs: pytest.fail("unsafe tool executed"),
    )
    try:
        with pytest.raises(PermissionError, match="writable by another account"):
            verify_formula_runtime.verify_runtime(
                node, modules, manifest=manifest, expected_manifest_sha256=digest
            )
        assert path.stat().st_mode & 0o777 == (0o777 if path.is_dir() else 0o666)
    finally:
        path.chmod(before)


@pytest.mark.skipif(__import__("os").name != "posix", reason="portable symlink fixture")
def test_installation_alias_retarget_never_changes_executed_tool(
    approved_tools, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    node, modules, manifest, digest = approved_tools
    other_node = tmp_path / "unapproved-node"
    other_node.write_bytes(b"unapproved executable")
    other_modules = tmp_path / "unapproved-modules"
    other_modules.mkdir()
    node_alias = tmp_path / "node-alias"
    module_alias = tmp_path / "module-alias"
    node_alias.symlink_to(node)
    module_alias.symlink_to(modules, target_is_directory=True)
    monkeypatch.setenv("STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION", "1")
    monkeypatch.setenv("STRATHMARK_FORMULA_TOOLCHAIN_MANIFEST", str(manifest))
    monkeypatch.setenv("STRATHMARK_FORMULA_TOOLCHAIN_SHA256", digest)
    monkeypatch.setattr(
        test_formula_replay, "_artifact_engine_paths", lambda: (node_alias, module_alias)
    )

    def version_probe(argv, **kwargs):
        assert argv[0] == str(node)
        node_alias.unlink()
        module_alias.unlink()
        node_alias.symlink_to(other_node)
        module_alias.symlink_to(other_modules, target_is_directory=True)
        return "v24.19.0\n"

    def replay(argv, **kwargs):
        assert argv[0] == str(node)
        assert kwargs["env"]["STRATHMARK_ARTIFACT_NODE_MODULES"] == str(modules)
        return SimpleNamespace(returncode=0, stdout='{"status": "verified"}', stderr="")

    monkeypatch.setattr(verify_formula_runtime.subprocess, "check_output", version_probe)
    monkeypatch.setattr(test_formula_replay.subprocess, "run", replay)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    test_formula_replay.test_designated_verification_runs_independent_artifact_engine(scratch)
    assert json.loads((scratch / "formula-toolchain-verification.json").read_text())["toolchain"][
        "node_path"
    ] == str(node)
