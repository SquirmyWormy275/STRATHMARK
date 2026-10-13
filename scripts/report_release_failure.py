"""Track owner-assigned GitHub alerts and exact failed-run recovery."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from scripts.publish_github_release import run_gh

ACTIONS_BOT_ID = 41898282


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
    run_id, attempt = int(run["id"]), int(run.get("run_attempt", 1))
    title = f"Release automation failure: {run['name']} (run {run_id})"
    marker = f"<!-- strathmark-release-alert: {run_id}:{attempt} -->"
    pages = gh(
        ["api", f"repos/{repository}/issues?state=all&per_page=100", "--paginate", "--slurp"]
    )
    existing = None
    previous_attempt = 0
    for page in pages:
        for issue in page:
            previous = re.search(
                r"<!-- strathmark-release-alert: ([0-9]+):([0-9]+) -->", issue.get("body") or ""
            )
            if (
                issue.get("title") == title
                and issue.get("user", {}).get("id") == ACTIONS_BOT_ID
                and issue.get("user", {}).get("type") == "Bot"
                and previous
                and int(previous[1]) == run_id
            ):
                existing, previous_attempt = issue, int(previous[2])
                break
        if existing:
            break
    if existing and (
        previous_attempt > attempt
        or (existing["state"] == "closed" and previous_attempt >= attempt)
    ):
        return
    success = run["conclusion"] == "success"
    if success:
        if not existing or existing["state"] == "closed":
            return
        if run["name"] == "Publish to PyPI":
            jobs = gh(
                [
                    "api",
                    f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                ]
            )
            if not any(
                job["name"] == "Publish matching GitHub Release after PyPI verification"
                and job["conclusion"] == "success"
                for job in jobs["jobs"]
            ):
                return
    elif existing and previous_attempt == attempt:
        return
    url = f"https://github.com/{repository}/actions/runs/{run_id}"
    body = f"{marker}\n\n{run['name']} {'recovered' if success else 'failed'} on maintained `main` (attempt {attempt}).\n\n[Inspect workflow run {run_id}]({url}).\n\n"
    if not success:
        body += "Check the failed jobs and recover this exact run using docs/PACKAGE_RELEASE.md. Do not move existing tags or replace uploaded packages. A successful retry of this same run closes the alert; a different version cannot hide its unrecovered release.\n"
    with tempfile.TemporaryDirectory(prefix="strathmark-release-alert-") as directory:
        path = Path(directory) / "alert.md"
        path.write_text(body, encoding="utf-8")
        if existing:
            number = str(existing["number"])
            if existing["state"] == "closed":
                gh(["issue", "reopen", number, "--repo", repository])
            gh(["issue", "edit", number, "--repo", repository, "--body-file", str(path)])
            if success:
                gh(["issue", "close", number, "--repo", repository, "--reason", "completed"])
            else:
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
