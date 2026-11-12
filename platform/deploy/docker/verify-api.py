#!/usr/bin/env python3
"""Exercise only the loopback internal deployment; never print credentials."""

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CheckFailed(Exception):
    pass


def require(value, message):
    if not value:
        raise CheckFailed(message)


class Checks:
    def __init__(self, base):
        parsed = urlsplit(base)
        require(base in ("http://127.0.0.1:18090", "http://localhost:18090"),
                "acceptance script requires the isolated loopback endpoint")
        require(parsed.username is None, "URL credentials are prohibited")
        self.base = base
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.passed = []

    def call(self, method, path, token=None, body=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=25)
        except HTTPError as error:
            response = error
        with response:
            require(response.status == expected,
                    "HTTP check failed: method={} expected={} actual={}".format(
                        method, expected, response.status))
            return json.loads(response.read())

    def passed_check(self, name):
        self.passed.append(name)
        print("PASS " + name, flush=True)

    def login(self, username, password, device):
        return self.call("POST", "/v1/login", body={
            "username": username, "password": password, "device_name": device,
        })["token"]

    def message(self, token, message_id, hidden=True):
        result = self.call("GET", "/v1/messages?include_hidden=" + str(hidden).lower(), token)
        found = [m for m in result["messages"] if m["id"] == message_id]
        require(len(found) == 1, "expected exactly one authorized message")
        return found[0]


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(value, file)


def exercise(checks, admin, state_file):
    require(not state_file.exists(), "existing acceptance state must be inspected, not overwritten")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + secrets.token_hex(3)
    people = {}
    for name in ("alice", "bob"):
        username = name + "_" + run_id.lower()
        password = secrets.token_hex(24)
        person_id = checks.call("POST", "/v1/admin/users", admin, {
            "username": username, "display_name": name.title(), "password": password,
        })["id"]
        people[name] = {"id": person_id, "username": username, "password": password}
    topic = checks.call("POST", "/v1/admin/topics", admin, {"name": "Acceptance " + run_id})
    for person in people.values():
        checks.call("PUT", "/v1/admin/topics/{}/members/{}".format(topic["id"], person["id"]), admin)
    alice = people["alice"]
    bob = people["bob"]
    a1 = checks.login(alice["username"], alice["password"], "api-test-phone-a")
    a2 = checks.login(alice["username"], alice["password"], "api-test-phone-b")
    b1 = checks.login(bob["username"], bob["password"], "api-test-phone-c")
    checks.passed_check("administrator-created identities and three independent sessions")

    payload = {"external_id": "http-test:" + run_id, "title": "\u5185\u6d4b\u544a\u8b66",
               "message": "CPU load is 5.2\n**Test notification**", "priority": 8}
    first = checks.call("POST", "/v1/ingest", topic["ingest_token"], payload)
    again = checks.call("POST", "/v1/ingest", topic["ingest_token"], payload)
    require(first["created"] and not again["created"] and first["id"] == again["id"],
            "message ingestion was not idempotent")
    checks.call("POST", "/v1/ingest", topic["ingest_token"], dict(payload, message="changed"), 409)
    message_id = first["id"]
    require(checks.message(a1, message_id)["title"] == payload["title"], "UTF-8 title corrupted")
    require(not checks.message(b1, message_id)["state"]["read"], "new Bob message already read")
    checks.passed_check("UTF-8 message ingestion, duplicate suppression and conflicting-payload 409")

    state_path = "/v1/messages/{}/state".format(message_id)
    state = checks.call("PATCH", state_path, a1, {
        "read": True, "hidden": True, "favorite": True, "expected_version": 0,
    })
    require(state["version"] == 1, "first state version incorrect")
    synced = checks.message(a2, message_id)["state"]
    require(synced == state, "Alice state did not synchronize to second session")
    require(not any(m["id"] == message_id for m in checks.call("GET", "/v1/messages", a2)["messages"]),
            "hidden message remains in default inbox")
    bob_state = checks.message(b1, message_id)["state"]
    require(not any(bob_state[k] for k in ("read", "hidden", "favorite")),
            "Alice mutation changed Bob state")
    checks.call("PATCH", state_path, a2, {"read": False, "expected_version": 0}, 409)
    checks.passed_check("cross-session state sync, personal isolation, hiding and stale-write 409")

    checks.call("PATCH", state_path, a1,
                {"read": False, "expected_version": 1, "actor_id": bob["id"]}, 400)
    checks.call("POST", "/v1/admin/users", b1, {}, 403)
    checks.call("GET", "/v1/messages", topic["ingest_token"], expected=401)
    checks.call("POST", "/v1/ingest", a1, payload, 401)
    checks.call("GET", "/v1/messages?token=" + a1, expected=401)
    checks.passed_check("actor spoofing, privilege escalation and wrong-token access rejected")

    own_events = checks.call("GET", "/v1/audit", a1)["events"]
    require(all(e["actor_id"] == alice["id"] for e in own_events), "audit leaked another person's events")
    state_events = [e for e in own_events if e["action"] == "update_message_state"]
    require(len(state_events) == 1 and state_events[0]["device_name"] == "api-test-phone-a",
            "audit attribution or failed-write handling incorrect")
    checks.passed_check("audit records authenticated person and excludes rejected mutations")

    membership = "/v1/admin/topics/{}/members/{}".format(topic["id"], bob["id"])
    checks.call("DELETE", membership, admin)
    require(checks.call("GET", "/v1/messages", b1)["messages"] == [], "revoked member can still read")
    checks.call("PATCH", state_path, b1, {"read": True, "expected_version": 0}, 404)
    checks.call("PUT", membership, admin)
    checks.passed_check("membership revocation blocks both reads and writes")

    checks.call("DELETE", "/v1/session", a1)
    checks.call("GET", "/v1/me", a1, expected=401)
    checks.call("GET", "/v1/me", a2)
    checks.passed_check("single-device logout preserves the other device")
    private_json(state_file, {"run_id": run_id, "people": people, "alice_token": a2,
                             "bob_token": b1, "message_id": message_id, "state": state,
                             "checks": checks.passed})
    checks.call("DELETE", "/v1/session", admin)
    print("Ready for container restart verification; protected state saved.", flush=True)


