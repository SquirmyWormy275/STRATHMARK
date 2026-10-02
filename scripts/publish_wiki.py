"""Preview, check, or publish versioned pages and verify their remote contents."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(directory: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=directory, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "check", "publish"), default="preview")
    args = parser.parse_args()
    remote = git(ROOT, "remote", "get-url", "origin")
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?", remote
    )
    if match is None:
        parser.error("origin must identify the GitHub repository")
    slug = match.group(1)
    repository = slug.rsplit("/", 1)[1]
    source = ROOT / ("wiki" if (ROOT / "wiki/Home.md").is_file() else "docs/wiki")
    if git(ROOT, "status", "--porcelain") and args.mode == "publish":
        parser.error("publish only from a committed clean repository")
    if args.mode == "publish":
        published_source = git(ROOT, "ls-remote", "origin", "refs/heads/main").split()[0]
        if git(ROOT, "rev-parse", "HEAD") != published_source:
            parser.error("publish only the exact merged main commit")
    pages = [page for page in sorted(source.glob("*.md")) if page.name != "README.md"]
    if not pages:
        parser.error("no versioned wiki pages found")
    with tempfile.TemporaryDirectory(prefix=repository.lower() + "-wiki-") as temporary:
        wiki = Path(temporary)
        subprocess.run(
            ["git", "clone", "--quiet", f"https://github.com/{slug}.wiki.git", str(wiki)],
            check=True,
        )
        for page in pages:
            shutil.copyfile(page, wiki / page.name)
        changed = bool(git(wiki, "status", "--porcelain"))
        if args.mode == "check":
            if changed:
                raise SystemExit("Published wiki differs from versioned source")
            print("Published wiki matches every versioned page.")
            return
        if args.mode == "preview":
            subprocess.run(
                ["git", "add", "--", *[page.name for page in pages]], cwd=wiki, check=True
            )
            print(git(wiki, "diff", "--cached", "--stat"))
            print(git(wiki, "diff", "--cached"))
            return
        if changed:
            subprocess.run(
                ["git", "add", "--", *[page.name for page in pages]], cwd=wiki, check=True
            )
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"Publish versioned wiki from {repository} {git(ROOT, 'rev-parse', '--short', 'HEAD')}",
                ],
                cwd=wiki,
                check=True,
            )
            subprocess.run(["git", "push", "origin", "HEAD"], cwd=wiki, check=True)
        expected = git(wiki, "rev-parse", "HEAD")
        subprocess.run(["git", "fetch", "--quiet", "origin"], cwd=wiki, check=True)
        if git(wiki, "rev-parse", "origin/HEAD") != expected:
            raise SystemExit("Remote wiki HEAD changed; fetch and review before republishing")
        for page in pages:
            published = subprocess.check_output(
                ["git", "show", f"origin/HEAD:{page.name}"], cwd=wiki
            )
            if published != page.read_bytes():
                raise SystemExit(f"Remote page verification failed: {page.name}")
        print(f"Published and verified {repository} wiki at {expected}.")


if __name__ == "__main__":
    main()
