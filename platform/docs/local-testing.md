# Local Testing and Acceptance

## Backend

Prerequisites: Go 1.26+, a fresh MySQL 8 database, and database credentials. Use a
separate database and account; never point this prototype at Gotify's schema.

```sh
cd backend
go test -race -v ./...
go vet ./...
go build -o zgotify ./cmd/zgotify
```

Configure `ZGOTIFY_MYSQL_DSN` through your local secret environment, for example
`user:password@tcp(127.0.0.1:3307)/zgotify_internal`. Do not commit its actual value.
The server sets connection timeouts, UTC and timestamp parsing. Enable verified
TLS in the DSN when connecting over a network that requires it.

```sh
./zgotify -command migrate
read -s ADMIN_PASSWORD
printf '%s\n' "$ADMIN_PASSWORD" | ./zgotify -command bootstrap -username admin -name Administrator
unset ADMIN_PASSWORD
./zgotify -listen 127.0.0.1:8090
```

Bootstrap only works when the people table is empty. It never resets an existing
administrator. Normal server startup does not migrate the database. The service
defaults to loopback; terminate TLS at a separately configured ALB/test reverse
proxy before exposing it outside the local machine.

To exercise real SQL behavior, supply `ZGOTIFY_TEST_MYSQL_DSN` pointing to a fresh
disposable database whose name starts with `zgotify_test_`, then run:

```sh
go test -race -v ./internal/platform -run TestMySQLTwoPeopleTwoDevices
```

The integration test creates only test identities and data and leaves them for
inspection. It requires an empty database on each run. It verifies two API store
instances sharing sessions, duplicate ingestion, independent personal state,
cross-device reads, conflict handling, membership revocation, logout and disabling.
It does not simulate four ECS, ALB failover or real push providers.

## Android

Prerequisites: JDK 17, Android SDK platform 36, Gradle-wrapper dependencies.

```sh
cd android
./gradlew :app:testDebugUnitTest :app:assembleDebug :app:lintDebug
```

The internal package is `com.zgotify.app.internal`, so it installs alongside the
previous Gotify/zgotify app. It is debug-signed and not an upgrade/release package.
Output: `app/build/outputs/apk/debug/app-debug.apk`.

Use the new API server address, not the Gotify address. On the standard Android
emulator, `http://10.0.2.2:8090` reaches the local backend. HTTP is accepted only
for explicit local hosts in debug builds; other endpoints require HTTPS.

Tokens are encrypted with an Android Keystore AES-GCM key. Passwords are not
persisted. Account profiles are separate, adding an account can be cancelled, and
stale in-flight UI responses are ignored after account changes. Foreground first-
page refresh is every 15 seconds; manual refresh resets paging. Room caches are
partitioned by normalized server URL and authenticated numeric person ID. A
durable queue stores only the personal fields changed locally. Pending edits are
shown separately from acknowledged server state and survive process recreation.
Notification diagnostics include an explicit local test notification;
they do not imply that remote/background delivery has been implemented.

### Offline Policy

- Cached content is available for at most 24 hours since a successful `/me` and
  complete `/topics` authorization check. Expiry hides content but retains queued
  intent. Recheck on every Activity resume, including older pages.
- A valid empty topics snapshot purges all cached messages and queued operations
  for that person. Revoked topic data is removed before any queued PATCH. Offline
  revocation is not instantaneous; app-private storage is not a remote wipe.
- A current-session 401 purges that person's cache and requests reauthentication.
  Stale requests from a replaced Activity/session cannot mutate the current cache.
- Network/429/5xx failures retain intent, with 5-second exponential retry backoff
  capped at 5 minutes. Retries run only during foreground/manual synchronization.
- A 409 fetches current state. If the requested fields already match, acknowledge
  the queue without another mutation (including a lost-success response). Otherwise
  show a conflict. Only explicit user confirmation rebases the touched fields;
  untouched fields on another device are preserved. A second conflict is possible.
- A detail/PATCH 404 discards the inaccessible message and pending operation.
  Other non-transient errors remain visible for explicit handling, not busy retry.
- Cache retention is 1000 non-pending messages per person; pending messages are
  protected. Online pagination is independent of this offline retention limit.
- Tokens remain Keystore-encrypted. Passwords are not persisted. Message bodies
  are in app-private Room storage, not encrypted by an app-specific database key.
  Android backup is disabled. Do not describe this as end-to-end encryption.
- Logout must revoke the server session before removing the local account. A
  failed/offline logout does not claim successful remote revocation.

### Disposable Emulator UI Check

Use an empty dedicated Android 36 emulator, not a personal device. Install the
internal debug APK, then run this loopback-only fixture (Python 3.9+):

```sh
adb -s emulator-5580 install -r app/build/outputs/apk/debug/app-debug.apk
python3 testing/runtime_acceptance.py --adb "$ANDROID_HOME/platform-tools/adb" \
  --serial emulator-5580 --output /tmp/zgotify-android-acceptance
```

The fixture uses synthetic Alice/Bob accounts, a temporary listener on
127.0.0.1:18092 and an ADB reverse tunnel; it removes both at the end. It exercises real Android UI,
Keystore and Room against a simulated API, including service-unavailable recovery,
force-stop/reopen, account cancellation, state isolation, explicit conflicts,
membership removal and logout. It does not test the Go/MySQL deployment or OEM
background delivery. Begin each run with a fresh disposable app data set.

## Manual Core Workflow

1. Bootstrap the administrator and log in using `POST /v1/login`.
2. Create Alice and Bob using `POST /v1/admin/users`; record their returned IDs.
3. Create a topic and grant both people membership using the membership endpoints.
4. Publish a synthetic message using that topic's ingest token.
5. On device A log in as Alice; on device B log in as Alice; verify both can read it.
6. Change read/hidden/favorite on A and refresh B; confirm the same personal state.
7. Switch B to Bob; confirm Alice's state did not alter Bob's copy.
8. Inspect audit and verify actor IDs identify authenticated people, not phone names.
9. Revoke membership and disable an account; confirm subsequent reads/writes fail.
10. Verify cancelled login, expired session, network failure and stale-write 409.

## Not Yet Accepted

- RDS execution. Dedicated-container MySQL and real HTTP acceptance passed;
  see `local-mysql-acceptance.md` for scope and evidence.
- Physical-phone UI/Keystore behavior and two real devices against the deployed API.
- Background delivery and remote push SDKs. REST Gotify catch-up is separately
  accepted in `gotify-import-acceptance.md`, not by these Android tests.
- Four-node worker failover, node-local Gotify event gaps and long-running tests.
- Production deployment, release signing, distribution and migration from old APKs.
