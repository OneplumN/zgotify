#!/usr/bin/env python3
"""Bounded UI acceptance against an operator-deployed, isolated API/MySQL backend.

Requires the internal debug APK on disposable Android 36 emulators 5580/5582.
The operator supplies the deployment provenance and a loopback tunnel; this script
neither deploys nor starts an HTTP fixture. Pipe the API admin password on stdin, never
in argv or an environment variable. Every invocation needs a NEW output directory.
No retries of login or creation requests. Login limits include both API and UI.

Only synthetic IDs/check codes and post-login screenshots are evidence. Raw API,
adb, UI XML, credentials and exception text are never written to host files/logs.
The app itself persists its normal encrypted profiles on the disposable emulators;
these are NOT cleared. Dispose of the emulators after evidence collection. Topics
and messages remain on the isolated backend (no deletion API); accounts are disabled.
This is not vendor push, physical-phone, or upstream Gotify full-chain acceptance.
"""
import argparse
import http.client
import json
import os
import re
import secrets
import select
import shlex
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


ORIGIN = "http://127.0.0.1:18090"
CHECKS = (
    "backend_ready_02", "alice_same_person_two_devices", "alice_read_sync_b",
    "bob_personal_state_isolation", "add_account_cancel_preserves_bob",
    "offline_favorite_durable_replayed_once", "revoked_membership_cache_purged",
    "disabled_old_sessions_rejected",
)
REASONS = frozenset((
    "arguments", "origin_refused", "serial_refused", "mutation_consent_required",
    "stdin_pipe_required", "stdin_timeout", "password_input", "output_not_new",
    "adb_failed", "emulator_required", "sdk_required", "reverse_exists",
    "profile_unreadable", "existing_profiles", "http_transport", "http_status",
    "response_shape", "login_budget", "identity_mismatch", "ui_timeout",
    "ui_shape", "state_mismatch", "backend_version", "message_mismatch",
    "membership_mismatch", "cancel_changed_profiles", "incomplete_checks",
    "internal_failure", "interrupted", "cleanup_failed", "evidence_write",
))


class Failure(Exception):
    def __init__(self, code):
        self.code = code if code in REASONS else "internal_failure"
        super().__init__(self.code)


def failure_code(error):
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "interrupted"
    return error.code if isinstance(error, Failure) else "internal_failure"


def require(condition, code):
    if not condition:
        raise Failure(code)


def validate_origin(value):
    # Exact equality also rejects credentials, suffixes, whitespace and redirects.
    require(value == ORIGIN, "origin_refused")
    return value


def validate_serials(a, b):
    require((a, b) == ("emulator-5580", "emulator-5582"), "serial_refused")


def positive_id(value):
    require(type(value) is int and 0 < value < 2**64, "response_shape")
    return value


def profile_store(raw):
    try:
        require(len(raw) <= 65536, "profile_unreadable")
        root = ET.fromstring(raw)
        require(root.tag == "map", "profile_unreadable")
        values = {}
        for node in root:
            name = node.get("name")
            require(name in ("profiles", "active") and name not in values
                    and node.tag == "string", "profile_unreadable")
            values[name] = node.text or ""
        profiles = json.loads(values.get("profiles", "[]"))
        require(type(profiles) is list and all(type(p) is dict for p in profiles),
                "profile_unreadable")
        return profiles, values.get("active", "")
    except (ValueError, ET.ParseError, TypeError):
        raise Failure("profile_unreadable") from None


def require_empty_profiles(raw):
    profiles, active = profile_store(raw)
    require(not profiles and not active, "existing_profiles")


class LoginBudget:
    """A stricter whole-run limit: never reset the ten-attempt budget mid-run."""
    def __init__(self):
        self.attempts = {}

    def claim(self, username):
        key = username.strip().lower()
        require(self.attempts.get(key, 0) < 10, "login_budget")
        self.attempts[key] = self.attempts.get(key, 0) + 1

    def count(self, username):
        return self.attempts.get(username.strip().lower(), 0)


@dataclass(repr=False)
class Account:
    role: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    person_id: int = 0
    token: str = field(default="", repr=False)
    creation_started: bool = False


def new_accounts():
    nonce = secrets.token_hex(12)
    return tuple(Account(role, "accept_" + role + "_" + nonce, secrets.token_hex(18))
                 for role in ("alice", "bob"))


