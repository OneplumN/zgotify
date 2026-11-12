"""Offline tests only: synthetic secrets, no listeners or network connections."""

import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit


SCRIPT = Path(__file__).with_name("verify-gotify-import.py")
spec = importlib.util.spec_from_file_location("verify_gotify_import", SCRIPT)
h = None
if SCRIPT.is_file():
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)


class ArtifactTest(unittest.TestCase):
    def test_harness_is_present(self):
        self.assertTrue(SCRIPT.is_file(), "isolated acceptance harness is missing")


class Response(io.BytesIO):
    def __init__(self, value, status=200):
        super().__init__(json.dumps(value).encode("utf-8"))
        self.status = status

    def getcode(self):
        return self.status


class FixtureHTTP:
    """Model only the external boundary, including sparse, unrelated IDs."""

    def __init__(self):
        self.requests = []
        self.users = {}
        self.sessions = {}
        self.upstream = []
        self.imported = []
        self.states = {}
        self.login_count = 0
        self.fail_message = None
        self.fail_user = False
        self.fail_logout = False
        self.fail_after_patch = False
        self.version = "2.9.1"

    def import_messages(self):
        # This substitutes for the operator's importer, never runs one.
        self.imported = [dict(m, id=10001 + i * 11, topic_id=55)
                         for i, m in enumerate(self.upstream)]

    def open(self, req, timeout):
        url = urlsplit(req.full_url)
        assert url.scheme == "http" and url.netloc in (
            "127.0.0.1:18090", "127.0.0.1:18091")
        assert timeout > 0
        query = parse_qs(url.query)
        assert "token" not in query
        body = json.loads(req.data) if req.data is not None else None
        method, path = req.get_method(), url.path
        auth = req.get_header("Authorization")
        key = req.get_header("X-gotify-key")
        self.requests.append((url.netloc, method, path, query, body))
        if url.port == 18091:
            if path == "/version":
                assert method == "GET"
                return Response({"version": self.version, "commit": "fixture"})
            if path in ("/current/user", "/client"):
                assert auth == "Basic " + base64.b64encode(
                    b"admin:synthetic-fixture-password").decode()
                if path == "/current/user":
                    assert method == "GET"
                    return Response({"id": 37, "name": "admin", "admin": True})
                assert method == "POST" and body["name"]
                return Response({"id": 71, "token": "synthetic-client-token"})
            if path == "/application":
                assert key == "synthetic-client-token" and method == "POST"
                assert body["defaultPriority"] == 9
                return Response({"id": 83, "token": "synthetic-app-token"})
            if path == "/message" and method == "POST":
                assert key == "synthetic-app-token"
                seq = len(self.upstream) + 1
                if self.fail_message == seq:
                    raise URLError("private payload and token must not escape")
                assert body["title"] == "Import fixture {}".format(seq)
                assert body["priority"] == 8 + seq % 2
                assert body["message"] == (
                    "# Import fixture {}\n\n**\u771f\u5b9e\u5bfc\u5165\u9a8c\u6536**"
                    "\n\n\u6d88\u606f\u5e8f\u53f7: {}\n").format(seq, seq)
                assert body["extras"] == {"client::display": {"contentType": "text/markdown"}}
                message = dict(body, id=101 + seq * 7, appid=83)
                self.upstream.append(message)
                return Response(message)
            assert method == "GET" and path == "/message" and key == "synthetic-client-token"
            assert query["limit"] == ["100"]
            before = int(query.get("since", ["0"])[0])
            messages = sorted(self.upstream, key=lambda m: m["id"], reverse=True)
            messages = [m for m in messages if not before or m["id"] < before][:100]
            return Response({"messages": messages, "paging": {
                "next": "https://production.invalid/message?token=DO-NOT-FOLLOW"}})

        if path == "/healthz":
            return Response({"status": "ok"})
        if path == "/v1/login":
            self.login_count += 1
            username = body["username"]
            if username == "admin":
                assert body["password"] == "synthetic-admin-password"
                person = {"id": 1, "username": "admin", "admin": True}
            else:
                assert body["password"] == self.users[username]["password"]
                person = {k: self.users[username][k] for k in ("id", "username", "admin")}
            token = "synthetic-session-{}".format(self.login_count)
            self.sessions[token] = person
            return Response({"token": token, "person": person})
        token = auth[7:] if auth and auth.startswith("Bearer ") else ""
        assert token in self.sessions
        person = self.sessions[token]
        if path == "/v1/session":
            assert method == "DELETE"
            if self.fail_logout:
                return Response({"error": "private logout failure"}, 500)
            del self.sessions[token]
            return Response({"ok": True})
        if path.startswith("/v1/admin/"):
            assert person["admin"]
            if path == "/v1/admin/topics":
                assert method == "POST"
                return Response({"id": 55, "ingest_token": "synthetic-ingest-token"})
            if path == "/v1/admin/users":
                assert method == "POST" and body["admin"] is False
                if self.fail_user:
                    return Response({"error": "secret user failure"}, 500)
                user = dict(body, id=201 + len(self.users))
                self.users[body["username"]] = user
                return Response({"id": user["id"]})
            assert method == "PUT" and path.startswith("/v1/admin/topics/55/members/")
            return Response({"ok": True})
        if path == "/v1/messages":
            assert method == "GET" and query["limit"] == ["100"]
            before = int(query.get("before", ["0"])[0])
            messages = []
            for m in sorted(self.imported, key=lambda m: m["id"], reverse=True):
                state = self.states.get((person["id"], m["id"]), {
                    "read": False, "hidden": False, "favorite": False, "version": 0})
                if before and m["id"] >= before:
                    continue
                if state["hidden"] and query.get("include_hidden") != ["true"]:
                    continue
                messages.append(dict(m, state=dict(state)))
            return Response({"messages": messages[:100]})
        assert method == "PATCH" and path.endswith("/state")
        mid = int(path.split("/")[3])
        state_key = (person["id"], mid)
        previous = self.states.get(state_key, {
            "read": False, "hidden": False, "favorite": False, "version": 0})
        if body["expected_version"] != previous["version"]:
            return Response({"error": "conflict"}, 409)
        updated = {k: body.get(k, previous[k]) for k in ("read", "hidden", "favorite")}
        updated["version"] = previous["version"] + 1
        self.states[state_key] = updated
        if self.fail_after_patch:
            raise URLError("ambiguous state mutation with private data")
        return Response(updated)


