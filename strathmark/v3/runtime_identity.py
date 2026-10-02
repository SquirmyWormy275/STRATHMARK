"""Content identity of the installed Python implementation, including editable builds."""

from hashlib import sha256
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
