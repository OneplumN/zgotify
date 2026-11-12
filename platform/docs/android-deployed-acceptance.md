# Deployed API Android Acceptance

Date: 2026-11-12. Gotify compatibility baseline remains **2.9.1**. This stage
connects the independent Android 0.2 app to its real Go API and MySQL database on
the authorized isolated test ECS. It does not replace production Gotify/iGotify.

## Deployment Boundary

Only the isolated API image was replaced. MySQL 8.4.13, the Gotify 2.9.1 fixture,
the importer, their volumes, the original `.env` and existing host websites were
left unchanged. The additive topics/detail endpoints use the existing schema;
no database migration was run. Before replacement, the old API binary and a
consistent dump of the isolated `zgotify_internal` database were backed up in a
private server-side directory. This backup has not been restoration-tested.

- API binary SHA-256:
  `05b5375858816495378e944970d82761504f47b38f82f6a329ff4f85f18a5919`
- API image: `zgotify-internal:0.2.0-android-test`
- Image ID:
  `sha256:917803ebc90d71b35956663eca787d92b30cb82fd26f35ed343abdd8cd683a13`
- Tested APK SHA-256:
  `d6e8e2edddd4d4a8cafb5b7bfc4eff5478e5c9a1e5c0b203b849a76dc7bb44b9`
- APK package: `com.zgotify.app.internal`; version: `0.2.0-internal`.

The initial image could not execute its binary because upload preserved mode
`700`. Build inputs were corrected to `755` for the binary and `644` for the
public CA bundle, then rebuilt. The non-root, network-disabled help check passed
before the API was recreated; subsequent health checks returned version 0.2.0.

The API-only overlay must be included when recreating this deployment:

```sh
cd /opt/zgotify-test
export ZGOTIFY_ANDROID_API_IMAGE=zgotify-internal:0.2.0-android-test
docker compose -f compose.yaml -f compose.local-mysql.yaml \
  -f compose.gotify-test.yaml -f compose.android-test.yaml config --quiet
docker compose -f compose.yaml -f compose.local-mysql.yaml \
  -f compose.gotify-test.yaml -f compose.android-test.yaml \
  up -d --no-deps --no-build api
```

This assumes the reviewed overlay has been installed and the verified local
image has already passed the help check. Never run `down -v` or recreate MySQL
for this API update. Deliberate rollback uses the same command **without**
`compose.android-test.yaml`, selecting the previous image from the unchanged
`.env`. Rollback retains the database and synthetic test data; the old API lacks
the new topics/detail reads, so the Android 0.2 client is not accepted with it.

## Reproduce the UI Test

The harness starts no mock HTTP service. Provide an SSH tunnel from workstation
loopback `18090` to the isolated ECS's loopback `18090`. The API administrator
username is `admin`, not the MySQL/SSH administrator `root`.

Use two NEW disposable Android 36 emulators on serials `emulator-5580` and
`emulator-5582`, with the internal APK installed. The harness refuses a physical
device, an existing account profile, an existing port reverse or a reused output
directory. It does not clear app data automatically. Check deployment provenance
before authorizing mutations: a loopback URL alone is not proof of isolation.

From the project directory, with `ADB` set to the SDK's absolute adb path:

```sh
ssh root@TEST_ECS_HOST 'cat /opt/zgotify-test/secrets/admin_password' |
  python3 -B android/testing/deployed_acceptance.py \
    --api http://127.0.0.1:18090 --adb "$ADB" \
    --serial-a emulator-5580 --serial-b emulator-5582 \
    --output artifacts/android-deployed-NEW-RUN \
    --allow-isolated-test-mutations --admin-password-stdin
```

Use an authenticated SSH connection or key; do not put passwords in arguments
or print the secret file. The password pipe is required. Generated users and
credentials exist only for this run. Cleanup disables these users, checks old
sessions are rejected, revokes known API sessions and removes its ADB reverses.
Cleanup failure makes the run fail. Topics/messages remain because there is no
test-data deletion API. Dispose of the test emulators after collecting evidence;
their app profiles are not cleared by the harness.

The offline scenario removes the emulator's ADB reverse and force-stops/reopens
the app before queuing a change. Removing only the reverse listener can leave an
existing HTTP keep-alive usable; it is not sufficient evidence of an outage.
The queued change must then survive another offline process restart and appear
exactly once in server state after restoring the reverse and refreshing.

Reports contain only synthetic IDs, check codes and counts. Screenshots are
limited to post-login inboxes; raw UI dumps, API bodies and tokens are not saved.
Evidence stays in ignored artifacts, not the public repository. Public CI runs
only the harness's mocked guard tests, not this secret-dependent deployment test.

## Acceptance Status

Two dedicated Android 36 arm64 emulators and the real deployed Go API/MySQL:
**eight checks passed**, including cleanup with no failures:

1. API readiness/version 0.2 and schema availability.
2. The same authenticated person on both Android devices.
3. Read state changed on A and synchronized to B after refresh.
4. A different person's read, favorite and hidden flags remain independent.
5. Cancelling account entry preserves the current profile and inbox.
6. Offline favorite intent survives process restart, then produces one server
   version increment on recovery and no second increment on another refresh.
7. Revoked topic content is removed and stays absent after an offline restart.
8. Disabling an account rejects its old API sessions and state writes; after
   dismissing the reauthentication dialog, the underlying inbox is empty.

This completed run used synthetic person IDs 8/9, topic 4 and messages 228/229.
Earlier failed runs are retained as failed evidence: the first used the wrong API
administrator name, and the second did not terminate existing HTTP connections
before asserting an outage. Neither is counted as a completed acceptance run.
Only the disposable test app profiles were reset between runs, not existing
personal devices. All four synthetic users from the two data-bearing runs were
confirmed disabled with zero remaining sessions. Existing Gotify imports stayed
at 222 rows, and schema versions stayed at 1/2.

Fresh automated checks: 38 deployed-harness guard tests, 19 import-harness tests,
Go race tests for all three packages and `go vet` passed. The review found and
fixed unverified cleanup identity and login-modal masking risks; regression tests
now cover both. This milestone does not change the Android application source or
APK. Android build/unit-test/lint evidence is recorded in the earlier offline
acceptance report and separately rerun by GitHub CI after source synchronization.

Post-login screenshots and the machine-readable completed-run report remain in
ignored local artifacts. API health alone is not the evidence for these results.

## Not Claimed

- Physical phones, vendor push, background/Doze delivery, four-node failover,
  ALB or RDS acceptance.
- Immediate synchronization: this version synchronizes on foreground/manual
  refresh. A disconnected phone cannot receive immediate membership revocation.
- A full Gotify-to-phone run: this harness creates synthetic messages through
  the independent `/v1/ingest` endpoint. The earlier real 2.9.1 import acceptance
  remains separately documented in `gotify-import-acceptance.md`.
- Production changes, a stable release, release signing or default-branch merge.
