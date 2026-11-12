"""Offline acceptance guards; all credentials and API/UI fixtures are synthetic."""
import contextlib
import copy
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import deployed_acceptance as base

SCRIPT = Path(__file__).with_name("gotify_render_acceptance.py")
h = None
if SCRIPT.is_file():
    spec = importlib.util.spec_from_file_location("gotify_render_acceptance", SCRIPT)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)


def document():
    run = "0123456789abcdef"
    return {"schema": 1, "api_origin": base.ORIGIN,
            "admin_password": "synthetic-admin-password", "run_id": run,
            "fixture": {"topic_id": 55, "topic_name": "Import fixture test-run",
                        "source_key": "fixture-test-run", "application_id": 83,
                        "original_count": 205},
            "messages": [{"id": 900 + i, "gotify_id": 300 + i, "topic_id": 55,
                          "title": "Render-" + run + "-" + kind,
                          "message": "**Render " + run + "**", "priority": 9,
                          "content_type": "text/" + kind}
                         for i, kind in enumerate(("markdown", "plain"))]}


def api_rows(doc):
    rows = [dict(m) for m in reversed(doc["messages"])]
    for i in range(doc["fixture"]["original_count"], 0, -1):
        rows.append({"id": i, "topic_id": 55, "title": "Import fixture %d" % i,
                     "message": "# Import fixture %d\n\n**\u771f\u5b9e\u5bfc\u5165\u9a8c\u6536**\n\n\u6d88\u606f\u5e8f\u53f7: %d\n" % (i, i),
                     "content_type": "text/markdown", "priority": 8 + i % 2})
    for row in rows:
        row.pop("gotify_id", None)
        row.update(created_at="2026-11-12T00:00:00Z",
                   state={"read": False, "hidden": False, "favorite": False, "version": 0})
    return rows


class FakeAPI:
    def __init__(self, doc):
        self.rows = api_rows(doc)
        self.calls, self.sessions = [], {}
        self.username = None
        self.enabled = True
        self.bad_identity = False
        self.lost_create = False
        self.lost_login = False
        self.fail_disable = False
        self.bad_detail = False

    def request(self, method, path, token="", body=None, expected=(200,)):
        self.calls.append((method, path))
        if path == "/healthz":
            return {"status": "ok", "version": "0.2.0-internal"}
        if path == "/v1/login":
            admin = body["username"] == "admin"
            person = {"id": 1 if admin else 77, "username": body["username"], "admin": admin}
            key = "synthetic-root-session" if admin else "synthetic-user-session"
            self.sessions[key] = person
            if not admin and self.lost_login:
                raise base.Failure("http_transport")
            return {"token": key, "person": person}
        if path == "/v1/me":
            if token not in self.sessions or (token == "synthetic-user-session" and not self.enabled):
                if 401 not in expected:
                    raise base.Failure("http_status")
                return {}
            if 200 not in expected:
                raise base.Failure("http_status")
            person = dict(self.sessions[token])
            if self.bad_identity and not person["admin"]:
                person["username"] = "unrelated-person"
            return {"person": person}
        if path == "/v1/session":
            self.sessions.pop(token, None)
            return {"ok": True}
        if path == "/v1/admin/users":
            self.username = body["username"]
            if self.lost_create:
                raise base.Failure("http_transport")
            return {"id": 77}
        if path.startswith("/v1/admin/users/"):
            if path != "/v1/admin/users/77" or self.fail_disable:
                raise base.Failure("http_status")
            self.enabled = False
            return {"ok": True}
        if path.startswith("/v1/admin/topics/55/members/"):
            return {"ok": True}
        if path == "/v1/topics":
            return {"topics": [{"id": 55, "name": "Import fixture test-run"}]}
        if path.startswith("/v1/messages?"):
            before = int(path.split("before=")[1].split("&")[0])
            return {"messages": [m for m in self.rows if not before or m["id"] < before][:100]}
        if path.startswith("/v1/messages/"):
            row = dict(next(m for m in self.rows if m["id"] == int(path.rsplit("/", 1)[1])))
            if self.bad_detail:
                row["content_type"] = "text/plain"
            return {"message": row}
        raise AssertionError("unexpected synthetic API route")


