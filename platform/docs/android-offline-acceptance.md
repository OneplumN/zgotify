# Android Offline Acceptance

Date: 2026-11-12. Internal Android version: 0.2.0, package
`com.zgotify.app.internal`. This is a separate debug application, not an upgrade
to the legacy client or a stable remote-push release.

## Implemented

- Room cache partitioned by normalized API origin and authenticated person ID.
- Durable read/hidden/favorite changes containing only touched fields; optimistic
  state is labeled pending until server acknowledgement.
- Response-loss reconciliation, explicit 409 resolution, bounded retry and current-
  session invalidation. A late old-session response cannot clear a new queue.
- Server-state version preservation, non-reused in-process mutation revisions,
  raw-page pagination independent of hidden filtering, and bounded retention that
  permits offline operations on a recently fetched older page.
- Membership reconciliation, 24-hour offline visibility limit, explicit logout,
  cancellable account entry, and session-bound dialogs.
- Additive authenticated backend reads: `GET /v1/topics` and
  `GET /v1/messages/{id}`. No database migration is needed for these endpoints.

## Automated Checks

- Android unit tests: **77 passed**, zero failed or skipped. Includes 16 Room
  persistence/transaction tests, 26 synchronization tests, 22 API adapter contract
  tests, seven transport/login tests and six stale-session fencing tests.
- `assembleDebug` and `lintDebug`: passed; zero errors, seven warnings. Warnings
  concern newer dependency versions, Android backup configuration, a redundant
  SDK check and text concatenation. They are not suppressed by a lint baseline.
- Go race tests: all three packages passed. `go vet`: passed. The two real-MySQL
  integration tests are skipped without a fresh dedicated test DSN in this run.
- Existing import acceptance harness unit tests: **19 passed**. This is not a
  rerun of the earlier real Gotify/MySQL import acceptance.

Tests first reproduced the wrong PATCH response wrapper, Unicode length mismatch,
stale-session mutation, revision reuse and hidden/retained-page pagination issues.
The final test run used the corrected implementation. Room storage was tested with
real SQLite through Robolectric, not an in-memory repository substitute.

## Runtime Boundary

The reproducible harness is `android/testing/runtime_acceptance.py`; see
`local-testing.md` for the disposable-emulator command. It uses actual Android UI,
Keystore and Room with a loopback-only HTTP fixture. Test accounts and messages are
synthetic. The fixture returns service-unavailable errors; this is not a cellular
handover, Doze or OEM process-reclamation experiment.

Android 36 arm64 emulator: **seven workflow groups passed**:

1. Alice login and authorized online inbox.
2. Cached content and queued read survive force-stop followed by manual reopen.
3. Service recovery acknowledges one touched-field PATCH, without duplicate mutation.
4. Bob retains independent state; cancelling add-account preserves the active account.
5. Hidden-message conflict remains reachable; explicit rebase preserves the other
   session's favorite flag and submits only the hidden field with the new version.
6. Revoked topic content is purged and remains absent after process restart.
7. Logout removes Alice and preserves Bob's account and cached inbox.

The fixture observed exactly two successful state mutations: read at version 0,
then hidden at version 2 following the simulated other-session change. Screenshots
and detailed machine-readable evidence remain in ignored local artifacts, not in
the public repository. Initial harness failures (keyboard coordinates and the
emulator host-gateway path) were corrected; only the completed seven-group run
is counted as acceptance. The final run uses an ADB loopback tunnel.

## Not Claimed

- No physical phone, two real Android devices, RDS, four-node ALB routing or vendor
  push SDK acceptance in this milestone.
- No promise of delivery while backgrounded, force-stopped or screen-off. The
  force-stop test reopens the app and checks persistence, not background delivery.
- No production changes, public release, release signing or default-branch merge.
- The new read endpoints are source changes in this milestone. The earlier ECS
  deployment has not been updated by this test. Deploy the matching backend to
  the isolated test environment before trying this APK against that environment.
- The cache policy cannot immediately revoke content on a disconnected phone.
  Personal state synchronizes on foreground/manual refresh, not in real time.
