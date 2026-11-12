#!/usr/bin/env python3
"""One-shot operator preparation, ONLY from /opt/zgotify-test.

Requires the protected import_fixture.json and the already-running continuous
importer. Creates secrets/android-render/ (0700), journal.json and input.json
(0600); refuses reuse, including partial/ambiguous runs. Never delete that
directory to retry. No importer/container management or upstream deletion.

input.json is PRIVATE credential handoff, NOT evidence. Pipe it directly into
gotify_render_acceptance.py stdin through the operator's authorized transport;
never print it, put it in argv, or copy it into logs/evidence. stdout contains
only a sanitized status. A failed run requires private operator reconciliation.
"""
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import time
from urllib.error import HTTPError, URLError

spec = importlib.util.spec_from_file_location("render_import_fixture", Path(__file__).with_name("verify-gotify-import.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
require = fixture.require


def applications(http, token):
    """The existing transport accepts objects; this one read endpoint returns an array."""
    require(http.base == fixture.GOTIFY_ORIGIN, "origin")
    req = fixture.Request(http.base + "/application", method="GET", headers={
        "Accept": "application/json", "X-Gotify-Key": fixture.credential(token)})
    try:
        with http.opener.open(req, timeout=25) as response:
            require(response.getcode() == 200, "http")
            raw = response.read(65537)
            require(len(raw) <= 65536, "response")
            value = json.loads(raw)
            require(type(value) is list and len(value) <= 100 and all(type(v) is dict for v in value), "response")
            return value
    except (HTTPError, URLError, OSError, ValueError, UnicodeError):
        raise fixture.CheckFailed("transport") from None


class Preparation:
    def __init__(self, layout, api, gotify):
        self.layout, self.api, self.gotify = layout, api, gotify
        self.directory = layout.secrets / "android-render"
        self.path = self.directory / "journal.json"
        self.state = None
        self.original = fixture.Harness(layout, api, gotify)

    def save(self):
        fixture.atomic_fixture(self.path, self.state)

    def mutate(self, action, request, record):
        require(self.state["pending"] is None)
        self.state["pending"] = {"action": action, "started_at_utc": fixture.now()}
        self.save()
        result = request()
        record(result)
        self.state["pending"] = None
        self.save()
        return result

    def identity(self):
        self.original.load()  # Established permission, origin, source and journal guards.
        old = self.original.state
        self.original.check_version()
        health = self.api.call("GET", "/healthz")
        require(health.get("status") == "ok" and health.get("version") == "0.2.0-internal", "version")
        user = self.gotify.call("GET", "/current/user", old["gotify"]["client"]["token"])
        require(type(user.get("id")) is int and user["id"] == old["gotify"]["user_id"]
                and user.get("name") == "admin" and user.get("admin") is True, "identity")
        matches = [app for app in applications(self.gotify, old["gotify"]["client"]["token"])
                   if type(app.get("id")) is int and app["id"] == old["gotify"]["application"]["id"]]
        require(len(matches) == 1 and matches[0].get("name") == "Import fixture " + old["run_id"]
                and matches[0].get("token") == old["gotify"]["application"]["token"], "identity")

    def login(self):
        old = self.original.state
        recent = [t for t in old["login_attempts"] if t >= time.time() - 300]
        require(len(recent) < 10, "login_budget")
        self.state["login_attempts"] = recent + [time.time()]
        self.save()
        person = old["people"]["alice"]

        def record(result):
            token = fixture.credential(result.get("token"))
            self.state["sessions"].append(token)
            self.save()  # Track the token even if the identity response is wrong.
            self.verify_person(result.get("person"), person)

        result = self.mutate("login", lambda: self.api.call("POST", "/v1/login", body={
            "username": person["username"], "password": person["password"],
            "device_name": "android-render-preparation"}), record)
        token = result["token"]
        self.verify_person(self.api.call("GET", "/v1/me", token).get("person"), person)
        topics = self.api.call("GET", "/v1/topics", token).get("topics")
        require(type(topics) is list and len(topics) == 1 and type(topics[0]) is dict
                and type(topics[0].get("id")) is int and topics[0]["id"] == old["topic"]["id"]
                and topics[0].get("name") == "Import fixture " + old["run_id"], "identity")
        return token

    @staticmethod
    def verify_person(actual, expected):
        require(type(actual) is dict and type(actual.get("id")) is int
                and actual["id"] == expected["id"] and actual.get("username") == expected["username"]
                and actual.get("admin") is False, "identity")

    def seed(self):
        old = self.original.state
        for kind in ("markdown", "plain"):
            body = {"title": "Render-" + self.state["run_id"] + "-" + kind,
                    "message": "**Render " + self.state["run_id"] + "**", "priority": 9}
            if kind == "markdown":
                body["extras"] = {"client::display": {"contentType": "text/markdown"}}

            def record(result):
                mid = fixture.positive(result.get("id"))
                require(mid not in {m["gotify_id"] for m in old["messages"] + self.state["messages"]}
                        and type(result.get("appid")) is int
                        and result["appid"] == old["gotify"]["application"]["id"]
                        and all(result.get(k) == v for k, v in body.items())
                        and type(result.get("priority")) is int, "identity")
                if kind == "plain":
                    require(not result.get("extras"), "identity")
                self.state["messages"].append({"gotify_id": mid, "topic_id": old["topic"]["id"],
                    "title": body["title"], "message": body["message"], "priority": 9,
                    "content_type": "text/" + kind})

            self.mutate("message_" + kind, lambda: self.gotify.call("POST", "/message",
                old["gotify"]["application"]["token"], body), record)

    @staticmethod
    def match(actual, expected):
        require(type(actual) is dict, "response")
        fixture.positive(actual.get("id"))
        fixture.positive(actual.get("topic_id"))
        require(type(actual.get("priority")) is int
                and all(actual.get(k) == v for k, v in expected.items() if k != "gotify_id"), "identity")

    def poll(self, token):
        deadline = time.monotonic() + 120
        for _ in range(60):
            require(time.monotonic() < deadline, "import_timeout")
            matches, before = {}, 0
            for _ in range(10):
                require(time.monotonic() < deadline, "import_timeout")
                rows = self.api.call("GET", "/v1/messages?include_hidden=true&before=" + str(before), token).get("messages")
                require(type(rows) is list and len(rows) <= 100, "response")
                for row in rows:
                    require(type(row) is dict, "response")
                    mid = fixture.positive(row.get("id"))
                    require(not before or mid < before, "pagination")
                    before = mid
                    for expected in self.state["messages"]:
                        if row.get("title") == expected["title"]:
                            self.match(row, expected)
                            require(expected["title"] not in matches, "identity")
                            matches[expected["title"]] = mid
                if len(rows) < 100:
                    break
            else:
                raise fixture.CheckFailed("pagination")
            if len(matches) == 2:
                require(len(set(matches.values())) == 2, "identity")
                for message in self.state["messages"]:
                    message["id"] = matches[message["title"]]
                    self.match(self.api.call("GET", "/v1/messages/" + str(message["id"]), token).get("message"), message)
                self.save()
                return
            time.sleep(2)
        raise fixture.CheckFailed("import_timeout")

    def logout(self):
        failed = False
        for token in list(self.state["sessions"]):
            try:
                self.api.call("DELETE", "/v1/session", token, expected=(200, 401))
                self.api.call("GET", "/v1/me", token, expected=401)
                self.state["sessions"].remove(token)
                self.save()
            except BaseException:
                failed = True
        require(not failed, "logout")

    def run(self):
        fixture.absent(self.directory)
        self.identity()
        password = self.layout.password("admin_password")
        self.directory.mkdir(mode=0o700)
        fixture.inspect_directory(self.directory, private=True)
        self.state = {"schema": 1, "run_id": secrets.token_hex(8), "pending": None,
                      "sessions": [], "messages": [], "login_attempts": [], "phase": "preparing"}
        fixture.exclusive_file(self.path, fixture.json_bytes(self.state))
        try:
            token = self.login()
            self.seed()
            self.poll(token)
        finally:
            self.logout()
        old = self.original.state
        doc = {"schema": 1, "api_origin": fixture.API_ORIGIN, "admin_password": password,
               "run_id": self.state["run_id"], "messages": self.state["messages"],
               "fixture": {"topic_id": old["topic"]["id"], "topic_name": "Import fixture " + old["run_id"],
                           "source_key": old["source_key"], "application_id": old["gotify"]["application"]["id"],
                           "original_count": len(old["messages"])}}
        self.state["phase"] = "complete"
        self.save()
        fixture.exclusive_file(self.directory / "input.json", fixture.json_bytes(doc))


def main(argv=None):
    try:
        parser = fixture.Parser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--api", default=fixture.API_ORIGIN)
        parser.add_argument("--gotify", default=fixture.GOTIFY_ORIGIN)
        parser.add_argument("--allow-isolated-test-mutations", action="store_true", required=True)
        args = parser.parse_args(argv)
        require(args.api == fixture.API_ORIGIN and args.gotify == fixture.GOTIFY_ORIGIN, "origin")
        with fixture.Layout("android-render", None) as layout:
            Preparation(layout, fixture.HTTP(args.api), fixture.HTTP(args.gotify)).run()
        print('{"status":"ok","phase":"android-render-prepared"}')
        return 0
    except fixture.CheckFailed as error:
        category = error.category
    except KeyboardInterrupt:
        category = "interrupted"
    except Exception:
        category = "internal"
    print(json.dumps({"status": "fail", "category": category}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