class API:
    def __init__(self, origin):
        validate_origin(origin)

    def request(self, method, path, token="", body=None, expected=(200,)):
        require(path.startswith("/v1/") or path == "/healthz", "arguments")
        connection = None
        try:
            # http.client neither consults proxy environment variables nor follows redirects.
            connection = http.client.HTTPConnection("127.0.0.1", 18090, timeout=15)
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            raw = None
            if body is not None:
                raw = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            require(response.status in expected, "http_status")
            if response.status != 200:
                return {}  # Error bodies may contain secrets; do not even read them.
            raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024, "response_shape")
            result = json.loads(raw)
            require(type(result) is dict, "response_shape")
            return result
        except Failure:
            raise
        except (ValueError, UnicodeError):
            raise Failure("response_shape") from None
        except Exception:
            raise Failure("http_transport") from None
        finally:
            if connection is not None:
                connection.close()


class Device:
    package = "com.zgotify.app.internal"

    def __init__(self, adb, serial, output):
        require(serial in ("emulator-5580", "emulator-5582"), "serial_refused")
        self.prefix = [adb, "-s", serial]
        self.serial = serial
        self.output = output
        self.authenticated = False

    def adb(self, *args, stdin=None):
        try:
            return subprocess.run(self.prefix + list(args), input=stdin,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=30, check=True).stdout
        except Exception:
            raise Failure("adb_failed") from None

    def profile_xml(self):
        # Backup preferences take precedence on Android. Refuse ambiguous recovery state.
        command = ("if [ -e shared_prefs/zgotify_personal.xml.bak ]; then exit 33; fi; "
                   "if [ -e shared_prefs/zgotify_personal.xml ]; then "
                   "cat shared_prefs/zgotify_personal.xml; else printf '<map/>'; fi")
        return self.adb("shell", "run-as", self.package, "sh", "-c", shlex.quote(command))

    def preflight(self):
        require(self.adb("get-state").strip() == b"device", "emulator_required")
        require(self.adb("shell", "getprop", "ro.kernel.qemu").strip() == b"1", "emulator_required")
        require(self.adb("shell", "getprop", "ro.build.version.sdk").strip() == b"36", "sdk_required")
        reverse = self.adb("reverse", "--list").splitlines()
        require(not any(b"tcp:18090" in line.split() for line in reverse), "reverse_exists")
        require_empty_profiles(self.profile_xml())

    def nodes(self):
        # Stream XML to stdout without requiring a terminal or leaving a UI dump on disk.
        raw = self.adb("exec-out", "uiautomator", "dump", "/proc/self/fd/1")
        start, end = raw.find(b"<?xml"), raw.rfind(b"</hierarchy>")
        require(start >= 0 and end >= start, "ui_shape")
        try:
            return list(ET.fromstring(raw[start:end + len(b"</hierarchy>")]).iter("node"))
        except ET.ParseError:
            raise Failure("ui_shape") from None

    def wait(self, predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            nodes = self.nodes()
            if predicate(nodes):
                return nodes
            time.sleep(.3)
        raise Failure("ui_timeout")

    def text(self, value, exact=True):
        def matches(node):
            text = node.get("text", "")
            return text == value if exact else value in text
        nodes = self.wait(lambda ns: any(matches(n) for n in ns))
        return next(n for n in nodes if matches(n))

    def tap(self, node):
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        require(bounds is not None, "ui_shape")
        x1, y1, x2, y2 = map(int, bounds.groups())
        require(x2 > x1 and y2 > y1, "ui_shape")
        self.adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))

    def click(self, value, exact=True):
        self.tap(self.text(value, exact))

    @staticmethod
    def ready(nodes):
        return any(n.get("content-desc") == "刷新" and n.get("enabled") == "true" for n in nodes)

    def refresh(self):
        nodes = self.wait(self.ready)
        self.tap(next(n for n in nodes if n.get("content-desc") == "刷新" and n.get("enabled") == "true"))

    def menu(self, value):
        self.wait(self.ready)
        nodes = self.wait(lambda ns: any(n.get("content-desc") in ("More options", "更多选项") for n in ns))
        self.tap(next(n for n in nodes if n.get("content-desc") in ("More options", "更多选项")))
        self.click(value)

    def restart(self):
        self.adb("shell", "am", "force-stop", self.package)
        self.adb("shell", "am", "start", "-n", self.package + "/com.zgotify.app.MainActivity")

    def remove_reverse(self):
        try:
            self.adb("reverse", "--remove", "tcp:18090")
        except Failure:
            pass  # Already absent is safe, but only a successful list can establish that.
        reverse = self.adb("reverse", "--list").splitlines()
        require(not any(b"tcp:18090" in line.split() for line in reverse), "reverse_exists")

    def reauthenticate(self):
        def login_visible(nodes):
            return any(n.get("text") == "登录 zgotify" for n in nodes)
        nodes = self.wait(lambda ns: login_visible(ns) or self.ready(ns))
        if not login_visible(nodes):
            self.tap(next(n for n in nodes if n.get("content-desc") == "刷新" and n.get("enabled") == "true"))
        self.wait(login_visible)

    def inbox(self, count, pending=0, offline=False):
        label = "离线缓存" if offline else "在线"
        text = "%s · %d 条 · 待同步 %d · 待处理 0" % (label, count, pending)
        return self.wait(lambda ns: self.ready(ns) and any(n.get("text") == text for n in ns))

    def active_identity(self, account):
        profiles, active = profile_store(self.profile_xml())
        matches = [p for p in profiles if p.get("server") == ORIGIN
                   and p.get("username") == account.username and p.get("person_id") == account.person_id]
        require(len(matches) == 1 and active == ORIGIN + "|" + account.username, "identity_mismatch")
        self.text(account.username + " · " + ORIGIN)

    def login(self, account, budget):
        self.authenticated = False
        self.text("登录 zgotify")
        for index, value in enumerate((ORIGIN, account.username, account.password)):
            nodes = self.wait(lambda ns: len([n for n in ns if n.get("class") == "android.widget.EditText"]) == 3)
            fields = [n for n in nodes if n.get("class") == "android.widget.EditText"]
            self.tap(fields[index])
            # Input travels over stdin, not in a host process's command-line arguments.
            self.adb("shell", "-T", "sh", "-s", stdin=("input text " + shlex.quote(value) + "\n").encode())
        budget.claim(account.username)
        self.click("登录")
        self.inbox(2)
        self.active_identity(account)
        self.authenticated = True

    def row(self, title, read=False, hidden=False, favorite=False, pending=False):
        node = self.text(title + "\n", exact=False)
        flags = node.get("text", "").split("\n")[1].split(" · ")
        require(("已读" in flags) == read and ("未读" in flags) != read
                and ("已隐藏" in flags) == hidden and ("已收藏" in flags) == favorite
                and ("待同步" in flags) == pending and "同步待处理" not in flags, "state_mismatch")

    def operation(self, title, operation):
        self.wait(self.ready)
        self.click(title + "\n", exact=False)
        self.click("操作")
        self.click(operation)

    def absent(self, titles):
        nodes = self.nodes()
        require(not any(title in n.get("text", "") for n in nodes for title in titles), "message_mismatch")

    def screenshot(self, name):
        if not self.authenticated or name not in CHECKS:
            return False
        def unsafe(nodes):
            return any(n.get("class") == "android.widget.EditText" or n.get("password") == "true"
                       or "登录 zgotify" in n.get("text", "") for n in nodes)
        nodes = self.wait(lambda ns: self.ready(ns) or unsafe(ns))
        if unsafe(nodes):
            return False
        raw = self.adb("exec-out", "screencap", "-p")
        require(raw.startswith(b"\x89PNG\r\n\x1a\n"), "ui_shape")
        with (self.output / (name + "-" + self.serial + ".png")).open("xb") as stream:
            stream.write(raw)
        return True


