#!/usr/bin/env python3
"""Operator-only real-import rendering acceptance, complementary to the eight checks.

Pipe the protected prepare-android-render.py input.json directly to stdin. Never
print it, pass credentials in argv, or include it in evidence. Requires a NEW
output directory and a clean disposable Android 36 emulator-5580 with APK
0.3.0-internal; API health remains 0.2.0-internal. No deployment or upstream
access is performed here. Only synthetic inbox screenshots and sanitized results
are retained. Dispose of the emulator afterwards; this does not clear profiles.
"""
import json
import os
from pathlib import Path
import re
import secrets
import select
import stat
import sys
import time

import deployed_acceptance as base

INPUT_LIMIT = 8192
CHECKS = ("backend_ready_020", "fixture_list_detail", "markdown_row", "plain_literal_row")
require = base.require


def keys(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "response_shape")


def credential(value):
    require(type(value) is str and 0 < len(value) <= 4096
            and all(33 <= ord(c) <= 126 for c in value), "response_shape")
    return value


def validate_document(doc):
    keys(doc, "schema api_origin admin_password run_id fixture messages")
    require(type(doc["schema"]) is int and doc["schema"] == 1, "response_shape")
    base.validate_origin(doc["api_origin"])
    password = doc["admin_password"]
    require(type(password) is str and 12 <= len(password.encode("utf-8")) <= 72
            and not any(ord(c) < 32 or ord(c) == 127 for c in password), "password_input")
    run = doc["run_id"]
    require(type(run) is str and re.fullmatch(r"[0-9a-f]{16}", run), "response_shape")
    fixture = doc["fixture"]
    keys(fixture, "topic_id topic_name source_key application_id original_count")
    for key in ("topic_id", "application_id"):
        base.positive_id(fixture[key])
    source = fixture["source_key"]
    require(type(source) is str and re.fullmatch(r"fixture-[a-zA-Z0-9-]{1,80}", source)
            and fixture["topic_name"] == "Import fixture " + source[8:], "identity_mismatch")
    require(type(fixture["original_count"]) is int and fixture["original_count"] in (205, 222),
            "message_mismatch")
    require(type(doc["messages"]) is list and len(doc["messages"]) == 2, "response_shape")
    for kind, message in zip(("markdown", "plain"), doc["messages"]):
        keys(message, "id gotify_id topic_id title message priority content_type")
        for key in ("id", "gotify_id", "topic_id"):
            base.positive_id(message[key])
        require(message["topic_id"] == fixture["topic_id"]
                and message["title"] == "Render-" + run + "-" + kind
                and message["message"] == "**Render " + run + "**"
                and message["content_type"] == "text/" + kind
                and type(message["priority"]) is int and message["priority"] == 9,
                "message_mismatch")
    for key in ("id", "gotify_id"):
        require(len({m[key] for m in doc["messages"]}) == 2, "message_mismatch")
    return doc


def read_document(stream):
    fd = stream.fileno()
    require(stat.S_ISFIFO(os.fstat(fd).st_mode), "stdin_pipe_required")
    deadline, raw = time.monotonic() + 10, b""
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0 and select.select([fd], [], [], remaining)[0], "stdin_timeout")
        chunk = os.read(fd, INPUT_LIMIT + 1 - len(raw))
        if not chunk:
            break
        raw += chunk
        require(len(raw) <= INPUT_LIMIT, "response_shape")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "response_shape")
            result[key] = value
        return result

    try:
        return validate_document(json.loads(raw, object_pairs_hook=unique))
    except (ValueError, UnicodeError, RecursionError):
        raise base.Failure("response_shape") from None


def row_nodes(nodes, partial_titles=()):
    require(base.Device.ready(nodes) and not any(
        n.get("class") == "android.widget.EditText" or n.get("password") == "true"
        or "\u767b\u5f55 zgotify" in n.get("text", "") for n in nodes), "ui_shape")
    rows = {}
    for node in nodes:
        groups = [[child for child in node if child.get("resource-id") == base.Device.package + ":id/" + name]
                  for name in ("message_title", "message_state", "message_text")]
        if not any(groups):
            continue
        require(len(groups[0]) == 1 and all(len(group) <= 1 for group in groups), "ui_shape")
        name = groups[0][0].get("text", "")
        require(name not in rows and all(child.get("package") == base.Device.package
                for group in groups for child in group), "ui_shape")
        # Only identified historical rows may have offscreen state/body children.
        require(name in partial_titles or all(len(group) == 1 for group in groups), "ui_shape")
        rows[name] = groups[2][0].get("text", "") if groups[2] else None
    return rows


def rendered_rows(nodes, messages, historical_titles=()):
    targets = {message["title"] for message in messages}
    rows = row_nodes(nodes, set(historical_titles) - targets)
    for message in messages:
        expected = message["message"][2:-2] if message["content_type"] == "text/markdown" else message["message"]
        require(rows.get(message["title"]) == expected, "message_mismatch")
    return rows


