"""Content identity of the installed Python implementation, including editable builds."""

import re
import subprocess
import tarfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from strathmark.v3.contracts.canonical import canonical_digest


def implementation_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    return canonical_digest(
        {
            str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py"))
        }
    )


def verify_source_revision(repository: Path, revision: str) -> str:
    """Verify every installed Python source byte against an immutable Git commit."""
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("source revision must be a complete lowercase Git commit")
    archive = subprocess.run(
        ["git", "-C", str(repository.resolve(strict=True)), "archive", revision, "strathmark"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    with tarfile.open(fileobj=BytesIO(archive)) as tree:
        sources = {
            item.name.removeprefix("strathmark/"): sha256(tree.extractfile(item).read()).hexdigest()
            for item in tree.getmembers()
            if item.isfile() and item.name.endswith(".py")
        }
    digest = implementation_digest()
    if canonical_digest(sources) != digest:
        raise ValueError("installed implementation differs from the requested source commit")
    return digest
