# Released UI Deployed Acceptance

Baseline: published source commit `884ad38`, Android `0.3.0-internal`, upstream
Gotify Server 2.9.1. The existing isolated test deployment is authorized; no
production service, default branch, public listener or Release may change.

## Task 1: Real Gotify Rendering Harness

Implement explicit operator-run acceptance for the isolated Gotify fixture ->
existing REST importer -> personal API -> Android renderer path. Reuse the
existing deployed harness's guards and device helpers, without changing its eight
checks or weakening them. This is complementary to that suite, not a replacement.

Write scope: new `android/testing/gotify_render_acceptance.py`, its unit tests,
new `deploy/docker/prepare-android-render.py` and its unit tests; narrowly extend
`Device.login` with an optional expected-inbox-count argument only if necessary.
Keep all backend and Android app implementation unchanged.

Remote preparation must run only in `/opt/zgotify-test`, use the existing protected
fixture journal via the established file-permission/origin/source guards, and
target only loopback Gotify `127.0.0.1:18091` / API `127.0.0.1:18090`. Check version
2.9.1 and fixture identity. It may send exactly two new synthetic messages, one
explicit Markdown and one plain with literal markup, through the existing test
application. Do not overwrite the original import journal or replay previous
messages. Persist a new private one-shot journal before mutations; refuse reuse,
record ambiguity and do not automatically retry creates or logins. Poll reads
boundedly until the real continuous importer has produced both canonical IDs.
Do not alter or restart the importer. Log out known preparation API sessions.

For the local UI run, pass a bounded JSON document through stdin only, containing
the initial API admin credential and synthetic expected message metadata; never
include upstream tokens, raw private journals, secrets in argv, evidence or logs.
Create a new synthetic personal account and grant only the verified fixture topic.
Use one clean disposable Android 36 emulator on serial 5580. Validate list/detail
types and exact message identity/body, then verify both the Markdown-rendered row
and plain literal row using same-row lookup. Require an actual post-login screen,
not a login modal or text from another row. Capture only synthetic inbox evidence.
Disable generated accounts, revoke known sessions, remove owned ADB reverse and
fail the run if cleanup fails. Preserve messages/topics as prior test evidence;
do not delete upstream messages or clear unrelated app profiles.

Use TDD for origin/credential/input guards, row rendering assertions, one-shot
preparation and verified cleanup identity. Run local Python tests; no SSH or
emulator actions from the implementation subagent. Do not commit/push/deploy or
spawn more agents. Report changed paths, exact tests/counts and any residual risk.

## Task 2: Deploy and Verify

- [x] Inspect live test container images, mounts, schema and existing-site health.
- [x] Back up isolated API binary/image selection and consistent API DB dump.
- [x] Build and validate replacement API image; recreate only the API.
- [x] Run new real MySQL content-type integration in a fresh dedicated test schema.
- [x] Run the unchanged eight deployed checks using two disposable emulators.
- [x] Run new real Gotify rendering acceptance, inspect screenshots and cleanup.
- [ ] Record precise evidence, limits and rollback; review and sync source to draft PR.

No schema migration of the API database is expected. Health version remains the
existing API protocol version; image/binary SHA identify this content-type build.
New UI verification does not establish vendor push, real-phone background delivery,
RDS/ALB or four-node failover. The test APK is not a production release.
