"""Optional authenticated Flask workspace over exactly the application operations."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

from .contracts import EventError, Principal, digest


def private_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise EventError("private_configuration_file_required")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise EventError("private_configuration_requires_mode_600")
    return path


def create_workspace(ops, auth_file, session_key_file, *, origin=None):
    # GUI packages are deliberately imported only at this optional boundary.
    from flask import Flask, abort, redirect, render_template_string, request, session, url_for
    from flask_wtf.csrf import CSRFProtect
    from werkzeug.security import check_password_hash

    auth_file = private_file(auth_file)
    key_file = private_file(session_key_file)
    key = key_file.read_text().strip()
    if len(key) < 32:
        raise EventError("stable_session_key_required")
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=bool(origin and origin.startswith("https://")),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )
    CSRFProtect(app)

    def accounts():
        return json.loads(private_file(auth_file).read_text())

    def principal():
        username = session.get("operator")
        account = accounts().get(username)
        if not account or session.get("account_binding") != digest(account):
            session.clear()
            abort(401)
        return Principal(username, frozenset(account["shows"]), account["role"])

    @app.before_request
    def scope_transport():
        if origin and request.host_url.rstrip("/") != origin:
            abort(400)
        if (
            request.method == "POST"
            and request.headers.get("Origin")
            and request.headers["Origin"] != request.host_url.rstrip("/")
        ):
            abort(403)
        if request.endpoint not in {"login", "static"} and "operator" not in session:
            return redirect(url_for("login"))

    @app.after_request
    def secure_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Chromium gives native form POSTs Origin:null under no-referrer.
        # same-origin preserves strict origin validation without disclosing
        # referrers to another origin. Ordinary CSRF remains mandatory.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = request.form.get("username", "")
            account = accounts().get(username)
            # Maintained password verifier; no bearer/service token in browser.
            valid = (
                check_password_hash(account["password_hash"], request.form.get("password", ""))
                if account
                else False
            )
            if valid:
                session.clear()
                session.update(operator=username, account_binding=digest(account))
                return redirect(url_for("index"))
            error = "Sign-in failed."
        return render_template_string(_LOGIN, error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        user = principal()
        with ops.store.transaction() as db:
            events = [
                (event_id, show_id)
                for event_id, show_id in db.execute(
                    "SELECT event_id,show_id FROM events ORDER BY event_id"
                )
                if show_id in user.shows
            ]
        return render_template_string(_INDEX, events=events)

    def render_event(state, error=None, status=200):
        eligible = ops._eligible(state)
        ordinal = (
            len(state["rounds"])
            if state["rounds"] and state["rounds"][-1]["state"] == "advanced"
            else max(0, len(state["rounds"]) - 1)
        )
        rule = state["config"]["round_rules"][min(ordinal, len(state["config"]["round_rules"]) - 1)]
        roster = {r["entry_id"]: r for r in state["config"]["roster"]}
        return render_template_string(
            _EVENT,
            state=state,
            error=error,
            request_id=lambda: str(uuid4()),
            json=json,
            eligible=eligible,
            rule=rule,
            roster=roster,
        ), status

    @app.route("/events/<path:event_id>", methods=["GET", "POST"])
    def event(event_id):
        user = principal()
        try:
            state = ops.read(user, event_id)
        except EventError:
            abort(403)
        if request.method == "POST":
            try:
                action = request.form["action"]
                payload = json.loads(request.form.get("payload", "{}"))
                if action == "fields" and request.form.get("field_form"):
                    eligible = ops._eligible(state)
                    ordinal = (
                        len(state["rounds"])
                        if state["rounds"] and state["rounds"][-1]["state"] == "advanced"
                        else max(0, len(state["rounds"]) - 1)
                    )
                    rule = state["config"]["round_rules"][
                        min(ordinal, len(state["config"]["round_rules"]) - 1)
                    ]
                    grouped = {}
                    for entry_id in eligible:
                        heat_number = int(request.form["heat_" + entry_id])
                        if not 1 <= heat_number <= 512:
                            raise EventError("invalid_heat_number")
                        heat_id = rule["round_id"] + ":heat:" + str(heat_number)
                        field = grouped.setdefault(
                            heat_id, {"heat_id": heat_id, "entries": [], "stands": []}
                        )
                        field["entries"].append(entry_id)
                        field["stands"].append(request.form["stand_" + entry_id])
                    payload = {"fields": list(grouped.values())}
                elif action == "advance" and request.form.get("advance_form"):
                    payload = {
                        "tie_selections": {
                            heat["heat_id"]: request.form.getlist("qualify_" + heat["heat_id"])
                            for heat in state["rounds"][-1]["fields"]
                            if request.form.getlist("qualify_" + heat["heat_id"])
                        },
                        "reason": request.form.get("reason", ""),
                    }
                elif action in {"review", "issue"}:
                    payload = {
                        "heat_id": request.form["heat_id"],
                        "sheet_digest": request.form["sheet_digest"],
                    }
                    if action == "issue":
                        payload["confirm"] = request.form.get("confirm") == "yes"
                elif action in {"outcomes", "correct"}:
                    _, heat = ops._heat(state, request.form["heat_id"])
                    payload = {
                        "heat_id": heat["heat_id"],
                        "issue_id": heat["issue_id"],
                        "outcomes": [],
                    }
                    if request.form.get("allow_partial_timers") == "yes":
                        payload["allow_partial_timers"] = True
                    for entry_id in heat["entries"]:
                        row = {
                            "entry_id": entry_id,
                            "status": request.form.get("status_" + entry_id, "finished"),
                        }
                        for timer in ("timer1_seconds", "timer2_seconds"):
                            value = request.form.get(timer + "_" + entry_id)
                            if value:
                                row[timer] = value
                        payload["outcomes"].append(row)
                    if action == "correct":
                        payload.update(
                            reason=request.form.get("reason"),
                            supersedes=request.form.get("supersedes"),
                        )
                elif action == "accept":
                    payload = {
                        "heat_id": request.form["heat_id"],
                        "result_digest": request.form["result_digest"],
                        "ack_partial_timers": request.form.get("ack_partial_timers") == "yes",
                    }
                ops.execute(
                    user,
                    event_id,
                    request.form["request_id"],
                    int(request.form["expected_revision"]),
                    request.form["editor"],
                    action,
                    payload,
                )
                return redirect(url_for("event", event_id=event_id), code=303)
            except (EventError, ValueError, KeyError, TypeError) as exc:
                return render_event(
                    ops.read(user, event_id), str(exc), 409 if isinstance(exc, EventError) else 400
                )
        return render_event(state)

    @app.get("/sheet/<path:event_id>/<heat_id>")
    def sheet(event_id, heat_id):
        try:
            state = ops.read(principal(), event_id)
            _, heat = ops._heat(state, heat_id)
            if not heat.get("issue_id"):
                raise EventError("issued_sheet_required")
        except EventError:
            abort(403)
        roster = {r["entry_id"]: r for r in state["config"]["roster"]}
        return render_template_string(_SHEET, state=state, heat=heat, roster=roster)

    @app.get("/export/<path:event_id>")
    def export(event_id):
        try:
            payload = ops.export(principal(), event_id)
        except EventError:
            abort(403)
        return app.response_class(
            json.dumps(payload, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=managed-event.json"},
        )

    return app


def add_account(auth_file, actor, show):
    import getpass

    from werkzeug.security import generate_password_hash

    from .store import atomic_json

    path = Path(auth_file)
    registry = json.loads(private_file(path).read_text()) if path.exists() else {}
    if actor in registry:
        raise EventError("account_exists: use an explicit reviewed credential rotation")
    password = getpass.getpass("New workspace password: ")
    if len(password) < 12 or getpass.getpass("Repeat password: ") != password:
        raise EventError("passwords_must_match_and_have_at_least_12_characters")
    registry[actor] = {
        "password_hash": generate_password_hash(password),
        "shows": [show],
        "role": "director",
    }
    atomic_json(path, registry)


def new_session_key(path):
    path = Path(path)
    with path.open("x") as output:
        if os.name != "nt":
            os.fchmod(output.fileno(), 0o600)
        output.write(secrets.token_hex(32))


_BASE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>STRATHMARK workspace</title>
<style>*{box-sizing:border-box}h1{overflow-wrap:anywhere;font-size:clamp(1.4rem,4vw,2rem)}section,form{min-width:0}fieldset{min-inline-size:0;max-width:100%}table{display:block;overflow-x:auto;max-width:100%}td input{width:8rem}td select{max-width:9rem}body{overflow-wrap:anywhere;font:17px system-ui;max-width:1080px;margin:2rem auto;padding:0 1rem;color:#18252b;background:#f5f4ef}input,select,button,textarea{font:inherit;padding:.6rem;margin:.2rem;max-width:95%}button{cursor:pointer}table{border-collapse:collapse;width:100%;background:white}td,th{white-space:nowrap;padding:.5rem;border-bottom:1px solid #bbc4c4;text-align:left}section{background:white;padding:1rem;margin:1rem 0;border:1px solid #bbc4c4}textarea{width:95%;min-height:7rem}.error{color:#9b1b1b}code{overflow-wrap:anywhere}@media print{table{display:table;overflow:visible}button,form,nav{display:none}body{background:white;margin:0}}:focus-visible{outline:3px solid #15608b}</style>
"""
_LOGIN = (
    _BASE
    + """<h1>STRATHMARK workspace</h1><p>Sign in to your scoped operator account.</p>{% if error %}<p role="alert" class="error">{{error}}</p>{% endif %}<form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label>Username <input name="username" autocomplete="username" required></label><label>Password <input name="password" type="password" autocomplete="current-password" required></label><button>Sign in</button></form></html>"""
)
_INDEX = (
    _BASE
    + """<h1>Event workspace</h1><p>Synthetic managed-event v1. Official use is disabled.</p><ul>{% for id,show in events %}<li><a href="{{url_for('event',event_id=id)}}">{{id}}</a> — {{show}}</li>{% endfor %}</ul><p>Create an event using the supplied-data CLI or your host application.</p></html>"""
)
_EVENT = (
    _BASE
    + """
{% macro hidden(action) %}<input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="expected_revision" value="{{state.revision}}"><input type="hidden" name="request_id" value="{{request_id()}}"><input type="hidden" name="editor" value="gui"><input type="hidden" name="action" value="{{action}}">{% endmacro %}
<nav><a href="/">Events</a> · <a href="{{url_for('export',event_id=state.event_id)}}">Download event and synchronization record</a></nav>
<h1>{{state.event_id}}</h1><p>Revision {{state.revision}} · {{state.status}} · editor {{state.editor}}</p><p>Synthetic only · {{state.config.engine}} / {{state.config.mark_policy}} · {{state.config.discipline}} · {{state.config.wood.diameter_mm}} mm {{state.config.wood.species}} · quality {{state.config.wood.quality if state.config.wood.quality is not none else 'unknown'}} (V2 ignores quality)</p>
{% if error %}<p role="alert" class="error">{{error}}</p>{% endif %}
{% if state.editor != 'gui' %}<p>Current editor is {{state.editor}}. Hand over deliberately from that interface using <code>event handover</code>, payload <code>{"editor":"gui"}</code>. Read-only views remain available.</p>{% else %}
<section><h2>Event controls</h2><form method="post">{{hidden('forecast')}}<button>Prepare seeding forecasts</button></form>
<details><summary>Assign the exact next fields and physical stands</summary><form method="post">{{hidden('fields')}}<input type="hidden" name="field_form" value="yes"><p>{{rule.round_id}} · maximum {{rule.capacity}} entrants per heat. Every eligible entry must be placed once. Shared gear must be separated.</p><table><tr><th>Competitor / entry</th><th>Heat number</th><th>Physical stand</th></tr>{% for entry in eligible %}<tr><td>{{roster[entry].display_name}} ({{entry}})</td><td><input type="number" name="heat_{{entry}}" value="{{loop.index0 // rule.capacity + 1}}" min="1" max="512" required aria-label="{{entry}} heat"></td><td><select name="stand_{{entry}}" aria-label="{{entry}} stand">{% for stand in state.config.stands %}<option {% if loop.index0 == (eligible.index(entry) % rule.capacity) %}selected{% endif %}>{{stand}}</option>{% endfor %}</select></td></tr>{% endfor %}</table><button>Save complete next fields</button></form></details>
<form method="post">{{hidden('advance')}}<input type="hidden" name="advance_form" value="yes">{% if state.rounds %}{% for heat in state.rounds[-1].fields %}{% if heat.result_revisions and heat.result_revisions[-1].accepted %}<fieldset><legend>{{heat.heat_id}} advancement tie decision (select only when required)</legend>{% for row in heat.result_revisions[-1].outcomes if row.status == 'finished' %}<label><input type="checkbox" name="qualify_{{heat.heat_id}}" value="{{row.entry_id}}">{{row.entry_id}} · position {{row.official_position}}</label>{% endfor %}</fieldset>{% endif %}{% endfor %}{% endif %}<label>Tie decision reason <input name="reason"></label><button>Advance accepted round</button></form><form method="post">{{hidden('finalize')}}<button>Finalize accepted final</button></form><form method="post">{{hidden('handover')}}<input type="hidden" name="payload" value='{"editor":"cli"}'><button>Hand over to CLI</button></form></section>{% endif %}
{% for round in state.rounds %}<section id="{{round.round_id}}"><h2>{{round.round_id}} · {{round.state}}</h2>{% for heat in round.fields %}<h3 id="{{heat.heat_id}}">{{heat.heat_id}} · {{heat.state}}</h3><p>Stands: {{heat.stands|join(', ')}} · Field <code>{{heat.field_digest}}</code></p>
{% if heat.sheet %}<table><thead><tr><th>Entry</th><th>Start count (s)</th><th>Predicted raw duration (s)</th></tr></thead><tbody>{% for row in heat.sheet.rows|sort(attribute='start_mark_seconds') %}<tr><td>{{row.entry_id}}</td><td>{{row.start_mark_seconds}}</td><td>{{row.predicted_raw_seconds|float|round(3)}}{% if row.degraded %} · degraded{% endif %}{% if row.warnings %} · {{row.warnings|join("; ")}}{% endif %}</td></tr>{% endfor %}</tbody></table>{% endif %}
{% if heat.issue_id %}<p>Issued <code>{{heat.issue_id}}</code> · <a href="{{url_for('sheet',event_id=state.event_id,heat_id=heat.heat_id)}}">Independent printable start sheet</a></p>{% endif %}
{% if state.editor == 'gui' %}{% if not heat.issue_id %}<form method="post">{{hidden('calculate')}}<input type="hidden" name="payload" value='{{json.dumps({"heat_id":heat.heat_id})}}'><button>Calculate exact field</button></form>{% if heat.sheet %}<form method="post">{{hidden('review')}}<input type="hidden" name="heat_id" value="{{heat.heat_id}}"><input type="hidden" name="sheet_digest" value="{{heat.sheet.digest}}"><button>Review whole sheet</button></form><form method="post">{{hidden('issue')}}<input type="hidden" name="heat_id" value="{{heat.heat_id}}"><input type="hidden" name="sheet_digest" value="{{heat.sheet.digest}}"><label><input type="checkbox" name="confirm" value="yes" required>Confirm issue of these reviewed marks</label><button>Issue</button></form>{% endif %}
{% else %}<form method="post">{{hidden('correct' if heat.result_revisions else 'outcomes')}}<input type="hidden" name="heat_id" value="{{heat.heat_id}}">
{% if heat.result_revisions %}<input type="hidden" name="supersedes" value="{{heat.result_revisions[-1].digest}}"><label>Correction reason <input name="reason" required></label>{% endif %}<table><tr><th>Entry</th><th>Status</th><th>Timer 1 raw seconds</th><th>Timer 2 raw seconds</th></tr>{% for entry in heat.entries %}<tr><td>{{entry}}</td><td><select name="status_{{entry}}">{% for status in ['finished','dnf','dq','dns','scratched','void'] %}<option>{{status}}</option>{% endfor %}</select></td><td><input name="timer1_seconds_{{entry}}" inputmode="decimal" aria-label="{{entry}} timer 1"></td><td><input name="timer2_seconds_{{entry}}" inputmode="decimal" aria-label="{{entry}} timer 2"></td></tr>{% endfor %}</table><label><input type="checkbox" name="allow_partial_timers" value="yes">Capture incomplete dual timers for explicit judge review</label><button>Record complete {{'correction' if heat.result_revisions else 'outcomes'}}</button></form>
{% if heat.result_revisions and not heat.result_revisions[-1].accepted %}<form method="post">{{hidden('accept')}}<input type="hidden" name="heat_id" value="{{heat.heat_id}}"><input type="hidden" name="result_digest" value="{{heat.result_revisions[-1].digest}}"><label><input type="checkbox" name="ack_partial_timers" value="yes">Judge acknowledges any partial timers</label><button>Accept result revision</button></form>{% endif %}{% endif %}{% endif %}
{% if heat.result_revisions %}<p>Result revision {{heat.result_revisions[-1].source_revision}} · accepted {{heat.result_revisions[-1].accepted}}</p><table><tr><th>Entry</th><th>Status</th><th>Raw duration (s)</th><th>Start count (s)</th><th>Common-count completion (s)</th><th>Position</th></tr>{% for row in heat.result_revisions[-1].outcomes %}<tr><td>{{row.entry_id}}</td><td>{{row.status}}</td><td>{{row.raw_duration_seconds}}</td><td>{{row.start_mark_seconds}}</td><td>{{row.common_count_seconds}}</td><td>{{row.official_position}}</td></tr>{% endfor %}</table>{% endif %}{% endfor %}</section>{% endfor %}
<section><h2>Synchronization</h2>{% for delivery in state.exports %}<p>{{delivery.payload.source_result_id}} r{{delivery.payload.source_revision}} · {{delivery.state}} · publisher {{delivery.publisher}}</p>{% else %}<p>No accepted result delivery yet.</p>{% endfor %}</section></html>"""
)
_SHEET = (
    _BASE
    + """<h1>Synthetic start sheet — {{heat.heat_id}}</h1><p>Official-use activation disabled.</p><p>{{state.event_id}} · {{state.config.discipline}} · {{state.config.wood.diameter_mm}} mm {{state.config.wood.species}}</p><p>Issue <code>{{heat.issue_id}}</code><br>Field <code>{{heat.field_digest}}</code><br>Sheet <code>{{heat.sheet.digest}}</code></p><table><tr><th>Entry</th><th>Name</th><th>Physical stand</th><th>Start count seconds</th></tr>{% for row in heat.sheet.rows|sort(attribute='start_mark_seconds') %}<tr><td>{{row.entry_id}}</td><td>{{roster[row.entry_id].display_name}}</td><td>{{heat.stands[heat.entries.index(row.entry_id)]}}</td><td>{{row.start_mark_seconds}}</td></tr>{% endfor %}</table><p>Record raw timer readings from each competitor's legal start. Common-count completion = start mark + raw cutting duration. Retain this sheet independently before running the heat. During an outage record all outcomes against these exact issue and field references; reconcile into the original authority after recovery. Unissued fields pause.</p></html>"""
)
