from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _run_isolated_import(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path.cwd()),
            "STRATHMARK_TEST_DB": "1",
            "STRATHMARK_V3_DB_PATH": str(tmp_path / "v3.sqlite3"),
            "STRATHMARK_DB_PATH": str(tmp_path / "legacy-v2.sqlite3"),
            "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "runtime"),
            "STRATHMARK_V3_BLOB_ROOT": str(tmp_path / "blobs"),
            "STRATHMARK_V3_BUNDLE_ROOT": str(tmp_path / "bundles"),
            "STRATHMARK_V3_ARCHIVE_ROOT": str(tmp_path / "archive"),
        }
    )
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_v3_and_existing_public_api_import_without_eager_side_effects(tmp_path: Path) -> None:
    result = _run_isolated_import(
        """
        import importlib.abc
        import os
        import pathlib
        import socket
        import sqlite3
        import sys
        import threading

        blocked_roots = {
            "catboost", "google.generativeai", "lightgbm", "ollama", "supabase", "xgboost"
        }

        class BlockedOptionalFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in blocked_roots or any(
                    fullname.startswith(root + ".") for root in blocked_roots
                ):
                    raise AssertionError(f"optional provider/native ML import attempted: {fullname}")
                return None

        def forbidden(*args, **kwargs):
            raise AssertionError("import attempted an external side effect")

        sys.meta_path.insert(0, BlockedOptionalFinder())
        os.mkdir = forbidden
        os.makedirs = forbidden
        pathlib.Path.mkdir = forbidden
        sqlite3.connect = forbidden
        socket.create_connection = forbidden
        socket.socket.connect = forbidden
        threading.Thread.start = forbidden

        import strathmark
        import strathmark.v3
        from strathmark import HandicapCalculator, PredictionV2Model
        from strathmark.v3.contracts.canonical import canonical_digest

        assert HandicapCalculator is not None
        assert PredictionV2Model is not None
        assert canonical_digest({"ok": True})
        assert "strathmark.v3.composition" not in sys.modules
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_production_test_path_is_rejected_before_any_client_can_load(tmp_path: Path) -> None:
    result = _run_isolated_import(
        """
        import importlib.abc
        import sqlite3
        import sys

        class BlockClientImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "supabase" or fullname.startswith("supabase."):
                    raise AssertionError("client loaded before configuration validation")
                return None

        def forbidden_connect(*args, **kwargs):
            raise AssertionError("database connection opened before configuration validation")

        sys.meta_path.insert(0, BlockClientImports())
        sqlite3.connect = forbidden_connect

        from strathmark.v3.composition import resolve_runtime_config
        from strathmark.v3.contracts.errors import ConfigurationError

        try:
            resolve_runtime_config(
                {
                    "STRATHMARK_TEST_DB": "1",
                    "STRATHMARK_V3_DB_PATH": "C:/runtime/production/v3.sqlite3",
                    "STRATHMARK_V3_TEMP_PATH": "C:/runtime/test-temp",
                }
            )
        except ConfigurationError:
            pass
        else:
            raise AssertionError("known production target was accepted")

        assert "supabase" not in sys.modules
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