def verify_restart(checks, admin, state_file):
    fixture = json.loads(state_file.read_text())
    alice = fixture["people"]["alice"]
    bob = fixture["people"]["bob"]
    a2, b1 = fixture["alice_token"], fixture["bob_token"]
    checks.passed = fixture["checks"]
    checks.call("GET", "/v1/me", a2)
    checks.call("GET", "/v1/me", b1)
    require(checks.message(a2, fixture["message_id"])["state"] == fixture["state"],
            "personal state changed after container restart")
    require(not checks.message(b1, fixture["message_id"])["state"]["read"],
            "Bob state changed after restart")
    checks.passed_check("sessions, messages and personal state survive database/API restart")

    a3 = checks.login(alice["username"], alice["password"], "api-test-phone-d")
    new_password = secrets.token_hex(24)
    checks.call("POST", "/v1/password", a2,
                {"old_password": alice["password"], "new_password": new_password})
    for token in (a2, a3):
        checks.call("GET", "/v1/me", token, expected=401)
    checks.call("POST", "/v1/login", body={"username": alice["username"],
                "password": alice["password"], "device_name": "old-password-test"}, expected=401)
    new_session = checks.login(alice["username"], new_password, "new-password-test")
    checks.call("DELETE", "/v1/session", new_session)
    checks.passed_check("password change invalidates all old sessions and the old password")

    for person in (alice, bob):
        checks.call("PATCH", "/v1/admin/users/" + str(person["id"]), admin, {"enabled": False})
    checks.call("GET", "/v1/me", b1, expected=401)
    checks.passed_check("account disabling revokes sessions; generated test identities disabled")
    checks.call("DELETE", "/v1/session", admin)
    report = {"run_id": fixture["run_id"], "passed": checks.passed,
              "completed_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "real HTTP and MySQL; device labels simulate sessions, not physical phones",
              "push_verified": False}
    reports = Path("reports")
    reports.mkdir(mode=0o700, exist_ok=True)
    private_json(reports / ("api-" + fixture["run_id"] + ".json"), report)
    state_file.unlink()
    print("Completed {} checks; report contains no credentials.".format(len(checks.passed)), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("exercise", "verify-restart"))
    parser.add_argument("--base", default="http://127.0.0.1:18090")
    parser.add_argument("--admin-password-file", default="secrets/admin_password")
    parser.add_argument("--state-file", default="secrets/http_acceptance.json")
    args = parser.parse_args()
    checks = Checks(args.base)
    checks.call("GET", "/healthz")
    admin_password = Path(args.admin_password_file).read_text().rstrip("\r\n")
    admin = checks.login("admin", admin_password, "acceptance-controller")
    if args.phase == "exercise":
        exercise(checks, admin, Path(args.state_file))
    else:
        verify_restart(checks, admin, Path(args.state_file))


if __name__ == "__main__":
    try:
        main()
    except CheckFailed as error:
        print("FAIL " + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("FAIL unexpected " + type(error).__name__ + "; credentials and bodies suppressed", file=sys.stderr)
        sys.exit(1)
