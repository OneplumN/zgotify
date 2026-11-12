#!/usr/bin/env python3
"""Isolated Android UI acceptance against a loopback-only API fixture, not production.

Requires an already booted disposable emulator and an installed internal debug APK.
Uses synthetic accounts only. The fixture exits when the test finishes.
"""
import argparse
import copy
import json
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class Fixture:
    def __init__(self):
        self.lock = threading.RLock()
        self.offline = False
        self.sessions = {}
        self.states = {}
        self.writes = []
        self.topics = {1: {7}, 2: {7}}
        self.next_token = 0

    def state(self, person, message):
        return self.states.setdefault((person, message), {
            "read": False, "hidden": False, "favorite": False, "version": 0})

    def message(self, person, message):
        return {"id": message, "topic_id": 7,
                "title": "CPU alert" if message == 42 else "Database alert",
                "message": "Synthetic offline acceptance message. No production data.",
                "priority": 9, "created_at": "2026-11-12T04:00:00Z",
                "state": copy.deepcopy(self.state(person, message))}

    def handle(self, method, path, bearer, body):
        with self.lock:
            if self.offline:
                return 503, {"error": "fixture unavailable"}
            url = urlsplit(path)
            path = url.path
            if method == "POST" and path == "/v1/login":
                name = body.get("username", "").lower()
                person = {"alice": 1, "bob": 2}.get(name)
                if not person or body.get("password") != "Fixture-only-123!":
                    return 401, {"error": "invalid credentials"}
                self.next_token += 1
                token = f"synthetic-session-{self.next_token}"
                self.sessions[token] = person
                return 200, {"token": token, "person": {"id": person, "username": name}}
            person = self.sessions.get(bearer.removeprefix("Bearer "))
            if not person:
                return 401, {"error": "invalid credentials"}
            if method == "GET" and path == "/v1/me":
                return 200, {"person": {"id": person, "username": "alice" if person == 1 else "bob"}}
            if method == "GET" and path == "/v1/topics":
                return 200, {"topics": [{"id": topic, "name": "Synthetic"} for topic in self.topics[person]]}
            if method == "GET" and path == "/v1/messages":
                before = int(parse_qs(url.query).get("before", [0])[0])
                return 200, {"messages": [self.message(person, mid) for mid in (42, 41)
                                           if 7 in self.topics[person] and (not before or mid < before)]}
            match = re.fullmatch(r"/v1/messages/(\d+)(/state)?", path)
            if match:
                mid = int(match[1])
                if mid not in (41, 42) or 7 not in self.topics[person]:
                    return 404, {"error": "not found"}
                if method == "GET" and not match[2]:
                    return 200, {"message": self.message(person, mid)}
                if method == "PATCH" and match[2]:
                    state = self.state(person, mid)
                    if body.get("expected_version") != state["version"]:
                        return 409, {"error": "conflict"}
                    for key in ("read", "hidden", "favorite"):
                        if key in body:
                            assert type(body[key]) is bool
                            state[key] = body[key]
                    state["version"] += 1
                    self.writes.append({"person": person, "message": mid, "patch": copy.deepcopy(body)})
                    return 200, copy.deepcopy(state)
            if method == "DELETE" and path == "/v1/session":
                del self.sessions[bearer.removeprefix("Bearer ")]
                return 200, {"ok": True}
            return 404, {"error": "not found"}


