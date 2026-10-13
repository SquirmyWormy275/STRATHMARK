"""Release alerts reject fork runs, deduplicate retries, and preserve newer failures."""

from __future__ import annotations

from pathlib import Path

from scripts.report_release_failure import report_failure


def _event(conclusion="failure", *, number=10):
    return {
        "repository": {"full_name": "owner/STRATHMARK"},
        "workflow_run": {
            "head_repository": {"full_name": "owner/STRATHMARK"},
            "head_branch": "main",
            "status": "completed",
            "name": "Publish to PyPI",
            "event": "workflow_run",
            "conclusion": conclusion,
            "id": 12345,
            "run_number": number,
            "run_attempt": 1,
        },
    }


def test_failure_creates_assigned_alert_and_duplicate_delivery_is_a_noop():
    commands = []
    issues = []

    def github(args):
        commands.append(args)
        if args[:2] == ["issue", "list"]:
            return issues
        assert args[:2] == ["issue", "create"]
        assert args[args.index("--assignee") + 1] == "owner"
        body = Path(args[args.index("--body-file") + 1]).read_text()
        assert "https://github.com/owner/STRATHMARK/actions/runs/12345" in body
        issues.append({"number": 7, "title": args[args.index("--title") + 1], "body": body})

    report_failure(_event(), "owner/STRATHMARK", gh=github)
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    assert len(commands) == 3


def test_success_closes_alert_but_older_success_cannot_hide_newer_failure():
    commands = []
    issue = {
        "number": 7,
        "title": "Release automation failure: Publish to PyPI",
        "body": "<!-- strathmark-release-alert: 10:1 -->",
    }

    def github(args):
        commands.append(args)
        if args[0] == "api":
            return {
                "jobs": [
                    {
                        "name": "Publish matching GitHub Release after PyPI verification",
                        "conclusion": "success",
                    }
                ]
            }
        return [issue] if args[:2] == ["issue", "list"] else None

    report_failure(_event("success", number=9), "owner/STRATHMARK", gh=github)
    assert len(commands) == 1
    report_failure(_event("success", number=11), "owner/STRATHMARK", gh=github)
    assert commands[-1][:3] == ["issue", "close", "7"]


def test_skipped_publisher_cannot_close_an_unrecovered_release_alert():
    commands = []

    def github(args):
        commands.append(args)
        if args[0] == "api":
            return {
                "jobs": [
                    {
                        "name": "Publish matching GitHub Release after PyPI verification",
                        "conclusion": "skipped",
                    }
                ]
            }
        return [
            {
                "number": 7,
                "title": "Release automation failure: Publish to PyPI",
                "body": "<!-- strathmark-release-alert: 9:1 -->",
            }
        ]

    report_failure(_event("success"), "owner/STRATHMARK", gh=github)
    assert len(commands) == 2


def test_new_failure_updates_one_existing_alert_and_notifies_the_assignee():
    commands = []

    def github(args):
        commands.append(args)
        return [
            {
                "number": 7,
                "title": "Release automation failure: Publish to PyPI",
                "body": "<!-- strathmark-release-alert: 9:1 -->",
            }
        ]

    report_failure(_event(), "owner/STRATHMARK", gh=github)
    assert [args[1] for args in commands] == ["list", "edit", "comment"]


def test_fork_pull_request_and_canceled_ci_never_open_release_alerts():
    def unexpected(args):
        raise AssertionError(args)

    for field, value in [
        ("head_repository", {"full_name": "fork/STRATHMARK"}),
        ("head_branch", "feature"),
        ("conclusion", "cancelled"),
    ]:
        event = _event()
        event["workflow_run"][field] = value
        report_failure(event, "owner/STRATHMARK", gh=unexpected)
    event = _event()
    event["workflow_run"].update(name="CI", event="pull_request")
    report_failure(event, "owner/STRATHMARK", gh=unexpected)
