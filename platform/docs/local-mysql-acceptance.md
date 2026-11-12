# Temporary MySQL Container Validation

This report records the initial API 0.1/schema v1 milestone. The deployment has
since advanced to API 0.2/schema v2; see [Gotify import acceptance](gotify-import-acceptance.md)
for the current runtime, additional test schema and rollback requirements.

## Scope

On 2026-11-12 the user changed the immediate test database choice from cloud RDS
to an independent MySQL container on the authorized ECS. This is a disposable
internal test deployment, not RDS or high availability. Existing Gotify/Zabbix
data, ALB listeners, public ports and hosted websites are outside its scope.

The base Compose file still supports a separate database. The local-only overlay
is `deploy/docker/compose.local-mysql.yaml` and must be selected explicitly.

## Isolation and Credentials

- `zgotify_internal`: API/manual-test data, distinct from existing databases.
- `zgotify_test_store_v1`: empty database for the Go integration suite only.
- `zgotify_app`: SELECT, INSERT, UPDATE and DELETE on the API database.
- `zgotify_migrate`: schema privileges on the API database; only mounted in the
  one-shot migration service, not the persistent API container.
- `zgotify_it`: access to the integration database only.
- MySQL has no host port mapping and only joins an internal Docker network.
- API remains bound to host loopback `127.0.0.1:18090`.
- MySQL has a 640 MiB memory limit and a 128 MiB InnoDB buffer pool. These are
  small-test settings, not production sizing. Binary logging and Performance
  Schema are disabled here; this setup provides no database replication/PITR.
- Database passwords are random and file-backed. The parent secret directory
  is mode 700; files mounted for non-root processes are read-only mode 444.
  The initial API administrator password remains in a root-only mode-600 file.
  Host root and Docker administrators can access these secrets.

The official MySQL entrypoint supports `_FILE` password settings and first-start
SQL initialization: https://hub.docker.com/_/mysql . No empty-password mode is used.
Communication within this isolated Docker database network does not enable TLS;
reassess TLS and CA verification before switching to RDS or another host.

## Image Provenance

The ECS could not reach Docker Hub directly. The official Linux/amd64 image was
downloaded on the workstation using the pinned `crane` tool v0.22.1 and transferred
over SSH. No third-party mirror, insecure TLS option or server DNS change is used.

- Official reference: `docker.io/library/mysql:8.4`
- Selected platform manifest: `sha256:72c368d898861f3b3a07af0e6d690400f44f048799ed46822783d9224cc2c86d`
- Image configuration: `sha256:0f1a0ccadda3a1734d1db557e203ec58621d56fa248525016e85fe4f631311ec`
- Docker imported image ID: `sha256:8a6d0a505239a5241ee5d53d31183e7bc4745c9954cd4843596db31c42c855b4`
- Metadata version: `MYSQL_VERSION=8.4.13-1.el9`
- Transferred archive SHA-256: `8134de6abc6a39c745e9bd3fa4ed6020de011b714b207a6a69a9cd0b2992dbb0`

The local overlay requires `ZGOTIFY_MYSQL_IMAGE` to identify the verified imported
image and uses `pull_policy: never`. Record the actual imported image ID before
starting; do not silently resolve another `latest` tag.
`deploy/docker/local-image.env` records the image imported on this ECS. Install it
as `/opt/zgotify-test/.env` only after checking that the same image is present;
it contains Compose selection and the image ID, not database credentials.

## Initialization Corrections Found During Validation

The first real run exposed a privilege-revocation mismatch: the official image
escapes underscores in its database grant, so revoking an unescaped schema grant
failed and later initialization statements did not run. The fix revokes all
privileges of the newly created test application account, then grants only DML
against a literal, escaped schema name. Migration and integration accounts remain
separate. The existing test volume was preserved and its incomplete initialization
was repaired without resetting passwords or removing data.

The health check now requires both the final TCP listener and an authenticated
local-Socket query against the integration schema. A process-level ping alone
had incorrectly reported the partly initialized instance healthy. Root TCP login
is not needed: the pre-existing wildcard root account in this new test container
was locked, and new deployments request only localhost root access.

## First Start

All remote commands below run in `/opt/zgotify-test`. Upload the overlay,
`prepare-local-mysql.sh`, `verify-api.py` and the cross-compiled integration test
binary. The latter is mounted at `tests/platform-mysql.test`:

```sh
# On the workstation, from backend/:
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go test -c \
  -o ../artifacts/platform-mysql.test ./internal/platform
```

The preparation script refuses to replace any existing secret directory or
database volume. Run it once on an empty test deployment; never delete secrets
or volumes to force it past its guard:

