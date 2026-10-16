"""Actual Chromium QA on an installed wheel with synthetic private authority."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash

root = Path(sys.argv[1]).resolve()
root.mkdir(mode=0o700)
os.environ.update(
    STRATHMARK_TEST_DB="1",
    STRATHMARK_DB_PATH=str(root / "v2.sqlite3"),
    STRATHMARK_V3_DB_PATH=str(root / "v3.sqlite3"),
)
from strathmark.events import EventOperations, Principal
from strathmark.events.examples import synthetic_event

private = root / "private"
private.mkdir(mode=0o700)
ops = EventOperations(private / "events.sqlite3")
actor = Principal("synthetic-director", frozenset({"synthetic:missoula"}), "director")
state = ops.create(actor, "create-browser", synthetic_event(), "cli")
auth = private / "accounts.json"
auth.write_text(
    json.dumps(
        {
            "synthetic-director": {
                "password_hash": generate_password_hash("SyntheticBrowserOnly123"),
                "shows": ["synthetic:missoula"],
                "role": "director",
            },
            "synthetic-outsider": {
                "password_hash": generate_password_hash("SyntheticBrowserOnly123"),
                "shows": ["synthetic:other"],
                "role": "judge",
            },
        }
    )
)
auth.chmod(0o600)
key = private / "session.key"
key.write_text("synthetic-browser-session-key-only-1234567890")
key.chmod(0o600)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
url = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [
        sys.executable,
        "-I",
        "-m",
        "strathmark",
        "workspace",
        "--database",
        str(ops.store.path),
        "--auth-file",
        str(auth),
        "--session-key-file",
        str(key),
        "--port",
        str(port),
    ],
    stdout=(private / "server.log").open("w"),
    stderr=subprocess.STDOUT,
)
report = {
    "browser": "system Chromium via Playwright",
    "data": "synthetic",
    "official_use": False,
    "checks": [],
}


def check(name, value):
    report["checks"].append({"name": name, "passed": bool(value)})
    assert value, name


try:
    deadline = time.monotonic() + 20
    while True:
        try:
            import urllib.request

            urllib.request.urlopen(url + "/login", timeout=1).close()
            break
        except OSError:
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Owned workspace did not start")
            time.sleep(0.05)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/login")
        page.get_by_label("Username").fill("synthetic-director")
        page.get_by_label("Password").fill("SyntheticBrowserOnly123")
        page.get_by_label("Password").press("Tab")
        page.keyboard.press("Enter")
        page.wait_for_url(url + "/")
        page.get_by_role("link", name=state["event_id"], exact=True).click()
        check("CLI ownership is visibly read-only", "Hand over deliberately" in page.content())
        state = ops.execute(
            actor,
            state["event_id"],
            "browser-handover",
            state["revision"],
            "cli",
            "handover",
            {"editor": "gui"},
        )
        page.reload()
        page.get_by_text("Assign the exact next fields and physical stands", exact=True).click()
        page.get_by_role("button", name="Save complete next fields", exact=True).click()
        check(
            "native field/stand form accepted",
            len(ops.read(actor, state["event_id"])["rounds"][0]["fields"]) == 2,
        )
        page.get_by_role("button", name="Calculate exact field", exact=True).first.click()
        page.get_by_role("button", name="Review whole sheet", exact=True).first.click()
        page.get_by_label("Confirm issue of these reviewed marks").check()
        page.get_by_role("button", name="Issue", exact=True).first.click()
        state = ops.read(actor, state["event_id"])
        heat = state["rounds"][0]["fields"][0]
        check(
            "real engine issued sheet",
            bool(heat["issue_id"]) and all(r["model_version"] for r in heat["sheet"]["rows"]),
        )
        for i, entry in enumerate(heat["entries"]):
            page.get_by_label(entry + " timer 1", exact=True).fill(str(20 + i))
            page.get_by_label(entry + " timer 2", exact=True).fill(str(20.2 + i))
        page.get_by_role("button", name="Record complete outcomes", exact=True).first.click()
        page.get_by_role("button", name="Accept result revision", exact=True).first.click()
        check(
            "accepted dual-timer results shown with both clocks",
            "Common-count completion (s)" in page.content(),
        )
        stale = context.new_page()
        stale.goto(page.url)
        page.get_by_role("button", name="Prepare seeding forecasts", exact=True).click()
        stale.get_by_role("button", name="Prepare seeding forecasts", exact=True).click()
        check(
            "stale browser edit refused",
            "stale_event_revision" in stale.get_by_role("alert").inner_text(),
        )
        stale.close()
        page.screenshot(path=str(root / "workspace-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        check(
            "narrow viewport contains tables without page overflow",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
        )
        check(
            "mobile result columns remain readable and scroll within tables",
            page.locator("table").first.evaluate(
                'table => table.scrollWidth > table.clientWidth && table.querySelector("th").clientHeight < 60'
            ),
        )
        page.screenshot(path=str(root / "workspace-mobile.png"), full_page=True)
        printed = context.new_page()
        printed.goto(
            url
            + page.get_by_role(
                "link", name="Independent printable start sheet"
            ).first.get_attribute("href")
        )
        printed.emulate_media(media="print")
        check(
            "independent issued print sheet retains identity",
            heat["issue_id"] in printed.content()
            and "start mark + raw cutting duration" in printed.content(),
        )
        printed.screenshot(path=str(root / "issued-sheet.png"), full_page=True)
        (root / "issued-sheet.html").write_text(printed.content())
        response = page.request.post(page.url, data={"action": "forecast"})
        check("normal browser CSRF remains enforced", response.status == 400)
        outsider = browser.new_context()
        other = outsider.new_page()
        other.goto(url + "/login")
        other.get_by_label("Username").fill("synthetic-outsider")
        other.get_by_label("Password").fill("SyntheticBrowserOnly123")
        other.get_by_role("button", name="Sign in", exact=True).click()
        denied = other.goto(url + "/events/" + state["event_id"])
        check("authenticated wrong-show access refused", denied.status == 403)
        check("no browser script errors", not errors)
        report["chromium_version"] = browser.version
        browser.close()
    report["status"] = "PASS"
finally:
    server.terminate()
    server.wait(timeout=15)
    auth.unlink(missing_ok=True)
    key.unlink(missing_ok=True)
    (root / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
