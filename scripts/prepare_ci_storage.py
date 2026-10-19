"""Keep Windows CI authority off the runner's shared work drive."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision-authority", action="store_true")
    args = parser.parse_args()
    environment_file = Path(os.environ["GITHUB_ENV"])
    root = Path(os.environ["RUNNER_TEMP"])
    values = {}
    if os.name == "nt":
        root = Path.home() / f".strathmark-ci-{uuid4().hex}"
        root.mkdir(mode=0o700)
        if args.provision_authority:
            from strathmark.private_storage import private_directory

            private_directory(root)
        # Build-only jobs do not yet have package dependencies. Their installed
        # smoke probes provision their own fresh authority subdirectories.
        values.update(TEMP=str(root), TMP=str(root))
    values["STRATHMARK_CI_TEMP"] = root.as_posix()
    with environment_file.open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
