"""Local guard tests only; no server, adb, credentials, or deployment required."""
import contextlib
import io
import json
import os
import stat
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, call, patch

import deployed_acceptance as acceptance


class GuardTests(unittest.TestCase):
    def test_origin_is_exact_not_merely_a_loopback_hostname(self):
        self.assertEqual(acceptance.validate_origin("http://127.0.0.1:18090"),
                         "http://127.0.0.1:18090")
        for origin in ("http://localhost:18090", "http://127.0.0.1:18091",
                       "https://127.0.0.1:18090", "http://127.0.0.1:18090/",
                       "http://127.0.0.1:18090@remote", "http://remote:18090",
                       "http://user:secret@127.0.0.1:18090", "http://[::1]:18090",
                       "http://127.0.0.1:18090?x=y", "http://127.0.0.1:18090#x",
                       " http://127.0.0.1:18090", "http://127.0.0.1:18090\n"):
            with self.subTest(origin=origin), self.assertRaises(acceptance.Failure):
                acceptance.validate_origin(origin)

    def test_only_the_two_authorized_serials_in_their_roles(self):
        acceptance.validate_serials("emulator-5580", "emulator-5582")
        for pair in (("phone", "emulator-5582"), ("emulator-5554", "emulator-5582"),
                     ("emulator-5580", "emulator-5580"),
                     ("emulator-5582", "emulator-5580")):
            with self.subTest(pair=pair), self.assertRaises(acceptance.Failure):
                acceptance.validate_serials(*pair)

    def test_empty_preferences_are_accepted(self):
        acceptance.require_empty_profiles(b"<map/>")
        acceptance.require_empty_profiles(b'<map><string name="profiles">[]</string></map>')

    def test_existing_damaged_or_ambiguous_profiles_are_refused(self):
        for raw in (b'<map><string name="profiles">[{"token":"secret"}]</string></map>',
                    b'<map><string name="active">old</string></map>',
                    b'<map><string name="profiles">null</string></map>',
                    b'<map><string name="profiles">broken</string></map>',
                    b'<map><string name="profiles">[]</string><string name="profiles">[]</string></map>',
                    b'<wrong/>', b'', b'not XML'):
            with self.subTest(raw=raw), self.assertRaises(acceptance.Failure):
                acceptance.require_empty_profiles(raw)

    def test_adb_failure_never_exposes_command_output_or_secret(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        error = subprocess.CalledProcessError(1, ["secret"], b"secret", b"secret")
        with patch.object(acceptance.subprocess, "run", side_effect=error), \
                self.assertRaises(acceptance.Failure) as caught:
            device.adb("shell", "input", "text", "secret")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(acceptance.failure_code(caught.exception), "adb_failed")

    def test_untrusted_error_text_is_not_evidence(self):
        for error in (RuntimeError("password=secret"),
                      acceptance.Failure("password=secret")):
            self.assertEqual(acceptance.failure_code(error), "internal_failure")

    def test_login_budget_includes_ui_and_api_and_failed_attempts(self):
        budget = acceptance.LoginBudget()
        for _ in range(10):
            budget.claim(" Alice ")
        with self.assertRaises(acceptance.Failure):
            budget.claim("alice")
        budget.claim("bob")
        self.assertEqual(budget.count("ALICE"), 10)
        self.assertEqual(budget.count("bob"), 1)

    def test_credentials_have_safe_lengths_and_no_repr_secrets(self):
        one, two = acceptance.new_accounts()
        self.assertNotEqual(one.username, two.username)
        for account in (one, two):
            self.assertRegex(account.username, r"^[a-z0-9][a-z0-9._-]{2,63}$")
            self.assertGreaterEqual(len(account.password), 12)
            self.assertLessEqual(len(account.password), 72)
            self.assertNotIn(account.password, repr(account))
            self.assertNotIn(account.username, repr(account))

    def test_physical_device_cannot_spoof_just_the_serial(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        with patch.object(device, "adb", side_effect=[b"device\n", b"0\n"]) as adb:
            with self.assertRaises(acceptance.Failure):
                device.preflight()
        self.assertFalse(any("reverse" in c.args for c in adb.call_args_list))

    def test_preflight_does_not_clear_profiles_or_touch_reverse(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        with patch.object(device, "adb", side_effect=[b"device", b"1", b"36", b""]) as adb, \
                patch.object(device, "profile_xml", return_value=b'<map><string name="profiles">[{}]</string></map>'):
            with self.assertRaises(acceptance.Failure):
                device.preflight()
        calls = adb.call_args_list
        self.assertFalse(any("clear" in c.args or "--remove" in c.args or "start" in c.args for c in calls))

    def test_existing_reverse_is_not_adopted_or_removed(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        with patch.object(device, "adb", side_effect=[b"device", b"1", b"36",
                                                     b"host-1 tcp:18090 tcp:9999\n"]):
            with self.assertRaises(acceptance.Failure):
                device.preflight()

    def test_screenshot_refuses_password_or_login_fields(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        device.authenticated = True
        nodes = [ET.Element("node", {"class": "android.widget.EditText", "password": "true"}),
                 ET.Element("node", {"content-desc": "\u5237\u65b0", "enabled": "true"})]
        with patch.object(device, "nodes", return_value=nodes), patch.object(device, "adb") as adb:
            self.assertFalse(device.screenshot("alice_same_person_two_devices"))
            adb.assert_not_called()

    def test_ui_timeout_never_dumps_login_nodes(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        nodes = [ET.Element("node", {"text": "secret-password", "password": "true"})]
        with patch.object(device, "nodes", return_value=nodes), \
                patch.object(acceptance.time, "monotonic", side_effect=[0, 1, 46]), \
                patch.object(acceptance.time, "sleep"), \
                patch.object(device, "screenshot") as screenshot, \
                self.assertRaises(acceptance.Failure) as caught:
            device.wait(lambda ns: False)
        self.assertEqual(str(caught.exception), "ui_timeout")
        screenshot.assert_not_called()

    def test_ui_dump_streams_without_a_remote_evidence_file(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        raw = b'<?xml version="1.0"?><hierarchy><node text="secret"/></hierarchy>\nDone'
        with patch.object(device, "adb", return_value=raw) as adb:
            self.assertEqual(len(device.nodes()), 1)
        adb.assert_called_once_with("exec-out", "uiautomator", "dump", "/proc/self/fd/1")

    def test_expired_login_dialog_does_not_require_hidden_status_text(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        nodes = [ET.Element("node", {"text": "\u767b\u5f55 zgotify"})]
        with patch.object(device, "nodes", return_value=nodes), patch.object(device, "tap") as tap:
            device.reauthenticate()
        tap.assert_not_called()

    def test_absent_reverse_after_removal_error_is_safe(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        with patch.object(device, "adb", side_effect=[acceptance.Failure("adb_failed"), b""]):
            device.remove_reverse()

    def test_reverse_still_present_after_removal_is_a_failure(self):
        device = acceptance.Device("adb", "emulator-5580", Path("unused"))
        with patch.object(device, "adb", side_effect=[b"", b"host tcp:18090 tcp:18090"]):
            with self.assertRaises(acceptance.Failure):
                device.remove_reverse()


class StdinTests(unittest.TestCase):
    def test_secret_pipe_is_read_without_logging(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"test-only-password12\n")
            with os.fdopen(read_fd, "r") as stream, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(acceptance.read_admin_password(stream), "test-only-password12")
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        finally:
            os.close(write_fd)

    def test_regular_files_and_terminals_are_refused(self):
        stream = Mock()
        for mode in (stat.S_IFREG, stat.S_IFCHR):
            with patch.object(acceptance.os, "fstat", return_value=Mock(st_mode=mode)), \
                    self.assertRaises(acceptance.Failure):
                acceptance.read_admin_password(stream)

    def test_pipe_without_input_has_a_timeout(self):
        with patch.object(acceptance.os, "fstat", return_value=Mock(st_mode=stat.S_IFIFO)), \
                patch.object(acceptance.select, "select", return_value=([], [], [])), \
                self.assertRaises(acceptance.Failure) as caught:
            acceptance.read_admin_password(Mock())
        self.assertEqual(str(caught.exception), "stdin_timeout")

    def test_invalid_argv_is_not_echoed(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(acceptance.main(["--password", "secret"]), 1)
        self.assertEqual(stdout.getvalue(), "FAIL arguments\n")
        self.assertEqual(stderr.getvalue(), "")


class TransportTests(unittest.TestCase):
    def test_http_errors_discard_response_bodies(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 401
        api = acceptance.API(acceptance.ORIGIN)
        with patch.object(acceptance.http.client, "HTTPConnection", return_value=connection), \
                self.assertRaises(acceptance.Failure) as caught:
            api.request("GET", "/v1/me", token="secret")
        response.read.assert_not_called()
        connection.close.assert_called_once()
        self.assertEqual(acceptance.failure_code(caught.exception), "http_status")

    def test_redirect_cannot_forward_credentials(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        with patch.object(acceptance.http.client, "HTTPConnection", return_value=connection) as factory:
            with self.assertRaises(acceptance.Failure):
                acceptance.API(acceptance.ORIGIN).request("GET", "/v1/me", token="secret")
        self.assertEqual(factory.call_count, 1)
        factory.assert_called_with("127.0.0.1", 18090, timeout=15)

    def test_exact_state_rejects_wrong_version_and_extra_side_effects(self):
        expected = {"read": True, "hidden": False, "favorite": False, "version": 1}
        acceptance.require_state(dict(expected), True, False, False, 1)
        for change in ({"version": 2}, {"hidden": True}, {"read": 1}):
            with self.subTest(change=change), self.assertRaises(acceptance.Failure):
                acceptance.require_state(dict(expected, **change), True, False, False, 1)


class SetupIdentityTests(unittest.TestCase):
    class CreationBoundaryReached(Exception):
        pass

    def make_harness(self, person):
        args = acceptance.parse_args(["--api", acceptance.ORIGIN, "--adb", "adb",
                                     "--serial-a", "emulator-5580", "--serial-b", "emulator-5582",
                                     "--output", "unused", "--allow-isolated-test-mutations",
                                     "--admin-password-stdin"])
        harness = acceptance.Harness(args)
        harness.api = Mock()
        # Stop at the first mutation boundary, after the real setup/login guards.
        harness.api.request.side_effect = [
            {"status": "ok", "version": "0.2.0-internal"},
            {"token": "test-admin-session", "expires_at": "2026-11-13T00:00:00Z",
             "person": dict({"id": 1, "username": "admin", "display_name": "Administrator",
                             "admin": True}, **person)},
            self.CreationBoundaryReached(),
        ]
        return harness

    def test_setup_logs_in_as_api_admin_before_creating_accounts(self):
        harness = self.make_harness({})
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(self.CreationBoundaryReached):
            harness.setup("test-only-admin-password")
        calls = harness.api.request.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1].args, ("POST", "/v1/login"))
        self.assertEqual(calls[1].kwargs["body"], {
            "username": "admin", "password": "test-only-admin-password",
            "device_name": "deployed-acceptance-api"})
        self.assertEqual(calls[2].args, ("POST", "/v1/admin/users"))
        self.assertEqual(calls[2].kwargs["token"], "test-admin-session")
        self.assertEqual(harness.budget.count("admin"), 1)
        self.assertEqual(harness.budget.count("root"), 0)
        self.assertEqual(harness.sessions, ["test-admin-session"])

    def test_setup_rejects_wrong_admin_identity_before_mutations(self):
        for person in ({"username": "root"}, {"admin": False}, {"admin": 1}):
            with self.subTest(person=person):
                harness = self.make_harness(person)
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(acceptance.Failure) as caught:
                    harness.setup("test-only-admin-password")
                self.assertEqual(str(caught.exception), "identity_mismatch")
                self.assertEqual([call.args for call in harness.api.request.call_args_list],
                                 [("GET", "/healthz"), ("POST", "/v1/login")])
                self.assertEqual(harness.accounts, [])
                self.assertEqual(harness.sessions, ["test-admin-session"])

    def test_unverified_creation_id_cannot_be_used_by_cleanup(self):
        for created_id, identity_override in ((777, {}), (101, {"username": "unrelated"}),
                                              (101, {"admin": True})):
            with self.subTest(created_id=created_id, identity_override=identity_override):
                harness = self.make_harness({})
                accounts = list(acceptance.new_accounts())
                alice = accounts[0]
                verified_person = {"id": 101, "username": alice.username,
                                   "display_name": "Synthetic Alice", "admin": False}
                harness.api.request.side_effect = [
                    {"status": "ok", "version": "0.2.0-internal"},
                    {"token": "test-admin-session", "person": {
                        "id": 1, "username": "admin", "admin": True}},
                    {"id": created_id},
                    {"token": "test-alice-session", "person": verified_person},
                    {"person": dict(verified_person, **identity_override)},
                ]
                with patch.object(acceptance, "new_accounts", return_value=accounts), \
                        contextlib.redirect_stdout(io.StringIO()), self.assertRaises(acceptance.Failure) as caught:
                    harness.setup("test-only-admin-password")
                self.assertEqual(str(caught.exception), "identity_mismatch")
                unverified_id = alice.person_id
                disabled_paths = []

                def cleanup_request(method, path, **kwargs):
                    if (method, path) == ("POST", "/v1/login"):
                        self.assertEqual(kwargs["body"]["username"], alice.username)
                        return {"token": "test-recovered-session", "person": verified_person}
                    if method == "PATCH":
                        disabled_paths.append(path)
                    return {"ok": True}

                harness.api.request.side_effect = cleanup_request
                self.assertEqual(harness.cleanup(), [])
                self.assertEqual(unverified_id, 0)
                self.assertEqual(disabled_paths, ["/v1/admin/users/101"])
                self.assertNotIn("/v1/admin/users/777", disabled_paths)


class WorkflowSequenceTests(unittest.TestCase):
    class OfflineBoundaryReached(Exception):
        pass

    def make_harness(self):
        args = acceptance.parse_args(["--api", acceptance.ORIGIN, "--adb", "adb",
                                     "--serial-a", "emulator-5580", "--serial-b", "emulator-5582",
                                     "--output", "unused", "--allow-isolated-test-mutations",
                                     "--admin-password-stdin"])
        harness = acceptance.Harness(args)
        harness.api = Mock()
        harness.api.request.return_value = {"person": {"id": 101}}
        harness.accounts = list(acceptance.new_accounts())
        harness.accounts[0].person_id = 101
        harness.accounts[1].person_id = 102
        harness.topic_id = 301
        harness.message_ids = [501, 502]
        harness.titles = ["Synthetic-1", "Synthetic-2"]
        harness.checks = ["backend_ready_02"]
        harness.devices = [Mock(spec=acceptance.Device), Mock(spec=acceptance.Device)]
        for device in harness.devices:
            device.profile_xml.return_value = b"<map/>"
        # The real workflow/ordering runs; other tests cover API state and identity guards.
        harness.setup = Mock()
        harness.state = Mock()
        harness.topic_membership = Mock()
        return harness

    def test_offline_inbox_requires_restart_after_reverse_removal(self):
        harness = self.make_harness()
        a, b = harness.devices

        def stop_at_offline_inbox(*args, **kwargs):
            if kwargs.get("offline"):
                raise self.OfflineBoundaryReached()

        a.inbox.side_effect = stop_at_offline_inbox
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(self.OfflineBoundaryReached):
            harness.workflow("test-only-admin-password")
        self.assertEqual(harness.phase, "offline_favorite_durable_replayed_once")
        calls = a.method_calls
        removal = calls.index(call.remove_reverse())
        self.assertEqual(calls[removal:], [call.remove_reverse(), call.restart(), call.inbox(2, offline=True)])
        b.remove_reverse.assert_not_called()

    def disabled_session_harness(self, stale_rows):
        harness = self.make_harness()
        b = harness.devices[1]
        visible = {"modal": False, "dismissed": False, "inspected": False}

        def show_login():
            visible["modal"] = True

        def click(value, *args, **kwargs):
            if value == "\u53d6\u6d88" and visible["modal"]:
                visible["modal"] = False
                visible["dismissed"] = True

        def nodes():
            if harness.phase != "disabled_old_sessions_rejected":
                return []
            if visible["modal"]:
                return [ET.Element("node", {"text": "\u767b\u5f55 zgotify"})]
            visible["inspected"] = True
            return [ET.Element("node", {"text": harness.titles[0]})] if stale_rows else []

        b.reauthenticate.side_effect = show_login
        b.click.side_effect = click
        b.nodes.side_effect = nodes
        b.absent.side_effect = lambda titles: acceptance.Device.absent(b, titles)
        return harness, visible

    def test_disabled_session_dismisses_login_before_inspecting_inbox(self):
        harness, visible = self.disabled_session_harness(stale_rows=False)
        with contextlib.redirect_stdout(io.StringIO()):
            harness.workflow("test-only-admin-password")
        self.assertTrue(visible["dismissed"])
        self.assertTrue(visible["inspected"])
        self.assertIn("disabled_old_sessions_rejected", harness.checks)

    def test_login_modal_cannot_hide_stale_rows_and_produce_a_pass(self):
        harness, visible = self.disabled_session_harness(stale_rows=True)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(acceptance.Failure) as caught:
            harness.workflow("test-only-admin-password")
        self.assertEqual(str(caught.exception), "message_mismatch")
        self.assertTrue(visible["dismissed"])
        self.assertTrue(visible["inspected"])
        self.assertNotIn("disabled_old_sessions_rejected", harness.checks)


class CleanupTests(unittest.TestCase):
    def make_harness(self):
        args = acceptance.parse_args(["--api", acceptance.ORIGIN, "--adb", "adb",
                                     "--serial-a", "emulator-5580", "--serial-b", "emulator-5582",
                                     "--output", "unused", "--allow-isolated-test-mutations",
                                     "--admin-password-stdin"])
        harness = acceptance.Harness(args)
        harness.api = Mock()
        harness.admin_token = "admin-secret"
        harness.sessions = ["alice-secret", "bob-secret", "admin-secret"]
        harness.accounts = list(acceptance.new_accounts())
        harness.accounts[0].person_id = 101
        harness.accounts[1].person_id = 102
        harness.reverse_devices = list(harness.devices)
        for device in harness.devices:
            device.adb = Mock()
        return harness

    def test_cleanup_continues_after_every_failure_and_revokes_admin_last(self):
        harness = self.make_harness()
        harness.api.request.side_effect = RuntimeError("admin-secret")
        harness.devices[0].adb.side_effect = RuntimeError("device secret")
        failures = harness.cleanup()
        calls = harness.api.request.call_args_list
        self.assertEqual([c.args[:2] for c in calls[:2]], [
            ("PATCH", "/v1/admin/users/101"), ("PATCH", "/v1/admin/users/102")])
        revokes = [c for c in calls if c.args[:2] == ("DELETE", "/v1/session")]
        self.assertEqual([c.kwargs["token"] for c in revokes],
                         ["alice-secret", "bob-secret", "admin-secret"])
        harness.devices[1].adb.assert_any_call("reverse", "--remove", "tcp:18090")
        self.assertTrue(failures)
        self.assertNotIn("secret", json.dumps(failures))

    def test_disabled_sessions_401_are_terminal_cleanup_success(self):
        harness = self.make_harness()
        harness.api.request.return_value = {"ok": True}
        for device in harness.devices:
            device.adb.return_value = b""
        self.assertEqual(harness.cleanup(), [])

    def test_disable_false_acknowledgement_fails_cleanup(self):
        harness = self.make_harness()
        harness.api.request.return_value = {"ok": False}
        for device in harness.devices:
            device.adb.return_value = b""
        self.assertIn("disable_account", harness.cleanup())

    def test_created_but_unidentified_user_is_recovered_for_cleanup(self):
        harness = self.make_harness()
        account = harness.accounts[0]
        account.creation_started = True
        account.person_id = 0
        harness.api.request.return_value = {"ok": True}
        for device in harness.devices:
            device.adb.return_value = b""
        with patch.object(harness, "login_api", return_value={"token": "recovered", "person": {
                "id": 101, "username": account.username, "admin": False}}):
            self.assertEqual(harness.cleanup(), [])
        harness.api.request.assert_any_call("PATCH", "/v1/admin/users/101",
                                            token="admin-secret", body={"enabled": False})

    def test_cleanup_failure_cannot_be_reported_as_pass(self):
        harness = self.make_harness()
        harness.checks = list(acceptance.CHECKS)
        with patch.object(harness, "preflight"), patch.object(harness, "workflow"), \
                patch.object(harness, "cleanup", return_value=["disable_account"]), \
                patch.object(harness, "write_report") as write, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(harness.execute("secret"), 1)
        self.assertEqual(write.call_args.args[0]["status"], "FAIL")

    def test_exception_still_cleans_and_writes_sanitized_failure(self):
        harness = self.make_harness()
        with patch.object(harness, "preflight"), \
                patch.object(harness, "workflow", side_effect=RuntimeError("secret body")), \
                patch.object(harness, "cleanup", return_value=[]) as cleanup, \
                patch.object(harness, "write_report") as write, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(harness.execute("secret"), 1)
        cleanup.assert_called_once()
        self.assertNotIn("secret", json.dumps(write.call_args.args[0]) + stdout.getvalue())

    def test_partial_checks_cannot_pass_even_without_an_exception(self):
        harness = self.make_harness()
        with patch.object(harness, "preflight"), patch.object(harness, "workflow"), \
                patch.object(harness, "cleanup", return_value=[]), patch.object(harness, "write_report") as write, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(harness.execute("secret"), 1)
        self.assertEqual(write.call_args.args[0]["status"], "FAIL")

    def test_bad_api_state_stops_workflow_before_check_success(self):
        harness = self.make_harness()
        harness.message_ids = [501, 502]
        harness.titles = ["Synthetic-1", "Synthetic-2"]
        harness.topic_id = 301
        harness.checks = ["backend_ready_02"]
        harness.devices = [Mock(), Mock()]
        harness.api.request.return_value = {"message": {"id": 501, "topic_id": 301,
            "state": {"read": False, "hidden": False, "favorite": False, "version": 0}}}
        with patch.object(harness, "setup"), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(acceptance.Failure):
            harness.workflow("secret")
        self.assertEqual(harness.checks, ["backend_ready_02", "alice_same_person_two_devices"])


if __name__ == "__main__":
    unittest.main()
