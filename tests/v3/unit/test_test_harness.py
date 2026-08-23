from __future__ import annotations

import os
from pathlib import Path

_PATH_VARIABLES = (
    "STRATHMARK_V3_DB_PATH",
    "STRATHMARK_DB_PATH",
    "STRATHMARK_V3_TEMP_PATH",
    "STRATHMARK_V3_BLOB_ROOT",
    "STRATHMARK_V3_BUNDLE_ROOT",
    "STRATHMARK_V3_ARCHIVE_ROOT",
    "HYPOTHESIS_STORAGE_DIRECTORY",
)
_COLLECTION_PATHS = {name: Path(os.environ[name]).resolve() for name in _PATH_VARIABLES}
_COLLECTION_TEST_FLAG = os.environ["STRATHMARK_TEST_DB"]


def test_v3_harness_sets_every_isolated_path_before_test_import(
    tmp_path_factory,
) -> None:
    paths = _COLLECTION_PATHS
    base_temp = tmp_path_factory.getbasetemp().resolve()

    assert _COLLECTION_TEST_FLAG == "1"
    assert len(set(paths.values())) == len(paths)
    assert paths["STRATHMARK_V3_DB_PATH"] != paths["STRATHMARK_DB_PATH"]
    assert all(path == base_temp or base_temp in path.parents for path in paths.values())
    assert paths["STRATHMARK_V3_DB_PATH"].parent.is_dir()
    assert paths["STRATHMARK_DB_PATH"].parent.is_dir()
    assert all(
        paths[name].is_dir()
        for name in (
            "STRATHMARK_V3_TEMP_PATH",
            "STRATHMARK_V3_BLOB_ROOT",
            "STRATHMARK_V3_BUNDLE_ROOT",
            "STRATHMARK_V3_ARCHIVE_ROOT",
            "HYPOTHESIS_STORAGE_DIRECTORY",
        )
    )
    assert all("production" not in path.as_posix().casefold() for path in paths.values())
