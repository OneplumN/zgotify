# Android 0.3 Deployed Rendering Acceptance

Date: 2026-11-12. Upstream baseline: Gotify Server 2.9.1. This increment deploys
the companion API's additive content-type support to the already authorized
isolated test ECS. Production Gotify/iGotify, ALB, RDS, public listeners, existing
websites and the published legacy APK remain outside scope.

## Deployment Provenance

Backend source is the unchanged `platform/backend` tree from `884ad38`. Built
with Go 1.26.3 for Linux/amd64, CGO disabled, with `-trimpath`.

- API binary SHA-256: `fd939288ba29ca1d8412910d66d803f01de21829814e068338a6211d71cc8d40`.
- API image: `zgotify-internal:0.3.0-render-test`.
- Image ID: `sha256:eea376d136c07d2a0c5cd8f409128aae1ee14a2a610d13cdb64a0e5cc74d277b`.
- Android: `com.zgotify.app.internal`, `0.3.0-internal`, version code 3.
- APK SHA-256: `462609a67a26f2f32249ab420e2b9059b488e17f201a9b237533344f58f33c1f`.

The API's health response remains protocol version `0.2.0-internal`. That string
alone cannot establish which binary is running; use the recorded image/binary
hash. The same Android APK already tested against the local fixture is used here.

Only the API container was recreated. MySQL, Gotify fixture and importer container
IDs/images and their existing volumes were unchanged. The API still binds only
to `127.0.0.1:18090`; MySQL has no host port. No API schema migration was run:
schema versions remain 1/2. The original `.env` and API Compose overlay were
compared byte-for-byte against the new backup and were unchanged.

The previous API binary and a consistent isolated application-database dump are
retained in `/opt/zgotify-test/backups/android-render-20261112/` on the ECS.
The directory is private and the SQL dump is mode 0600. The old API image is also
retained. **Backup restoration has not been tested.**

## Fresh MySQL Integration

`TestMySQLTwoPeopleTwoDevices` passed in 1.82 seconds against a NEW dedicated
`zgotify_test_render_20261112` schema in MySQL 8.4.13. Credentials stayed in a
protected server-side file and the test account only has access to that schema.
The old integration schemas and application database were not reset.

This run includes the new real-SQL checks for direct-ingest plain text, imported
Markdown metadata in list/detail, personal-state isolation, pagination, membership
enforcement and non-disclosure of raw extras. The test schema remains populated
as evidence; do not rerun the empty-schema suite against it or clear it automatically.
Local Go race tests and vet also passed.

## Android Acceptance Status

The Android 0.3 dual-emulator run passed all eight existing deployed checks:
same-person login/read synchronization, different-person state isolation,
cancelled account entry, durable offline favorite/replay exactly once, membership
revocation/cache purge and disabled-session rejection. Cleanup passed. SQL
confirmed synthetic people 12/13 disabled with zero remaining sessions, and both
ADB reverses were removed. New synthetic message IDs are 232/233, topic 6.

Completed report and post-login screenshots are retained in the local-only
`artifacts/android-render-deployed-20261112-run2/` directory. The screenshots show
the reused UI with the expected separate read/hidden/favorite state. This suite
still seeds messages through `/v1/ingest`, not Gotify.

The complementary real-Gotify rendering run passed four checks: API readiness,
fixture list/detail identity and content types, the Markdown row, and the plain
literal row. Two messages were sent once to the verified Gotify 2.9.1 fixture;
the existing continuous REST importer, not direct API ingestion, produced them.
Read-only SQL confirmed upstream IDs 223/224 map to canonical IDs 235/234.
The import count increased from 222 to 224; total API messages became 233.

The completed report and screenshot are retained locally in
`artifacts/android-render-gotify-20261112-run2/`. The inspected screenshot shows
bold Markdown without delimiters, literal `**` in the plain-text row, and the
expected 100-row first-page status. The backend test checked all 224 records;
the UI does not claim to have loaded all of them into its first page.

The first real-import UI run failed with `ui_shape` before a profile was stored.
Its person 14 was disabled with zero sessions. Three subsequent UI reads, four
restart/login-visible probes and a clean-profile startup probe did not reproduce
the failure. No app or harness change is claimed to fix that intermittent issue.
The second run used a new person and output directory with sanitized stack/tap
diagnostics; it reused the same two imported messages without reseeding.
Person 15 was disabled with zero sessions, all known sessions were revoked and
the owned ADB reverse was removed. Both run records remain separate.

The first dual-emulator run failed with `ui_shape` while reading the initial
login UI. It is retained as failed evidence. Its synthetic users 10/11 were
disabled with zero remaining sessions, and ADB reverse cleanup passed. Subsequent
login-visible probes succeeded; an exact root cause was not established. A second
run uses fresh output and records only sanitized function names/line numbers on
that error, never credentials or raw login UI dumps.

All four synthetic users 10/11/12/13 from the two runs were confirmed disabled
with zero sessions. Both runs retain their own synthetic messages as evidence;
the original import count stayed 222 after these direct-ingest tests.

After real-import acceptance, SQL confirmed all six people 10-15 disabled with
zero sessions. The private preparation journal is complete with no pending
operation or session. SHA-256 checks confirmed the original import journal and
source configuration were unchanged. Their old 222-message record is historical;
do not rerun the old exact-count verification as if it included this new pair.

133 local Python regression tests passed (96 Android harness + 37 deployment
harness). Independent review found and fixed two test defects before the real
run: total-count versus first-page-count confusion and rejection of clipped
historical rows. Target rows still require strict same-row/package assertions.
The final source-matched Android rebuild also passed all 90 unit tests, debug
assembly and lint (0 errors, 13 warnings). This later rebuild was not installed
for the recorded UI runs; their original APK and SHA-256 above remain retained.
Both disposable AVDs and this test's dedicated SSH tunnel were removed. Existing
host website health checks remained HTTP 200; unrelated connections were left alone.

## Operations and Rollback

Use the explicit API-only overlay when recreating this test API:

```sh
cd /opt/zgotify-test
export ZGOTIFY_ANDROID_API_IMAGE=zgotify-internal:0.3.0-render-test
docker compose -f compose.yaml -f compose.local-mysql.yaml \
  -f compose.gotify-test.yaml -f compose.android-test.yaml config --quiet
docker compose -f compose.yaml -f compose.local-mysql.yaml \
  -f compose.gotify-test.yaml -f compose.android-test.yaml \
  up -d --no-deps --no-build api
```

For a deliberate rollback of THIS additive API change, set
`ZGOTIFY_ANDROID_API_IMAGE=zgotify-internal:0.2.0-android-test` and run the same
two Compose commands. Its expected image ID is
`sha256:917803ebc90d71b35956663eca787d92b30cb82fd26f35ed343abdd8cd683a13`.
Keep all volumes and data; do not run `down -v`. The older API still supports
personal state but lacks `content_type`, so Android falls back to literal text.
This rollback does not delete the new synthetic test data and is not a database
restore rehearsal. Omitting the API-only overlay is not the same rollback.

## Remaining Boundaries

These checks do not establish physical-phone operation, vendor push, background
or Doze notification delivery, four-node failover, external RDS/ALB, release
signing or production readiness. No stable Release or default-branch merge is
part of this increment. Existing production instances remain untouched.
