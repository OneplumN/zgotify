#!/usr/bin/env python3
"""Isolated, operator-driven real-Gotify import acceptance (Python stdlib).

Run on the authorized ECS, from /opt/zgotify-test, with official
gotify/server:2.9.1 already bound ONLY to 127.0.0.1:18091 and named
gotify-fixture on the importer's internal Docker network. This script cannot
attest an image digest; it checks /version and never manages containers.

Required: secrets/ mode 0700, admin_password and gotify_fixture_password
regular owned files (0400, 0600 or 0444 under that private directory).
Do not supply credentials as command arguments or print the fixture file.

    python3 verify-gotify-import.py prepare
    # Operator runs importer: -command import -sources /run/secrets/gotify_sources.json -once
    python3 verify-gotify-import.py verify --expected 205
    # With the importer stopped:
    python3 verify-gotify-import.py seed-offline
    # Operator runs the same importer again (and may repeat it to test deduplication).
    python3 verify-gotify-import.py verify --expected 222

Mount gotify_client_token and gotify_sources.json read-only at /run/secrets/.
prepare and seed-offline are one-shot, including after partial failure. The
0600 fixture journal retains generated credentials, returned IDs, sessions
whose logout failed, and any uncertain operation for PRIVATE manual recovery.
Never delete it and blindly rerun. Existing reports are not overwritten.
Users remain enabled for later manual testing; all known sessions are logged
out, including on failure. A lost login response cannot be logged out without
its token and is recorded as an uncertain operation. No automatic retries.
Seven API logins total in the normal two-stage flow; a persistent conservative
budget caps this harness at ten attempts per five minutes across all users.
No physical-phone or push acceptance is claimed.
"""

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path("/opt/zgotify-test")
API_ORIGIN = "http://127.0.0.1:18090"
GOTIFY_ORIGIN = "http://127.0.0.1:18091"
IMAGE = "gotify/server:2.9.1"
FLAGS = ("read", "hidden", "favorite")


class CheckFailed(Exception):
    def __init__(self, category, status=None):
        # Categories are code-owned, never derived from response/exception text.
        self.category = category
        self.status = status
        super().__init__(category)


def require(condition, category="state"):
    if not condition:
        raise CheckFailed(category)


def positive(value):
    require(type(value) is int and value > 0, "identity")
    return value


def credential(value):
    require(isinstance(value, str) and 0 < len(value) <= 4096 and
            all(33 <= ord(c) <= 126 for c in value), "response")
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    def __init__(self, base):
        require(base in (API_ORIGIN, GOTIFY_ORIGIN), "origin")
        self.base = base
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, method, path, token=None, body=None, expected=200, basic=None):
        require(isinstance(path, str) and path.startswith("/") and
                not path.startswith("//") and "\\" not in path and
                all(33 <= ord(c) <= 126 for c in path), "origin")
        parsed = urlsplit(path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment, "origin")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        require(not (token and basic), "state")
        if token:
            token = credential(token)
            if self.base == API_ORIGIN:
                headers["Authorization"] = "Bearer " + token
            else:
                headers["X-Gotify-Key"] = token
        if basic:
            require(self.base == GOTIFY_ORIGIN, "origin")
            headers["Authorization"] = "Basic " + base64.b64encode(
                ("admin:" + basic).encode("utf-8")).decode("ascii")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, headers=headers, method=method)
        accepted = (expected,) if isinstance(expected, int) else expected
        try:
            try:
                response = self.opener.open(req, timeout=25)
            except HTTPError as error:
                response = error
            with response:
                status = response.getcode()
                if status not in accepted:
                    raise CheckFailed("http", status)
                if status != 200:
                    return None  # Never read an error body, even an expected 409/401.
                raw = response.read(32 * 1024 * 1024 + 1)
                require(len(raw) <= 32 * 1024 * 1024, "response")
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise CheckFailed("response") from None
                require(isinstance(value, dict), "response")
                return value
        except (URLError, OSError, TimeoutError):
            raise CheckFailed("transport") from None


def inspect_file(path, modes):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            info.st_uid == os.geteuid(), "path")
    require(stat.S_IMODE(info.st_mode) in modes, "permission")
    return info


def inspect_directory(path, private=False):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(), "path")
    mode = stat.S_IMODE(info.st_mode)
    require(mode == 0o700 if private else not mode & 0o022, "permission")


def absent(path):
    require(not os.path.lexists(path), "exists")


def read_file(path, modes, limit):
    before = inspect_file(path, modes)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        require((info.st_dev, info.st_ino) == (before.st_dev, before.st_ino), "path")
        value = file.read(limit + 1)
    require(len(value) <= limit, "file")
    return value


