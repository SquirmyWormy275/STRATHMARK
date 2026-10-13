"""Alerts authenticate ownership and bind recovery to the exact failed run."""

from pathlib import Path

from scripts.report_release_failure import report_failure


def _event(conclusion="failure", *, attempt=1, run_id=12345):
    return {
        "repository": {"full_name": "owner/STRATHMARK"},
        "workflow_run": {
            "head_repository": {"full_name": "owner/STRATHMARK"},
            "head_branch": "main",
            "status": "completed",
            "name": "Publish to PyPI",
            "event": "workflow_run",
            "conclusion": conclusion,
            "id": run_id,
            "run_number": 10,
            "run_attempt": attempt,
        },
    }


class GitHub:
    def __init__(self):
        self.commands = []
        self.issues = []
        self.job_conclusion = "success"

    def __call__(self, args):
        self.commands.append(args)
        if args[0] == "api":
            if "/issues?" in args[1]:
                assert "state=all" in args[1] and "--paginate" in args
                return [self.issues]
            return {
                "jobs": [
                    {
                        "name": "Publish matching GitHub Release after PyPI verification",
                        "conclusion": self.job_conclusion,
                    }
                ]
            }
        if "--body-file" in args:
            body = Path(args[args.index("--body-file") + 1]).read_text()
        if args[:2] == ["issue", "create"]:
            assert args[args.index("--assignee") + 1] == "owner"
            assert "https://github.com/owner/STRATHMARK/actions/runs/12345" in body
            self.issues.append(
                {
                    "number": 7,
                    "title": args[args.index("--title") + 1],
                    "body": body,
                    "state": "open",
                    "user": {"id": 41898282, "type": "Bot"},
                }
            )
        elif args[:2] == ["issue", "edit"]:
            self.issues[0]["body"] = body
        elif args[:2] == ["issue", "close"]:
            self.issues[0]["state"] = "closed"
        elif args[:2] == ["issue", "reopen"]:
            self.issues[0]["state"] = "open"
        return None


def test_failure_creates_assigned_alert_and_duplicate_delivery_is_a_noop():
    github = GitHub()
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    assert len(github.commands) == 3


def test_only_same_run_recovery_closes_the_failed_release_alert():
    github = GitHub()
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    report_failure(_event("success", run_id=54321), "owner/STRATHMARK", gh=github)
    assert github.issues[0]["state"] == "open"
    report_failure(_event("success", attempt=2), "owner/STRATHMARK", gh=github)
    assert github.issues[0]["state"] == "closed"
    assert "12345:2" in github.issues[0]["body"]


def test_delayed_old_failure_cannot_reopen_recovered_alert():
    github = GitHub()
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    report_failure(_event("success", attempt=2), "owner/STRATHMARK", gh=github)
    before = len(github.commands)
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    report_failure(_event(attempt=2), "owner/STRATHMARK", gh=github)
    assert len(github.commands) == before + 2
    assert github.issues[0]["state"] == "closed"


def test_newer_failed_attempt_reopens_one_recovered_alert():
    github = GitHub()
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    report_failure(_event("success", attempt=2), "owner/STRATHMARK", gh=github)
    report_failure(_event(attempt=3), "owner/STRATHMARK", gh=github)
    assert len(github.issues) == 1 and github.issues[0]["state"] == "open"
    assert any(args[:2] == ["issue", "reopen"] for args in github.commands)


def test_skipped_publisher_cannot_hide_unrecovered_failure():
    github = GitHub()
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    github.job_conclusion = "skipped"
    report_failure(_event("success", attempt=2), "owner/STRATHMARK", gh=github)
    assert github.issues[0]["state"] == "open"


def test_forged_public_issue_cannot_suppress_real_alert():
    github = GitHub()
    github.issues.append(
        {
            "number": 99,
            "title": "Release automation failure: Publish to PyPI (run 12345)",
            "body": "<!-- strathmark-release-alert: 12345:999999999 -->",
            "state": "open",
            "user": {"id": 123, "type": "User"},
        }
    )
    report_failure(_event(), "owner/STRATHMARK", gh=github)
    assert len(github.issues) == 2
    assert github.issues[0]["body"].endswith("999999999 -->")


def test_older_success_cannot_hide_newer_failure_in_the_same_run():
    github = GitHub()
    report_failure(_event(attempt=3), "owner/STRATHMARK", gh=github)
    report_failure(_event("success", attempt=2), "owner/STRATHMARK", gh=github)
    assert github.issues[0]["state"] == "open"


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