class PresentTests(unittest.TestCase):
    def test_render_harness_exists(self):
        self.assertTrue(SCRIPT.is_file(), "render harness not implemented")


@unittest.skipIf(h is None, "implementation pending")
class InputTests(unittest.TestCase):
    def test_valid_bounded_synthetic_document(self):
        result = h.validate_document(document())
        self.assertEqual(result["fixture"]["topic_id"], 55)

    def test_origins_and_extra_secret_fields_refused(self):
        for origin in ("http://localhost:18090", base.ORIGIN + "/", "https://remote.invalid"):
            doc = document()
            doc["api_origin"] = origin
            with self.assertRaises(base.Failure):
                h.validate_document(doc)
        for field in ("upstream_token", "journal", "sessions"):
            doc = document()
            doc[field] = "synthetic-sensitive-value"
            with self.assertRaises(base.Failure):
                h.validate_document(doc)

    def test_passwords_reject_controls_and_byte_overflow(self):
        for password in ("short", "a" * 73, "a" * 11 + "\n", "a" * 12 + "\x00", "\u00e9" * 37):
            doc = document()
            doc["admin_password"] = password
            with self.assertRaises(base.Failure):
                h.validate_document(doc)

    def test_forged_or_nonsynthetic_metadata_refused(self):
        for key, value in (("id", True), ("gotify_id", 0), ("topic_id", 56),
                           ("title", "private title"), ("message", "private body"),
                           ("content_type", "text/html"), ("priority", True)):
            doc = document()
            doc["messages"][0][key] = value
            with self.assertRaises(base.Failure):
                h.validate_document(doc)
        doc = document()
        doc["messages"][1]["id"] = doc["messages"][0]["id"]
        with self.assertRaises(base.Failure):
            h.validate_document(doc)

    def pipe(self, raw):
        read, write = os.pipe()
        with os.fdopen(write, "wb") as stream:
            stream.write(raw)
        with os.fdopen(read, "rb") as stream:
            return h.read_document(stream)

    def test_pipe_document_round_trip(self):
        self.assertEqual(self.pipe(json.dumps(document()).encode())["run_id"], "0123456789abcdef")

    def test_duplicate_keys_trailing_input_invalid_json_refused(self):
        for raw in (b'{"schema":1,"schema":1}', b'{} {}', b'\xff', b'[]', b'{'):
            with self.assertRaises(base.Failure):
                self.pipe(raw)

    def test_regular_file_input_refused(self):
        with tempfile.TemporaryFile() as stream, self.assertRaises(base.Failure):
            h.read_document(stream)

    def test_input_size_and_timeout_are_bounded(self):
        with patch.object(h.os, "read", return_value=b"x" * (h.INPUT_LIMIT + 1)), \
                self.assertRaises(base.Failure):
            self.pipe(b"x")
        with patch.object(h.select, "select", return_value=([], [], [])), \
                self.assertRaises(base.Failure):
            self.pipe(b"x")

    def test_cli_rejects_credential_arguments_without_echo(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(h.main(["--password=synthetic-sensitive-value"]), 1)
        self.assertNotIn("synthetic-sensitive-value", out.getvalue())


@unittest.skipIf(h is None, "implementation pending")
class RowTests(unittest.TestCase):
    def screen(self):
        root = ET.Element("hierarchy")
        ET.SubElement(root, "node", {"content-desc": "\u5237\u65b0", "enabled": "true"})
        for i, msg in enumerate(document()["messages"]):
            row = ET.SubElement(root, "node", {"package": base.Device.package})
            body = "Render 0123456789abcdef" if i == 0 else "**Render 0123456789abcdef**"
            for name, text in (("message_title", msg["title"]), ("message_state", "\u672a\u8bfb"),
                               ("message_text", body)):
                ET.SubElement(row, "node", {"resource-id": base.Device.package + ":id/" + name,
                                          "text": text, "class": "android.widget.TextView",
                                          "package": base.Device.package})
        return root

    def check(self, root):
        return h.rendered_rows(list(root.iter("node")), document()["messages"])

    def test_exact_markdown_and_plain_same_row(self):
        self.check(self.screen())

    def test_markdown_raw_or_plain_rendered_fails(self):
        for index, body in ((1, "**Render 0123456789abcdef**"), (2, "Render 0123456789abcdef")):
            root = self.screen()
            root[index][2].set("text", body)
            with self.assertRaises(base.Failure):
                self.check(root)

    def test_other_row_cannot_supply_expected_body(self):
        root = self.screen()
        root[1][2].set("text", "wrong")
        ET.SubElement(root, "node", {"text": "Render 0123456789abcdef"})
        with self.assertRaises(base.Failure):
            self.check(root)

    def test_missing_duplicate_or_nested_body_refused(self):
        for mode in ("missing", "duplicate", "nested", "duplicate_title"):
            root = self.screen()
            if mode == "missing":
                root[1].remove(root[1][2])
            elif mode == "duplicate":
                root[1].append(copy.deepcopy(root[1][2]))
            elif mode == "nested":
                body = root[1][2]
                root[1].remove(body)
                ET.SubElement(root[1], "node").append(body)
            else:
                root.append(copy.deepcopy(root[1]))
            with self.assertRaises(base.Failure):
                self.check(root)

    def test_login_modal_input_and_nonready_screen_refused(self):
        for attrs in ({"text": "\u767b\u5f55 zgotify"}, {"class": "android.widget.EditText"},
                      {"password": "true"}):
            root = self.screen()
            ET.SubElement(root, "node", attrs)
            with self.assertRaises(base.Failure):
                self.check(root)
        root = self.screen()
        root[0].set("enabled", "false")
        with self.assertRaises(base.Failure):
            self.check(root)

    def test_foreign_package_cannot_supply_a_message_row(self):
        root = self.screen()
        root[1][2].set("package", "unrelated.app")
        with self.assertRaises(base.Failure):
            self.check(root)


@unittest.skipIf(h is None, "implementation pending")
class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.doc = document()
        args = SimpleNamespace(api=base.ORIGIN, serial="emulator-5580", adb="adb",
                               output=str(Path(self.temp.name) / "new-output"),
                               allow_isolated_test_mutations=True, input_stdin=True)
        self.harness = h.RenderHarness(args)
        self.api = FakeAPI(self.doc)
        self.harness.api = self.api
        self.addCleanup(patch.stopall)
        patch.object(socket, "socket", side_effect=AssertionError("network prohibited")).start()
        patch.object(base.subprocess, "run", side_effect=AssertionError("adb prohibited")).start()

    def test_only_authorized_serial_and_consent(self):
        for key, value in (("serial", "emulator-5582"), ("api", "http://localhost:18090"),
                           ("allow_isolated_test_mutations", False), ("input_stdin", False)):
            args = SimpleNamespace(api=base.ORIGIN, serial="emulator-5580", adb="adb", output="unused",
                                   allow_isolated_test_mutations=True, input_stdin=True)
            setattr(args, key, value)
            with self.assertRaises(base.Failure):
                h.RenderHarness(args)

    def test_list_detail_and_only_fixture_membership(self):
        self.harness.setup(self.doc)
        self.assertEqual(self.harness.inbox_count, 207)
        puts = [path for method, path in self.api.calls if method == "PUT"]
        self.assertEqual(puts, ["/v1/admin/topics/55/members/77"])
        self.assertEqual(self.harness.cleanup(), [])
        self.assertFalse(self.api.enabled)
        self.assertFalse(self.api.sessions)

    def test_wrong_detail_content_type_fails(self):
        self.api.bad_detail = True
        with self.assertRaises(base.Failure):
            self.harness.setup(self.doc)
        self.assertEqual(self.harness.cleanup(), [])

    def test_unrelated_message_refuses_evidence(self):
        self.api.rows[-1]["message"] = "not synthetic"
        with self.assertRaises(base.Failure):
            self.harness.setup(self.doc)
        self.assertEqual(self.harness.cleanup(), [])

    def test_mismatched_identity_is_never_disabled(self):
        self.api.bad_identity = True
        with self.assertRaises(base.Failure):
            self.harness.setup(self.doc)
        self.assertIn("disable_account", self.harness.cleanup())
        self.assertFalse(any(method == "PATCH" for method, _ in self.api.calls))

    def test_lost_create_recovers_identity_with_first_login_only(self):
        self.api.lost_create = True
        with self.assertRaises(base.Failure):
            self.harness.setup(self.doc)
        self.assertEqual(self.harness.cleanup(), [])
        self.assertEqual(self.api.calls.count(("POST", "/v1/admin/users")), 1)
        self.assertEqual(self.api.calls.count(("POST", "/v1/login")), 2)
        self.assertFalse(self.api.enabled)

    def test_lost_login_is_never_retried_even_in_cleanup(self):
        self.api.lost_login = True
        with self.assertRaises(base.Failure):
            self.harness.setup(self.doc)
        self.assertIn("disable_account", self.harness.cleanup())
        self.assertEqual(self.api.calls.count(("POST", "/v1/login")), 2)
        self.assertFalse(any(method == "PATCH" for method, _ in self.api.calls))

    def test_cleanup_failure_fails_run_and_still_revokes_sessions(self):
        self.api.fail_disable = True
        with patch.object(self.harness, "preflight"), \
                patch.object(self.harness, "workflow", side_effect=lambda doc: self.harness.setup(doc)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.harness.execute(self.doc), 1)
        self.assertFalse(self.api.sessions)
        self.assertNotIn(self.doc["admin_password"], out.getvalue())

    def test_existing_output_refused_before_device_or_mutation(self):
        self.harness.output.mkdir()
        with self.assertRaises(base.Failure) as caught:
            self.harness.preflight()
        self.assertEqual(caught.exception.code, "output_not_new")
        self.assertFalse(self.api.calls)

    def first_page_workflow(self, original_count):
        self.doc["fixture"]["original_count"] = original_count
        self.api.rows = api_rows(self.doc)
        device = self.harness.devices[0]
        nodes = [ET.Element("node", {"content-desc": "\u5237\u65b0", "enabled": "true"}),
                 ET.Element("node", {"text": "\u5728\u7ebf \u00b7 100 \u6761 \u00b7 \u5f85\u540c\u6b65 0 \u00b7 \u5f85\u5904\u7406 0"})]

        def login(account, budget, expected_inbox_count):
            # Replace native input only; exercise the real inbox predicate and wait.
            device.inbox(expected_inbox_count)

        with patch.object(device, "login", side_effect=login), \
                patch.object(device, "nodes", return_value=nodes), \
                patch.object(base.time, "monotonic", side_effect=itertools.count()), \
                patch.object(base.time, "sleep"), patch.object(device, "restart"), \
                patch.object(self.harness, "reverse"), patch.object(self.harness, "capture"):
            try:
                self.harness.workflow(self.doc)
            except base.Failure as error:
                self.fail("100-row first page was rejected: " + error.code)
        self.assertEqual(self.harness.inbox_count, original_count + 2)
        self.assertEqual(self.harness.checks, list(h.CHECKS))
        self.assertEqual(self.harness.cleanup(), [])

    def test_workflow_accepts_first_page_status_for_207_records(self):
        self.first_page_workflow(205)

    def test_workflow_accepts_first_page_status_for_224_records(self):
        self.first_page_workflow(222)

    def test_list_rejects_boolean_id_duplicate_id_and_wrong_topic(self):
        for key, value in (("id", True), ("id", 901), ("topic_id", 56), ("priority", True)):
            self.api.rows = api_rows(self.doc)
            self.api.rows[1][key] = value
            self.harness.topic_id = 55
            with self.assertRaises(base.Failure):
                self.harness.verify_messages(self.doc, "synthetic-user-session")

    def test_cleanup_removes_only_owned_reverse_even_when_disable_fails(self):
        self.harness.setup(self.doc)
        self.api.fail_disable = True
        device = self.harness.devices[0]
        self.harness.reverse_devices = [device]
        with patch.object(device, "remove_reverse", side_effect=base.Failure("adb_failed")):
            failures = self.harness.cleanup()
        self.assertIn("disable_account", failures)
        self.assertIn("remove_reverse", failures)
        self.assertFalse(self.api.sessions)

    def test_cleanup_does_not_touch_unowned_reverse(self):
        self.harness.setup(self.doc)
        with patch.object(self.harness.devices[0], "remove_reverse", side_effect=AssertionError("not owned")):
            self.assertEqual(self.harness.cleanup(), [])

    def test_preflight_checks_apk_version_separate_from_api_version(self):
        with patch.object(self.harness.devices[0], "preflight"), \
                patch.object(self.harness.devices[0], "adb", return_value=b"versionName=0.2.0-internal"), \
                self.assertRaises(base.Failure) as caught:
            self.harness.preflight()
        self.assertEqual(caught.exception.code, "backend_version")
        self.assertFalse(self.api.calls)

    def capture(self, root, authenticated=True):
        self.harness.output.mkdir(exist_ok=True)
        self.harness.accounts = [base.Account("renderer", "synthetic-user", "synthetic-password", 77)]
        device = self.harness.devices[0]
        device.authenticated = authenticated
        with patch.object(device, "active_identity"), \
                patch.object(device, "nodes", return_value=list(root.iter("node"))), \
                patch.object(device, "adb", return_value=b"\x89PNG\r\n\x1a\nsynthetic-png"):
            self.harness.capture(self.doc)

    def test_screenshot_permits_verified_historical_synthetic_rows(self):
        root = RowTests().screen()
        historical = copy.deepcopy(root[1])
        historical[0].set("text", "Import fixture 205")
        historical[2].set("text", "Import fixture 205\n\n\u771f\u5b9e\u5bfc\u5165\u9a8c\u6536\n\n\u6d88\u606f\u5e8f\u53f7: 205")
        root.append(historical)
        self.capture(root)
        path = self.harness.output / "gotify-render-emulator-5580.png"
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def historical_fragment(self, visible_children=1):
        root = RowTests().screen()
        historical = copy.deepcopy(root[1])
        historical[0].set("text", "Import fixture 205")
        for child in list(historical)[visible_children:]:
            historical.remove(child)
        root.append(historical)
        return root

    def capture_fragment(self, visible_children):
        try:
            self.capture(self.historical_fragment(visible_children))
        except base.Failure as error:
            self.fail("identified historical clipping was rejected: " + error.code)
        self.assertTrue((self.harness.output / "gotify-render-emulator-5580.png").exists())

    def test_screenshot_permits_title_only_historical_fragment(self):
        self.capture_fragment(1)

    def test_screenshot_permits_title_state_historical_fragment(self):
        self.capture_fragment(2)

    def test_historical_clipping_never_relaxes_either_target_row(self):
        for target in (1, 2):
            for mode in ("missing_title", "missing_state", "missing_body", "duplicate_body",
                         "duplicate_title", "nested_body", "other_row_body", "foreign_package", "foreign_id"):
                with self.subTest(target=target, mode=mode):
                    root = self.historical_fragment()
                    row = root[target]
                    if mode.startswith("missing_"):
                        row.remove(row[{"missing_title": 0, "missing_state": 1, "missing_body": 2}[mode]])
                    elif mode == "duplicate_body":
                        row.append(copy.deepcopy(row[2]))
                    elif mode == "duplicate_title":
                        root.append(copy.deepcopy(row))
                    elif mode in ("nested_body", "other_row_body"):
                        body = row[2]
                        row.remove(body)
                        if mode == "nested_body":
                            ET.SubElement(row, "node").append(body)
                        else:
                            root[-1].append(body)
                    elif mode == "foreign_package":
                        row[2].set("package", "unrelated.app")
                    else:
                        row[2].set("resource-id", "unrelated.app:id/message_text")
                    with self.assertRaises(base.Failure):
                        self.capture(root)
                    self.assertFalse((self.harness.output / "gotify-render-emulator-5580.png").exists())

    def test_partial_unknown_or_out_of_range_history_refused(self):
        for title in ("unrelated message", "Import fixture 0", "Import fixture 206"):
            root = self.historical_fragment()
            root[-1][0].set("text", title)
            with self.assertRaises(base.Failure):
                self.capture(root)

    def test_partial_history_with_foreign_package_or_duplicate_title_refused(self):
        for mode in ("foreign_title", "foreign_state", "duplicate_title"):
            root = self.historical_fragment(2)
            if mode == "duplicate_title":
                root[-1].append(copy.deepcopy(root[-1][0]))
            else:
                root[-1][0 if mode == "foreign_title" else 1].set("package", "unrelated.app")
            with self.assertRaises(base.Failure):
                self.capture(root)

    def test_unidentified_historical_fragment_refused(self):
        root = self.historical_fragment(2)
        root[-1].remove(root[-1][0])
        with self.assertRaises(base.Failure):
            self.capture(root)

    def test_historical_fragment_does_not_hide_login_or_input(self):
        for attrs in ({"text": "\u767b\u5f55 zgotify"}, {"class": "android.widget.EditText"},
                      {"password": "true"}):
            root = self.historical_fragment()
            ET.SubElement(root, "node", attrs)
            with self.assertRaises(base.Failure):
                self.capture(root)

    def test_screenshot_refuses_login_and_unknown_row_before_writing(self):
        for mode in ("unauthenticated", "login", "unknown_row"):
            root = RowTests().screen()
            if mode == "login":
                ET.SubElement(root, "node", {"password": "true"})
            elif mode == "unknown_row":
                extra = copy.deepcopy(root[1])
                extra[0].set("text", "private message")
                root.append(extra)
            with self.assertRaises(base.Failure):
                self.capture(root, authenticated=mode != "unauthenticated")
            self.assertFalse((self.harness.output / "gotify-render-emulator-5580.png").exists())

    def test_screenshot_never_overwrites_existing_evidence(self):
        self.capture(RowTests().screen())
        with self.assertRaises(FileExistsError):
            self.capture(RowTests().screen())

    def test_success_report_contains_no_credentials_and_cleanup_is_verified(self):
        self.harness.output.mkdir()
        self.harness.output_ready = True

        def workflow(doc):
            self.harness.setup(doc)
            self.harness.checks.extend(("markdown_row", "plain_literal_row"))

        with patch.object(self.harness, "preflight"), patch.object(self.harness, "workflow", side_effect=workflow), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.harness.execute(self.doc), 0)
        report = (self.harness.output / "result.json").read_text()
        for value in (self.doc["admin_password"], self.harness.accounts[0].password,
                      self.harness.accounts[0].username, "synthetic-root-session", "synthetic-user-session"):
            self.assertTrue(value not in report and value not in out.getvalue(), "credential leaked")
        self.assertFalse(self.api.enabled)
        self.assertFalse(self.api.sessions)


if __name__ == "__main__":
    unittest.main()
