"""No network, remote commands or real credentials. Fresh private temp fixtures only."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import test_verify_gotify_import as original

SCRIPT = Path(__file__).with_name("prepare-android-render.py")
h = None
if SCRIPT.is_file():
    spec = importlib.util.spec_from_file_location("prepare_android_render", SCRIPT)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)


class RenderHTTP(original.FixtureHTTP):
    def __init__(self):
        super().__init__()
        self.render_posts = 0
        self.fail_render = False
        self.fail_render_login = False
        self.never_import = False
        self.bad_app = False
        self.polls = 0
        self.journal_path = None

    def open(self, req, timeout):
        url = urlsplit(req.full_url)
        method = req.get_method()
        body = json.loads(req.data) if req.data is not None else None
        if url.port == 18091 and url.path == "/current/user" and req.get_header("X-gotify-key"):
            return original.Response({"id": 37, "name": "admin", "admin": True})
        if url.port == 18091 and url.path == "/application" and method == "GET":
            return original.Response([{"id": 83, "name": "Import fixture " + self.fixture_run,
                                       "token": "wrong-app" if self.bad_app else "synthetic-app-token"}])
        if url.port == 18091 and url.path == "/message" and method == "POST" and body["title"].startswith("Render-"):
            journal = json.loads(self.journal_path.read_text())
            assert journal["pending"]["action"].startswith("message_")
            self.render_posts += 1
            if self.fail_render is True or self.fail_render == self.render_posts:
                raise URLError("synthetic-sensitive-value")
            message = dict(body, id=5000 + self.render_posts, appid=83)
            self.upstream.append(message)
            return original.Response(message)
        if url.port == 18090 and url.path == "/healthz":
            return original.Response({"status": "ok", "version": "0.2.0-internal"})
        if url.port == 18090 and url.path == "/v1/login" and self.journal_path and self.journal_path.exists():
            journal = json.loads(self.journal_path.read_text())
            assert journal["pending"]["action"] == "login"
            if self.fail_render_login:
                self.login_count += 1
                raise URLError("synthetic-sensitive-value")
        if url.port == 18090 and url.path == "/v1/me":
            token = req.get_header("Authorization")[7:]
            if token not in self.sessions:
                return original.Response({}, 401)
            return original.Response({"person": self.sessions[token]})
        if url.port == 18090 and url.path == "/v1/topics":
            return original.Response({"topics": [{"id": 55, "name": "Import fixture " + self.fixture_run}]})
        if url.port == 18090 and url.path.startswith("/v1/messages"):
            self.polls += 1
            if not self.never_import:
                self.import_messages()
            rows = [dict(m, content_type="text/markdown" if "extras" in m else "text/plain")
                    for m in self.imported]
            if url.path != "/v1/messages":
                mid = int(url.path.rsplit("/", 1)[1])
                return original.Response({"message": next(m for m in rows if m["id"] == mid)})
            before = int(parse_qs(url.query).get("before", [0])[0])
            rows = sorted((m for m in rows if not before or m["id"] < before), key=lambda m: m["id"], reverse=True)
            return original.Response({"messages": rows[:100]})
        return super().open(req, timeout)


class PresentTests(unittest.TestCase):
    def test_preparation_exists(self):
        self.assertTrue(SCRIPT.is_file(), "one-shot preparation not implemented")


@unittest.skipIf(h is None, "implementation pending")
class PreparationTests(unittest.TestCase):
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
        old = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.http = RenderHTTP()
        for module in (original.h, h.fixture):
            stack.enter_context(patch.object(module, "ROOT", self.root))
            stack.enter_context(patch.object(module, "build_opener", return_value=self.http))
        stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network prohibited")))
        stack.enter_context(patch.object(h.time, "sleep"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(original.h.main(["prepare"]), 0)
        self.original = (self.root / "secrets/import_fixture.json").read_bytes()
        self.http.fixture_run = json.loads(self.original)["run_id"]
        self.http.journal_path = self.root / "secrets/android-render/journal.json"

    def run_prepare(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = h.main(["--allow-isolated-test-mutations", *extra])
        for marker in ("synthetic-", "Traceback", "http://", self.http.fixture_run):
            self.assertNotIn(marker, out.getvalue())
        self.assertTrue((self.root / "secrets/import_fixture.json").read_bytes() == self.original)
        return code

    def journal(self):
        return json.loads(self.http.journal_path.read_text())

    def test_exactly_two_messages_real_import_reads_and_private_handoff(self):
        self.assertEqual(self.run_prepare(), 0)
        self.assertEqual(self.http.render_posts, 2)
        self.assertFalse(self.http.sessions)
        self.assertIsNone(self.journal()["pending"])
        handoff = self.root / "secrets/android-render/input.json"
        doc = json.loads(handoff.read_text())
        self.assertEqual([m["content_type"] for m in doc["messages"]], ["text/markdown", "text/plain"])
        self.assertEqual([m["gotify_id"] for m in doc["messages"]], [5001, 5002])
        self.assertEqual(len({m["id"] for m in doc["messages"]}), 2)
        self.assertTrue(doc["admin_password"] == "synthetic-admin-password")
        self.assertFalse(any(key in handoff.read_text() for key in ("synthetic-app-token", "synthetic-client-token")))
        for path in (handoff, self.http.journal_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(handoff.parent.stat().st_mode), 0o700)

    def test_refuses_successful_run_reuse_without_any_new_mutation(self):
        self.assertEqual(self.run_prepare(), 0)
        attempts = self.http.login_count
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.render_posts, 2)
        self.assertEqual(self.http.login_count, attempts)

    def test_ambiguous_create_recorded_no_retry_and_no_handoff(self):
        self.http.fail_render = True
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.journal()["pending"]["action"], "message_markdown")
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.render_posts, 1)
        self.assertFalse(self.http.sessions)
        self.assertFalse((self.root / "secrets/android-render/input.json").exists())

    def test_ambiguous_login_recorded_and_not_retried(self):
        attempts = self.http.login_count
        self.http.fail_render_login = True
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.journal()["pending"]["action"], "login")
        self.assertEqual(self.http.login_count, attempts + 1)
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.login_count, attempts + 1)

    def test_wrong_version_or_application_prevents_messages(self):
        for field, value in (("version", "2.9.0"), ("bad_app", True)):
            with self.subTest(field=field):
                setattr(self.http, field, value)
                self.assertEqual(self.run_prepare(), 1)
                self.assertEqual(self.http.render_posts, 0)
                setattr(self.http, field, "2.9.1" if field == "version" else False)

    def test_import_wait_is_bounded_and_never_reseeds(self):
        self.http.never_import = True
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.render_posts, 2)
        self.assertLessEqual(self.http.polls, 60 * 10)
        self.assertFalse(self.http.sessions)

    def test_logout_failure_fails_without_handoff(self):
        self.http.fail_logout = True
        self.assertEqual(self.run_prepare(), 1)
        self.assertTrue(self.journal()["sessions"])
        self.assertFalse((self.root / "secrets/android-render/input.json").exists())

    def test_non_loopback_and_credential_argv_refused(self):
        self.assertEqual(self.run_prepare("--api", "http://remote.invalid"), 1)
        self.assertEqual(self.run_prepare("--password=synthetic-sensitive-value"), 1)
        self.assertEqual(self.http.render_posts, 0)

    def test_original_permission_and_source_guards_remain_active(self):
        path = self.root / "secrets/import_fixture.json"
        path.chmod(0o644)
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.render_posts, 0)

    def test_fixture_origin_mismatch_refused_before_mutation(self):
        state = json.loads(self.original)
        state["gotify_origin"] = "http://remote.invalid"
        self.original = json.dumps(state).encode()
        (self.root / "secrets/import_fixture.json").write_bytes(self.original)
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.render_posts, 0)

    def test_source_mismatch_refused_before_mutation(self):
        source = self.root / "secrets/gotify_sources.json"
        source.chmod(0o600)
        source.write_text('{"sources": []}')
        source.chmod(0o444)
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.render_posts, 0)

    def test_second_create_ambiguity_preserves_first_message_without_replay(self):
        self.http.fail_render = 2
        self.assertEqual(self.run_prepare(), 1)
        journal = self.journal()
        self.assertEqual(len(journal["messages"]), 1)
        self.assertEqual(journal["pending"]["action"], "message_plain")
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.render_posts, 2)
        self.assertFalse(self.http.sessions)

    def test_handoff_is_accepted_by_local_input_contract_without_upstream_credentials(self):
        self.assertEqual(self.run_prepare(), 0)
        local_dir = SCRIPT.parents[2] / "android/testing"
        spec = importlib.util.spec_from_file_location("render_local_contract", local_dir / "gotify_render_acceptance.py")
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(local_dir)] + sys.path):
            spec.loader.exec_module(module)
        doc = json.loads((self.root / "secrets/android-render/input.json").read_text())
        result = module.validate_document(doc)
        self.assertEqual(result["fixture"]["topic_id"], 55)

    def test_wrong_working_directory_refused_before_mutation(self):
        child = self.root / "different"
        child.mkdir()
        os.chdir(child)
        try:
            self.assertEqual(self.run_prepare(), 1)
        finally:
            os.chdir(self.root)
        self.assertEqual(self.http.render_posts, 0)

    def test_existing_symlink_run_directory_is_not_followed(self):
        target = self.root / "unrelated"
        target.mkdir()
        (self.root / "secrets/android-render").symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_prepare(), 1)
        self.assertFalse(list(target.iterdir()))
        self.assertEqual(self.http.render_posts, 0)

    def test_prior_login_budget_refused_without_attempt(self):
        state = json.loads(self.original)
        state["login_attempts"] = [h.time.time()] * 10
        self.original = json.dumps(state).encode()
        (self.root / "secrets/import_fixture.json").write_bytes(self.original)
        attempts = self.http.login_count
        self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(self.http.login_count, attempts)
        self.assertEqual(self.http.render_posts, 0)

    def test_missing_consent_never_opens_layout_or_mutates(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(h.main([]), 1)
        self.assertFalse(self.http.journal_path.exists())
        self.assertEqual(self.http.render_posts, 0)


if __name__ == "__main__":
    unittest.main()