def handler(fixture):
    class Handler(BaseHTTPRequestHandler):
        def dispatch(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            status, value = fixture.handle(self.command, self.path, self.headers.get("Authorization", ""), body)
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PATCH = do_DELETE = dispatch

        def log_message(self, *_):
            pass
    return Handler


class Device:
    package = "com.zgotify.app.internal"

    def __init__(self, adb, serial, output):
        self.prefix = [adb, "-s", serial]
        self.output = output

    def adb(self, *args):
        return subprocess.check_output(self.prefix + list(args), timeout=30)

    def nodes(self):
        self.adb("shell", "uiautomator", "dump", "/sdcard/zgotify-window.xml")
        xml = self.adb("exec-out", "cat", "/sdcard/zgotify-window.xml")
        return list(ET.fromstring(xml).iter("node"))

    def wait(self, predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            nodes = self.nodes()
            matches = [node for node in nodes if predicate(node)]
            if matches:
                return matches
            time.sleep(.3)
        self.screenshot("failure")
        raise AssertionError("UI condition timed out: " + repr([node.get("text") for node in nodes]))

    def text(self, value, exact=False):
        return self.wait(lambda n: n.get("text", "") == value if exact else value in n.get("text", ""))

    def tap(self, node):
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds")))
        self.adb("shell", "input", "tap", str((x1+x2)//2), str((y1+y2)//2))

    def click(self, value, exact=True):
        self.tap(self.text(value, exact)[0])

    def menu(self, value):
        self.tap(self.wait(lambda n: n.get("content-desc") == "More options")[0])
        self.click(value)

    def refresh(self):
        self.tap(self.wait(lambda n: n.get("content-desc") == "刷新" and n.get("enabled") == "true")[0])

    def restart(self):
        self.adb("shell", "am", "force-stop", self.package)
        self.adb("shell", "am", "start", "-n", self.package + "/com.zgotify.app.MainActivity")

    def login(self, name, port):
        for index, value in enumerate([f"http://127.0.0.1:{port}", name, "Fixture-only-123!"]):
            fields = self.wait(lambda n: n.get("class") == "android.widget.EditText")
            assert len(fields) == 3
            self.tap(fields[index])
            self.adb("shell", "input", "text", value)
        self.click("登录")
        self.text("在线 · 2 条")

    def operation(self, value):
        self.click("CPU alert", exact=False)
        self.click("操作")
        self.click(value)

    def screenshot(self, name):
        (self.output / f"{name}.png").write_bytes(self.adb("exec-out", "screencap", "-p"))


def run(args):
    if not re.fullmatch(r"emulator-\d+", args.serial):
        raise ValueError("Use a disposable Android emulator, not a personal device")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    fixture = Fixture()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(fixture))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    device = Device(args.adb, args.serial, output)
    checks = []
    def passed(message):
        checks.append(message)
        print("PASS: " + message, flush=True)
    try:
        device.adb("reverse", f"tcp:{args.port}", f"tcp:{args.port}")
        device.restart()
        device.login("alice", args.port)
        device.screenshot("01-online")
        passed("Alice login and online inbox")
        fixture.offline = True
        device.refresh()
        device.text("离线缓存 · 2 条")
        device.operation("标记已读")
        device.text("离线缓存 · 2 条 · 待同步 1")
        device.restart()
        device.text("离线缓存 · 2 条 · 待同步 1")
        assert "已读" in device.text("CPU alert")[0].get("text")
        device.screenshot("02-offline-after-restart")
        passed("Cached content and queued read survive process force-stop and reopen")
        fixture.offline = False
        device.refresh()
        device.text("在线 · 2 条 · 待同步 0")
        assert fixture.state(1, 42)["read"] and len(fixture.writes) == 1
        passed("Recovery submits one touched-field patch and acknowledges queue")
        device.menu("添加账号")
        device.login("bob", args.port)
        assert "未读" in device.text("CPU alert")[0].get("text")
        device.menu("添加账号")
        device.click("取消")
        assert device.text("bob · http://127.0.0.1")
        device.screenshot("03-bob-isolated")
        passed("Bob has independent state; cancel add-account preserves current account")
        device.menu("账号")
        device.click("alice\n", exact=False)
        device.text("在线 · 2 条")
        fixture.offline = True
        device.refresh()
        device.text("离线缓存 · 2 条")
        device.operation("为我隐藏")
        device.text("离线缓存 · 1 条 · 待同步 1")
        with fixture.lock:
            fixture.state(1, 42).update(favorite=True, version=2)
            fixture.offline = False
        device.refresh()
        device.text("待同步 1 · 待处理 1")
        assert len(fixture.writes) == 1
        device.menu("待同步修改")
        device.click("CPU alert")
        device.text("同步待处理")
        device.screenshot("04-explicit-conflict")
        device.click("保留本机修改")
        device.text("在线 · 1 条 · 待同步 0")
        state = fixture.state(1, 42)
        assert state["read"] and state["hidden"] and state["favorite"] and state["version"] == 3
        assert len(fixture.writes) == 2 and set(fixture.writes[-1]["patch"]) == {"expected_version", "hidden"}
        passed("Hidden conflict remains reachable; explicit rebase preserves other-device favorite")
        device.menu("包含已隐藏消息")
        device.text("在线 · 2 条")
        assert "已隐藏" in device.text("CPU alert")[0].get("text")
        fixture.topics[1] = set()
        device.refresh()
        device.text("在线 · 0 条 · 待同步 0")
        device.restart()
        device.text("在线 · 0 条")
        passed("Revoked topic is purged and stays absent after restart")
        device.menu("注销当前设备")
        device.click("注销")
        device.text("bob · http://127.0.0.1")
        device.text("在线 · 2 条")
        assert "未读" in device.text("CPU alert")[0].get("text")
        device.screenshot("05-logout-preserves-bob")
        passed("Logout removes Alice locally and retains Bob account/cache")
        result = {"checks": checks, "passed": len(checks), "fixture_successful_patches": fixture.writes,
                  "scope": "Android emulator UI + loopback HTTP fixture; not ECS/MySQL/OEM push acceptance"}
        (output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        device.adb("reverse", "--remove", f"tcp:{args.port}")
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--port", type=int, default=18092)
    run(parser.parse_args())
