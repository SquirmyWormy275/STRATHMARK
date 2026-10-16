"""Optional UI parity, normal CSRF, scoped sessions and issued printing."""

import json
import re

import pytest

pytest.importorskip("flask_wtf")
from werkzeug.security import generate_password_hash

from strathmark.events import EventOperations, Principal
from strathmark.events.examples import synthetic_event
from strathmark.events.workspace import create_workspace


def csrf(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.mark.parametrize("heat_id", ["q0", "q/zero"])
def test_scoped_browser_parity_csrf_stale_editor_and_print(tmp_path, heat_id):
    ops = EventOperations(tmp_path / "authority.sqlite3")
    actor = Principal("director", frozenset({"synthetic:missoula"}), "director")
    state = ops.create(actor, "create", synthetic_event(), "gui")
    auth = tmp_path / "operators.json"
    auth.write_text(
        json.dumps(
            {
                "director": {
                    "password_hash": generate_password_hash("synthetic-test-password"),
                    "shows": ["synthetic:missoula"],
                    "role": "director",
                },
                "outsider": {
                    "password_hash": generate_password_hash("synthetic-test-password"),
                    "shows": ["synthetic:other"],
                    "role": "judge",
                },
            }
        )
    )
    auth.chmod(0o600)
    key = tmp_path / "session.key"
    key.write_text("synthetic-test-only-session-key-1234567890")
    key.chmod(0o600)
    app = create_workspace(ops, auth, key)
    app.config["TESTING"] = True
    client = app.test_client()
    path = "/events/" + state["event_id"]
    assert client.get(path).status_code == 302
    assert (
        client.post(
            "/login", data={"username": "director", "password": "synthetic-test-password"}
        ).status_code
        == 400
    )
    token = csrf(client.get("/login"))
    assert (
        client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "director",
                "password": "synthetic-test-password",
            },
        ).status_code
        == 302
    )
    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["Referrer-Policy"] == "same-origin"
    assert "quality unknown" in page.text
    token = csrf(page)
    assert client.post(path, data={"action": "forecast"}).status_code == 400

    def post(action, payload, *, extra=None, revision=None):
        nonlocal state
        form = {
            "csrf_token": token,
            "action": action,
            "expected_revision": revision or state["revision"],
            "editor": "gui",
            "request_id": f"browser-{state['revision']}-{action}",
            "payload": json.dumps(payload),
        }
        form.update(extra or {})
        response = client.post(path, data=form)
        state = ops.read(actor, state["event_id"])
        return response

    fields = [
        {
            "heat_id": heat_id if n == 0 else "q1",
            "entries": [f"e{i}" for i in range(n * 4, n * 4 + 4)],
            "stands": ["1", "2", "3", "4"],
        }
        for n in range(2)
    ]
    assert post("fields", {"fields": fields}).status_code == 303
    from flask import url_for

    with app.test_request_context():
        sheet_path = url_for("sheet", event_id=state["event_id"], heat_id=heat_id)
    assert client.get(sheet_path).status_code == 403
    assert post("calculate", {"heat_id": heat_id}).status_code == 303
    sheet = state["rounds"][0]["fields"][0]["sheet"]
    assert (
        post("review", {}, extra={"heat_id": heat_id, "sheet_digest": sheet["digest"]}).status_code
        == 303
    )
    assert (
        post("issue", {}, extra={"heat_id": heat_id, "sheet_digest": sheet["digest"]}).status_code
        == 409
    )
    assert (
        post(
            "issue",
            {},
            extra={"heat_id": heat_id, "sheet_digest": sheet["digest"], "confirm": "yes"},
        ).status_code
        == 303
    )
    printed = client.get(sheet_path)
    assert printed.status_code == 200
    assert state["rounds"][0]["fields"][0]["issue_id"] in printed.text
    assert "start mark + raw cutting duration" in printed.text
    assert post("forecast", {}, revision=1).status_code == 409
    export = client.get("/export/" + state["event_id"])
    assert export.json == ops.export(actor, state["event_id"])
    assert not any(
        value in export.text
        for value in ("synthetic-test-password", "password_hash", "session.key")
    )
    # Account scope is authoritative on each request, including after login.
    registry = json.loads(auth.read_text())
    registry["director"]["shows"] = ["synthetic:other"]
    auth.write_text(json.dumps(registry))
    assert client.get(path).status_code == 401


def test_director_retention_form_keeps_acceptance_separate(tmp_path):
    from tests.test_managed_events import command, corrected_issued_progression

    ops = EventOperations(tmp_path / "authority.sqlite3")
    state = corrected_issued_progression(ops, retain=False)
    state = command(ops, state, "handover", {"editor": "gui"})
    auth = tmp_path / "operators.json"
    auth.write_text(
        json.dumps(
            {
                name: {
                    "password_hash": generate_password_hash("synthetic-password"),
                    "shows": ["synthetic:missoula"],
                    "role": role,
                }
                for name, role in [("official", "judge"), ("director", "director")]
            }
        )
    )
    auth.chmod(0o600)
    key = tmp_path / "session.key"
    key.write_text("synthetic-private-session-key-1234567890")
    key.chmod(0o600)
    app = create_workspace(ops, auth, key)
    client = app.test_client()
    path = "/events/" + state["event_id"]
    for username, expected in [("official", 409), ("director", 303)]:
        token = csrf(client.get("/login"))
        assert (
            client.post(
                "/login",
                data={"csrf_token": token, "username": username, "password": "synthetic-password"},
            ).status_code
            == 302
        )
        page = client.get(path)
        token = csrf(page)
        assert "Director retention reason" in page.text
        response = client.post(
            path,
            data={
                "csrf_token": token,
                "action": "retain_downstream",
                "request_id": "retention-" + username,
                "expected_revision": state["revision"],
                "editor": "gui",
                "reason": "Director's synthetic appeal decision",
            },
        )
        assert response.status_code == expected
    actor = Principal("director", frozenset({"synthetic:missoula"}), "director")
    state = ops.read(actor, state["event_id"])
    assert state["progression_decisions"][-1]["actor_id"] == "director"
    page = client.get(path)
    assert "Director&#39;s synthetic appeal decision" in page.text
    response = client.post(
        path,
        data={
            "csrf_token": csrf(page),
            "action": "finalize",
            "request_id": "premature-finalize",
            "expected_revision": state["revision"],
            "editor": "gui",
        },
    )
    assert response.status_code == 409 and "all_summary_results_must_be_accepted" in response.text