def read_json(path, modes):
    try:
        return json.loads(read_file(path, modes, 1024 * 1024))
    except (ValueError, UnicodeError):
        raise CheckFailed("file") from None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive_file(path, data, mode=0o600):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
        file.flush()
        os.fchmod(file.fileno(), mode)
        os.fsync(file.fileno())
    sync_directory(path.parent)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def atomic_fixture(path, value):
    previous = inspect_file(path, (0o600,))
    fd, temporary = tempfile.mkstemp(prefix=".import_fixture-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(json_bytes(value))
            file.flush()
            os.fsync(file.fileno())
        current = inspect_file(path, (0o600,))
        require((previous.st_dev, previous.st_ino) == (current.st_dev, current.st_ino), "path")
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


class Layout:
    def __init__(self, phase, expected):
        require(Path.cwd() == ROOT, "path")
        # Reject symlink ancestors as well as leaf symlinks; no configurable root.
        for path in (ROOT,) + tuple(ROOT.parents):
            require(not path.is_symlink(), "path")
        inspect_directory(ROOT)
        self.secrets = ROOT / "secrets"
        inspect_directory(self.secrets, private=True)
        self.path = self.secrets / "import_fixture.json"
        self.token_path = self.secrets / "gotify_client_token"
        self.sources_path = self.secrets / "gotify_sources.json"
        self.reports = ROOT / "reports"
        self.phase, self.expected = phase, expected
        self.lock = None

    def __enter__(self):
        self.lock = os.open(self.secrets, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.preflight()
            return self
        except BaseException:
            os.close(self.lock)
            raise

    def __exit__(self, *args):
        os.close(self.lock)

    def preflight(self):
        if os.path.lexists(self.reports):
            inspect_directory(self.reports, private=True)
        if self.phase == "prepare":
            for path in (self.path, self.token_path, self.sources_path):
                absent(path)
            for name in ("admin_password", "gotify_fixture_password"):
                inspect_file(self.secrets / name, (0o400, 0o600, 0o444))
            for count in (205, 222):
                absent(self.reports / "gotify-import-{}.json".format(count))
        else:
            inspect_file(self.path, (0o600,))
            inspect_file(self.token_path, (0o444,))
            inspect_file(self.sources_path, (0o444,))
            if self.phase == "verify":
                absent(self.report_path())
        if not self.reports.exists():
            self.reports.mkdir(mode=0o700)
        inspect_directory(self.reports, private=True)
        # Prove output directories writable before any external mutation.
        for directory in (self.secrets, self.reports):
            fd, probe = tempfile.mkstemp(prefix=".import-preflight-", dir=directory)
            os.close(fd)
            os.unlink(probe)
            sync_directory(directory)

    def password(self, name):
        try:
            value = read_file(self.secrets / name, (0o400, 0o600, 0o444), 4096).decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise CheckFailed("file") from None
        minimum = 12 if name == "admin_password" else 1
        require(minimum <= len(value.encode("utf-8")) <= 72 and
                not any(ord(c) < 32 or ord(c) == 127 for c in value), "file")
        return value

    def report_path(self):
        return self.reports / "gotify-import-{}.json".format(self.expected)


def payload(sequence):
    return {"title": "Import fixture {}".format(sequence),
            "message": ("# Import fixture {}\n\n**\u771f\u5b9e\u5bfc\u5165\u9a8c\u6536**"
                        "\n\n\u6d88\u606f\u5e8f\u53f7: {}\n").format(sequence, sequence),
            "priority": 8 + sequence % 2,
            "extras": {"client::display": {"contentType": "text/markdown"}}}


def source_config(state):
    return {"sources": [{"key": state["source_key"], "url": "http://gotify-fixture:80",
                         "user_id": state["gotify"]["user_id"], "topic_id": state["topic"]["id"],
                         "application_id": state["gotify"]["application"]["id"],
                         "client_token_file": "/run/secrets/gotify_client_token", "allow_http": True}]}


class Harness:
    def __init__(self, layout, api, gotify):
        self.layout, self.api, self.gotify = layout, api, gotify
        self.state = None

    def save(self):
        self.state["updated_at_utc"] = now()
        atomic_fixture(self.layout.path, self.state)

    def mutate(self, action, request, record):
        require(self.state["pending"] is None)
        self.state["pending"] = {"action": action, "started_at_utc": now()}
        self.save()  # A timeout may mean the write committed. Never replay it.
        result = request()
        record(result)
        self.state["pending"] = None
        self.save()
        return result

    def budget(self, attempts):
        cutoff = time.time() - 300
        recent = [t for t in self.state["login_attempts"] if t >= cutoff]
        require(len(recent) + attempts <= 10, "login_budget")
        return recent

    def login(self, role, password, device):
        username = "admin" if role == "admin" else self.state["people"][role]["username"]
        self.state["login_attempts"] = self.budget(1) + [time.time()]
        self.save()

        def record(result):
            token = credential(result["token"])
            self.state["sessions"].append({"role": role, "token": token})
            person = result["person"]
            require(person["username"] == username and person["admin"] is (role == "admin"), "identity")
            if role != "admin":
                require(person["id"] == self.state["people"][role]["id"], "identity")

        result = self.mutate("login", lambda: self.api.call("POST", "/v1/login", body={
            "username": username, "password": password, "device_name": device}), record)
        return result["token"]

    def logout_all(self):
        failed = False
        for session in list(self.state["sessions"]):
            try:
                self.api.call("DELETE", "/v1/session", session["token"], expected=(200, 401))
                self.state["sessions"].remove(session)
                self.save()
            except (Exception, KeyboardInterrupt):
                failed = True  # Keep failed/uncertain logout tokens in the private journal.
        return not failed

    def check_version(self):
        require(self.gotify.call("GET", "/version")["version"] == "2.9.1", "version")

    def load(self):
        state = read_json(self.layout.path, (0o600,))
        require(state["schema"] == 1 and state["api_origin"] == API_ORIGIN and
                state["gotify_origin"] == GOTIFY_ORIGIN and state["fixture_image"] == IMAGE)
        require(state["phase"] in ("prepared", "offline_seeded") and
                state["pending"] is None and state["sessions"] == [])
        require(state["source_key"] == "fixture-" + state["run_id"])
        require(state["offline"] == ("complete" if state["phase"] == "offline_seeded" else "not_started"))
        count = 222 if state["phase"] == "offline_seeded" else 205
        require(len(state["messages"]) == count)
        ids = set()
        for seq, record in enumerate(state["messages"], 1):
            require(record["sequence"] == seq)
            mid = positive(record["gotify_id"])
            require(mid not in ids, "identity")
            ids.add(mid)
        positive(state["gotify"]["user_id"])
        positive(state["gotify"]["application"]["id"])
        positive(state["topic"]["id"])
        credential(state["gotify"]["application"]["token"])
        for role in ("alice", "bob"):
            person = state["people"][role]
            positive(person["id"])
            require(person["admin"] is False and len(person["password"]) >= 12)
        require(state["people"]["alice"]["id"] != state["people"]["bob"]["id"], "identity")
        require(read_json(self.layout.sources_path, (0o444,)) == source_config(state), "config")
        token = read_file(self.layout.token_path, (0o444,), 4096).decode("ascii").rstrip("\r\n")
        require(token == credential(state["gotify"]["client"]["token"]), "config")
        self.state = state

    def prepare(self):
        admin_password = self.layout.password("admin_password")
        fixture_password = self.layout.password("gotify_fixture_password")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz") + secrets.token_hex(3)
        people = {role: {"username": role + "_" + run_id, "password": secrets.token_hex(24),
                         "admin": False} for role in ("alice", "bob")}
        self.state = {"schema": 1, "run_id": run_id, "source_key": "fixture-" + run_id,
                      "api_origin": API_ORIGIN, "gotify_origin": GOTIFY_ORIGIN, "fixture_image": IMAGE,
                      "created_at_utc": now(), "phase": "preparing", "pending": None,
                      "offline": "not_started", "people": people, "gotify": {}, "memberships": [],
                      "messages": [], "sessions": [], "login_attempts": []}
        exclusive_file(self.layout.path, json_bytes(self.state))
        try:
            self.check_version()
            require(self.api.call("GET", "/healthz")["status"] == "ok", "health")
            user = self.gotify.call("GET", "/current/user", basic=fixture_password)
            require(user["name"] == "admin" and user["admin"] is True, "identity")
            self.state["gotify"]["user_id"] = positive(user["id"])
            self.save()

            def gotify_record(kind, result):
                self.state["gotify"][kind] = {"id": positive(result["id"]),
                                               "token": credential(result["token"])}

            self.mutate("create_client", lambda: self.gotify.call("POST", "/client",
                basic=fixture_password, body={"name": "Import fixture " + run_id}),
                lambda result: gotify_record("client", result))
            self.mutate("create_application", lambda: self.gotify.call("POST", "/application",
                self.state["gotify"]["client"]["token"], body={"name": "Import fixture " + run_id,
                "description": "Isolated import acceptance", "defaultPriority": 9}),
                lambda result: gotify_record("application", result))
            admin = self.login("admin", admin_password, "gotify-import-controller")

            def topic_record(result):
                self.state["topic"] = {"id": positive(result["id"]),
                                       "ingest_token": credential(result["ingest_token"])}

            self.mutate("create_topic", lambda: self.api.call("POST", "/v1/admin/topics", admin,
                {"name": "Import fixture " + run_id}), topic_record)
            for role, person in people.items():
                self.mutate("create_" + role, lambda: self.api.call("POST", "/v1/admin/users", admin,
                    {"username": person["username"], "password": person["password"],
                     "display_name": role.title() + " import fixture", "admin": False}),
                    lambda result: person.update(id=positive(result["id"])))
                self.mutate("membership_" + role, lambda: self.api.call("PUT",
                    "/v1/admin/topics/{}/members/{}".format(self.state["topic"]["id"], person["id"]), admin),
                    lambda result: self.state["memberships"].append(person["id"]))
        finally:
            if not self.logout_all():
                raise CheckFailed("logout")
        exclusive_file(self.layout.token_path,
                       (self.state["gotify"]["client"]["token"] + "\n").encode("ascii"), 0o444)
        exclusive_file(self.layout.sources_path, json_bytes(source_config(self.state)), 0o444)
        self.seed(1, 205)
        self.state["phase"] = "prepared"
        self.save()

    def seed(self, start, end):
        for sequence in range(start, end + 1):
            def record(result):
                mid = positive(result["id"])
                require(all(m["gotify_id"] != mid for m in self.state["messages"]), "identity")
                self.state["messages"].append({"sequence": sequence, "gotify_id": mid})
            self.mutate("seed_message_{}".format(sequence), lambda: self.gotify.call("POST", "/message",
                self.state["gotify"]["application"]["token"], payload(sequence)), record)

    def seed_offline(self):
        require(self.state["phase"] == "prepared" and self.state["offline"] == "not_started")
        self.check_version()
        self.state["offline"] = "started"
        self.state["phase"] = "seeding_offline"
        self.save()
        self.seed(206, 222)
        self.state["offline"] = "complete"
        self.state["phase"] = "offline_seeded"
        self.save()

    def pages(self, upstream, token, hidden=True):
        client = self.gotify if upstream else self.api
        path = "/message" if upstream else "/v1/messages"
        result, before = [], 0
        for _ in range(10000):
            query = {"limit": 100}
            if not upstream:
                query["include_hidden"] = str(hidden).lower()
            if before:
                query["since" if upstream else "before"] = before
            # Never follow an upstream paging link, even one pointing to loopback.
            rows = client.call("GET", path + "?" + urlencode(query), token)["messages"]
            require(isinstance(rows, list) and len(rows) <= 100, "pagination")
            if not rows:
                return result
            for row in rows:
                mid = positive(row["id"])
                require(before == 0 or mid < before, "pagination")
                before = mid
                result.append(row)
        raise CheckFailed("pagination")

    def fixture_rows(self, rows, upstream=False):
        field = "appid" if upstream else "topic_id"
        target = self.state["gotify"]["application"]["id"] if upstream else self.state["topic"]["id"]
        selected = [row for row in rows if row[field] == target]
        require(len(selected) == len(self.state["messages"]), "count")
        by_title = {row["title"]: row for row in selected}
        require(len(by_title) == len(selected), "duplicate")
        ordered = []
        for record in self.state["messages"]:
            expected = payload(record["sequence"])
            require(expected["title"] in by_title, "content")
            row = by_title[expected["title"]]
            for key in ("title", "message", "priority"):
                require(row[key] == expected[key], "content")
            if upstream:
                require(row["id"] == record["gotify_id"], "identity")
                require(row.get("extras") == expected["extras"], "content")
            else:
                self.valid_state(row["state"])
            ordered.append(row)
        return ordered

    @staticmethod
    def valid_state(state):
        require(type(state["version"]) is int and state["version"] >= 0 and
                all(type(state[k]) is bool for k in FLAGS), "response")

    def verify(self, expected):
        require(len(self.state["messages"]) == expected, "count")
        self.budget(3)
        self.check_version()
        before = self.pages(True, self.state["gotify"]["client"]["token"])
        self.fixture_rows(before, upstream=True)
        try:
            a1 = self.login("alice", self.state["people"]["alice"]["password"], "gotify-import-alice-a")
            a2 = self.login("alice", self.state["people"]["alice"]["password"], "gotify-import-alice-b")
            b1 = self.login("bob", self.state["people"]["bob"]["password"], "gotify-import-bob-a")
            alice = self.fixture_rows(self.pages(False, a1))
            other = self.fixture_rows(self.pages(False, a2))
            bob = self.fixture_rows(self.pages(False, b1))
            require(alice == other, "sync")
            require([m["id"] for m in alice] == [m["id"] for m in bob], "identity")
            require(all(m["state"] == {"read": False, "hidden": False, "favorite": False, "version": 0}
                        for m in bob), "isolation")
            target = alice[0]
            version = target["state"]["version"]
            patch = dict.fromkeys(FLAGS, True)
            patch["expected_version"] = version
            state_path = "/v1/messages/{}/state".format(target["id"])
            updated = self.mutate("alice_state", lambda: self.api.call("PATCH", state_path, a1, patch),
                                  lambda result: self.state.update(last_alice_state=result))
            self.valid_state(updated)
            require(updated["version"] == version + 1 and all(updated[k] for k in FLAGS), "version")
            synced = self.fixture_rows(self.pages(False, a2))
            require(synced[0]["state"] == updated, "sync")
            visible = self.pages(False, a2, hidden=False)
            require(not any(m["id"] == target["id"] for m in visible), "hidden")
            expected_visible = {m["id"] for m in synced if not m["state"]["hidden"]}
            require({m["id"] for m in visible if m["topic_id"] == self.state["topic"]["id"]}
                    == expected_visible, "hidden")
            require(self.fixture_rows(self.pages(False, b1)) == bob, "isolation")
            self.api.call("PATCH", state_path, a2,
                          {"read": False, "expected_version": version}, expected=409)
            require(self.fixture_rows(self.pages(False, a2))[0]["state"] == updated, "version")
            after = self.pages(True, self.state["gotify"]["client"]["token"])
            self.fixture_rows(after, upstream=True)
            require([m["id"] for m in before] == [m["id"] for m in after], "upstream")
            mapping = [{"sequence": record["sequence"], "gotify_id": record["gotify_id"],
                        "platform_id": message["id"]} for record, message in zip(self.state["messages"], alice)]
            report = {"status": "passed", "run_id": self.state["run_id"], "expected": expected,
                      "completed_at_utc": now(), "page_size": 100, "fixture_image": IMAGE,
                      "gotify_version": "2.9.1", "topic_id": self.state["topic"]["id"],
                      "alice_count": len(alice), "alice_second_device_count": len(other), "bob_count": len(bob),
                      "upstream_before": len(before), "upstream_after": len(after),
                      "alice_version_before": version, "alice_version_after": updated["version"],
                      "mapping": mapping, "physicalphone": False, "push": False,
                      "users_left_enabled": True}
        finally:
            if not self.logout_all():
                raise CheckFailed("logout")
        self.state["last_verified_expected"] = expected
        self.state["mapping"] = mapping
        self.save()
        exclusive_file(self.layout.report_path(), json_bytes(report))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckFailed("arguments")  # argparse normally echoes user input.


def main(argv=None):
    try:
        parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        phases = parser.add_subparsers(dest="phase", required=True)
        for name in ("prepare", "seed-offline", "verify"):
            command = phases.add_parser(name)
            command.add_argument("--base", default=API_ORIGIN)
            command.add_argument("--gotify-base", default=GOTIFY_ORIGIN)
            if name == "verify":
                command.add_argument("--expected", type=int, choices=(205, 222), required=True)
            else:
                command.set_defaults(expected=None)
        args = parser.parse_args(argv)
        require(args.base == API_ORIGIN and args.gotify_base == GOTIFY_ORIGIN, "origin")
        with Layout(args.phase, args.expected) as layout:
            harness = Harness(layout, HTTP(args.base), HTTP(args.gotify_base))
            if args.phase == "prepare":
                harness.prepare()
            else:
                harness.load()
                if args.phase == "seed-offline":
                    harness.seed_offline()
                else:
                    harness.verify(args.expected)
        print(json.dumps({"status": "ok", "phase": args.phase,
                          "message_count": len(harness.state["messages"])}))
        return 0
    except CheckFailed as error:
        report = {"status": "fail", "category": error.category}
        if error.status is not None:
            report["http_status"] = error.status
    except KeyboardInterrupt:
        report = {"status": "fail", "category": "interrupted"}
    except OSError:
        report = {"status": "fail", "category": "filesystem"}
    except Exception:
        report = {"status": "fail", "category": "internal"}
    print(json.dumps(report), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