@unittest.skipIf(h is None, "implementation not yet present")
class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "secrets").mkdir(mode=0o700)
        for name, value in (("admin_password", "synthetic-admin-password"),
                            ("gotify_fixture_password", "synthetic-fixture-password")):
            path = self.root / "secrets" / name
            path.write_text(value + "\n")
            path.chmod(0o600)
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        self.http = FixtureHTTP()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(h, "ROOT", self.root))
        stack.enter_context(patch.object(h, "build_opener", return_value=self.http))
        stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network prohibited")))

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = h.main(list(args))
        output = out.getvalue() + err.getvalue()
        self.assertNotIn("synthetic-", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("http://", output)
        self.assertNotIn("https://", output)
        return code, output

    def state(self):
        return json.loads((self.root / "secrets/import_fixture.json").read_text())

    def test_full_two_stage_acceptance_and_sparse_id_mapping(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        state = self.state()
        self.assertEqual(len(state["messages"]), 205)
        self.assertEqual(state["messages"][0], {"sequence": 1, "gotify_id": 108})
        self.assertEqual(self.http.sessions, {})
        source = json.loads((self.root / "secrets/gotify_sources.json").read_text())
        self.assertEqual(source, {"sources": [{
            "key": state["source_key"], "url": "http://gotify-fixture:80", "user_id": 37,
            "topic_id": 55, "application_id": 83,
            "client_token_file": "/run/secrets/gotify_client_token", "allow_http": True}]})
        self.assertEqual((self.root / "secrets/gotify_client_token").read_text().strip(),
                         "synthetic-client-token")
        for name, mode in (("import_fixture.json", 0o600), ("gotify_client_token", 0o444),
                           ("gotify_sources.json", 0o444)):
            self.assertEqual(stat.S_IMODE((self.root / "secrets" / name).stat().st_mode), mode)
        self.http.import_messages()
        # Unrelated topics must not change the expected fixture count.
        self.http.imported.append(dict(self.http.imported[0], id=99999, topic_id=99))
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 0)
        self.assertEqual(self.invoke("seed-offline")[0], 0)
        self.assertEqual(len(self.http.upstream), 222)
        before = len(self.http.requests)
        self.assertEqual(self.invoke("seed-offline")[0], 1)
        self.assertEqual(len(self.http.requests), before)
        self.http.import_messages()
        self.assertEqual(self.invoke("verify", "--expected", "222")[0], 0)
        self.assertEqual(self.http.login_count, 7)
        self.assertEqual(self.http.sessions, {})
        alice = self.state()["people"]["alice"]
        self.assertEqual(self.http.states[(alice["id"], 10001)]["version"], 2)
        for expected in (205, 222):
            report = json.loads((self.root / "reports" / (
                "gotify-import-{}.json".format(expected))).read_text())
            self.assertEqual(report["expected"], expected)
            self.assertFalse(report["physicalphone"])
            self.assertFalse(report["push"])
            self.assertEqual(report["upstream_before"], report["upstream_after"])
            self.assertEqual(report["mapping"][0], {
                "sequence": 1, "gotify_id": 108, "platform_id": 10001})
            for word in ("password", "token", "synthetic-", "message\""):
                self.assertNotIn(word, json.dumps(report))

    def test_required_paths_checked_before_any_http(self):
        (self.root / "secrets/gotify_fixture_password").unlink()
        self.assertEqual(self.invoke("prepare")[0], 1)
        self.assertEqual(self.http.requests, [])
        self.assertFalse((self.root / "secrets/import_fixture.json").exists())

    def test_existing_or_symlink_output_prevents_mutations(self):
        for name in ("import_fixture.json", "gotify_client_token", "gotify_sources.json"):
            with self.subTest(name=name):
                path = self.root / "secrets" / name
                path.symlink_to(self.root / "absent")
                self.assertEqual(self.invoke("prepare")[0], 1)
                self.assertTrue(path.is_symlink())
                self.assertEqual(self.http.requests, [])
                path.unlink()

    def test_insecure_secrets_directory_and_symlink_input_rejected(self):
        directory = self.root / "secrets"
        directory.chmod(0o755)
        self.assertEqual(self.invoke("prepare")[0], 1)
        directory.chmod(0o700)
        password = directory / "admin_password"
        password.unlink()
        password.symlink_to(directory / "gotify_fixture_password")
        self.assertEqual(self.invoke("prepare")[0], 1)
        self.assertEqual(self.http.requests, [])

    def test_non_loopback_origins_and_bad_arguments_are_redacted(self):
        for option, url in (("--base", "http://localhost:18090"),
                            ("--gotify-base", "http://gotify-fixture:80"),
                            ("--base", "http://127.0.0.1:18090@production.invalid"),
                            ("--gotify-base", "http://127.0.0.1:18091/?token=synthetic-secret")):
            self.assertEqual(self.invoke("prepare", option, url)[0], 1)
        self.assertEqual(self.invoke("verify", "--expected", "synthetic-secret")[0], 1)
        self.assertEqual(self.http.requests, [])

    def test_wrong_directory_rejected(self):
        os.chdir(self.root.parent)
        self.assertEqual(self.invoke("prepare")[0], 1)
        self.assertEqual(self.http.requests, [])

    def test_failure_keeps_journal_and_never_replays_prepare(self):
        self.http.fail_message = 4
        self.assertEqual(self.invoke("prepare")[0], 1)
        state = self.state()
        self.assertEqual(len(state["messages"]), 3)
        self.assertIsNotNone(state["pending"])
        self.assertEqual(self.http.sessions, {})
        before = len(self.http.requests)
        self.assertEqual(self.invoke("prepare")[0], 1)
        self.assertEqual(self.invoke("seed-offline")[0], 1)
        self.assertEqual(len(self.http.requests), before)

    def test_user_creation_failure_logs_admin_out_and_retains_credentials(self):
        self.http.fail_user = True
        code, output = self.invoke("prepare")
        self.assertEqual(code, 1)
        self.assertNotIn("secret user failure", output)
        self.assertEqual(self.http.sessions, {})
        self.assertTrue(self.state()["people"]["alice"]["password"])
        self.assertIsNotNone(self.state()["pending"])

    def test_seed_partial_failure_cannot_duplicate_messages(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        self.http.fail_message = 209
        self.assertEqual(self.invoke("seed-offline")[0], 1)
        self.assertEqual(len(self.state()["messages"]), 208)
        before = len(self.http.requests)
        self.assertEqual(self.invoke("seed-offline")[0], 1)
        self.assertEqual(len(self.http.requests), before)

    def test_missing_import_or_corrupted_payload_cannot_pass(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertEqual(self.http.sessions, {})
        self.http.import_messages()
        self.http.imported[5]["message"] = "incorrect private body"
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertEqual(self.http.sessions, {})
        self.assertFalse((self.root / "reports/gotify-import-205.json").exists())

    def test_duplicate_title_and_duplicate_page_cannot_pass(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        self.http.import_messages()
        self.http.imported[-1]["title"] = "Import fixture 1"
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.http.import_messages()
        self.http.imported.append(copy.deepcopy(self.http.imported[-1]))
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)

    def test_upstream_deletion_is_detected_and_sessions_closed(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        self.http.import_messages()
        original = self.http.open

        def open_and_delete(req, timeout):
            result = original(req, timeout)
            if req.get_method() == "PATCH" and result.status == 200:
                self.http.upstream.pop()
            return result

        self.http.open = open_and_delete
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertEqual(self.http.sessions, {})
        self.assertFalse((self.root / "reports/gotify-import-205.json").exists())

    def test_existing_report_and_modified_config_fail_before_login(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        before = len(self.http.requests)
        report = self.root / "reports/gotify-import-205.json"
        report.write_text("preserve")
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertEqual(report.read_text(), "preserve")
        report.unlink()
        source = self.root / "secrets/gotify_sources.json"
        source.chmod(0o600)
        source.write_text('{"sources":[{"url":"https://production.invalid"}]}')
        source.chmod(0o444)
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertEqual(len(self.http.requests), before)

    def test_wrong_gotify_version_prevents_external_mutations(self):
        self.http.version = "2.8.0"
        self.assertEqual(self.invoke("prepare")[0], 1)
        self.assertTrue(all(req[1] == "GET" for req in self.http.requests))

    def test_logout_failure_preserves_sessions_and_prevents_success_report(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        self.http.import_messages()
        self.http.fail_logout = True
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertEqual(len(self.state()["sessions"]), 3)
        self.assertFalse((self.root / "reports/gotify-import-205.json").exists())

    def test_ambiguous_patch_is_journaled_and_all_sessions_closed(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        self.http.import_messages()
        self.http.fail_after_patch = True
        self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertIsNotNone(self.state()["pending"])
        self.assertEqual(self.http.sessions, {})

    def test_retries_respect_persistent_login_budget(self):
        self.assertEqual(self.invoke("prepare")[0], 0)
        for _ in range(5):
            self.assertEqual(self.invoke("verify", "--expected", "205")[0], 1)
        self.assertLessEqual(self.http.login_count, 10)
        self.assertEqual(self.http.sessions, {})

    def test_http_errors_do_not_read_or_print_bodies_and_redirects_disabled(self):
        with patch.object(h, "build_opener", wraps=h.__dict__["build_opener"]) as builder:
            client = h.HTTP(h.API_ORIGIN)
            handlers = builder.call_args.args
            self.assertTrue(any(isinstance(x, h.ProxyHandler) and x.proxies == {} for x in handlers))
            redirect = next(x for x in handlers if isinstance(x, h.NoRedirect))
            self.assertIsNone(redirect.redirect_request(None, None, 302, "", {},
                                                       "https://production.invalid"))
        raw = io.BytesIO(b"synthetic-secret-error-body")
        error = HTTPError("http://127.0.0.1:18090/?token=synthetic-secret", 302,
                          "synthetic-secret", {}, raw)
        with patch.object(self.http, "open", side_effect=error):
            with self.assertRaises(h.CheckFailed):
                client.call("GET", "/healthz")
        self.assertTrue(raw.closed)
        for path in ("//production.invalid/", "https://production.invalid/", "/x\nHost: bad"):
            with self.assertRaises(h.CheckFailed):
                client.call("GET", path)


if __name__ == "__main__":
    unittest.main()
