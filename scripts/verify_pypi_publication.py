"""Verify PyPI contains the exact built artifacts, then install the indexed release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def verify_artifact_metadata(dist_dir: Path, version: str, metadata: dict) -> None:
    artifacts = sorted([*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz")])
    if len(artifacts) != 2 or not any(path.suffix == ".whl" for path in artifacts):
        raise ValueError("expected one built wheel and one source archive")
    if metadata.get("info", {}).get("version") != version:
        raise ValueError("PyPI metadata does not identify the requested version")
    files = {file["filename"]: file for file in metadata.get("urls", [])}
    if set(files) != {artifact.name for artifact in artifacts}:
        raise ValueError("PyPI file set does not match the built artifacts")
    for artifact in artifacts:
        published = files[artifact.name]
        if published.get("yanked"):
            raise ValueError("published artifact is yanked")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != published["digests"]["sha256"]:
            raise ValueError(f"published artifact hash differs: {artifact.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    url = f"https://pypi.org/pypi/strathmark/{args.version}/json"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=30) as reply:
                metadata = json.load(reply)
            verify_artifact_metadata(args.dist_dir, args.version, metadata)
            break
        except (urllib.error.HTTPError, ValueError):
            if attempt == 5:
                raise
            time.sleep(10)
    with tempfile.TemporaryDirectory(prefix="strathmark-pypi-") as directory:
        root = Path(directory)
        environment = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--no-cache-dir",
                f"strathmark=={args.version}",
            ],
            check=True,
            cwd=root,
        )
        subprocess.run([str(python), "-m", "pip", "check"], check=True, cwd=root)
        env = {**os.environ, "EXPECTED_STRATHMARK_VERSION": args.version}
        subprocess.run(
            [
                str(python),
                "-c",
                "import os; from importlib.metadata import version; "
                "import strathmark; import strathmark.v3; "
                "assert version('strathmark') == os.environ['EXPECTED_STRATHMARK_VERSION']",
            ],
            check=True,
            cwd=root,
            env=env,
        )
        demo = subprocess.run(
            [str(python), "-m", "strathmark", "demo"],
            check=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = json.loads(demo.stdout)
        assert output["purpose"] == "synthetic_v2_demo"
        assert len(output["results"]) == 2
        assert all(not result["degraded"] for result in output["results"])
    print(f"PyPI {args.version}: exact artifact hashes and isolated indexed installation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
