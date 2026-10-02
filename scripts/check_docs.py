"""Validate repository Markdown links and links published into the separate wiki."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    files = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    errors = []
    for filename in sorted(set(files)):
        if not filename.endswith(".md"):
            continue
        page = ROOT / filename
        if not page.exists():
            continue
        wiki = filename.startswith(("wiki/", "docs/wiki/")) and page.name != "README.md"
        for raw in re.findall(
            r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)', page.read_text(encoding="utf-8")
        ):
            link = raw.strip("<>")
            path = unquote(urlsplit(link).path)
            if not path or urlsplit(link).scheme or link.startswith("//"):
                continue
            if wiki and path.startswith("../"):
                errors.append(
                    f"{filename}: repository-relative link cannot be published to the wiki: {link}"
                )
            target = page.parent / path
            if not target.exists() and not (wiki and target.with_suffix(".md").exists()):
                errors.append(f"{filename}: missing local target: {link}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Repository and wiki-source Markdown links passed.")


if __name__ == "__main__":
    main()