def require_state(state, read, hidden, favorite, version):
    expected = {"read": read, "hidden": hidden, "favorite": favorite, "version": version}
    require(type(state) is dict and state == expected
            and all(type(state.get(k)) is bool for k in ("read", "hidden", "favorite"))
            and type(state.get("version")) is int, "state_mismatch")


class Harness:
    def __init__(self, args):
        validate_origin(args.api)
        validate_serials(args.serial_a, args.serial_b)
        require(args.allow_isolated_test_mutations and args.admin_password_stdin, "mutation_consent_required")
        self.output = Path(args.output)
        self.output_ready = False
        self.api = API(args.api)
        self.devices = [Device(args.adb, serial, self.output) for serial in (args.serial_a, args.serial_b)]
        self.reverse_devices = []
        self.accounts = []
        self.sessions = []
        self.admin_token = ""
        self.budget = LoginBudget()
        self.topic_id = 0
        self.message_ids = []
        self.titles = []
        self.checks = []
        self.phase = "preflight"

    def preflight(self):
        try:
            self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
            self.output_ready = True
        except OSError:
            raise Failure("output_not_new") from None
        # Both devices must pass before any app launch, reverse or server mutation.
        for device in self.devices:
            device.preflight()

    def passed(self, name, device=None):
        require(name in CHECKS and name not in self.checks, "incomplete_checks")
        if device is not None:
            require(device.screenshot(name), "ui_shape")
        self.checks.append(name)
        print("CHECK_OK " + name, flush=True)

    def login_api(self, username, password):
        self.budget.claim(username)
        result = self.api.request("POST", "/v1/login", body={
            "username": username, "password": password, "device_name": "deployed-acceptance-api"})
        token = result.get("token")
        require(type(token) is str and bool(token), "response_shape")
        self.sessions.append(token)  # Track before validating the remaining response.
        return result

    def reverse(self, device, enabled):
        if enabled:
            if device not in self.reverse_devices:
                self.reverse_devices.append(device)  # Also clean up an ambiguous failed creation.
            device.adb("reverse", "tcp:18090", "tcp:18090")
        else:
            device.remove_reverse()

    def state(self, account, index, *expected):
        result = self.api.request("GET", "/v1/messages/%d" % self.message_ids[index], token=account.token)
        message = result.get("message", {})
        require(message.get("id") == self.message_ids[index] and message.get("topic_id") == self.topic_id,
                "message_mismatch")
        require_state(message.get("state"), *expected)

    def topic_membership(self, account, present):
        result = self.api.request("GET", "/v1/topics", token=account.token).get("topics")
        require(type(result) is list and [t.get("id") for t in result] == ([self.topic_id] if present else []),
                "membership_mismatch")

    def setup(self, password):
        self.phase = "setup"
        health = self.api.request("GET", "/healthz")
        require(health.get("status") == "ok" and type(health.get("version")) is str
                and re.fullmatch(r"0\.2\.\d+(?:[-+][A-Za-z0-9.-]+)?", health["version"]), "backend_version")
        self.passed("backend_ready_02")
        result = self.login_api("admin", password)
        self.admin_token = result["token"]
        person = result.get("person", {})
        require(person.get("username") == "admin" and person.get("admin") is True, "identity_mismatch")
        root_id = positive_id(person.get("id"))
        self.accounts = list(new_accounts())
        for account in self.accounts:
            account.creation_started = True
            result = self.api.request("POST", "/v1/admin/users", token=self.admin_token, body={
                "username": account.username, "password": account.password,
                "display_name": "Synthetic acceptance " + account.role, "admin": False})
            created_id = positive_id(result.get("id"))
            require(created_id != root_id, "identity_mismatch")
            result = self.login_api(account.username, account.password)
            account.token = result["token"]
            person = self.api.request("GET", "/v1/me", token=account.token).get("person", {})
            require(person.get("id") == created_id and person.get("username") == account.username
                    and person.get("admin") is False, "identity_mismatch")
            account.person_id = created_id
        require(self.accounts[0].person_id != self.accounts[1].person_id, "identity_mismatch")
        result = self.api.request("POST", "/v1/admin/topics", token=self.admin_token,
                                  body={"name": "Synthetic acceptance " + secrets.token_hex(8)})
        self.topic_id = positive_id(result.get("id"))
        ingest = result.get("ingest_token")
        require(type(ingest) is str and bool(ingest), "response_shape")
        for account in self.accounts:
            self.api.request("PUT", "/v1/admin/topics/%d/members/%d" % (self.topic_id, account.person_id),
                             token=self.admin_token)
            self.topic_membership(account, True)
        for index in range(2):
            title = "Acceptance-%s-%d" % (secrets.token_hex(4), index + 1)
            result = self.api.request("POST", "/v1/ingest", token=ingest, body={
                "external_id": "acceptance:" + secrets.token_hex(16), "title": title,
                "message": "Synthetic isolated UI acceptance message.", "priority": 5})
            self.message_ids.append(positive_id(result.get("id")))
            self.titles.append(title)
            require(result.get("created") is True, "message_mismatch")
        require(len(set(self.message_ids)) == 2, "message_mismatch")
        for account in self.accounts:
            messages = self.api.request("GET", "/v1/messages?include_hidden=true", token=account.token).get("messages")
            require(type(messages) is list and sorted(m.get("id") for m in messages) == sorted(self.message_ids),
                    "message_mismatch")
            for index in range(2):
                self.state(account, index, False, False, False, 0)

    def workflow(self, password):
        self.setup(password)
        alice, bob = self.accounts
        a, b = self.devices
        one, two = self.titles

        self.phase = "alice_same_person_two_devices"
        for device in self.devices:
            self.reverse(device, True)
            device.restart()
            device.login(alice, self.budget)
            device.row(one)
            device.row(two)
        self.passed(self.phase, a)

        self.phase = "alice_read_sync_b"
        a.operation(one, "标记已读")
        a.inbox(2)
        self.state(alice, 0, True, False, False, 1)
        b.refresh()
        b.inbox(2)
        b.active_identity(alice)
        b.row(one, read=True)
        self.state(bob, 0, False, False, False, 0)
        self.passed(self.phase, b)

        self.phase = "bob_personal_state_isolation"
        b.menu("添加账号")
        b.login(bob, self.budget)
        b.row(one)
        b.operation(two, "标记已读")
        b.inbox(2)
        self.state(bob, 1, True, False, False, 1)
        b.operation(one, "收藏")
        b.inbox(2)
        self.state(bob, 0, False, False, True, 1)
        b.operation(one, "为我隐藏")
        b.inbox(1)
        b.absent([one])
        self.state(bob, 0, False, True, True, 2)
        self.state(alice, 0, True, False, False, 1)
        self.state(alice, 1, False, False, False, 0)
        b.menu("包含已隐藏消息")
        b.inbox(2)
        b.row(one, hidden=True, favorite=True)
        b.row(two, read=True)
        self.passed(self.phase, b)

        self.phase = "add_account_cancel_preserves_bob"
        before = profile_store(b.profile_xml())
        b.menu("添加账号")
        b.click("取消")
        b.inbox(2)
        b.active_identity(bob)
        require(profile_store(b.profile_xml()) == before, "cancel_changed_profiles")
        b.row(one, hidden=True, favorite=True)
        b.row(two, read=True)
        self.state(bob, 0, False, True, True, 2)
        self.state(bob, 1, True, False, False, 1)
        self.passed(self.phase, b)

        self.phase = "offline_favorite_durable_replayed_once"
        self.reverse(a, False)
        # Removing a reverse listener does not close an existing HTTP keep-alive.
        # Stop the process before reopening so no established tunnel survives.
        a.restart()
        a.inbox(2, offline=True)
        print("STEP_OK offline_cold_start", flush=True)
        a.operation(two, "收藏")
        a.inbox(2, pending=1, offline=True)
        self.state(alice, 1, False, False, False, 0)
        a.restart()
        a.inbox(2, pending=1, offline=True)
        a.row(two, favorite=True, pending=True)
        self.state(alice, 1, False, False, False, 0)
        print("STEP_OK offline_queue_survived_restart", flush=True)
        self.reverse(a, True)
        a.refresh()
        a.inbox(2)
        a.row(two, favorite=True)
        self.state(alice, 1, False, False, True, 1)
        a.refresh()
        a.inbox(2)
        self.state(alice, 1, False, False, True, 1)
        self.state(alice, 0, True, False, False, 1)
        self.state(bob, 1, True, False, False, 1)
        self.passed(self.phase, a)

        self.phase = "revoked_membership_cache_purged"
        self.api.request("DELETE", "/v1/admin/topics/%d/members/%d" % (self.topic_id, alice.person_id),
                         token=self.admin_token)
        self.topic_membership(alice, False)
        for mid in self.message_ids:
            self.api.request("GET", "/v1/messages/%d" % mid, token=alice.token, expected=(404,))
        a.refresh()
        a.inbox(0)
        a.menu("包含已隐藏消息")
        a.inbox(0)
        a.absent(self.titles)
        self.reverse(a, False)
        a.restart()
        a.inbox(0, offline=True)
        a.absent(self.titles)
        self.reverse(a, True)
        a.refresh()
        a.inbox(0)
        self.topic_membership(bob, True)
        self.state(bob, 0, False, True, True, 2)
        self.passed(self.phase, a)

        self.phase = "disabled_old_sessions_rejected"
        self.api.request("PATCH", "/v1/admin/users/%d" % bob.person_id,
                         token=self.admin_token, body={"enabled": False})
        self.api.request("GET", "/v1/me", token=bob.token, expected=(401,))
        self.api.request("GET", "/v1/messages/%d" % self.message_ids[0], token=bob.token, expected=(401,))
        self.api.request("PATCH", "/v1/messages/%d/state" % self.message_ids[0], token=bob.token,
                         body={"read": True, "expected_version": 2}, expected=(401,))
        b.authenticated = False
        b.reauthenticate()
        b.click("取消")
        b.wait(b.ready)
        b.absent(self.titles)
        person = self.api.request("GET", "/v1/me", token=alice.token).get("person", {})
        require(person.get("id") == alice.person_id, "identity_mismatch")
        self.passed(self.phase)

    def cleanup(self):
        failures = []

        def attempt(name, action):
            try:
                action()
            except BaseException:
                failures.append(name)

        def disable(account):
            if not account.person_id:
                # Creation may have committed before a transport error. Recover only this
                # synthetic identity, without retrying creation or querying other users.
                result = self.login_api(account.username, account.password)
                person = result.get("person", {})
                require(person.get("username") == account.username and person.get("admin") is False,
                        "identity_mismatch")
                account.person_id = positive_id(person.get("id"))
                account.token = result["token"]
            result = self.api.request("PATCH", "/v1/admin/users/%d" % account.person_id,
                                      token=self.admin_token, body={"enabled": False})
            require(result.get("ok") is True, "response_shape")
            if account.token:
                self.api.request("GET", "/v1/me", token=account.token, expected=(401,))

        for account in self.accounts:
            if account.creation_started or account.person_id:
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

    def write_report(self, report):
        require(self.output_ready, "evidence_write")
        with (self.output / "result.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")

    def execute(self, password):
        error = None
        cleanup_failures = []
        try:
            self.preflight()
            self.workflow(password)
            require(self.checks == list(CHECKS), "incomplete_checks")
        except BaseException as caught:
            error = failure_code(caught)
        finally:
            try:
                cleanup_failures = self.cleanup()
            except BaseException:
                cleanup_failures = ["cleanup_internal_failure"]
        report = {
            "status": "FAIL" if error or cleanup_failures else "PASS",
            "phase": self.phase, "failure": error,
            "checks": self.checks, "cleanup_failures": cleanup_failures,
            "person_ids": [a.person_id for a in self.accounts if a.person_id],
            "topic_id": self.topic_id, "message_ids": self.message_ids,
            "login_attempts": dict({"admin": self.budget.count("admin")}, **{
                a.role: self.budget.count(a.username) for a in self.accounts}),
            "scope": "Operator-deployed API 0.2.x / MySQL; two disposable Android 36 emulator UIs. "
                     "Deployment provenance is operator-supplied, not independently attested. "
                     "Not vendor push, physical phones, or upstream Gotify full-chain.",
            "retained": "Synthetic topics/messages and emulator app data; no deletion/clear API used.",
        }
        try:
            self.write_report(report)
        except BaseException:
            report["status"], report["failure"] = "FAIL", "evidence_write"
        print(json.dumps(report, sort_keys=True), flush=True)
        return 0 if report["status"] == "PASS" else 1


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("arguments")  # argparse normally echoes untrusted argv values.


def parse_args(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--api", required=True)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial-a", required=True)
    parser.add_argument("--serial-b", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-isolated-test-mutations", action="store_true", required=True)
    parser.add_argument("--admin-password-stdin", action="store_true", required=True)
    return parser.parse_args(argv)


def read_admin_password(stream):
    fd = stream.fileno()
    require(stat.S_ISFIFO(os.fstat(fd).st_mode), "stdin_pipe_required")
    deadline, raw = time.monotonic() + 10, b""
    while b"\n" not in raw:
        remaining = deadline - time.monotonic()
        require(remaining > 0 and select.select([fd], [], [], remaining)[0], "stdin_timeout")
        chunk = os.read(fd, 74 - len(raw))
        if not chunk:
            break
        raw += chunk
        require(len(raw) < 74, "password_input")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    require(12 <= len(raw) <= 72 and b"\n" not in raw and b"\r" not in raw, "password_input")
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise Failure("password_input") from None


def main(argv=None):
    try:
        harness = Harness(parse_args(argv))
        password = read_admin_password(sys.stdin)
        return harness.execute(password)
    except (Exception, KeyboardInterrupt) as caught:
        print("FAIL " + failure_code(caught), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
