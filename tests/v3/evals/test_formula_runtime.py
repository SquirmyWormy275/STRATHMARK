"""Portable mode cannot accidentally qualify a mismatched artifact runtime."""

from __future__ import annotations

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
    with pytest.raises(ValueError, match="artifact_tool_version requires 2.8.52, observed 2.8.59"):
        verify_formula_runtime.verify_runtime(node, modules)


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
