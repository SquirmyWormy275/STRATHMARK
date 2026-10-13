"""Keep one owner-assigned GitHub alert per failing main release workflow."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from scripts.publish_github_release import run_gh


def report_failure(event: dict, repository: str, *, gh=run_gh) -> None:
    run = event.get("workflow_run", {})
    if (
        event.get("repository", {}).get("full_name") != repository
        or run.get("head_repository", {}).get("full_name") != repository
        or run.get("head_branch") != "main"
        or run.get("status") != "completed"
        or run.get("name") not in {"CI", "Publish to PyPI"}
        or (run.get("name") == "CI" and run.get("event") != "push")
        or run.get("conclusion") not in {"failure", "timed_out", "success"}
    ):
        return
    run_id = int(run["id"])
    sequence = (int(run["run_number"]), int(run.get("run_attempt", 1)))
    title = f"Release automation failure: {run['name']}"
    marker = f"<!-- strathmark-release-alert: {sequence[0]}:{sequence[1]} -->"
    issues = gh(
        [
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title,body",
        ]
    )
    existing = next(
        (
            issue
            for issue in issues
            if issue["title"] == title and "<!-- strathmark-release-alert:" in issue["body"]
        ),
        None,
    )
    if existing:
        previous = re.search(
            r"<!-- strathmark-release-alert: ([0-9]+):([0-9]+) -->", existing["body"]
        )
        if not previous or tuple(map(int, previous.groups())) > sequence:
            return
    if run["conclusion"] == "success":
        if existing:
            if run["name"] == "Publish to PyPI":
                jobs = gh(
                    [
                        "api",
                        f"repos/{repository}/actions/runs/{run_id}/attempts/"
                        f"{sequence[1]}/jobs?per_page=100",
                    ]
                )
                if not any(
                    job["name"] == "Publish matching GitHub Release after PyPI verification"
                    and job["conclusion"] == "success"
                    for job in jobs["jobs"]
                ):
                    return
            gh(
                [
                    "issue",
                    "close",
                    str(existing["number"]),
                    "--repo",
                    repository,
                    "--reason",
                    "completed",
                ]
            )
        return
    url = f"https://github.com/{repository}/actions/runs/{run_id}"
    if existing and marker in existing["body"]:
        return
    body = (
        f"{marker}\n\n{run['name']} failed on maintained `main`.\n\n"
        f"[Inspect workflow run {run_id}]({url}).\n\n"
        "Check the failed jobs and recover the release using docs/PACKAGE_RELEASE.md. "
        "Do not move existing tags or replace uploaded packages. "
        "This alert closes when a later run of the same workflow succeeds.\n"
    )
    with tempfile.TemporaryDirectory(prefix="strathmark-release-alert-") as directory:
        path = Path(directory) / "alert.md"
        path.write_text(body, encoding="utf-8")
        if existing:
            number = str(existing["number"])
            gh(["issue", "edit", number, "--repo", repository, "--body-file", str(path)])
            gh(["issue", "comment", number, "--repo", repository, "--body-file", str(path)])
        else:
            gh(
                [
                    "issue",
                    "create",
                    "--repo",
                    repository,
                    "--title",
                    title,
                    "--assignee",
                    repository.split("/")[0],
                    "--body-file",
                    str(path),
                ]
            )


def main() -> int:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    report_failure(event, os.environ["GITHUB_REPOSITORY"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