```sh
cd /opt/zgotify-test
bash prepare-local-mysql.sh
export COMPOSE_FILE=compose.yaml:compose.local-mysql.yaml
# Set ZGOTIFY_MYSQL_IMAGE to the verified imported image ID before continuing.
docker compose config --quiet
docker compose up -d --wait --wait-timeout 180 database
docker compose run --rm migrate
docker compose run --rm -T --no-deps api \
  -command bootstrap -username admin -name Administrator < secrets/admin_password
docker compose up -d --no-deps api
curl --fail --silent --show-error http://127.0.0.1:18090/healthz
```

Root authentication for manual database checks uses the mounted
`/run/secrets/mysql_admin_cnf` client configuration. Never pass passwords with
`-pPASSWORD`, echo them, or put raw DSNs in commands, logs or screenshots.

## Acceptance Commands

```sh
docker compose run --rm integration
python3 verify-api.py exercise
```

The Go integration suite requires an empty `zgotify_test_*` database. It leaves
evidence, and refuses a subsequent run against that nonempty database. Prepare
another dedicated empty test database/account for repeat runs; do not reset the
API's database.

The HTTP suite creates uniquely named synthetic identities and a notification.
It checks ingestion idempotency, UTF-8 content, personal state isolation, two
sessions for the same person, audit attribution, privilege rejection, revocation
and stale writes. A protected temporary file retains its session fixture for a
controlled restart check:

```sh
docker compose stop api
docker compose restart database
docker compose up -d --wait --wait-timeout 180 database
docker compose up -d --no-deps api
# Wait until /healthz is 200 before continuing.
python3 verify-api.py verify-restart
```

After successful restart checks, the script tests password-change invalidation,
disables generated test identities, logs out its administrator session, removes
the temporary credential fixture and writes a credential-free report under
`reports/`. No failed check is reported as a pass.

Phone/device names in these tests are simulated API sessions, not real phones.
Neither these tests nor a healthy container proves Android background delivery,
notification banners, Gotify import, four-node failover or RDS compatibility.

## Observed Results: 2026-11-12

- Local Go race tests (uncached) and vet passed.
- `TestMySQLTwoPeopleTwoDevices` passed against real MySQL 8.4.13 after the
  initialization correction. Its first failed attempt is retained as evidence.
- HTTP exercise and post-restart checks passed all 10 groups. The tests covered
  actual HTTP requests, separate sessions, database persistence and authorization.
- Both API and database containers were restarted, and pre-restart sessions,
  messages and independent personal state remained valid.
- Password changes invalidated old sessions/passwords; disabling accounts revoked
  active sessions. Generated HTTP test identities were disabled after acceptance.
- The transient HTTP credential fixture was removed on successful completion.
- Credential-free JSON report: `/opt/zgotify-test/reports/api-20261112T031018Z4bb96e.json`.
- Database backup completed at `/opt/zgotify-test/backups/local-test-after-acceptance.sql`
  with mode 600. Backup restoration is not yet tested; do not equate a successful
  dump with a verified restore.
- Runtime inspection confirmed no database host port mappings, an internal-only
  database network, and API binding only to `127.0.0.1:18090`. Neither running
  container reported an OOM kill during this final inspection.
- Local HTTPS regression requests to the existing Fandazi, Malatang and Regex
  sites returned 200 after deployment. Their Nginx configuration was not changed.
- Observed steady memory before restart: approximately 10 MiB for API and 203 MiB
  for MySQL; about 816 MiB host memory remained available after restart. This is
  a small functional test, not a capacity benchmark or long-duration observation.

## Operations and Rollback

Use `docker compose stop api database` to pause this isolated deployment, leaving
its named volume and secrets intact. Start using the same Compose configuration
and secrets. Never use `down -v` or a Docker prune command for rollback.

Restoring the previous Zabbix lab is documented in `docker-testing.md`; stop both
new containers first to release memory. No existing Nginx configuration requires
restoration because it is not modified.

For phone testing, use the SSH tunnel and USB `adb reverse` described in
`docker-testing.md`, or provision a separately approved HTTPS test endpoint.
An internet-facing deployment is not created by this procedure.

The initial administrator is `admin`; retrieve its initial password on the ECS
from `/opt/zgotify-test/secrets/admin_password` using an authorized root session,
not through chat, screenshots or logs. The HTTP test identities are intentionally
disabled; create separate manual-test accounts and memberships before phone
acceptance. Changing the administrator password makes this initial-password file
obsolete and requires updating the acceptance controller's credential separately.
