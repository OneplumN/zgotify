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
./gradlew :app:assembleDebug :app:lintDebug
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
page refresh is every 15 seconds; manual refresh resets paging. There is no local
message database or offline write queue yet. A failed write is never shown as
success. Notification diagnostics include an explicit local test notification;
they do not imply that remote/background delivery has been implemented.

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
- Live Android UI/Keystore behavior without a connected emulator/device.
- Background delivery, remote push SDKs, offline persistence and Gotify catch-up.
- Four-node worker failover, node-local Gotify event gaps and long-running tests.
- Production deployment, release signing, distribution and migration from old APKs.