def validate_message(actual, expected):
    require(type(actual) is dict, "response_shape")
    for key in ("id", "topic_id"):
        base.positive_id(actual.get(key))
    require(type(actual.get("priority")) is int, "response_shape")
    for key in ("title", "message", "content_type", "created_at"):
        require(type(actual.get(key)) is str, "response_shape")
    require(all(actual.get(k) == v for k, v in expected.items() if k != "gotify_id"), "message_mismatch")
    base.require_state(actual.get("state"), False, False, False, 0)


class RenderHarness(base.Harness):
    def __init__(self, args):
        base.validate_origin(args.api)
        require(args.serial == "emulator-5580", "serial_refused")
        require(args.allow_isolated_test_mutations and args.input_stdin, "mutation_consent_required")
        self.output, self.output_ready = Path(args.output), False
        self.api = base.API(args.api)
        self.devices = [base.Device(args.adb, args.serial, self.output)]
        self.reverse_devices, self.accounts, self.sessions = [], [], []
        self.budget = base.LoginBudget()
        self.admin_token = ""
        self.root_id = self.created_id = self.topic_id = self.inbox_count = 0
        self.inbox_page_count = 0
        self.checks, self.message_ids, self.ambiguous_actions = [], [], []
        self.phase = "preflight"

    def preflight(self):
        super().preflight()
        raw = self.devices[0].adb("shell", "dumpsys", "package", base.Device.package)
        versions = re.findall(rb"\bversionName=([^\s]+)", raw)
        require(versions == [b"0.3.0-internal"], "backend_version")

    def login_api(self, username, password):
        # The base budget is retained; this acceptance is stricter: one API attempt per identity.
        require(self.budget.count(username) == 0, "login_budget")
        self.ambiguous_actions.append("login")
        result = super().login_api(username, password)
        credential(result["token"])
        self.ambiguous_actions.remove("login")
        return result

    def account_identity(self, account):
        if not account.token:
            result = self.login_api(account.username, account.password)
            account.token = result["token"]
        person = self.api.request("GET", "/v1/me", token=account.token).get("person")
        require(type(person) is dict and person.get("username") == account.username
                and person.get("admin") is False, "identity_mismatch")
        pid = base.positive_id(person.get("id"))
        require(pid != self.root_id and (not self.created_id or pid == self.created_id)
                and (not account.person_id or pid == account.person_id), "identity_mismatch")
        account.person_id = pid

    def setup(self, doc):
        validate_document(doc)
        self.phase = "setup"
        health = self.api.request("GET", "/healthz")
        require(health.get("status") == "ok" and health.get("version") == "0.2.0-internal", "backend_version")
        self.checks.append(CHECKS[0])
        result = self.login_api("admin", doc["admin_password"])
        self.admin_token = result["token"]
        person = self.api.request("GET", "/v1/me", token=self.admin_token).get("person")
        require(type(person) is dict and person.get("username") == "admin" and person.get("admin") is True,
                "identity_mismatch")
        self.root_id = base.positive_id(person.get("id"))
        self.topic_id = doc["fixture"]["topic_id"]
        account = base.Account("renderer", "render_" + secrets.token_hex(12), secrets.token_hex(24))
        self.accounts = [account]
        account.creation_started = True
        self.ambiguous_actions.append("create_account")
        result = self.api.request("POST", "/v1/admin/users", token=self.admin_token, body={
            "username": account.username, "password": account.password,
            "display_name": "Synthetic rendering acceptance", "admin": False})
        self.created_id = base.positive_id(result.get("id"))
        require(self.created_id != self.root_id, "identity_mismatch")
        self.ambiguous_actions.remove("create_account")
        self.account_identity(account)
        require(self.api.request("PUT", "/v1/admin/topics/%d/members/%d" % (self.topic_id, account.person_id),
                                 token=self.admin_token).get("ok") is True, "response_shape")
        topics = self.api.request("GET", "/v1/topics", token=account.token).get("topics")
        require(type(topics) is list and len(topics) == 1 and type(topics[0]) is dict
                and type(topics[0].get("id")) is int and topics[0].get("id") == self.topic_id
                and topics[0].get("name") == doc["fixture"]["topic_name"], "membership_mismatch")
        self.verify_messages(doc, account.token)
        self.checks.append(CHECKS[1])

    def verify_messages(self, doc, token):
        rows, before, first_page_count = [], 0, 0
        for page_index in range(10):
            page = self.api.request("GET", "/v1/messages?include_hidden=true&before=%d" % before,
                                    token=token).get("messages")
            require(type(page) is list and len(page) <= 100, "response_shape")
            if page_index == 0:
                first_page_count = len(page)
            for message in page:
                require(type(message) is dict, "response_shape")
                mid = base.positive_id(message.get("id"))
                require(not before or mid < before, "message_mismatch")
                before = mid
                rows.append(message)
            if len(page) < 100:
                break
        else:
            raise base.Failure("message_mismatch")
        count = doc["fixture"]["original_count"]
        require(len(rows) == count + 2, "message_mismatch")
        self.message_ids = [m["id"] for m in doc["messages"]]
        require({m["id"] for m in rows[:2]} == set(self.message_ids), "message_mismatch")
        expected = {m["title"]: m for m in doc["messages"]}
        for sequence in range(1, count + 1):
            title = "Import fixture %d" % sequence
            expected[title] = {"topic_id": self.topic_id, "title": title,
                "message": "# Import fixture %d\n\n**\u771f\u5b9e\u5bfc\u5165\u9a8c\u6536**\n\n\u6d88\u606f\u5e8f\u53f7: %d\n" % (sequence, sequence),
                "content_type": "text/markdown", "priority": 8 + sequence % 2}
        for row in rows:
            wanted = expected.pop(row.get("title"), None)
            require(wanted is not None, "message_mismatch")
            validate_message(row, wanted)
        require(not expected, "message_mismatch")
        for message in doc["messages"]:
            detail = self.api.request("GET", "/v1/messages/%d" % message["id"], token=token).get("message")
            validate_message(detail, message)
        self.inbox_count = len(rows)
        self.inbox_page_count = first_page_count

    def capture(self, doc):
        device = self.devices[0]
        require(device.authenticated, "identity_mismatch")
        device.active_identity(self.accounts[0])
        # Old fixture rows can share the viewport with the two new messages.
        # Every inbox body was checked as synthetic before opening the app.
        historical = {
            "Import fixture %d" % i for i in range(1, doc["fixture"]["original_count"] + 1)}
        rows = rendered_rows(device.nodes(), doc["messages"], historical)
        allowed = {m["title"] for m in doc["messages"]} | historical
        require(set(rows) <= allowed, "ui_shape")
        raw = device.adb("exec-out", "screencap", "-p")
        require(raw.startswith(b"\x89PNG\r\n\x1a\n"), "ui_shape")
        path = self.output / "gotify-render-emulator-5580.png"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)

    def workflow(self, doc):
        self.setup(doc)
        self.phase = "rendering"
        device = self.devices[0]
        self.reverse(device, True)
        device.restart()
        device.login(self.accounts[0], self.budget, expected_inbox_count=self.inbox_page_count)
        self.capture(doc)
        self.checks.extend(CHECKS[2:])

    def cleanup(self):
        failures = []

        def attempt(name, action):
            try:
                action()
            except BaseException:
                failures.append(name)

        def disable(account):
            # Never PATCH a create-response ID until /me verifies ownership.
            # An ambiguous create permits its FIRST login, never a repeated login.
            self.account_identity(account)
            require(self.api.request("PATCH", "/v1/admin/users/%d" % account.person_id,
                                     token=self.admin_token, body={"enabled": False}).get("ok") is True,
                    "response_shape")
            self.api.request("GET", "/v1/me", token=account.token, expected=(401,))

        for account in self.accounts:
            if account.creation_started:
                attempt("disable_account", lambda account=account: disable(account))
        tokens = list(dict.fromkeys(t for t in self.sessions if t != self.admin_token))
        if self.admin_token:
            tokens.append(self.admin_token)
        for token in tokens:
            attempt("revoke_session", lambda token=token: self.api.request(
                "DELETE", "/v1/session", token=token, expected=(200, 401)))
            attempt("verify_revoked_session", lambda token=token: self.api.request(
                "GET", "/v1/me", token=token, expected=(401,)))
        for device in self.reverse_devices:
            attempt("remove_reverse", device.remove_reverse)
        return failures

    def execute(self, doc):
        error = None
        try:
            validate_document(doc)
            self.preflight()
            self.workflow(doc)
            require(self.checks == list(CHECKS), "incomplete_checks")
        except BaseException as caught:
            error = base.failure_code(caught)
        finally:
            try:
                failures = self.cleanup()
            except BaseException:
                failures = ["cleanup_internal_failure"]
        report = {"status": "FAIL" if error or failures else "PASS", "failure": error,
                  "phase": self.phase, "checks": self.checks, "cleanup_failures": failures,
                  "ambiguous_actions": self.ambiguous_actions,
                  "person_ids": [a.person_id for a in self.accounts if a.person_id],
                  "topic_id": self.topic_id, "message_ids": self.message_ids,
                  "api_version": "0.2.0-internal", "apk_version": "0.3.0-internal",
                  "scope": "Real-import synthetic rendering on one disposable emulator; no push or physical phone claim."}
        try:
            self.write_report(report)
        except BaseException:
            report["status"], report["failure"] = "FAIL", "evidence_write"
        print(json.dumps(report, sort_keys=True), flush=True)
        return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    try:
        parser = base.SafeParser(description=__doc__, allow_abbrev=False)
        for name in ("api", "adb", "serial", "output"):
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--allow-isolated-test-mutations", action="store_true", required=True)
        parser.add_argument("--input-stdin", action="store_true", required=True)
        harness = RenderHarness(parser.parse_args(argv))
        return harness.execute(read_document(sys.stdin))
    except (Exception, KeyboardInterrupt) as caught:
        print("FAIL " + base.failure_code(caught), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
