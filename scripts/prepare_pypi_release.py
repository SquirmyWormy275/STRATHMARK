"""Prepare immutable PyPI tags after successful CI on the maintained main branch."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from scripts.verify_release_tag import ReleaseTagError, tomllib


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _json(url: str, *, token: str | None = None) -> dict | None:
    headers = {"Accept": "application/json", "User-Agent": "strathmark-release"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=30
        ) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ReleaseTagError(f"release metadata request failed with HTTP {error.code}") from error


def pypi_has_release(version: str) -> bool:
    data = _json(f"https://pypi.org/pypi/strathmark/{version}/json")
    if data is None:
        return False
    files = data.get("urls", [])
    if (
        data.get("info", {}).get("version") != version
        or not any(file["packagetype"] == "bdist_wheel" for file in files)
        or not any(file["packagetype"] == "sdist" for file in files)
        or any(file.get("yanked") for file in files)
    ):
        raise ReleaseTagError("existing PyPI release is incomplete or yanked; review recovery")
    return True


def source_ci_passed(commit: str) -> bool:
    repository = os.environ["GITHUB_REPOSITORY"]
    query = urllib.parse.urlencode(
        {"head_sha": commit, "branch": "main", "event": "push", "status": "success"}
    )
    data = _json(
        f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs?{query}",
        token=os.environ["GH_TOKEN"],
    )
    return bool(
        data
        and any(
            run["head_sha"] == commit
            and run["head_branch"] == "main"
            and run["event"] == "push"
            and run["conclusion"] == "success"
            for run in data.get("workflow_runs", [])
        )
    )


def successful_main_ci(event: dict, repository: str) -> str:
    run = event.get("workflow_run", {})
    if (
        event.get("repository", {}).get("full_name") != repository
        or run.get("head_repository", {}).get("full_name") != repository
        or run.get("head_branch") != "main"
        or run.get("event") != "push"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise ReleaseTagError("automatic publication requires successful same-repository main CI")
    commit = run.get("head_sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseTagError("CI event does not identify an exact source commit")
    return commit


def prepare_release(
    repository: Path,
    ci_commit: str,
    *,
    release_exists: Callable[[str], bool] = pypi_has_release,
    ci_passed: Callable[[str], bool] = source_ci_passed,
    push: bool = True,
) -> dict[str, str]:
    head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    if head != ci_commit:
        return {"publish": "false", "reason": "main has advanced; wait for its own CI"}
    if _git(repository, "status", "--porcelain", "--untracked-files=all").stdout:
        raise ReleaseTagError("release tooling checkout must be clean")
    version = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?", version
    ):
        raise ReleaseTagError("project version must use a canonical Python release version")
    if release_exists(version):
        return {"publish": "false", "version": version, "reason": "version already on PyPI"}

    source_tag = f"v{version}"
    authorization_tag = f"pypi-v{version}"
    source_ref = f"refs/tags/{source_tag}"
    existing = _git(repository, "rev-parse", "--verify", source_ref, check=False)
    if existing.returncode == 0:
        source_commit = _git(repository, "rev-parse", f"{source_ref}^{{commit}}").stdout.strip()
        metadata = _git(repository, "show", f"{source_ref}:pyproject.toml").stdout
        if tomllib.loads(metadata)["project"]["version"] != version:
            raise ReleaseTagError("existing immutable source tag has a different package version")
        if _git(
            repository, "merge-base", "--is-ancestor", source_commit, head, check=False
        ).returncode:
            raise ReleaseTagError("existing source tag is not an ancestor of maintained main")
        if source_commit != head and not ci_passed(source_commit):
            raise ReleaseTagError("existing source tag has no successful main CI")
    else:
        source_commit = head

    authorization_ref = f"refs/tags/{authorization_tag}"
    authorization = _git(repository, "cat-file", "-t", authorization_ref, check=False)
    if authorization.returncode == 0:
        if authorization.stdout.strip() != "tag":
            raise ReleaseTagError("existing publication authorization must be annotated")
        authorized_commit = _git(
            repository, "rev-parse", f"{authorization_ref}^{{commit}}"
        ).stdout.strip()
        if authorized_commit != source_commit:
            raise ReleaseTagError(
                "existing publication authorization identifies a different commit"
            )

    for tag, exists, message in (
        (source_tag, existing.returncode == 0, f"STRATHMARK {version}: successful main CI"),
        (
            authorization_tag,
            authorization.returncode == 0,
            f"Authorize PyPI {version} under the owner's automatic publication standing order.",
        ),
    ):
        if not exists:
            _git(
                repository,
                "-c",
                "user.name=github-actions[bot]",
                "-c",
                "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "tag",
                "-a",
                tag,
                source_commit,
                "-m",
                message,
            )
    if push:
        _git(repository, "push", "--atomic", "origin", source_ref, authorization_ref)
    return {
        "publish": "true",
        "release_tag": authorization_tag,
        "version": version,
        "source_commit": source_commit,
    }


def main() -> int:
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ReleaseTagError("publisher must execute on main")
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        tag = os.environ["RELEASE_TAG"]
        if not re.fullmatch(r"pypi-v[0-9][0-9A-Za-z.]*", tag):
            raise ReleaseTagError("manual publication requires a safe pypi-v<version> tag")
        outputs = {"publish": "true", "release_tag": tag}
    elif os.environ.get("GITHUB_EVENT_NAME") == "workflow_run":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        commit = successful_main_ci(event, os.environ["GITHUB_REPOSITORY"])
        outputs = prepare_release(Path.cwd(), commit)
    else:
        raise ReleaseTagError("unsupported publication trigger")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in outputs.items():
            stream.write(f"{key}={value}\n")
    print(json.dumps(outputs, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
