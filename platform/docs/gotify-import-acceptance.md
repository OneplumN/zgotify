# Gotify Import Acceptance: 2026-11-12

## Result And Scope

The isolated ECS now runs zgotify API `0.2.0-internal`, dedicated MySQL 8.4.13,
an independent official Gotify 2.9.1 fixture and a REST import worker. Production
Gotify/iGotify, the four-ECS ALB deployment and existing websites were not changed.
No GitHub release, Android APK update or new public listener was made.

The real fixture-to-personal-inbox path passed:

1. Created 205 real Gotify messages containing UTF-8 text, Markdown extras and
   priorities 8/9. Imported three pages: 100 + 100 + 5.
2. Read all 205 from three authenticated sessions (Alice A, Alice B, Bob).
   Alice's read/hidden/favorite state synchronized across her sessions; Bob's
   state remained independent. Stale state writes returned 409.
3. With the one-shot importer exited, sent 17 additional upstream messages.
   A fresh importer process recovered exactly those 17; total became 222.
4. Ran another full sweep. It created zero additional messages. SQL verified
   222 import metadata records, 222 distinct message IDs and 222 outbox rows.
5. Repeated the personal-state and upstream-preservation checks with 222 messages.
   The upstream messages remained present; personal hiding never deleted them.
6. Started continuous import, restarted its container and checked the database
   again. All three counts remained 222; subsequent scans created zero records.

These are real HTTP/MySQL checks using simulated device sessions, not physical
Android devices. Outbox rows remain pending; phone push was not exercised.

## Other Verification

- `go test -race -count=1 ./...` and `go vet ./...` passed locally.
- The Python acceptance harness passed 19 local unit tests.
- `TestMySQLGotifyImport` passed against the NEW `zgotify_test_import_v2`
  schema on MySQL 8.4.13 in 3.32 seconds. It exercised populated v1 -> v2 migration,
  migration rerun, two-store lease contention, expiry/takeover fencing,
  stale cursor rejection, reopened-store cursor recovery, late lower-ID recovery,
  replay deduplication, metadata preservation and transactional conflict rollback.
- Final source review found no remaining confirmed P1/P2 issue in the import slice.
  This is not a comprehensive production security audit.
- All four test containers were running with no OOM kill and zero automatic
  restarts at the final inspection. API health returned `0.2.0-internal`.
- Three existing, unrelated HTTPS sites each returned 200 through local Nginx
  with TLS verification. Their domains are omitted from this public report.
  This is a local-origin check, not external ALB acceptance.

The integration schema is now populated. Do not rerun that fresh-schema test on
it or clear it automatically. A new run needs a new explicitly provisioned test
schema and credentials. Existing `zgotify_test_store_v1` evidence is preserved.

## Runtime And Artifacts

All remote paths below are on the authorized test ECS, under `/opt/zgotify-test`.

- API: `127.0.0.1:18090`, image `zgotify-internal:0.2.0-test`.
- Gotify fixture: `127.0.0.1:18091`, independent SQLite named volume.
- MySQL: internal Docker network only; no host port.
- Importer: no host port; same protected app DSN, dedicated upstream client token.
- Credentials and fixture journal: `secrets/`, mode 0700. The journal contains
  generated test-user passwords; do not print, download or publish it.
- Sanitized reports: `reports/gotify-import-205.json`,
  `reports/gotify-import-222.json`, `reports/mysql-gotify-import.log`,
  `reports/gotify-import-first.log`, `reports/gotify-import-catchup.log`,
  `reports/gotify-import-replay.log`.
- Pre-migration app database backup: `backups/pre-gotify-import-v2.sql` (0600).
- Post-acceptance app and integration database backup:
  `backups/after-gotify-import-v2.sql` (0600, 171512 bytes).
- Previous executable: `backups/zgotify-api-0.1.0`; previous image tag preserved.
- Previous Compose environment: `backups/pre-import-compose.env`.

The six sanitized reports were individually inspected against strict field and
log-line allowlists before transfer. A local copy, including their archive, is
under `artifacts/validation-20261112-import/`. These reports contain test counts
and numeric ID mappings, not credentials or message titles/bodies. Database
backups and the credential-bearing fixture journal remain on the test server.

The test users remain enabled for a later phone test, but all acceptance login
sessions were logged out. Use the protected journal on the server to obtain a
test account through a secure operator workflow; do not reuse these accounts in
production. No phone-facing HTTPS endpoint has been provisioned.

## Provenance

Linux amd64 backend SHA-256:
`ca56373cf92a3803c6547363d288fa51b3b0864f19741afb6d7c093fa482c298`

Linux amd64 integration-test binary SHA-256:
`5fc52ce390a0f17729646b0a4fe03da155aaed56244d8f6664e5c0bf9bb310ac`

Official Gotify 2.9.1 amd64 manifest:
`sha256:2d691df89b44cac215f79f065a46b5b7de89c4b3375e4f2c5ca5e45ea6aa924a`

Offline archive SHA-256 (matched before and after transfer):
`cd678731501d30473e941d40c437d51a3fc05e0c25706f15ee6303bd8c84db05`

Actual imported Docker image ID:
`sha256:eac2f300d23302f278ad42745f93677c9d72743414454efca5536a503e705f4b`

## Rollback Boundary

Stop the isolated importer first if investigation is needed. This does not affect
the original Gotify service or existing websites. Keep all named volumes and
protected backups. Do not use `docker compose down -v`.

Schema v2 is additive, but the old API's readiness check accepts only v1. Merely
changing its image tag is not a valid rollback. A rollback requires a separate
restore rehearsal into a new test database using the pre-migration backup and
the previous image. Do not overwrite the current database or delete schema-version
rows as an automatic rollback. Backup creation was checked; restoration has not
been rehearsed in this phase.

## Not Yet Accepted

Production source credentials/endpoints, large-history latency/load, four-ECS
failover, external RDS, Android real-device UX/offline storage, vendor push SDKs,
screen-off/background arrival and visible notification banners remain pending.
This backend milestone must not be described as a stable Android push release.
See `gotify-import.md` for pagination, configuration and content limits.
