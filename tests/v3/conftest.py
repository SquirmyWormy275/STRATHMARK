"""Collection-time isolation for the V3 test tree.

V3 and legacy database paths plus every mutable artifact root are assigned
inside an explicitly supplied pytest base directory before V3 test modules are
collected.  Production and operator paths are never valid test targets.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Install isolated paths before pytest imports any V3 test module."""

    if config.option.basetemp is None:
        raise pytest.UsageError("V3 tests require an explicit isolated --basetemp path")

    factory = getattr(config, "_tmp_path_factory", None)
    base_temp = (
        factory.getbasetemp()
        if factory is not None
        else Path(str(config.option.basetemp)).expanduser().resolve(strict=False)
    )
    session_root = base_temp / f"strathmark-v3-{uuid.uuid4().hex}"
    database_root = session_root / "databases"
    roots = {
        "STRATHMARK_V3_TEMP_PATH": session_root / "runtime",
        "STRATHMARK_V3_BLOB_ROOT": session_root / "blobs",
        "STRATHMARK_V3_BUNDLE_ROOT": session_root / "bundles",
        "STRATHMARK_V3_ARCHIVE_ROOT": session_root / "archive",
        "STRATHMARK_V3_BACKUP_ROOT": session_root / "backup",
        "STRATHMARK_V3_RECOVERY_ROOT": session_root / "recovery",
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT": session_root / "integrity-keys",
        "HYPOTHESIS_STORAGE_DIRECTORY": session_root / "hypothesis",
    }

    database_root.mkdir(parents=True, exist_ok=False)
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=False)

    os.environ["STRATHMARK_TEST_DB"] = "1"
    os.environ["STRATHMARK_V3_DB_PATH"] = str(database_root / "v3.sqlite3")
    os.environ["STRATHMARK_DB_PATH"] = str(database_root / "legacy-v2.sqlite3")
    for variable, root in roots.items():
        os.environ[variable] = str(root)
